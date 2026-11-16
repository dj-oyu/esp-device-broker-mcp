"""Trusted local startup composition; never enabled by MCP or normal settings.

This helper does not change ACLs, start a broker or grant build permission.
Imports require a separate explicit local request batch with dynamic authority.
It composes the existing native guards and real compiler path.
"""

import os
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from .config import Settings
from .firmware_build import FirmwareBuild
from .private_artifact_store import PrivateArtifactStore
from .private_build_producer import NativePrivateProducer
from .private_build_runtime import PrivateBuildRuntime
from .private_input_windows import _root_parts
from .private_runtime_session import PrivateRuntimeSession
from .private_startup_import import StartupImports
from .private_store_session import PrivateStoreSession
from .private_workspace_windows import _Scratch


@dataclass(frozen=True)
class PrivateStartup:
    enabled: bool = False
    scratch_container: Path | None = field(default=None, repr=False)


class PrivateFactories(TypedDict, total=False):
    private_store_session_factory: Callable[[Path, Callable[[], None]], PrivateStoreSession]
    private_runtime_session_factory: Callable[
        [FirmwareBuild, PrivateStoreSession], PrivateRuntimeSession
    ]


def _absolute(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise ValueError("private_startup_path_invalid")
    return path.resolve(strict=False)


def _check_paths(settings: Settings, container: Path) -> None:
    scratch = _absolute(container)
    state = _absolute(settings.state_dir)
    if scratch == Path(scratch.anchor) or not container.is_dir():
        raise ValueError("private_startup_container_invalid")
    if not (settings.state_dir / "history.sqlite3").is_file():
        raise ValueError("private_startup_existing_database_required")
    protected = [state]
    for project in settings.build_projects.values():
        protected.extend([_absolute(project.project_dir), _absolute(project.trusted_profile)])
        if project.build_root is not None:
            protected.append(_absolute(project.build_root))
        if project.source_repository is not None:
            protected.append(_absolute(project.source_repository.cache_root))
            protected.extend(_absolute(p) for p in project.source_repository.previous_cache_roots)
        protected.extend(_absolute(item.local_root) for item in project.private_inputs.values())
    if any(scratch.is_relative_to(p) or p.is_relative_to(scratch) for p in protected):
        raise ValueError("private_startup_path_overlap")


def _validate_paths(settings: Settings, container: Path) -> None:
    try:
        _check_paths(settings, container)
    except (OSError, RuntimeError):
        raise ValueError("private_startup_path_unavailable") from None


def _verify_container(container: Path) -> None:
    # Read-only, no-follow native traversal. Materialization repeats these checks
    # and creates protected child roots. No bootstrap ACL mutation here.
    native = _Scratch()
    parts = _root_parts(str(container))
    with ExitStack() as held:
        current = native.anchor(parts[0], held)
        for part in parts[1:]:
            current = native.child(current, part, True, held)
        native.verify_acl(current)


def private_startup_factories(
    settings: Settings,
    config: PrivateStartup | None = None,
    *,
    imports: StartupImports | None = None,
) -> PrivateFactories:
    """Explicit local caller supplies kwargs to Broker; off returns no overrides.

    Existing protected state must already be provisioned. The Store factory pins
    its database before opening SQLite; runtime factory validates scratch again
    immediately before installing. Per-job source/receipt/authority checks remain.
    """
    if imports is not None and type(imports) is not StartupImports:
        raise ValueError("private_startup_import_batch_invalid")
    config = config if config is not None else PrivateStartup()
    if type(config.enabled) is not bool:
        raise ValueError("private_startup_enabled_invalid")
    if not config.enabled:
        if imports is not None:
            raise ValueError("private_startup_import_requires_enabled_runtime")
        return {}
    if os.name != "nt":
        raise ValueError("private_startup_windows_required")
    container = config.scratch_container
    if container is None:
        raise ValueError("private_startup_container_required")
    _validate_paths(settings, container)
    captured = settings.model_copy(deep=True)

    def store_factory(path: Path, notify: Callable[[], None]) -> PrivateStoreSession:
        if settings != captured or path != captured.state_dir / "history.sqlite3":
            raise ValueError("private_startup_settings_changed")
        _validate_paths(captured, container)
        _verify_container(container)
        return PrivateStoreSession(path, on_commit=notify)

    def runtime_factory(
        builder: FirmwareBuild, session: PrivateStoreSession
    ) -> PrivateRuntimeSession:
        if builder.settings != captured:
            raise ValueError("private_startup_settings_changed")
        _validate_paths(captured, container)
        _verify_container(container)
        if imports is not None:
            imports.run(builder, session.store)
            if builder.settings != captured or settings != captured:
                raise ValueError("private_startup_settings_changed")
        producer = NativePrivateProducer(builder, container)
        runtime = PrivateBuildRuntime(producer, PrivateArtifactStore(session.store.db))
        return PrivateRuntimeSession(builder, session, runtime)

    return {
        "private_store_session_factory": store_factory,
        "private_runtime_session_factory": runtime_factory,
    }
