"""Read-only access to per-job ``.jsonl`` logs written by earlier brokers.

Nothing writes these files any more: new logs go to the SQLite store in ``job_logs``.
This module only lets a job that finished before the switch stay readable, and is
removed together with ``job_logs.LegacyJsonl`` use once those logs are no longer needed.

Format (schema_version 1), one ``<job_id>.jsonl`` file per job: a header line
``{"schema_version":1,"job_id","principal_id","kind","created"}``, then entries
``{"seq","t","stream","level","text"}`` and structured records ``{"seq","t","record","data"}``.
"""

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_JOB_BYTES = 8 * 1024 * 1024
TRUNCATION_MARKER = "[job log size limit reached; further output dropped]"
_IDENT = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_SUFFIX = ".jsonl"
_REPARSE = 0x400


def _linked(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


class LegacyJsonl:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, job_id: str) -> Path | None:
        if type(job_id) is not str or _IDENT.fullmatch(job_id) is None:
            return None
        if _linked(self.root):
            return None
        path = self.root / (job_id + _SUFFIX)
        if _linked(path) or not path.is_file():
            return None
        return path

    def _load(self, path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], bool] | None:
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                data = handle.read(MAX_JOB_BYTES + 65536)
            lines = data.split(b"\n")
            header = json.loads(lines[0])
            if type(header) is not dict or header.get("schema_version") != SCHEMA_VERSION:
                return None
        except (OSError, ValueError):
            return None
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

    def _owned(
        self, job_id: str, principal_id: str | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool] | None:
        path = self._path(job_id)
        loaded = self._load(path) if path is not None else None
        if loaded is None or loaded[0].get("job_id") != job_id:
            return None
        if principal_id is not None and loaded[0].get("principal_id") != principal_id:
            return None
        return loaded

    def read(self, job_id: str, principal_id: str, stream: str) -> dict[str, Any] | None:
        loaded = self._owned(job_id, principal_id)
        if loaded is None:
            return None
        header, entries, partial = loaded
        return {
            "header": header,
            "entries": [entry for entry in entries if entry.get("stream") == stream],
            "truncated": any(entry.get("text") == TRUNCATION_MARKER for entry in entries),
            "partial_line_skipped": partial,
        }

    def records(self, job_id: str, principal_id: str, kind: str) -> list[dict[str, Any]] | None:
        loaded = self._owned(job_id, principal_id)
        if loaded is None:
            return None
        return [
            {"seq": entry.get("seq"), "t": entry.get("t"), "data": entry["data"]}
            for entry in loaded[1]
            if entry.get("record") == kind and type(entry.get("data")) is dict
        ]

    def owner(self, job_id: str) -> str | None:
        loaded = self._owned(job_id, None)
        principal = loaded[0].get("principal_id") if loaded is not None else None
        return principal if isinstance(principal, str) else None
