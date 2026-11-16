"""Disconnected staging orchestration; the firmware execution gate stays closed.

Only broker-internal trusted code may supply the container. This does not expose
MCP inputs, run build scripts, publish artifacts or change deployment settings.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .private_build_binding import plan_kwargs
from .private_input_contract import InputFile
from .private_input_registration import PrivateInputRegistration
from .private_input_store import inventory_digest
from .private_input_windows import read_registered_windows
from .private_workspace_plan import prepare_workspace
from .private_workspace_windows import materialize_windows_workspace
from .source_snapshot import SourceSnapshots

if TYPE_CHECKING:
    from .firmware_build import FirmwareBuild


@dataclass(frozen=True)
class StagedPrivateBuild:
    directory: Path = field(repr=False)
    composition_identity: str
    build_fingerprint: str


def _recheck(builder: "FirmwareBuild", plan: dict[str, Any], principal: str) -> None:
    builder._check_cancelled()
    if not plan_kwargs(plan) or plan.get("build_principal") != principal:
        raise ValueError("private_build_not_owned")
    if builder._reprepare(plan, principal)["fingerprint"] != plan["fingerprint"]:
        raise ValueError("build_inputs_changed")


@contextmanager
def stage_private_build(
    builder: "FirmwareBuild", plan: dict[str, Any], *, principal: str, container: Path
) -> Iterator[StagedPrivateBuild]:
    """Stage owned receipt bytes without replacing the public source identity.

    Not called from FirmwareBuild.execute yet. The consumer must keep the context
    alive until all build children are stopped/joined. Native Windows validation,
    authority/ownership across jobs, logs and private artifact provenance must be
    completed before installing this into the production execution path.
    """
    plan = dict(plan)  # Keep external plan-dict edits from changing this operation.
    _recheck(builder, plan, principal)
    project_id = plan["project_id"]
    registration = builder.settings.build_projects[project_id].private_inputs[
        plan["private_input_id"]
    ]
    snapshots = SourceSnapshots(builder.settings)
    # Hold the source cache lease across native byte extraction, not just resolve.
    with snapshots.cache_guard(project_id):
        root, metadata = snapshots._resolve(project_id, plan["source_receipt"], principal)
        source_root, scratch_root = root.resolve(strict=True), container.resolve(strict=True)
        if source_root.is_relative_to(scratch_root) or scratch_root.is_relative_to(source_root):
            raise ValueError("private_workspace_source_overlap")
        manifest = tuple(
            InputFile(path, record["bytes"], record["sha256"])
            for path, record in sorted(metadata["files"].items())
        )
        source_registration = PrivateInputRegistration(
            local_root=root,
            destination="source",
            source_commit=metadata["commit"],
            inventory_sha256=inventory_digest(manifest),
            recipe_sha256=registration.recipe_sha256,
        )
        source_bytes = tuple(
            read_registered_windows(
                source_registration,
                manifest,
                cancelled=builder.process.stop_requested.is_set,
            )
        )
    builder._check_cancelled()
    factory = builder.private_store_factory
    if factory is None:
        raise ValueError("private_build_registration_unavailable")
    with factory() as store:
        workspace = prepare_workspace(
            store=store,
            registration=registration,
            project_id=project_id,
            input_id=plan["private_input_id"],
            principal=principal,
            private_receipt=plan["private_input_receipt"],
            source_receipt=plan["source_receipt"],
            source_commit=metadata["commit"],
            source_inventory=manifest,
            source_bytes=source_bytes,
        )
    _recheck(builder, plan, principal)
    with materialize_windows_workspace(
        workspace,
        container=container,
        cancelled=builder.process.stop_requested.is_set,
    ) as directory:
        _recheck(builder, plan, principal)
        yield StagedPrivateBuild(directory, workspace.identity, plan["fingerprint"])
        _recheck(builder, plan, principal)
