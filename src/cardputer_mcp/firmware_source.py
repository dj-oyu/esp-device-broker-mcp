"""Existing plan/job approval boundary for source fetch; no build or USB action."""

import asyncio
import threading
from typing import Any

from .build_logs import BuildLogs
from .source_snapshot import SOURCE_ERROR_CODES, SourceError, SourceSnapshots


class FirmwareSource:
    def __init__(self, settings: Any, store: Any) -> None:
        self.settings, self.store = settings, store
        self.snapshots = SourceSnapshots(settings)
        self.stop = threading.Event()
        self.active = False
        self.logs: dict[str, BuildLogs] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.committed: str | None = None

    def begin(self) -> None:
        self.stop.clear()
        self.committed = None

    def request_stop(self, job_id: str) -> bool:
        if self.committed == job_id:
            return False
        self.stop.set()
        return self.active

    def prepare(self, request: Any, principal: str) -> dict[str, Any]:
        self.snapshots.settings = self.settings
        return self.snapshots.prepare(request.project_id, request.source_commit, principal)

    def authorize_receipt(self, project_id: str, receipt: str, principal: str) -> None:
        self.snapshots.settings = self.settings
        _, metadata = self.snapshots.resolve(project_id, receipt, principal)
        job = self.store().job(metadata["fetch_job_id"])
        plan = self.store().plan(job["plan_hash"]) if job is not None else None
        if (
            job is None
            or job["state"] != "succeeded"
            or plan is None
            or plan.get("test") != "firmware_fetch_source"
            or plan.get("source_principal") != principal
            or plan.get("project_id") != project_id
            or plan.get("source_commit") != metadata["commit"]
            or (job.get("diagnostic") or {}).get("source", {}).get("source_receipt") != receipt
        ):
            raise SourceError("successful_owned_source_fetch_required")

    async def execute(self, plan: dict[str, Any], stage: Any, live: Any) -> str:
        if self.active:
            raise SourceError("source_fetch_busy")
        self.active = True
        while len(self.logs) >= 8:
            del self.logs[next(iter(self.logs))]
        logs = self.logs[plan["job_id"]] = BuildLogs(plan["source_principal"])
        loop = asyncio.get_running_loop()

        async def check() -> bool:
            return not self.stop.is_set() and live() is True

        def valid() -> bool:
            return asyncio.run_coroutine_threadsafe(check(), loop).result(timeout=5)

        def append(name: str) -> None:
            logs.append("-- " + name)
            stage(name)

        def emit(name: str) -> None:
            loop.call_soon_threadsafe(append, name)

        worker = asyncio.create_task(asyncio.to_thread(self.snapshots.fetch, plan, valid, emit))
        interrupted = False
        try:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    interrupted = True
                    self.stop.set()
            result = worker.result()
            if interrupted or self.stop.is_set() or live() is not True:
                raise SourceError("source_fetch_interrupted")
            self.results[plan["job_id"]] = result
            return "source_snapshot_fetched_no_build_no_flash"
        except Exception as failure:
            code = failure.code if isinstance(failure, SourceError) else "source_fetch_failed"
            self.record_failure(plan, code)
            raise SourceError(code) from None
        finally:
            self.active = False

    def record_failure(self, plan: dict[str, Any], code: str) -> None:
        code = SourceError(code).code
        self.results.pop(plan["job_id"], None)
        logs = self.logs.get(plan["job_id"])
        if logs is None:
            while len(self.logs) >= 8:
                del self.logs[next(iter(self.logs))]
            logs = self.logs[plan["job_id"]] = BuildLogs(plan["source_principal"])
        message = "error: source fetch failed: " + code
        if not logs.entries or logs.entries[-1]["text"] != message:
            logs.append(message)

    def commit_success(self, plan: dict[str, Any], result: str, live: Any) -> None:
        if self.stop.is_set() or live() is not True or self.active:
            raise SourceError("source_final_commit_not_authorized")
        summary = self.results.pop(plan["job_id"])
        self.store().set_job(plan["job_id"], "succeeded", result, {"source": summary})
        self.committed = plan["job_id"]

    def read_logs(self, request: Any, principal: str) -> dict[str, Any]:
        logs = self.logs.get(request.job_id)
        if logs is None:
            result = dict(
                entries=[],
                expired=True,
                gap=True,
                has_more=False,
                next_cursor=dict(epoch="0" * 32, sequence=0),
            )
        else:
            result = logs.read(principal, request)
        job = self.store().job(request.job_id)
        plan = self.store().plan(job["plan_hash"]) if job else None
        if plan is None or plan.get("source_principal") != principal:
            raise SourceError("source_log_not_available")
        if job is not None and job["state"] == "succeeded":
            result["source"] = job["diagnostic"]["source"]
        diagnostic = job.get("diagnostic") if job is not None else None
        failure_code = diagnostic.get("code") if isinstance(diagnostic, dict) else None
        failed = (
            job is not None
            and job["state"] == "needs_review"
            and type(failure_code) is str
            and failure_code in SOURCE_ERROR_CODES
        )
        if failed:
            result["diagnostic"] = {"code": failure_code}
        try:
            result["source_cache"] = self.snapshots.usage(plan["project_id"])
        except Exception as failure:
            if not failed:
                raise
            result["source_cache"] = {
                "available": False,
                "code": failure.code if isinstance(failure, SourceError) else "source_fetch_failed",
            }
        return result
