"""Opt-in Windows Tunnel launcher support. No cloud calls during preparation.

The native client is supplied and verified by the local operator. This module
never installs it, creates tunnels, or provisions external credentials.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx2
from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field

from .broker_link import DEFAULT_BROKER_URL, canonical_broker_port
from .client import SURFACE_TOOLS, call, connect, validate_url
from .config import Settings, TunnelMetadata, load_settings

RELEASE = "0.0.15"
ARCHIVE_SHA256 = "3b53133a1e24d43f63088d843860cb1701a4c3ed6390de2e19f69089e43bddc1"
SAFE_ENV = frozenset({"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "COMSPEC"})


class TunnelError(ValueError):
    """Fixed diagnostic codes only; never include native output or supplied values."""


class TunnelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    client_exe: Path
    client_archive: Path
    tunnel_id: str = Field(pattern=r"^tunnel_[a-f0-9]{32}$")
    broker_config: Path = Path("examples/tunnel-mock.toml")
    health_port: int = Field(default=8766, strict=True, ge=1024, le=65535)
    ingress_enabled: bool = Field(default=False, strict=True)


_METADATA_FIELDS = ("tunnel_id", "client_exe", "client_archive", "health_port", "ingress_enabled")


def _project_path(path: Path, project: Path) -> Path:
    return (path if path.is_absolute() else project / path).resolve()


def _read_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _metadata_values(metadata: TunnelMetadata, project: Path) -> dict[str, Any]:
    values = metadata.model_dump(exclude_none=True, exclude_unset=True)
    # An explicitly blank ID is an unconfigured template, never a usable ID.
    if not values.get("tunnel_id"):
        values.pop("tunnel_id", None)
    for key in ("client_exe", "client_archive"):
        if key in values:
            values[key] = _project_path(values[key], project)
    return values


def _legacy_values(path: Path, project: Path) -> tuple[Path, dict[str, Any]]:
    legacy = TunnelSettings.model_validate(_read_config(path))
    metadata = TunnelMetadata.model_validate(
        {
            key: getattr(legacy, key)
            for key in _METADATA_FIELDS
            if key != "ingress_enabled" or key in legacy.model_fields_set
        }
    )
    return _project_path(legacy.broker_config, project), _metadata_values(metadata, project)


def _resolve_metadata(
    broker_config: Path,
    project: Path,
    broker: Settings,
    legacy_path: Path | None,
) -> TunnelSettings | None:
    values = {} if broker.tunnel is None else _metadata_values(broker.tunnel, project)
    # Only the canonical broker TOML can enable its HTTP route. A legacy file
    # cannot enable a route the running broker would never load.
    values["ingress_enabled"] = broker.tunnel.ingress_enabled if broker.tunnel else False
    legacy_path = _project_path(legacy_path or Path("tunnel.local.toml"), project)
    if legacy_path != broker_config and legacy_path.exists():
        legacy_broker, legacy = _legacy_values(legacy_path, project)
        if legacy_broker == broker_config:
            if any(key in values and values[key] != value for key, value in legacy.items()):
                raise TunnelError("tunnel_config_conflict")
            values = {**legacy, **values}
    if not all(key in values for key in ("tunnel_id", "client_exe", "client_archive")):
        return None
    return TunnelSettings.model_validate({**values, "broker_config": broker_config})


def load_gui_config(
    broker_config: Path, project: Path, legacy_path: Path | None = None
) -> TunnelSettings | None:
    """Resolve one broker's metadata without connecting, writing, or migrating.

    Compatible legacy metadata may fill omitted canonical fields. Conflicting
    explicit values for the same broker always fail closed, including IDs.
    """
    try:
        project = project.resolve()
        broker_config = _project_path(broker_config, project)
        broker = Settings.model_validate(_read_config(broker_config))
        return _resolve_metadata(broker_config, project, broker, legacy_path)
    except TunnelError:
        raise
    except Exception:
        raise TunnelError("invalid_private_tunnel_config") from None


def load_config(path: Path, project: Path) -> TunnelSettings:
    """Accept canonical broker TOML or a compatible historical flat config."""
    try:
        project = project.resolve()
        path = _project_path(path, project)
        raw = _read_config(path)
        legacy_fields = {*_METADATA_FIELDS, "broker_config"}
        if "tunnel" not in raw and legacy_fields.intersection(raw):
            legacy = TunnelSettings.model_validate(raw)
            broker_path = _project_path(legacy.broker_config, project)
            if broker_path.exists():
                broker = Settings.model_validate(_read_config(broker_path))
                result = _resolve_metadata(broker_path, project, broker, path)
            else:
                # Preserve old preparation-only path resolution. Preflight still
                # requires the actual broker config before any client is started.
                result = legacy.model_copy(
                    update={
                        key: _project_path(getattr(legacy, key), project)
                        for key in ("client_exe", "client_archive", "broker_config")
                    }
                )
        else:
            broker = Settings.model_validate(raw)
            result = _resolve_metadata(path, project, broker, None)
        if result is None:
            raise TunnelError("tunnel_metadata_incomplete")
        return result
    except TunnelError:
        raise
    except Exception:
        raise TunnelError("invalid_private_tunnel_config") from None


def _toml_scalar(value: Any) -> str:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise TunnelError("invalid_private_tunnel_config")
        # JSON's ASCII-only non-BMP encoding uses surrogate pairs, which TOML
        # correctly rejects. Emit Unicode scalars directly and escape DEL, the
        # additional TOML-forbidden control character JSON leaves literal.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    raise TunnelError("invalid_private_tunnel_config")


def _toml_document(values: dict[str, Any]) -> str:
    """Serialize validated, scalar-only settings while preserving their values."""
    lines: list[str] = []

    def table(values: dict[str, Any], prefix: tuple[str, ...]) -> None:
        if prefix:
            lines.extend(("", "[" + ".".join(json.dumps(key) for key in prefix) + "]"))
        for key, value in values.items():
            if not isinstance(value, dict):
                lines.append(f"{json.dumps(key)} = {_toml_scalar(value)}")
        for key, value in values.items():
            if isinstance(value, dict):
                table(value, (*prefix, key))

    table(values, ())
    return "\n".join(lines) + "\n"


def _reject_symlink_path(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise TunnelError("unsafe_tunnel_config_path")
    if not path.is_file():
        raise TunnelError("unsafe_tunnel_config_path")


def save_gui_config(
    broker_config: Path, project: Path, metadata: TunnelSettings | dict[str, Any]
) -> None:
    """Explicit operator save only; rewrite comments/formatting, never secrets.

    Preserve existing non-tunnel TOML values, validate the complete document,
    refuse legacy conflicts, and atomically replace only the selected file.
    The GUI must require a disconnected manager before calling this function.
    """
    temporary: Path | None = None
    try:
        project = project.absolute()
        target = broker_config if broker_config.is_absolute() else project / broker_config
        _reject_symlink_path(target)
        target = target.resolve()
        original = target.read_bytes()
        raw = tomllib.loads(original.decode("utf-8"))
        Settings.model_validate(raw)  # Unknown existing fields are never erased.
        if isinstance(metadata, TunnelSettings):
            if _project_path(metadata.broker_config, project) != target:
                raise TunnelError("tunnel_config_conflict")
            metadata = {
                key: getattr(metadata, key)
                for key in _METADATA_FIELDS
                if key != "ingress_enabled" or key in metadata.model_fields_set
            }
        # The GUI only edits launcher metadata. Preserve the manually selected
        # broker mode when that read-only field was not supplied by its caller.
        if "ingress_enabled" not in metadata and "ingress_enabled" in raw.get("tunnel", {}):
            metadata = {**metadata, "ingress_enabled": raw["tunnel"]["ingress_enabled"]}
        selected = TunnelMetadata.model_validate(metadata)
        raw["tunnel"] = selected.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        # Persist portable TOML path spellings, independent of the saving OS.
        for key in ("client_exe", "client_archive"):
            value = getattr(selected, key)
            if value is not None and key in raw["tunnel"]:
                raw["tunnel"][key] = value.as_posix()
        broker = Settings.model_validate(raw)
        _resolve_metadata(target, project, broker, None)  # Do not strand conflicting metadata.
        serialized = _toml_document(raw)
        round_trip = tomllib.loads(serialized)
        Settings.model_validate(round_trip)
        if round_trip != raw:
            raise TunnelError("invalid_private_tunnel_config")
        document = serialized.encode("utf-8")
        # Exclusive creation in the same directory avoids unsafe fixed temp names
        # and makes replacement atomic without assuming the directory is private.
        with tempfile.NamedTemporaryFile(
            prefix=".tunnel-config-", dir=target.parent, delete=False
        ) as f:
            temporary = Path(f.name)
            f.write(document)
            f.flush()
            os.fsync(f.fileno())
        _reject_symlink_path(target)
        if target.read_bytes() != original:
            raise TunnelError("tunnel_config_changed")
        os.replace(temporary, target)
        temporary = None
    except TunnelError:
        raise
    except Exception:
        raise TunnelError("invalid_private_tunnel_config") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Never expose paths through cleanup errors.


def _digest(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
        raise TunnelError("client_release_verification_failed")
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_release(settings: TunnelSettings) -> None:
    """Pin official AMD64 ZIP, then compare supplied EXE with its archive member."""
    try:
        if _digest(settings.client_archive) != ARCHIVE_SHA256:
            raise ValueError
        if settings.client_exe.name.lower() != "tunnel-client.exe":
            raise ValueError
        with zipfile.ZipFile(settings.client_archive) as archive:
            for item in archive.infolist():
                kind = stat.S_IFMT(item.external_attr >> 16)
                if (
                    "/" in item.filename
                    or "\\" in item.filename
                    or item.filename in (".", "..")
                    or kind not in (0, stat.S_IFREG)
                ):
                    raise ValueError
            names = [name for name in archive.namelist() if name == "tunnel-client.exe"]
            if len(names) != 1 or archive.getinfo(names[0]).file_size > 100 * 1024 * 1024:
                raise ValueError
            with archive.open(names[0]) as member:
                digest = hashlib.sha256()
                while chunk := member.read(65536):
                    digest.update(chunk)
                expected = digest.hexdigest()
        if _digest(settings.client_exe) != expected:
            raise ValueError
    except Exception:
        raise TunnelError("client_release_verification_failed") from None


def lock_path() -> Path:
    # File locking, never PID inspection or stale-process killing.
    return Path(tempfile.gettempdir()) / "cardputer-tunnel-client.lockfile"


def check_listener(port: int) -> None:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise TunnelError("tunnel_health_listener_exists")


def preflight(settings: TunnelSettings, *, enable_serial: bool = False) -> dict[str, Any]:
    if settings.tunnel_id == "tunnel_" + "0" * 32:
        raise TunnelError("tunnel_id_placeholder")
    broker = load_settings(settings.broker_config)
    if settings.ingress_enabled != (broker.tunnel.ingress_enabled if broker.tunnel else False):
        raise TunnelError("tunnel_config_conflict")
    if broker.backend == "serial" and not enable_serial:
        raise TunnelError("serial_tunnel_requires_explicit_enable_serial")
    if settings.health_port == broker.port:
        raise TunnelError("health_port_conflicts_with_broker")
    verify_release(settings)
    check_listener(settings.health_port)
    try:
        with FileLock(str(lock_path()), timeout=0):
            pass
    except Timeout:
        raise TunnelError("tunnel_launcher_already_running") from None
    return {
        "ok": True,
        "broker_config": str(settings.broker_config),
        "backend": broker.backend,
        "url": f"http://127.0.0.1:{broker.port}/mcp",
    }


def runtime_api_key(supplied: str, environment: Mapping[str, str]) -> str:
    """Resolve only on explicit connection; never persist or echo credentials."""
    return (
        supplied
        or environment.get("CARDPUTER_TUNNEL_API_KEY", "")
        or environment.get("CONTROL_PLANE_API_KEY", "")
    )


def _consume_runtime_api_key() -> str:
    key = runtime_api_key("", os.environ)
    os.environ.pop("CARDPUTER_TUNNEL_API_KEY", None)
    os.environ.pop("CONTROL_PLANE_API_KEY", None)
    return key


def child_environment(source: dict[str, str], api_key: str, bearer: str) -> dict[str, str]:
    api_key = runtime_api_key(api_key, source)
    if not api_key or len(api_key) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
        raise TunnelError("invalid_runtime_key")
    if re.fullmatch(r"Bearer (?:[a-f0-9]{64}|cpk_[a-f0-9]{32}\.[a-f0-9]{64})", bearer) is None:
        raise TunnelError("invalid_mcp_bearer")
    env = {key: value for key, value in source.items() if key.upper() in SAFE_ENV}
    env["CARDPUTER_TUNNEL_API_KEY"] = api_key
    env["CARDPUTER_TUNNEL_BEARER"] = bearer
    return env


def _local_broker_url(url: str) -> str:
    try:
        validate_url(url)
        if canonical_broker_port(url) is None:
            raise ValueError
    except ValueError:
        raise TunnelError("invalid_local_broker_url") from None
    return url


def check_ingress_mode(identity: dict[str, Any], ingress_enabled: bool) -> None:
    """A stale or older broker must never silently receive a different route."""
    if (
        type(ingress_enabled) is not bool
        or identity.get("tunnel_ingress_enabled") is not ingress_enabled
    ):
        raise TunnelError("tunnel_broker_ingress_mismatch")


def client_arguments(settings: TunnelSettings, url: str) -> list[str]:
    url = _local_broker_url(url)
    if type(settings.ingress_enabled) is not bool:
        raise TunnelError("invalid_private_tunnel_config")
    if settings.ingress_enabled:
        url = url.removesuffix("/mcp") + "/tunnel/mcp"
    return [
        str(settings.client_exe),
        "run",
        "--control-plane.base-url=https://api.openai.com",
        f"--control-plane.tunnel-id={settings.tunnel_id}",
        "--control-plane.api-key=env:CARDPUTER_TUNNEL_API_KEY",
        "--control-plane.poll-channel=main",
        f"--mcp.server-url={url}",
        "--mcp.extra-headers=Authorization: env:CARDPUTER_TUNNEL_BEARER",
        "--mcp.discovery-extra-headers=Authorization: env:CARDPUTER_TUNNEL_BEARER",
        "--mcp.max-concurrent-requests=4",
        "--control-plane.max-inflight=4",
        f"--health.listen-addr=127.0.0.1:{settings.health_port}",
        "--log.level=warn",
        "--log.http-raw-unsafe=false",
        "--allow-remote-ui=false",
        "--open-web-ui=false",
        "--harpoon.capture-payloads=false",
        "--harpoon.allow-plaintext-http=false",
        "--cloudflared.managed=false",
        "--health.show-details=false",
        "--log.format=json",
        "--mcp.startup-wait-timeout=30s",
    ]


async def check_broker(url: str, bearer: str, *, ingress_enabled: bool = False) -> dict[str, Any]:
    url = _local_broker_url(url)
    async with asyncio.timeout(5):
        async with connect(url, bearer.removeprefix("Bearer ")) as client:
            identity = await call(client, "status", {"view": "identity"})
            if identity.get("ok") is not True or identity.get("authenticated") is not True:
                raise TunnelError("tunnel_broker_auth_failed")
            if identity.get("origin") != "remote_mcp":
                raise TunnelError("tunnel_broker_origin_rejected")
            scopes = identity.get("scopes")
            if not isinstance(scopes, list) or "read" not in scopes:
                raise TunnelError("tunnel_broker_scope_rejected")
            check_ingress_mode(identity, ingress_enabled)
            status = await call(client, "status", {"view": "device"})
            tools = await client.list_tools()
    if not status.get("ok") or status.get("active_job") or status.get("owner"):
        raise TunnelError("broker_not_idle")
    if status.get("hardware_connected") or status.get("ownership_uncertain"):
        raise TunnelError("broker_hardware_not_closed")
    if not set(SURFACE_TOOLS) <= {item.name for item in tools.tools}:
        raise TunnelError("mcp_discovery_failed")
    return {
        "ok": True,
        "backend": status["backend"],
        "needs_review": bool(status.get("needs_review")),
        "cloud_verified": False,
    }


def supervise(settings: TunnelSettings, url: str, env: dict[str, str]) -> int:
    """Suppress all native output. No key/body/path can escape via child errors.

    Only this owned client gets a stop signal when its operator closes this
    foreground console. The independent broker and its jobs are never stopped.
    """
    with FileLock(str(lock_path()), timeout=0):
        check_listener(settings.health_port)
        child = subprocess.Popen(
            client_arguments(settings, url),
            env=env,
            cwd=settings.client_exe.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        env.clear()
        deadline = time.monotonic() + 45
        ready = False
        try:
            with httpx2.Client(trust_env=False, timeout=1) as http:
                while child.poll() is None:
                    if not ready and time.monotonic() < deadline:
                        try:
                            response = http.get(f"http://127.0.0.1:{settings.health_port}/readyz")
                            if response.status_code == 200:
                                print(
                                    "tunnel_local_readiness_only; "
                                    "cloud polling and app discovery are UNVERIFIED",
                                    flush=True,
                                )
                                ready = True
                        except httpx2.HTTPError:
                            pass
                    elif not ready:
                        print(
                            "tunnel_readiness_not_confirmed; "
                            "inspect account setup; no broker was stopped",
                            flush=True,
                        )
                        ready = True  # One warning; keep the owned process visible and alive.
                    time.sleep(0.2)
        except KeyboardInterrupt:
            return 0
        finally:
            # Reap only the subprocess we just created; no PID lookup or broker termination.
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        print("tunnel_client_exited; native output suppressed to protect credentials", flush=True)
        return 1


def launch_console(settings_path: Path, project: Path, shell: Path, *, enable_serial: bool) -> None:
    """Windows-only fixed-command bootstrap; allocate a genuinely separate console."""
    if os.name != "nt" or shell.name.lower() not in ("powershell.exe", "pwsh.exe"):
        raise TunnelError("unsupported_console_shell")
    if not shell.is_absolute() or not shell.is_file():
        raise TunnelError("unsupported_console_shell")

    def quote(value: Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    command = (
        "$ErrorActionPreference = 'Stop'; try { Set-Location -LiteralPath "
        + quote(project)
        + "; & "
        + quote(Path(sys.executable))
        + " -m cardputer_mcp.tunnel run --project "
        + quote(project)
        + " --config "
        + quote(settings_path)
    )
    if enable_serial:
        command += " --enable-serial"
    command += (
        " } finally { [Environment]::SetEnvironmentVariable("
        "'CARDPUTER_TUNNEL_API_KEY', $null, 'Process');"
        " [Environment]::SetEnvironmentVariable('CARDPUTER_TUNNEL_BEARER', $null, 'Process') };"
        " Write-Host 'Tunnel supervisor exited; the independent broker was not stopped.'"
    )
    import base64

    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    env = child_environment(
        dict(os.environ),
        _consume_runtime_api_key(),
        os.environ.pop("CARDPUTER_TUNNEL_BEARER", ""),
    )
    subprocess.Popen(
        [str(shell), "-NoLogo", "-NoProfile", "-NoExit", "-EncodedCommand", encoded],
        cwd=project,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 16),
    )
    env.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "check", "run", "launch-console"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--shell", type=Path)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--enable-serial", action="store_true")
    parser.add_argument("--url", default=DEFAULT_BROKER_URL)
    args = parser.parse_args()
    try:
        if args.command == "check":
            token = os.environ.get("CARDPUTER_REMOTE_TOKEN", "")
            ingress_enabled = False
            if args.config is not None:
                broker = load_settings(_project_path(args.config, args.project))
                ingress_enabled = broker.tunnel.ingress_enabled if broker.tunnel else False
                if args.url != f"http://127.0.0.1:{broker.port}/mcp":
                    raise TunnelError("tunnel_broker_url_mismatch")
            print(
                json.dumps(
                    asyncio.run(
                        check_broker(args.url, "Bearer " + token, ingress_enabled=ingress_enabled)
                    )
                )
            )
            return
        if args.config is None:
            raise TunnelError("private_config_required")
        if args.command == "launch-console":
            if args.shell is None:
                raise TunnelError("console_shell_required")
            launch_console(args.config, args.project, args.shell, enable_serial=args.enable_serial)
            return
        settings = load_config(args.config, args.project)
        result = preflight(settings, enable_serial=args.enable_serial)
        if args.command == "preflight":
            print(json.dumps(result))
            return
        api_key = _consume_runtime_api_key()
        bearer = os.environ.pop("CARDPUTER_TUNNEL_BEARER", "")
        env = child_environment(dict(os.environ), api_key, bearer)
        asyncio.run(check_broker(result["url"], bearer, ingress_enabled=settings.ingress_enabled))
        raise SystemExit(supervise(settings, result["url"], env))
    except TunnelError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"ok": False, "error": "tunnel_preparation_failed"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
