"""Fixed device fault categories; never retain or publish console text.

An exception named ``interrupted`` is still a VM fault. Expected owner stops
must use their normal stop protocol, not an exception-message exemption.
"""

import re

ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
FAULT = re.compile(
    rb"Guru Meditation|panic(?:'ed)?|START_FAILED|PRESENTER_STEP_FAILED|APP[_ ]ERROR|"
    rb"APP_REFUSED|EXECUTION FAILED|abort\(|assert(?:ion)? failed|Stack canary|"
    rb"stack overflow|watchdog|Brownout detector|ESP_ERROR_CHECK failed|"
    rb"RUNAWAY|LOADSTALL|DEMOFAIL|FRAMEFAIL|LOADFAIL|DEGRADE|\bOOM\b|"
    rb"hook (?:rejected|threw):|stop hook did not finish|"
    rb"(?:Internal|Type|Range|Reference|Syntax|Eval|URI)Error\s*:",
    re.IGNORECASE,
)


def fault_category(raw: bytes) -> str | None:
    """Classify bounded caller-owned bytes, including fragmented VM messages."""
    cleaned = ANSI.sub(b"", raw)
    if not FAULT.search(cleaned):
        return None
    if re.search(rb"watchdog|FRAME RUNAWAY|JOB QUEUE RUNAWAY", cleaned, re.IGNORECASE):
        return "watchdog_or_runaway"
    if re.search(
        rb"(?:Internal|Type|Range|Reference|Syntax|Eval|URI)Error\s*:", cleaned
    ):
        return "vm_exception"
    return "device_fault"
