"""Internal private finalization adapter; production execution gate stays closed.

Only a trusted producer may supply staged composition and captured output bytes.
This does not stage inputs, start children, install a store, or enable a build.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .private_artifact_store import PrivateArtifactStore
from .private_build_binding import plan_kwargs, require_same_binding

if TYPE_CHECKING:
    from .firmware_build import FirmwareBuild
    from .store import Store


def commit_private_candidate(
    builder: "FirmwareBuild",
    store: "Store",
    artifacts: PrivateArtifactStore,
    *,
    plan: dict[str, Any],
    composition_identity: str,
    files: tuple[tuple[str, bytes], ...],
    live: Callable[[], bool],
) -> str:
    """Serialize cancellation, atomic receipt/audit/pin/job commit, then notify.

    Call on the broker loop after worker exit. The same builder lock is used by
    request_stop. SQLite connection and tables must already be protected/installed.
    No await or background callback may be introduced in the lock region.
    """
    if artifacts.db is not store.db or not plan_kwargs(plan):
        raise ValueError("private_build_finalizer_invalid")
    plan = dict(plan)
    job_id = plan["job_id"]
    principal = plan["build_principal"]
    with builder._lock:

        def guard() -> bool:
            builder._check_cancelled()
            if builder.process.active or builder.process.uncertain or live() is not True:
                return False
            fresh = builder._reprepare(plan, principal)
            require_same_binding(plan, fresh)
            builder._check_cancelled()
            return bool(
                not builder.process.active
                and not builder.process.uncertain
                and fresh["fingerprint"] == plan["fingerprint"]
            )

        receipt = artifacts.publish(
            plan=plan,
            principal=principal,
            composition_identity=composition_identity,
            files=files,
            guard=guard,
            runtime_bookkeeping=True,
        )
        builder._committed_job = job_id
    # These are observability only. A notification failure must not rewrite the
    # committed successful job or cause replay of the publication transaction.
    store._console_state(job_id, "succeeded")
    notify = getattr(store.db, "_notify", None)
    if notify is not None:
        try:
            notify()
        except Exception:
            pass
    return receipt
