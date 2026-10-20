"""Fixed display-only explanations from bounded structured diagnostics."""

REASON_LABELS = {
    "app_load_out_of_memory": "起動失敗：メモリ不足の記録",
    "out_of_memory_observed": "メモリ不足の記録あり",
    "vm_exception": "VM例外を検出",
    "watchdog_or_runaway": "監視タイムアウト・暴走を検出",
    "fixed_workload_stall": "測定処理の停止を検出",
    "device_fault": "端末の異常を検出",
    "serial_marker_timeout": "端末の応答待ち時間を超過",
    "serial_capture_timeout": "画面取得の待機時間を超過",
    "serial_close_unconfirmed": "端末接続の終了を確認できず",
    "serial_read_failed": "端末からの読み取りに失敗",
    "source_cache_scan_limit": "ソースキャッシュ検査の上限超過",
    "firmware_build_failed": "ビルド失敗",
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
