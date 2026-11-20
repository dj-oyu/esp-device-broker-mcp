"""Registration digests bind approved *content*, never where files are stored.

A digest ties a plan or receipt to the locally approved registration facts that
decide what executes or which provenance is trusted: repositories, tool identities,
profiles, allow flags and device selectors. The submodule allowlist is advisory and
bound into none of them, so editing it invalidates nothing. Source receipts are owned
by repository, project and principal, not by a registration digest. Output and
cache locations (and their private-ACL confirmations) are operator housekeeping:
moving a folder must not invalidate durable evidence, so they are excluded.

Source receipts written before either change carried digests that included the
cache location or the allowlist; they need no recomputation, since a receipt is
checked by its repository, project and principal. Unknown project/flash digests
still fail closed.

The removed settings (``allow_flash``/``allow_backup``/``allow_restore``,
``unattended_flash.enabled`` and the ``private_*_acl_confirmed`` declarations) no longer
exist, so new digests do not contain them. Plans, receipts and bindings written while
they existed carry digests that did: each accepted set therefore also holds the digest
recomputed for every combination of the removed booleans (``_legacy_*`` below), so
nothing stored becomes invalid. The combinations only ever reproduce earlier values;
they never accept a registration that differs in any other field.
"""

import hashlib
import json
from collections.abc import Iterator
from itertools import product
from typing import Any

# Location-only fields (never an executable identity or an allow decision).
SOURCE_LOCATION_FIELDS = {"cache_root"}
SOURCE_ADVISORY_FIELDS = {"submodule_repositories"}
PROJECT_EXCLUDE: dict[str, Any] = {
    "build_root": True,
    "source_repository": {name: True for name in SOURCE_LOCATION_FIELDS | SOURCE_ADVISORY_FIELDS},
}
# What project_digest hashed while the allowlist was still bound.
_PROJECT_BOUND_EXCLUDE: dict[str, Any] = {
    "build_root": True,
    "source_repository": {name: True for name in SOURCE_LOCATION_FIELDS},
}
# Previous releases hashed everything. The field added later
# (``previous_cache_roots``) no longer exists, so their exact JSON needs no exclusion.
_PROJECT_OLD_EXCLUDE: dict[str, Any] = {}
FLASH_LOCATION_FIELDS = {"backup_directory"}
_BOOLS = (True, False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _compact(value: Any) -> str:
    """The text pydantic's ``model_dump_json`` produces for the same JSON-mode dump."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def source_digest(registration: Any, *, legacy: bool = False) -> str:
    """Path- and allowlist-independent digest (``legacy`` is kept for callers)."""
    del legacy
    return _sha(
        registration.model_dump_json(exclude=SOURCE_LOCATION_FIELDS | SOURCE_ADVISORY_FIELDS)
    )


def project_digest(project: Any) -> str:
    return _sha(project.model_dump_json(exclude=PROJECT_EXCLUDE))


def _legacy_project_documents(project: Any) -> Iterator[str]:
    """The old full-model JSON, with the removed ``private_acl_confirmed`` re-inserted."""
    document = project.model_dump(mode="json", exclude=_PROJECT_OLD_EXCLUDE)
    source_repository = document.get("source_repository")
    if source_repository is None:
        yield _compact(document)
        return
    for acl in _BOOLS:
        old: dict[str, Any] = {}
        for key, value in source_repository.items():
            old[key] = value
            if key == "cache_root":  # the field's old position
                old["private_acl_confirmed"] = acl
        yield _compact({**document, "source_repository": old})


def project_digests(project: Any) -> frozenset[str]:
    """Current digest plus the digests earlier releases computed for this registration
    (with the current allowlist, which they still bound)."""
    return frozenset(
        {
            project_digest(project),
            _sha(project.model_dump_json(exclude=_PROJECT_BOUND_EXCLUDE)),
            *(_sha(text) for text in _legacy_project_documents(project)),
        }
    )


def flash_digest(registration: Any) -> str:
    return _sha(registration.model_dump_json(exclude=FLASH_LOCATION_FIELDS))


_LEGACY_FLASH_HEAD = (
    "selector",
    "flash_bytes",
    "project_id",
    "python_tool",
    "esptool_directory",
    "esptool_sha256",
    "esptool_version",
)


def _legacy_flash_documents(registration: Any, *, locations: bool) -> Iterator[str]:
    """Old JSON for every combination of the removed booleans, in the old field order.

    ``locations`` False reproduces the location-independent digest of earlier
    releases; True the older ones that also hashed ``backup_directory`` and its ACL
    declaration.
    """
    base = registration.model_dump(mode="json")
    unattended = base["unattended_flash"]
    enabled_values = _BOOLS if unattended is not None else (None,)
    acl_values = _BOOLS if locations else (None,)
    for acl, backup, flash, restore, enabled in product(
        acl_values, _BOOLS, _BOOLS, _BOOLS, enabled_values
    ):
        document: dict[str, Any] = {key: base[key] for key in _LEGACY_FLASH_HEAD}
        if locations:
            document["backup_directory"] = base["backup_directory"]
            document["private_backup_acl_confirmed"] = acl
        document["original_elf_sha256"] = base["original_elf_sha256"]
        document["allow_backup"] = backup
        document["allow_flash"] = flash
        document["unattended_flash"] = (
            None if unattended is None else {"enabled": enabled, **unattended}
        )
        document["allow_restore"] = restore
        yield _compact(document)


def flash_digests(registration: Any) -> frozenset[str]:
    return frozenset(
        {
            flash_digest(registration),
            *(
                _sha(text)
                for locations in (False, True)
                for text in _legacy_flash_documents(registration, locations=locations)
            ),
        }
    )
