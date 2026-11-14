# Device errors and terminal job outcomes

Failures of device jobs are classified by `device_failure_outcome.py`. The removed
DERBY fixed-workload trial and its private raw evidence are no longer produced;
rows written by it stay in the durable history as generic records.

## failed or needs_review

A device job that raises a failure ends in one of two states. `failed` means the
device state is known: the serial port (or flash driver) close was confirmed and no
device flash write had started. The job keeps its result code, sets no uncertainty
latch and does not cancel other queued jobs. `needs_review` means the state is not
known; it cancels every queued job (`recovery_review_required`) and stays until it
is reviewed. The rule is in `src/cardputer_mcp/device_failure_outcome.py`; flash
specifics are in FIRMWARE-FLASH.md ("Failure outcomes").

| Failure | Close confirmed | Write started or possible | Outcome |
| --- | --- | --- | --- |
| Serial job (observe, capture, restart, app session) with a typed failure: marker/read/capture timeout, read error, unplug, device fault, protocol error | yes | never (no flash write) | `failed` |
| Same | no | - | `needs_review` |
| `serial_operation_uncertain`, `serial_close_unconfirmed`, `operation_interrupted_review_required`, `multiple_device_owners` | any | any | `needs_review` |
| Legacy `derby_smoke` capture failure that is a HOME recovery source (`serial_capture_timeout`, `serial_capture_invalid`) | any | any | `needs_review` |
| Mock `workflow` (models a flash stage) | any | yes | `needs_review` |
| Flash/restore/backup before `driver.write` | yes | no | `failed` |
| Flash/restore after `driver.write` began | any | yes | `needs_review` |
| Unknown exception, result validation failure, failed final commit | any | any | `needs_review` |

HOME recovery stays available only for an unreviewed `needs_review` capture failure
of a legacy `derby_smoke` job; no new such job can be created.

Historical job state and failure results are never rewritten by this change.
The `review` summary separates historical failure from resolved uncertainty;
current `status.safety` reports resource uncertainty and required next action.
Neither report grants authority or proves a physical device state. A pending
review requires inspection and, only where eligible and explicitly requested,
the existing HOME recovery flow. A resolved review retains the historical error.

Validation uses synthetic serial bytes and public fixture data. It does not open
a real device, deploy software, restart a service, or change authorization.
