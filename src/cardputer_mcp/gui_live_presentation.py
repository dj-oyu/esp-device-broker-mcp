"""Bounded log presentation and keyed widget updates; no network or device IO."""

import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from .models import FLASH_TESTS, TestName

MAX_LOG_LINES = 256
MAX_LOG_BYTES = 128 * 1024
LOG_TESTS = frozenset({TestName.SOURCE, TestName.BUILD, TestName.APP_SESSION, *FLASH_TESTS})


def logs_applicable(test: str | None) -> bool:
    """Unknown historical request types keep the existing guarded read path."""
    return test is None or test in LOG_TESTS


def sync_rows(tree: Any, rows: list[tuple[str, str, tuple[str, ...]]]) -> None:
    """Patch stable IDs without stealing keyboard focus or the reading position."""
    selected, focused = tuple(tree.selection()), tree.focus()
    horizontal, vertical = tree.xview(), tree.yview()
    previous_order = tuple(tree.get_children())
    anchor = ""
    if previous_order and callable(getattr(tree, "identify_row", None)):
        # Tk 8.6 can identify offscreen rows at heading coordinates. Require
        # an actual visible bounding box containing this pixel, independently
        # of the selected/focused item.
        for y in range(tree.winfo_height()):
            identifier = tree.identify_row(y)
            bounds = tree.bbox(identifier) if identifier else ()
            if bounds and bounds[1] <= y < bounds[1] + bounds[3]:
                anchor = identifier
                break
    wanted = {identifier for identifier, _, _ in rows}
    removed = [identifier for identifier in previous_order if identifier not in wanted]
    if anchor and anchor not in wanted:
        position = previous_order.index(anchor)
        candidates = (*previous_order[position + 1 :], *reversed(previous_order[:position]))
        anchor = next((identifier for identifier in candidates if identifier in wanted), "")
    if removed:
        tree.delete(*removed)
    for position, (identifier, text, values) in enumerate(rows):
        if tree.exists(identifier):
            old = tree.item(identifier)
            if old.get("text", "") != text or tuple(map(str, old.get("values", ()))) != values:
                tree.item(identifier, text=text, values=values)
            if tree.index(identifier) != position:
                tree.move(identifier, "", position)
        else:
            tree.insert("", position, iid=identifier, text=text, values=values)
    keep = tuple(identifier for identifier in selected if tree.exists(identifier))
    if tuple(tree.selection()) != keep:
        tree.selection_set(keep)
    if focused and tree.exists(focused) and tree.focus() != focused:
        tree.focus(focused)
    if horizontal and tree.xview() != horizontal:
        tree.xview_moveto(horizontal[0])
    if anchor and tree.exists(anchor) and rows:
        # Restore the stable row, not its former fraction of a changing list.
        tree.yview_moveto(tree.index(anchor) / len(rows))
    elif vertical and tree.yview() != vertical:
        tree.yview_moveto(vertical[0])


def replace_text(widget: Any, text: str, *, follow: bool = False) -> None:
    """Append growing output and preserve the actual top visible line."""
    previous = widget.get("1.0", "end-1c")
    if previous == text:
        return
    xview, yview = widget.xview(), widget.yview()
    at_end = bool(yview and yview[-1] >= 0.999)
    top = widget.index("@0,0")
    append = bool(previous and text.startswith(previous))
    if not append:
        # Retention can trim a prefix while appending. Keep the surviving top line.
        line, column = map(int, top.split("."))
        tail = "\n".join(previous.split("\n")[line - 1 :])
        retained_at = text.find(tail) if tail else -1
        top = f"{text.count(chr(10), 0, retained_at) + 1}.{column}" if retained_at >= 0 else "1.0"
    widget.configure(state="normal")
    if append:
        widget.insert("end-1c", text[len(previous) :])
    else:
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
    widget.configure(state="disabled")
    if follow and at_end:
        widget.see("end")
    else:
        widget.yview(top)
    if xview:
        widget.xview_moveto(xview[0])


@dataclass(frozen=True)
class LogRequestState:
    """Selected-job read progress; notifications remain dirty during a read."""

    version: int = 1
    requested_version: int = 0
    request_id: int = 0

    @property
    def pending(self) -> bool:
        return self.request_id != 0

    @property
    def dirty(self) -> bool:
        return self.version > self.requested_version

    def changed(self) -> "LogRequestState":
        return replace(self, version=self.version + 1)

    def started(self, request_id: int) -> "LogRequestState":
        return replace(self, requested_version=self.version, request_id=request_id)

    def completed(self, request_id: int) -> "LogRequestState":
        return replace(self, request_id=0) if request_id == self.request_id else self

    def settled(self) -> "LogRequestState":
        """A denied read waits for an explicit retry or a new authentication generation."""
        return replace(self, request_id=0, requested_version=self.version)


@dataclass(frozen=True)
class LogView:
    phase: Literal[
        "idle",
        "loading",
        "available",
        "empty",
        "not_applicable",
        "expired",
        "denied",
        "unavailable",
    ] = "idle"
    epoch: str = ""
    sequence: int = 0
    lines: tuple[str, ...] = ()
    gap: bool = False
    has_more: bool = False
    received_utc: str = ""

    @property
    def cursor(self) -> dict[str, Any] | None:
        return {"epoch": self.epoch, "sequence": self.sequence} if self.epoch else None

    @property
    def message(self) -> str:
        return {
            "idle": "Select a job to read its permitted output.",
            "loading": "Reading permitted output...",
            "available": "Output received for the requesting principal",
            "empty": "No output is retained for this job yet.",
            "not_applicable": "Logs unsupported for this job type.",
            "expired": "Logs expired or not retained.",
            "denied": "Log access denied for this principal.",
            "unavailable": "Logs unavailable.",
        }[self.phase]


def read_log_result(view: LogView, response: Any) -> LogView:
    """Parse only output fields, never arbitrary response strings or manifests."""
    if type(response) is not dict:
        return LogView("unavailable")
    if response.get("ok") is not True:
        code = response.get("error")
        phase = (
            "denied" if code in ("not_authorized", "authentication_required", "local_operator_required")
            else "expired" if code == "expired"
            else "not_applicable" if code == "unsupported"
            else "unavailable"
        )
        return LogView(phase)
    try:
        cursor, entries = response["next_cursor"], response["entries"]
        if type(cursor) is not dict or type(entries) is not list or len(entries) > 100:
            raise ValueError
        epoch, sequence = cursor["epoch"], cursor["sequence"]
        if (
            type(epoch) is not str
            or re.fullmatch(r"[a-f0-9]{32}", epoch) is None
            or type(sequence) is not int
            or not 0 <= sequence <= 2**63 - 1
            or any(type(response.get(key)) is not bool for key in ("gap", "expired", "has_more"))
            or view.epoch == epoch
            and sequence < view.sequence
        ):
            raise ValueError
        if response["expired"]:
            if entries or response["has_more"]:
                raise ValueError
            return LogView("expired", epoch, sequence, gap=True)
        reset = bool(view.epoch and view.epoch != epoch)
        lines = [] if reset else list(view.lines)
        after = 0 if reset else view.sequence
        previous = 0
        for entry in entries:
            if type(entry) is not dict:
                raise ValueError
            number, text = entry["sequence"], entry["text"]
            if (
                type(number) is not int
                or not previous < number <= sequence
                or type(text) is not str
                or len(text) > 4096
            ):
                raise ValueError
            previous = number
            if number > after:
                lines.append(text)
        clipped = len(lines) > MAX_LOG_LINES
        lines = lines[-MAX_LOG_LINES:]
        size = sum(len(line.encode("utf-8")) for line in lines)
        while lines and size > MAX_LOG_BYTES:
            size -= len(lines.pop(0).encode("utf-8"))
            clipped = True
        return LogView(
            "available" if lines else "empty",
            epoch,
            sequence,
            tuple(lines),
            view.gap or response["gap"] or reset or clipped,
            response["has_more"],
        )
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError):
        return LogView("unavailable")
