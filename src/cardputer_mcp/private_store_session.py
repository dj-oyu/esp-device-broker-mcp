"""Explicit protected Store lifetime; not installed in the broker runtime."""

import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from types import TracebackType

from .private_input_store import PrivateInputReader
from .private_receipt_factory import PathGuard, PrivateReceiptFactory
from .private_state_windows import private_database_path, verified_private_database_path
from .store import Store


class PrivateStoreSession:
    """Pin an existing protected DB before opening its writable Store connection.

    Store initialization writes schema/WAL. This is NOT a read-only preflight.
    Owner thread opens/closes; worker threads only use receipt_factory. Closing
    rejects new readers and retains Store/guard while any reader is active, until
    the owner explicitly retries close after joining workers. No timeout cleanup.

    Runtime/process supervision is deliberately absent: consumers MUST join every
    child before close, even children not using a receipt reader. This class does
    not install adapters, change ACLs, ingest data or grant execution permission.
    Retain the session object for explicit recovery if context exit fails.
    """

    def __init__(self, path: Path, *, path_guard: PathGuard = verified_private_database_path):
        self._path = path
        self._guard = path_guard
        self._owner = threading.get_ident()
        self._lock = threading.RLock()
        self._held = ExitStack()
        self._store: Store | None = None
        self._factory: PrivateReceiptFactory | None = None
        self._active = 0
        self._accepting = False
        self._opened = False
        self._closed = False
        self._cleanup_uncertain = False

    def _owner_thread(self) -> None:
        if threading.get_ident() != self._owner:
            raise ValueError("private_store_session_owner_thread_required")

    def __enter__(self) -> "PrivateStoreSession":
        self._owner_thread()
        with self._lock:
            if self._opened or self._closed:
                raise ValueError("private_store_session_not_available")
            self._opened = True
            try:
                identity = self._held.enter_context(self._guard(self._path))
                # Keep the object reachable even if initialization raises after connect.
                self._store = Store.__new__(Store)
                Store.__init__(self._store, self._path)
                actual = private_database_path(self._store.db)
                if os.path.normcase(str(actual)) != os.path.normcase(str(self._path)):
                    raise ValueError("private_state_database_changed")
                factory = PrivateReceiptFactory.capture(self._store, path_guard=self._guard)
                if factory.identity != identity:
                    raise ValueError("private_state_database_changed")
                self._factory = factory
                self._accepting = True
                return self
            except BaseException:
                self.close()
                raise

    @property
    def store(self) -> Store:
        self._owner_thread()
        with self._lock:
            if not self._accepting or self._store is None:
                raise ValueError("private_store_session_not_available")
            return self._store

    @property
    def receipt_factory(self) -> Callable[[], AbstractContextManager[PrivateInputReader]]:
        with self._lock:
            if not self._accepting:
                raise ValueError("private_store_session_not_available")
            return self._read

    @contextmanager
    def _read(self) -> Iterator[PrivateInputReader]:
        with self._lock:
            if not self._accepting or self._factory is None:
                raise ValueError("private_store_session_not_available")
            self._active += 1
            factory = self._factory
        try:
            scope = factory()
            try:
                reader = scope.__enter__()
            except BaseException:
                # Enter may fail while unwinding a partially opened SQLite scope.
                # Its close state is opaque; conservatively retain the outer pin.
                with self._lock:
                    self._cleanup_uncertain = True
                    self._accepting = False
                raise
            try:
                yield reader
            except BaseException:
                failure = sys.exc_info()
                try:
                    suppressed = scope.__exit__(*failure)
                except BaseException:
                    with self._lock:
                        self._cleanup_uncertain = True
                        self._accepting = False
                    raise
                if not suppressed:
                    raise
            else:
                try:
                    scope.__exit__(None, None, None)
                except BaseException:
                    with self._lock:
                        self._cleanup_uncertain = True
                        self._accepting = False
                    raise
        finally:
            with self._lock:
                self._active -= 1

    def close(self) -> None:
        self._owner_thread()
        with self._lock:
            self._accepting = False
            if self._cleanup_uncertain:
                raise ValueError("private_store_session_cleanup_uncertain")
            if self._active:
                raise ValueError("private_store_session_readers_active")
            if self._closed:
                return
            # Failure to close SQLite must not release its native lifetime pin.
            if self._store is not None and hasattr(self._store, "db"):
                self._store.close()
            self._store = None
            try:
                self._held.close()
            except BaseException:
                self._cleanup_uncertain = True
                raise
            self._closed = True
            self._factory = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
