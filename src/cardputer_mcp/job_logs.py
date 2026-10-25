"""Durable, redacted, append-only per-job logs under ``<state_dir>/job-logs/``.

Saving is unconditional: every entry handed over is written as it arrives, on
every job outcome. Text is redacted before it touches disk. Writers never raise
into the job; a store failure only marks the log unavailable.

On-disk format (schema_version 1), one ``<job_id>.jsonl`` file per job:
first line ``{"schema_version":1,"job_id","principal_id","kind","created"}``,
then entries ``{"seq","t","stream","level","text"}``.

Structured records ``{"seq","t","record","data"}`` (``flash_outcome``,
``app_event``) are an additive part of schema_version 1: they carry no
``stream`` key, so stream readers (including older brokers) skip them.
"""

import hashlib
import json
import os
import re
import stat
import threading
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

SCHEMA_VERSION = 1
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
TRUNCATION_MARKER = "[job log size limit reached; further output dropped]"
_PROGRESS = re.compile(
    r"\[\d+/\d+\]|(?:-- |Executing action|Build complete|info: ROM read \d+/\d+ bytes)"
)
_IDENT = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_SUFFIX = ".jsonl"
_REPARSE = 0x400


class JobLogError(Exception):
    pass


def _linked(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _now() -> str:
    return datetime.now(UTC).isoformat()


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


class JobLog:
    """One job's open append-only file. ``append`` never raises."""

    def __init__(self, path: Path, handle: Any, size: int, seq: int) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._handle: Any = handle
        self._size = size
        self._seq = seq
        self.truncated = False
        self.unavailable = False
        self.redactions: tuple[str, ...] = ()

    def append(self, stream: str, text: str, level: str = "unknown") -> None:
        try:
            if stream not in STREAMS:
                raise JobLogError("job_log_stream_invalid")
            safe = redact_log_text(text, self.redactions)
            with self._lock:
                if self._handle is None or self.truncated:
                    return
                self._seq += 1
                line = self._encode(stream, level, safe)
                if self._size + len(line) > MAX_JOB_BYTES:
                    self.truncated = True
                    line = self._encode(stream, "warning", TRUNCATION_MARKER)
                self._handle.write(line)
                self._handle.flush()
                self._size += len(line)
        except Exception:
            self.unavailable = True

    def record(self, kind: str, data: dict[str, Any], *, reserve: bool = False) -> None:
        """Append one structured, redacted record; never raises."""
        try:
            if kind not in RECORDS or type(data) is not dict:
                raise JobLogError("job_log_record_invalid")
            safe = _redact_value(data, self.redactions)
            with self._lock:
                if self._handle is None or (self.truncated and not reserve):
                    return
                limit = MAX_JOB_BYTES + (RECORD_RESERVE_BYTES if reserve else 0)
                entry = {"seq": self._seq + 1, "t": _now(), "record": kind, "data": safe}
                line = (json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
                if len(line) > MAX_RECORD_BYTES or self._size + len(line) > limit:
                    return
                self._seq += 1
                self._handle.write(line)
                self._handle.flush()
                self._size += len(line)
        except Exception:
            self.unavailable = True

    def _encode(self, stream: str, level: str, text: str) -> bytes:
        entry = {"seq": self._seq, "t": _now(), "stream": stream, "level": level, "text": text}
        return (json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n").encode()

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            self.unavailable = True
        finally:
            try:
                handle.close()
            except Exception:
                self.unavailable = True


class JobLogStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()

    @staticmethod
    def _job(job_id: str) -> None:
        if type(job_id) is not str or _IDENT.fullmatch(job_id) is None:
            raise JobLogError("job_log_identifier_invalid")

    def _path(self, job_id: str) -> Path:
        self._job(job_id)
        # The broker-owned job-logs directory and each file must not be links.
        # Ancestors are the operator's chosen state location and stay unchecked.
        if _linked(self.root):
            raise JobLogError("job_log_linked_path_not_supported")
        path = self.root / (job_id + _SUFFIX)
        if _linked(path):
            raise JobLogError("job_log_linked_path_not_supported")
        return path

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
        with self._lock:
            self._job(job_id)
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self._path(job_id)
            self.prune({job_id, *keep}, tiers)
            flags = (
                os.O_CREAT
                | os.O_WRONLY
                | os.O_APPEND
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            existing = path.exists()
            if existing:
                header, entries, _ = self._load(path)
                if header.get("principal_id") != principal_id:
                    raise JobLogError("job_log_owner_mismatch")
                seq = max((entry.get("seq", 0) for entry in entries), default=0)
            descriptor = os.open(path, flags, 0o600)
            handle = os.fdopen(descriptor, "ab")
            try:
                if not existing:
                    header_line = json.dumps(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "job_id": job_id,
                            "principal_id": principal_id,
                            "kind": str(kind),
                            "created": _now(),
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                    handle.write((header_line + "\n").encode())
                    handle.flush()
                    seq = 0
                size = os.fstat(handle.fileno()).st_size
            except Exception:
                handle.close()
                raise
            return JobLog(path, handle, size, seq)

    def prune(self, keep: set[str], tiers: Mapping[str, int] | None = None) -> None:
        """Drop job logs beyond the count/byte caps; never those in ``keep``.

        ``tiers`` ranks the retention value of each job id (missing = 0). Lower
        tiers are evicted first and oldest first within a tier, so ordinary logs
        go before needs_review / flash / LKG-producing ones.
        """
        try:
            names = os.listdir(self.root)
        except FileNotFoundError:
            return
        files: list[tuple[int, int, Path]] = []
        for name in names:
            if not name.endswith(_SUFFIX) or _IDENT.fullmatch(name[: -len(_SUFFIX)]) is None:
                continue
            path = self.root / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
                continue
            files.append((info.st_mtime_ns, info.st_size, path))
        ranks = tiers or {}

        def job(path: Path) -> str:
            return path.name[: -len(_SUFFIX)]

        # Walk from the most valuable (highest tier, then newest) down; once the
        # caps are exceeded, everything further down that is not kept goes.
        files.sort(key=lambda item: (int(ranks.get(job(item[2]), 0)), item[0]), reverse=True)
        # One slot is reserved for the job being started.
        count, total = 1, 0
        for _, size, path in files:
            count += 1
            total += size
            if job(path) in keep:
                continue
            if count > MAX_JOB_FILES or total > MAX_TOTAL_BYTES:
                try:
                    path.unlink()
                except OSError:
                    continue
                count -= 1
                total -= size

    def _load(self, path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(MAX_JOB_BYTES + 65536)
        lines = data.split(b"\n")
        header = json.loads(lines[0])
        if type(header) is not dict or header.get("schema_version") != SCHEMA_VERSION:
            raise JobLogError("job_log_invalid")
        entries: list[dict[str, Any]] = []
        partial = False
        for raw in lines[1:]:
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                # A crash can leave one unfinished final line; skip it.
                partial = True
                continue
            if type(entry) is dict:
                entries.append(entry)
        return header, entries, partial

    def read(self, job_id: str, principal_id: str, stream: str) -> dict[str, Any] | None:
        """Return the owner's entries of one stream, or None when nothing is readable."""
        try:
            path = self._path(job_id)
            if not path.exists():
                return None
            header, entries, partial = self._load(path)
        except Exception:
            return None
        if header.get("job_id") != job_id or header.get("principal_id") != principal_id:
            return None
        selected = [entry for entry in entries if entry.get("stream") == stream]
        truncated = any(entry.get("text") == TRUNCATION_MARKER for entry in entries)
        return {
            "header": header,
            "entries": selected,
            "truncated": truncated,
            "partial_line_skipped": partial,
        }

    def records(self, job_id: str, principal_id: str, kind: str) -> list[dict[str, Any]] | None:
        """The owner's structured records of one kind, oldest first; None if unreadable."""
        try:
            path = self._path(job_id)
            if not path.exists():
                return None
            header, entries, _ = self._load(path)
        except Exception:
            return None
        if header.get("job_id") != job_id or header.get("principal_id") != principal_id:
            return None
        return [
            {"seq": entry.get("seq"), "t": entry.get("t"), "data": entry["data"]}
            for entry in entries
            if entry.get("record") == kind and type(entry.get("data")) is dict
        ]

    def owner(self, job_id: str) -> str | None:
        """The principal recorded in a job log's header, or None when unreadable."""
        try:
            path = self._path(job_id)
            if not path.exists():
                return None
            header, _, _ = self._load(path)
        except Exception:
            return None
        principal = header.get("principal_id")
        return principal if header.get("job_id") == job_id and isinstance(principal, str) else None

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
