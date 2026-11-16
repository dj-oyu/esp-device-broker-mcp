"""Typed existing-control selection only; no private upload/path or runtime install."""

import hashlib
from contextlib import nullcontext

import pytest
from pydantic import ValidationError
from test_profile_jobs import register
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.models import PrepareRequest
from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import PrivateInputStore, inventory_digest

BASE = dict(
    job_id="private-selector",
    test="firmware_build",
    project_id="fixture",
    source_receipt="c" * 64,
    private_input_id="synthetic",
    private_input_receipt="a" * 64,
)


@pytest.mark.parametrize(
    "change",
    [
        {"private_input_id": None},
        {"private_input_receipt": None},
        {"project_id": None},
        {"source_receipt": None},
        {"session": "d" * 64},
        {"after": "ABCDEFGH"},
        {"test": "firmware_fetch_source", "source_commit": "e" * 40},
        {
            "test": "firmware_flash",
            "session": "d" * 64,
            "device_id": "fixture",
            "build_job_id": "old",
        },
        {"test": "diagnostics", "session": "d" * 64},
        {"private_input_id": "../private"},
        {"private_input_receipt": "invalid"},
    ],
)
def test_invalid_selector_combinations_refused(change):
    with pytest.raises(ValidationError):
        PrepareRequest(**(BASE | change))


@pytest.mark.parametrize("missing", ["private_input_id", "private_input_receipt"])
def test_partial_pair_refused(missing):
    values = dict(BASE)
    del values[missing]
    with pytest.raises(ValidationError):
        PrepareRequest(**values)


async def test_mcp_selector_preserves_control_owner_and_default_gate(
    registry_service, tmp_path, monkeypatch
):
    service = registry_service
    project = register(service, tmp_path)
    broker = service.resident
    builder = broker.firmware_build
    owner = service.keys["second"].metadata.principal_id
    broker.profile_jobs._sync_settings()
    # Source-receipt service is separately tested. Preserve private receipt ownership here.
    monkeypatch.setattr(broker.firmware_source, "authorize_receipt", lambda *_: None)
    monkeypatch.setattr(builder, "_project", lambda *_: (project, {"commit": "e" * 40}))
    recipe = builder.prepare("fixture", owner, "c" * 64)["fingerprint"]
    data = b"synthetic-only"
    registration = PrivateInputRegistration(
        local_root=tmp_path / "never-read",
        destination="assets",
        source_commit="e" * 40,
        recipe_sha256=recipe,
        inventory_sha256=inventory_digest(
            (InputFile("fixture", len(data), hashlib.sha256(data).hexdigest()),)
        ),
    )
    project.private_inputs["synthetic"] = registration
    inputs = PrivateInputStore(broker._store().db)
    receipt = inputs.ingest(
        registration=registration,
        project_id="fixture",
        input_id="synthetic",
        principal=owner,
        files=(("fixture", data),),
    )
    builder.private_store_factory = lambda: nullcontext(inputs)
    request = {k: v for k, v in BASE.items() if k != "test"} | {
        "kind": "build",
        "private_input_receipt": receipt,
    }
    for who, accepted in (("reader", False), ("first", False), ("second", True)):
        async with connect(service.url, service.keys[who].token) as remote:
            result = await call(remote, "submit", request)
        assert result["ok"] is accepted, (who, result)
        if accepted:
            plan = broker._store().plan(result["plan_hash"])
            assert plan["private_input_id"] == "synthetic"
            assert plan["private_input_receipt"] == receipt
            assert len(plan["private_binding_sha256"]) == 64
            assert plan["build_principal"] == owner
            assert "local_root" not in repr(result) and str(registration.local_root) not in repr(
                result
            )
    assert builder.private_runtime is None


async def test_mcp_refuses_partial_deferred_and_path_selectors(registry_service):
    service = registry_service
    request = {k: v for k, v in BASE.items() if k != "test"} | {"kind": "build"}
    cases = [
        request | {"private_input_receipt": None},
        request | {"after": "ABCDEFGH"},
        request | {"source_receipt": None},
        request | {"private_input_id": "../path"},
        request | {"local_root": "C:/private"},
        request | {"kind": "fetch", "source_commit": "e" * 40},
    ]
    async with connect(service.url, service.keys["second"].token) as remote:
        for case in cases:
            try:
                result = await call(remote, "submit", case)
            except ValueError as error:
                assert str(error) == "MCP request rejected; check the typed arguments"
            else:
                assert result["ok"] is False
    assert service.resident._store().db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_selector_schema_adds_only_two_bounded_fields():
    import json
    from pathlib import Path

    tools = json.loads((Path(__file__).parent / "fixtures/surface_tools_list.json").read_text())
    assert len(tools) == 6
    properties = next(tool for tool in tools if tool["name"] == "submit")["inputSchema"]["$defs"][
        "SubmitRequest"
    ]["properties"]
    selectors = {key: properties[key] for key in ("private_input_id", "private_input_receipt")}
    assert len(json.dumps(selectors, separators=(",", ":"))) <= 500
