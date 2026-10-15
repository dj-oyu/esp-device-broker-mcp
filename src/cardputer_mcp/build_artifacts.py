"""Internal successful-build artifact resolver; physical layout is builder-owned."""

import copy
import threading
from pathlib import Path
from typing import Any


class BuildArtifacts:
    def __init__(self) -> None:
        self._records: dict[str, tuple[str, Path, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def publish(self, job_id: str, principal: str, root: Path, manifest: dict[str, Any]) -> None:
        if not principal or not root.is_dir() or manifest.get("flash_authorized") is not False:
            raise ValueError("invalid_internal_artifact_receipt")
        with self._lock:
            while len(self._records) >= 8:
                self._records.pop(next(iter(self._records)))
            self._records[job_id] = principal, root.resolve(), copy.deepcopy(manifest)

    def discard(self, job_id: str) -> None:
        with self._lock:
            self._records.pop(job_id, None)

    def resolve(self, job_id: str, principal: str) -> tuple[Path, dict[str, Any]]:
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record[0] != principal:
                raise ValueError("owned_build_artifact_not_available")
            return record[1], copy.deepcopy(record[2])
