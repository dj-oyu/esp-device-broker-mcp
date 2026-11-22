"""One owned SOURCE read worker. No Store, broker or authentication callbacks."""

import asyncio
import copy
import hashlib
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from .config import Settings
from .source_snapshot import SourceError, SourceSnapshots

_T = TypeVar("_T")


def settings_identity(settings: Settings) -> str:
    return hashlib.sha256(settings.model_dump_json().encode()).hexdigest()


def source_inputs(
    settings: Settings, project_id: str, commit: str, principal: str, stop: threading.Event
) -> tuple[dict[str, Any], dict[str, Any]]:
    if stop.is_set():
        raise SourceError("source_fetch_interrupted")
    try:
        snapshots = SourceSnapshots(settings)
        details = snapshots.prepare(project_id, commit, principal)
        usage = snapshots.usage(project_id, stop=stop)
    except SourceError:
        raise
    except Exception:
        raise SourceError("source_fetch_failed") from None
    if stop.is_set():
        raise SourceError("source_fetch_interrupted")
    # prepare's limits are a shared module constant; detach all returned inputs.
    return copy.deepcopy(details), {"source_cache": usage}


def source_usage(settings: Settings, project_id: str, stop: threading.Event) -> dict[str, int]:
    try:
        return SourceSnapshots(settings).usage(project_id, stop=stop)
    except SourceError:
        raise
    except Exception:
        raise SourceError("source_fetch_failed") from None


def source_inventory(settings: Settings, project_id: str, stop: threading.Event) -> dict[str, Any]:
    try:
        return SourceSnapshots(settings).inventory(project_id, stop=stop)
    except SourceError:
        raise
    except Exception:
        raise SourceError("source_fetch_failed") from None


SOURCE_READ_WAIT_SECONDS = 120.0


class SourceReadWorker:
    """One source-cache read at a time; later requests wait their turn, in order.

    Cancellation prevents publication immediately, but cannot interrupt a stuck
    OS filesystem syscall. Shutdown retains ownership until that thread returns.
    Two clients can ask at once, so a request waits (bounded) behind the earlier
    one instead of being refused; a closing worker still refuses at once.
    """

    def __init__(self) -> None:
        self.worker: asyncio.Task[Any] | None = None
        self.stop: threading.Event | None = None
        self.closing = False
        self._turn = asyncio.Lock()  # FIFO: asyncio.Lock wakes waiters in arrival order

    async def run(self, action: Callable[[threading.Event], _T]) -> _T:
        if self.closing:
            raise SourceError("source_fetch_busy")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SOURCE_READ_WAIT_SECONDS
        try:
            await asyncio.wait_for(self._turn.acquire(), SOURCE_READ_WAIT_SECONDS)
        except TimeoutError:
            raise SourceError("source_fetch_busy") from None
        try:
            # A cancelled caller can leave its thread running; that thread still
            # owns the cache guard, so wait for it too (within the same bound).
            while self.worker is not None and not self.worker.done() and not self.closing:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise SourceError("source_fetch_busy")
                await asyncio.wait({self.worker}, timeout=remaining)
            if self.closing or self.worker is not None:
                raise SourceError("source_fetch_busy")
            stop = threading.Event()
            self.stop = stop
            worker = asyncio.create_task(asyncio.to_thread(action, stop))
            self.worker = worker
            worker.add_done_callback(self._completed)
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                stop.set()
                raise
        finally:
            self._turn.release()

    def _completed(self, worker: asyncio.Task[Any]) -> None:
        if not worker.cancelled():
            worker.exception()  # Retrieve errors even after the request was cancelled.
        if self.worker is worker:
            self.worker = None
            self.stop = None

    async def close(self) -> None:
        self.closing = True
        if self.stop is not None:
            self.stop.set()
        worker = self.worker
        if worker is not None:
            interrupted = False
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    interrupted = True
                except Exception:
                    break
            if interrupted:
                raise asyncio.CancelledError
