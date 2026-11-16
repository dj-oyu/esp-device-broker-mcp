"""Generated local composition checks; native protection is tested on Windows."""

from types import SimpleNamespace

import pytest

from cardputer_mcp import private_startup as startup
from cardputer_mcp.config import BuildProject, Settings
from cardputer_mcp.private_build_producer import NativePrivateProducer
from cardputer_mcp.private_startup import PrivateStartup, private_startup_factories


def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "os", SimpleNamespace(name="nt"))
    state, scratch = tmp_path / "state", tmp_path / "scratch"
    state.mkdir()
    scratch.mkdir()
    (state / "history.sqlite3").touch()
    return Settings(state_dir=state), scratch


def test_off_has_no_overrides_or_filesystem_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "_validate_paths", lambda *_: pytest.fail("off checked paths"))
    assert private_startup_factories(Settings(state_dir=tmp_path / "missing")) == {}
    assert private_startup_factories(Settings(), PrivateStartup(False, tmp_path)) == {}


def test_enabled_requires_windows(monkeypatch):
    monkeypatch.setattr(startup, "os", SimpleNamespace(name="posix"))
    with pytest.raises(ValueError, match="windows_required"):
        private_startup_factories(Settings(), PrivateStartup(True))


def test_enabled_requires_container(tmp_path, monkeypatch):
    settings, _ = prepared(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="container_required"):
        private_startup_factories(settings, PrivateStartup(True))


def test_missing_database_does_not_create_it(tmp_path, monkeypatch):
    settings, scratch = prepared(tmp_path, monkeypatch)
    db = settings.state_dir / "history.sqlite3"
    db.unlink()
    with pytest.raises(ValueError, match="existing_database_required"):
        private_startup_factories(settings, PrivateStartup(True, scratch))
    assert not db.exists()


@pytest.mark.parametrize("which", ["state", "project", "output"])
def test_overlap_refused(tmp_path, monkeypatch, which):
    settings, scratch = prepared(tmp_path, monkeypatch)
    if which == "state":
        scratch = settings.state_dir
    else:
        project = BuildProject(
            project_dir=scratch if which == "project" else tmp_path / "source",
            trusted_profile=tmp_path / "profile.ps1",
            build_root=scratch if which == "output" else None,
        )
        settings = settings.model_copy(update={"build_projects": {"fixture": project}})
    with pytest.raises(ValueError, match="path_overlap"):
        private_startup_factories(settings, PrivateStartup(True, scratch))


def test_composition_uses_native_defaults_without_starting(tmp_path, monkeypatch):
    settings, scratch = prepared(tmp_path, monkeypatch)
    factories = private_startup_factories(settings, PrivateStartup(True, scratch))
    seen = []
    monkeypatch.setattr(startup, "_verify_container", lambda p: seen.append(p))
    store = factories["private_store_session_factory"](
        settings.state_dir / "history.sqlite3", lambda: None
    )
    assert not store._opened
    assert store._guard is startup.PrivateStoreSession.__init__.__kwdefaults__["path_guard"]
    monkeypatch.setattr(startup, "PrivateArtifactStore", lambda db: SimpleNamespace(db=db))
    monkeypatch.setattr(startup, "PrivateRuntimeSession", lambda b, s, r: (b, s, r))
    builder = SimpleNamespace(settings=settings)
    session = SimpleNamespace(store=SimpleNamespace(db=object()))
    result = factories["private_runtime_session_factory"](builder, session)
    runtime = result[2]
    assert isinstance(runtime.produce, NativePrivateProducer)
    assert runtime.produce.compile_staged is None
    assert (
        runtime.operation_guard
        is startup.PrivateBuildRuntime.__init__.__kwdefaults__["operation_guard"]
    )
    assert seen == [scratch, scratch]


def test_settings_mutation_and_wrong_database_refused(tmp_path, monkeypatch):
    settings, scratch = prepared(tmp_path, monkeypatch)
    factories = private_startup_factories(settings, PrivateStartup(True, scratch))
    with pytest.raises(ValueError, match="settings_changed"):
        factories["private_store_session_factory"](tmp_path / "wrong.db", lambda: None)
    settings.build_projects["other"] = BuildProject(
        project_dir=tmp_path / "source", trusted_profile=tmp_path / "profile.ps1"
    )
    with pytest.raises(ValueError, match="settings_changed"):
        factories["private_store_session_factory"](
            settings.state_dir / "history.sqlite3", lambda: None
        )


def test_config_does_not_expose_private_path(tmp_path):
    assert str(tmp_path) not in repr(PrivateStartup(True, tmp_path))


def test_bad_native_scratch_fails_before_store_open(tmp_path, monkeypatch):
    settings, scratch = prepared(tmp_path, monkeypatch)
    factories = private_startup_factories(settings, PrivateStartup(True, scratch))
    monkeypatch.setattr(
        startup,
        "_verify_container",
        lambda _: (_ for _ in ()).throw(ValueError("private_workspace_acl_unexpected")),
    )
    with pytest.raises(ValueError, match="acl_unexpected"):
        factories["private_store_session_factory"](
            settings.state_dir / "history.sqlite3", lambda: None
        )
    assert (settings.state_dir / "history.sqlite3").stat().st_size == 0


def test_private_registration_mutation_refused(tmp_path, monkeypatch):
    from cardputer_mcp.private_input_registration import PrivateInputRegistration

    settings, scratch = prepared(tmp_path, monkeypatch)
    registration = PrivateInputRegistration(
        local_root=tmp_path / "input",
        destination="asset",
        source_commit="a" * 40,
        inventory_sha256="b" * 64,
        recipe_sha256="c" * 64,
    )
    project = BuildProject(
        project_dir=tmp_path / "source",
        trusted_profile=tmp_path / "profile.ps1",
        private_inputs={"fixture": registration},
    )
    settings.build_projects["fixture"] = project
    factories = private_startup_factories(settings, PrivateStartup(True, scratch))
    project.private_inputs["fixture"] = registration.model_copy(
        update={"local_root": tmp_path / "replacement"}
    )
    with pytest.raises(ValueError, match="settings_changed"):
        factories["private_store_session_factory"](
            settings.state_dir / "history.sqlite3", lambda: None
        )


@pytest.mark.parametrize("which", ["private", "cache", "previous", "ancestor"])
def test_other_protected_root_overlap(tmp_path, monkeypatch, which):
    from cardputer_mcp.config import SourceRepository
    from cardputer_mcp.private_input_registration import PrivateInputRegistration

    settings, scratch = prepared(tmp_path, monkeypatch)
    if which == "ancestor":
        scratch = tmp_path
    inputs = {}
    if which == "private":
        inputs["fixture"] = PrivateInputRegistration(
            local_root=scratch / "inputs",
            destination="asset",
            source_commit="a" * 40,
            inventory_sha256="b" * 64,
            recipe_sha256="c" * 64,
        )
    project = BuildProject(
        project_dir=tmp_path / "source",
        trusted_profile=tmp_path / "profile.ps1",
        private_inputs=inputs,
        source_repository=SourceRepository(
            github_repository="example/fixture",
            cache_root=scratch if which == "cache" else tmp_path / "cache",
            previous_cache_roots=[scratch] if which == "previous" else [],
        ),
    )
    settings.build_projects["fixture"] = project
    with pytest.raises(ValueError, match="path_overlap"):
        private_startup_factories(settings, PrivateStartup(True, scratch))
