"""Receipt-bound composition planning, with no cache or filesystem mutations.

The broker must authenticate/resolve the public source receipt before calling
this module. A digest is identity, not authorization. Materialization and build
execution deliberately remain outside this checkpoint.
"""

import hashlib
from dataclasses import dataclass, field

from .private_input_contract import InputFile, _inventory, composition_identity
from .private_input_registration import PrivateInputRegistration
from .private_input_store import PrivateInputStore


@dataclass(frozen=True)
class WorkspacePlan:
    identity: str
    files: tuple[tuple[str, bytes], ...] = field(repr=False)


def prepare_workspace(
    *,
    store: PrivateInputStore,
    registration: PrivateInputRegistration,
    project_id: str,
    input_id: str,
    principal: str,
    private_receipt: str,
    source_receipt: str,
    source_commit: str,
    source_inventory: tuple[InputFile, ...],
    source_bytes: tuple[tuple[str, bytes], ...],
) -> WorkspacePlan:
    """Prepare independent immutable bytes, never hardlinks into public cache.

    source_inventory must come from the broker's verified owned source receipt,
    not request parameters. Even matching caller-supplied inventory cannot prove
    that a public Git commit owns these bytes.
    """
    actual = []
    for path, payload in source_bytes:
        if type(payload) is not bytes:
            raise ValueError("source_bytes_required")
        actual.append(InputFile(path, len(payload), hashlib.sha256(payload).hexdigest()))
    if _inventory(tuple(actual)) != _inventory(source_inventory):
        raise ValueError("source_inventory_changed")
    private = store.resolve(
        receipt=private_receipt,
        registration=registration,
        project_id=project_id,
        input_id=input_id,
        principal=principal,
    )
    private_inventory = tuple(
        InputFile(path, len(data), hashlib.sha256(data).hexdigest()) for path, data in private
    )
    identity = composition_identity(
        project_id=project_id,
        principal=principal,
        input_principal=principal,
        input_project_id=project_id,
        source_receipt=source_receipt,
        input_receipt=private_receipt,
        source_commit=source_commit,
        allowed_source_commit=registration.source_commit,
        registration_digest=registration.identity(project_id=project_id, input_id=input_id),
        recipe_digest=registration.recipe_sha256,
        destination=registration.destination,
        source_files=source_inventory,
        private_files=private_inventory,
    )
    return WorkspacePlan(
        identity,
        tuple(
            sorted(
                source_bytes
                + tuple((f"{registration.destination}/{path}", data) for path, data in private)
            )
        ),
    )
