"""Ended lease evidence releases abandoned plans, never device/job authority."""

import json
import unittest
from unittest.mock import patch

import pytest
from test_source_history_retention import HistoryFixture

from cardputer_mcp.broker import Broker, BrokerError, digest_plan
from cardputer_mcp.config import Settings
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, LeaseRequest, Origin, RenewRequest
from cardputer_mcp.source_payloads import _reclaim
from cardputer_mcp.source_resources import resource_report
from cardputer_mcp.source_retention import retention_evidence
from cardputer_mcp.source_snapshot import SourceError


class SourcePlanLifetimeTests(HistoryFixture, unittest.TestCase):
    def setUp(self):
        self.setup_history()
        self.old, _, _ = self.publish_generation(1, principal="other")
        self.current, _, _ = self.publish_generation(2)

    def request(self, lease="ended", state="released", request="request"):
        self.store.put_request(request, None, 60, state="owning", lease_id=lease)
        if state != "owning":
            self.store.set_request(request, state, "synthetic-lifecycle")

    def prepared(self, job="abandoned", lease="ended", state=None, direct=False):
        return self.add(
            {
                "job_id": job,
                "test": "firmware_build" if direct else "firmware_flash",
                "broker_epoch": "epoch",
                "session_id": lease,
                "flash_principal": "other",
                "build_job_id": "build-1",
                **(
                    {"project_id": "fixture", "source_receipt": self.old["source_receipt"]}
                    if direct
                    else {}
                ),
            },
            state,
        )

    def test_closed_lease_reclaims_other_owner_abandoned_plan_and_keeps_receipt_and_lkg(self):
        original = (self.payload(self.old).parent / "receipt.json").read_bytes()
        self.request()
        self.prepared()
        self.prepared("direct", direct=True)
        report = resource_report(
            self.store, "fixture", "owner", self.snapshots.inventory("fixture"), epoch="epoch"
        )
        self.assertEqual(report["reclamation_candidate_payloads"], 1)
        self.assertNotIn(self.old["source_receipt"], json.dumps(report))
        self.assertNotIn("abandoned", json.dumps(report))
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=2):
            self.fetch(3)
        self.assertFalse(self.payload(self.old).exists())
        self.assertEqual((self.payload(self.old).parent / "receipt.json").read_bytes(), original)
        self.assertTrue(self.payload(self.current).exists())
        self.assertEqual(len(self.evidence()["provenance"]), 2)
        self.assertIsNone(self.store.job("abandoned"))

    def test_missing_owning_review_or_unfinished_closure_is_not_positive_evidence(self):
        self.prepared()
        self.assertIn(self.old["source_receipt"], self.evidence()["protected"])
        for state in ("owning", "needs_review", "unknown", "cancelled", "released", "expired"):
            with self.subTest(state=state):
                if self.store.request("request") is None:
                    self.request(state=state)
                else:
                    self.store.set_request("request", state)
                if state in {"released", "expired"}:
                    # Even a closure label without its completed transition is insufficient.
                    self.store.db.execute("UPDATE device_requests SET finished=NULL")
                self.assertIn(self.old["source_receipt"], self.evidence()["protected"])
        self.store.set_request("request", "expired")
        self.assertNotIn(self.old["source_receipt"], self.evidence()["protected"])

    def test_closed_lease_never_unprotects_unfinished_jobs(self):
        self.request()
        for number, state in enumerate(("queued", "running", "needs_review", "unknown")):
            for direct in (False, True):
                with self.subTest(state=state, direct=direct):
                    job = f"active-{number}-{direct}"
                    self.prepared(job, state=state, direct=direct)
                    self.assertIn(self.old["source_receipt"], self.evidence()["protected"])
                    self.store.set_job(job, "failed")
        self.assertNotIn(self.old["source_receipt"], self.evidence()["protected"])

    def test_current_plan_from_another_lease_still_blocks_final_reclamation(self):
        self.request()
        self.prepared()
        metadata = self.snapshots.resolve("fixture", self.old["source_receipt"], "other")[1]
        calls = 0

        def evidence():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.request("current", "owning", "new-request")
                self.prepared("current-plan", "current")
            return self.evidence()

        self.snapshots.retention = evidence
        with self.snapshots.cache_guard("fixture"):
            with self.assertRaisesRegex(SourceError, "source_cache_protected"):
                _reclaim(
                    self.snapshots, self.plan(3), self.old["source_receipt"], metadata, lambda: True
                )
        self.assertTrue(self.payload(self.old).exists())
        self.assertEqual(calls, 2)

    def test_duplicate_or_truncated_lease_evidence_disables_reclamation(self):
        self.request()
        self.prepared()
        self.request("ended", "owning", "duplicate")
        self.assertFalse(self.evidence()["complete"])
        self.store.db.execute("DELETE FROM device_requests WHERE id='duplicate'")
        self.assertTrue(self.evidence()["complete"])
        for number in range(21):
            self.request(f"extra-{number}", "released", f"request-{number}")
        with patch("cardputer_mcp.source_retention.REFERENCE_LIMIT", 20):
            self.assertFalse(self.evidence()["complete"])


@pytest.mark.parametrize("ending", ["release", "expire"])
async def test_real_broker_ended_lease_cannot_be_renewed_or_execute_old_plan(tmp_path, ending):
    now = [100.0]
    broker = Broker(
        Settings(state_dir=tmp_path / "state"),
        lock_path=tmp_path / "broker.lock",
        clock=lambda: now[0],
    )
    await broker.start()
    try:
        token = broker.acquire(AcquireRequest(owner=Origin.LOCAL, lease_seconds=60))["session"]
        session = broker.session
        assert session is not None
        store = broker._store()
        source_receipt = "a" * 64
        build = {
            "job_id": "build",
            "test": "firmware_build",
            "project_id": "fixture",
            "source_receipt": source_receipt,
        }
        build_key = digest_plan(build)
        store.put_plan(build_key, build)
        store.add_job("build", build_key)
        store.set_job("build", "succeeded")

        def evidence():
            return retention_evidence(store, "fixture", "owner", broker.epoch, now=now[0])

        # An immutable stored firmware plan: admission must reject its old lease
        # before preflight/file checks. No driver, compiler or device is invoked.
        plan = {
            "broker_epoch": broker.epoch,
            "session_id": session.id,
            "job_id": "abandoned",
            "test": "firmware_flash",
            "build_job_id": "build",
        }
        key = digest_plan(plan)
        broker._store().put_plan(key, plan)
        now[0] = 150.0
        broker.renew(RenewRequest(session=token, lease_seconds=60))
        now[0] = 170.0  # original deadline passed; renewal remains owning
        broker.maintenance_tick()
        assert broker._store().request(session.request_id)["state"] == "owning"
        assert evidence()["protected"][source_receipt] == ["executable_firmware_operation"]
        if ending == "release":
            broker.release(LeaseRequest(session=token))
        else:
            now[0] = 211.0
            broker.maintenance_tick()
        assert broker._store().request(session.request_id)["state"] in {"released", "expired"}
        assert evidence()["complete"]
        assert source_receipt not in evidence()["protected"]
        with pytest.raises(BrokerError, match="invalid_session"):
            broker.renew(RenewRequest(session=token, lease_seconds=60))
        fresh = broker.acquire(AcquireRequest(owner=Origin.LOCAL))["session"]
        assert broker.session.id != session.id
        with pytest.raises(BrokerError, match="plan_session_mismatch"):
            await broker.execute_async(ExecuteRequest(session=fresh, plan_hash=key))
        assert broker._store().job("abandoned") is None
    finally:
        await broker.close()
