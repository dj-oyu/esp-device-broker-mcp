import re
from pathlib import Path

import pytest

from cardputer_mcp.public_reasons import (
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


def test_public_and_suppressed_do_not_overlap():
    assert not PUBLIC_REASON_CODES & SUPPRESSED_REASON_CODES


def test_public_codes_are_plain_snake_case_without_values():
    for code in PUBLIC_REASON_CODES | SUPPRESSED_REASON_CODES:
        assert re.fullmatch(r"[a-z][a-z0-9_]{2,80}", code)


def _raiser(exc: Exception):
    def call():
        raise exc

    return call


def test_registered_reason_is_returned():
    assert _result(_raiser(ValueError("source_fetch_busy"))) == {
        "ok": False,
        "error": "source_fetch_busy",
    }


@pytest.mark.parametrize("code", sorted(SUPPRESSED_REASON_CODES))
def test_suppressed_reason_stays_invalid_request(code):
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
    assert _result(_raiser(SourceError("source_cache_protected")))["error"] == (
        "source_cache_protected"
    )
