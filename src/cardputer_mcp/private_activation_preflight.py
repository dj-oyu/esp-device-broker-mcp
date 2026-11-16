"""Read-only diagnostic, not an activation grant or production installation."""

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

from .private_build_binding import plan_kwargs, require_same_binding
from .private_input_store import PrivateInputReader
from .private_state_windows import verified_private_database

if TYPE_CHECKING:
    from .firmware_build import FirmwareBuild
    from .store import Store


def preflight_private_activation(
    builder: "FirmwareBuild",
    store: "Store",
    plan: dict[str, Any],
    *,
    principal: str,
    authorized: Callable[[], bool],
    state_guard: Callable[["Store"], AbstractContextManager[Any]] = verified_private_database,
) -> dict[str, str]:
    """Check an actual authenticated caller's registered build without changing state.

    Broker-loop only, with trusted internal callbacks. Native DB protection spans
    validation; each source resolution holds its own source-cache read lock.
    No factory is invoked and no schema is created. The summary is not reusable
    authority: execution must repeat its own checks under its operation guard.
    Native guard remains the default;
    tests may explicitly substitute a generated fixture guard.
    """
    snapshot = json.loads(json.dumps(plan, allow_nan=False))
    if (
        not principal
        or snapshot.get("build_principal") != principal
        or not snapshot.get("project_id")
        or not snapshot.get("source_receipt")
        or snapshot.get("device_id") is not None
        or snapshot.get("test") not in (None, "firmware_build")
        or not plan_kwargs(snapshot)
    ):
        raise ValueError("private_activation_plan_invalid")

    def check() -> None:
        if authorized() is not True:
            raise ValueError("private_activation_not_authorized")
        builder._check_cancelled()
        if (
            builder.private_runtime is not None
            or builder.process.active
            or builder.process.uncertain
            or builder.output_uncertain
            or store.db.in_transaction
        ):
            raise ValueError("private_activation_not_idle")

    with builder._lock:
        check()
        with state_guard(store):
            reader = PrivateInputReader(store.db)
            # Reprepare performs registered source ownership, recipe and complete
            # private receipt/registration rehash using this exact Store connection.
            for _ in range(2):
                check()
                fresh = builder._reprepare(snapshot, principal, _private_reader=reader)
                require_same_binding(snapshot, fresh)
                if fresh.get("fingerprint") != snapshot.get("fingerprint"):
                    raise ValueError("build_inputs_changed")
                check()
    return {"status": "preflight_checked", "runtime": "not_installed", "activation": "not_granted"}
