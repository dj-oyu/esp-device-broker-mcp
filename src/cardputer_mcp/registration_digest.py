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
"""

import hashlib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

# Location-only fields (never an executable identity or an allow decision).
SOURCE_LOCATION_FIELDS = {"cache_root", "private_acl_confirmed", "previous_cache_roots"}
PROJECT_EXCLUDE: dict[str, Any] = {
    "build_root": True,
    "source_repository": {name: True for name in SOURCE_LOCATION_FIELDS},
}
# Previous releases hashed everything; only the field added later is dropped to
# reproduce their exact JSON.
_PROJECT_OLD_EXCLUDE: dict[str, Any] = {"source_repository": {"previous_cache_roots": True}}
FLASH_LOCATION_FIELDS = {"backup_directory", "private_backup_acl_confirmed"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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
    result = {source_digest(registration)}
    if include_legacy_shape:
        result.add(source_digest(registration, legacy=True))
    for root in roots:
        old = _PreLocationSourceRegistration(
            github_repository=registration.github_repository,
            cache_root=root,
            private_acl_confirmed=registration.private_acl_confirmed,
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


def project_digests(project: Any) -> frozenset[str]:
    """Current digest plus the old full-model digest of the current registration."""
    return frozenset(
        {project_digest(project), _sha(project.model_dump_json(exclude=_PROJECT_OLD_EXCLUDE))}
    )


def flash_digest(registration: Any) -> str:
    return _sha(registration.model_dump_json(exclude=FLASH_LOCATION_FIELDS))


def flash_digests(registration: Any) -> frozenset[str]:
    return frozenset({flash_digest(registration), _sha(registration.model_dump_json())})
