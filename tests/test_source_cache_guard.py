"""Process-local cache exclusion and cooperative budgets with tiny synthetic trees."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_source_preflight import registered

from cardputer_mcp import source_snapshot
from cardputer_mcp.config import Settings
from cardputer_mcp.source_snapshot import SourceError, SourceSnapshots


def caches(tmp_path):
    settings = registered(Settings(), tmp_path)
    project = settings.build_projects["fixture"]
    alias = settings.model_copy(update={"build_projects": {"alias": project}})
    return SourceSnapshots(settings), SourceSnapshots(alias), project.source_repository.cache_root


def test_same_root_guard_is_shared_across_instances_and_alias_projects_and_is_reentrant(tmp_path):
    first, second, _ = caches(tmp_path)
    with first.cache_guard("fixture"):
        assert first.usage("fixture")["bytes"] == 0
        assert second.usage("alias")["bytes"] == 0


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
    assert list(root.iterdir()) == [] and not first.uncertain
    assert second.usage("alias")["snapshots"] == 0


def test_cache_inventory_counts_empty_directories_against_entry_budget(tmp_path, monkeypatch):
    first, _, root = caches(tmp_path)
    root.mkdir(mode=0o700)
    snapshot = root / "synthetic-snapshot"
    snapshot.mkdir()
    for index in range(5):
        (snapshot / f"empty-{index}").mkdir()
    monkeypatch.setattr(source_snapshot, "CACHE_SCAN_ENTRIES", 3)
    with pytest.raises(SourceError, match="source_cache_scan_limit"):
        first.usage("fixture")


def test_cache_inventory_has_cooperative_time_and_stop_budgets(tmp_path, monkeypatch):
    first, _, root = caches(tmp_path)
    root.mkdir(mode=0o700)
    (root / "synthetic-snapshot").mkdir()
    ticks = iter([0.0, 0.0, 6.0])
    monkeypatch.setattr(source_snapshot, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(SourceError, match="source_cache_scan_limit"):
        first.usage("fixture")
    monkeypatch.setattr(source_snapshot, "time", SimpleNamespace(monotonic=lambda: 0.0))
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(SourceError, match="source_fetch_interrupted"):
        first.usage("fixture", stop=stopped)


def test_cache_inventory_verifies_ancestors_per_directory_not_per_file(tmp_path, monkeypatch):
    first, _, root = caches(tmp_path)
    root.mkdir(mode=0o700)
    leaf = root / "synthetic-snapshot" / "nested" / "leaf"
    leaf.mkdir(parents=True)
    for index in range(250):
        (leaf / f"file-{index}").write_bytes(b"data")
    original = Path.lstat
    calls = 0

    def counted(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", counted)
    assert first.usage("fixture")["bytes"] == 1000
    assert calls < 100  # Files are checked once, without replaying every ancestor.


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
