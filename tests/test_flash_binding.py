"""Source/build integrity remains independent of retired execution grants."""

import json

import pytest
from test_flash_policy import env as env

from cardputer_mcp.flash_binding import live_binding, prepare_binding, validate_binding
from cardputer_mcp.broker import BrokerError


async def test_source_binding_uses_real_owned_receipt_without_policy(env):
    prepared = env.prepare("binding", build_job_id="build-one")
    binding = prepare_binding(env.broker, prepared["plan"])
    assert binding.fetch_receipt == env.receipt
    assert live_binding(env.broker, binding, prepared["plan"])
    assert validate_binding(env.broker, binding, prepared["plan"])
    assert env.store.job("binding") is None


@pytest.mark.parametrize("mutation", ["receipt", "fetch_plan", "build_plan", "project"])
async def test_callback_free_binding_rejects_changed_identity(env, mutation):
    prepared = env.prepare("binding", build_job_id="build-one")
    binding = prepare_binding(env.broker, prepared["plan"])
    if mutation == "receipt":
        env.store.db.execute("UPDATE jobs SET diagnostic=NULL WHERE id='fetch-one'")
    elif mutation in ("fetch_plan", "build_plan"):
        job_id = "fetch-one" if mutation == "fetch_plan" else "build-one"
        digest = env.store.job(job_id)["plan_hash"]
        plan = env.store.plan(digest)
        plan["tampered"] = True
        env.store.db.execute(
            "UPDATE plans SET payload=? WHERE hash=?", (json.dumps(plan), digest)
        )
    else:
        project = env.broker.settings.build_projects["fixture"]
        env.broker.settings = env.broker.settings.model_copy(update={
            "build_projects": {"fixture": project.model_copy(update={
                "profile_execution_policy": "RemoteSigned"
            })}
        })
    assert not live_binding(env.broker, binding, prepared["plan"])


async def test_dispatch_rehashes_source_and_artifacts(env):
    prepared = env.prepare("binding", build_job_id="build-one")
    binding = prepare_binding(env.broker, prepared["plan"])
    (env.output / "app.bin").write_bytes(b"tampered")
    assert not validate_binding(env.broker, binding, prepared["plan"])


@pytest.mark.parametrize("phase", ["admission", "dispatch"])
async def test_authentication_callback_cannot_change_registered_target(env, phase):
    prepared = env.prepare("changed-target", build_job_id="build-one")
    before = env.driver.writes
    if phase == "dispatch":
        env.execute(prepared)

    def changed(*args):
        env.configure(allow_flash=False)
        return True

    env.broker.principal_valid = changed
    if phase == "admission":
        with pytest.raises(BrokerError, match="execution_intent_not_available|backend_preflight_failed"):
            env.execute(prepared)
        assert env.store.job("changed-target") is None
    else:
        await env.broker.queue.join()
        assert env.broker.job("changed-target")["state"] == "cancelled"
    assert env.driver.writes == before
