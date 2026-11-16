"""Internal deferred wiring. No native producer or production installation yet."""

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .private_build_finalize import commit_private_candidate
from .private_build_output import PrivateBuildLogs
from .private_input_contract import InputFile, _hash, _inventory
from .private_state_windows import verified_private_database

if TYPE_CHECKING:
    from .firmware_build import FirmwareBuild
    from .private_artifact_store import PrivateArtifactStore
    from .store import Store


def _plan(plan: dict[str, Any]) -> str:
    return json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class PrivateBuildCandidate:
    composition_identity: str
    files: tuple[tuple[str, bytes], ...] = field(repr=False)


class PrivateBuildRuntime:
    """Trusted producer must stage privately, join children and capture bounded bytes.

    The producer is internal code, never an MCP callback. Production must not
    install this adapter until native producer/storage checks are demonstrated.
    """

    def __init__(
        self,
        produce: Callable[[dict[str, Any], PrivateBuildLogs], PrivateBuildCandidate],
        artifacts: "PrivateArtifactStore",
        *,
        operation_guard: Callable[
            ["Store"], AbstractContextManager[None]
        ] = verified_private_database,
    ) -> None:
        self.produce = produce
        self.artifacts = artifacts
        self.pending: dict[str, tuple[str, PrivateBuildCandidate]] = {}
        self.running = False
        self._accepting = True
        self.operation_guard = operation_guard
        self._scope_release_failed = False
        self._operation: tuple[FirmwareBuild, Store, str, ExitStack] | None = None

    def stop_accepting(self) -> None:
        """Irreversible admission stop; caller serializes on the builder lock."""
        self._accepting = False

    def _check_accepting(self) -> None:
        if not self._accepting:
            raise ValueError("private_build_runtime_stopping")

    def begin_operation(
        self, builder: "FirmwareBuild", store: "Store", plan: dict[str, Any]
    ) -> None:
        """Broker-loop entry, before worker handoff; no SQLite use in worker guard."""
        with builder._lock:
            self._check_accepting()
            if self._operation is not None or self.running or self.pending:
                raise ValueError("private_build_operation_in_use")
            if self.artifacts.db is not store.db:
                raise ValueError("private_build_finalizer_invalid")
            builder._check_cancelled()
            if builder.process.active or builder.process.uncertain:
                raise ValueError("private_build_child_not_joined")
            payload = _plan(plan)
            with ExitStack() as entered:
                entered.enter_context(self.operation_guard(store))
                if builder.process.active or builder.process.uncertain:
                    self._operation = (builder, store, payload, entered.pop_all())
                    builder.output_uncertain = True
                    raise ValueError("private_build_child_not_joined")
                builder._check_cancelled()
                self._operation = (builder, store, payload, entered.pop_all())

    def _require_operation(self, builder: "FirmwareBuild", plan: dict[str, Any]) -> None:
        if (
            self._operation is None
            or self._operation[0] is not builder
            or self._operation[2] != _plan(plan)
        ):
            raise ValueError("private_build_operation_unavailable")

    def _close_operation(self) -> None:
        if self._operation is None or self._scope_release_failed:
            return
        builder, _, _, scope = self._operation
        with builder._lock:
            if self.running or builder.process.active or builder.process.uncertain:
                builder.output_uncertain = True
                return  # retained until explicit joined recovery
            try:
                scope.close()
            except BaseException:
                self._scope_release_failed = True
                builder.output_uncertain = True
                return  # successful DB commit must never become failure via cleanup
            self._operation = None

    def release_after_join(self) -> None:
        """Explicit recovery; does not clear broker uncertainty or producer roots."""
        if self._scope_release_failed:
            raise ValueError("private_build_state_release_uncertain")
        if self._operation is not None:
            builder = self._operation[0]
            if self.running or builder.process.active or builder.process.uncertain:
                raise ValueError("private_build_child_not_joined")
            self._close_operation()

    def execute(self, builder: "FirmwareBuild", plan: dict[str, Any]) -> str:
        payload = _plan(plan)
        snapshot = json.loads(payload)
        with builder._lock:
            self._check_accepting()
            self._require_operation(builder, plan)
            if self.running or self.pending:
                raise ValueError("private_build_output_in_use")
            self.running = True
        try:
            builder._check_cancelled()
            if (
                builder._reprepare(snapshot, snapshot["build_principal"])["fingerprint"]
                != snapshot["fingerprint"]
            ):
                raise ValueError("build_inputs_changed")
            logs = PrivateBuildLogs(snapshot["build_principal"], job_id=snapshot["job_id"])
            builder.logs[snapshot["job_id"]] = logs
            builder._check_cancelled()
            candidate = self.produce(json.loads(payload), logs)
            _hash(candidate.composition_identity)
            if (
                type(candidate.files) is not tuple
                or not candidate.files
                or len(candidate.files) > 32
            ):
                raise ValueError("private_artifact_limit")
            if (
                any(type(data) is not bytes for _, data in candidate.files)
                or sum(len(data) for _, data in candidate.files) > 128 * 1024 * 1024
            ):
                raise ValueError("private_artifact_limit")
            _inventory(
                tuple(
                    InputFile(path, len(data), hashlib.sha256(data).hexdigest())
                    for path, data in candidate.files
                )
            )
            with builder._lock:
                builder._check_cancelled()
                if builder.process.active or builder.process.uncertain:
                    builder.output_uncertain = True
                    raise ValueError("private_build_child_not_joined")
                if (
                    builder._reprepare(snapshot, snapshot["build_principal"])["fingerprint"]
                    != snapshot["fingerprint"]
                ):
                    raise ValueError("build_inputs_changed")
                builder._check_cancelled()
                self.pending[snapshot["job_id"]] = (payload, candidate)
            return "private_build_succeeded_no_flash"
        finally:
            with builder._lock:
                if builder.process.active or builder.process.uncertain:
                    builder.output_uncertain = True
                self.running = False

    def commit(
        self,
        builder: "FirmwareBuild",
        store: "Store",
        plan: dict[str, Any],
        live: Callable[[], bool],
    ) -> str:
        with builder._lock:
            self._check_accepting()
            self._require_operation(builder, plan)
            if self._operation is None or self._operation[1] is not store:
                raise ValueError("private_build_finalizer_invalid")
            payload, candidate = self.pending[plan["job_id"]]
            if payload != _plan(plan):
                raise ValueError("private_build_candidate_changed")
            receipt = commit_private_candidate(
                builder,
                store,
                self.artifacts,
                plan=plan,
                composition_identity=candidate.composition_identity,
                files=candidate.files,
                live=live,
            )
            self.pending.pop(plan["job_id"])
            self._close_operation()
            return receipt

    def discard(self, job_id: str) -> None:
        self.pending.pop(job_id, None)
        if self._operation is not None and json.loads(self._operation[2])["job_id"] == job_id:
            self._close_operation()
