"""Local terminal key management; deliberately never exposed as MCP tools.

Only issue/rotate reveal a token: once, directly on the confirmed operator's
terminal, or, only with an explicit ``write_env`` path, into that already
existing private stdio env file (see env_file_writer) without displaying it.
Their return values are metadata-only and safe for ordinary CLI JSON. No secret
is accepted in an argument or copied to any other file, clipboard or environment.
Tests must inject synthetic registry randomness, terminal streams and inputs.
"""

import getpass
import re
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, TextIO

from .env_file_writer import (
    EnvFileWriteError,
    PreparedEnvWrite,
    check_env_file,
    prepare_env_write,
)
from .key_registry import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    IssuedKey,
    KeyMetadata,
    KeyPrincipal,
    KeyRegistry,
    KeyRegistryError,
)
from .models import Origin
from .registry_defaults import (
    default_key_registry_path,
    prepare_default_key_directory,
)

KeyCommand = Literal["issue", "list", "revoke", "rotate", "verify"]
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@()\-]{0,63}\Z")
_KEY_ID = re.compile(r"cpk_[a-f0-9]{32}\Z")
_WARNING = (
    "The new persistent key will be shown ONCE on this local terminal. "
    "It remains in terminal scrollback and may be captured by terminal recording. "
    "Anyone with the key can use its granted access until expiry or revocation. "
    "Only its hash is stored in the registry. Keep the displayed key private; "
    "do not paste it into logs or shell arguments."
)
_ENV_WARNING = (
    "The new persistent key will be written into the selected existing private env "
    "file, replacing only its CARDPUTER_CLIENT_KEY line, and will not be displayed. "
    "Anyone who can read that file can use the key until expiry or revocation. "
    "Only its hash is stored in the registry."
)


class KeyOperatorError(ValueError):
    """Fixed, sanitized local-operator errors."""


class RegistryFactory(Protocol):
    def __call__(self, path: Path, *, create: bool = True) -> KeyRegistry: ...


def _write(stream: TextIO, text: str) -> None:
    try:
        if stream.write(text) != len(text):
            raise KeyOperatorError("key_terminal_unavailable")
        stream.flush()
    except (OSError, ValueError, UnicodeError):
        raise KeyOperatorError("key_terminal_unavailable") from None


def _tty(input_stream: TextIO, output_stream: TextIO) -> None:
    try:
        if input_stream.isatty() and output_stream.isatty():
            return
    except (OSError, ValueError):
        pass
    raise KeyOperatorError("key_interactive_terminal_required")


def _confirm(
    exact: str,
    description: str,
    callback: Callable[[str], str] | None,
    input_stream: TextIO,
    output_stream: TextIO,
) -> None:
    _write(output_stream, description + "\n")
    prompt = f"Type {exact} to continue: "
    try:
        if callback is None:
            _write(output_stream, prompt)
            answer = input_stream.readline().rstrip("\r\n")
        else:
            answer = callback(prompt)
    except (EOFError, KeyboardInterrupt, OSError, ValueError):
        raise KeyOperatorError("key_action_cancelled") from None
    if answer != exact:
        raise KeyOperatorError("key_action_cancelled")


def _principal(principal: KeyPrincipal) -> dict[str, object]:
    return {
        "principal_id": principal.principal_id,
        "key_id": principal.key_id,
        "origin": principal.origin.value,
        "scopes": sorted(principal.scopes),
    }


def _metadata(key: KeyMetadata) -> dict[str, object]:
    return {
        **_principal(key),
        "label": key.label,
        "issued_at": key.issued_at,
        "expires_at": key.expires_at,
        "revoked_at": key.revoked_at,
    }


def _identifier(key_id: str | None) -> str:
    if type(key_id) is not str or _KEY_ID.fullmatch(key_id) is None:
        raise KeyOperatorError("invalid_key_id")
    return key_id


def _expiry(ttl_seconds: int) -> None:
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise KeyOperatorError("invalid_key_expiry")


def _duration(seconds: float) -> str:
    """Compact human-readable duration, rounded down: 30d 0h, 3h 5m, 12m, 40s."""
    days, rest = divmod(max(int(seconds), 0), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, remainder = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{remainder}s"


def _inherited_lifetime(key: KeyMetadata) -> int:
    """The predecessor's originally granted lifetime, within the allowed range."""
    return min(max(round(key.expires_at - key.issued_at), 1), MAX_TTL_SECONDS)


def _description(
    label: str,
    origin: Origin,
    control: bool,
    ttl_seconds: int,
    to_env: bool = False,
    lifetime_note: str = "",
) -> str:
    return (
        f"Client: {label}\nOrigin: {origin.value}\n"
        f"Scopes: {'read, control' if control else 'read'}\n"
        f"Expires after: {ttl_seconds} seconds ({_duration(ttl_seconds)}){lifetime_note}\n"
        f"{_ENV_WARNING if to_env else _WARNING}"
    )


def _revoke_or_flag(registry: KeyRegistry, key_id: str, revoked_code: str) -> KeyOperatorError:
    try:
        registry.revoke(key_id)
    except KeyRegistryError:
        return KeyOperatorError("key_env_write_failed_check_registry_and_revoke")
    return KeyOperatorError(revoked_code)


def _write_env(
    issued: IssuedKey,
    prepared: PreparedEnvWrite,
    registry: KeyRegistry,
    output_stream: TextIO,
    *,
    rotated: bool,
) -> dict[str, object]:
    """Deliver an already committed key into the prepared env file.

    On failure an issued key is revoked. A rotation has already revoked its
    predecessor, so it falls back to the one-time terminal display; if that
    also fails the new key is revoked and a fresh issuance is required.
    """
    key_id = issued.metadata.key_id
    try:
        prepared.commit(issued.token)
    except EnvFileWriteError as failure:
        # Rare Windows case: the original was renamed to the hidden backup and
        # the new file could not take its name, so the env file is missing.
        kept = str(failure).endswith("_original_kept_as_backup")
        if not rotated:
            code = "key_env_write_failed_new_key_revoked"
            raise _revoke_or_flag(
                registry, key_id, code + ("_original_kept_as_backup" if kept else "")
            ) from None
        try:
            _write(
                output_stream,
                "Writing the selected env file failed: it was NOT updated, or the update "
                "could not be verified. The replacement key is shown once below instead; "
                "put it into the env file's CARDPUTER_CLIENT_KEY line yourself.\n"
                + (
                    "The env file is missing; its previous content was kept beside it as "
                    "a hidden .bak file. Rename that back first.\n"
                    if kept
                    else ""
                ),
            )
            shown = _show_once(issued, registry, output_stream)
        except KeyOperatorError:
            # _show_once revokes on its own failure; revoking again is idempotent.
            raise _revoke_or_flag(
                registry, key_id, "key_env_write_and_display_failed_new_key_revoked_run_keys_issue"
            ) from None
        return {**shown, "env_file_updated": False}
    except (KeyboardInterrupt, SystemExit) as failure:
        # The swap outcome is uncertain; invalidate the new key, then propagate.
        error = _revoke_or_flag(registry, key_id, "key_env_write_interrupted_new_key_revoked")
        if error.args[0] != "key_env_write_interrupted_new_key_revoked":
            raise error from None
        if isinstance(failure, KeyboardInterrupt):
            raise KeyboardInterrupt from None
        raise SystemExit(failure.code if type(failure.code) is int else 1) from None
    return {"key": _metadata(issued.metadata), "token_shown": False, "env_file_updated": True}


def _check_env(write_env: Path | None, expected_key_id: str | None) -> None:
    if write_env is None:
        return
    try:
        check_env_file(write_env, expected_key_id=expected_key_id)
    except EnvFileWriteError as error:
        # EnvFileWriteError messages are fixed codes without paths or values.
        raise KeyOperatorError(str(error)) from None


def _prepare(write_env: Path | None, expected_key_id: str | None) -> PreparedEnvWrite | None:
    if write_env is None:
        return None
    try:
        return prepare_env_write(write_env, expected_key_id=expected_key_id)
    except EnvFileWriteError as error:
        raise KeyOperatorError(str(error)) from None


def _show_once(
    issued: IssuedKey, registry: KeyRegistry, output_stream: TextIO
) -> dict[str, object]:
    try:
        _write(output_stream, "API key (shown once): " + issued.token + "\n")
    except (KeyOperatorError, KeyboardInterrupt, SystemExit) as failure:
        # Never redisplay on an uncertain write; invalidate the newly issued key.
        try:
            registry.revoke(issued.metadata.key_id)
        except KeyRegistryError:
            raise KeyOperatorError("key_display_failed_check_registry_and_revoke") from None
        # A stream can raise a control exception during write or flush. Revoke
        # first, then propagate control flow without any possibly secret message.
        if isinstance(failure, KeyboardInterrupt):
            raise KeyboardInterrupt from None
        if isinstance(failure, SystemExit):
            raise SystemExit(failure.code if type(failure.code) is int else 1) from None
        raise KeyOperatorError("key_display_failed_new_key_revoked") from None
    return {"key": _metadata(issued.metadata), "token_shown": True}


def _read_secret(callback: Callable[[str], str] | None, output_stream: TextIO) -> str:
    try:
        if callback is not None:
            return callback("API key (hidden): ")
        # getpass may otherwise silently fall back to an echoing stdin read.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass("API key (hidden): ", stream=output_stream)
    except (EOFError, KeyboardInterrupt, OSError, ValueError, getpass.GetPassWarning):
        raise KeyOperatorError("key_hidden_input_unavailable") from None


def run(
    command: KeyCommand,
    path: Path | None = None,
    *,
    label: str | None = None,
    origin: Origin = Origin.REMOTE,
    control: bool = False,
    ttl_seconds: int | None = None,
    key_id: str | None = None,
    confirm: Callable[[str], str] | None = None,
    secret_input: Callable[[str], str] | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    registry_factory: RegistryFactory = KeyRegistry,
    write_env: Path | None = None,
) -> dict[str, object]:
    """Perform one local command, returning only JSON-compatible safe metadata.

    The parent directory must exist. A new registry may be created only after
    confirmed issue; all other commands require an existing registry. No command
    supports a noninteractive bypass for confirmation or secret input/output.
    Without ``ttl_seconds``, issue uses the one-hour default and rotate keeps
    the current key's originally granted lifetime (capped at the maximum).
    ``write_env`` (issue/rotate only) names an existing private env file that
    receives the new key instead of the terminal. It is fully validated and a
    temporary file is prepared before the registry changes.
    """
    automatic_path = path is None
    path = default_key_registry_path() if path is None else path
    if automatic_path:
        original_factory = registry_factory

        def prepared_registry(path: Path, *, create: bool = True) -> KeyRegistry:
            if create:
                prepare_default_key_directory(path)
            return original_factory(path, create=create)

        registry_factory = prepared_registry
    incoming = sys.stdin if input_stream is None else input_stream
    outgoing = sys.stdout if output_stream is None else output_stream
    if command not in ("issue", "list", "revoke", "rotate", "verify"):
        raise KeyOperatorError("invalid_key_command")
    if write_env is not None and command not in ("issue", "rotate"):
        raise KeyOperatorError("key_write_env_not_supported")
    if command != "list":
        _tty(incoming, outgoing)
    if command in ("issue", "rotate") and ttl_seconds is not None:
        _expiry(ttl_seconds)
    if command == "issue":
        if type(label) is not str or _LABEL.fullmatch(label) is None or label != label.strip():
            raise KeyOperatorError("invalid_key_label")
        if not isinstance(origin, Origin):
            raise KeyOperatorError("invalid_key_origin")
        if type(control) is not bool:
            raise KeyOperatorError("invalid_key_scopes")
        _check_env(write_env, None)
        lifetime = DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        _confirm(
            "ISSUE",
            _description(label, origin, control, lifetime, write_env is not None),
            confirm,
            incoming,
            outgoing,
        )
        # Validate again and prepare the temporary file before any DB or key exists.
        prepared = _prepare(write_env, None)
        try:
            # This is deliberately after confirmation: denial creates no DB or key.
            registry = registry_factory(path, create=True)
            try:
                issued = registry.issue(
                    label,
                    origin,
                    scopes={"read", "control"} if control else {"read"},
                    ttl_seconds=lifetime,
                )
                if prepared is None:
                    return _show_once(issued, registry, outgoing)
                return _write_env(issued, prepared, registry, outgoing, rotated=False)
            finally:
                registry.close()
        finally:
            if prepared is not None:
                prepared.discard()
    identifier = _identifier(key_id) if command in ("revoke", "rotate") else None
    registry = registry_factory(path, create=False)
    try:
        if command == "list":
            return {"keys": [_metadata(key) for key in registry.list_keys()]}
        if command == "verify":
            token = _read_secret(secret_input, outgoing)
            principal = registry.authenticate("Bearer " + token)
            if principal is None:
                return {"valid": False}
            return {"valid": True, "principal": _principal(principal)}
        # Both remaining operations require a validated public identifier.
        assert identifier is not None
        metadata = next((key for key in registry.list_keys() if key.key_id == identifier), None)
        if metadata is None:
            raise KeyOperatorError("key_not_found")
        if command == "revoke":
            _confirm(
                f"REVOKE {identifier}",
                f"Revoke key {identifier} for {metadata.label} ({metadata.origin.value}).\n"
                "Further requests using this key will be rejected.",
                confirm,
                incoming,
                outgoing,
            )
            return {"key_id": identifier, "revoked": registry.revoke(identifier)}
        to_env = write_env is not None
        if ttl_seconds is None:
            lifetime = _inherited_lifetime(metadata)
            note = " - the current key's original lifetime"
        else:
            lifetime, note = ttl_seconds, " - as requested"
        # A key line holding a different valid key is refused: wrong file.
        _check_env(write_env, identifier)
        _confirm(
            f"ROTATE {identifier}",
            _description(
                metadata.label,
                metadata.origin,
                "control" in metadata.scopes,
                lifetime,
                to_env,
                note,
            )
            + f"\nKey {identifier} will be revoked immediately when replacement succeeds.\n"
            + (
                "If writing the env file fails, the replacement is shown once here instead.\n"
                if to_env
                else ""
            )
            + "If displaying the replacement fails, a new issuance may be needed.",
            confirm,
            incoming,
            outgoing,
        )
        prepared = _prepare(write_env, identifier)
        try:
            rotated = registry.rotate(identifier, ttl_seconds=lifetime)
            if prepared is None:
                return _show_once(rotated, registry, outgoing)
            return _write_env(rotated, prepared, registry, outgoing, rotated=True)
        finally:
            if prepared is not None:
                prepared.discard()
    finally:
        registry.close()
