"""Removed permission/attestation settings that older configuration files still contain.

``allow_flash``/``allow_backup``/``allow_restore``, ``unattended_flash.enabled`` and the
``private_*_acl_confirmed`` declarations no longer gate anything. A legacy ``true`` is
accepted and ignored (it never widens what is permitted). A legacy ``false`` used to
mean "closed", so it is refused with a fixed message instead of silently becoming
"open"; the operator deletes the line. Messages name only the key, never a value.
"""

from collections.abc import Mapping
from typing import Any


def drop_removed_gates(data: Any, removed: Mapping[str, str]) -> Any:
    """``removed`` maps each legacy key to the fixed advice shown when it is ``false``."""
    if not isinstance(data, dict) or not any(key in data for key in removed):
        return data
    refusals = []
    for key, advice in removed.items():
        if key not in data:
            continue
        if type(data[key]) is not bool:
            refusals.append(f"removed_setting_{key}_must_be_boolean: delete the `{key}` line")
        elif data[key] is False:
            refusals.append(
                f"removed_setting_{key}_false: `{key} = false` is no longer supported; "
                f"delete the `{key}` line ({advice})"
            )
    if refusals:
        raise ValueError("; ".join(refusals))
    return {k: v for k, v in data.items() if k not in removed}
