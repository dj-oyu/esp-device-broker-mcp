"""Fixed display-only explanations from bounded structured diagnostics."""

REASON_LABELS = {
    "app_load_out_of_memory": "App load failed: out of memory observed",
    "out_of_memory_observed": "Out of memory observed",
    "vm_exception": "VM exception observed",
    "watchdog_or_runaway": "Watchdog timeout or runaway observed",
    "fixed_workload_stall": "Fixed workload stalled",
    "device_fault": "Device fault observed",
    "serial_marker_timeout": "Device response timed out",
    "serial_capture_timeout": "Screen capture timed out",
    "serial_close_unconfirmed": "Device connection close unconfirmed",
    "serial_read_failed": "Device read failed",
    "source_cache_scan_limit": "Source cache scan limit reached",
    "firmware_build_failed": "Firmware build failed",
}
DETAILS = frozenset({"app_load_out_of_memory", "out_of_memory_observed"})
CATEGORIES = frozenset(
    {"vm_exception", "watchdog_or_runaway", "fixed_workload_stall", "device_fault"}
)


def failure_reason(state: object, result: object, category: object, detail: object) -> str | None:
    if type(state) is not str or state not in {"failed", "needs_review"}:
        return None
    if type(result) is not str:
        return None
    if result == "serial_fault_marker":
        if type(detail) is str and detail in DETAILS:
            return detail
        if type(category) is str and category in CATEGORIES:
            return category
        return "device_fault"
    if type(result) is str and result in REASON_LABELS and result not in DETAILS | CATEGORIES:
        return result
    return None
