import ast
import re
from pathlib import Path

import pytest

from cardputer_mcp.app_wire import Result
from cardputer_mcp.broker import BrokerError
from cardputer_mcp.public_errors import to_public
from cardputer_mcp.public_reasons import (
    BROKER_REASON_CODES,
    PUBLIC_REASON_CODES,
    SUPPRESSED_REASON_CODES,
    public_reason,
)
from cardputer_mcp.server import _result
from cardputer_mcp.source_snapshot import SourceError

SOURCE = Path(__file__).resolve().parents[1] / "src" / "cardputer_mcp"
# Modules on the MCP request path whose ValueError text can reach _result().
REQUEST_PATH_MODULES = (
    "backend",
    "broker",
    "build_artifacts",
    "build_logs",
    "build_process",
    "firmware_build",
    "firmware_flash",
    "flash_models",
    "flash_security",
    "logs",
    "models",
    "profile_jobs",
    "source_cache_lock",
    "store",
)
LITERAL = re.compile(r'raise ValueError\("([a-z0-9_]+)"\)')


def _literal_codes() -> set[str]:
    found: set[str] = set()
    for name in REQUEST_PATH_MODULES:
        found.update(LITERAL.findall((SOURCE / f"{name}.py").read_text(encoding="utf-8")))
    return found


def test_every_request_path_reason_is_classified():
    unclassified = _literal_codes() - PUBLIC_REASON_CODES - SUPPRESSED_REASON_CODES
    assert not unclassified, sorted(unclassified)
    assert not PUBLIC_REASON_CODES & SUPPRESSED_REASON_CODES
    for code in PUBLIC_REASON_CODES | SUPPRESSED_REASON_CODES:
        assert re.fullmatch(r"[a-z][a-z0-9_]{2,80}", code)


def _raiser(exc: Exception):
    def call():
        raise exc

    return call


def test_suppressed_reason_stays_invalid_request():
    for code in sorted(SUPPRESSED_REASON_CODES):
        assert _result(_raiser(ValueError(code))) == {"ok": False, "error": "invalid_request"}


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("/private/path is bad"),
        ValueError("source_fetch_busy: /private/path"),
        ValueError("source_fetch_busy", "extra"),
        ValueError(),
        ValueError(42),
        ValueError("not_in_the_vocabulary"),
    ],
)
def test_unregistered_text_is_flattened(exc):
    assert public_reason(exc) == "invalid_request"
    assert _result(_raiser(exc)) == {"ok": False, "error": "invalid_request"}


def test_source_error_keeps_its_own_public_form():
    assert _result(_raiser(ValueError("source_fetch_busy"))) == {
        "ok": False,
        "error": "source_fetch_busy",
    }
    assert _result(_raiser(SourceError("source_cache_protected")))["error"] == (
        "source_cache_protected"
    )


# Modules whose ValueError text the broker re-wraps as BrokerError(str(exc)).
REWRAPPED_MODULES = (
    "app_events",
    "app_models",
    "app_sessions",
    "app_transport",
    "app_wire",
    "build_locations",
    "build_logs",
    "continuation",
    "firmware_build",
    "firmware_flash",
    "flash_lifecycle",
    "flash_manifest",
)


def _raised_codes(path: Path, only: set[str] | None = None) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args):
            continue
        func = node.exc.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        first = node.exc.args[0]
        if (only is None or name in only) and isinstance(first, ast.Constant):
            if isinstance(first.value, str) and re.fullmatch(r"[a-z0-9_]+", first.value):
                found.add(first.value)
    return found


def test_every_code_that_can_reach_a_client_as_broker_error_is_in_the_vocabulary():
    required = {result.name.lower() for result in Result}
    for path in SOURCE.glob("*.py"):
        required |= _raised_codes(path, {"BrokerError"})
    for name in REWRAPPED_MODULES:
        required |= _raised_codes(SOURCE / f"{name}.py")
    missing = required - BROKER_REASON_CODES - PUBLIC_REASON_CODES
    assert not missing, sorted(missing)
    for code in BROKER_REASON_CODES:
        assert re.fullmatch(r"[a-z][a-z0-9_]{1,80}", code)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (BrokerError("ticket_invalid"), {"error": "ticket_invalid"}),
        (BrokerError("cursor_ahead"), {"error": "cursor_ahead"}),
        (BrokerError("C:/Users/SYNTHETIC_PRIVATE/secret"), {"error": "invalid_request"}),
        (BrokerError("a" * 64), {"error": "invalid_request"}),
        (ValueError("SYNTHETIC free text"), {"error": "invalid_request"}),
        (RuntimeError("not public"), None),
    ],
)
def test_one_gate_maps_failures_to_fixed_public_codes(exc, expected):
    assert to_public(exc) == expected
