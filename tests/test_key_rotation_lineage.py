"""Rotation lineage: only an explicit rotation lets a key reuse its predecessor's inputs.

Synthetic fixture keys and principals only, under temporary directories; no USB/SDK.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_flash_policy import env as env

from cardputer_mcp.broker import BrokerError
from cardputer_mcp.flash_binding import prepare_binding
from cardputer_mcp.key_registry import KeyRegistry
from cardputer_mcp.models import (
    AcquireRequest,
    ExecuteRequest,
    LeaseRequest,
    Origin,
    PrepareRequest,
)
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.registry_defaults import LiveKeyRegistry
from cardputer_mcp.server import RequestAuth


class Sources:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0
        self.counter = 0

    def clock(self) -> float:
        return self.now

    def random_bytes(self, size: int) -> bytes:
        self.counter += 1
        return self.counter.to_bytes(size, "big")


@pytest.fixture
def sources() -> Sources:
    return Sources()


@pytest.fixture
def registry(tmp_path: Path, sources: Sources) -> Iterator[KeyRegistry]:
    value = KeyRegistry(
        tmp_path / "keys.sqlite3", clock=sources.clock, random_bytes=sources.random_bytes
    )
    yield value
    value.close()


def test_rotation_chain_shares_one_owner(registry: KeyRegistry) -> None:
    first = registry.issue("agent", Origin.LOCAL, scopes={"read", "control"})
    second = registry.rotate(first.metadata.key_id)
    third = registry.rotate(second.metadata.key_id)
    a, b, c = (key.metadata.key_id for key in (first, second, third))
    assert registry.same_owner(a, a)
    assert registry.same_owner(a, b) and registry.same_owner(b, a)
    # The root owner is carried, not just the immediate predecessor.
    assert registry.same_owner(a, c) and registry.same_owner(b, c)
    # Principals stay distinct; lineage is not authentication.
    assert registry.authenticate("Bearer " + first.token) is None
    assert registry.authenticate("Bearer " + second.token) is None
    assert registry.authenticate("Bearer " + third.token) is not None


def test_fresh_issue_never_inherits_even_with_the_same_label(registry: KeyRegistry) -> None:
    first = registry.issue("agent", Origin.LOCAL, scopes={"read", "control"})
    successor = registry.rotate(first.metadata.key_id)
    unrelated = registry.issue("agent", Origin.LOCAL, scopes={"read", "control"})
    for key in (first, successor):
        assert not registry.same_owner(key.metadata.key_id, unrelated.metadata.key_id)
    # Revoke + issue (the compromise response) also starts a new lineage.
    registry.revoke(successor.metadata.key_id)
    after_revoke = registry.issue("agent", Origin.LOCAL, scopes={"read", "control"})
    assert not registry.same_owner(first.metadata.key_id, after_revoke.metadata.key_id)
    # Unknown or malformed identifiers fail closed.
    assert not registry.same_owner(first.metadata.key_id, "cpk_" + "f" * 32)
    assert not registry.same_owner("not-a-key", "not-a-key")


def test_failed_rotation_records_no_lineage(registry: KeyRegistry, sources: Sources) -> None:
    expired = registry.issue("agent", Origin.LOCAL, ttl_seconds=1)
    sources.now += 1
    with pytest.raises(ValueError, match="^key_not_active$"):
        registry.rotate(expired.metadata.key_id)
    with sqlite3.connect(registry._path) as db:
        assert db.execute("SELECT count(*) FROM key_lineage").fetchone()[0] == 0


def test_store_without_lineage_table_opens_and_rotation_adds_it(
    tmp_path: Path, sources: Sources
) -> None:
    path = tmp_path / "keys.sqlite3"
    writer = KeyRegistry(path, clock=sources.clock, random_bytes=sources.random_bytes)
    old = writer.issue("agent", Origin.REMOTE, scopes={"read", "control"})
    writer.close()
    # A registry written before lineage existed (same schema version 1).
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE key_lineage")
    reader = KeyRegistry(path, create=False, clock=sources.clock, random_bytes=sources.random_bytes)
    try:
        assert reader.authenticate("Bearer " + old.token) is not None
        new = reader.rotate(old.metadata.key_id)
        assert reader.same_owner(old.metadata.key_id, new.metadata.key_id)
    finally:
        reader.close()
    live = LiveKeyRegistry(path)
    assert live.same_owner(old.metadata.key_id, new.metadata.key_id)
    assert not LiveKeyRegistry(tmp_path / "missing.sqlite3").same_owner(
        old.metadata.key_id, new.metadata.key_id
    )


def test_request_auth_links_only_rotated_registry_keys(registry: KeyRegistry) -> None:
    first = registry.issue("agent", Origin.REMOTE, scopes={"read", "control"})
    successor = registry.rotate(first.metadata.key_id)
    unrelated = registry.issue("other", Origin.REMOTE, scopes={"read", "control"})
    auth = RequestAuth([None], [registry])
    old, new, other = (key.metadata.principal_id for key in (first, successor, unrelated))
    assert auth.same_owner(old, new)
    assert not auth.same_owner(old, other) and not auth.same_owner(new, other)
    # Ephemeral cloud/process principals keep exact-identity behavior.
    assert auth.same_owner("ephemeral:remote_mcp", "ephemeral:remote_mcp")
    assert not auth.same_owner("ephemeral:remote_mcp", new)
    assert not auth.same_owner(old, "ephemeral:remote_mcp")
    assert not RequestAuth([None], [None]).same_owner(old, new)


def _lineage(*pairs: tuple[str, str]):
    linked = {frozenset(pair) for pair in pairs}
    return lambda recorded, current: frozenset((recorded, current)) in linked


def _switch_principal(env, principal: str) -> str:
    broker = env.broker
    broker.release(LeaseRequest(session=broker.session.capability))
    return broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id=principal)["session"]


def _prepare(env, session: str, job_id: str, action: Action, **references):
    return env.broker.prepare(
        PrepareRequest(
            session=session, job_id=job_id, test=action, device_id="fixture", **references
        )
    )


async def test_rotated_successor_flashes_the_predecessors_build(env):
    broker = env.broker
    broker.principal_lineage = _lineage(("alice", "alice-rotated"))
    session = _switch_principal(env, "alice-rotated")
    before = env.driver.writes
    prepared = _prepare(env, session, "successor-flash", Action.FLASH, build_job_id="build-one")
    plan = prepared["plan"]
    # Stored principals are never rewritten: the flash plan is the successor's own,
    # the build plan keeps its original owner.
    assert plan["flash_principal"] == "alice-rotated"
    assert env.store.plan(env.store.job("build-one")["plan_hash"])["build_principal"] == "alice"
    assert prepare_binding(broker, plan).fetch_receipt == env.receipt
    broker.execute(ExecuteRequest(session=session, plan_hash=prepared["plan_hash"]))
    await broker.queue.join()
    assert broker.job("successor-flash")["state"] == "succeeded", broker.job("successor-flash")
    assert env.driver.writes > before


async def test_rotated_successor_restores_the_predecessors_backup(env):
    broker = env.broker
    broker.principal_lineage = _lineage(("alice", "alice-rotated"))
    session = _switch_principal(env, "alice-rotated")
    prepared = _prepare(
        env, session, "successor-restore", Action.RESTORE, backup_job_id="backup-one"
    )
    assert prepared["plan"]["flash_principal"] == "alice-rotated"
    broker.execute(ExecuteRequest(session=session, plan_hash=prepared["plan_hash"]))
    await broker.queue.join()
    assert broker.job("successor-restore")["state"] == "succeeded", broker.job("successor-restore")


@pytest.mark.parametrize("lineage", ["none", "other_pair", "raises", "truthy"])
async def test_unrelated_principal_cannot_use_another_keys_build_or_backup(env, lineage):
    broker = env.broker
    if lineage == "other_pair":
        broker.principal_lineage = _lineage(("alice", "alice-rotated"))
    elif lineage == "raises":

        def failing(*_):
            raise RuntimeError("registry unavailable")

        broker.principal_lineage = failing
    elif lineage == "truthy":
        # Only an exact True links principals.
        broker.principal_lineage = lambda *_: 1  # type: ignore[assignment,return-value]
    session = _switch_principal(env, "mallory")
    before = env.driver.writes
    with pytest.raises(BrokerError):
        _prepare(env, session, "foreign-flash", Action.FLASH, build_job_id="build-one")
    with pytest.raises(BrokerError):
        _prepare(env, session, "foreign-restore", Action.RESTORE, backup_job_id="backup-one")
    assert env.store.job("foreign-flash") is None and env.store.job("foreign-restore") is None
    assert env.driver.writes == before


async def test_off_loop_admission_rechecks_lineage_in_the_final_block(env):
    """execute_async: lineage is part of the binding recomputed with no await."""
    broker = env.broker
    broker.principal_lineage = _lineage(("alice", "alice-rotated"))
    session = _switch_principal(env, "alice-rotated")
    prepared = _prepare(env, session, "async-flash", Action.FLASH, build_job_id="build-one")
    request = ExecuteRequest(session=session, plan_hash=prepared["plan_hash"])

    def unlink() -> None:
        # Runs after the off-loop verification and before the final synchronous pass.
        broker.principal_lineage = None

    with pytest.raises(BrokerError):
        await broker.execute_async(request, reauthorize=unlink)
    assert env.store.job("async-flash") is None
    broker.principal_lineage = _lineage(("alice", "alice-rotated"))
    await broker.execute_async(request)
    await broker.queue.join()
    assert broker.job("async-flash")["state"] == "succeeded", broker.job("async-flash")
