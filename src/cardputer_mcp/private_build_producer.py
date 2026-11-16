"""Default-uninstalled native producer composition; compiler remains trusted code.

No actual inputs are provisioned here. Native generated fixture validation is
required before a production caller installs this producer into PrivateBuildRuntime.
"""

import json
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .private_build_output import PrivateBuildLogs
from .private_build_runtime import PrivateBuildCandidate
from .private_build_workspace import _recheck, stage_private_build
from .private_input_contract import InputFile, _inventory
from .private_input_registration import PrivateInputRegistration
from .private_input_store import inventory_digest
from .private_input_windows import read_registered_windows
from .private_workspace_plan import WorkspacePlan
from .private_workspace_windows import materialize_windows_workspace

if TYPE_CHECKING:
    from .firmware_build import FirmwareBuild


class NativePrivateProducer:
    def __init__(
        self,
        builder: "FirmwareBuild",
        container: Path,
        compile_staged: Callable[[Path, Path, PrivateBuildLogs], tuple[InputFile, ...]],
    ) -> None:
        self.builder = builder
        self.container = container
        self.compile_staged = compile_staged
        self.retained: ExitStack | None = None

    def __call__(self, plan: dict[str, Any], logs: PrivateBuildLogs) -> PrivateBuildCandidate:
        if self.retained is not None:
            raise ValueError("private_workspace_cleanup_pending")
        plan = json.loads(json.dumps(plan))
        builder = self.builder
        principal = plan["build_principal"]
        _recheck(builder, plan, principal)
        if builder.process.active or builder.process.uncertain:
            raise ValueError("private_build_child_not_joined")
        contexts = ExitStack()
        try:
            source = contexts.enter_context(
                stage_private_build(builder, plan, principal=principal, container=self.container)
            )
            output = contexts.enter_context(
                materialize_windows_workspace(
                    WorkspacePlan(source.composition_identity, (("broker-output.marker", b""),)),
                    container=self.container,
                    cancelled=builder.process.stop_requested.is_set,
                )
            )
            if (
                source.directory == output
                or source.directory.is_relative_to(output)
                or output.is_relative_to(source.directory)
            ):
                raise ValueError("private_workspace_source_overlap")
            _recheck(builder, plan, principal)
            if builder.process.active or builder.process.uncertain:
                raise ValueError("private_build_child_not_joined")
            if builder.process.stop_requested.is_set():
                raise ValueError("private_build_cancelled")
            manifest = self.compile_staged(source.directory, output, logs)
            if builder.process.active or builder.process.uncertain:
                raise ValueError("private_build_child_not_joined")
            _recheck(builder, plan, principal)
            if (
                type(manifest) is not tuple
                or len(manifest) > 32
                or sum(item.size for item in manifest) > 128 * 1024 * 1024
            ):
                raise ValueError("private_artifact_limit")
            _inventory(manifest)
            if any(Path(item.path).suffix not in {".bin", ".elf"} for item in manifest):
                raise ValueError("private_artifact_type_invalid")
            registration = builder.settings.build_projects[plan["project_id"]].private_inputs[
                plan["private_input_id"]
            ]
            output_registration = PrivateInputRegistration(
                local_root=output,
                destination="outputs",
                source_commit=registration.source_commit,
                inventory_sha256=inventory_digest(manifest),
                recipe_sha256=registration.recipe_sha256,
            )
            files = tuple(
                read_registered_windows(
                    output_registration, manifest, cancelled=builder.process.stop_requested.is_set
                )
            )
            _recheck(builder, plan, principal)
            candidate = PrivateBuildCandidate(source.composition_identity, files)
        finally:
            if builder.process.active or builder.process.uncertain:
                # Do not delete directories while an owned child may still use them.
                self.retained = contexts
                builder.output_uncertain = True
            else:
                try:
                    contexts.close()
                except BaseException:
                    builder.output_uncertain = True
                    raise
        return candidate

    def release_after_join(self) -> None:
        """Explicit trusted recovery only; never search/kill a process by name/PID."""
        if self.builder.process.active or self.builder.process.uncertain:
            raise ValueError("private_build_child_not_joined")
        if self.retained is not None:
            contexts, self.retained = self.retained, None
            contexts.close()
        # Do not clear output_uncertain: broker recovery must establish all latches.
