"""Generated native ingest at startup; source/recipe synthetic, no IDF or live inputs."""

import hashlib
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_private_state_windows_native import new_protected_directory

from cardputer_mcp import private_registration_ingest
from cardputer_mcp.broker import Broker
from cardputer_mcp.config import BuildProject, Settings
from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import PrivateInputReader, inventory_digest
from cardputer_mcp.private_startup import PrivateStartup, private_startup_factories
from cardputer_mcp.private_startup_import import PrivateImportRequest, StartupImports

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


async def test_native_startup_ingests_generated_bytes_before_workers(tmp_path, monkeypatch):
    state_parent, scratch_parent = tmp_path / "state", tmp_path / "scratch"
    state_parent.mkdir()
    scratch_parent.mkdir()
    state = new_protected_directory(state_parent)
    scratch = new_protected_directory(scratch_parent)
    (state / "history.sqlite3").touch()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    payload = b"generated startup fixture only"
    (inputs / "fixture.bin").write_bytes(payload)
    manifest = (InputFile("fixture.bin", len(payload), hashlib.sha256(payload).hexdigest()),)
    registration = PrivateInputRegistration(
        local_root=inputs,
        destination="assets",
        source_commit="a" * 40,
        recipe_sha256="b" * 64,
        inventory_sha256=inventory_digest(manifest),
    )
    settings = Settings(
        state_dir=state,
        build_projects={
            "fixture": BuildProject(
                project_dir=tmp_path / "source",
                trusted_profile=tmp_path / "profile.ps1",
                private_inputs={"fixture": registration},
            )
        },
    )
    request = PrivateImportRequest("fixture", "fixture", "c" * 64, "fixture-owner", manifest)
    checked = []

    def authorized(item):
        assert item is request
        assert broker.worker is None and broker.profile_jobs.worker is None
        assert broker.store is not None
        checked.append(True)
        return True  # Only this synthetic fixture, not a production grant.

    batch = StartupImports((request,), authorized=authorized, cancelled=lambda: False)
    factories = private_startup_factories(settings, PrivateStartup(True, scratch), imports=batch)
    broker = Broker(settings, lock_path=tmp_path / "device.lock", **factories)
    monkeypatch.setattr(
        private_registration_ingest,
        "SourceSnapshots",
        lambda _: SimpleNamespace(
            cache_guard=lambda _: nullcontext(),
            _resolve=lambda *_: (tmp_path / "source", {"commit": "a" * 40}),
        ),
    )
    monkeypatch.setattr(
        broker.firmware_build, "prepare", lambda *_a, **_k: {"fingerprint": "b" * 64}
    )
    await broker.start()
    try:
        assert checked and len(batch.receipts) == 1
        reader = PrivateInputReader(broker._store().db)
        assert reader.resolve(
            receipt=batch.receipts[0],
            registration=registration,
            project_id="fixture",
            input_id="fixture",
            principal="fixture-owner",
        ) == (("fixture.bin", payload),)
        with pytest.raises(ValueError, match="not_owned"):
            reader.resolve(
                receipt=batch.receipts[0],
                registration=registration,
                project_id="fixture",
                input_id="fixture",
                principal="other",
            )
        assert broker.private_runtime_session is not None
        assert not broker.firmware_build.manifests
    finally:
        await broker.close()
    assert broker.store is None
