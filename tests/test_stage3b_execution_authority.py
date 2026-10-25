"""Stage 3b: one ExecutionAuthority, with the intentional differences as table rows.

The behaviour of each checkpoint is pinned by tests/test_stage3b_authority_characterization.py.
This file checks the structure: the module is synchronous, every listed check exists,
the four broker gates call the authority instead of their own chains, and the
differences of docs/redesign/contracts/flash.md pitfall 14 stay visible in ``RULES``.
"""

import ast
import inspect
from pathlib import Path

from test_stage3a_sync_sections import _decorated, _module_tree, _sections, _violations

import cardputer_mcp.broker as broker_module
import cardputer_mcp.execution_authority as authority_module
from cardputer_mcp.execution_authority import RULES, STEPS, Checkpoint, ExecutionAuthority

ASYNC_NODES = (ast.Await, ast.AsyncWith, ast.AsyncFor, ast.AsyncFunctionDef)


def test_the_authority_module_never_awaits():
    name, tree = _module_tree(authority_module)
    assert not [type(node).__name__ for node in ast.walk(tree) if isinstance(node, ASYNC_NODES)]
    sections = _sections(tree)
    assert {"check", "allows", "_run"} <= set(sections)
    assert _violations(sections) == []
    for method in ("check", "allows", "_run"):
        function = getattr(ExecutionAuthority, method)
        assert not inspect.iscoroutinefunction(function)
        assert not inspect.isgeneratorfunction(function)


def test_every_checkpoint_lists_known_checks_and_one_callback():
    assert set(RULES) == set(Checkpoint)
    for checkpoint, rules in RULES.items():
        assert set(rules.steps) <= set(STEPS), checkpoint
        assert rules.steps.count("principal_callback") == 1, checkpoint
        # The callback's answer is checked after it, and every identity check
        # that must see the callback's effects is listed after it too.
        callback = rules.steps.index("principal_callback")
        assert rules.steps.index("authorized") > callback, checkpoint
        assert rules.steps.index("validator_unchanged") > callback, checkpoint
        assert set(rules.codes) <= set(rules.steps), checkpoint
    assert set(STEPS) == {step for rules in RULES.values() for step in rules.steps}


def _has(step):
    return {checkpoint for checkpoint, rules in RULES.items() if step in rules.steps}


FLASH = {Checkpoint.FLASH_IO, Checkpoint.FLASH_COMMIT}


def test_the_kept_differences_are_rows_of_the_table():
    """docs/redesign/contracts/flash.md section 3 and pitfall 14."""
    assert _has("queue_has_room") == _has("history_has_room") == {Checkpoint.ENQUEUE}
    assert _has("job_row_absent") == {Checkpoint.ENQUEUE}
    assert _has("not_physically_busy") == {Checkpoint.DISPATCH}
    assert _has("job_row_queued") == _has("plan_epoch") == {Checkpoint.DISPATCH}
    assert _has("registration_allows") == FLASH
    assert _has("collector_live") == {Checkpoint.ENQUEUE, Checkpoint.DISPATCH, Checkpoint.RUNNING}
    # The strong _execution_uncertain() only at admission; the running gates use the
    # weaker allow_profile_cleanup=True inside _running_job_binding_live.
    assert _has("not_uncertain") == {Checkpoint.ENQUEUE, Checkpoint.DISPATCH}
    assert _has("running_job_live") == {Checkpoint.RUNNING} | FLASH
    assert [c for c, rules in RULES.items() if rules.committing] == [Checkpoint.FLASH_COMMIT]
    assert RULES[Checkpoint.FLASH_IO].steps == RULES[Checkpoint.FLASH_COMMIT].steps
    # Registration and the lease identity are read before the flash callback only.
    flash = RULES[Checkpoint.FLASH_IO].steps
    assert flash.index("registration_allows") < flash.index("principal_callback")
    assert flash.count("registration_allows") == 1
    # Exceptions: only RUNNING turns any of them into a refusal; only flash lets a
    # callback exception through.
    assert [c for c, rules in RULES.items() if rules.errors_refuse] == [Checkpoint.RUNNING]
    assert {c for c, rules in RULES.items() if not rules.callback_errors_refuse} == FLASH
    # The principal: the lease's, except for flash; dispatch alone skips a missing one
    # and re-reads the validator for its call.
    assert {c for c, rules in RULES.items() if rules.capture_principal} == FLASH
    assert [c for c, rules in RULES.items() if rules.without_principal] == [Checkpoint.DISPATCH]
    assert [c for c, rules in RULES.items() if rules.reread_validator] == [Checkpoint.DISPATCH]
    assert "snapshot_validator" not in RULES[Checkpoint.DISPATCH].steps
    assert dict(RULES[Checkpoint.ENQUEUE].codes) == {"authorized": "principal_not_authorized"}
    assert {rules.refused for rules in RULES.values()} == {"execution_intent_not_available"}


def _broker_function(tree, name):
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    )


def test_the_broker_gates_go_through_the_authority():
    _, tree = _module_tree(broker_module)
    for name, call in (
        ("_execute", "self.authority.check"),
        ("admit", "self.authority.check"),
        ("_running_authorized", "self.authority.allows"),
        ("_flash_live", "self.authority.allows"),
    ):
        function = _broker_function(tree, name)
        calls = [
            ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)
        ]
        assert calls.count(call) == 1, name
        # No copied chain is left beside the authority call.
        for direct in ("collector_live", "live_binding", "flash_digests"):
            assert direct not in calls, (name, direct)
    assert _decorated(_broker_function(tree, "admit"))
    source = Path(inspect.getsourcefile(broker_module)).read_text(encoding="utf-8")
    assert "requires_current_principal" not in source
