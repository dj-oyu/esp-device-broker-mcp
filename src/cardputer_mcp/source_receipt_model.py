"""Validate-only shape of a source snapshot receipt (receipt.json, schema 1 and 2).

A receipt's name is the sha256 of its stored bytes, so readers validate the parsed dict
and keep using that dict: nothing here dumps, normalizes or rewrites a receipt.

Each reader keeps its own ordered checks as its failure path, because their order decides
the reason code (schema 3 without a principal is ``source_receipt_not_owned`` in
``SourceSnapshots._resolve``, not ``source_snapshot_unavailable``; ``metadata`` never
raises for a missing key). The models state what a reader has already proven when it
accepts a receipt and are checked at that point, so they never reject a receipt a reader
accepts. Keys a reader does not check stay ``Any`` or undeclared (``extra="allow"``).
"""

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, StrictStr, TypeAdapter, model_validator

# Compared with ``in``/``==`` exactly as the readers always have: JSON true and 1.0 read
# as schema 1 and 2.0 as schema 2. Schema 2 adds ``submodules`` and ``root_source_sha256``.
SCHEMA_VERSIONS = (1, 2)


def supported_schema(version: Any) -> bool:
    return version in SCHEMA_VERSIONS


def _supported(version: Any) -> Any:
    if not supported_schema(version):
        raise ValueError("unsupported source receipt schema_version")
    return version


class _Shape(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class SourceReceiptHead(_Shape):
    schema_version: Annotated[Any, AfterValidator(_supported)]
    # Matched against a set of hex digests; an unhashable value never gets this far.
    registration_sha256: StrictStr


class SourceFileEntry(_Shape):
    """One ``files`` entry, equal to ``file_receipt`` output for the file on disk."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    bytes: int | float  # compared with ==, so 5.0 matches a 5-byte file
    sha256: StrictStr
    mode: Any  # passed back into file_receipt, so any stored value compares equal
    git_sha1: StrictStr


class SubmoduleEntry(_Shape):
    """The exact key set ``validate_submodule_provenance`` accepts."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: StrictStr
    repository: StrictStr
    commit: StrictStr
    tree_sha1: StrictStr
    archive_sha256: StrictStr
    source_sha256: StrictStr


_SUBMODULES = TypeAdapter(list[SubmoduleEntry])


class ResolvedSourceReceipt(SourceReceiptHead):
    """What ``SourceSnapshots._resolve`` has proven when it returns a receipt.

    ``repository``, ``commit``, ``tree_sha1`` and the other provenance keys are read by
    callers with ``[]`` and are deliberately not required here: a receipt missing one
    still resolves today and fails later in its caller.
    """

    principal: StrictStr
    project_id: StrictStr
    source_sha256: StrictStr
    files: dict[str, SourceFileEntry]

    @model_validator(mode="after")
    def _schema_2_submodules(self) -> "ResolvedSourceReceipt":
        if self.schema_version == 2:
            _SUBMODULES.validate_python((self.model_extra or {}).get("submodules"))
        return self


class ListedSourceReceipt(SourceReceiptHead):
    """What ``source_payloads.metadata`` has proven when it returns a receipt.

    It reads with ``.get`` and never looks at ``files`` or ``submodules``, so a schema 2
    receipt without valid submodules is still listed (and later refused by ``_resolve``).
    """

    repository: StrictStr
