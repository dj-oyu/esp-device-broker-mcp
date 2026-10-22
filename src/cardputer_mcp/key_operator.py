"""Local terminal key management; deliberately never exposed as MCP tools.

Only issue/rotate reveal a token, once, directly on the confirmed operator's
terminal. Their return values are metadata-only and safe for ordinary CLI JSON.
No secret is accepted in an argument or copied to a file/clipboard/environment.
Tests must inject synthetic registry randomness, terminal streams and inputs.
"""

import getpass
import re
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, TextIO

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
    validate_default_key_path,
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


def _description(label: str, origin: Origin, control: bool, ttl_seconds: int) -> str:
    return (
        f"Client: {label}\nOrigin: {origin.value}\n"
        f"Scopes: {'read, control' if control else 'read'}\n"
        f"Expires after: {ttl_seconds} seconds\n{_WARNING}"
    )


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
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    key_id: str | None = None,
    confirm: Callable[[str], str] | None = None,
    secret_input: Callable[[str], str] | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    registry_factory: RegistryFactory = KeyRegistry,
) -> dict[str, object]:
    """Perform one local command, returning only JSON-compatible safe metadata.

    The parent directory must exist. A new registry may be created only after
    confirmed issue; all other commands require an existing registry. No command
    supports a noninteractive bypass for confirmation or secret input/output.
    """
    automatic_path = path is None
    path = default_key_registry_path() if path is None else path
    if automatic_path:
        original_factory = registry_factory

        def checked_registry(path: Path, *, create: bool = True) -> KeyRegistry:
            if create:
                prepare_default_key_directory(path)
            else:
                validate_default_key_path(path)
            registry = original_factory(path, create=create)
            try:
                validate_default_key_path(path)
            except Exception:
                registry.close()
                raise
            return registry

        registry_factory = checked_registry
    incoming = sys.stdin if input_stream is None else input_stream
    outgoing = sys.stdout if output_stream is None else output_stream
    if command not in ("issue", "list", "revoke", "rotate", "verify"):
        raise KeyOperatorError("invalid_key_command")
    if command != "list":
        _tty(incoming, outgoing)
    if command in ("issue", "rotate"):
        _expiry(ttl_seconds)
    if command == "issue":
        if type(label) is not str or _LABEL.fullmatch(label) is None or label != label.strip():
            raise KeyOperatorError("invalid_key_label")
        if not isinstance(origin, Origin):
            raise KeyOperatorError("invalid_key_origin")
        if type(control) is not bool:
            raise KeyOperatorError("invalid_key_scopes")
        _confirm(
            "ISSUE",
            _description(label, origin, control, ttl_seconds),
            confirm,
            incoming,
            outgoing,
        )
        # This is deliberately after confirmation: denial creates no DB or key.
        registry = registry_factory(path, create=True)
        try:
            issued = registry.issue(
                label,
                origin,
                scopes={"read", "control"} if control else {"read"},
                ttl_seconds=ttl_seconds,
            )
            return _show_once(issued, registry, outgoing)
        finally:
            registry.close()
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
        _confirm(
            f"ROTATE {identifier}",
            _description(metadata.label, metadata.origin, "control" in metadata.scopes, ttl_seconds)
            + f"\nKey {identifier} will be revoked immediately when replacement succeeds.\n"
            "If displaying the replacement fails, a new issuance may be needed.",
            confirm,
            incoming,
            outgoing,
        )
        return _show_once(registry.rotate(identifier, ttl_seconds=ttl_seconds), registry, outgoing)
    finally:
        registry.close()
