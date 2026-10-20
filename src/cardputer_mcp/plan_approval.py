"""Bounded immutable plan validation; execution authority belongs to the client request."""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .broker import Broker, BrokerError, Session, digest_plan
from .models import FLASH_TESTS, TestName

_MAX_PLAN_BYTES = 16 * 1024
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
        raise BrokerError("plan_validation_unavailable") from None


class PlanApprovals:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker

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
            if "derby_detail_mode" in plan:
                expected_keys |= {"derby_detail_mode"}
                if plan["derby_detail_mode"] not in ("ON", "OFF") or "source_receipt" not in plan:
                    raise ValueError
            if "derby_dma_mode" in plan:
                expected_keys |= {"derby_dma_mode"}
                if "diagnostic_entry_sha256" not in plan:
                    raise ValueError
            from .diagnostic_registry import validate_diagnostic_record

            validate_diagnostic_record(plan.get("source_provenance", {}), plan)
            if "diagnostic_entry_sha256" in plan:
                from .diagnostic_registry import validate_entry_binding

                if "derby_detail_mode" not in plan:
                    raise ValueError
                expected_keys |= {"diagnostic_entry_sha256"}
                validate_entry_binding(plan.get("source_provenance", {}), plan)
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
                    | ({"submodules"} if "submodules" in provenance else set())
                    or provenance["source_receipt"] != plan["source_receipt"]
                    or not _hex(provenance["commit"], 40)
                    or not _hex(provenance["tree_sha1"], 40)
                    or not _hex(provenance["source_sha256"], 64)
                    or provenance["dependency_policy"]
                    != (
                        "gitlinks_fixed_commits_no_fetch"
                        if "submodules" in provenance
                        else "vendored_components_no_fetch"
                    )
                    or type(provenance["repository"]) is not str
                    or re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", provenance["repository"])
                    is None
                ):
                    raise ValueError
                if "submodules" in provenance:
                    from .source_snapshot import validate_submodule_provenance

                    validate_submodule_provenance(provenance["submodules"])
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
                    *(
                        ["-D", "POCKET_DERBY_DETAIL_MEASURE=" + plan["derby_detail_mode"]]
                        if "derby_detail_mode" in plan
                        else []
                    ),
                    *(
                        ["-D", "KASANE_DMA_OVERLAP=" + plan["derby_dma_mode"]]
                        if "derby_dma_mode" in plan
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
            if test == "derby_smoke":
                from .derby_smoke import SMOKE

                expected_keys = expected_keys | {"smoke"}
                if plan.get("smoke") != SMOKE:
                    raise ValueError
            if test == TestName.DERBY_FIXED.value:
                from .derby_fixed import FIXED_PLAN

                expected_keys = expected_keys | {"fixed_workload"}
                if plan.get("fixed_workload") != FIXED_PLAN:
                    raise ValueError
                if "flash_job_id" in plan or "collector_binding" in plan:
                    from .collector_binding import validate_collector_schema

                    expected_keys |= {"flash_job_id", "collector_binding"}
                    validate_collector_schema(plan)
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
                    "derby_smoke",
                    "derby_fixed_workload",
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
                    else "successful_flash_receipt"
                    if test == "derby_fixed_workload" and "flash_job_id" in plan
                    else "configured_expected_hash_only"
                )
                or plan.get("protocol")
                != (
                    "pocketjs-derby-smoke-v1"
                    if test == "derby_smoke"
                    else "pocketjs-derby-fixed-v2"
                    if test == "derby_fixed_workload"
                    else "pocketjs-legacy-capture-v1"
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
        if self.broker.settings.backend != self.broker.backend.name or (
            self.broker.backend.name == "serial"
            and (
                self.broker.settings.serial is None
                or getattr(self.broker.backend, "config", None) != self.broker.settings.serial
            )
        ):
            raise BrokerError("plan_backend_mismatch")
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
        elif plan["test"] == TestName.SOURCE.value:
            if plan["source_principal"] != lease.principal_id:
                raise BrokerError("plan_session_mismatch")
        elif plan["backend"] == "serial":
            config = self.broker.settings.serial
            receipt_bound = plan["test"] == TestName.DERBY_FIXED.value and "flash_job_id" in plan
            if receipt_bound:
                from .collector_binding import validate_collector_schema

                validate_collector_schema(plan)
                if plan["collector_binding"]["principal_id"] != lease.principal_id:
                    raise BrokerError("plan_session_mismatch")
            if config is None or (
                (
                    not receipt_bound
                    and plan["expected_firmware_sha256"] != config.expected_firmware_sha256
                )
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

    def validate_current(self, plan_hash: str, lease: Session) -> dict[str, Any]:
        """Rebuild the bounded preflight without approval or operator attestation."""
        with _fixed_errors():
            plan = self._validate(plan_hash, lease)
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
                        plan["project_id"],
                        lease.principal_id or "",
                        plan.get("source_receipt"),
                        plan.get("derby_detail_mode"),
                        **(
                            {"derby_dma_mode": plan["derby_dma_mode"]}
                            if "derby_dma_mode" in plan
                            else {}
                        ),
                    )
                elif plan["test"] == TestName.DERBY_FIXED.value and "flash_job_id" in plan:
                    from .collector_binding import prepare_collector

                    details = prepare_collector(
                        self.broker, plan["flash_job_id"], lease.principal_id or ""
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
            return plan

    def approve(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise BrokerError("approval_interface_retired")

    def list_pending(self) -> dict[str, Any]:
        return {"plans": [], "retired": True}
