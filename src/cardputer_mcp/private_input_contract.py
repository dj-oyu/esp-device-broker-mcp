"""Pure composition identity; not an importer, authorization or execution API.

Callers must verify owned durable receipts and registration before using this
contract. No filesystem reads, source mutation or private delivery occurs here.
"""

import hashlib
import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class InputFile:
    path: str
    size: int
    sha256: str


def _hash(value: str, length: int = 64) -> None:
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise ValueError("invalid_input_digest")


def _path(value: str) -> str:
    # Portable conservative names avoid Windows aliases, ADS and normalization.
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("invalid_input_path")
    parts = value.split("/")
    for part in parts:
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
            or part in {".", ".."}
            or part.endswith((".", " "))
            or part.split(".")[0].upper()
            in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
                *(f"LPT{i}" for i in range(10))}
        ):
            raise ValueError("invalid_input_path")
    return value.casefold()


def _inventory(files: tuple[InputFile, ...]) -> dict[str, InputFile]:
    if not files or len(files) > 100_000:
        raise ValueError("input_file_limit")
    indexed: dict[str, InputFile] = {}
    total = 0
    for file in files:
        key = _path(file.path)
        _hash(file.sha256)
        if type(file.size) is not int or file.size < 0:
            raise ValueError("invalid_input_size")
        total += file.size
        if total > 256 * 1024 * 1024:
            raise ValueError("input_byte_limit")
        if key in indexed:
            raise ValueError("input_path_collision")
        indexed[key] = file
    for key in indexed:
        parts = key.split("/")
        if any("/".join(parts[:i]) in indexed for i in range(1, len(parts))):
            raise ValueError("input_path_collision")
    return indexed


def composition_identity(
    *,
    project_id: str,
    principal: str,
    input_principal: str,
    input_project_id: str,
    source_receipt: str,
    input_receipt: str,
    source_commit: str,
    allowed_source_commit: str,
    registration_digest: str,
    recipe_digest: str,
    destination: str,
    source_files: tuple[InputFile, ...],
    private_files: tuple[InputFile, ...],
) -> str:
    """Bind exact inputs without treating private bytes as part of a Git commit.

    destination and allowed_source_commit are trusted registration values, never
    caller-selected permissions. This function alone does not authenticate them.
    """
    if not principal or principal != input_principal:
        raise ValueError("private_input_not_owned")
    if not project_id or project_id != input_project_id:
        raise ValueError("private_input_wrong_project")
    for digest in (source_receipt, input_receipt, registration_digest, recipe_digest):
        _hash(digest)
    _hash(source_commit, 40)
    _hash(allowed_source_commit, 40)
    if source_commit != allowed_source_commit:
        raise ValueError("private_input_commit_not_allowed")
    _path(destination)
    _inventory(source_files)
    _inventory(private_files)
    composed = source_files + tuple(
        InputFile(f"{destination}/{file.path}", file.size, file.sha256)
        for file in private_files
    )
    _inventory(composed)
    record = {
        "schema": "private-composition-v1",
        "project": project_id,
        "principal": principal,
        "source_receipt": source_receipt,
        "private_receipt": input_receipt,
        "public_commit": source_commit,
        "registration": registration_digest,
        "recipe": recipe_digest,
        "destination": destination,
        "files": sorted((f.path, f.size, f.sha256) for f in composed),
    }
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()
