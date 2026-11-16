"""Thread-owned read-only receipt connections; production wiring is separate."""

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .private_input_store import PrivateInputReader
from .private_state_windows import private_database_path, verified_private_database_path

if TYPE_CHECKING:
    from .store import Store

PathGuard = Callable[[Path], AbstractContextManager[tuple[int, bytes]]]


@dataclass(frozen=True)
class PrivateReceiptFactory:
    """Trusted internal factory, never an MCP path or callback input.

    Capture on the Store owner thread. Each call runs entirely on its caller's
    thread, pins the captured identity before connect, and closes before unpinning.
    Only identity from capture onward is established. Python SQLite does not
    expose the existing main connection's OS handle: capture cannot prove that
    its pathname was not replaced before capture. Production needs a path guard
    spanning the main Store open/lifetime (or equivalent independent proof).
    mode=ro blocks SQL/main-DB writes, not all SQLite WAL/SHM filesystem effects.
    This is not a malicious same-identity callback sandbox or an activation grant.
    """

    path: Path = field(repr=False)
    identity: tuple[int, bytes] = field(repr=False)
    path_guard: PathGuard = field(repr=False)

    @classmethod
    def capture(
        cls, store: "Store", *, path_guard: PathGuard = verified_private_database_path
    ) -> "PrivateReceiptFactory":
        path = private_database_path(store.db)
        with path_guard(path) as identity:
            if private_database_path(store.db) != path:
                raise ValueError("private_state_database_changed")
            return cls(path, identity, path_guard)

    @contextmanager
    def __call__(self) -> Iterator[PrivateInputReader]:
        with self.path_guard(self.path) as identity:
            if identity != self.identity:
                raise ValueError("private_state_database_changed")
            # No immutable=1: the broker can legitimately commit new WAL receipts.
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                actual = private_database_path(db)
                if os.path.normcase(str(actual)) != os.path.normcase(str(self.path)):
                    raise ValueError("private_state_database_changed")
                # Existing tables must exist; this never initializes schema.
                db.execute("SELECT receipt,record FROM private_input_receipts LIMIT 0")
                db.execute("SELECT receipt,path,payload FROM private_input_bytes LIMIT 0")
                yield PrivateInputReader(db)
