"""Versioned private-cache projection, committed by an immutable flash plan.

Hashes establish offline consistency with a trusted job/plan, not archive
authenticity. The original manifest digest still includes the private cache.
"""

import copy
import hashlib
import json
import re
from typing import Any

from .build_artifacts import manifest_digest

CONTRACT = "broker-build-manifest-export-v1"
MAX_EXPORT_BYTES = 1024 * 1024
MAX_CACHE_BYTES = 128 * 1024
_ERROR = "invalid_manifest_export"


def _hex(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def canonical_json(value: Any) -> bytes:
    """UTF-8, sorted keys, compact separators, ASCII escapes, no nonfinite numbers."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def _validate_public_manifest(manifest: Any) -> None:
    if type(manifest) is not dict or type(manifest.get("schema_version")) is not int:
        raise ValueError(_ERROR)
    if manifest["schema_version"] != 1:
        raise ValueError(_ERROR)
    envelope = manifest.get("diagnostic_modeproof")
    if not isinstance(envelope, dict):
        return
    if "cmake_cache" in envelope:
        raise ValueError(_ERROR)
    if "cache_evidence" not in envelope:
        if "requested_dma_mode" in envelope:
            raise ValueError(_ERROR)
        return
    evidence = envelope["cache_evidence"]
    if (
        type(evidence) is not dict
        or set(evidence) != {"retained_private", "bytes", "sha256"}
        or evidence["retained_private"] is not True
        or type(evidence["bytes"]) is not int
        or not 0 < evidence["bytes"] <= MAX_CACHE_BYTES
        or not _hex(evidence["sha256"])
    ):
        raise ValueError(_ERROR)
    proof = envelope["proof"]
    if any(
        value != evidence["sha256"]
        for value in (
            proof["dma_overlap"]["cmake_cache_sha256"],
            proof["diagnostic"]["build_mode"]["cmake_cache_sha256"],
            proof["diagnostic"]["build_measure"]["cmake_cache_sha256"],
        )
    ):
        raise ValueError(_ERROR)


def public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Preserve the existing projection; never return raw cached paths or secrets."""
    result = copy.deepcopy(manifest)
    envelope = result.get("diagnostic_modeproof")
    if isinstance(envelope, dict) and "cmake_cache" in envelope:
        if "cache_evidence" in envelope or type(envelope["cmake_cache"]) is not str:
            raise ValueError(_ERROR)
        raw = envelope.pop("cmake_cache").encode("utf-8")
        envelope["cache_evidence"] = {
            "retained_private": True,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    return result


def validate_manifest_export(export: Any) -> None:
    """Closed envelope schema; malformed new records never select a legacy path."""
    try:
        if (
            type(export) is not dict
            or set(export) != {"schema_version", "contract", "original_manifest_sha256", "manifest"}
            or type(export["schema_version"]) is not int
            or export["schema_version"] != 1
            or export["contract"] != CONTRACT
            or not _hex(export["original_manifest_sha256"])
        ):
            raise ValueError(_ERROR)
        _validate_public_manifest(export["manifest"])
        if len(canonical_json(export)) > MAX_EXPORT_BYTES:
            raise ValueError(_ERROR)
    except (KeyError, TypeError, ValueError, RecursionError, UnicodeError):
        raise ValueError(_ERROR) from None


def manifest_export_sha256(export: Any) -> str:
    validate_manifest_export(export)
    return hashlib.sha256(canonical_json(export)).hexdigest()


def make_manifest_export(manifest: dict[str, Any]) -> dict[str, Any]:
    """Call only after original durable receipt, owner, proof and bytes validate."""
    export = {
        "schema_version": 1,
        "contract": CONTRACT,
        "original_manifest_sha256": manifest_digest(manifest),
        "manifest": public_manifest(manifest),
    }
    validate_manifest_export(export)
    return export


def export_response(manifest: dict[str, Any]) -> dict[str, Any]:
    export = make_manifest_export(manifest)
    return {
        "manifest": export["manifest"],
        "manifest_export": export,
        "manifest_export_sha256": manifest_export_sha256(export),
    }


def parse_manifest_export(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(_ERROR)
            result[key] = value
        return result

    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_EXPORT_BYTES:
            raise ValueError(_ERROR)
        export = json.loads(raw, object_pairs_hook=unique)
        validate_manifest_export(export)
        if not isinstance(export, dict):
            raise ValueError(_ERROR)
        return export
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ValueError(_ERROR) from None


def validate_plan_export(plan: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Recheck present commitments; leave legacy plans/receipts unchanged."""
    if "manifest_export_sha256" not in plan:
        return
    if (
        not _hex(plan["manifest_export_sha256"])
        or plan.get("manifest_sha256") != manifest_digest(manifest)
        or plan["manifest_export_sha256"] != manifest_export_sha256(make_manifest_export(manifest))
    ):
        raise ValueError(_ERROR)
