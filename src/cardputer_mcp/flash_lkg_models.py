"""Validate-only models for the LKG envelope and the LKG metadata inside it.

Both describe bytes that are already stored under their sha256 receipt. They
check a parsed JSON value and are never dumped back: callers keep using the
parsed object itself, so the stored bytes and every receipt stay unchanged.
They accept everything the hand-written readers accepted before them.
"""

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict

from .flash_lifecycle import FlashFailure


def _schema_version_one(value: Any) -> Any:
    # Equality, as the reader always compared it: 1.0 and true also pass.
    if value != 1:
        raise ValueError("lkg_envelope_schema_version")
    return value


class LkgEnvelope(BaseModel):
    """`lkg/<receipt>`: exactly these three keys (flash_lkg_store.FirmwareSnapshots).

    `metadata` is opaque to the store and kept as the parsed object. Each
    `files` value names another stored object; FirmwareSnapshots.get checks it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Annotated[Any, AfterValidator(_schema_version_one)]
    metadata: Any
    files: dict[str, str]


# Compared in this order; the first missing key or mismatch decides the outcome.
LKG_BINDING_FIELDS = ("device_id", "principal", "selector_sha256", "flash_bytes")


class LkgMetadata(BaseModel):
    """The `metadata` of an LKG envelope, as firmware_flash reads it.

    Written shapes (all still read):
    - artifact_bundle: `bundle`, `manifest` and a `compat` dict;
    - full_backup written by a restore: `backup_sha256`, `backup_job_id`, `compat: null`;
    - full_backup written at flash time: the same without a `compat` key.
    Absent `compat` and `compat: null` both mean "no layout evidence"; which one
    was stored stays visible through `compat_stored`. Unknown keys are kept.
    Values are not type-checked here: the readers compare them as stored.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    device_id: Any
    principal: Any
    selector_sha256: Any
    flash_bytes: Any
    kind: Any = None
    expected_device_sha256: Any = None
    expected_elf_sha256: Any = None
    expected_app_bin_sha256: Any = None
    backup_sha256: Any = None
    backup_job_id: Any = None
    bundle: Any = None
    manifest: Any = None
    compat: Any = None

    @property
    def compat_stored(self) -> bool:
        return "compat" in self.model_fields_set


def bound_lkg_metadata(metadata: Any, binding: Mapping[str, Any], kind: Any) -> LkgMetadata:
    """Check a loaded LKG's device binding and kind, then view it as LkgMetadata.

    The binding keys are indexed one by one, so a missing key still escapes as a
    bare KeyError (and non-object metadata as TypeError) unless an earlier key
    already mismatched, exactly as before. Once they are present the metadata is
    a JSON object and the permissive model cannot fail.
    """
    for name in LKG_BINDING_FIELDS:
        if metadata[name] != binding[name]:
            raise FlashFailure("lkg_device_binding_changed")
    view = LkgMetadata.model_validate(metadata)
    if view.kind != kind:
        raise FlashFailure("lkg_receipt_kind_changed")
    return view


def lkg_compat(metadata: Mapping[str, Any]) -> Any:
    """The stored layout evidence of bound LKG metadata; None when absent or null."""
    return LkgMetadata.model_validate(metadata).compat
