import hashlib
import sqlite3
from contextlib import closing, contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cardputer_mcp.config import BuildProject
from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.private_build_workspace import stage_private_build
from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import PrivateInputStore, inventory_digest
from cardputer_mcp.private_workspace_materialize import materialize_workspace


@pytest.fixture
def staging(tmp_path):
    source = (("CMakeLists.txt", b"# synthetic public source"),)
    (tmp_path / "CMakeLists.txt").write_bytes(source[0][1])
    profile = tmp_path / "fixture.ps1"
    profile.write_text("# not executed")
    private = (("fixture.bin", b"generated private fixture"),)
    reg = PrivateInputRegistration(
        local_root=tmp_path / "unused",
        destination="assets",
        source_commit="a" * 40,
        recipe_sha256="b" * 64,
        inventory_sha256=inventory_digest(
            tuple(InputFile(p, len(b), hashlib.sha256(b).hexdigest()) for p, b in private)
        ),
    )
    project = BuildProject(
        project_dir=tmp_path, trusted_profile=profile, private_inputs={"fixture": reg}
    )
    builder = FirmwareBuild(SimpleNamespace(build_projects={"p": project}))
    with patch.object(builder, "_project", return_value=(project, {"commit": "a" * 40})):
        recipe = builder.prepare("p", "owner", source_receipt="c" * 64)["fingerprint"]
        reg = reg.model_copy(update={"recipe_sha256": recipe})
        project.private_inputs["fixture"] = reg
        with closing(sqlite3.connect(":memory:")) as db:
            store = PrivateInputStore(db)
            builder.private_store_factory = lambda: nullcontext(store)
            receipt = store.ingest(
                registration=reg,
                project_id="p",
                input_id="fixture",
                principal="owner",
                files=private,
            )
            plan = builder.prepare(
                "p",
                "owner",
                source_receipt="c" * 64,
                private_input_id="fixture",
                private_input_receipt=receipt,
            )
            state = {"lease": False}

            @contextmanager
            def guard(*args):
                state["lease"] = True
                try:
                    yield
                finally:
                    state["lease"] = False

            def read(*args, **kwargs):
                assert state["lease"], "source read must occur under cache lease"
                return iter(source)

            snapshot = SimpleNamespace(
                cache_guard=guard,
                _resolve=lambda *args: (
                    tmp_path,
                    {
                        "commit": "a" * 40,
                        "files": {
                            p: {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                            for p, b in source
                        },
                    },
                ),
            )
            with (
                patch(
                    "cardputer_mcp.private_build_workspace.SourceSnapshots", return_value=snapshot
                ),
                patch(
                    "cardputer_mcp.private_build_workspace.read_registered_windows",
                    side_effect=read,
                ),
                patch(
                    "cardputer_mcp.private_build_workspace.materialize_windows_workspace",
                    side_effect=lambda plan, container, cancelled: materialize_workspace(
                        plan, protected_parent=container, cancelled=cancelled
                    ),
                ),
            ):
                # Scratch is outside source so reprepare does not hash generated files.
                scratch = tmp_path.parent / (tmp_path.name + "-scratch")
                scratch.mkdir()
                yield builder, plan, scratch, tmp_path


def test_owned_receipt_to_staging_keeps_source_identity(staging):
    builder, plan, scratch, source = staging
    with stage_private_build(builder, plan, principal="owner", container=scratch) as staged:
        assert (
            staged.directory / "assets/fixture.bin"
        ).read_bytes() == b"generated private fixture"
        assert (staged.directory / "CMakeLists.txt").read_bytes() == (
            source / "CMakeLists.txt"
        ).read_bytes()
        assert staged.build_fingerprint == plan["fingerprint"]
        assert str(staged.directory) not in repr(staged)
    assert list(scratch.iterdir()) == []
    assert not (source / "assets").exists()


def test_wrong_principal_never_stages(staging):
    builder, plan, scratch, _ = staging
    with pytest.raises(ValueError, match="not_owned"):
        with stage_private_build(builder, plan, principal="other", container=scratch):
            pytest.fail()
    assert list(scratch.iterdir()) == []


def test_fingerprint_tamper_never_stages(staging):
    builder, plan, scratch, _ = staging
    with pytest.raises(ValueError, match="build_inputs_changed"):
        with stage_private_build(
            builder, plan | {"fingerprint": "d" * 64}, principal="owner", container=scratch
        ):
            pytest.fail()
    assert list(scratch.iterdir()) == []


def test_consumer_failure_cleans_and_does_not_run_firmware(staging):
    builder, plan, scratch, _ = staging
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with stage_private_build(builder, plan, principal="owner", container=scratch):
            raise RuntimeError("synthetic failure")
    assert list(scratch.iterdir()) == []
    with pytest.raises(ValueError, match="workspace_unavailable"):
        builder._execute(plan, lambda _: None)


def test_revoked_or_changed_recipe_on_exit_cleans(staging):
    builder, plan, scratch, source = staging
    with pytest.raises(ValueError, match="recipe_mismatch"):
        with stage_private_build(builder, plan, principal="owner", container=scratch):
            (source / "fixture.ps1").write_text("# changed")
    assert list(scratch.iterdir()) == []


def test_cancel_before_staging_creates_nothing(staging):
    builder, plan, scratch, _ = staging
    builder.process.stop_requested.set()
    with pytest.raises(ValueError, match="interrupted"):
        with stage_private_build(builder, plan, principal="owner", container=scratch):
            pytest.fail()
    assert list(scratch.iterdir()) == []


def test_scratch_cannot_overlap_public_source(staging):
    builder, plan, _, source = staging
    before = sorted(p.name for p in source.iterdir())
    with pytest.raises(ValueError, match="source_overlap"):
        with stage_private_build(builder, plan, principal="owner", container=source):
            pytest.fail()
    assert sorted(p.name for p in source.iterdir()) == before


def test_source_read_mismatch_never_stages(staging):
    builder, plan, scratch, _ = staging
    with patch(
        "cardputer_mcp.private_build_workspace.read_registered_windows",
        return_value=iter((("CMakeLists.txt", b"changed"),)),
    ):
        with pytest.raises(ValueError, match="source_inventory_changed"):
            with stage_private_build(builder, plan, principal="owner", container=scratch):
                pytest.fail()
    assert list(scratch.iterdir()) == []
