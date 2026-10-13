"""Bounded public GitHub commit archives in broker-owned private snapshots."""

import hashlib
import io
import json
import os
import re
import shutil
import stat
import time
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from .flash_backup import BackupStore

def _not_link(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SourceError("source_linked_path_rejected")

LIMITS = {
    "download_bytes": 64 * 1048576,
    "expanded_bytes": 256 * 1048576,
    "files": 100000,
    "operation_seconds": 180,
    "cache_snapshots": 8,
    "cache_bytes": 2 * 1024 * 1048576,
}


class SourceError(ValueError):
    """Fixed safe diagnostics, never upstream URLs, paths or response bodies."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
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
    if len(name.encode()) > 1024 or len(parts) > 32 or any(
        not part or part in (".", "..") or part.casefold() == ".git" or part.endswith((".", " "))
        or re.search(r'[\\:<>"|?*\x00-\x1f\x7f]', part)
        or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
        for part in parts
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
    return dict(bytes=len(data), sha256=digest(data), mode=mode,
                git_sha1=hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest())


def extract(archive: bytes, root: Path, prefix: str, live: Callable[[], bool], published_root: Path | None = None) -> dict[str, Any]:
    receipts: dict[str, Any] = {}
    seen: set[str] = set()
    spelling: dict[str, str] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        entries = zipped.infolist()
        if len(entries) > LIMITS["files"] * 2:
            raise SourceError("source_file_limit")
        for item in entries:
            if live() is not True:
                raise SourceError("source_fetch_interrupted")
            if not item.filename.startswith(prefix + "/"):
                raise SourceError("source_archive_commit_mismatch")
            name = item.filename[len(prefix) + 1:].rstrip("/")
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
            if os.name == "nt" and max(len(str(target)), len(str((published_root or root) / name))) > 240:
                raise SourceError("source_windows_path_limit")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if len(receipts) >= LIMITS["files"] or not 0 <= item.file_size <= LIMITS["expanded_bytes"] - total:
                raise SourceError("source_expansion_limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(item) as source, target.open("xb") as output:
                data = source.read(item.file_size + 1)
                if len(data) != item.file_size:
                    raise SourceError("source_archive_size_mismatch")
                if data.startswith(b"version https://git-lfs.github.com/spec/v1") or name.casefold().endswith(".gitmodules"):
                    raise SourceError("source_submodule_or_lfs_not_supported")
                if name.casefold().endswith(".gitattributes") and b"filter=lfs" in data:
                    raise SourceError("source_submodule_or_lfs_not_supported")
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            total += len(data)
            receipts[name] = file_receipt(data, "100755" if mode & 0o111 else "100644")
    if "CMakeLists.txt" not in receipts:
        raise SourceError("source_idf_project_missing")
    return receipts


class SourceSnapshots:
    def __init__(self, settings: Any, transport: Callable[..., bytes] = download) -> None:
        self.settings, self.transport = settings, transport

    def registration(self, project_id: str) -> tuple[Any, Any]:
        project = self.settings.build_projects.get(project_id)
        registration = getattr(project, "source_repository", None)
        if project is None or registration is None:
            raise SourceError("registered_source_repository_required")
        root = registration.cache_root.resolve()
        work = project.project_dir.resolve()
        if root.is_relative_to(work) or work.is_relative_to(root) or root.is_relative_to(self.settings.state_dir.resolve()):
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

    def usage(self, project_id: str) -> dict[str, int]:
        _, registration = self.registration(project_id)
        root = registration.cache_root
        size = count = 0
        if root.exists():
            for path in root.iterdir():
                _not_link(path)
                if not path.is_dir():
                    raise SourceError("source_cache_unknown_entry")
                count += 1
                for item in path.rglob("*"):
                    _not_link(item)
                    if item.is_file():
                        size += item.stat().st_size
                    if size > LIMITS["cache_bytes"]:
                        raise SourceError("source_cache_limit")
        return dict(snapshots=count, bytes=size, max_snapshots=8, max_bytes=LIMITS["cache_bytes"])

    def prepare(self, project_id: str, commit: str, principal: str) -> dict[str, Any]:
        _, registration = self.registration(project_id)
        if not principal or re.fullmatch(r"[a-f0-9]{40}", commit) is None:
            raise SourceError("source_full_commit_required")
        return dict(
            project_id=project_id, source_commit=commit, source_principal=principal,
            source_repository=registration.github_repository,
            source_registration_sha256=digest(registration.model_dump_json().encode()),
            limits=LIMITS, stages=["source_download", "source_validate", "source_publish"],
            effects=["Fetches one registered public GitHub commit into a private broker snapshot",
                     "No user worktree overwrite, build, USB, flash, credential or SDK installation",
                     "Bounded cache; no automatic deletion of retained snapshots"],
        )

    def fetch(self, plan: dict[str, Any], live: Callable[[], bool], stage: Callable[[str], None]) -> dict[str, Any]:
        project_id, principal = plan["project_id"], plan["source_principal"]
        expected = self.prepare(project_id, plan["source_commit"], principal)
        if any(plan.get(key) != value for key, value in expected.items()):
            raise SourceError("source_plan_changed")
        _, registration = self.registration(project_id)
        usage = self.usage(project_id)
        if usage["snapshots"] >= 8 or usage["bytes"] + LIMITS["expanded_bytes"] + 32 * 1048576 > LIMITS["cache_bytes"]:
            raise SourceError("source_cache_full")
        root = BackupStore(registration.cache_root, windows_private_acl_confirmed=registration.private_acl_confirmed).directory
        if shutil.disk_usage(root).free < LIMITS["expanded_bytes"] + 512 * 1048576:
            raise SourceError("source_disk_reserve_required")
        deadline = time.monotonic() + LIMITS["operation_seconds"]

        def valid() -> bool:
            return time.monotonic() < deadline and live() is True

        pending = root / ("pending-" + uuid4().hex)
        pending.mkdir(mode=0o700)
        try:
            repository, commit = plan["source_repository"], plan["source_commit"]
            stage("source_download")
            metadata = json.loads(self.transport(
                f"https://api.github.com/repos/{repository}/git/commits/{commit}", deadline, valid, 1048576
            ))
            if metadata.get("sha") != commit or re.fullmatch(r"[a-f0-9]{40}", metadata.get("tree", {}).get("sha", "")) is None:
                raise SourceError("source_commit_metadata_mismatch")
            archive = self.transport(f"https://codeload.github.com/{repository}/zip/{commit}", deadline, valid, LIMITS["download_bytes"])
            source = pending / "source"
            source.mkdir()
            stage("source_validate")
            files = extract(archive, source, repository.split("/")[1] + "-" + commit, valid, root / ("f" * 64) / "source")
            if tree_hash(files) != metadata["tree"]["sha"]:
                raise SourceError("source_git_tree_mismatch")
            value = dict(schema_version=1, fetch_job_id=plan["job_id"], principal=principal,
                         project_id=project_id, repository=repository, commit=commit,
                         tree_sha1=metadata["tree"]["sha"], archive_sha256=digest(archive),
                         source_sha256=digest(canonical(files)), files=files,
                         registration_sha256=plan["source_registration_sha256"])
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
            if target.exists():
                raise SourceError("source_snapshot_conflict")
            pending.rename(target)
            return dict(source_receipt=receipt, repository=repository, commit=commit,
                        tree_sha1=value["tree_sha1"], source_sha256=value["source_sha256"],
                        files=len(files), bytes=sum(item["bytes"] for item in files.values()))
        except SourceError:
            raise
        except Exception:
            raise SourceError("source_fetch_failed") from None
        finally:
            if pending.exists():
                shutil.rmtree(pending)

    def resolve(self, project_id: str, receipt: str, principal: str) -> tuple[Path, dict[str, Any]]:
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
            if value["schema_version"] != 1 or value["principal"] != principal or value["project_id"] != project_id or value["registration_sha256"] != digest(registration.model_dump_json().encode()):
                raise SourceError("source_receipt_not_owned")
            source = root / "source"
            _not_link(source)
            files = {}
            for item in source.rglob("*"):
                _not_link(item)
                if not item.is_file():
                    continue
                name = item.relative_to(source).as_posix()
                entry = value["files"].get(name)
                if entry is None or item.stat().st_size != entry["bytes"] or len(files) >= LIMITS["files"]:
                    raise SourceError("source_snapshot_changed")
                files[name] = file_receipt(item.read_bytes(), entry["mode"])
            if files != value["files"] or digest(canonical(files)) != value["source_sha256"]:
                raise SourceError("source_snapshot_changed")
            return source, value
        except SourceError:
            raise
        except Exception:
            raise SourceError("source_snapshot_unavailable") from None
