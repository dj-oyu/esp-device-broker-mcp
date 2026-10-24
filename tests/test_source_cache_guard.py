"""Process-local cache exclusion and cooperative budgets with tiny synthetic trees."""

import asyncio
import threading

import pytest
from test_source_preflight import registered

from cardputer_mcp import source_snapshot
from cardputer_mcp.config import Settings
from cardputer_mcp.source_cache_lock import LOCK_NAME
from cardputer_mcp.source_snapshot import SourceError, SourceSnapshots


def caches(tmp_path):
    settings = registered(Settings(), tmp_path)
    project = settings.build_projects["fixture"]
    alias = settings.model_copy(update={"build_projects": {"alias": project}})
    return SourceSnapshots(settings), SourceSnapshots(alias), project.source_repository.cache_root


async def test_other_thread_same_cache_returns_busy_without_blocking(tmp_path):
    first, second, _ = caches(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def own_cache():
        with first.cache_guard("fixture"):
            entered.set()
            assert release.wait(timeout=2)

    owned = asyncio.create_task(asyncio.to_thread(own_cache))
    assert await asyncio.to_thread(entered.wait, 1)
    try:
        with pytest.raises(SourceError, match="source_fetch_busy"):
            await asyncio.wait_for(asyncio.to_thread(second.usage, "alias"), 0.5)
        with pytest.raises(SourceError, match="source_fetch_busy"):
            await asyncio.wait_for(
                asyncio.to_thread(second.resolve, "alias", "a" * 64, "owner"), 0.5
            )
    finally:
        release.set()
        await owned
    assert second.usage("alias")["bytes"] == 0
    with first.cache_guard("fixture"):  # same-root guard is re-entrant and shared by aliases
        assert first.usage("fixture")["bytes"] == 0 and second.usage("alias")["bytes"] == 0


async def test_fetch_retains_guard_through_failed_download_and_pending_cleanup(
    tmp_path, monkeypatch
):
    first, second, root = caches(tmp_path)
    entered, release = threading.Event(), threading.Event()
    remove = source_snapshot.shutil.rmtree

    def no_network(*args):
        raise SourceError("source_fetch_interrupted")

    def cleanup(path):
        entered.set()
        assert release.wait(timeout=2)
        return remove(path)

    first.transport = no_network
    monkeypatch.setattr(source_snapshot.shutil, "rmtree", cleanup)
    plan = first.prepare("fixture", "a" * 40, "owner") | {"job_id": "synthetic-fetch"}
    task = asyncio.create_task(asyncio.to_thread(first.fetch, plan, lambda: True, lambda _: None))
    assert await asyncio.to_thread(entered.wait, 1)
    try:
        assert any(root.iterdir())
        with pytest.raises(SourceError, match="source_fetch_busy"):
            await asyncio.to_thread(second.usage, "alias")
    finally:
        release.set()
        with pytest.raises(SourceError, match="source_fetch_interrupted"):
            await task
    assert list(root.iterdir()) == [root / LOCK_NAME] and not first.uncertain
    assert (root / LOCK_NAME).stat().st_size == 64
    usage = second.usage("alias")
    assert usage["snapshots"] == 0 and usage["pending_snapshots"] == 0


def test_cache_inventory_rejects_nested_link_before_traversing_it(tmp_path):
    first, _, root = caches(tmp_path)
    root.mkdir(mode=0o700)
    leaf = root / "synthetic-snapshot" / "nested"
    leaf.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_bytes(b"SECRET")
    try:
        (leaf / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Creating a directory link requires platform support")
    with pytest.raises(SourceError, match="source_linked_path_rejected"):
        first.usage("fixture")
    assert (outside / "private").read_bytes() == b"SECRET"
