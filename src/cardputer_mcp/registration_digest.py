"""Registration digests bind approved *content*, never where files are stored.

A digest ties a plan or receipt to the locally approved registration facts that
decide what executes or which provenance is trusted: repositories and submodule
allowlists, tool identities, profiles, allow flags and device selectors. Output and
cache locations (and their private-ACL confirmations) are operator housekeeping:
moving a folder must not invalidate durable evidence, so they are excluded.

Records written before this change were bound to digests that included those
locations. They stay verifiable through the old-style recomputation below, which
only ever uses the registered current path or an operator-listed
``previous_cache_roots`` entry. Unknown digests still fail closed.

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
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

# Location-only fields (never an executable identity or an allow decision).
SOURCE_LOCATION_FIELDS = {"cache_root", "previous_cache_roots"}
PROJECT_EXCLUDE: dict[str, Any] = {
    "build_root": True,
    "source_repository": {name: True for name in SOURCE_LOCATION_FIELDS},
}
# Previous releases hashed everything; only the field added later is dropped to
# reproduce their exact JSON.
_PROJECT_OLD_EXCLUDE: dict[str, Any] = {"source_repository": {"previous_cache_roots": True}}
FLASH_LOCATION_FIELDS = {"backup_directory"}
_BOOLS = (True, False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _compact(value: Any) -> str:
    """The text pydantic's ``model_dump_json`` produces for the same JSON-mode dump."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class _PreLocationSourceRegistration(BaseModel):
    """Exact field set/order hashed by releases before location-independent digests."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    github_repository: str
    cache_root: Path
    private_acl_confirmed: bool
    submodule_repositories: list[str]


def source_digest(registration: Any, *, legacy: bool = False) -> str:
    """Path-independent digest. ``legacy`` keeps the pre-submodule binding shape."""
    excluded = set(SOURCE_LOCATION_FIELDS)
    if legacy or not registration.submodule_repositories:
        excluded.add("submodule_repositories")
    return _sha(registration.model_dump_json(exclude=excluded))


def source_digests(registration: Any, *, schema_version: int | None = None) -> frozenset[str]:
    """Every digest a receipt/marker may legitimately carry for this registration.

    Schema 2 receipts were always bound with the submodule allowlist; only older
    (or unspecified) records also accept the pre-submodule shape.
    """
    include_legacy_shape = schema_version != 2
    roots = [registration.cache_root, *getattr(registration, "previous_cache_roots", ())]
    result = {source_digest(registration)}  # location-independent; no removed flag in it
    if include_legacy_shape:
        result.add(source_digest(registration, legacy=True))
    for root, acl in product(roots, _BOOLS):  # acl: the removed private_acl_confirmed
        old = _PreLocationSourceRegistration(
            github_repository=registration.github_repository,
            cache_root=root,
            private_acl_confirmed=acl,
            submodule_repositories=list(registration.submodule_repositories),
        )
        # Old releases dropped an empty submodule list from the hashed JSON.
        full = set() if registration.submodule_repositories else {"submodule_repositories"}
        result.add(_sha(old.model_dump_json(exclude=full)))
        if include_legacy_shape:
            result.add(_sha(old.model_dump_json(exclude={"submodule_repositories"})))
    return frozenset(result)


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
    """Current digest plus the old full-model digests of the current registration."""
    return frozenset(
        {project_digest(project), *(_sha(text) for text in _legacy_project_documents(project))}
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
