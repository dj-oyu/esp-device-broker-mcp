"""Bounded public GitHub commit archives in broker-owned private snapshots."""

import base64
import configparser
import hashlib
import io
import json
import os
import re
import shutil
import stat
import threading
import time
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from uuid import uuid4
from weakref import WeakValueDictionary

from .flash_backup import BackupStore
from .registration_digest import source_digest, source_digests
from .source_cache_lock import LOCK_NAME, CacheLock


def _not_link(path: Path) -> None:
    # Keep every no-follow ancestor check, but avoid constructing a full tuple
    # of Path objects for each visited directory (and for every resolved file).
    candidate = os.fspath(path)
    while True:
        try:
            linked = _path_is_link(candidate)
        except FileNotFoundError:
            pass
        else:
            if linked:
                raise SourceError("source_linked_path_rejected")
        parent = os.path.dirname(candidate) or "."
        if parent == candidate:
            break
        candidate = parent


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


@lru_cache(maxsize=1)
def _windows_attributes_query() -> Callable[[str], int]:
    # Cache only the function binding, never path attributes or scan results.
    import ctypes

    kernel = vars(ctypes)["WinDLL"]("kernel32", use_last_error=True)
    query = kernel.GetFileAttributesW
    query.argtypes = [ctypes.c_wchar_p]
    query.restype = ctypes.c_uint32
    return cast(Callable[[str], int], query)


def _path_is_link(path: str) -> bool:
    if os.name == "nt":
        try:
            attributes = _windows_attributes_query()(path)
        except (AttributeError, KeyError, OSError):
            attributes = 0xFFFFFFFF
        # GetFileAttributesW returns attributes of the link itself. Every
        # reparse point remains rejected, including junctions/mount points.
        # https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getfileattributesw
        if attributes != 0xFFFFFFFF:
            return bool(attributes & 0x400)
    # Preserve normal lstat error semantics (including missing/denied paths),
    # UNC-share-root behavior, and the POSIX no-follow implementation.
    return _linked(os.lstat(path))


LIMITS = {
    "download_bytes": 64 * 1048576,
    "expanded_bytes": 256 * 1048576,
    "files": 100000,
    "operation_seconds": 180,
    "cache_snapshots": 16,
    "cache_bytes": 2 * 1024 * 1048576,
    "submodule_depth": 3,
    "submodule_count": 16,
}

# Allow finite headroom for cold metadata I/O across the 16-snapshot cache.
# This remains a cooperative per-traversal deadline, separate from fetch's 180s.
CACHE_SCAN_SECONDS = 30.0
CACHE_SCAN_ENTRIES = 1_000_000
EVIDENCE_SNAPSHOTS = 4096
EVIDENCE_BYTES = 128 * 1048576
_CACHE_LOCKS: WeakValueDictionary[str, Any] = WeakValueDictionary()
_CACHE_LOCKS_GUARD = threading.Lock()


SOURCE_ERROR_CODES = frozenset(
    {
        "registered_source_repository_required",
        "source_archive_case_alias",
        "source_archive_commit_mismatch",
        "source_archive_duplicate_path",
        "source_archive_link_or_encryption_rejected",
        "source_archive_path_rejected",
        "source_archive_size_mismatch",
        "source_archive_untracked_file",
        "source_cache_full",
        "source_cache_limit",
        "source_cache_root_conflict",
        "source_cache_scan_limit",
        "source_cache_unknown_entry",
        "source_cache_owner_conflict",
        "source_cache_protected",
        "source_cache_references_incomplete",
        "source_cache_evidence_full",
        "source_commit_metadata_mismatch",
        "source_cleanup_unconfirmed",
        "source_disk_reserve_required",
        "source_download_failed",
        "source_download_limit",
        "source_expansion_limit",
        "source_fetch_busy",
        "source_fetch_failed",
        "source_fetch_interrupted",
        "source_fetch_interrupted_or_download_limit",
        "source_file_limit",
        "source_final_commit_not_authorized",
        "source_full_commit_required",
        "source_git_blob_mismatch",
        "source_git_blob_rejected",
        "source_git_tree_metadata_rejected",
        "source_git_tree_mismatch",
        "source_gitlink_path_conflict",
        "source_gitmodules_gitlink_mismatch",
        "source_gitmodules_limit",
        "source_gitmodules_rejected",
        "source_http_rejected",
        "source_idf_project_missing",
        "source_linked_path_rejected",
        "source_log_not_available",
        "source_plan_changed",
        "source_private_acl_confirmation_required",
        "source_private_cache_required",
        "source_receipt_changed",
        "source_receipt_invalid",
        "source_receipt_limit",
        "source_receipt_not_owned",
        "source_redirect_rejected",
        "source_snapshot_changed",
        "source_snapshot_conflict",
        "source_snapshot_unavailable",
        "source_snapshot_evicted_fetch_required",
        "source_submodule_count_limit",
        "source_submodule_depth_or_cycle",
        "source_submodule_or_lfs_not_supported",
        "source_submodule_provenance_rejected",
        "source_windows_path_limit",
        "successful_owned_source_fetch_required",
    }
)


def safe_source_code(value: object) -> str:
    if type(value) is str and len(value) <= 96 and value in SOURCE_ERROR_CODES:
        return value
    return "source_fetch_failed"


class SourceError(ValueError):
    """Allowlisted diagnostics; native exception text never becomes a code."""

    def __init__(self, code: object = "source_fetch_failed") -> None:
        super().__init__(safe_source_code(code))

    @property
    def code(self) -> str:
        return safe_source_code(self.args[0] if len(self.args) == 1 else None)

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {"error": self.code}
        explanations = {
            "source_cache_protected": "No safely reclaimable payload: executable plans, "
            "unfinished jobs, recovery references, or unknown/unowned entries occupy capacity",
            "source_cache_references_incomplete": (
                "Reference evidence is incomplete; no payload reclaimed"
            ),
            "source_cache_evidence_full": (
                "Finite receipt evidence capacity exhausted; receipts preserved"
            ),
            "source_snapshot_evicted_fetch_required": "Receipt retained; explicitly fetch the same "
            "registered commit to restore verified source bytes before preparing a build",
            "source_cache_owner_conflict": (
                "Cache is bound to another state store; no shared-store reclamation"
            ),
        }
        if self.code in explanations:
            result["explanation"] = explanations[self.code]
        if isinstance(self, CacheScanLimit):
            result["scan"] = dict(self.scan)
        return result


class CacheScanLimit(SourceError):
    """Only bounded counters, never paths or incomplete capacity totals."""

    def __init__(self, reason: str, entries: int, elapsed_ms: int) -> None:
        super().__init__("source_cache_scan_limit")
        self.scan = dict(
            reason="entries" if reason == "entries" else "deadline",
            entries=entries,
            elapsed_ms=elapsed_ms,
            max_entries=CACHE_SCAN_ENTRIES,
            max_elapsed_ms=int(CACHE_SCAN_SECONDS * 1000),
            complete=False,
        )


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def registration_digest(registration: Any, *, legacy: bool = False) -> str:
    """Path-independent binding for new plans/receipts (see registration_digest.py)."""
    return source_digest(registration, legacy=legacy)


SUBMODULE_LIMITS = {"depth": 3, "count": 16, "gitmodules_bytes": 65536}


def validate_submodule_provenance(value: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= SUBMODULE_LIMITS["count"]:
        raise SourceError("source_submodule_provenance_rejected")
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "repository",
            "commit",
            "tree_sha1",
            "archive_sha256",
            "source_sha256",
        }:
            raise SourceError("source_submodule_provenance_rejected")
        if (
            not isinstance(item["path"], str)
            or not isinstance(item["repository"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", item["repository"]) is None
            or any(
                not isinstance(item[key], str) or re.fullmatch(r"[a-f0-9]{40}", item[key]) is None
                for key in ("commit", "tree_sha1")
            )
            or any(
                not isinstance(item[key], str) or re.fullmatch(r"[a-f0-9]{64}", item[key]) is None
                for key in ("archive_sha256", "source_sha256")
            )
            or item["path"].casefold() in seen
        ):
            raise SourceError("source_submodule_provenance_rejected")
        safe_name(item["path"])
        seen.add(item["path"].casefold())


def submodule_paths(data: bytes, allowed: list[str]) -> dict[str, str]:
    if len(data) > SUBMODULE_LIMITS["gitmodules_bytes"]:
        raise SourceError("source_gitmodules_limit")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(data.decode("utf-8"))
        if parser.defaults() or len(parser.sections()) > SUBMODULE_LIMITS["count"]:
            raise ValueError
        result: dict[str, str] = {}
        for section in parser.sections():
            if re.fullmatch(r'submodule "[^"\x00-\x1f]{1,128}"', section) is None:
                raise ValueError
            values = dict(parser[section])
            if set(values) != {"path", "url"}:
                raise ValueError
            path, url = values["path"], values["url"]
            safe_name(path)
            match = re.fullmatch(
                r"https://github\.com/([A-Za-z0-9_-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", url
            )
            if match is None or match[1] not in allowed:
                raise ValueError
            if any(
                path.casefold() == other.casefold()
                or path.casefold().startswith(other.casefold() + "/")
                or other.casefold().startswith(path.casefold() + "/")
                for other in result
            ):
                raise ValueError
            result[path] = match[1]
        return result
    except SourceError:
        raise
    except Exception:
        raise SourceError("source_gitmodules_rejected") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise SourceError("source_redirect_rejected")


def download(url: str, deadline: float, live: Callable[[], bool], limit: int) -> bytes:
    # URLs are constructed exclusively from trusted registration and a full SHA.
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "esp-device-broker-source-v1"})
    parts: list[bytes] = []
    total = 0
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise SourceError("source_http_rejected")
            while True:
                if time.monotonic() >= deadline or live() is not True:
                    raise SourceError("source_fetch_interrupted")
                block = response.read(min(65536, limit - total + 1))
                if time.monotonic() >= deadline or live() is not True:
                    raise SourceError("source_fetch_interrupted")
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise SourceError("source_download_limit")
                parts.append(block)
    except SourceError:
        raise
    except Exception:
        raise SourceError("source_download_failed") from None
    return b"".join(parts)


def safe_name(name: str) -> None:
    parts = name.split("/")
    if (
        len(name.encode()) > 1024
        or len(parts) > 32
        or any(
            not part
            or part in (".", "..")
            or part.casefold() == ".git"
            or part.endswith((".", " "))
            or re.search(r'[\\:<>"|?*\x00-\x1f\x7f]', part)
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
            for part in parts
        )
    ):
        raise SourceError("source_archive_path_rejected")


def tree_hash(files: dict[str, dict[str, Any]]) -> str:
    tree: dict[str, Any] = {}
    for path, receipt in files.items():
        branch = tree
        parts = path.split("/")
        for part in parts[:-1]:
            branch = branch.setdefault(part, {})
        branch[parts[-1]] = receipt

    def git_tree(branch: dict[str, Any]) -> bytes:
        records = []
        for name, item in branch.items():
            directory = "git_sha1" not in item
            mode = "40000" if directory else item["mode"]
            sha = git_tree(item) if directory else bytes.fromhex(item["git_sha1"])
            key = name.encode() + (b"/" if directory else b"")
            records.append((key, mode.encode() + b" " + name.encode() + b"\0" + sha))
        data = b"".join(record for _, record in sorted(records))
        return hashlib.sha1(b"tree " + str(len(data)).encode() + b"\0" + data).digest()

    return git_tree(tree).hex()


def file_receipt(data: bytes, mode: str) -> dict[str, Any]:
    return dict(
        bytes=len(data),
        sha256=digest(data),
        mode=mode,
        git_sha1=hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest(),
    )


def extract(
    archive: bytes,
    root: Path,
    prefix: str,
    live: Callable[[], bool],
    published_root: Path | None = None,
    *,
    require_idf: bool = True,
    budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    receipts: dict[str, Any] = {}
    seen: set[str] = set()
    spelling: dict[str, str] = {}
    total = 0
    if budget is None:
        budget = {"expanded": 0, "files": 0, "entries": 0}
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        entries = zipped.infolist()
        budget["entries"] += len(entries)
        if budget["entries"] > LIMITS["files"] * 2:
            raise SourceError("source_file_limit")
        for item in entries:
            if live() is not True:
                raise SourceError("source_fetch_interrupted")
            if not item.filename.startswith(prefix + "/"):
                raise SourceError("source_archive_commit_mismatch")
            name = item.filename[len(prefix) + 1 :].rstrip("/")
            if not name:
                continue
            safe_name(name)
            parts = name.split("/")
            for depth in range(1, len(parts) + 1):
                ancestor = "/".join(parts[:depth])
                folded = ancestor.casefold()
                if folded in spelling and spelling[folded] != ancestor:
                    raise SourceError("source_archive_case_alias")
                spelling[folded] = ancestor
            key = name.casefold()
            if key in seen:
                raise SourceError("source_archive_duplicate_path")
            seen.add(key)
            mode = item.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
                raise SourceError("source_archive_link_or_encryption_rejected")
            target = root / name
            if (
                os.name == "nt"
                and max(len(str(target)), len(str((published_root or root) / name))) > 240
            ):
                raise SourceError("source_windows_path_limit")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if (
                budget["files"] >= LIMITS["files"]
                or not 0 <= item.file_size <= LIMITS["expanded_bytes"] - budget["expanded"]
            ):
                raise SourceError("source_expansion_limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(item) as source, target.open("xb") as output:
                data = source.read(item.file_size + 1)
                if len(data) != item.file_size:
                    raise SourceError("source_archive_size_mismatch")
                if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
                    raise SourceError("source_submodule_or_lfs_not_supported")
                if name.casefold().endswith(".gitattributes") and b"filter=lfs" in data:
                    raise SourceError("source_submodule_or_lfs_not_supported")
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            total += len(data)
            budget["expanded"] += len(data)
            budget["files"] += 1
            receipts[name] = file_receipt(data, "100755" if mode & 0o111 else "100644")
    if require_idf and "CMakeLists.txt" not in receipts:
        raise SourceError("source_idf_project_missing")
    return receipts


class RepositoryArchives:
    """Fixed Gitlink recursion; all repositories share one operation budget."""

    def __init__(
        self,
        transport: Callable[..., bytes],
        registration: Any,
        deadline: float,
        live: Callable[[], bool],
        published_root: Path,
    ) -> None:
        self.transport, self.registration = transport, registration
        self.deadline, self.live, self.published_root = deadline, live, published_root
        self.downloaded = self.count = 0
        self.tree_entries = 0
        self.budget = {"expanded": 0, "files": 0, "entries": 0}
        self.submodules: list[dict[str, Any]] = []

    def get(self, url: str, limit: int) -> bytes:
        remaining = LIMITS["download_bytes"] - self.downloaded
        if remaining <= 0 or self.live() is not True:
            raise SourceError("source_fetch_interrupted_or_download_limit")
        limit = min(limit, remaining)
        data = self.transport(url, self.deadline, self.live, limit)
        if len(data) > limit:
            raise SourceError("source_download_limit")
        self.downloaded += len(data)
        if self.live() is not True:
            raise SourceError("source_fetch_interrupted")
        return data

    def restore_archive(
        self, repository: str, sha: str, root: Path, files: dict[str, Any], path: str
    ) -> dict[str, str]:
        tree = json.loads(
            self.get(
                f"https://api.github.com/repos/{repository}/git/trees/{sha}?recursive=1",
                8 * 1048576,
            )
        )
        entries = tree.get("tree")
        if (
            tree.get("sha") != sha
            or tree.get("truncated") is not False
            or not isinstance(entries, list)
        ):
            raise SourceError("source_git_tree_metadata_rejected")
        self.tree_entries += len(entries)
        if self.tree_entries > LIMITS["files"] * 2:
            raise SourceError("source_git_tree_metadata_rejected")
        regular: dict[str, dict[str, Any]] = {}
        links: dict[str, str] = {}
        seen: set[str] = set()
        spelling: dict[str, str] = {}
        for item in entries:
            if not isinstance(item, dict):
                raise SourceError("source_git_tree_metadata_rejected")
            name, blob_sha, mode = item.get("path"), item.get("sha"), item.get("mode")
            if (
                not isinstance(name, str)
                or not isinstance(blob_sha, str)
                or re.fullmatch(r"[a-f0-9]{40}", blob_sha) is None
                or name.casefold() in seen
            ):
                raise SourceError("source_git_tree_metadata_rejected")
            safe_name(name)
            safe_name(path + "/" + name if path else name)
            seen.add(name.casefold())
            parts = name.split("/")
            for depth in range(1, len(parts) + 1):
                ancestor = "/".join(parts[:depth])
                folded = ancestor.casefold()
                if folded in spelling and spelling[folded] != ancestor:
                    raise SourceError("source_archive_case_alias")
                spelling[folded] = ancestor
            if mode == "040000" and item.get("type") == "tree":
                continue
            if mode == "160000" and item.get("type") == "commit":
                links[name] = blob_sha
                continue
            if (
                mode not in ("100644", "100755")
                or item.get("type") != "blob"
                or type(item.get("size")) is not int
                or not 0 <= item["size"] <= LIMITS["expanded_bytes"]
            ):
                raise SourceError("source_git_tree_metadata_rejected")
            if name == ".gitmodules" and item["size"] > SUBMODULE_LIMITS["gitmodules_bytes"]:
                raise SourceError("source_gitmodules_limit")
            regular[name] = item
        if set(files) - set(regular):
            raise SourceError("source_archive_untracked_file")
        archive_spelling: dict[str, str] = {}
        for number, target in enumerate(root.rglob("*")):
            if number % 256 == 0 and self.live() is not True:
                raise SourceError("source_fetch_interrupted")
            relative = target.relative_to(root).as_posix()
            archive_spelling[relative.casefold()] = relative
        for name, item in regular.items():
            parts = name.split("/")
            for depth in range(1, len(parts) + 1):
                prefix = "/".join(parts[:depth])
                if archive_spelling.get(prefix.casefold(), prefix) != prefix:
                    raise SourceError("source_archive_case_alias")
            current = files.get(name)
            if current is not None and (
                current["git_sha1"] == item["sha"] and current["mode"] == item["mode"]
            ):
                continue
            if self.budget["expanded"] + item["size"] > LIMITS["expanded_bytes"] or (
                current is None and self.budget["files"] >= LIMITS["files"]
            ):
                raise SourceError("source_expansion_limit")
            encoded_limit = item["size"] * 2 + 65536
            response = json.loads(
                self.get(
                    f"https://api.github.com/repos/{repository}/git/blobs/{item['sha']}",
                    encoded_limit,
                )
            )
            if (
                response.get("sha") != item["sha"]
                or response.get("encoding") != "base64"
                or type(response.get("size")) is not int
                or response["size"] != item["size"]
                or not isinstance(response.get("content"), str)
            ):
                raise SourceError("source_git_blob_rejected")
            try:
                data = base64.b64decode(response["content"].replace("\n", ""), validate=True)
            except (ValueError, TypeError):
                raise SourceError("source_git_blob_rejected") from None
            receipt = file_receipt(data, item["mode"])
            if len(data) != item["size"] or receipt["git_sha1"] != item["sha"]:
                raise SourceError("source_git_blob_mismatch")
            if data.startswith(b"version https://git-lfs.github.com/spec/v1") or (
                name.casefold().endswith(".gitattributes") and b"filter=lfs" in data
            ):
                raise SourceError("source_submodule_or_lfs_not_supported")
            target = root / name
            _not_link(target)
            # Tree paths receive the same private-cache and Windows checks as ZIP paths.
            relative_path = target.relative_to(root)
            if (
                os.name == "nt"
                and max(len(str(target)), len(str(self.published_root / path / relative_path)))
                > 240
            ):
                raise SourceError("source_windows_path_limit")
            if self.live() is not True:
                raise SourceError("source_fetch_interrupted")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb" if current is not None else "xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self.budget["expanded"] += len(data)
            self.budget["files"] += int(current is None)
            files[name] = receipt
        return links

    def fetch(
        self,
        repository: str,
        commit: str,
        root: Path,
        path: str = "",
        ancestors: tuple[tuple[str, str], ...] = (),
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        identity = (repository.casefold(), commit)
        if len(ancestors) > SUBMODULE_LIMITS["depth"] or identity in ancestors:
            raise SourceError("source_submodule_depth_or_cycle")
        if path:
            self.count += 1
            if self.count > SUBMODULE_LIMITS["count"]:
                raise SourceError("source_submodule_count_limit")
            safe_name(path)
        metadata = json.loads(
            self.get(f"https://api.github.com/repos/{repository}/git/commits/{commit}", 1048576)
        )
        if (
            metadata.get("sha") != commit
            or re.fullmatch(r"[a-f0-9]{40}", metadata.get("tree", {}).get("sha", "")) is None
        ):
            raise SourceError("source_commit_metadata_mismatch")
        archive = self.get(
            f"https://codeload.github.com/{repository}/zip/{commit}", LIMITS["download_bytes"]
        )
        root.mkdir(parents=True, exist_ok=True)
        own_files = extract(
            archive,
            root,
            repository.split("/")[1] + "-" + commit,
            self.live,
            self.published_root / path,
            require_idf=False,
            budget=self.budget,
        )
        links: dict[str, str] = {}
        configured: dict[str, str] = {}
        if ".gitmodules" in own_files or tree_hash(own_files) != metadata["tree"]["sha"]:
            links = self.restore_archive(repository, metadata["tree"]["sha"], root, own_files, path)
        if not path and "CMakeLists.txt" not in own_files:
            raise SourceError("source_idf_project_missing")
        if ".gitmodules" in own_files:
            configured = submodule_paths(
                (root / ".gitmodules").read_bytes(), self.registration.submodule_repositories
            )
        if set(configured) != set(links):
            raise SourceError("source_gitmodules_gitlink_mismatch")
        git_files = dict(own_files)
        for name, sha in links.items():
            # Reject tracked files, overlapping links, and archive directory aliases.
            prefixes = {
                "/".join(name.split("/")[:depth]) for depth in range(1, len(name.split("/")) + 1)
            }
            for item in root.rglob("*"):
                relative = item.relative_to(root).as_posix()
                if any(
                    relative.casefold() == prefix.casefold() and relative != prefix
                    for prefix in prefixes
                ):
                    raise SourceError("source_archive_case_alias")
                if item.is_file() and (
                    relative == name
                    or relative.startswith(name + "/")
                    or name.startswith(relative + "/")
                ):
                    raise SourceError("source_gitlink_path_conflict")
            target = root / name
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise SourceError("source_gitlink_path_conflict")
            git_files[name] = {"mode": "160000", "git_sha1": sha}
        if tree_hash(git_files) != metadata["tree"]["sha"]:
            raise SourceError("source_git_tree_mismatch")
        evidence = dict(
            repository=repository,
            commit=commit,
            tree_sha1=metadata["tree"]["sha"],
            archive_sha256=digest(archive),
            source_sha256=digest(canonical(own_files)),
        )
        if path:
            self.submodules.append(dict(path=path, **evidence))
        files = dict(own_files)
        for name in sorted(links):
            child_path = path + "/" + name if path else name
            child, _ = self.fetch(
                configured[name], links[name], root / name, child_path, (*ancestors, identity)
            )
            files.update({name + "/" + key: value for key, value in child.items()})
        return files, evidence


class SourceSnapshots:
    def __init__(self, settings: Any, transport: Callable[..., bytes] = download) -> None:
        self.settings, self.transport = settings, transport
        self.uncertain = False
        self.retention: Callable[[], dict[str, Any]] | None = None
        self.publication: Callable[[str], None] | None = None
        self.restore_source: dict[str, Any] | None = None

    @contextmanager
    def cache_guard(self, project_id: str, *, write: bool = False) -> Iterator[None]:
        """Serialize this process's cache users without waiting on another owner.

        Keep the guard through fetch publication and cleanup. Reentrant calls
        such as fetch -> usage stay on their owning worker thread.
        """
        project = self.settings.build_projects.get(project_id)
        registration = getattr(project, "source_repository", None)
        if registration is None:
            raise SourceError("registered_source_repository_required")
        key = os.path.normcase(str(registration.cache_root.resolve()))
        self.registration(project_id)
        with _CACHE_LOCKS_GUARD:
            lock = _CACHE_LOCKS.get(key)
            if lock is None:
                lock = CacheLock()
                _CACHE_LOCKS[key] = lock
        try:
            with lock.hold(registration.cache_root, self.settings.state_dir, write=write):
                yield
        except SourceError:
            raise
        except ValueError as exc:
            raise SourceError(str(exc)) from None

    def registration(self, project_id: str) -> tuple[Any, Any]:
        project = self.settings.build_projects.get(project_id)
        registration = getattr(project, "source_repository", None)
        if project is None or registration is None:
            raise SourceError("registered_source_repository_required")
        root = registration.cache_root.resolve()
        work = project.project_dir.resolve()
        if (
            root.is_relative_to(work)
            or work.is_relative_to(root)
            or root.is_relative_to(self.settings.state_dir.resolve())
        ):
            raise SourceError("source_cache_root_conflict")
        if project.build_root is not None:
            output = project.build_root.resolve()
            if root.is_relative_to(output) or output.is_relative_to(root):
                raise SourceError("source_cache_root_conflict")
        _not_link(registration.cache_root)
        if registration.cache_root.exists() and os.name == "posix":
            info = registration.cache_root.stat()
            if info.st_uid != int(vars(os)["geteuid"]()) or stat.S_IMODE(info.st_mode) != 0o700:
                raise SourceError("source_private_cache_required")
        if os.name == "nt" and registration.private_acl_confirmed is not True:
            raise SourceError("source_private_acl_confirmation_required")
        return project, registration

    def usage(self, project_id: str, *, stop: threading.Event | None = None) -> dict[str, int]:
        with self.cache_guard(project_id):
            return self._usage(project_id, stop)

    def inventory(self, project_id: str, *, stop: threading.Event | None = None) -> dict[str, Any]:
        # One current traversal for both total capacity and individual entries.
        with self.cache_guard(project_id):
            records: list[dict[str, Any]] = []
            usage = self._usage(project_id, stop, records)
            return {"usage": usage, "records": records}

    def _usage(
        self,
        project_id: str,
        stop: threading.Event | None,
        records: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        _, registration = self.registration(project_id)
        root = registration.cache_root
        size = count = pending_count = evidence_count = evidence_bytes = retired_count = 0
        entries = 0
        started = time.monotonic()
        observed = started
        deadline = started + CACHE_SCAN_SECONDS

        def exceeded(reason: str) -> CacheScanLimit:
            return CacheScanLimit(reason, entries, max(0, int((observed - started) * 1000)))

        def check_budget() -> None:
            nonlocal observed
            if stop is not None and stop.is_set():
                raise SourceError("source_fetch_interrupted")
            observed = time.monotonic()
            if observed >= deadline:
                raise exceeded("deadline")

        def check() -> None:
            nonlocal entries
            check_budget()
            entries += 1
            if entries > CACHE_SCAN_ENTRIES:
                raise exceeded("entries")

        check()
        if root.exists():
            with os.scandir(root) as snapshots:
                for snapshot in snapshots:
                    check()
                    snapshot_path = Path(snapshot.path)
                    _not_link(snapshot_path)
                    if snapshot.name == LOCK_NAME:
                        info = snapshot_path.stat(follow_symlinks=False)
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_nlink != 1
                            or info.st_size not in (0, 64)
                        ):
                            raise SourceError("source_cache_unknown_entry")
                        size += info.st_size
                        continue
                    if not stat.S_ISDIR(snapshot.stat(follow_symlinks=False).st_mode):
                        raise SourceError("source_cache_unknown_entry")
                    child_names: set[str] = set()
                    count += 1
                    evidence_count += bool(re.fullmatch(r"[a-f0-9]{64}", snapshot.name))
                    pending_count += bool(re.fullmatch(r"pending-[a-f0-9]{32}", snapshot.name))
                    before_size, before_entries = size, entries
                    pending = [snapshot_path]
                    while pending:
                        # Empty directories still consume traversal time. Check
                        # the deadline and cancellation without counting them
                        # twice against the entry budget.
                        check_budget()
                        directory = pending.pop()
                        # Recheck every traversed directory and its ancestors;
                        # file entries need only one no-follow stat because the
                        # containing directory has just been verified.
                        _not_link(directory)
                        with os.scandir(directory) as children:
                            for item in children:
                                check()
                                if directory == snapshot_path:
                                    child_names.add(item.name)
                                info = item.stat(follow_symlinks=False)
                                if _linked(info):
                                    raise SourceError("source_linked_path_rejected")
                                if stat.S_ISDIR(info.st_mode):
                                    pending.append(Path(item.path))
                                elif stat.S_ISREG(info.st_mode):
                                    # Windows DirEntry.stat omits link counts (0).
                                    # Destructive validation uses Path.stat/lstat.
                                    if info.st_nlink > 1:
                                        raise SourceError("source_linked_path_rejected")
                                    size += info.st_size
                                    if item.name == "receipt.json" and directory == snapshot_path:
                                        evidence_bytes += info.st_size
                                else:
                                    raise SourceError("source_cache_unknown_entry")
                                if size > LIMITS["cache_bytes"]:
                                    raise SourceError("source_cache_limit")
                    evidence_only = self._evidence_only(snapshot_path, child_names)
                    retired_count += evidence_only
                    count -= evidence_only
                    if records is not None and len(records) < 256:
                        records.append(
                            dict(
                                name=snapshot.name,
                                bytes=size - before_size,
                                entries=entries - before_entries + 1,
                                payload_present=not evidence_only,
                            )
                        )
        check_budget()
        return dict(
            snapshot_entries=count + retired_count,
            evicted_snapshots=retired_count,
            snapshots=count,
            bytes=size,
            max_snapshots=LIMITS["cache_snapshots"],
            max_bytes=LIMITS["cache_bytes"],
            pending_snapshots=pending_count,
            evidence_snapshots=evidence_count,
            evidence_bytes=evidence_bytes,
            max_evidence_snapshots=EVIDENCE_SNAPSHOTS,
            max_evidence_bytes=EVIDENCE_BYTES,
            remaining_snapshots=max(0, LIMITS["cache_snapshots"] - count),
            remaining_bytes=max(0, LIMITS["cache_bytes"] - size),
            scan_entries=entries,
            scan_elapsed_ms=max(0, int((observed - started) * 1000)),
        )

    def prepare(self, project_id: str, commit: str, principal: str) -> dict[str, Any]:
        _, registration = self.registration(project_id)
        if not principal or re.fullmatch(r"[a-f0-9]{40}", commit) is None:
            raise SourceError("source_full_commit_required")
        return dict(
            project_id=project_id,
            source_commit=commit,
            source_principal=principal,
            source_repository=registration.github_repository,
            source_registration_sha256=registration_digest(registration),
            limits=LIMITS,
            stages=["source_download", "source_validate", "source_publish"],
            effects=[
                "Fetches a registered public GitHub commit and fixed allowlisted Gitlinks "
                "into a private snapshot",
                "No user worktree overwrite, build, USB, flash, credential or SDK installation",
                "Reuse verified owned sources; automatically reclaim unreferenced payloads "
                "within fixed bounds while retaining receipts and protecting executable plans, "
                "unfinished jobs and recovery evidence",
            ],
        )

    def fetch(
        self, plan: dict[str, Any], live: Callable[[], bool], stage: Callable[[str], None]
    ) -> dict[str, Any]:
        _, registration = self.registration(plan["project_id"])
        BackupStore(
            registration.cache_root,
            windows_private_acl_confirmed=registration.private_acl_confirmed,
        )
        with self.cache_guard(plan["project_id"], write=True):
            return self._fetch(plan, live, stage)

    def _fetch(
        self, plan: dict[str, Any], live: Callable[[], bool], stage: Callable[[str], None]
    ) -> dict[str, Any]:
        project_id, principal = plan["project_id"], plan["source_principal"]
        expected = self.prepare(project_id, plan["source_commit"], principal)
        if any(plan.get(key) != value for key, value in expected.items()):
            raise SourceError("source_plan_changed")
        _, registration = self.registration(project_id)
        self.restore_source = None
        deadline = time.monotonic() + LIMITS["operation_seconds"]

        def valid() -> bool:
            return time.monotonic() < deadline and live() is True

        if self.retention is not None:
            from .source_payloads import admit_source

            reused = admit_source(self, plan, valid, stage)
            if reused is not None:
                return reused
        usage = self.usage(project_id)
        if (
            usage["snapshots"] >= LIMITS["cache_snapshots"]
            or usage["bytes"] + LIMITS["expanded_bytes"] + 32 * 1048576 > LIMITS["cache_bytes"]
        ):
            raise SourceError("source_cache_full")
        if self.restore_source is None and (
            usage["evidence_snapshots"] >= EVIDENCE_SNAPSHOTS
            or usage["evidence_bytes"] + 32 * 1048576 > EVIDENCE_BYTES
        ):
            raise SourceError("source_cache_evidence_full")
        root = BackupStore(
            registration.cache_root,
            windows_private_acl_confirmed=registration.private_acl_confirmed,
        ).directory
        if shutil.disk_usage(root).free < LIMITS["expanded_bytes"] + 512 * 1048576:
            raise SourceError("source_disk_reserve_required")
        pending = root / ("pending-" + uuid4().hex)
        pending.mkdir(mode=0o700)
        try:
            with (pending / "reservation.json").open("xb") as handle:
                handle.write(
                    canonical(
                        {
                            "job_id": plan["job_id"],
                            "project_id": project_id,
                            "principal": principal,
                            "registration_sha256": plan["source_registration_sha256"],
                        }
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            repository, commit = plan["source_repository"], plan["source_commit"]
            stage("source_download")
            source = pending / "source"
            archives = RepositoryArchives(
                self.transport, registration, deadline, valid, root / ("f" * 64) / "source"
            )
            stage("source_validate")
            files, evidence = archives.fetch(repository, commit, source)
            value = dict(
                schema_version=2 if archives.submodules else 1,
                fetch_job_id=plan["job_id"],
                principal=principal,
                project_id=project_id,
                repository=repository,
                commit=commit,
                tree_sha1=evidence["tree_sha1"],
                archive_sha256=evidence["archive_sha256"],
                source_sha256=digest(canonical(files)),
                files=files,
                registration_sha256=plan["source_registration_sha256"],
            )
            if archives.submodules:
                validate_submodule_provenance(archives.submodules)
                value["submodules"] = archives.submodules
                value["root_source_sha256"] = evidence["source_sha256"]
            restoring = self.restore_source
            if restoring is not None:
                for key in (
                    "schema_version",
                    "principal",
                    "project_id",
                    "repository",
                    "commit",
                    "tree_sha1",
                    "source_sha256",
                    "files",
                    "submodules",
                    "root_source_sha256",
                ):
                    if value.get(key) != restoring.get(key):
                        raise SourceError("source_snapshot_changed")
                value = restoring
            encoded = canonical(value)
            if len(encoded) > 32 * 1048576:
                raise SourceError("source_receipt_limit")
            receipt = digest(encoded)
            with (pending / "receipt.json").open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if valid() is not True:
                raise SourceError("source_fetch_interrupted")
            stage("source_publish")
            target = root / receipt
            if restoring is not None:
                if (
                    not self._evidence_only(target)
                    or (target / "receipt.json").read_bytes() != encoded
                ):
                    raise SourceError("source_receipt_changed")
                source.rename(target / "source")
                (pending / "receipt.json").unlink()
                (pending / "reservation.json").unlink()
                pending.rmdir()
            elif target.exists():
                raise SourceError("source_snapshot_conflict")
            else:
                if self.publication is not None:
                    try:
                        self.publication(receipt)
                    except SourceError:
                        raise
                    except Exception:
                        self.uncertain = True
                        raise SourceError("source_cleanup_unconfirmed") from None
                    if valid() is not True:
                        raise SourceError("source_fetch_interrupted")
                (pending / "reservation.json").unlink()
                pending.rename(target)
            return dict(
                **({"submodules": value["submodules"]} if archives.submodules else {}),
                source_receipt=receipt,
                repository=repository,
                commit=commit,
                tree_sha1=value["tree_sha1"],
                source_sha256=value["source_sha256"],
                files=len(files),
                bytes=sum(item["bytes"] for item in files.values()),
            )
        except SourceError:
            raise
        except Exception:
            raise SourceError("source_fetch_failed") from None
        finally:
            try:
                if pending.exists():
                    shutil.rmtree(pending)
            except Exception:
                self.uncertain = True
                raise SourceError("source_cleanup_unconfirmed") from None

    def resolve(self, project_id: str, receipt: str, principal: str) -> tuple[Path, dict[str, Any]]:
        with self.cache_guard(project_id):
            return self._resolve(project_id, receipt, principal)

    def _resolve(
        self, project_id: str, receipt: str, principal: str
    ) -> tuple[Path, dict[str, Any]]:
        _, registration = self.registration(project_id)
        if re.fullmatch(r"[a-f0-9]{64}", receipt) is None:
            raise SourceError("source_receipt_invalid")
        root = registration.cache_root / receipt
        try:
            path = root / "receipt.json"
            _not_link(path)
            if path.stat().st_size > 32 * 1048576:
                raise SourceError("source_receipt_limit")
            raw = path.read_bytes()
            if digest(raw) != receipt:
                raise SourceError("source_receipt_changed")
            value = json.loads(raw)
            if (
                value["schema_version"] not in (1, 2)
                or value["principal"] != principal
                or value["project_id"] != project_id
                or value["registration_sha256"]
                not in source_digests(registration, schema_version=value["schema_version"])
            ):
                raise SourceError("source_receipt_not_owned")
            source = root / "source"
            if (root / "reclaim.json").exists() or not source.exists():
                raise SourceError("source_snapshot_evicted_fetch_required")
            if value["schema_version"] == 2:
                validate_submodule_provenance(value.get("submodules"))
            _not_link(source)
            files: dict[str, dict[str, Any]] = {}
            for item in source.rglob("*"):
                _not_link(item)
                info = item.lstat()
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise SourceError("source_cache_unknown_entry")
                if info.st_nlink != 1:
                    raise SourceError("source_linked_path_rejected")
                name = item.relative_to(source).as_posix()
                entry = value["files"].get(name)
                if entry is None or info.st_size != entry["bytes"] or len(files) >= LIMITS["files"]:
                    raise SourceError("source_snapshot_changed")
                files[name] = file_receipt(item.read_bytes(), entry["mode"])
            if files != value["files"] or digest(canonical(files)) != value["source_sha256"]:
                raise SourceError("source_snapshot_changed")
            return source, value
        except SourceError:
            raise
        except Exception:
            raise SourceError("source_snapshot_unavailable") from None

    @staticmethod
    def _evidence_only(path: Path, names: set[str] | None = None) -> bool:
        if re.fullmatch(r"[a-f0-9]{64}", path.name) is None:
            return False
        if (names if names is not None else {item.name for item in path.iterdir()}) != {
            "receipt.json"
        }:
            return False
        receipt = path / "receipt.json"
        _not_link(receipt)
        info = receipt.stat()
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_size <= 32 * 1048576
            and digest(receipt.read_bytes()) == path.name
        )
