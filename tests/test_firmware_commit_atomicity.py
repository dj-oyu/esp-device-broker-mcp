"""Stage 0.5 characterization: Store.finish_firmware is one all-or-nothing commit.

A failure injected at each step inside the BEGIN IMMEDIATE transaction must
leave job state, summary, device_writes.seq, LKG history and the current
firmware_lkg pointer exactly as they were. No device IO.
"""

import pytest

from cardputer_mcp.store import Store

PROMOTION = dict(
    device_id="fixture",
    principal="owner",
    receipt="b" * 64,
    kind="artifact_bundle",
    baseline="initial-backup",
)
SUMMARY = {"readback_verified": True, "last_known_good_receipt": "b" * 64}


class _Injected(RuntimeError):
    pass


class _FailingConnection:
    """Delegates to the real connection; fails right after a matching statement."""

    def __init__(self, db, trigger, *, replace_sql=None, fail_commit=False, error=_Injected):
        self._db = db
        self._trigger = trigger
        self._replace_sql = replace_sql
        self._fail_commit = fail_commit
        self._error = error
        self.fired = False

    def execute(self, sql, *args):
        if self._trigger is not None and sql.startswith(self._trigger):
            self.fired = True
            if self._replace_sql is not None:
                return self._db.execute(self._replace_sql, *args)
            self._db.execute(sql, *args)
            raise self._error("injected")
        return self._db.execute(sql, *args)

    def commit(self):
        if self._fail_commit:
            self.fired = True
            raise self._error("injected commit failure")
        return self._db.commit()

    def __getattr__(self, name):
        return getattr(self._db, name)


def _durable(store):
    db = store.db
    return dict(
        jobs=[
            tuple(r) for r in db.execute("SELECT id,state,result,finished FROM jobs ORDER BY id")
        ],
        summaries=[tuple(r) for r in db.execute("SELECT * FROM firmware_job_summaries ORDER BY 1")],
        seq=store.device_write_seq(),
        history=[tuple(r) for r in db.execute("SELECT * FROM firmware_lkg_history ORDER BY 1")],
        current=[tuple(r) for r in db.execute("SELECT * FROM firmware_lkg ORDER BY 1")],
    )


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.put_plan("a" * 64, {})
    store.add_job("one", "a" * 64)
    store.set_job("one", "running")
    store.finish_firmware(
        "one", "firmware_flash_completed", PROMOTION | {"receipt": "a" * 64}, lambda: True,
        {"readback_verified": True}, "owner",
    )  # fmt: skip
    store.add_job("two", "a" * 64)
    store.set_job("two", "running")
    yield store
    store.db.close()


# step -> (statement prefix, kwargs for _FailingConnection)
STEPS = {
    "after_job_update": ("UPDATE jobs SET state='succeeded'", {}),
    "after_summary_insert": ("INSERT INTO firmware_job_summaries", {}),
    "after_seq_bump": ("UPDATE device_writes SET seq=seq+1", {}),
    "after_history_insert": ("INSERT OR IGNORE INTO firmware_lkg_history", {}),
    "after_pointer_upsert": ("INSERT INTO firmware_lkg VALUES", {}),
    "final_reread_mismatch": (
        "SELECT state,plan_hash,result FROM jobs",
        {"replace_sql": "SELECT 'running' AS state,plan_hash,result FROM jobs WHERE id=?"},
    ),
    "commit_failure": (None, {"fail_commit": True}),
    "crash_after_pointer_upsert": ("INSERT INTO firmware_lkg VALUES", {"error": KeyboardInterrupt}),
}


@pytest.mark.parametrize("step", sorted(STEPS))
def test_injected_failure_at_each_step_commits_nothing(store, tmp_path, step):
    trigger, options = STEPS[step]
    before = _durable(store)
    real = store.db
    failing = _FailingConnection(real, trigger, **options)
    store.db = failing
    expected = options.get("error", _Injected)
    if step == "final_reread_mismatch":
        expected = ValueError
    try:
        with pytest.raises(expected) as raised:
            store.finish_firmware(
                "two", "firmware_flash_completed", PROMOTION, lambda: True, SUMMARY, "owner"
            )
    finally:
        store.db = real
    assert failing.fired
    if expected is ValueError:
        assert str(raised.value) == "firmware_final_commit_not_authorized"
    assert not real.in_transaction
    assert _durable(store) == before
    # A second connection sees the same durable state.
    reopened = Store(tmp_path / "state.sqlite3")
    try:
        assert _durable(reopened) == before
    finally:
        reopened.db.close()
    # Nothing is left locked: the same job can still commit exactly once.
    store.finish_firmware(
        "two", "firmware_flash_completed", PROMOTION, lambda: True, SUMMARY, "owner"
    )
    after = _durable(store)
    assert after["seq"] == before["seq"] + 1
    assert store.firmware_current("fixture")["receipt"] == "b" * 64


@pytest.mark.parametrize(
    "case",
    ["missing_principal", "oversized_summary", "second_guard_false", "first_guard_false"],
)
def test_natural_rejections_commit_nothing(store, case):
    before = _durable(store)
    summary, principal, guard = SUMMARY, "owner", (lambda: True)
    code = "firmware_summary_invalid"
    if case == "missing_principal":
        principal = None
    elif case == "oversized_summary":
        summary = {"padding": "x" * 4096}
    else:
        code = "firmware_final_commit_not_authorized"
        answers = iter((True, False) if case == "second_guard_false" else (False,))

        def guard():
            return next(answers)

    with pytest.raises(ValueError, match=f"^{code}$"):
        store.finish_firmware(
            "two", "firmware_flash_completed", PROMOTION, guard, summary, principal
        )
    assert not store.db.in_transaction
    assert _durable(store) == before


def test_not_running_job_is_refused_before_any_write(store):
    store.set_job("two", "needs_review", "flash_readback_mismatch_needs_review")
    before = _durable(store)
    calls = []
    with pytest.raises(ValueError, match="^firmware_final_commit_not_authorized$"):
        store.finish_firmware(
            "two", "x", PROMOTION, lambda: calls.append(1) or True, SUMMARY, "owner"
        )
    # The guard is never evaluated for a job that is not running.
    assert calls == []
    assert _durable(store) == before


def test_backup_commit_without_promotion_or_summary_leaves_seq_and_lkg(store):
    before = _durable(store)
    store.finish_firmware("two", "firmware_backup_completed", None, lambda: True, None, "owner")
    after = _durable(store)
    assert after["seq"] == before["seq"]
    assert after["history"] == before["history"] and after["current"] == before["current"]
    assert after["summaries"] == before["summaries"]
    assert store.job("two")["state"] == "succeeded"


def test_history_keeps_first_completed_job_while_pointer_moves(store):
    store.finish_firmware(
        "two", "firmware_flash_completed", PROMOTION | {"receipt": "a" * 64}, lambda: True,
        SUMMARY, "owner",
    )  # fmt: skip
    assert store.firmware_known("fixture", "a" * 64)["completed_job"] == "one"
    assert store.firmware_current("fixture")["completed_job"] == "two"


def test_pointer_is_keyed_by_device_only_across_principals(store):
    # One current pointer per device; a different principal's success replaces it,
    # every principal reads the same row, and the installer stays recorded.
    store.finish_firmware(
        "two", "firmware_flash_completed", PROMOTION | {"principal": "other"}, lambda: True,
        SUMMARY, "other",
    )  # fmt: skip
    current = store.firmware_current("fixture")
    assert current["receipt"] == "b" * 64 and current["principal"] == "other"
    assert store.firmware_known("fixture", "a" * 64)["principal"] == "owner"
