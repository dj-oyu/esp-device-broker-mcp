"""Durable, redacted, append-only per-job logs in ``<state_dir>/job-logs/job-logs.sqlite3``.

Saving is unconditional: every entry handed over is written as it arrives, on
every job outcome. Text is redacted before it touches the database. Writers never
raise into the job; a store failure only marks the log unavailable.

Schema (``PRAGMA user_version = 1``, WAL):

* ``log_jobs``: one row per job (owner, kind, created, bytes, truncated, closed).
* ``log_lines``: one row per entry, clustered by ``(job_id, seq)`` so a job replays
  as one range scan in the order it arrived, with no sort. ``seq`` counts every
  entry of the job. A text line sets ``stream``/``level``/``text``; a structured
  record (``flash_outcome``, ``app_event``) sets ``record``/``data`` and no stream.
  ``t`` (UTC milliseconds) is for display only; ``seq`` alone gives the order.

Rows are buffered and written in one transaction per batch (every 64 rows, 200 ms,
a read, or the end of the job). A crash can lose the last unwritten batch; the job's
end is committed with a full sync. Logs from before the switch stay readable from
their ``<job_id>.jsonl`` files (``job_logs_legacy``), which nothing writes any more.
"""

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .build_logs import redact_log_text
from .device_observation import (
    _ANSI,
    _BINARY,
    _CREDENTIAL,
    _LOG_PREFIX,
    _PEM_MARKER,
    _REDACTED,
    REDACTION_LIMITATIONS,
)
from .job_logs_legacy import LegacyJsonl, _linked

SCHEMA_VERSION = 1
DATABASE_NAME = "job-logs.sqlite3"
STREAMS = frozenset({"source", "build", "flash", "device", "app"})
RECORDS = frozenset({"flash_outcome", "app_event"})
MAX_RECORD_BYTES = 16 * 1024
# A record written with ``reserve`` may exceed the per-job cap by this much, so
# a terminal summary is still saved after streamed output hit the size limit.
RECORD_RESERVE_BYTES = 32 * 1024
MAX_JOB_BYTES = 8 * 1024 * 1024
MAX_JOB_FILES = 500
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_READ_LINES = 1000
MAX_READ_BYTES = 256 * 1024
MAX_DEVICE_LINE_BYTES = 4096
# Bytes counted per entry on top of its text, standing in for the row overhead.
ENTRY_OVERHEAD = 48
FLUSH_ROWS = 64
FLUSH_SECONDS = 0.2
TRUNCATION_MARKER = "[job log size limit reached; further output dropped]"
_PROGRESS = re.compile(
    r"\[\d+/\d+\]|(?:-- |Executing action|Build complete|info: ROM read \d+/\d+ bytes)"
)
_IDENT = re.compile(r"[a-zA-Z0-9_-]{1,64}")

_SCHEMA = """
CREATE TABLE log_jobs(
  job_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  created TEXT NOT NULL,
  updated INTEGER NOT NULL,
  bytes INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  closed INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE TABLE log_lines(
  job_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  t INTEGER NOT NULL,
  stream TEXT,
  level TEXT,
  text TEXT,
  record TEXT,
  data TEXT,
  PRIMARY KEY (job_id, seq)
) WITHOUT ROWID;
"""


class JobLogError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _millis() -> int:
    return int(time.time() * 1000)


def _iso(millis: int) -> str:
    return datetime.fromtimestamp(millis / 1000, UTC).isoformat()


def _redact_value(value: Any, redactions: tuple[str, ...]) -> Any:
    """Apply the job-log text redaction to every string in a JSON value."""
    if isinstance(value, str):
        return redact_log_text(value, redactions)
    if isinstance(value, dict):
        return {
            redact_log_text(str(key), redactions): _redact_value(item, redactions)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, redactions) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_log_text(str(value), redactions)


Row = tuple[int, int, str | None, str | None, str | None, str | None, str | None]


class JobLog:
    """One job's open log. ``append`` and ``record`` never raise."""

    def __init__(self, store: "JobLogStore", job_id: str, size: int, seq: int) -> None:
        self._lock = threading.Lock()
        self._store = store
        self._job_id = job_id
        self._size = size
        self._seq = seq
        self._rows: list[Row] = []
        self._flushed_at = time.monotonic()
        self._open = True
        self.truncated = False
        self.unavailable = False
        self.redactions: tuple[str, ...] = ()

    def append(self, stream: str, text: str, level: str = "unknown") -> None:
        try:
            if stream not in STREAMS:
                raise JobLogError("job_log_stream_invalid")
            safe = redact_log_text(text, self.redactions)
            with self._lock:
                if not self._open or self.truncated:
                    return
                size = len(safe.encode("utf-8", "replace")) + ENTRY_OVERHEAD
                if self._size + size > MAX_JOB_BYTES:
                    self.truncated = True
                    stream, level, safe = stream, "warning", TRUNCATION_MARKER
                    size = len(safe) + ENTRY_OVERHEAD
                self._seq += 1
                self._size += size
                self._rows.append((self._seq, _millis(), stream, level, safe, None, None))
                self._flush_if_due()
        except Exception:
            self.unavailable = True

    def record(self, kind: str, data: dict[str, Any], *, reserve: bool = False) -> None:
        """Append one structured, redacted record; never raises."""
        try:
            if kind not in RECORDS or type(data) is not dict:
                raise JobLogError("job_log_record_invalid")
            safe = _redact_value(data, self.redactions)
            encoded = json.dumps(safe, ensure_ascii=True, separators=(",", ":"))
            with self._lock:
                if not self._open or (self.truncated and not reserve):
                    return
                limit = MAX_JOB_BYTES + (RECORD_RESERVE_BYTES if reserve else 0)
                size = len(encoded) + ENTRY_OVERHEAD
                if size > MAX_RECORD_BYTES or self._size + size > limit:
                    return
                self._seq += 1
                self._size += size
                self._rows.append((self._seq, _millis(), None, None, None, kind, encoded))
                self._flush_if_due()
        except Exception:
            self.unavailable = True

    def _flush_if_due(self) -> None:
        if len(self._rows) >= FLUSH_ROWS or time.monotonic() - self._flushed_at >= FLUSH_SECONDS:
            self._flush(final=False)

    def _flush(self, *, final: bool) -> None:
        """Write the buffered rows in one transaction; caller holds ``_lock``."""
        rows, self._rows = self._rows, []
        self._flushed_at = time.monotonic()
        if not rows and not final:
            return
        if not self._store.write(self._job_id, rows, self._size, self.truncated, final):
            self.unavailable = True

    def flush(self) -> None:
        """Write what is buffered now; called by readers so they see current output."""
        with self._lock:
            if self._open:
                self._flush(final=False)

    def close(self) -> None:
        with self._lock:
            if not self._open:
                return
            self._open = False
            self._flush(final=True)
        self._store.forget(self._job_id, self)


class JobLogStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._open: dict[str, JobLog] = {}
        self._legacy = LegacyJsonl(root)

    @staticmethod
    def _job(job_id: str) -> None:
        if type(job_id) is not str or _IDENT.fullmatch(job_id) is None:
            raise JobLogError("job_log_identifier_invalid")

    # -- database ---------------------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        """The one connection, opened on first use. Caller holds ``_lock``."""
        if self._db is not None:
            return self._db
        # The broker-owned job-logs directory and database must not be links.
        # Ancestors are the operator's chosen state location and stay unchecked.
        if _linked(self.root):
            raise JobLogError("job_log_linked_path_not_supported")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / DATABASE_NAME
        if _linked(path):
            raise JobLogError("job_log_linked_path_not_supported")
        try:
            connection = self._open_database(path)
        except sqlite3.DatabaseError:
            # The logs are not authoritative: set a damaged file aside and start over.
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            for suffix in ("", "-wal", "-shm"):
                source = path.with_name(path.name + suffix)
                if source.exists():
                    source.replace(path.with_name(f"{path.name}.corrupt-{stamp}{suffix}"))
            connection = self._open_database(path)
        self._db = connection
        return connection

    @staticmethod
    def _open_database(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                # Only a new file can still choose auto_vacuum; it must precede the tables.
                connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA secure_delete=ON")
            if version == 0:
                connection.executescript(
                    f"BEGIN; {_SCHEMA} PRAGMA user_version={SCHEMA_VERSION}; COMMIT;"
                )
            elif version != SCHEMA_VERSION:
                raise JobLogError("job_log_schema_unsupported")
            elif connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("job_log_database_damaged")
            return connection
        except BaseException:
            connection.close()
            raise

    def close(self) -> None:
        with self._lock:
            logs = list(self._open.values())
        for log in logs:  # outside the lock: a log takes its own lock first, then the store's
            log.close()
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def write(self, job_id: str, rows: list[Row], size: int, truncated: bool, final: bool) -> bool:
        """One transaction for a batch; False when the store could not take it."""
        try:
            with self._lock:
                db = self._connection()
                if final:
                    db.execute("PRAGMA synchronous=FULL")
                try:
                    db.execute("BEGIN IMMEDIATE")
                    db.executemany(
                        "INSERT INTO log_lines(job_id,seq,t,stream,level,text,record,data) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        [(job_id, *row) for row in rows],
                    )
                    db.execute(
                        "UPDATE log_jobs SET bytes=?, truncated=?, updated=?, closed=? "
                        "WHERE job_id=?",
                        (size, int(truncated), _millis(), int(final), job_id),
                    )
                    db.execute("COMMIT")
                except BaseException:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
                finally:
                    if final:
                        db.execute("PRAGMA synchronous=NORMAL")
            return True
        except Exception:
            return False

    def forget(self, job_id: str, log: JobLog) -> None:
        with self._lock:
            if self._open.get(job_id) is log:
                del self._open[job_id]

    def _flush_open(self) -> None:
        """Write every open log's buffer. Never called with the store lock held."""
        with self._lock:
            logs = list(self._open.values())
        for log in logs:
            log.flush()

    # -- writing ----------------------------------------------------------------------

    def begin(
        self,
        job_id: str,
        principal_id: str,
        kind: str,
        *,
        keep: Iterable[str] = (),
        tiers: Mapping[str, int] | None = None,
    ) -> JobLog:
        """Open (or reopen for append) a job's log. Prunes old logs first."""
        if not principal_id or type(principal_id) is not str:
            raise JobLogError("job_log_principal_required")
        self._job(job_id)
        previous = self._open.get(job_id)
        if previous is not None:
            previous.close()
        with self._lock:
            db = self._connection()
            self.prune({job_id, *keep}, tiers)
            row = db.execute(
                "SELECT principal_id, bytes, truncated FROM log_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            seq = size = 0
            truncated = False
            if row is not None:
                if row[0] != principal_id:
                    raise JobLogError("job_log_owner_mismatch")
                size, truncated = row[1], bool(row[2])
                seq = db.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM log_lines WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                db.execute("UPDATE log_jobs SET closed=0 WHERE job_id=?", (job_id,))
            else:
                db.execute(
                    "INSERT INTO log_jobs(job_id,principal_id,kind,created,updated) "
                    "VALUES(?,?,?,?,?)",
                    (job_id, principal_id, str(kind), _now(), _millis()),
                )
            log = JobLog(self, job_id, size, seq)
            log.truncated = truncated
            self._open[job_id] = log
            return log

    def prune(self, keep: set[str], tiers: Mapping[str, int] | None = None) -> None:
        """Drop job logs beyond the count/byte caps; never those in ``keep``.

        ``tiers`` ranks the retention value of each job id (missing = 0). Lower
        tiers are evicted first and oldest first within a tier, so ordinary logs
        go before needs_review / flash / LKG-producing ones.
        """
        try:
            with self._lock:
                db = self._connection()
                ranks = tiers or {}
                rows = db.execute("SELECT job_id, bytes, updated FROM log_jobs").fetchall()
                # Most valuable first (highest tier, then newest); once the caps are
                # exceeded, everything further down that is not kept goes.
                rows.sort(key=lambda row: (int(ranks.get(row[0], 0)), row[2]), reverse=True)
                # One slot is reserved for the job being started.
                count, total = 1, 0
                doomed: list[str] = []
                for job_id, size, _ in rows:
                    count += 1
                    total += size
                    if job_id in keep:
                        continue
                    if count > MAX_JOB_FILES or total > MAX_TOTAL_BYTES:
                        doomed.append(job_id)
                        count -= 1
                        total -= size
                for job_id in doomed:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("DELETE FROM log_lines WHERE job_id=?", (job_id,))
                    db.execute("DELETE FROM log_jobs WHERE job_id=?", (job_id,))
                    db.execute("COMMIT")
                if doomed:
                    db.execute("PRAGMA incremental_vacuum(512)")
        except Exception:
            return

    # -- reading ----------------------------------------------------------------------

    def _header(self, job_id: str) -> dict[str, Any] | None:
        row = (
            self._connection()
            .execute(
                "SELECT principal_id, kind, created, truncated FROM log_jobs WHERE job_id=?",
                (job_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "principal_id": row[0],
            "kind": row[1],
            "created": row[2],
            "truncated": bool(row[3]),
        }

    def read(self, job_id: str, principal_id: str, stream: str) -> dict[str, Any] | None:
        """Return the owner's entries of one stream, or None when nothing is readable."""
        try:
            self._job(job_id)
            self._flush_open()
            with self._lock:
                header = self._header(job_id)
                if header is None:
                    return self._legacy.read(job_id, principal_id, stream)
                if header["principal_id"] != principal_id:
                    return None
                rows = (
                    self._connection()
                    .execute(
                        "SELECT seq, t, level, text FROM log_lines "
                        "WHERE job_id=? AND stream=? ORDER BY seq",
                        (job_id, stream),
                    )
                    .fetchall()
                )
        except Exception:
            return None
        return {
            "header": header,
            "entries": [
                {"seq": seq, "t": _iso(t), "stream": stream, "level": level, "text": text}
                for seq, t, level, text in rows
            ],
            "truncated": header["truncated"],
            "partial_line_skipped": False,
        }

    def records(self, job_id: str, principal_id: str, kind: str) -> list[dict[str, Any]] | None:
        """The owner's structured records of one kind, oldest first; None if unreadable."""
        try:
            self._job(job_id)
            self._flush_open()
            with self._lock:
                header = self._header(job_id)
                if header is None:
                    return self._legacy.records(job_id, principal_id, kind)
                if header["principal_id"] != principal_id:
                    return None
                rows = (
                    self._connection()
                    .execute(
                        "SELECT seq, t, data FROM log_lines "
                        "WHERE job_id=? AND record=? ORDER BY seq",
                        (job_id, kind),
                    )
                    .fetchall()
                )
            records = [
                {"seq": seq, "t": _iso(t), "data": json.loads(data)} for seq, t, data in rows
            ]
        except Exception:
            return None
        return [record for record in records if type(record["data"]) is dict]

    def owner(self, job_id: str) -> str | None:
        """The principal recorded for a job's log, or None when unreadable."""
        try:
            self._job(job_id)
            with self._lock:
                header = self._header(job_id)
                if header is None:
                    return self._legacy.owner(job_id)
                principal = header["principal_id"]
                return principal if isinstance(principal, str) else None
        except Exception:
            return None

    @staticmethod
    def epoch(job_id: str) -> str:
        """Stable cursor epoch for one job's durable log; differs from RAM epochs."""
        return hashlib.sha256(b"durable_job_log:" + job_id.encode()).hexdigest()[:32]

    def build_read(
        self,
        job_id: str,
        principal_id: str,
        stream: str,
        request: Any,
        *,
        allow_empty: bool = False,
    ) -> dict[str, Any] | None:
        """Serve a build/source/flash stream with the RAM reader's cursor/filter semantics.

        ``allow_empty`` serves the owner's existing log with no entries of this
        stream as an empty page (a device job that received no bytes).
        """
        loaded = self.read(job_id, principal_id, stream)
        if loaded is None or not (loaded["entries"] or allow_empty):
            return None
        epoch = self.epoch(job_id)
        if not loaded["entries"]:
            cursor = request.cursor
            if cursor is not None and cursor.epoch == epoch and cursor.sequence > 0:
                raise ValueError("cursor_ahead")
            return dict(
                entries=[],
                expired=False,
                gap=cursor is not None and cursor.epoch != epoch,
                oldest_sequence=1,
                next_cursor=dict(epoch=epoch, sequence=0),
                has_more=False,
                source="durable_job_log",
                size_limit_reached=loaded["truncated"],
            )
        items: list[dict[str, Any]] = [
            dict(
                sequence=int(entry.get("seq", 0)),
                text=str(entry.get("text", "")),
                level=str(entry.get("level", "unknown")),
                stream=stream,
                progress=bool(_PROGRESS.match(str(entry.get("text", "")))),
            )
            for entry in loaded["entries"]
        ]
        last = items[-1]["sequence"]
        cursor = request.cursor
        reset = cursor is not None and cursor.epoch != epoch
        after = 0 if cursor is None or reset else cursor.sequence
        if after > last:
            raise ValueError("cursor_ahead")
        selected: set[int] = set()
        for index, item in enumerate(items):
            if item["level"] in request.levels or item["progress"]:
                selected.update(
                    range(
                        max(0, index - request.context),
                        min(len(items), index + request.context + 1),
                    )
                )
        result = [
            item
            for index, item in enumerate(items)
            if index in selected and item["sequence"] > after
        ][: request.limit]
        sequence = result[-1]["sequence"] if len(result) == request.limit else last
        return dict(
            entries=result,
            expired=False,
            gap=reset,
            oldest_sequence=items[0]["sequence"],
            next_cursor=dict(epoch=epoch, sequence=sequence),
            has_more=sequence < last,
            source="durable_job_log",
            size_limit_reached=loaded["truncated"],
        )

    def app_events_read(
        self, job_id: str, principal_id: str, request: Any
    ) -> dict[str, Any] | None:
        """Serve an app session's control events and app logs from the durable copy.

        Same response shape as ``AppEvents.read``; the cursor epoch is the durable
        one, so a RAM cursor resets (``gap``) instead of skipping entries.
        """
        loaded = self.read(job_id, principal_id, "app")
        events = self.records(job_id, principal_id, "app_event")
        if loaded is None or events is None or not (loaded["entries"] or events):
            return None
        epoch = self.epoch(job_id)
        cursor = request.cursor
        reset = cursor is not None and cursor.epoch != epoch
        control_after = 0 if cursor is None or reset else cursor.control
        log_after = 0 if cursor is None or reset else cursor.log
        control: list[dict[str, Any]] = [
            event["data"] for event in events if type(event["data"].get("sequence")) is int
        ]
        logs: list[dict[str, Any]] = [
            dict(
                sequence=int(entry.get("seq", 0)),
                text=str(entry.get("text", "")),
                level=str(entry.get("level", "unknown")),
                stream="observed_during_run",
                progress=False,
            )
            for entry in loaded["entries"]
        ]
        last_control = control[-1]["sequence"] if control else 0
        last_log = logs[-1]["sequence"] if logs else 0
        if control_after > last_control or log_after > last_log:
            raise ValueError("cursor_ahead")
        entries = [event for event in control if event["sequence"] > control_after]
        entries = entries[: request.limit]
        log_entries = [item for item in logs if item["sequence"] > log_after][: request.limit]
        next_control = entries[-1]["sequence"] if entries else max(control_after, last_control)
        next_log = log_entries[-1]["sequence"] if log_entries else max(log_after, last_log)
        return dict(
            entries=entries,
            logs=log_entries,
            log_attribution="observed_during_run",
            oldest_seq=dict(
                control=control[0]["sequence"] if control else 1,
                log=logs[0]["sequence"] if logs else 1,
            ),
            next_cursor=dict(epoch=epoch, control=next_control, log=next_log),
            has_more=next_control < last_control or next_log < last_log,
            gap=reset,
            dropped_count=dict(control=0, log=0),
            expired=False,
            expiry_seconds=0,
            source="durable_job_log",
            size_limit_reached=loaded["truncated"],
            partial_line_skipped=loaded["partial_line_skipped"],
        )

    def device_report(self, job_id: str, principal_id: str) -> dict[str, Any] | None:
        loaded = self.read(job_id, principal_id, "device")
        if loaded is None or not loaded["entries"]:
            return None
        texts = [str(entry.get("text", "")) for entry in loaded["entries"]]
        tail: list[str] = []
        size = 0
        for text in reversed(texts):
            size += len(text.encode("utf-8")) + 1
            if len(tail) >= MAX_READ_LINES or size > MAX_READ_BYTES:
                break
            tail.append(text)
        tail.reverse()
        return {
            "job_id": job_id,
            "source": "durable_job_log",
            "kind": loaded["header"].get("kind"),
            "created": loaded["header"].get("created"),
            "logs": tail,
            "total_lines": len(texts),
            "omitted_oldest_lines": len(texts) - len(tail),
            "size_limit_reached": loaded["truncated"],
            "partial_line_skipped": loaded["partial_line_skipped"],
            "redaction_limitations": REDACTION_LIMITATIONS,
        }


def archivable_jobs(root: Path, live: set[str]) -> list[str]:
    """Job ids in the log database that no live job owns (offline, broker stopped)."""
    path = root / DATABASE_NAME
    if _linked(root) or _linked(path) or not path.is_file():
        return []
    connection = sqlite3.connect(path)
    try:
        return sorted(
            row[0]
            for row in connection.execute("SELECT job_id FROM log_jobs")
            if row[0] not in live and _IDENT.fullmatch(row[0]) is not None
        )
    except sqlite3.DatabaseError:
        return []
    finally:
        connection.close()


def archive_jobs(root: Path, destination: Path, job_ids: list[str]) -> int:
    """Move these jobs' log rows into a database at ``destination``; returns how many moved."""
    if not job_ids:
        return 0
    target = JobLogStore._open_database(destination)
    source = sqlite3.connect(root / DATABASE_NAME, isolation_level=None)
    moved = 0
    try:
        source.execute("PRAGMA busy_timeout=5000")
        for job_id in job_ids:
            job = source.execute("SELECT * FROM log_jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                continue
            lines = source.execute("SELECT * FROM log_lines WHERE job_id=?", (job_id,)).fetchall()
            target.execute("BEGIN IMMEDIATE")
            target.execute("INSERT OR REPLACE INTO log_jobs VALUES(?,?,?,?,?,?,?,?)", job)
            target.executemany("INSERT OR REPLACE INTO log_lines VALUES(?,?,?,?,?,?,?,?)", lines)
            target.execute("COMMIT")
            source.execute("BEGIN IMMEDIATE")
            source.execute("DELETE FROM log_lines WHERE job_id=?", (job_id,))
            source.execute("DELETE FROM log_jobs WHERE job_id=?", (job_id,))
            source.execute("COMMIT")
            moved += 1
        if moved:
            source.execute("PRAGMA incremental_vacuum(512)")
    finally:
        source.close()
        target.close()
    return moved


class DeviceLogTap:
    """Passively turn received serial bytes into redacted device log lines.

    Mirrors ``DeviceLogCollector`` redaction (credential patterns, PEM blocks,
    capture frame records, binary lines) and then ``redact_log_text``. Never
    reads, writes or controls the port; ``feed`` never raises.
    """

    def __init__(self, log: JobLog) -> None:
        self.log = log
        self._pending = bytearray()
        self._overlong = False
        self._pem: bytes | None = None
        self._after_gap = False
        self.lines = self.omitted = self.redacted = 0

    def feed(self, data: bytes) -> None:
        try:
            parts = bytes(data).split(b"\n")
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                if not self._overlong:
                    self._pending += part
                    if len(self._pending) > MAX_DEVICE_LINE_BYTES:
                        self._pending.clear()
                        self._overlong = True
                if last:
                    break
                if self._overlong:
                    self._overlong = False
                    self.omitted += 1
                    self.log.append("device", "[omitted: overlong line]", "warning")
                else:
                    self._line(bytes(self._pending))
                self._pending.clear()
        except Exception:
            self.log.unavailable = True

    def mark_gap(self) -> None:
        """A port handoff: the next line may be a suffix of a cut line, so omit it."""
        try:
            self._track_pem(bytes(self._pending))
            if self._pending or self._overlong:
                self.omitted += 1
            self._pending.clear()
            self._overlong = False
            self._after_gap = True
            self.log.append("device", "[serial port reopened; capture gap]", "warning")
        except Exception:
            self.log.unavailable = True

    def finish(self) -> None:
        try:
            if self._pending and not self._after_gap:
                self._line(bytes(self._pending))
            elif self._pending or self._overlong:
                self.omitted += 1
            self._pending.clear()
            self._pem = None
        except Exception:
            self.log.unavailable = True

    def _track_pem(self, line: bytes) -> bool:
        seen = False
        for marker in _PEM_MARKER.finditer(_ANSI.sub(b"", line)):
            seen = True
            if marker.group(1) == b"BEGIN" and self._pem is None:
                self._pem = marker.group(2)
            elif marker.group(1) == b"END" and marker.group(2) == self._pem:
                self._pem = None
        return seen

    def _line(self, raw: bytes) -> None:
        line = _ANSI.sub(b"", raw)
        if line.endswith(b"\r"):
            line = line[:-1]
        if self._after_gap:
            self._after_gap = False
            self._track_pem(line)
            self.omitted += 1
            return
        sensitive = self._pem is not None
        sensitive = self._track_pem(line) or sensitive
        body = _LOG_PREFIX.sub(b"", line, count=1).lstrip(b" \t")
        if body.startswith((b"PIX", b"CAPTURE")) or _BINARY.search(line):
            self.omitted += 1
            return
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            self.omitted += 1
            self.log.append("device", "[omitted: non-UTF-8 line]", "warning")
            return
        if sensitive or _CREDENTIAL.search(line) is not None:
            self.redacted += 1
            rendered = _REDACTED
        else:
            rendered = text.encode("unicode_escape").decode("ascii")
        self.lines += 1
        self.log.append("device", rendered, _level(rendered))


def _level(text: str) -> str:
    prefix = re.match(r"([IWEVD]) \(", text)
    if prefix is not None:
        return {"I": "info", "W": "warning", "E": "error", "D": "debug", "V": "debug"}[
            prefix.group(1)
        ]
    for candidate in ("error", "warning", "debug", "info"):
        if re.search(r"\b" + candidate + r"\b", text, re.I):
            return candidate
    return "unknown"


def attach(logs: Any, opener: Any, job_id: str, principal_id: str, kind: str, stream: str) -> None:
    """Tee a RAM ``BuildLogs`` into the durable store; never raises."""
    if opener is None or getattr(logs, "durable", None) is not None:
        return
    try:
        durable = opener(job_id, principal_id, kind)
    except Exception:
        durable = None
    if durable is not None:
        durable.redactions = tuple(getattr(logs, "redactions", ()))
        logs.durable_stream = stream
        logs.durable = durable
