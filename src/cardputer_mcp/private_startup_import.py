"""Explicit local import request batch; no remote input or implicit authority."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from . import build_variant
from .firmware_build import FirmwareBuild
from .private_input_contract import InputFile, _hash, _inventory
from .private_registration_ingest import ingest_registered_input
from .store import Store


@dataclass(frozen=True, repr=False)
class PrivateImportRequest:
    project_id: str
    input_id: str
    source_receipt: str
    principal: str
    manifest: tuple[InputFile, ...] = field(repr=False)
    build_variant: str | None = None

    def __post_init__(self) -> None:
        if (
            any(
                type(s) is not str or not s
                for s in (self.project_id, self.input_id, self.source_receipt, self.principal)
            )
            or type(self.manifest) is not tuple
            or any(type(item) is not InputFile for item in self.manifest)
        ):
            raise ValueError("private_startup_import_request_invalid")
        if any(
            re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", s) is None
            for s in (self.project_id, self.input_id)
        ):
            raise ValueError("private_startup_import_request_invalid")
        _hash(self.source_receipt)
        _inventory(self.manifest)
        if self.build_variant is not None and not build_variant.name_ok(self.build_variant):
            raise ValueError("private_startup_import_request_invalid")


class StartupImports:
    """One startup attempt, exact requests and dynamic local authority callbacks.

    Receipts are private local results, ordered like requests. Imports commit
    individually. A later failure does not undo earlier database commits; even a
    failed import may have committed a receipt before its final recheck failed.
    This object is not authority; callbacks must consult the caller's real grant.
    """

    def __init__(
        self,
        requests: tuple[PrivateImportRequest, ...],
        *,
        authorized: Callable[[PrivateImportRequest], bool],
        cancelled: Callable[[], bool],
    ) -> None:
        if (
            type(requests) is not tuple
            or len(requests) > 16
            or any(type(item) is not PrivateImportRequest for item in requests)
            or not callable(authorized)
            or not callable(cancelled)
        ):
            raise ValueError("private_startup_import_batch_invalid")
        self._requests = requests
        self._authorized = authorized
        self._cancelled = cancelled
        self._attempted = False
        self._receipts: list[str] = []

    @property
    def receipts(self) -> tuple[str, ...]:
        return tuple(self._receipts)

    def run(self, builder: FirmwareBuild, store: Store) -> None:
        if self._attempted:
            raise ValueError("private_startup_import_already_attempted")
        self._attempted = True
        try:
            for request in self._requests:
                if self._cancelled() or self._authorized(request) is not True:
                    raise ValueError("private_startup_import_not_authorized")

                def authorized(req: PrivateImportRequest = request) -> bool:
                    return self._authorized(req)

                receipt = ingest_registered_input(
                    builder,
                    store,
                    project_id=request.project_id,
                    input_id=request.input_id,
                    source_receipt=request.source_receipt,
                    principal=request.principal,
                    manifest=request.manifest,
                    authorized=authorized,
                    cancelled=self._cancelled,
                    variant=(
                        {"build_variant": request.build_variant}
                        if request.build_variant is not None
                        else None
                    ),
                )
                self._receipts.append(receipt)
        except Exception:
            # Local callback/OS exceptions can contain paths, identities or input
            # filenames. No raw detail is propagated into broker diagnostics.
            raise ValueError("private_startup_import_failed") from None
