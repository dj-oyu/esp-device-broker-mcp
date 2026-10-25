"""Shared readiness wait for in-process uvicorn test servers.

The deadline stays short so a server that genuinely cannot start fails fast. When it
expires, the failure names the uvicorn startup stage that is still pending, the
time at which each stage was first observed, event-loop/GC stall evidence, the
loop thread's stack sampled while the loop was blocked, every thread stack and the
asyncio task call graphs. Observation is test-side only: the helper polls
public-ish ``uvicorn.Server`` state and never patches the broker.

Stacks taken at the deadline only show where the loop is after a synchronous block
has ended. A watchdog thread therefore samples the loop thread's stack once a single
block reaches each ``BLOCK_SAMPLE_AT`` duration; it is joined before the wait returns.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import threading
import time
import traceback
from typing import Any

import uvicorn

# Show bind/lifespan startup errors; access logs stay disabled per fixture.
LOG_LEVEL = "error"
READY_TIMEOUT = 5.0
POLL_SECONDS = 0.01
STALL_SECONDS = 0.25
MAX_TASKS = 40
# Sample the loop thread's stack once a single block reaches each duration.
BLOCK_SAMPLE_AT = (1.0, 3.0, 6.0)
WATCHDOG_SECONDS = 0.05
MAX_BLOCK_SAMPLES = 12

STAGES = (
    "config_load",  # Server._serve has not constructed its lifespan yet.
    "lifespan_startup_queued",  # lifespan.startup is queued; the app has not taken it.
    "app_lifespan_startup",  # The app is running its lifespan startup (broker start).
    "socket_bind_listen",  # Lifespan done; loop.create_server bind/listen pending.
    "post_bind",  # Listener exists; uvicorn has not yet set started.
    "started",
)


class ServerStartupTimeout(TimeoutError):
    """The readiness deadline expired; the message carries the diagnostics."""


def startup_stage(server: uvicorn.Server) -> str:
    if server.started:
        return "started"
    lifespan = getattr(server, "lifespan", None)
    if lifespan is None:
        return "config_load"
    event = getattr(lifespan, "startup_event", None)
    if event is not None and not event.is_set():
        queue = getattr(lifespan, "receive_queue", None)
        if queue is not None and queue.qsize():
            return "lifespan_startup_queued"
        return "app_lifespan_startup"
    if getattr(server, "servers", None) is None:
        return "socket_bind_listen"
    return "post_bind"


class _GcTimer:
    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.longest = 0.0
        self._began: float | None = None

    def __call__(self, phase: str, info: dict[str, Any]) -> None:
        if phase == "start":
            self._began = time.perf_counter()
        elif self._began is not None:
            spent = time.perf_counter() - self._began
            self._began = None
            self.count += 1
            self.total += spent
            self.longest = max(self.longest, spent)


class _StallSampler:
    """Sample the loop thread's stack while the event loop is blocked.

    The loop calls ``poll()`` on every iteration; a daemon thread compares the time
    since the last poll with ``BLOCK_SAMPLE_AT``. Identical stacks are folded
    together, the number of samples is bounded and shared state is behind a lock.
    """

    def __init__(self, loop_thread: int) -> None:
        self._loop_thread = loop_thread
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._began = self._last_poll = time.perf_counter()
        self._block_start = self._last_poll
        self._next_threshold = 0
        self._samples: dict[str, list[str]] = {}
        self._sample_count = 0
        self._thread = threading.Thread(
            target=self._run, name="readiness-stall-sampler", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def poll(self) -> None:
        with self._lock:
            self._last_poll = time.perf_counter()

    def _due(self, now: float) -> float | None:
        """Return the current block length when a new sample is due (lock held)."""
        if self._last_poll != self._block_start:
            self._block_start = self._last_poll  # the loop polled; a new block may begin
            self._next_threshold = 0
        blocked = now - self._last_poll
        due = False
        while (
            self._next_threshold < len(BLOCK_SAMPLE_AT)
            and blocked >= BLOCK_SAMPLE_AT[self._next_threshold]
        ):
            self._next_threshold += 1
            due = True
        return blocked if due and self._sample_count < MAX_BLOCK_SAMPLES else None

    def _run(self) -> None:
        while not self._stop.wait(WATCHDOG_SECONDS):
            now = time.perf_counter()
            with self._lock:
                blocked = self._due(now)
            if blocked is None:
                continue
            frame = sys._current_frames().get(self._loop_thread)
            stack = "".join(traceback.format_stack(frame)) if frame is not None else "<none>\n"
            del frame
            when = f"blocked {blocked:.1f}s at +{now - self._began:.1f}s"
            with self._lock:
                self._samples.setdefault(stack, []).append(when)
                self._sample_count += 1

    def report(self) -> str:
        with self._lock:
            samples = list(self._samples.items())
        if not samples:
            return f"<loop never stayed blocked for {BLOCK_SAMPLE_AT[0]:g}s>"
        return "\n".join(f"--- {'; '.join(whens)} ---\n{stack}" for stack, whens in samples)


def _thread_stacks() -> str:
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    blocks = []
    for ident, frame in sys._current_frames().items():
        stack = "".join(traceback.format_stack(frame))
        blocks.append(f"--- thread {names.get(ident, '?')} ({ident}) ---\n{stack}")
    return "\n".join(blocks)


def _task_graph(task: asyncio.Task[Any]) -> str:
    formatter = getattr(asyncio, "format_call_graph", None)  # Python 3.14+
    if formatter is not None:
        try:
            return str(formatter(task))
        except Exception as exc:  # diagnostics must not mask the timeout
            return f"<call graph unavailable: {exc!r}>"
    # Task.print_stack shows only the outermost frame of a suspended task; follow
    # the await chain instead so the innermost awaited frame is visible.
    lines = [repr(task)]
    awaited: Any = task.get_coro()
    for _ in range(200):
        if awaited is None:
            break
        frame = next(
            (
                getattr(awaited, name)
                for name in ("cr_frame", "ag_frame", "gi_frame")
                if getattr(awaited, name, None) is not None
            ),
            None,
        )
        if frame is None:
            lines.append(f"  awaiting {awaited!r}")
            break
        code = frame.f_code
        lines.append(f'  File "{code.co_filename}", line {frame.f_lineno}, in {code.co_qualname}')
        awaited = next(
            (
                getattr(awaited, name)
                for name in ("cr_await", "ag_await", "gi_yieldfrom")
                if getattr(awaited, name, None) is not None
            ),
            None,
        )
    return "\n".join(lines)


def _task_stacks(server_task: asyncio.Task[Any]) -> str:
    others = [task for task in asyncio.all_tasks() if task is not server_task]
    others.sort(key=lambda task: task.get_name())
    shown = [server_task, *others[: MAX_TASKS - 1]]
    blocks = [f"--- task {task.get_name()} ---\n{_task_graph(task)}" for task in shown]
    if len(others) > MAX_TASKS - 1:
        blocks.append(f"... {len(others) - (MAX_TASKS - 1)} more tasks omitted")
    return "\n".join(blocks)


def readiness_report(
    label: str,
    server: uvicorn.Server,
    task: asyncio.Task[Any],
    *,
    timeout: float,
    first_seen: dict[str, float],
    polls: int,
    longest_gap: float,
    stalls: int,
    gc_timer: _GcTimer,
    sampler: _StallSampler | None = None,
) -> str:
    seen = ", ".join(f"{stage}@{first_seen[stage]:.3f}s" for stage in STAGES if stage in first_seen)
    return (
        f"{label}: uvicorn not ready within {timeout:g}s; "
        f"pending stage: {startup_stage(server)}\n"
        f"stages first observed: {seen}\n"
        f"polls: {polls}, longest poll gap: {longest_gap:.3f}s, "
        f"gaps over {STALL_SECONDS:g}s: {stalls}\n"
        f"gc during wait: {gc_timer.count} collections, {gc_timer.total:.3f}s total, "
        f"longest {gc_timer.longest:.3f}s; threads: {threading.active_count()}, "
        f"tasks: {len(asyncio.all_tasks())}\n"
        f"=== loop thread stacks while loop was blocked ===\n"
        f"{sampler.report() if sampler is not None else '<not sampled>'}\n"
        f"=== thread stacks at deadline ===\n{_thread_stacks()}\n"
        f"=== asyncio tasks (server task first) ===\n{_task_stacks(task)}"
    )


async def wait_until_started(
    server: uvicorn.Server,
    task: asyncio.Task[Any],
    label: str,
    *,
    deadline: float = READY_TIMEOUT,
) -> None:
    """Wait for ``server.started``; an early exit re-raises or fails with ``label``."""
    began = time.perf_counter()
    last = began
    first_seen: dict[str, float] = {}
    polls = stalls = 0
    longest_gap = 0.0
    gc_timer = _GcTimer()
    gc.callbacks.append(gc_timer)
    sampler = _StallSampler(threading.get_ident())
    sampler.start()
    try:
        while True:
            sampler.poll()
            now = time.perf_counter()
            gap = now - last
            last = now
            longest_gap = max(longest_gap, gap)
            stalls += gap > STALL_SECONDS
            polls += 1
            first_seen.setdefault(startup_stage(server), now - began)
            if server.started:
                return
            if task.done():
                await task
                raise AssertionError(f"{label} stopped before readiness")
            if now - began >= deadline:
                report = readiness_report(
                    label,
                    server,
                    task,
                    timeout=deadline,
                    first_seen=first_seen,
                    polls=polls,
                    longest_gap=longest_gap,
                    stalls=stalls,
                    gc_timer=gc_timer,
                    sampler=sampler,
                )
                print(report, file=sys.stderr, flush=True)
                raise ServerStartupTimeout(report)
            await asyncio.sleep(POLL_SECONDS)
    finally:
        sampler.stop()
        gc.callbacks.remove(gc_timer)
