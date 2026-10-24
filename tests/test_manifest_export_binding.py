"""Committed exports use the existing owned source/build/flash chain; no hardware."""

import copy
import json

import pytest
from test_flash_policy import env as env

from cardputer_mcp.broker import BrokerError, digest_plan
from cardputer_mcp.firmware_flash import validate_flash_plan
from cardputer_mcp.flash_binding import prepare_binding, validate_binding
from cardputer_mcp.models import BuildLogRequest
from cardputer_mcp.plan_approval import PlanApprovals


async def test_new_flash_commits_durable_export_and_revalidates_projection(env, monkeypatch):
    prepared = env.prepare("export-bound", build_job_id="build-one")
    plan = prepared["plan"]
    result = env.broker.firmware_build.read_logs(
        BuildLogRequest(job_id="build-one"), "alice", project_id="fixture"
    )
    assert plan["manifest_export_sha256"] == result["manifest_export_sha256"]
    assert plan["manifest_sha256"] == result["manifest_export"]["original_manifest_sha256"]
    PlanApprovals._schema(plan)
    binding = prepare_binding(env.broker, plan)
    assert validate_binding(env.broker, binding, plan)

    import cardputer_mcp.firmware_flash as flash

    original = flash.make_manifest_export

    def changed(manifest):
        export = original(manifest)
        export["original_manifest_sha256"] = "0" * 64
        return export

    monkeypatch.setattr(flash, "make_manifest_export", changed)
    before = env.driver.writes
    assert not validate_binding(env.broker, binding, plan)
    with pytest.raises(BrokerError):
        env.execute(prepared)
    assert env.store.job("export-bound") is None and env.driver.writes == before


@pytest.mark.parametrize("damage", ["delete", "wrong", "extra", "null", "uppercase"])
async def test_commitment_damage_cannot_reuse_a_fixed_trusted_plan_hash(env, damage):
    prepared = env.prepare("export-damaged", build_job_id="build-one")
    plan = copy.deepcopy(prepared["plan"])
    if damage == "delete":
        plan.pop("manifest_export_sha256")
    elif damage == "extra":
        plan["manifest_export_other"] = "0" * 64
    else:
        plan["manifest_export_sha256"] = {
            "null": None,
            "bool": True,
            "wrong": "0" * 64,
            "uppercase": "A" * 64,
        }[damage]
    assert digest_plan(plan) != prepared["plan_hash"]
    if damage in ("null", "uppercase"):
        with pytest.raises(ValueError):
            validate_flash_plan(plan)
        with pytest.raises(ValueError):
            env.broker.firmware_flash.prepare_plan(plan)
    before = env.driver.writes
    env.store.db.execute(
        "UPDATE plans SET payload=? WHERE hash=?",
        (json.dumps(plan), prepared["plan_hash"]),
    )
    env.store.db.commit()
    with pytest.raises(BrokerError):
        env.execute(prepared)
    assert env.store.job("export-damaged") is None and env.driver.writes == before
