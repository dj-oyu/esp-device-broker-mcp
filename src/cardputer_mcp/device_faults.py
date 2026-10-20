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
    rb"RUNAWAY|DEMOFAIL|FRAMEFAIL|LOADFAIL|DEGRADE|\bOOM\b|"
    rb"hook (?:rejected|threw):|stop hook did not finish|"
    rb"(?:Internal|Type|Range|Reference|Syntax|Eval|URI)Error\s*:",
    re.IGNORECASE,
)


def fault_category(raw: bytes, *, fixed_workload: bool = False) -> str | None:
    """Classify bounded caller-owned bytes, including fragmented VM messages."""
    cleaned = ANSI.sub(b"", raw)
    # LOADSTALL is a nonfatal phase event in the ordinary game smoke. The
    # pinned fixed diagnostic parser rejects it as an invalid measured trial.
    if fixed_workload and re.search(rb"LOADSTALL", cleaned, re.IGNORECASE):
        return "fixed_workload_stall"
    if not FAULT.search(cleaned):
        return None
    if re.search(rb"watchdog|FRAME RUNAWAY|JOB QUEUE RUNAWAY", cleaned, re.IGNORECASE):
        return "watchdog_or_runaway"
    if re.search(rb"(?:Internal|Type|Range|Reference|Syntax|Eval|URI)Error\s*:", cleaned):
        return "vm_exception"
    return "device_fault"


def fault_detail(raw: bytes) -> str | None:
    """Display hint only, after an existing fault decision; never retain text."""
    cleaned = ANSI.sub(b"", raw)
    if re.search(
        rb"\bAPP_LOAD\b[^\r\n]{0,256}\bcompile\b[^\r\n]{0,64}\bout of memory\b",
        cleaned,
        re.IGNORECASE,
    ):
        return "app_load_out_of_memory"
    if re.search(rb"\bOOM\b", cleaned):
        return "out_of_memory_observed"
    return None
