"""Local exact-plan approval, optionally dispatching a previously submitted intent.

The caller must enforce local_cli authentication and control scope. These methods
are synchronous so validation and approval cannot interleave with broker tasks.
"""

import json
import math
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .broker import Broker, BrokerError, Session, digest_plan
from .models import FLASH_TESTS, TestName

_MAX_PLAN_BYTES = 16 * 1024
_MAX_PENDING = 64
_MAX_PENDING_SCAN = 256
_BASE_KEYS = {
    "version",
    "broker_epoch",
    "session_id",
    "device",
    "backend",
    "job_id",
    "test",
    "stages",
}
_SERIAL_KEYS = {
    "port",
    "device_fingerprint",
    "protocol",
    "expected_firmware_sha256",
    "firmware_verification",
    "observe_seconds",
    "effects",
}
_ATTESTATIONS = {
    "none": "",
    "flash_device_read_verified": "REGISTERED DEVICE VERIFIED FOR THIS BACKUP READ",
    "firmware_recovery_verified": "DEVICE AND FIRMWARE RECOVERY VERIFIED FOR THIS EXACT PLAN",
    "flash_device_backup_verified": "DEVICE AND BACKUP VERIFIED FOR THIS EXACT FLASH PLAN",
    "device_free_firmware": "DEVICE FREE AND FIRMWARE VERIFIED",
    "device_home_firmware": "DEVICE HOME AND FIRMWARE VERIFIED",
}


def _hex(value: object, length: int) -> bool:
    return type(value) is str and re.fullmatch(rf"[a-f0-9]{{{length}}}", value) is not None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BrokerError("plan_not_found_or_changed")
        result[key] = value
    return result


@contextmanager
def _fixed_errors() -> Iterator[None]:
    try:
        yield
    except BrokerError:
        raise
    except Exception:
        # SQLite, JSON, inventory and authentication exceptions may contain
        # configuration or credentials. Never reflect their text to an operator.
        raise BrokerError("plan_approval_unavailable") from None


class PlanApprovals:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker

    def _context(self) -> Session:
        broker = self.broker
        broker._store()
        broker.maintenance_tick()
        if broker.closing:
            raise BrokerError("broker_closing")
        if broker._store().needs_review():
            raise BrokerError("recovery_review_required")
        if broker._physical_busy() or broker.backend.hardware_connected:
            raise BrokerError("device_busy")
        job_counts = broker._store().counts()
        if not broker.queue.empty() or any(state in job_counts for state in ("queued", "running")):
            raise BrokerError("device_jobs_pending")
        lease = broker.session
        if lease is None or not math.isfinite(lease.deadline) or broker.clock() >= lease.deadline:
            raise BrokerError("plan_session_mismatch")
        if lease.principal_id is not None:
            valid = broker.principal_valid
            try:
                authorized = (
                    valid is not None
                    and valid(lease.principal_id, lease.owner, frozenset({"control"})) is True
                )
            except Exception:
                authorized = False
            if not authorized:
                raise BrokerError("principal_not_authorized")
        # Legacy direct-acquire tests may have no principal. This service is
        # still behind the local operator boundary, never an MCP tool.
        work = broker.work_sessions.get(lease.work_session_id or "")
        if lease.work_session_id is not None and (
            work is None
            or work.state != "open"
            or broker.clock() >= work.deadline
            or "control" not in work.scopes
            or work.principal_id != lease.principal_id
        ):
            raise BrokerError("plan_session_mismatch")
        if broker.settings.backend != broker.backend.name:
            raise BrokerError("plan_backend_mismatch")
        if broker.backend.name == "serial" and (
            broker.settings.serial is None
            or getattr(broker.backend, "config", None) != broker.settings.serial
        ):
            raise BrokerError("plan_backend_mismatch")
        return lease

    def _plan(self, plan_hash: str) -> dict[str, Any]:
        if not _hex(plan_hash, 64):
            raise BrokerError("plan_not_found_or_changed")
        row = (
            self.broker._store()
            .db.execute(
                "SELECT payload FROM plans WHERE hash=? AND length(CAST(payload AS BLOB))<=?",
                (plan_hash, _MAX_PLAN_BYTES),
            )
            .fetchone()
        )
        if row is None or type(row[0]) is not str:
            raise BrokerError("plan_not_found_or_changed")
        try:
            plan = json.loads(row[0], object_pairs_hook=_unique_object)
            if type(plan) is not dict or digest_plan(plan) != plan_hash:
                raise ValueError
            self._schema(plan)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise BrokerError("plan_not_found_or_changed") from None
        return plan

    @staticmethod
    def _schema(plan: dict[str, Any]) -> None:
        """Only known plan fields may reach the GUI; never echo extra JSON data."""
        if (
            type(plan.get("version")) is not int
            or plan["version"] != 1
            or not _hex(plan.get("broker_epoch"), 32)
            or not _hex(plan.get("session_id"), 32)
            or type(plan.get("job_id")) is not str
            or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", plan["job_id"]) is None
        ):
            raise ValueError
        backend, test = plan.get("backend"), plan.get("test")
        if type(test) is not str:
            raise ValueError
        if test in {t.value for t in FLASH_TESTS}:
            from .firmware_flash import FLASH_FIELDS, validate_flash_plan

            validate_flash_plan(plan)
            expected_keys = _BASE_KEYS | FLASH_FIELDS
        elif test == TestName.SOURCE.value:
            from .source_snapshot import LIMITS

            expected_keys = _BASE_KEYS | {
                "project_id",
                "source_commit",
                "source_principal",
                "source_repository",
                "source_registration_sha256",
                "limits",
                "effects",
            }
            if (
                backend not in ("mock", "serial")
                or plan.get("device")
                != ("mock-cardputer" if backend == "mock" else "configured-cardputer")
                or not _hex(plan.get("source_commit"), 40)
                or not _hex(plan.get("source_registration_sha256"), 64)
                or type(plan.get("project_id")) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", plan["project_id"]) is None
                or type(plan.get("source_principal")) is not str
                or not 1 <= len(plan["source_principal"]) <= 128
                or type(plan.get("source_repository")) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", plan["source_repository"])
                is None
                or plan.get("limits") != LIMITS
                or plan.get("stages") != ["source_download", "source_validate", "source_publish"]
                or type(plan.get("effects")) is not list
                or len(plan["effects"]) != 3
                or any(type(item) is not str or len(item) > 256 for item in plan["effects"])
            ):
                raise ValueError
        elif test == TestName.BUILD.value:
            expected_keys = _BASE_KEYS | {
                "project_id",
                "fingerprint",
                "expected_idf",
                "target",
                "build_principal",
                "effects",
                "argv_template",
                "profile_execution_policy",
            }
            if "source_receipt" in plan:
                expected_keys |= {"source_receipt", "source_provenance"}
                provenance = plan.get("source_provenance")
                if (
                    not _hex(plan["source_receipt"], 64)
                    or type(provenance) is not dict
                    or set(provenance)
                    != {
                        "source_receipt",
                        "repository",
                        "commit",
                        "tree_sha1",
                        "source_sha256",
                        "dependency_policy",
                    }
                    or provenance["source_receipt"] != plan["source_receipt"]
                    or not _hex(provenance["commit"], 40)
                    or not _hex(provenance["tree_sha1"], 40)
                    or not _hex(provenance["source_sha256"], 64)
                    or provenance["dependency_policy"] != "vendored_components_no_fetch"
                    or type(provenance["repository"]) is not str
                    or re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", provenance["repository"])
                    is None
                ):
                    raise ValueError
            if (
                backend not in ("mock", "serial")
                or plan.get("device")
                != ("mock-cardputer" if backend == "mock" else "configured-cardputer")
                or not _hex(plan.get("fingerprint"), 64)
                or type(plan.get("project_id")) is not str
                or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", plan["project_id"]) is None
                or plan.get("profile_execution_policy") not in ("inherit", "RemoteSigned")
                or plan.get("expected_idf") != "6.0.1"
                or plan.get("target") != "esp32s3"
                or type(plan.get("build_principal")) is not str
                or not 1 <= len(plan["build_principal"]) <= 128
                or plan.get("stages") != ["idf_profile", "idf_diagnostics", "idf_build"]
                or type(plan.get("effects")) is not list
                or len(plan["effects"]) != 3
                or any(type(value) is not str or len(value) > 256 for value in plan["effects"])
                or plan.get("argv_template")
                != [
                    "registered-python",
                    "registered-idf.py",
                    "-B",
                    "fingerprint-build-directory",
                    *(
                        ["-D", "SDKCONFIG=build-output-sdkconfig"]
                        if "source_receipt" in plan
                        else []
                    ),
                    "build",
                ]
            ):
                raise ValueError
        elif backend == "mock":
            from .backend import MockBackend

            details = MockBackend().prepare(TestName(test))
            expected_keys = _BASE_KEYS | set(details)
            if plan.get("device") != "mock-cardputer" or any(
                plan.get(key) != value for key, value in details.items()
            ):
                raise ValueError
        elif backend == "serial":
            expected_keys = _BASE_KEYS | _SERIAL_KEYS
            if test == "restart_and_observe":
                expected_keys = expected_keys | {"restart"}
                restart = plan.get("restart")
                if (
                    type(restart) is not dict
                    or set(restart)
                    != {
                        "method",
                        "attempts",
                        "rts_hold_milliseconds",
                        "reconnect_seconds",
                        "max_reopens",
                        "stable_usb_fingerprint",
                        "buffer_policy",
                    }
                    or restart.get("method") != "esp32s3_usb_serial_jtag_rts_v1"
                    or restart.get("buffer_policy") != "discard_before_reset"
                    or not _hex(restart.get("stable_usb_fingerprint"), 64)
                    or any(
                        type(restart.get(key)) is not int or restart[key] != value
                        for key, value in {
                            "attempts": 1,
                            "rts_hold_milliseconds": 100,
                            "reconnect_seconds": 8,
                            "max_reopens": 1,
                        }.items()
                    )
                ):
                    raise ValueError
            if test in ("firmware_observe", "restart_and_observe"):
                expected_keys = expected_keys | {"log_delivery"}
                delivery = plan.get("log_delivery")
                if (
                    type(delivery) is not dict
                    or set(delivery)
                    != {
                        "principal_id",
                        "origin",
                        "retention_seconds",
                        "max_received_bytes",
                        "max_lines",
                        "max_line_bytes",
                        "max_rendered_bytes",
                    }
                    or type(delivery.get("principal_id")) is not str
                    or re.fullmatch(r"[a-zA-Z0-9_.:@-]{1,128}", delivery["principal_id"]) is None
                    or delivery.get("origin") not in ("local_cli", "remote_mcp")
                    or any(
                        type(delivery.get(key)) is not int or delivery[key] != value
                        for key, value in {
                            "retention_seconds": 300,
                            "max_received_bytes": 65536,
                            "max_lines": 256,
                            "max_line_bytes": 1024,
                            "max_rendered_bytes": 32768,
                        }.items()
                    )
                ):
                    raise ValueError
            if (
                test
                not in (
                    "passive_observe",
                    "hello",
                    "capture_home",
                    "firmware_observe",
                    "restart_and_observe",
                )
                or plan.get("device") != "configured-cardputer"
                or plan.get("stages") != ["serial_open", test, "serial_close"]
                or not _hex(plan.get("device_fingerprint"), 64)
                or type(plan.get("port")) is not str
                or not 1 <= len(plan["port"]) <= 256
                or any(ord(char) < 32 or ord(char) == 127 for char in plan["port"])
                or plan.get("firmware_verification")
                != (
                    "unverified_device_report"
                    if test in ("firmware_observe", "restart_and_observe")
                    else "operator_attestation_only"
                )
                or plan.get("protocol")
                != (
                    "pocketjs-legacy-capture-v1"
                    if test == "capture_home"
                    else "esp-idf-startup-log-v1"
                    if test in ("firmware_observe", "restart_and_observe")
                    else "pocketjs-hello-v1"
                )
                or (
                    plan.get("expected_firmware_sha256") is not None
                    and not _hex(plan["expected_firmware_sha256"], 64)
                )
                or (
                    test not in ("passive_observe", "firmware_observe", "restart_and_observe")
                    and not _hex(plan.get("expected_firmware_sha256"), 64)
                )
                or type(plan.get("effects")) is not list
                or not 1 <= len(plan["effects"]) <= 16
                or any(type(value) is not str or len(value) > 1024 for value in plan["effects"])
            ):
                raise ValueError
            duration = plan.get("observe_seconds")
            if test == "passive_observe":
                if (
                    not isinstance(duration, (float, int))
                    or isinstance(duration, bool)
                    or not 0.1 <= duration <= 10
                ):
                    raise ValueError
            elif test in ("firmware_observe", "restart_and_observe"):
                if type(duration) is not int or duration != 30:
                    raise ValueError
            elif duration is not None:
                raise ValueError
        else:
            raise ValueError
        if set(plan) != expected_keys:
            raise ValueError

    def _validate(self, plan_hash: str, lease: Session) -> dict[str, Any]:
        plan = self._plan(plan_hash)
        if plan["broker_epoch"] != self.broker.epoch or plan["session_id"] != lease.id:
            raise BrokerError("plan_session_mismatch")
        if (
            plan["backend"] != self.broker.backend.name
            or plan["device"] != self.broker.backend.device
        ):
            raise BrokerError("plan_backend_mismatch")
        if plan["test"] in {t.value for t in FLASH_TESTS}:
            if plan["flash_principal"] != lease.principal_id:
                raise BrokerError("plan_session_mismatch")
        elif plan["test"] == TestName.BUILD.value:
            if plan["build_principal"] != lease.principal_id:
                raise BrokerError("plan_session_mismatch")
        elif plan["backend"] == "serial":
            config = self.broker.settings.serial
            if config is None or (
                plan["expected_firmware_sha256"] != config.expected_firmware_sha256
                or plan["observe_seconds"]
                != (
                    config.observe_seconds
                    if plan["test"] == "passive_observe"
                    else 30
                    if plan["test"] in ("firmware_observe", "restart_and_observe")
                    else None
                )
            ):
                raise BrokerError("plan_backend_mismatch")
            if plan["test"] in ("firmware_observe", "restart_and_observe") and plan[
                "log_delivery"
            ] != self.broker._log_delivery(lease):
                raise BrokerError("plan_session_mismatch")
        job = self.broker._store().job(plan["job_id"])
        if job is not None and (
            job["state"] != "awaiting_approval"
            or job["plan_hash"] != plan_hash
            or self.broker.pending_execution(plan_hash) is None
        ):
            raise BrokerError("plan_job_already_exists")
        return plan

    def _revision(self, plan: dict[str, Any], lease: Session) -> str:
        broker, store = self.broker, self.broker._store()
        work = broker.work_sessions.get(lease.work_session_id or "")
        intent = broker.pending_execution(digest_plan(plan))
        return digest_plan(
            {
                "plan": plan,
                "broker_epoch": broker.epoch,
                "lease_id": lease.id,
                "lease_deadline": lease.deadline,
                "principal_id": lease.principal_id,
                "owner": lease.owner,
                "request_id": lease.request_id,
                "work_session_id": lease.work_session_id,
                "work_deadline": work.deadline if work else None,
                "work_scopes": sorted(work.scopes) if work else None,
                "execution_intent": {
                    "epoch": intent.epoch,
                    "plan_hash": intent.plan_hash,
                    "job_id": intent.job_id,
                    "lease_id": intent.lease_id,
                    "principal_id": intent.principal_id,
                    "owner": intent.owner,
                    "work_session_id": intent.work_session_id,
                    "deadline": intent.deadline,
                }
                if intent
                else None,
                "backend": broker.backend.name,
                "device": broker.backend.device,
                "serial": broker.settings.serial.model_dump() if broker.settings.serial else None,
                "queue_revision": broker._queue_revision(),
                "queue_depth": broker.queue.qsize(),
                "waiters": list(broker.waiters),
                "jobs": store.counts(),
                "history": {
                    table: store.record_count(table)
                    for table in ("jobs", "plans", "work_sessions", "device_requests")
                },
            }
        )

    @staticmethod
    def _attestation(plan: dict[str, Any]) -> str:
        if plan["test"] == TestName.BACKUP.value:
            return "flash_device_read_verified"
        if plan["test"] in (TestName.FLASH.value, TestName.RESTORE.value):
            return (
                "firmware_recovery_verified"
                if plan["recovery_kind"] == "artifact_bundle"
                else "flash_device_backup_verified"
            )
        if plan["backend"] == "serial":
            if plan["test"] == "hello":
                return "device_free_firmware"
            if plan["test"] == "capture_home":
                return "device_home_firmware"
        return "none"

    def _remaining(self, lease: Session) -> float:
        return max(0, round(lease.deadline - self.broker.clock(), 3))

    def list_pending(self) -> dict[str, Any]:
        with _fixed_errors():
            self.broker._store()
            self.broker.maintenance_tick()
            if self.broker.session is None and not self.broker.closing:
                return {"broker_epoch": self.broker.epoch, "plans": [], "has_more": False}
            lease = self._context()
            allow_mock = (
                self.broker.backend.name == "mock" and self.broker.settings.allow_mock_execution
            )
            if (
                allow_mock
                and not self.broker._execution_intents
                and not self.broker.settings.build_projects
            ):
                return {"broker_epoch": self.broker.epoch, "plans": [], "has_more": False}
            # CASE prevents SQLite evaluating json_extract against corrupt JSON.
            query = """FROM plans p WHERE CASE WHEN json_valid(p.payload)
                AND length(CAST(p.payload AS BLOB))<=? THEN
                    json_extract(p.payload,'$.broker_epoch')=?
                    AND json_extract(p.payload,'$.session_id')=?
                    AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.id=json_extract(p.payload,'$.job_id')
                        AND (j.state!='awaiting_approval' OR j.plan_hash!=p.hash))
                ELSE 0 END
                AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.hash=p.hash)"""
            args = (_MAX_PLAN_BYTES, self.broker.epoch, lease.id)
            db = self.broker._store().db
            store_revision = db.total_changes
            plans: list[dict[str, Any]] = []
            capture_sample: tuple[str, dict[str, Any]] | None = None
            capture_permissions: set[bool] = set()
            last_rowid = 0
            scanned = 0
            # Bound each read and the returned list. Filter before applying the
            # display limit so automatic permits do not hide later manual work
            # or produce a misleading "more pending" badge.
            while len(plans) <= _MAX_PENDING:
                if scanned == _MAX_PENDING_SCAN:
                    if db.execute(
                        f"SELECT 1 {query} AND p.rowid>? LIMIT 1", (*args, last_rowid)
                    ).fetchone():
                        # An incomplete scan cannot establish a pending count.
                        raise BrokerError("plan_approval_unavailable")
                    break
                rows = db.execute(
                    f"SELECT p.rowid,p.hash {query} AND p.rowid>? ORDER BY p.rowid LIMIT ?",
                    (*args, last_rowid, min(_MAX_PENDING, _MAX_PENDING_SCAN - scanned)),
                ).fetchall()
                if not rows:
                    break
                for rowid, plan_hash in rows:
                    last_rowid = rowid
                    scanned += 1
                    try:
                        plan = self._validate(plan_hash, lease)
                    except BrokerError:
                        continue
                    submitted = self.broker.pending_execution(plan_hash) is not None
                    if (
                        allow_mock
                        and not submitted
                        and plan["test"]
                        not in ({TestName.BUILD.value} | {t.value for t in FLASH_TESTS})
                    ):
                        continue
                    policies = self.broker.capture_policies
                    if plan["test"] == "capture_home" and not submitted:
                        capture_sample = (plan_hash, plan)
                        authorization = (
                            policies.authorize_capture(plan, record_use=False) if policies else None
                        )
                        permitted = bool(
                            policies
                            and authorization is not None
                            and policies.validate_capture(authorization, plan)
                        )
                        capture_permissions.add(permitted)
                        if permitted:
                            continue
                    plans.append(
                        {
                            "plan_hash": plan_hash,
                            "job_id": plan["job_id"],
                            "test": plan["test"],
                            "backend": plan["backend"],
                        }
                    )
                    if len(plans) > _MAX_PENDING:
                        break
            # Registry callbacks above (and in _context) may consume a lease's
            # remaining time or change broker state. Never display their stale
            # candidates as current approval requests.
            current = self._context()
            pending = []
            for item in plans:
                try:
                    self._validate(item["plan_hash"], current)
                except BrokerError:
                    continue
                if db.execute(
                    "SELECT 1 FROM approvals WHERE hash=?", (item["plan_hash"],)
                ).fetchone():
                    continue
                pending.append(item)
            policies = self.broker.capture_policies
            if capture_sample is not None:
                plan_hash, plan = capture_sample
                self._validate(plan_hash, current)
                authorization = (
                    policies.authorize_capture(plan, record_use=False) if policies else None
                )
                permitted = bool(
                    policies
                    and authorization is not None
                    and policies.validate_capture(authorization, plan)
                )
                # Every validated capture has the same current lease, principal,
                # backend and configured target. Policy eligibility is shared;
                # job/hash only bind the resulting authorization. This terminal
                # probe catches expiry caused by a later callback, including
                # for an earlier skipped plan, without another validation loop.
                if capture_permissions != {permitted}:
                    raise BrokerError("plan_approval_unavailable")
            if db.total_changes != store_revision:
                raise BrokerError("plan_approval_unavailable")
            if self.broker.closing:
                raise BrokerError("broker_closing")
            if self.broker._store().needs_review():
                raise BrokerError("recovery_review_required")
            if self.broker._physical_busy() or self.broker.backend.hardware_connected:
                raise BrokerError("device_busy")
            if not self.broker.queue.empty() or any(
                state in self.broker._store().counts() for state in ("queued", "running")
            ):
                raise BrokerError("device_jobs_pending")
            if (
                current is not lease
                or self.broker.session is not current
                or self.broker.epoch != args[1]
                or current.id != args[2]
                or not math.isfinite(current.deadline)
                or self.broker.clock() >= current.deadline
            ):
                raise BrokerError("plan_session_mismatch")
            remaining = self._remaining(current)
            return {
                "broker_epoch": self.broker.epoch,
                "plans": [
                    {
                        **item,
                        "lease_remaining_seconds": remaining,
                        **self._intent_detail(item["plan_hash"], current, pending_list=True),
                    }
                    for item in pending[:_MAX_PENDING]
                ],
                "has_more": len(pending) > _MAX_PENDING,
            }

    def _intent_detail(
        self, plan_hash: str, lease: Session, *, pending_list: bool = False
    ) -> dict[str, Any]:
        intent = self.broker.pending_execution(plan_hash)
        if intent is None:
            return (
                {}
                if pending_list
                else {"execution_submitted": False, "intent_remaining_seconds": None}
            )
        return {
            "execution_submitted": True,
            "intent_remaining_seconds": max(
                0, round(min(intent.deadline, lease.deadline) - self.broker.clock(), 3)
            ),
        }

    def detail(self, plan_hash: str) -> dict[str, Any]:
        with _fixed_errors():
            lease = self._context()
            plan = self._validate(plan_hash, lease)
            return {
                "plan_hash": plan_hash,
                "plan": plan,
                "review_revision": self._revision(plan, lease),
                "lease_remaining_seconds": self._remaining(lease),
                "attestation": self._attestation(plan),
                **self._intent_detail(plan_hash, lease),
            }

    def validate_current(self, plan_hash: str) -> tuple[dict[str, Any], Session]:
        """Internal policy helper for idle plans; never serialize the returned lease.

        This checks current state but does not attest, approve, enumerate ports,
        or authorize execution. Policy callers must independently establish the
        policy permission and perform their own final metadata revalidation.
        """
        with _fixed_errors():
            lease = self._context()
            return self._validate(plan_hash, lease), lease

    def approve(
        self,
        plan_hash: str,
        review_revision: str,
        attestation: str,
        retain_local_home_line: bool = False,
    ) -> dict[str, Any]:
        with _fixed_errors():
            if type(retain_local_home_line) is not bool:
                raise BrokerError("local_home_diagnostic_not_available")
            lease = self._context()
            plan = self._validate(plan_hash, lease)
            if not _hex(review_revision, 64) or not secrets.compare_digest(
                review_revision, self._revision(plan, lease)
            ):
                raise BrokerError("stale_plan_review")
            if (
                type(attestation) is not str
                or attestation != _ATTESTATIONS[self._attestation(plan)]
            ):
                raise BrokerError("device_attestation_required")
            try:
                if plan["test"] in {t.value for t in FLASH_TESTS}:
                    details = self.broker.firmware_flash.prepare_plan(plan)
                elif plan["test"] == TestName.SOURCE.value:
                    from .models import PrepareRequest

                    request = PrepareRequest(
                        session=lease.capability,
                        project_id=plan["project_id"],
                        job_id=plan["job_id"],
                        test=TestName.SOURCE,
                        source_commit=plan["source_commit"],
                    )
                    details = self.broker.firmware_source.prepare(request, lease.principal_id or "")
                elif plan["test"] == TestName.BUILD.value:
                    if plan.get("source_receipt") is not None:
                        self.broker.firmware_source.authorize_receipt(
                            plan["project_id"], plan["source_receipt"], lease.principal_id or ""
                        )
                    details = self.broker.firmware_build.prepare(
                        plan["project_id"], lease.principal_id or "", plan.get("source_receipt")
                    )
                else:
                    details = self.broker.backend.prepare(TestName(plan["test"]))
            except Exception:
                raise BrokerError("backend_preflight_failed") from None
            expected = {key: plan[key] for key in _BASE_KEYS - {"stages"}} | details
            if plan["test"] in (TestName.FIRMWARE.value, TestName.RESTART.value):
                expected["log_delivery"] = self.broker._log_delivery(lease)
            if digest_plan(expected) != plan_hash:
                raise BrokerError("plan_backend_mismatch")
            # Enumeration may take time. Recheck expiry, authorization and all
            # review state immediately before persisting the approval.
            current_lease = self._context()
            current = self._validate(plan_hash, current_lease)
            if not secrets.compare_digest(review_revision, self._revision(current, current_lease)):
                raise BrokerError("stale_plan_review")
            store = self.broker._store()
            already = (
                store.db.execute("SELECT 1 FROM approvals WHERE hash=?", (plan_hash,)).fetchone()
                is not None
            )
            local_entry = None
            if retain_local_home_line:
                if already:
                    if not self.broker._local_home_opt_in_current(current, current_lease):
                        raise BrokerError("local_home_diagnostic_requires_fresh_approval")
                else:
                    local_entry = self.broker._make_local_home_diagnostic(current, current_lease)
                # Local consent is still bound to the same immutable reviewed
                # plan after recipient validity callbacks and metadata checks.
                final_lease = self._context()
                final_plan = self._validate(plan_hash, final_lease)
                if not secrets.compare_digest(
                    review_revision, self._revision(final_plan, final_lease)
                ):
                    raise BrokerError("stale_plan_review")
                if local_entry is not None and not self.broker._home_line_binding_live(
                    local_entry, final_lease
                ):
                    raise BrokerError("stale_plan_review")
                if local_entry is None and not self.broker._local_home_opt_in_current(
                    final_plan, final_lease
                ):
                    raise BrokerError("stale_plan_review")
            store.approve(plan_hash)
            if local_entry is not None:
                self.broker._install_local_home_diagnostic(local_entry)
            submitted = self.broker.dispatch_pending_execution(plan_hash)
            result = {
                "approved": True,
                "plan_hash": plan_hash,
                "already_approved": already,
                "execution_submitted": submitted,
            }
            if submitted:
                result["job_state"] = "queued"
            if retain_local_home_line:
                result["local_home_line_opt_in"] = True
            return result
