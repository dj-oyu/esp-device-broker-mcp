"""Pure, expiring retained-image reads. Only validated PNG bytes reach presentation."""

import math
from dataclasses import dataclass, field, replace
from typing import Any

from .gui_images import SOURCES, ImagePreview, ImageRow

UNAVAILABLE = "No retained image."
EXPIRED = "Retained image expired."
FAILED = "Image unavailable."
DENIED = "Image access denied."


@dataclass(frozen=True)
class PreviewState:
    generation: int = 0
    epoch: str = ""
    row: ImageRow | None = None
    request_id: int = 0
    preview: ImagePreview | None = field(default=None, repr=False)
    message: str = UNAVAILABLE
    attempted: bool = False

    @property
    def pending(self) -> bool:
        return self.request_id != 0

    def reconcile(
        self, generation: int, epoch: str, row: ImageRow | None, live: bool, now: float
    ) -> "PreviewState":
        """Invalidate changed authority, preserving the earliest identity deadline."""
        if not live or row is None:
            return PreviewState(generation, epoch)
        if (
            self.generation != generation
            or self.epoch != epoch
            or self.row is None
            or not self.row.same_image(row)
        ):
            current = PreviewState(
                generation,
                epoch,
                row,
                message=(
                    "Saved image available for automatic read."
                    if row.saved
                    else "Retained image available for automatic read."
                ),
            )
        else:
            bounded = replace(row, expires_at=min(row.expires_at, self.row.expires_at))
            preview = self.preview
            if preview is not None:
                bounded = replace(
                    bounded, expires_at=min(bounded.expires_at, preview.row.expires_at)
                )
                preview = replace(preview, row=bounded)
            current = replace(self, row=bounded, preview=preview)
        assert current.row is not None
        if not math.isfinite(now) or now >= current.row.expires_at:
            return replace(current, request_id=0, preview=None, message=EXPIRED)
        return current

    def can_read(self, now: float) -> bool:
        return bool(
            self.row and math.isfinite(now) and now < self.row.expires_at and not self.pending
        )

    def started(self, request_id: int) -> "PreviewState":
        return replace(
            self,
            request_id=request_id,
            preview=None,
            message="Reading retained image...",
            attempted=True,
        )

    def accept(
        self,
        generation: int,
        job_id: str,
        request_id: int,
        response: Any,
        started_at: float,
        now: float,
    ) -> "PreviewState":
        if (
            not self.pending
            or generation != self.generation
            or request_id != self.request_id
            or self.row is None
            or job_id != self.row.job_id
        ):
            return self
        if not math.isfinite(now) or now >= self.row.expires_at:
            return replace(self, request_id=0, preview=None, message=EXPIRED)
        if type(response) is dict and response.get("ok") is False:
            code = response.get("error")
            message = (
                DENIED
                if code in ("not_authorized", "authentication_required", "local_operator_required")
                else EXPIRED
                if code == "expired"
                else FAILED
            )
            return replace(self, request_id=0, preview=None, message=message)
        try:
            if (
                not math.isfinite(started_at)
                or started_at > now
                or type(response) is not dict
                or response.get("broker_epoch") != self.epoch
            ):
                raise ValueError
            preview = ImagePreview.parse(response, self.row, started_at)
            if now >= preview.row.expires_at:
                return replace(self, row=preview.row, request_id=0, preview=None, message=EXPIRED)
            return replace(
                self,
                row=preview.row,
                request_id=0,
                preview=preview,
                message=SOURCES[preview.row.source]
                + (" (saved copy)" if preview.row.saved else ""),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return replace(self, request_id=0, preview=None, message=FAILED)
