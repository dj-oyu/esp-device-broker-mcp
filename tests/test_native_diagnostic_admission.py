"""Native selector admission uses existing owned receipts and current authority."""

import pytest
from test_flash_policy import env as env

from cardputer_mcp.broker import BrokerError
from cardputer_mcp.models import ExecuteRequest, PrepareRequest
from cardputer_mcp.plan_approval import PlanApprovals
from cardputer_mcp.source_snapshot import SourceError


@pytest.mark.parametrize("env", ["prepare-only"], indirect=True)
async def test_registered_profile_and_legacy_lease_admission_bind_same_selector(env):
    broker = env.broker
    request = PrepareRequest(
        job_id="profile-native",
        test="firmware_build",
        project_id="fixture",
        source_receipt=env.receipt,
        native_diagnostic_mode="ON",
    )
    registered = broker.profile_jobs.prepare(request, "alice", "local_cli")
    leased = broker.prepare(
        request.model_copy(update={"job_id": "leased-native", "session": env.session})
    )
    assert (
        registered["plan"]["native_diagnostic_mode"]
        == leased["plan"]["native_diagnostic_mode"]
        == "ON"
    )
    assert registered["plan"]["fingerprint"] == leased["plan"]["fingerprint"]
    assert broker.session is not None
    PlanApprovals(broker).validate_current(leased["plan_hash"], broker.session)
    assert env.store.job("profile-native") is None
    assert env.store.job("leased-native") is None
    assert env.driver.writes == 0


@pytest.mark.parametrize("env", ["prepare-only"], indirect=True)
async def test_receipt_owner_and_current_auth_are_required_before_native_plan(env):
    broker = env.broker
    request = PrepareRequest(
        job_id="native",
        test="firmware_build",
        project_id="fixture",
        source_receipt=env.receipt,
        native_diagnostic_mode="ON",
    )
    count = env.store.record_count("plans")
    with pytest.raises(SourceError, match="source_receipt_not_owned"):
        broker.profile_jobs.prepare(request, "other", "local_cli")
    assert env.store.record_count("plans") == count
    prepared = broker.profile_jobs.prepare(request, "alice", "local_cli")
    with pytest.raises(BrokerError, match="owned_registered_project_plan_required"):
        broker.profile_jobs.execute(
            ExecuteRequest(plan_hash=prepared["plan_hash"]), "other", "local_cli"
        )
    broker.principal_valid = lambda *args: False
    with pytest.raises(BrokerError, match="principal_not_authorized"):
        broker.profile_jobs.execute(
            ExecuteRequest(plan_hash=prepared["plan_hash"]), "alice", "local_cli"
        )
    assert env.store.job("native") is None
    assert env.driver.writes == 0


@pytest.mark.parametrize("env", ["prepare-only"], indirect=True)
async def test_profile_change_invalidates_selected_plan_before_enqueue(env):
    broker = env.broker
    request = PrepareRequest(
        job_id="native",
        test="firmware_build",
        project_id="fixture",
        source_receipt=env.receipt,
        native_diagnostic_mode="OFF",
    )
    prepared = broker.profile_jobs.prepare(request, "alice", "local_cli")
    profile = broker.settings.build_projects["fixture"].trusted_profile
    profile.write_text("changed synthetic trusted profile")
    with pytest.raises(BrokerError, match="profile_or_principal_no_longer_valid"):
        broker.profile_jobs.execute(
            ExecuteRequest(plan_hash=prepared["plan_hash"]), "alice", "local_cli"
        )
    assert env.store.job("native") is None


@pytest.mark.parametrize("env", ["prepare-only"], indirect=True)
async def test_changed_source_cannot_enqueue_selected_plan(env):
    broker = env.broker
    prepared = broker.prepare(
        PrepareRequest(
            session=env.session,
            job_id="native",
            test="firmware_build",
            project_id="fixture",
            source_receipt=env.receipt,
            native_diagnostic_mode="ON",
        ),
    )
    root, _ = broker.firmware_source.snapshots.resolve("fixture", env.receipt, "alice")
    (root / "CMakeLists.txt").write_text("changed synthetic source")
    with pytest.raises(BrokerError, match="backend_preflight_failed"):
        broker.execute(ExecuteRequest(session=env.session, plan_hash=prepared["plan_hash"]))
    assert env.store.job("native") is None
    assert env.driver.writes == 0
