"""Reclaim only proven, unreferenced payloads; keep immutable receipt evidence."""

import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from .source_snapshot import (
    EVIDENCE_BYTES,
    EVIDENCE_SNAPSHOTS,
    LIMITS,
    SourceError,
    SourceSnapshots,
    _linked,
    _not_link,
    canonical,
    digest,
    file_receipt,
    registration_digest,
)


def metadata(
    snapshots: SourceSnapshots, plan: dict[str, Any], receipt: str, evidence: dict[str, Any]
) -> dict[str, Any] | None:
    if re.fullmatch(r"[a-f0-9]{64}", receipt) is None:
        return None
    _, registration = snapshots.registration(plan["project_id"])
    path = registration.cache_root / receipt / "receipt.json"
    _not_link(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 32 * 1048576:
        raise SourceError("source_receipt_changed")
    raw = path.read_bytes()
    if digest(raw) != receipt:
        raise SourceError("source_receipt_changed")
    value = json.loads(raw)
    proof = next(
        (
            item
            for item in evidence["sources"].get(receipt, [])
            if item["job_id"] == value.get("fetch_job_id")
        ),
        None,
    )
    if proof is None:
        job = evidence["jobs"].get(value.get("fetch_job_id"), {})
        if (
            job.get("state") not in {"failed", "cancelled"}
            or job.get("plan", {}).get("test") != "firmware_fetch_source"
            or job.get("plan", {}).get("source_principal") != plan["source_principal"]
            or job.get("plan", {}).get("project_id") != plan["project_id"]
        ):
            return None  # Uncertain publication never grants a receipt or removal.
        proof = {"plan": job["plan"]}
    expected = proof["plan"]
    if (
        value.get("schema_version") not in (1, 2)
        or value.get("project_id") != plan["project_id"]
        or value.get("principal") != plan["source_principal"]
        or value.get("repository") != registration.github_repository
        or value.get("commit") != expected.get("source_commit")
        or value.get("registration_sha256") != expected.get("source_registration_sha256")
        or expected.get("source_repository") != registration.github_repository
        or value.get("registration_sha256")
        not in {
            registration_digest(registration),
            registration_digest(registration, legacy=True),
        }
    ):
        return None
    return cast(dict[str, Any], value)


def _check_tree(root: Path, files: dict[str, Any] | None, live: Callable[[], bool]) -> int:
    """Validate the remainder after an interrupted removal, never follow links."""
    _not_link(root)
    count = 0
    size = 0
    for path in root.rglob("*"):
        if live() is not True:
            raise SourceError("source_fetch_interrupted")
        count += 1
        if count > LIMITS["files"] * 2:
            raise SourceError("source_file_limit")
        info = path.lstat()
        if _linked(info) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise SourceError("source_linked_path_rejected")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise SourceError("source_cache_unknown_entry")
        size += info.st_size
        if files is not None:
            expected = files.get(path.relative_to(root).as_posix())
            if (
                expected is None
                or info.st_size != expected["bytes"]
                or file_receipt(path.read_bytes(), expected["mode"]) != expected
            ):
                raise SourceError("source_snapshot_changed")
    return size


def _fresh(snapshots: SourceSnapshots, receipt: str, live: Callable[[], bool]) -> dict[str, Any]:
    assert snapshots.retention is not None
    evidence = snapshots.retention()
    if not evidence["complete"]:
        raise SourceError("source_cache_references_incomplete")
    if receipt in evidence["protected"]:
        raise SourceError("source_cache_protected")
    if live() is not True:
        raise SourceError("source_fetch_interrupted")
    return evidence


def _reclaim(
    snapshots: SourceSnapshots,
    plan: dict[str, Any],
    receipt: str,
    value: dict[str, Any],
    live: Callable[[], bool],
    *, dry_run: bool = False,
) -> int:
    _, registration = snapshots.registration(plan["project_id"])
    root = registration.cache_root / receipt
    source, removing, marker = root / "source", root / "reclaiming", root / "reclaim.json"
    expected = {
        "receipt": receipt,
        "project_id": plan["project_id"],
        "principal": plan["source_principal"],
    }
    names = {item.name for item in root.iterdir()}
    payload_bytes = 0
    if names - {"receipt.json", "source", "reclaiming", "reclaim.json"}:
        raise SourceError("source_cache_unknown_entry")
    if marker.exists():
        _not_link(marker)
        if marker.stat().st_nlink != 1 or marker.stat().st_size > 1024 or marker.read_bytes() != canonical(expected):
            raise SourceError("source_cache_unknown_entry")
        if source.exists() and removing.exists():
            raise SourceError("source_cache_unknown_entry")
        if source.exists():
            payload_bytes += _check_tree(source, value["files"], live)
        if removing.exists():
            payload_bytes += _check_tree(removing, value["files"], live)
    else:
        if removing.exists():
            raise SourceError("source_cache_unknown_entry")
        snapshots.resolve(plan["project_id"], receipt, plan["source_principal"])
        payload_bytes = sum(item["bytes"] for item in value["files"].values())
    _fresh(snapshots, receipt, live)
    if dry_run:
        return payload_bytes
    if not marker.exists():
        with marker.open("xb") as handle:
            handle.write(canonical(expected))
            handle.flush()
            os.fsync(handle.fileno())
    _fresh(snapshots, receipt, live)
    try:
        if source.exists():
            source.rename(removing)
        if removing.exists():
            shutil.rmtree(removing)
        marker.unlink()
    except Exception:
        snapshots.uncertain = True
        raise SourceError("source_cleanup_unconfirmed") from None
    return payload_bytes


def _pending(
    snapshots: SourceSnapshots,
    plan: dict[str, Any],
    evidence: dict[str, Any],
    live: Callable[[], bool],
) -> None:
    _, registration = snapshots.registration(plan["project_id"])
    for root in registration.cache_root.iterdir():
        if not re.fullmatch(r"pending-[a-f0-9]{32}", root.name):
            continue
        marker = root / "reservation.json"
        _not_link(marker)
        if not marker.is_file() or marker.stat().st_size > 1024:
            continue  # Legacy/unknown reservations are never guessed to be ours.
        try:
            value = json.loads(marker.read_bytes())
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        if {item.name for item in root.iterdir()} - {"source", "receipt.json", "reservation.json"}:
            continue
        job = evidence["jobs"].get(value.get("job_id"), {})
        if (
            value.get("project_id") != plan["project_id"]
            or value.get("principal") != plan["source_principal"]
            or value.get("registration_sha256") != plan["source_registration_sha256"]
            or job.get("state") not in {"succeeded", "failed", "cancelled"}
            or job.get("plan", {}).get("source_principal") != plan["source_principal"]
            or job.get("plan", {}).get("project_id") != plan["project_id"]
        ):
            continue
        _check_tree(root, None, live)
        _fresh(snapshots, "", live)
        try:
            shutil.rmtree(root)
        except Exception:
            snapshots.uncertain = True
            raise SourceError("source_cleanup_unconfirmed") from None


def admit_source(
    snapshots: SourceSnapshots,
    plan: dict[str, Any],
    live: Callable[[], bool],
    stage: Callable[[str], None],
) -> dict[str, Any] | None:
    assert snapshots.retention is not None
    evidence = snapshots.retention()
    _, registration = snapshots.registration(plan["project_id"])
    candidates = []
    # Successful owned receipt evidence is required even for a no-download reuse.
    for path in registration.cache_root.iterdir():
        if live() is not True:
            raise SourceError("source_fetch_interrupted")
        receipt = path.name
        if not re.fullmatch(r"[a-f0-9]{64}", receipt):
            continue
        value = metadata(snapshots, plan, receipt, evidence)
        if value is None:
            continue
        proofs = evidence["sources"].get(receipt, [])
        if (
            proofs
            and value["commit"] == plan["source_commit"]
            and not (path / "reclaim.json").exists()
        ):
            if snapshots._evidence_only(path):
                snapshots.restore_source = value
            else:
                snapshots.resolve(plan["project_id"], receipt, plan["source_principal"])
                if live() is not True:
                    raise SourceError("source_fetch_interrupted")
                stage("source_reuse_verified")
                return {**proofs[0]["summary"], "reused": True}
        if receipt not in evidence["protected"] and not snapshots._evidence_only(path):
            candidates.append(
                (min((item["finished"] for item in proofs), default=""), receipt, value)
            )
    usage = snapshots.usage(plan["project_id"])
    if snapshots.restore_source is None and (
        usage["evidence_snapshots"] >= EVIDENCE_SNAPSHOTS
        or usage["evidence_bytes"] + 32 * 1048576 > EVIDENCE_BYTES
    ):
        raise SourceError("source_cache_evidence_full")

    def full() -> bool:
        return (
            usage["snapshots"] >= LIMITS["cache_snapshots"]
            or usage["bytes"] + LIMITS["expanded_bytes"] + 32 * 1048576 > LIMITS["cache_bytes"]
        )

    if not full():
        return None
    if not evidence["complete"]:
        raise SourceError("source_cache_references_incomplete")
    # Check disk reserve before any removal: a failed preflight must not evict.
    if shutil.disk_usage(registration.cache_root).free < LIMITS["expanded_bytes"] + 512 * 1048576:
        raise SourceError("source_disk_reserve_required")
    _pending(snapshots, plan, evidence, live)
    usage = snapshots.usage(plan["project_id"])
    # Establish enough eligible capacity before removing any source payload.
    chosen = []
    remaining_count, remaining_bytes = usage["snapshots"], usage["bytes"]
    for _, receipt, value in sorted(candidates, key=lambda item: (item[0], item[1])):
        if (remaining_count < LIMITS["cache_snapshots"]
                and remaining_bytes + LIMITS["expanded_bytes"] + 32 * 1048576 <= LIMITS["cache_bytes"]):
            break
        freed = _reclaim(snapshots, plan, receipt, value, live, dry_run=True)
        chosen.append((receipt, value))
        remaining_count -= 1
        remaining_bytes -= freed
    if (remaining_count >= LIMITS["cache_snapshots"]
            or remaining_bytes + LIMITS["expanded_bytes"] + 32 * 1048576 > LIMITS["cache_bytes"]):
        raise SourceError("source_cache_protected")
    for receipt, value in chosen:
        stage("source_reclaim_unreferenced_payload")
        _reclaim(snapshots, plan, receipt, value, live)
    usage = snapshots.usage(plan["project_id"])
    if full():
        raise SourceError("source_cache_protected")
    return None
