"""Private build output policy primitives; runtime execution remains gated.

Opaque firmware/object bytes can contain original private inputs. They are not
made public by successful compilation or by removing source filenames.
"""

import hashlib
import json
from typing import Any

from .build_logs import BuildLogs
from .private_build_binding import plan_kwargs
from .private_input_contract import _hash

PRIVATE_MANIFEST_KEYS = frozenset(
    {
        "private_build",
        "private_input_id",
        "private_input_receipt",
        "private_binding_sha256",
    }
)
_EVENTS = {
    "preparing": "Private build: preparing",
    "compiling": "Private build: compiling",
    "verifying": "Private build: verifying",
    "failed": "Private build: failed; raw diagnostics withheld",
    "cancelled": "Private build: cancelled",
    "complete": "Private build: complete; artifacts retained privately",
}


class PrivateBuildLogs(BuildLogs):
    """Drop arbitrary child output before RAM, durable tee, console or callbacks.

    Compiler messages may echo source/model bytes without secret-like syntax;
    regex redaction cannot make these lines safe for shared job-log readers.
    """

    def append(self, line: str, stream: str = "stdout", *, console_output: bool = True) -> None:
        return None

    def event(self, code: str) -> None:
        if code not in _EVENTS:
            raise ValueError("private_build_event_invalid")
        super().append(_EVENTS[code], "broker", console_output=False)


def no_private_compiler_cache(env: dict[str, str]) -> dict[str, str]:
    """Return a copy with cache configuration removed; never probe a cache tool.

    The executor must additionally pass --no-ccache and bypass probe/configure/
    statistics. This is not a sandbox against trusted scripts invoking tools.
    """
    result = {
        key: value
        for key, value in env.items()
        if not key.upper().startswith(("CCACHE_", "SCCACHE_"))
        and key.upper()
        not in {"IDF_CCACHE_ENABLE", "CMAKE_C_COMPILER_LAUNCHER", "CMAKE_CXX_COMPILER_LAUNCHER"}
    }
    result.update(IDF_CCACHE_ENABLE="0", CCACHE_DISABLE="1", SCCACHE_DISABLE="1")
    return result


def private_artifact_provenance(
    plan: dict[str, Any],
    *,
    principal: str,
    composition_identity: str,
) -> dict[str, str | int]:
    if not plan_kwargs(plan) or not principal or plan.get("build_principal") != principal:
        raise ValueError("private_build_not_owned")
    for value in (composition_identity, plan["fingerprint"], plan.get("source_receipt")):
        if not isinstance(value, str):
            raise ValueError("private_build_provenance_invalid")
        _hash(value)
    record: dict[str, str | int] = {
        "schema_version": 1,
        "source_receipt": plan["source_receipt"],
        "input_receipt": plan["private_input_receipt"],
        "binding_sha256": plan["private_binding_sha256"],
        "composition_sha256": composition_identity,
        "build_fingerprint": plan["fingerprint"],
    }
    # This is namespace identity, not authorization; artifact lookup must still
    # enforce the principal against the owned successful job/receipt.
    record["output_namespace"] = hashlib.sha256(
        json.dumps(
            {"principal": principal, **record}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return record


def reject_private_export(manifest: dict[str, Any]) -> None:
    # Presence, not truthiness: malformed/null markers never select legacy export.
    if PRIVATE_MANIFEST_KEYS.intersection(manifest):
        raise ValueError("invalid_manifest_export")
