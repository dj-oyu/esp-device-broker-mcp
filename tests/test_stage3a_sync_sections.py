"""Stage 3a: the device worker's synchronous sections, checked in the source.

DESIGN-REDESIGN §3 "同期区間の規則": ``admit()`` and ``finish()`` are plain functions,
and waiting happens only inside the per-kind executors. A sync function cannot
``await``, so its checks and writes share one loop step. This test reads the AST, so a
later edit that adds an ``await``, ``async with`` or ``async for`` to a marked section
(or turns it into ``async def``) fails here even if no race test happens to hit it.
"""

import ast
import inspect
from pathlib import Path

import pytest

import cardputer_mcp.broker as broker_module
import cardputer_mcp.job_kinds as job_kinds
from cardputer_mcp.broker import Broker
from cardputer_mcp.job_kinds import JOB_KINDS, kind_for
from cardputer_mcp.models import TestName as Action

MODULES = (broker_module, job_kinds)
# Sections that must exist and be marked; the scan below also covers every other
# function decorated with ``@sync_section``.
REQUIRED = {
    "broker.py": {"admit", "finish", "_accept_result", "_finish_failure", "_finish_success"}
    | {"_close_admitted"},
    "job_kinds.py": {
        "_commit_app_session",
        "_commit_flash",
        "_commit_source",
        "_commit_fixed_trial",
        "_commit_succeeded",
    },
}
ALWAYS_SYNC = {"admit", "finish"}
ASYNC_NODES = (ast.Await, ast.AsyncWith, ast.AsyncFor, ast.AsyncFunctionDef)


def _decorated(node: ast.AST) -> bool:
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name == "sync_section":
            return True
    return False


def _sections(tree):
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
            _decorated(node) or node.name in ALWAYS_SYNC
        ):
            found[node.name] = node
    return found


def _violations(sections):
    problems = []
    for function, node in sections.items():
        if not isinstance(node, ast.FunctionDef):
            problems.append((function, "async def"))
        if not _decorated(node):
            problems.append((function, "not marked @sync_section"))
        problems.extend(
            (function, type(child).__name__)
            for child in ast.walk(node)
            if child is not node and isinstance(child, ASYNC_NODES)
        )
    return problems


def _module_tree(module):
    path = Path(inspect.getsourcefile(module))
    return path.name, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__.rpartition(".")[2])
def test_every_sync_section_is_a_plain_function_without_await(module):
    name, tree = _module_tree(module)
    sections = _sections(tree)
    missing = REQUIRED[name] - set(sections)
    assert not missing, f"sync sections not found or not marked: {sorted(missing)}"
    assert _violations(sections) == []


def test_the_ast_check_rejects_each_async_form():
    """The scan must catch what it claims to, so a pass is not vacuous."""
    source = """
@sync_section
def awaits():
    await thing()

@sync_section
def async_with():
    async with thing():
        pass

@sync_section
def async_for():
    async for item in thing():
        pass

@sync_section
def nested():
    async def inner():
        pass

@sync_section
async def coroutine():
    pass

def admit():
    pass

@sync_section
def clean():
    return 1
"""
    found = _violations(_sections(ast.parse(source)))
    assert sorted(found) == sorted(
        [
            ("awaits", "Await"),
            ("async_with", "AsyncWith"),
            ("async_for", "AsyncFor"),
            ("nested", "AsyncFunctionDef"),
            ("coroutine", "async def"),
            ("admit", "not marked @sync_section"),
        ]
    )


def test_runtime_sections_are_not_coroutines():
    for name in REQUIRED["broker.py"]:
        function = getattr(Broker, name)
        assert not inspect.iscoroutinefunction(function), name
        assert not inspect.isgeneratorfunction(function), name


def test_every_test_name_has_one_declared_kind():
    assert set(JOB_KINDS) == {test.value for test in Action}
    for test, kind in JOB_KINDS.items():
        assert kind_for(test) is kind
        assert inspect.iscoroutinefunction(kind.execute), test
        assert not inspect.iscoroutinefunction(kind.commit), test
    # An undeclared name keeps the former fallback: the backend executor.
    assert kind_for("undeclared") is JOB_KINDS[Action.HELLO.value]


def test_the_worker_awaits_only_the_queue_the_prefetch_and_the_executor():
    _, tree = _module_tree(broker_module)
    work = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_work"
    )
    awaited = sorted(
        ast.unparse(node.value) for node in ast.walk(work) if isinstance(node, ast.Await)
    )
    assert awaited == [
        "kind_for(run.test).execute(self, run)",
        "self._prefetch_flash_binding(job_id)",
        "self.queue.get()",
    ]
    assert not any(isinstance(node, ast.AsyncWith | ast.AsyncFor) for node in ast.walk(work))
