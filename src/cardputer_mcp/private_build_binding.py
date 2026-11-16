"""Bind owned private receipts into build identity; no staging or execution grant."""

import hashlib
import json
from typing import Any

from .private_input_contract import _hash
from .private_input_registration import PrivateInputRegistration
from .private_input_store import PrivateInputStore


def bind_private_build(
    *,
    store: PrivateInputStore,
    registration: PrivateInputRegistration,
    project_id: str,
    input_id: str,
    principal: str,
    receipt: str,
    source_receipt: str,
    source_commit: str,
    recipe_sha256: str,
) -> dict[str, str]:
    _hash(recipe_sha256)
    if recipe_sha256 != registration.recipe_sha256:
        raise ValueError("private_build_recipe_mismatch")
    _hash(source_receipt)
    _hash(source_commit, 40)
    if source_commit != registration.source_commit:
        raise ValueError("private_input_commit_not_allowed")
    # Rehash owned durable bytes; a matching receipt string alone is insufficient.
    store.resolve(
        receipt=receipt,
        registration=registration,
        project_id=project_id,
        input_id=input_id,
        principal=principal,
    )
    identity = {
        "schema": "private-build-binding-v1",
        "project_id": project_id,
        "input_id": input_id,
        "principal": principal,
        "private_receipt": receipt,
        "source_receipt": source_receipt,
        "source_commit": source_commit,
        "registration": registration.identity(project_id=project_id, input_id=input_id),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "private_input_id": input_id,
        "private_input_receipt": receipt,
        "private_binding_sha256": digest,
    }


def plan_kwargs(plan: dict[str, Any]) -> dict[str, str]:
    keys = ("private_input_id", "private_input_receipt", "private_binding_sha256")
    if not any(key in plan for key in keys):
        return {}
    if not all(key in plan and type(plan[key]) is str for key in keys):
        raise ValueError("private_build_binding_invalid")
    if not plan["private_input_id"]:
        raise ValueError("private_build_binding_invalid")
    _hash(plan["private_input_receipt"])
    _hash(plan["private_binding_sha256"])
    return {key: plan[key] for key in keys[:2]}


def require_same_binding(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    # Validate both complete records, including a present-but-invalid digest.
    plan_kwargs(expected)
    plan_kwargs(actual)
    keys = ("private_input_id", "private_input_receipt", "private_binding_sha256")
    if any(expected.get(key) != actual.get(key) for key in keys):
        raise ValueError("private_build_binding_changed")
