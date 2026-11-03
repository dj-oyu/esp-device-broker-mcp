"""A failed flash tells the agent what the broker knows and how to continue."""

from types import SimpleNamespace

import pytest

from cardputer_mcp.job_reads import flash_recovery_evidence

H1, H2, H3 = "a" * 64, "b" * 64, "c" * 64


def broker(current=None):
    store = SimpleNamespace(firmware_current=lambda device: current)
    return SimpleNamespace(_store=lambda: store)


def plan(**fields):
    return {
        "test": "firmware_flash",
        "device_id": "cardputer",
        "manifest_sha256": H1,
        "expected_app_bin_sha256": H2,
        "expected_elf_sha256": H3,
        "lkg_receipt": H3,
        "backup_job_id": None,
        **fields,
    }


@pytest.mark.parametrize(
    ("state", "result", "step"),
    [
        ("needs_review", "flash_native_failed_needs_review", "device_may_hold_attempted_firmware"),
        (
            "needs_review",
            "flash_boot_unverified_needs_review",
            "device_may_hold_attempted_firmware",
        ),
        ("failed", "lkg_current_firmware_changed_needs_review", "device_differs_from_named_lkg"),
    ],
)
def test_failed_flash_reports_hashes_lkg_and_next_step(state, result, step):
    evidence = flash_recovery_evidence(
        broker({"receipt": H3}), {"state": state, "result": result}, plan()
    )
    assert evidence is not None
    assert evidence["attempted"] == {
        "manifest_sha256": H1,
        "firmware_sha256": H2,
        "elf_sha256": H3,
    }
    assert evidence["named_baseline"] == {"lkg_receipt": H3, "backup_job_id": None}
    assert evidence["current_lkg"] == H3 and evidence["lkg_unchanged"] is True
    assert evidence["next_step"] == step
    assert "omit lkg_receipt" in evidence["explanation"] or "omitted" in evidence["explanation"]


def test_no_lkg_recorded_is_reported_as_none():
    evidence = flash_recovery_evidence(
        broker(None),
        {"state": "needs_review", "result": "flash_native_failed_needs_review"},
        plan(lkg_receipt=None),
    )
    assert evidence is not None and evidence["current_lkg"] is None
    assert evidence["named_baseline"]["lkg_receipt"] is None


@pytest.mark.parametrize(
    ("job", "test"),
    [
        ({"state": "succeeded", "result": "firmware_flash_completed"}, "firmware_flash"),
        ({"state": "failed", "result": "source_download_failed"}, "firmware_flash"),
        ({"state": "failed", "result": "lkg_bytes_changed"}, "firmware_flash"),
        (
            {"state": "needs_review", "result": "flash_native_failed_needs_review"},
            "firmware_backup",
        ),
    ],
)
def test_other_jobs_get_no_evidence(job, test):
    assert flash_recovery_evidence(broker(), job, plan(test=test)) is None
    assert flash_recovery_evidence(broker(), job, None) is None
