"""Registered source/build jobs: authenticated profile scope, no USB lease."""

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from .broker import BrokerError, digest_plan
from .build_locations import BuildLocationError
from .build_process import ProcessDeadlineExpired
from .config import Settings
from .continuation import TicketError
from .firmware_build import BuildPreflightFailure, hash_file
from .logs import Event
from .models import CancelRequest, ExecuteRequest, Origin, PrepareRequest, TestName
from .registration_digest import project_digest, project_digests
from .source_preflight import SourceReadWorker, settings_identity, source_inputs
from .source_retention import PLAN_LEASE_SECONDS
from .source_snapshot import SourceError

if TYPE_CHECKING:
    from .broker import Broker

PROFILE_TESTS = frozenset({TestName.SOURCE.value, TestName.BUILD.value})
# Whole-job limits, independent of connection lifetime and physical leases.
PROFILE_SECONDS = {TestName.SOURCE.value: 180, TestName.BUILD.value: 1800}


@dataclass(frozen=True)
class _SourcePreflight:
    store: Any
    epoch: str
    settings: Settings
    settings_hash: str
    details: dict[str, Any]
    storage: dict[str, Any]


class ProfileJobs:
    def __init__(self, broker: "Broker") -> None:
        self.broker = broker
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(broker.settings.max_queue)
        self.worker: asyncio.Task[None] | None = None
        self.active: str | None = None
        self.stops: dict[str, str] = {}
        self.deadline = 0.0
        self.uncertain = False
        self.state_uncertain = False
        self.source_reads = SourceReadWorker()

    def _uncertain(self, *, allow_owned_cleanup: bool = False) -> bool:
        process = self.broker.firmware_build.process
        # Only a live profile owner with an undecided cleanup may hold a peer.
        # Failed cleanup clears cleanup_pending before releasing any ownership.
        owned_cleanup = (
            allow_owned_cleanup
            and self.active is not None
            and process.active
            and process.cleanup_pending
        )
        return bool(
            self.uncertain
            or self.state_uncertain
            or self.broker.firmware_source.snapshots.uncertain
            or self.broker.firmware_build.output_uncertain
            or (process.uncertain and not owned_cleanup)
            or (
                self.active is None
                and self.broker.active_job is None
                and (
                    self.broker.firmware_source.active or self.broker.firmware_build.process.active
                )
            )
        )

    def start(self) -> None:
        self.worker = asyncio.create_task(self._work(), name="registered-project-worker")

    def _authorized(self, principal: str, origin: str) -> bool:
        try:
            valid = self.broker.principal_valid
            return bool(
                principal
                and valid is not None
                and valid(principal, origin, frozenset({"read", "control"})) is True
            )
        except Exception:
            return False

    def _registration(self, project_id: str) -> str:
        project = self.broker.settings.build_projects.get(project_id)
        if project is None:
            raise BrokerError("registered_build_project_required")
        return project_digest(project)

    def _registrations(self, project_id: str) -> frozenset[str]:
        project = self.broker.settings.build_projects.get(project_id)
        if project is None:
            raise BrokerError("registered_build_project_required")
        return project_digests(project)

    def _sync_settings(self) -> None:
        self.broker.firmware_source.settings = self.broker.settings
        self.broker.firmware_source.snapshots.settings = self.broker.settings
        self.broker.firmware_build.settings = self.broker.settings

    async def prepare_source(
        self,
        request: PrepareRequest,
        principal: str,
        origin: str,
        *,
        reauthorize: Callable[[], tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        broker, store = self.broker, self.broker._store()
        if broker.closing or not self._authorized(principal, origin):
            raise BrokerError("principal_not_authorized")
        if self._uncertain():
            raise BrokerError("recovery_review_required")
        if request.test != TestName.SOURCE or request.project_id is None:
            raise BrokerError("registered_build_project_required")
        self._registration(request.project_id)
        settings, epoch = broker.settings, broker.epoch
        captured = settings.model_copy(deep=True)
        identity = settings_identity(captured)
        owned_request = request.model_copy(deep=True)
        if owned_request.project_id is None or owned_request.source_commit is None:
            raise SourceError("source_full_commit_required")
        try:
            details, storage = await self.source_reads.run(
                partial(
                    source_inputs,
                    captured,
                    owned_request.project_id,
                    owned_request.source_commit,
                    principal,
                )
            )
        except SourceError:
            # Failure counters are private read output too. Never publish them
            # under a changed grant, registration or resident incarnation.
            if reauthorize is not None and reauthorize() != (principal, origin):
                raise BrokerError("principal_not_authorized") from None
            evidence = _SourcePreflight(store, epoch, settings, identity, {}, {})
            if broker.closing or not self._source_current(evidence):
                raise BrokerError("profile_or_principal_no_longer_valid") from None
            raise
        if reauthorize is not None and reauthorize() != (principal, origin):
            raise BrokerError("principal_not_authorized")
        evidence = _SourcePreflight(store, epoch, settings, identity, details, storage)
        return self.prepare(owned_request, principal, origin, _source=evidence)

    def _source_current(self, evidence: _SourcePreflight) -> bool:
        broker = self.broker
        return (
            broker.store is evidence.store
            and broker.epoch == evidence.epoch
            and broker.settings is evidence.settings
            and settings_identity(broker.settings) == evidence.settings_hash
        )

    def prepare(
        self,
        request: PrepareRequest,
        principal: str,
        origin: str,
        *,
        _source: _SourcePreflight | None = None,
    ) -> dict[str, Any]:
        broker, store = self.broker, self.broker._store()
        if broker.closing or not self._authorized(principal, origin):
            raise BrokerError("principal_not_authorized")
        # History is diagnostic. Current process/output/cache uncertainty
        # still blocks this independent source/build lane.
        if self._uncertain():
            raise BrokerError("recovery_review_required")
        if request.test.value not in PROFILE_TESTS or request.project_id is None:
            raise BrokerError("registered_build_project_required")
        registration = self._registration(request.project_id)
        if request.after is not None:
            return self._prepare_deferred(request, principal, origin, registration)
        if _source is not None:
            if request.test != TestName.SOURCE or not self._source_current(_source):
                raise BrokerError("profile_or_principal_no_longer_valid")
        else:
            self._sync_settings()
        if request.test == TestName.SOURCE:
            if _source is not None:
                details, storage = _source.details, _source.storage
            else:
                details = broker.firmware_source.prepare(request, principal)
                storage = {
                    "source_cache": broker.firmware_source.snapshots.usage(request.project_id)
                }
        else:
            if request.source_receipt is not None:
                broker.firmware_source.authorize_receipt(
                    request.project_id, request.source_receipt, principal
                )
            details = broker.firmware_build.prepare(
                request.project_id,
                principal,
                request.source_receipt,
                request.derby_detail_mode,
                **(
                    {"derby_dma_mode": request.derby_dma_mode}
                    if request.derby_dma_mode is not None
                    else {}
                ),
            )
            storage = broker.firmware_build.storage(request.project_id)
        plan = dict(
            version=2,
            execution_context="registered_project",
            broker_epoch=broker.epoch,
            job_id=request.job_id,
            test=request.test.value,
            principal_id=principal,
            origin=origin,
            registration_sha256=registration,
            execution_seconds=PROFILE_SECONDS[request.test.value],
            **details,
            **(
                {
                    "profile_sha256": hash_file(
                        broker.settings.build_projects[request.project_id].trusted_profile
                    )
                }
                if request.test == TestName.BUILD
                else {}
            ),
        )
        if request.test == TestName.BUILD and request.source_receipt is not None:
            expiry = time.time() + PLAN_LEASE_SECONDS
            deadline = broker.clock() + PLAN_LEASE_SECONDS
            # Repeated preparation of the same inputs must not consume history
            # or silently extend a still executable reservation.
            for row in store.db.execute(
                "SELECT payload FROM plans WHERE json_extract(payload,'$.job_id')=? LIMIT 10001",
                (request.job_id,),
            ):
                prior = json.loads(row[0])
                prior_expiry = prior.pop("source_plan_expires_at", 0)
                prior_deadline = prior.pop("source_plan_deadline", 0)
                if (
                    prior == plan
                    and isinstance(prior_deadline, (int, float))
                    and prior_deadline > broker.clock()
                ):
                    expiry = prior_expiry
                    deadline = prior_deadline
                    break
            plan["source_plan_expires_at"] = expiry
            plan["source_plan_deadline"] = deadline
        digest = digest_plan(plan)
        existing = store.job(request.job_id)
        if existing and existing["plan_hash"] != digest:
            raise BrokerError("job_id_conflict")
        if store.plan(digest) is None and store.record_count("plans") >= 10000:
            raise BrokerError("history_full_archive_offline")
        # Callbacks above may invalidate configuration or authorization.
        if not self._binding_live(plan):
            raise BrokerError("profile_or_principal_no_longer_valid")
        if self._uncertain():
            raise BrokerError("recovery_review_required")
        if _source is not None and not self._source_current(_source):
            raise BrokerError("profile_or_principal_no_longer_valid")
        # The awaited SOURCE path must recheck history after the final authority
        # callback, which can itself update durable state or configuration.
        if _source is not None:
            existing = store.job(request.job_id)
            if existing and existing["plan_hash"] != digest:
                raise BrokerError("job_id_conflict")
            if store.plan(digest) is None and store.record_count("plans") >= 10000:
                raise BrokerError("history_full_archive_offline")
        store.put_plan(digest, plan)
        return dict(plan_hash=digest, plan=plan, storage=storage, approval_required=False)

    def _prepare_deferred(
        self, request: PrepareRequest, principal: str, origin: str, registration: str
    ) -> dict[str, Any]:
        """Plan a build that continues a source fetch which may still be running."""
        broker, store = self.broker, self.broker._store()
        assert request.after is not None and request.project_id is not None
        try:
            ticket = broker.tickets.peek(request.after, principal)
        except TicketError as exc:
            raise BrokerError(str(exc)) from None
        parent_id = str(ticket["parent_job_id"])
        parent = store.job(parent_id)
        parent_plan = store.plan(parent["plan_hash"]) if parent is not None else None
        if (
            parent is None
            or parent_plan is None
            or parent_plan.get("test") != TestName.SOURCE.value
            or parent_plan.get("source_principal") != principal
            or parent_plan.get("project_id") != request.project_id
        ):
            raise BrokerError("ticket_invalid")
        if parent["state"] not in ("awaiting_approval", "queued", "running", "succeeded"):
            raise BrokerError("chain_broken")
        plan: dict[str, Any] = dict(
            version=2,
            execution_context="registered_project_deferred",
            broker_epoch=broker.epoch,
            job_id=request.job_id,
            test=TestName.BUILD.value,
            principal_id=principal,
            origin=origin,
            project_id=request.project_id,
            registration_sha256=registration,
            continues={"parent_job_id": parent_id},
        )
        if request.derby_detail_mode is not None:
            plan["derby_detail_mode"] = request.derby_detail_mode.value
        if request.derby_dma_mode is not None:
            plan["derby_dma_mode"] = request.derby_dma_mode.value
        digest = digest_plan(plan)
        existing = store.job(request.job_id)
        waiting = broker.deferred.get(request.job_id)
        if (existing and existing["plan_hash"] != digest) or (
            waiting and waiting["plan_hash"] != digest
        ):
            raise BrokerError("job_id_conflict")
        if store.plan(digest) is None and store.record_count("plans") >= 10000:
            raise BrokerError("history_full_archive_offline")
        store.put_plan(digest, plan)
        return dict(plan_hash=digest, plan=plan, storage={}, approval_required=False)

    def _execute_deferred(
        self, request: ExecuteRequest, plan: dict[str, Any], principal: str, origin: str
    ) -> dict[str, Any]:
        """Accept the owner's explicit request now; queue it when the parent succeeds."""
        broker, store = self.broker, self.broker._store()
        if (
            digest_plan(plan) != request.plan_hash
            or plan.get("principal_id") != principal
            or plan.get("origin") != origin
            or plan.get("test") != TestName.BUILD.value
        ):
            raise BrokerError("owned_registered_project_plan_required")
        waiting = broker.deferred.get(plan["job_id"])
        existing = store.job(plan["job_id"])
        if existing is not None or waiting is not None:
            if (existing or waiting or {}).get("plan_hash") not in (
                request.plan_hash,
                (waiting or {}).get("plan_hash"),
            ) or (waiting is not None and waiting["plan_hash"] != request.plan_hash):
                raise BrokerError("job_id_conflict")
            if waiting is not None:
                return {**broker.deferred.view(waiting), "deduplicated": True}
            assert existing is not None
            return {**existing, "deduplicated": True}
        if plan["broker_epoch"] != broker.epoch or not self._authorized(principal, origin):
            raise BrokerError("profile_or_principal_no_longer_valid")
        if store.record_count("jobs") >= 10000:
            raise BrokerError("history_full_archive_offline")
        parent_id = str(plan["continues"]["parent_job_id"])
        ticket = broker.tickets.row_for_parent(parent_id)
        if ticket is None:
            raise BrokerError("ticket_invalid")
        try:
            broker.deferred.add(plan["job_id"], request.plan_hash, parent_id, principal, origin)
            try:
                broker.tickets.consume(str(ticket["ticket_id"]), principal)
                broker.tickets.note_consumer(str(ticket["ticket_id"]), plan["job_id"])
            except TicketError:
                broker.deferred.remove(plan["job_id"])
                raise
        except TicketError as exc:
            raise BrokerError(str(exc)) from None
        row = broker.deferred.get(plan["job_id"])
        assert row is not None
        return {**broker.deferred.view(row), "deduplicated": False}

    def promote(self, row: dict[str, Any]) -> None:
        """Queue a waiting build once its source fetch succeeded; cancel it if that failed."""
        broker, store = self.broker, self.broker._store()
        job_id = str(row["job_id"])

        def cancel(reason: str) -> None:
            broker.deferred.finish(job_id, "cancelled", reason)

        parent = store.job(str(row["parent_job_id"]))
        if parent is None:
            return cancel("chain_broken")
        if parent["state"] in ("awaiting_approval", "queued", "running"):
            return
        if parent["state"] != "succeeded":
            return cancel("chain_broken")
        plan = store.plan(str(row["plan_hash"]))
        if plan is None or digest_plan(plan) != row["plan_hash"]:
            return cancel("plan_not_found_or_changed")
        receipt = ((parent.get("diagnostic") or {}).get("source") or {}).get("source_receipt")
        if not isinstance(receipt, str):
            return cancel("chain_broken")
        modes = {k: plan[k] for k in ("derby_detail_mode", "derby_dma_mode") if k in plan}
        try:
            prepared = self.prepare(
                PrepareRequest(
                    job_id=job_id,
                    test=TestName.BUILD,
                    project_id=plan["project_id"],
                    source_receipt=receipt,
                    **modes,
                ),
                str(row["principal_id"]),
                str(row["origin"]),
            )
            self.execute(
                ExecuteRequest(plan_hash=prepared["plan_hash"]),
                str(row["principal_id"]),
                str(row["origin"]),
            )
        except BrokerError as exc:
            if str(exc) == "queue_full":
                return  # transient: the lane is full; keep waiting
            return cancel(str(exc))
        except SourceError as exc:
            return cancel(exc.code)
        except ValueError:
            return cancel("chain_broken")
        broker.deferred.finish(job_id, "promoted", None, prepared["plan_hash"])

    def _plan(self, digest: str, principal: str, origin: str) -> dict[str, Any]:
        plan = self.broker._store().plan(digest)
        if (
            plan is None
            or digest_plan(plan) != digest
            or plan.get("version") != 2
            or plan.get("execution_context") != "registered_project"
            or plan.get("test") not in PROFILE_TESTS
            or plan.get("principal_id") != principal
            or plan.get("origin") != origin
        ):
            raise BrokerError("owned_registered_project_plan_required")
        return plan

    def _binding_live(self, plan: dict[str, Any]) -> bool:
        broker = self.broker
        try:
            registration = self._registration(plan["project_id"])
            accepted = self._registrations(plan["project_id"])
            authorized = self._authorized(plan["principal_id"], plan["origin"])
            return bool(
                authorized
                and not broker.closing
                and broker._state_lock.is_locked
                and plan["broker_epoch"] == broker.epoch
                and plan["registration_sha256"] in accepted
                and (
                    "source_plan_deadline" not in plan
                    or self.broker._store().job(plan["job_id"]) is not None
                    or broker.clock() < plan["source_plan_deadline"]
                )
                and self._registration(plan["project_id"]) == registration
                and (
                    plan["test"] != TestName.BUILD.value
                    or hash_file(broker.settings.build_projects[plan["project_id"]].trusted_profile)
                    == plan["profile_sha256"]
                )
            )
        except Exception:
            return False

    def execute(self, request: ExecuteRequest, principal: str, origin: str) -> dict[str, Any]:
        if not self._authorized(principal, origin):
            raise BrokerError("principal_not_authorized")
        stored = self.broker._store().plan(request.plan_hash)
        if stored is not None and stored.get("execution_context") == "registered_project_deferred":
            return self._execute_deferred(request, stored, principal, origin)
        plan = self._plan(request.plan_hash, principal, origin)
        store = self.broker._store()
        existing = store.job(plan["job_id"])
        if existing:
            if existing["plan_hash"] != request.plan_hash:
                raise BrokerError("job_id_conflict")
            return {**existing, "deduplicated": True}
        if self._uncertain():
            raise BrokerError("recovery_review_required")
        if not self._binding_live(plan):
            raise BrokerError("profile_or_principal_no_longer_valid")
        if self._uncertain():
            raise BrokerError("recovery_review_required")
        if self.queue.qsize() >= self.broker.settings.max_queue:
            raise BrokerError("queue_full")
        if store.record_count("jobs") >= 10000:
            raise BrokerError("history_full_archive_offline")
        # No await/callback between durable enqueue and queue insertion.
        store.add_job(plan["job_id"], request.plan_hash)
        self.queue.put_nowait(plan["job_id"])
        return {**self.broker.job(plan["job_id"]), "deduplicated": False}

    def cancel(self, request: CancelRequest, principal: str, origin: str) -> dict[str, Any]:
        if not self._authorized(principal, origin):
            raise BrokerError("principal_not_authorized")
        job = self.broker._store().job(request.job_id)
        if job is None:
            raise BrokerError("job_not_found")
        plan = self._plan(job["plan_hash"], principal, origin)
        if job["state"] == "queued":
            self.broker._store().set_job(request.job_id, "cancelled", "client_cancelled")
        elif job["state"] == "running":
            self._stop(plan, "client_cancelled")
            return {**job, "stop_requested": True, "requires_review": False}
        return self.broker.job(request.job_id)

    def operator_cancellable(
        self, plan: dict[str, Any] | None, principal: str, origin: str
    ) -> bool:
        return bool(
            origin == Origin.LOCAL.value
            and self._authorized(principal, origin)
            and self._operator_plan(plan)
        )

    def _operator_plan(self, plan: dict[str, Any] | None) -> bool:
        return bool(
            plan is not None
            and plan.get("version") == 2
            and plan.get("execution_context") == "registered_project"
            and plan.get("test") in PROFILE_TESTS
            and plan.get("broker_epoch") == self.broker.epoch
            and isinstance(plan.get("project_id"), str)
            and plan["project_id"] in self.broker.settings.build_projects
        )

    def operator_cancel(
        self, job_id: str, plan_hash: str, broker_epoch: str, principal: str, origin: str
    ) -> dict[str, Any]:
        # Separate local stop authority: never impersonate or replace the plan owner.
        if origin != Origin.LOCAL.value or not self._authorized(principal, origin):
            raise BrokerError("local_operator_required")
        store = self.broker._store()
        job = store.job(job_id)
        if broker_epoch != self.broker.epoch or job is None or job["plan_hash"] != plan_hash:
            raise BrokerError("stale_operator_snapshot")
        plan = store.plan(plan_hash)
        if (
            not self._operator_plan(plan)
            or plan is None
            or plan.get("job_id") != job_id
            or digest_plan(plan) != plan_hash
        ):
            raise BrokerError("registered_project_cancel_required")
        if job["state"] == "queued":
            store.set_job(job_id, "cancelled", "operator_cancelled")
        elif job["state"] == "running":
            self._stop(plan, "operator_cancelled")
            return {**job, "stop_requested": True, "requires_review": False}
        # A completion race is read-only; it cannot change a terminal outcome.
        return self.broker.job(job_id)

    def _stop(self, plan: dict[str, Any], reason: str) -> None:
        self.stops.setdefault(plan["job_id"], reason)
        if plan["test"] == TestName.SOURCE.value:
            self.broker.firmware_source.request_stop(plan["job_id"])
        else:
            self.broker.firmware_build.request_stop(plan["job_id"])

    def _live(self, plan: dict[str, Any]) -> bool:
        if (
            plan["job_id"] in self.stops
            or self.broker.clock() >= self.deadline
            or not self._binding_live(plan)
        ):
            return False
        job = self.broker._store().job(plan["job_id"])
        return bool(
            job is not None
            and job["state"] == "running"
            and job["plan_hash"] == digest_plan(plan)
            and self.broker.clock() < self.deadline
            and plan["job_id"] not in self.stops
        )

    async def _watch(self, plan: dict[str, Any]) -> None:
        # Authority checks must remain live through owned process teardown.
        # BuildProcess temporarily marks uncertainty during normal reader joins;
        # do not turn that resource hold into irreversible job cancellation.
        # Admission/dispatch and post-worker completion still check uncertainty.
        while True:
            if self.broker.clock() >= self.deadline:
                self._stop(plan, "execution_deadline_expired")
            elif not self._binding_live(plan):
                self._stop(plan, "profile_or_principal_no_longer_valid")
            await asyncio.sleep(0.1)

    async def _work(self) -> None:
        broker = self.broker
        while True:
            job_id = await self.queue.get()
            try:
                if job_id is None:
                    return
                store = broker._store()
                job = store.job(job_id)
                if job is None or job["state"] != "queued":
                    continue
                plan = store.plan(job["plan_hash"])
                if plan is None or digest_plan(plan) != job["plan_hash"]:
                    store.set_job(job_id, "needs_review", "profile_plan_inconsistent")
                    continue
                if not self._binding_live(plan) or self._uncertain():
                    store.set_job(job_id, "cancelled", "profile_or_principal_no_longer_valid")
                    continue
                self._sync_settings()
                self.active = job_id
                self.deadline = broker.clock() + plan["execution_seconds"]
                store.set_job(job_id, "running")
                watch = asyncio.create_task(self._watch(plan))
                source = plan["test"] == TestName.SOURCE.value

                def stage(name: str, job: str = job_id, db: Any = store) -> None:
                    db.set_stage(job, name)

                publication = [False]

                def live(
                    prepared: dict[str, Any] = plan, publishing: list[bool] = publication
                ) -> bool:
                    # Running process monitors may observe owned teardown. Once
                    # the worker has returned, publication must also recheck all
                    # resource latches after any authentication callback.
                    return self._live(prepared) and (not publishing[0] or not self._uncertain())

                committing = False
                try:
                    if source:
                        broker.firmware_source.begin()
                        result = await broker.firmware_source.execute(plan, stage, live)
                    else:
                        broker.firmware_build.begin()
                        broker.firmware_build.authority = live
                        result = await broker.firmware_build.execute(
                            plan, stage, defer_publish=True
                        )
                    if self._uncertain() or not self._live(plan):
                        self._stop(plan, "profile_or_principal_no_longer_valid")
                        raise ValueError("profile_job_stopped")
                    committing = True
                    publication[0] = True
                    if source:
                        broker.firmware_source.commit_success(plan, result, live)
                    else:
                        broker.firmware_build.commit_deferred(plan, live)
                        store.set_job(job_id, "succeeded", result)
                except Exception as failure:
                    code = (
                        failure.code
                        if isinstance(
                            failure, (SourceError, BuildPreflightFailure, BuildLocationError)
                        )
                        else "execution_deadline_expired"
                        if isinstance(failure, ProcessDeadlineExpired)
                        else "source_fetch_failed"
                        if source
                        else "firmware_build_failed"
                    )
                    if source:
                        broker.firmware_source.record_failure(plan, code)
                    else:
                        broker.firmware_build.discard_deferred(job_id)
                    if not self._live(plan):
                        self._stop(
                            plan,
                            "execution_deadline_expired"
                            if broker.clock() >= self.deadline
                            else "profile_or_principal_no_longer_valid",
                        )
                    reason = self.stops.get(job_id)
                    uncertain = (
                        (
                            committing
                            and reason is None
                            and not isinstance(failure, BuildPreflightFailure)
                        )
                        or isinstance(failure, sqlite3.Error)
                        or broker.firmware_source.snapshots.uncertain
                        or broker.firmware_source.active
                        or broker.firmware_build.process.active
                        or broker.firmware_build.process.uncertain
                        or broker.firmware_build.output_uncertain
                    )
                    if broker.firmware_source.snapshots.uncertain:
                        code = "source_cleanup_unconfirmed"
                    elif broker.firmware_build.process.uncertain:
                        code = "build_process_exit_unconfirmed"
                    elif broker.firmware_build.output_uncertain:
                        code = "build_output_release_unconfirmed"
                    elif isinstance(failure, sqlite3.Error):
                        code = "registered_project_state_inconsistent"
                    state = "needs_review" if uncertain else "cancelled" if reason else "failed"
                    outcome = code if uncertain else reason or code
                    diagnostic: dict[str, Any] = {"code": outcome}
                    if (
                        isinstance(failure, BuildPreflightFailure)
                        and outcome == failure.code
                        and failure.subprocess is not None
                    ):
                        diagnostic = failure.public_diagnostic()
                    if source and "explanation" in SourceError(outcome).public():
                        diagnostic["explanation"] = SourceError(outcome).public()["explanation"]
                    if isinstance(failure, SourceError) and outcome == failure.code:
                        if "scan" in failure.public():
                            diagnostic["scan"] = failure.public()["scan"]
                    store.set_job(job_id, state, outcome, diagnostic)
                    if uncertain:
                        self.state_uncertain = True
                        broker.logs.append(Event.REVIEW)
                finally:
                    watch.cancel()
                    await asyncio.gather(watch, return_exceptions=True)
                    broker.firmware_build.authority = None
                    self.stops.pop(job_id, None)
                    self.active = None
            except Exception:
                # A failure to read/write durable state cannot be represented as
                # an ordinary job failure. Keep this lane closed until recovery.
                self.uncertain = True
                broker.logs.append(Event.REVIEW)
            finally:
                self.queue.task_done()

    async def close(self) -> None:
        await self.source_reads.close()
        if self.active is not None:
            job = self.broker._store().job(self.active)
            if job is not None:
                plan = self.broker._store().plan(job["plan_hash"])
                if plan is not None:
                    self._stop(plan, "broker_shutdown")
        if self.worker is not None:
            await self.queue.put(None)
            await asyncio.shield(self.worker)
            self.worker = None
