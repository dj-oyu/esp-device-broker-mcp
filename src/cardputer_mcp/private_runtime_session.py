"""Explicit internal adapter ownership and shutdown; no production auto-install."""

import threading
from types import TracebackType

from .firmware_build import FirmwareBuild
from .private_build_runtime import PrivateBuildRuntime
from .private_store_session import PrivateStoreSession


class PrivateRuntimeSession:
    """Install only in an already-open protected session on its owner thread.

    The caller must stop outer job admission and join its worker before retrying
    close. This helper never kills, waits, infers successful joins from a timeout,
    or clears uncertainty. Existing BuildProcess completion bookkeeping supplies
    the child-exit evidence. Runtime admission is irreversibly stopped on close.
    Keep this object for explicit retry after a refused shutdown. Stop the outer
    broker queue before use: this wrapper does not stop ProfileJobs/public work,
    and no broker consumer may keep using this Store after shutdown.
    """

    def __init__(
        self, builder: FirmwareBuild, session: PrivateStoreSession, runtime: PrivateBuildRuntime
    ) -> None:
        self.builder, self.session, self.runtime = builder, session, runtime
        self._owner = threading.get_ident()
        self._shutdown_hold: object | None = None
        self._installed = False
        self._closed = False
        self._previous_factory = builder.private_store_factory
        self._factory = session.receipt_factory

    def _thread(self) -> None:
        if threading.get_ident() != self._owner:
            raise ValueError("private_runtime_session_owner_thread_required")

    def _joined(self) -> None:
        process = self.builder.process
        if (
            self.runtime.running
            or process.active
            or process.uncertain
            or process.cleanup_pending
            or process.owner is not None
            or any(process._retained)
            or self.builder.output_uncertain
        ):
            raise ValueError("private_runtime_session_join_unconfirmed")

    def __enter__(self) -> "PrivateRuntimeSession":
        self._thread()
        with self.builder._lock:
            if self._installed or self._closed or self.builder.private_runtime is not None:
                raise ValueError("private_runtime_session_not_idle")
            self._joined()
            self.builder._check_cancelled()
            if (
                self.runtime.pending
                or self.runtime._operation is not None
                or self.runtime._scope_release_failed
                or not self.runtime._accepting
                or self.builder._pending
                or self.runtime.artifacts.db is not self.session.store.db
                or self.builder.private_store_factory is not self._previous_factory
            ):
                raise ValueError("private_runtime_session_not_idle")
            self._shutdown_hold = self.session.retain_supervisor()
            self.builder.private_store_factory = self._factory
            self.builder.private_runtime = self.runtime
            self._installed = True
            return self

    def close(self) -> None:
        self._thread()
        with self.builder._lock:
            if self._closed:
                return
            if (
                not self._installed
                or self.builder.private_runtime is not self.runtime
                or self.builder.private_store_factory is not self._factory
            ):
                raise ValueError("private_runtime_session_ownership_changed")
            self.runtime.stop_accepting()
            self.session.stop_accepting()
            self.builder.request_stop()
            self._joined()
            for job in tuple(self.runtime.pending):
                self.runtime.discard(job)
            self.runtime.release_after_join()
            if self.runtime._operation is not None or self.runtime._scope_release_failed:
                raise ValueError("private_runtime_session_cleanup_unconfirmed")
            if self._shutdown_hold is not None:
                self.session.release_supervisor(self._shutdown_hold)
                self._shutdown_hold = None
            self.session.close()
            self.builder.private_runtime = None
            self.builder.private_store_factory = self._previous_factory
            self._closed = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
