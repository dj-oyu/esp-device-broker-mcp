"""Classify a failed device job as `failed` or `needs_review`.

`needs_review` is a barrier: it latches nothing by itself, but the broker cancels
every queued job (`recovery_review_required`) and the row blocks HOME recovery until
it is reviewed. It is for outcomes whose device state cannot be established. A
failure whose device state *is* known (nothing was written and the port/driver close
was confirmed) is an ordinary `failed` job with its existing result code; other
queued work keeps its place.

| Failure point                                  | Close     | Write started | Outcome      |
|                                                | confirmed | or possible   |              |
| ---------------------------------------------- | --------- | ------------- | ------------ |
| Known failure of a job with no device write    | yes       | no            | failed       |
| Known failure of a job with no device write    | no        | no            | needs_review |
| Flash/restore/backup before `driver.write`     | yes       | no            | failed       |
|   (probe, security, fence, identity, pre-write |           |               |              |
|   read error, authority lost before the write) |           |               |              |
| Flash/restore after `driver.write` began       | any       | yes           | needs_review |
|   (write, readback, boot, authority/cancel)    |           |               |              |
| Uncertain code (see UNCERTAIN_CODES)           | any       | any           | needs_review |
| DERBY capture failure (HOME recovery source)   | any       | any           | needs_review |
| Unknown exception (state not established)      | any       | any           | needs_review |

"Write" means a device flash write (`driver.write`). Non-flash serial jobs never
start one. The mock `workflow` kind models a flash stage, so it counts as a
possible write. A DERBY capture failure stays `needs_review` because the explicit
HOME recovery flow (HOME-RECOVERY.md) acts only on an unreviewed `needs_review`
source.

The classification never changes a result code, never retries, and never touches
the device. Pure function: no I/O, no clock.
"""

from dataclasses import dataclass
from typing import Literal

Outcome = Literal["failed", "needs_review"]

# Codes that by themselves say the device or ownership state is not known.
UNCERTAIN_CODES = frozenset(
    {
        "serial_operation_uncertain",
        "serial_close_unconfirmed",
        "operation_interrupted_review_required",
        "multiple_device_owners",
        "flash_shutdown_uncertain_needs_review",
        "flash_device_busy_or_uncertain",
        "flash_reader_handoff_unconfirmed",
        # The device is untouched, but the private local backup files may be partial.
        "backup_persistence_failed_needs_review",
    }
)

# Mirrors home_recovery.CAPTURE_FAILURES (kept here to avoid an import cycle;
# tests/test_device_failure_outcome.py pins that they are equal).
HOME_RECOVERY_SOURCE_FAILURES = frozenset({"serial_capture_invalid", "serial_capture_timeout"})


@dataclass(frozen=True)
class FailureEvidence:
    code: str
    # A typed failure with a fixed code, not an unknown exception.
    known: bool
    # The serial port / flash driver confirmed close (nothing still open or uncertain).
    close_confirmed: bool
    # A device flash write was started, or may have been started.
    write_possibly_started: bool
    # The source of an explicit HOME recovery (DERBY capture failure).
    home_recovery_source: bool = False


def classify(evidence: FailureEvidence) -> Outcome:
    if (
        evidence.known
        and evidence.close_confirmed
        and not evidence.write_possibly_started
        and not evidence.home_recovery_source
        and evidence.code not in UNCERTAIN_CODES
    ):
        return "failed"
    return "needs_review"
