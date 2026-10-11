"""Local-only image review, with explicit delivery approval and bounded PNG decoding.

The worker only reads retained captures or sends one operator decision. It never
captures, retries, writes image files, or calls Tk. A preview is a past capture,
not a live view; neither capture completeness nor approval verifies the LCD.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import queue
import re
import struct
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

if TYPE_CHECKING:
    import tkinter as tk

MAX_PNG_BYTES = 200_000
MAX_IMAGES = 8
MAX_TTL = 300
WIDTH, HEIGHT = 240, 135
INVALID_IMAGE = "The broker returned an invalid image preview. Refresh the image list."
REQUEST_FAILED = "Image request failed. Refresh to check the retained image; no retry was sent."
DECISION_FAILED = (
    "Image decision was not confirmed. It may have completed. Refresh before deciding again."
)
EXPIRED = "This retained image has expired. This request was not sent; refresh images."
EMPTY_IMAGES = (
    "No retained images available. Captures expire 5 minutes after completion; "
    "this does not mean capture never ran. Refresh does not recapture."
)
SELECTION_CHANGED = "Image selection changed. Preview the selected image before deciding."
CLOSING = "Waiting for the pending image request. Closing does not cancel a submitted decision."
SOURCES = {
    "synthetic_fixture": "TEST / NOT DEVICE",
    "render_buffer": "Device render buffer / LCD unverified",
}
Source = Literal["render_buffer", "synthetic_fixture"]
DeliveryState = Literal["pending", "approved", "rejected"]
Decision = Literal["approve", "reject"]
Action = Literal["list", "preview", "approve", "reject"]


class ImageAPI(Protocol):
    def images(self) -> dict[str, Any]: ...
    def image(self, job_id: str) -> dict[str, Any]: ...
    def decide_image(
        self, job_id: str, sha256: str, delivery_revision: str, decision: Decision
    ) -> dict[str, Any]: ...


def _text(value: Any, pattern: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(INVALID_IMAGE)
    return value


def _recipient(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value.isascii()
        or not value.isprintable()
        or value != value.strip()
    ):
        raise ValueError(INVALID_IMAGE)
    return value


def _seconds(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= MAX_TTL
    ):
        raise ValueError(INVALID_IMAGE)
    return float(value)


def _born(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(INVALID_IMAGE)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise ValueError(INVALID_IMAGE)
    return parsed.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class ImageRow:
    job_id: str
    sha256: str
    source: Source
    recipient_principal: str
    born_utc: str
    expires_at: float
    delivery_state: DeliveryState
    policy_allowed: bool = False

    @classmethod
    def parse(cls, data: dict[str, Any], started_at: float) -> ImageRow:
        source, state = data["source"], data["delivery_state"]
        policy_allowed = data.get("policy_allowed", False)
        if type(policy_allowed) is not bool:
            raise ValueError(INVALID_IMAGE)
        if source not in SOURCES or state not in ("pending", "approved", "rejected"):
            raise ValueError(INVALID_IMAGE)
        return cls(
            _text(data["job_id"], r"[a-zA-Z0-9_-]{1,64}"),
            _text(data["sha256"], r"[0-9a-f]{64}"),
            cast(Source, source),
            _recipient(data["recipient_principal"]),
            _born(data["born_utc"]),
            started_at + _seconds(data["remaining_seconds"]),
            cast(DeliveryState, state),
            policy_allowed,
        )

    def same_image(self, other: ImageRow) -> bool:
        return (
            self.job_id,
            self.sha256,
            self.source,
            self.recipient_principal,
            self.born_utc,
            self.delivery_state,
            self.policy_allowed,
        ) == (
            other.job_id,
            other.sha256,
            other.source,
            other.recipient_principal,
            other.born_utc,
            other.delivery_state,
            other.policy_allowed,
        )


@dataclass(frozen=True)
class ImagesSnapshot:
    broker_epoch: str
    rows: tuple[ImageRow, ...]

    @classmethod
    def parse(cls, data: dict[str, Any], started_at: float) -> ImagesSnapshot:
        try:
            if data["ok"] is not True or not isinstance(data["images"], list):
                raise ValueError(INVALID_IMAGE)
            if len(data["images"]) > MAX_IMAGES:
                raise ValueError(INVALID_IMAGE)
            rows = tuple(ImageRow.parse(row, started_at) for row in data["images"])
            if len({row.job_id for row in rows}) != len(rows):
                raise ValueError(INVALID_IMAGE)
            return cls(_text(data["broker_epoch"], r"[0-9a-f]{32}"), rows)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(INVALID_IMAGE) from None


def validate_png(encoded: Any, size: Any, sha256: str) -> bytes:
    """Check bounded RGB8 PNG structure, checksums and raster size before Tk sees it."""
    try:
        if type(size) is not int or not 1 <= size <= MAX_PNG_BYTES:
            raise ValueError(INVALID_IMAGE)
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_PNG_BYTES + 2) // 3):
            raise ValueError(INVALID_IMAGE)
        png = base64.b64decode(encoded, validate=True)
        if (
            len(png) != size
            or hashlib.sha256(png).hexdigest() != sha256
            or not png.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            raise ValueError(INVALID_IMAGE)
        offset, count, finished = 8, 0, False
        compressed = bytearray()
        idat_seen, idat_ended = False, False
        while offset < len(png):
            count += 1
            if count > 32 or len(png) - offset < 12:
                raise ValueError(INVALID_IMAGE)
            length = struct.unpack_from(">I", png, offset)[0]
            end = offset + 12 + length
            if end > len(png):
                raise ValueError(INVALID_IMAGE)
            kind = png[offset + 4 : offset + 8]
            payload = png[offset + 8 : end - 4]
            checksum = struct.unpack_from(">I", png, end - 4)[0]
            if checksum != zlib.crc32(kind + payload):
                raise ValueError(INVALID_IMAGE)
            if count == 1:
                if kind != b"IHDR" or payload != struct.pack(
                    ">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0
                ):
                    raise ValueError(INVALID_IMAGE)
            elif kind == b"IDAT":
                if idat_ended:
                    raise ValueError(INVALID_IMAGE)
                idat_seen = True
                compressed.extend(payload)
            elif kind == b"IEND":
                if payload or not idat_seen or end != len(png):
                    raise ValueError(INVALID_IMAGE)
                finished = True
            elif kind == b"tEXt":
                if b"\0" not in payload:
                    raise ValueError(INVALID_IMAGE)
                idat_ended = idat_seen
            else:
                raise ValueError(INVALID_IMAGE)
            offset = end
        if not finished:
            raise ValueError(INVALID_IMAGE)
        expected_size = HEIGHT * (1 + WIDTH * 3)
        decoder = zlib.decompressobj()
        raster = decoder.decompress(bytes(compressed), expected_size + 1)
        if (
            len(raster) != expected_size
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
            or any(raster[y * (1 + WIDTH * 3)] > 4 for y in range(HEIGHT))
        ):
            raise ValueError(INVALID_IMAGE)
        return png
    except (ValueError, TypeError, binascii.Error, struct.error, zlib.error, OverflowError):
        raise ValueError(INVALID_IMAGE) from None


@dataclass(frozen=True)
class ImagePreview:
    row: ImageRow
    delivery_revision: str
    png: bytes = field(repr=False)

    @classmethod
    def parse(cls, data: dict[str, Any], expected: ImageRow, started_at: float) -> ImagePreview:
        try:
            if data["ok"] is not True:
                raise ValueError(INVALID_IMAGE)
            metadata = data["metadata"]
            source = metadata["source"]
            row = ImageRow.parse({**data, "source": source}, started_at)
            synthetic = source == "synthetic_fixture"
            if (
                not row.same_image(expected)
                or data.get("source", source) != source
                or metadata["label"] != SOURCES[source]
                or metadata["is_device_capture"] is not (not synthetic)
                or metadata["mime_type"] != "image/png"
                or type(metadata["width"]) is not int
                or metadata["width"] != WIDTH
                or type(metadata["height"]) is not int
                or metadata["height"] != HEIGHT
                or metadata["sha256"] != row.sha256
                or metadata["capture_complete"] is not True
                or metadata["coherent_frame"] != "unknown"
                or metadata["lcd_transfer_ack"] != "unknown"
                or metadata["protocol"]
                != ("synthetic-image-v1" if synthetic else "pocketjs-legacy-capture-v1")
            ):
                raise ValueError(INVALID_IMAGE)
            png = validate_png(data["image_base64"], metadata["size_bytes"], row.sha256)
            # Never extend a list snapshot's deadline using a later response.
            row = replace(row, expires_at=min(expected.expires_at, row.expires_at))
            revision = _text(data["delivery_revision"], r"[0-9a-f]{64}")
            return cls(row, revision, png)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(INVALID_IMAGE) from None


@dataclass(frozen=True)
class ImageOperation:
    sequence: int
    generation: int
    action: Action
    started_at: float
    row: ImageRow | None = None
    delivery_revision: str = ""


@dataclass(frozen=True)
class ImageOutcome:
    operation: ImageOperation
    snapshot: ImagesSnapshot | None = None
    preview: ImagePreview | None = field(default=None, repr=False)
    delivery_state: DeliveryState | None = None
    error: str = ""


def perform_image(
    api: ImageAPI, operation: ImageOperation, clock: Callable[[], float] = time.monotonic
) -> ImageOutcome:
    """One request, no retries and no server-controlled diagnostic text."""
    try:
        if operation.action == "list":
            return ImageOutcome(
                operation, snapshot=ImagesSnapshot.parse(api.images(), operation.started_at)
            )
        row = operation.row
        if row is None:
            raise ValueError(INVALID_IMAGE)
        if clock() >= row.expires_at:
            return ImageOutcome(operation, error=EXPIRED)
        if operation.action == "preview":
            return ImageOutcome(
                operation,
                preview=ImagePreview.parse(api.image(row.job_id), row, operation.started_at),
            )
        decision = operation.action
        data = api.decide_image(row.job_id, row.sha256, operation.delivery_revision, decision)
        expected_state = "approved" if decision == "approve" else "rejected"
        if (
            data.get("ok") is not True
            or data.get("job_id") != row.job_id
            or data.get("delivery_state") != expected_state
        ):
            raise ValueError(INVALID_IMAGE)
        return ImageOutcome(operation, delivery_state=cast(DeliveryState, expected_state))
    except Exception:
        # Even safe client errors are not copied: a replacement transport may
        # attach response bodies, tokens, image data or native exception text.
        message = DECISION_FAILED if operation.action in ("approve", "reject") else REQUEST_FAILED
        return ImageOutcome(operation, error=message)


class ImageController:
    """Headless exact-preview, deadline, repeated-click and shutdown policy."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.snapshot: ImagesSnapshot | None = None
        self.preview: ImagePreview | None = None
        self.selected_id = ""
        self.sequence = 0
        self.generation = 0
        self.pending: ImageOperation | None = None
        self.displayed: ImagePreview | None = None
        self.closing = False
        self.closed = False
        self.failed = False
        self.message = "Select a retained capture and preview it locally before deciding."

    @property
    def busy(self) -> bool:
        return self.pending is not None

    def _clear_preview(self) -> None:
        self.generation += 1
        self.preview = self.displayed = None

    def _begin(self, action: Action, row: ImageRow | None = None) -> ImageOperation | None:
        if self.busy or self.closed or self.closing:
            return None
        self.sequence += 1
        revision = self.preview.delivery_revision if self.preview else ""
        operation = ImageOperation(
            self.sequence, self.generation, action, self.clock(), row, revision
        )
        self.pending = operation
        self.failed = False
        self.message = (
            "Loading retained images..." if action == "list" else "Image request pending..."
        )
        return operation

    def refresh(self) -> ImageOperation | None:
        if self.busy or self.closed or self.closing:
            return None
        self._clear_preview()
        self.snapshot = None
        self.selected_id = ""
        return self._begin("list")

    def select(self, job_id: str) -> None:
        if self.closed or self.closing or job_id == self.selected_id:
            return
        self._clear_preview()
        self.selected_id = job_id
        self.message = SELECTION_CHANGED

    def selected_row(self) -> ImageRow | None:
        return (
            next((row for row in self.snapshot.rows if row.job_id == self.selected_id), None)
            if self.snapshot
            else None
        )

    def load_preview(self) -> ImageOperation | None:
        row = self.selected_row()
        if self.busy or self.closed or self.closing or row is None:
            return None
        self._clear_preview()
        if self.clock() >= row.expires_at:
            self.message, self.failed = EXPIRED, True
            return None
        return self._begin("preview", row)

    def mark_displayed(self, preview: ImagePreview) -> bool:
        if (
            self.closed
            or self.closing
            or self.busy
            or self.preview is not preview
            or preview.row.job_id != self.selected_id
            or self.clock() >= preview.row.expires_at
        ):
            return False
        self.displayed = preview
        return True

    def can_decide(self) -> bool:
        preview, row = self.preview, self.selected_row()
        return bool(
            not (self.busy or self.closed or self.closing)
            and preview is not None
            and self.displayed is preview
            and row is not None
            and row.same_image(preview.row)
            and preview.row.delivery_state == "pending"
            and self.clock() < min(row.expires_at, preview.row.expires_at)
        )

    def decide(self, decision: Decision) -> ImageOperation | None:
        if decision not in ("approve", "reject") or not self.can_decide() or self.preview is None:
            return None
        operation = self._begin(decision, self.preview.row)
        self.displayed = None  # A second click can never reuse this displayed approval.
        return operation

    def accept(self, outcome: ImageOutcome) -> bool:
        if self.closed or self.pending is None or outcome.operation != self.pending:
            return False
        self.pending = None
        if self.closing:
            self.closed = True
            self._clear_preview()
            self.snapshot = None
            self.message = outcome.error or "Pending image request finished; closing."
            return True
        if outcome.operation.generation != self.generation:
            self.message = SELECTION_CHANGED
            return True
        self.failed = bool(outcome.error)
        if outcome.error:
            self._clear_preview()
            self.snapshot = None
            self.message = outcome.error
        elif outcome.snapshot is not None:
            self.snapshot = outcome.snapshot
            self.message = (
                "Select an image and click Preview selected image."
                if outcome.snapshot.rows
                else EMPTY_IMAGES
            )
        elif outcome.preview is not None:
            row = self.selected_row()
            if (
                row is None
                or not row.same_image(outcome.preview.row)
                or self.clock() >= outcome.preview.row.expires_at
            ):
                self._clear_preview()
                self.message, self.failed = SELECTION_CHANGED, True
            else:
                self.preview = outcome.preview
                self.message = (
                    "Preview loaded locally. Choose whether to allow delivery."
                    if outcome.preview.row.delivery_state == "pending"
                    else f"This retained image is already {outcome.preview.row.delivery_state}."
                )
        elif outcome.delivery_state is not None and self.preview is not None:
            row = replace(
                self.preview.row,
                delivery_state=outcome.delivery_state,
                policy_allowed=(
                    self.preview.row.policy_allowed
                    if outcome.delivery_state != "rejected"
                    else False
                ),
            )
            self.preview = replace(self.preview, row=row)
            if self.snapshot is not None:
                self.snapshot = replace(
                    self.snapshot,
                    rows=tuple(
                        row if item.job_id == row.job_id else item for item in self.snapshot.rows
                    ),
                )
            self.message = (
                "Approved: the named AI principal may read this exact retained image."
                if outcome.delivery_state == "approved"
                else "Rejected: this retained image cannot be sent. No new capture was requested."
            )
        return True

    def expire_preview(self) -> None:
        row = self.preview.row if self.preview is not None else self.selected_row()
        if row is not None and not self.busy and self.clock() >= row.expires_at:
            if self.preview is not None:
                self._clear_preview()
            self.message = "This retained image has expired. Refresh images."

    def hide(self) -> None:
        self._clear_preview()
        self.selected_id = ""

    def request_close(self) -> bool:
        if self.closed:
            return True
        self.closing = True
        self._clear_preview()
        self.message = CLOSING if self.busy else "Image review closed."
        self.closed = not self.busy
        if self.closed:
            self.snapshot = None
        return self.closed


class ImageWorker:
    def __init__(self, api: ImageAPI) -> None:
        self.api = api
        self.commands: queue.Queue[ImageOperation | None] = queue.Queue()
        self.results: queue.Queue[ImageOutcome] = queue.Queue()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name="cardputer-images", daemon=True)
        self.thread.start()

    def run(self) -> None:
        while not self.stopped.is_set():
            operation = self.commands.get()
            if operation is None or self.stopped.is_set():
                return
            self.results.put(perform_image(self.api, operation))
            del operation

    def stop(self) -> None:
        self.stopped.set()
        self.commands.put(None)


class ImagePanel:
    """Integrated Tk dialog. Constructing it does not make an HTTP request."""

    def __init__(self, root: tk.Tk, api: ImageAPI) -> None:
        self.root = root
        self.controller = ImageController()
        self.worker = ImageWorker(api)
        self.dialog: tk.Toplevel | None = None
        self.on_closed: Callable[[], None] | None = None
        self.photo: tk.PhotoImage | None = None
        self.rendered: ImagePreview | None = None
        self.rows: tuple[ImageRow, ...] = ()
        root.after(75, self._drain)
        root.after(1000, self._tick)

    @property
    def is_busy(self) -> bool:
        return self.controller.busy

    def show(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        if self.controller.closed or self.controller.closing:
            return
        if self.dialog is not None:
            self.dialog.deiconify()
            self.dialog.lift()
            if not self.controller.busy:
                self._submit(self.controller.refresh())
            return
        self.dialog = dialog = tk.Toplevel(self.root)
        dialog.title("Cardputer retained images")
        dialog.transient(self.root)
        dialog.geometry("940x720")
        dialog.minsize(840, 680)
        dialog.protocol("WM_DELETE_WINDOW", self.hide)
        box = ttk.Frame(dialog, padding=16)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="Local image review", font=("TkDefaultFont", 16, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            box,
            text="Review a past capture and its delivery permissions locally. "
            "This is not a live view.",
            wraplength=890,
        ).pack(anchor="w", pady=6)
        table = ttk.Frame(box)
        table.pack(fill="x")
        self.tree = ttk.Treeview(
            table,
            columns=("job", "source", "recipient", "state"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        for key, title, width in (
            ("job", "Capture job", 240),
            ("source", "Source", 230),
            ("recipient", "AI recipient principal", 230),
            ("state", "Delivery", 100),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=70)
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="x", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._select)
        bar = ttk.Frame(box)
        bar.pack(fill="x", pady=8)
        self.refresh_button = ttk.Button(bar, text="Refresh images", command=self._refresh)
        self.refresh_button.pack(side="left")
        self.preview_button = ttk.Button(bar, text="Preview selected image", command=self._preview)
        self.preview_button.pack(side="left", padx=8)
        self.source_var = tk.StringVar()
        ttk.Label(box, textvariable=self.source_var, font=("TkDefaultFont", 14, "bold")).pack(
            anchor="w"
        )
        self.image_label = ttk.Label(box, text="Select a retained capture to preview")
        self.image_label.pack(pady=8)
        self.details_var = tk.StringVar()
        ttk.Label(box, textvariable=self.details_var, justify="left", wraplength=890).pack(
            anchor="w"
        )
        actions = ttk.Frame(box)
        actions.pack(fill="x", pady=12)
        self.approve_button = ttk.Button(
            actions,
            text="Approve sending this image to AI",
            command=lambda: self._decide("approve"),
        )
        self.approve_button.pack(side="left")
        self.reject_button = ttk.Button(
            actions, text="Reject image", command=lambda: self._decide("reject")
        )
        self.reject_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Hide panel", command=self.hide).pack(side="right")
        self.status_var = tk.StringVar()
        self.status_label = ttk.Label(box, textvariable=self.status_var, wraplength=890)
        self.status_label.pack(anchor="w")
        ttk.Label(
            box,
            text="Approval permits the named principal to read only this image. Rejection is final "
            "for this retained image. No automatic retake or retry. Hiding or closing does not "
            "cancel a submitted decision.",
            wraplength=890,
        ).pack(anchor="w", pady=(8, 0))
        self._submit(self.controller.refresh())

    def _submit(self, operation: ImageOperation | None) -> None:
        if operation is not None:
            self.worker.commands.put(operation)
        self._render()

    def _refresh(self) -> None:
        self._submit(self.controller.refresh())

    def _select(self, _event: Any = None) -> None:
        selected = self.tree.selection()
        self.controller.select(selected[0] if selected else "")
        self._render()

    def _preview(self) -> None:
        self._submit(self.controller.load_preview())

    def _decide(self, decision: Decision) -> None:
        self._submit(self.controller.decide(decision))

    def _render(self) -> None:
        if self.dialog is None:
            return
        import tkinter as tk

        model = self.controller
        model.expire_preview()
        rows = model.snapshot.rows if model.snapshot else ()
        if rows != self.rows:
            self.rows = rows
            children = self.tree.get_children()
            if children:
                self.tree.delete(*children)
            for row in rows:
                self.tree.insert(
                    "",
                    "end",
                    iid=row.job_id,
                    values=(
                        row.job_id,
                        SOURCES[row.source],
                        row.recipient_principal,
                        row.delivery_state,
                    ),
                )
            if model.selected_id and self.tree.exists(model.selected_id):
                self.tree.selection_set(model.selected_id)
        preview = model.preview
        if preview is not self.rendered:
            self.photo = None
            self.rendered = None
            self.image_label.configure(image="", text="Select a retained capture to preview")
            if preview is not None:
                try:
                    # Parser has bounded and validated these exact bytes before Tk.
                    photo = tk.PhotoImage(master=self.dialog, data=base64.b64encode(preview.png))
                    self.image_label.configure(image=photo, text="")
                    self.photo, self.rendered = photo, preview
                    model.mark_displayed(preview)
                except tk.TclError:
                    model.preview = model.displayed = None
                    model.message, model.failed = (
                        "This PNG could not be displayed. Refresh images.",
                        True,
                    )
                    preview = None
        self.source_var.set(SOURCES[preview.row.source] if preview else "")
        if preview is not None:
            remaining = max(0, math.ceil(preview.row.expires_at - model.clock()))
            self.details_var.set(
                f"Capture job: {preview.row.job_id}\n"
                f"Completed at (host clock, UTC): {preview.row.born_utc}\n"
                f"Expires in: {remaining} s | Manual delivery decision: "
                f"{preview.row.delivery_state}\n"
                + (
                    "Key policy at last check: automatic sending permitted\n"
                    if preview.row.policy_allowed
                    else "Key policy at last check: no automatic sending permission\n"
                )
                + f"AI recipient principal: {preview.row.recipient_principal}\n"
                f"SHA-256: {preview.row.sha256}\n"
                "Complete capture: yes | Frame coherence: unknown | LCD transfer: unverified"
            )
        else:
            self.details_var.set("")
        idle = not (model.busy or model.closed or model.closing)
        self.refresh_button.configure(state="normal" if idle else "disabled")
        selected_row = model.selected_row()
        self.preview_button.configure(
            state="normal"
            if idle and selected_row is not None and model.clock() < selected_row.expires_at
            else "disabled"
        )
        for button in (self.approve_button, self.reject_button):
            button.configure(state="normal" if model.can_decide() else "disabled")
        self.status_var.set(model.message)
        self.status_label.configure(foreground="#9b1c1c" if model.failed else "")

    def hide(self) -> None:
        self.controller.hide()
        self._render()
        if self.dialog is not None:
            self.tree.selection_remove(*self.tree.selection())
            self.dialog.withdraw()

    def _drain(self) -> None:
        if self.worker.stopped.is_set():
            return
        try:
            while True:
                outcome = self.worker.results.get_nowait()
                if self.controller.accept(outcome):
                    self._render()
                    if self.controller.closed:
                        self._finish_close()
                        return
        except queue.Empty:
            pass
        self.root.after(75, self._drain)

    def _tick(self) -> None:
        if not self.worker.stopped.is_set():
            self._render()
            self.root.after(1000, self._tick)

    def can_close_window(self) -> bool:
        if self.controller.busy:
            self.controller.message = CLOSING
            self._render()
            if self.dialog is not None:
                self.dialog.deiconify()
                self.dialog.lift()
            return False
        return True

    def stop(self) -> bool:
        """Final root shutdown only; never abandon an in-flight decision."""
        if not self.can_close_window():
            return False
        self.controller.request_close()
        self._finish_close()
        return True

    def request_close(self, callback: Callable[[], None]) -> None:
        if self.controller.closed:
            callback()
            return
        if self.controller.closing:
            return
        self.on_closed = callback
        if self.controller.request_close():
            self._finish_close()
        else:
            self._render()

    def close(self, callback: Callable[[], None] | None = None) -> None:
        self.request_close(callback or (lambda: None))

    def _finish_close(self) -> None:
        self.worker.stop()
        self.photo = self.rendered = None
        if self.dialog is not None:
            self.dialog.destroy()
            self.dialog = None
        callback, self.on_closed = self.on_closed, None
        if callback is not None:
            callback()
