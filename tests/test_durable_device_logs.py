"""Device text is saved for every outcome and read back only by its owner (fake USB)."""

import json

import pytest
from test_device_observation_integration import (
    DEVICE_LOGS,
    LOG_LINE,
    PRINCIPAL,
    REMOTE_PRINCIPAL,
    SECRETS,
    execute,
    http_observation,  # noqa: F401  (fixture)
    http_observe,
    prepare,
    rpc,
    running,
    validate_prepared,
)

from cardputer_mcp.broker import BrokerError

JOB = "firmware-report"


@pytest.mark.parametrize("outcome", ["read_error", "uncertain", "succeeded_ram_expired"])
async def test_owner_reads_device_text_for_every_outcome(tmp_path, outcome):
    async with running(tmp_path) as env:
        if outcome == "read_error":
            env.port.chunks.append(OSError(LOG_LINE + SECRETS[0]))
        elif outcome == "uncertain":
            env.port.chunks.append(RuntimeError(LOG_LINE + SECRETS[0]))
        plan = prepare(env)
        validate_prepared(env, plan)
        await execute(env, plan)
        job = env.broker.job(JOB)
        if outcome == "succeeded_ram_expired":
            assert job["state"] == "succeeded"
            assert "sha256" in env.broker.read_device_logs(JOB, PRINCIPAL)
            env.clock.now += 301  # the RAM report is gone
        else:
            # Review classification fix: both read errors surface as the typed
            # serial_read_failed with a confirmed close, so `failed` (was needs_review).
            assert job["state"] == "failed"
        report = env.broker.read_device_logs(JOB, PRINCIPAL)
        assert report["source"] == "durable_job_log"
        assert report["state"] == job["state"] and report["kind"] == "firmware_observe"
        assert LOG_LINE in report["logs"]
        assert all(secret not in json.dumps(report) for secret in SECRETS)
        assert "job_log_unavailable" not in json.dumps(env.broker.job(JOB))
        with pytest.raises(BrokerError, match="^device_logs_not_available$"):
            env.broker.read_device_logs(JOB, REMOTE_PRINCIPAL)
        assert env.port.opens == env.port.closes == 1 and env.port.writes == []


async def test_store_failure_never_fails_the_job(tmp_path):
    async with running(tmp_path) as env:
        # A plain file where the directory should be: every write path fails.
        (env.broker.settings.state_dir / "job-logs").write_text("not a directory")
        plan = prepare(env)
        validate_prepared(env, plan)
        await execute(env, plan)
        job = env.broker.job(JOB)
        assert job["state"] == "succeeded"
        assert job["diagnostic"] == {"job_log_unavailable": True}
        (env.broker.settings.state_dir / "job-logs").unlink()


async def test_mcp_failed_job_logs_are_owner_only(http_observation):  # noqa: F811
    env = http_observation
    env.port.chunks.append(OSError("synthetic"))
    await http_observe(env)
    job = env.broker.job(JOB)
    assert job["state"] == "failed"  # review classification fix (was needs_review)
    report = (await rpc(env, "status", DEVICE_LOGS))["structuredContent"]
    assert report["ok"] and report["source"] == "durable_job_log"
    assert LOG_LINE in report["logs"]
    assert all(secret not in json.dumps(report) for secret in SECRETS)
    for key in ("other", "reader"):
        denied = (await rpc(env, "status", DEVICE_LOGS, token=env.keys[key].token))[
            "structuredContent"
        ]
        assert denied == {"ok": False, "error": "device_logs_not_available"}
