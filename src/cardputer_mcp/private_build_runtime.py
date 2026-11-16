"""Internal deferred wiring. No native producer or production installation yet."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .private_build_finalize import commit_private_candidate
from .private_build_output import PrivateBuildLogs
from .private_input_contract import InputFile, _hash, _inventory

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
    ) -> None:
        self.produce = produce
        self.artifacts = artifacts
        self.pending: dict[str, tuple[str, PrivateBuildCandidate]] = {}
        self.running = False

    def execute(self, builder: "FirmwareBuild", plan: dict[str, Any]) -> str:
        payload = _plan(plan)
        snapshot = json.loads(payload)
        with builder._lock:
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
        return receipt

    def discard(self, job_id: str) -> None:
        self.pending.pop(job_id, None)
