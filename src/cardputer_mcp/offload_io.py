"""Keep pure file reads/hashing off the broker event loop; SQLite stays on the loop.

A *compute* is a synchronous function whose pure file work goes through
`pure_io` and which performs no side effect before its last pure-I/O call.
Inside `offload`, a `pure_io` call that has no memoized result raises
`IOPending`; the driver runs that single call in a worker thread, memoizes its
value or exception and re-runs the compute on the loop. The final, successful
pass is entirely synchronous: every durable/store check it makes is current when
it returns, and the caller may continue synchronously (no await) into its final
checks and commit.

`complete` runs one synchronous pass using only already memoized results and
reports when the compute would need new I/O, so a caller can fail closed
instead of awaiting between its final checks and its commit.

Outside both, `pure_io` calls its function directly (unchanged synchronous
behaviour for every other caller).
"""

import asyncio
import json
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, TypeVar

_T = TypeVar("_T")
# Each pass needs at most a handful of distinct pure reads; this only bounds a bug.
_MAX_ROUNDS = 64


class IOPending(BaseException):  # noqa: N818 - control flow, not an error
    """A memo miss. BaseException, so validation code's `except Exception` cannot hide it."""

    def __init__(self, key: tuple[str, str], fn: Callable[..., Any], args: tuple[Any, ...]):
        super().__init__(key[0])
        self.key, self.fn, self.args = key, fn, args


class IOMemo:
    """Results of pure file work for one admission/dispatch; never shared between them."""

    def __init__(self) -> None:
        self.results: dict[tuple[str, str], tuple[bool, Any]] = {}

    def __len__(self) -> int:
        return len(self.results)


_active: ContextVar[IOMemo | None] = ContextVar("cardputer_offload_io", default=None)


def _key(name: str, args: tuple[Any, ...]) -> tuple[str, str]:
    return name, json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))


def pure_io(name: str, fn: Callable[..., _T], *args: Any) -> _T:
    """Pure file reading/hashing; `name` plus the JSON form of `args` identifies the call."""
    memo = _active.get()
    if memo is None:
        return fn(*args)
    key = _key(name, args)
    found = memo.results.get(key)
    if found is None:
        raise IOPending(key, fn, args)
    ok, value = found
    if ok:
        return value  # type: ignore[no-any-return]
    raise value


def _capture(fn: Callable[..., Any], args: tuple[Any, ...]) -> tuple[bool, Any]:
    try:
        return True, fn(*args)
    except Exception as exc:
        return False, exc


async def offload(compute: Callable[[], _T], memo: IOMemo | None = None) -> _T:
    """Run `compute` with its pure file work in worker threads.

    Returns the value of the last, fully synchronous pass on the loop; its
    exceptions (other than memo misses) propagate unchanged.
    """
    memo = memo if memo is not None else IOMemo()
    for _ in range(_MAX_ROUNDS):
        token = _active.set(memo)
        try:
            return compute()
        except IOPending as pending:
            missing = pending
        finally:
            _active.reset(token)
        # The context is reset above, so the worker thread runs `fn` directly.
        memo.results[missing.key] = await asyncio.to_thread(_capture, missing.fn, missing.args)
    raise RuntimeError("offload_io_rounds_exceeded")


def complete(compute: Callable[[], _T], memo: IOMemo) -> tuple[bool, _T | None]:
    """One synchronous pass from `memo` only; `(False, None)` when it needs new file work."""
    token = _active.set(memo)
    try:
        return True, compute()
    except IOPending:
        return False, None
    finally:
        _active.reset(token)
