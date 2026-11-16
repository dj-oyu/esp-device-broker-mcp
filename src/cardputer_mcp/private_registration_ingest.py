"""Internal, default-uninvoked registration import adapter; no MCP upload API."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

from . import build_variant
from .private_input_contract import InputFile
from .private_input_store import PrivateInputStore, inventory_digest
from .private_input_windows import read_registered_windows
from .private_state_windows import verified_private_database
from .source_snapshot import SourceSnapshots

if TYPE_CHECKING:
    from .firmware_build import FirmwareBuild
    from .store import Store


def ingest_registered_input(
    builder: "FirmwareBuild",
    store: "Store",
    *,
    project_id: str,
    input_id: str,
    source_receipt: str,
    principal: str,
    manifest: tuple[InputFile, ...],
    authorized: Callable[[], bool],
    cancelled: Callable[[], bool],
    variant: dict[str, Any] | None = None,
    state_guard: Callable[["Store"], AbstractContextManager[Any]] = verified_private_database,
) -> str:
    """Caller supplies authenticated local authority and exact registered inventory.

    Does not select a root from remote input, change registration, enable runtime,
    or transfer bytes outside the already-protected broker database.
    """
    registration = builder.settings.build_projects[project_id].private_inputs[input_id]
    identity = registration.identity(project_id=project_id, input_id=input_id)
    if inventory_digest(manifest) != registration.inventory_sha256:
        raise ValueError("private_input_inventory_mismatch")
    selection = build_variant.plan_kwargs(variant or {})

    def stopped() -> bool:
        current = builder.settings.build_projects.get(project_id)
        current_registration = current.private_inputs.get(input_id) if current else None
        return (
            cancelled()
            or authorized() is not True
            or current_registration is None
            or current_registration.identity(project_id=project_id, input_id=input_id) != identity
        )

    def recheck() -> None:
        if stopped():
            raise ValueError("private_input_import_not_authorized")
        snapshots = SourceSnapshots(builder.settings)
        with snapshots.cache_guard(project_id):
            _, metadata = snapshots._resolve(project_id, source_receipt, principal)
            if metadata["commit"] != registration.source_commit:
                raise ValueError("private_input_source_changed")
        prepared = builder.prepare(project_id, principal, source_receipt, **selection)
        if prepared["fingerprint"] != registration.recipe_sha256 or stopped():
            raise ValueError("private_input_recipe_changed")

    recheck()  # No private root reads or DB writes before authority/source/recipe match.
    with state_guard(store):
        recheck()
        inputs = PrivateInputStore(store.db)
        receipt = inputs.ingest(
            registration=registration,
            project_id=project_id,
            input_id=input_id,
            principal=principal,
            files=read_registered_windows(registration, manifest, cancelled=stopped),
            cancelled=stopped,
        )
        # If inputs changed during collection, do not report a usable import. The
        # retained receipt is still private and remains bound to the old registration.
        recheck()
        return receipt
