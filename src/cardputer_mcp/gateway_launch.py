"""``cardputer gateway``: run the Claude gateway and, only when configured, its Funnel.

Startup order: validate the broker TOML's ``[gateway]`` section, the broker key
file and the Google secret file; check Tailscale (this machine's name, the Funnel
permission, no existing HTTPS 443 serve/Funnel config); listen on 127.0.0.1;
then start ``tailscale funnel`` in the foreground as an owned Job Object child
and confirm that Funnel lists our target. Shutdown closes Funnel first, then the
listener. If the Funnel process exits on its own after it was confirmed open (a Tailscale
update restarts tailscaled), the same command is started again, up to 8 times with growing
waits, re-running the startup Tailscale checks each time; when that fails the gateway stops.

Nothing here starts or stops the broker, the OpenAI Tunnel or any device work.
Output is fixed event names; no secrets, tokens or file contents are printed.
"""

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx2
import uvicorn
from filelock import FileLock, Timeout

from . import client_environment, windows_process
from .broker_link import canonical_broker_port
from .config import GatewaySettings, Settings, load_settings
from .gateway_oauth import MCP_PATH, canonical_resource, create_gateway_app
from .gateway_relay import BrokerRelay
from .gateway_store import GatewayStore, GatewayStoreError, default_store_path

_FUNNEL_ENV_KEYS = ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "COMSPEC")
_FUNNEL_CONFIRM_SECONDS = 15.0
_TAILSCALE_SECONDS = 10.0
# Tailscale updates replace tailscaled (ending every foreground funnel/serve child) and can leave it
# unavailable for minutes: wait 5, 10, 20, 40 s, then 60 s, at most 8 times (about 8-9 minutes).
_RESTART_DELAYS: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0, 60.0)
_RESTART_STABLE_SECONDS = 300.0


class GatewayLaunchError(Exception):
    """A fixed public error code."""


@dataclass(frozen=True)
class Prepared:
    settings: Settings
    gateway: GatewaySettings
    broker: client_environment.ClientEnvironment = field(repr=False)
    google_secret: str = field(repr=False)


def prepare(config: Path) -> Prepared:
    try:
        settings = load_settings(config)
    except (OSError, ValueError):
        raise GatewayLaunchError("gateway_config_invalid") from None
    gateway = settings.gateway
    if gateway is None:
        raise GatewayLaunchError("gateway_config_missing")
    if settings.tunnel is None or not settings.tunnel.ingress_enabled:
        raise GatewayLaunchError("gateway_requires_tunnel_ingress")
    if gateway.port in (settings.port, settings.tunnel.health_port):
        raise GatewayLaunchError("gateway_port_conflict")
    try:
        broker = client_environment.load_client_environment(gateway.broker_env_file)
    except client_environment.ClientEnvironmentError:
        raise GatewayLaunchError("gateway_broker_env_invalid") from None
    if canonical_broker_port(broker.url) != settings.port:
        raise GatewayLaunchError("gateway_broker_port_mismatch")
    try:
        secret = client_environment.load_gateway_secret(gateway.google_client_secret_file)
    except client_environment.ClientEnvironmentError:
        raise GatewayLaunchError("gateway_google_secret_invalid") from None
    return Prepared(settings, gateway, broker, secret)


# Tailscale ------------------------------------------------------------------------

Runner = Callable[[list[str]], str]


def run_tailscale(args: list[str]) -> str:
    """Run one read-only Tailscale CLI query; no shell, no inherited console."""
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=_TAILSCALE_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        raise GatewayLaunchError("tailscale_unavailable") from None
    if completed.returncode != 0:
        raise GatewayLaunchError("tailscale_query_failed")
    return completed.stdout.decode("utf-8", errors="replace")


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        raise GatewayLaunchError("tailscale_output_invalid") from None


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _host(gateway: GatewaySettings) -> str:
    return urlparse(gateway.public_url).hostname or ""


def funnel_target(gateway: GatewaySettings) -> str:
    return f"http://127.0.0.1:{gateway.port}"


def check_tailscale(gateway: GatewaySettings, run: Runner = run_tailscale) -> None:
    exe = gateway.tailscale_exe
    if exe.name.casefold() not in windows_process.TAILSCALE_EXECUTABLES or not exe.is_file():
        raise GatewayLaunchError("tailscale_executable_missing")
    status = _json(run([str(exe), "status", "--json"]))
    me = status.get("Self") if isinstance(status, dict) else None
    if not isinstance(me, dict) or status.get("BackendState") != "Running":
        raise GatewayLaunchError("tailscale_not_running")
    name = str(me.get("DNSName", "")).rstrip(".").lower()
    if name != _host(gateway):
        raise GatewayLaunchError("gateway_public_url_not_this_machine")
    capabilities = [str(item) for item in me.get("Capabilities") or []]
    capabilities += [str(key) for key in (me.get("CapMap") or {})]
    if "funnel" not in capabilities and not any(
        item.startswith("https://tailscale.com/cap/funnel") for item in capabilities
    ):
        raise GatewayLaunchError("tailscale_funnel_not_permitted")
    if _https_443_in_use(_json(run([str(exe), "funnel", "status", "--json"])), gateway):
        # Never replace a serve/Funnel configuration someone else set up.
        raise GatewayLaunchError("tailscale_https_443_already_configured")


def _https_443_in_use(config: Any, gateway: GatewaySettings) -> bool:
    for node in _walk(config):
        if isinstance(node, dict):
            if "443" in (node.get("TCP") or {}):
                return True
            web = node.get("Web") or {}
            if isinstance(web, dict) and f"{_host(gateway)}:443" in web:
                return True
    return False


def funnel_confirmed(config: Any, gateway: GatewaySettings) -> bool:
    key = f"{_host(gateway)}:443"
    allowed = any(
        isinstance(node, dict) and (node.get("AllowFunnel") or {}).get(key) is True
        for node in _walk(config)
    )
    proxied = any(
        isinstance(node, dict) and node.get("Proxy") == funnel_target(gateway)
        for node in _walk(config)
    )
    return allowed and proxied


def funnel_arguments(gateway: GatewaySettings) -> list[str]:
    # Foreground: the Funnel config lives only as long as this owned process.
    return [str(gateway.tailscale_exe), "funnel", "--yes", funnel_target(gateway)]


def funnel_environment(source: Mapping[str, str]) -> dict[str, str]:
    upper = {key.upper(): value for key, value in source.items()}
    return {key: upper[key] for key in _FUNNEL_ENV_KEYS if key in upper}


# Running --------------------------------------------------------------------------


class _Server(uvicorn.Server):
    """Ctrl+C cancels ``serve`` (asyncio.run), whose ``finally`` closes Funnel first.

    uvicorn's own handler would re-raise the signal after shutdown, possibly
    before Funnel is closed; the Job Object still kills Funnel on process exit.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def _listen(server: uvicorn.Server) -> None:
    try:
        await server.serve()
    except SystemExit:
        # uvicorn exits when it cannot bind; inside a task that would escape the loop.
        pass


def lock_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "esp-device-broker-mcp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "gateway.lock"


def _event(name: str) -> None:
    # Local-time stamp in the same shape as the HTTP log lines, so a stop can be placed in time.
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    print(f"{stamp} {name}", flush=True)


# Self-healing of the Tailscale child ----------------------------------------------


@dataclass(frozen=True)
class _Exposure:
    """What differs between the gateway Funnel and the web UI serve/Funnel child."""

    prefix: str  # event prefix, e.g. "gateway_funnel"
    noun: str  # "the gateway" / "the web UI"
    arguments: list[str]
    cwd: str
    status_args: list[str]
    preflight: Callable[[], None]  # the same Tailscale checks as at startup
    confirmed: Callable[[Any], bool]
    refuse: Callable[[Any], str | None]  # a fixed code when the exposure is wider than asked
    open_event: str


@dataclass
class _Child:
    """The one owned Tailscale child; ``serve``'s ``finally`` closes whatever is here."""

    process: windows_process.OwnedWindowsProcess | None = None


@dataclass
class _Healing:
    run: Runner
    start_process: Callable[..., windows_process.OwnedWindowsProcess]
    environ: Mapping[str, str]
    poll_seconds: float
    confirm_seconds: float
    delays: Sequence[float]
    stable_seconds: float
    sleep: Callable[[float], Awaitable[None]]
    clock: Callable[[], float]
    attempt: int = 0
    opened_at: float = 0.0


_RETRYABLE = frozenset(
    {
        "tailscale_unavailable",
        "tailscale_query_failed",
        "tailscale_output_invalid",
        "tailscale_not_running",
        "tailscale_executable_missing",
        # Our previous session can still be listed for a moment; it is never reset here.
        "tailscale_https_443_already_configured",
        "tailscale_serve_port_already_configured",
    }
)


async def _open_exposure(exposure: _Exposure, child: _Child, heal: _Healing) -> None:
    """Start the child and wait until Tailscale lists exactly what was asked for."""
    prefix = exposure.prefix
    try:
        process = heal.start_process(
            exposure.arguments,
            funnel_environment(heal.environ),
            exposure.cwd,
            executables=windows_process.TAILSCALE_EXECUTABLES,
        )
    except windows_process.WindowsProcessError as error:
        child.process = error.owned
        raise GatewayLaunchError(f"{prefix}_start_failed") from None
    child.process = process
    deadline = heal.clock() + heal.confirm_seconds
    while True:
        if process.poll() is not None:
            raise GatewayLaunchError(f"{prefix}_exited")
        config = _json(await asyncio.to_thread(heal.run, exposure.status_args))
        refused = exposure.refuse(config)
        if refused is not None:
            raise GatewayLaunchError(refused)
        if exposure.confirmed(config):
            break
        if heal.clock() > deadline:
            raise GatewayLaunchError(f"{prefix}_not_confirmed")
        await heal.sleep(heal.poll_seconds)
    _event(exposure.open_event)


async def _release(child: _Child) -> bool:
    """Stop and close the current child; False when its end cannot be confirmed."""
    process = child.process
    if process is None:
        return True
    try:
        stopped = await asyncio.to_thread(process.stop, 5.0)
        if stopped:
            process.close()
    except windows_process.WindowsProcessError:
        return False
    if stopped:
        child.process = None
    return stopped


async def _restart_exposure(
    exposure: _Exposure,
    child: _Child,
    heal: _Healing,
    listener: "asyncio.Task[None]",
) -> bool:
    """Start the child again after it ended by itself; False means stop the service.

    Only the same command with the same arguments runs again, after the startup checks pass
    again. Nothing is reset, replaced or taken over. Cancellation (Ctrl+C) is not caught: the
    waits are plain awaits and ``serve``'s ``finally`` closes the current child.
    """
    prefix, noun = exposure.prefix, exposure.noun
    total = len(heal.delays)
    assert child.process is not None
    _event(f"{prefix}_exited exit_code={child.process.poll()}; restarting")
    while True:
        # A second child never starts before the previous one is confirmed gone.
        if not await _release(child):
            _event(f"{prefix}_restart_refused reason={prefix}_stop_unconfirmed; stopping {noun}")
            return False
        heal.attempt += 1
        if heal.attempt > total:
            _event(f"{prefix}_restart_gave_up attempts={total}; stopping {noun}")
            return False
        attempt = heal.attempt
        delay = heal.delays[attempt - 1]
        _event(f"{prefix}_restarting attempt {attempt}/{total} in {delay:g}s")
        await heal.sleep(delay)
        if listener.done():
            return False
        try:
            await asyncio.to_thread(exposure.preflight)
            await _open_exposure(exposure, child, heal)
        except GatewayLaunchError as error:
            reason = str(error)
            retryable = (
                reason in _RETRYABLE
                or reason in (f"{prefix}_exited", f"{prefix}_not_confirmed")
                or (reason == f"{prefix}_start_failed" and child.process is None)
            )
            if not retryable:
                _event(f"{prefix}_restart_refused reason={reason}; stopping {noun}")
                return False
            _event(f"{prefix}_restart_failed attempt {attempt}/{total} reason={reason}")
            continue
        except windows_process.WindowsProcessError:
            _event(f"{prefix}_restart_refused reason={prefix}_process_error; stopping {noun}")
            return False
        heal.opened_at = heal.clock()
        _event(f"{prefix}_restarted attempt {attempt}/{total}")
        return True


async def _watch_exposure(
    exposure: _Exposure,
    child: _Child,
    heal: _Healing,
    listener: "asyncio.Task[None]",
) -> int:
    """Hold the service open until the listener ends; 1 when the child could not be kept."""
    while not listener.done():
        process = child.process
        if process is not None and process.poll() is not None:
            if not heal.delays:
                _event(f"{exposure.prefix}_exited; stopping {exposure.noun}")
                return 1
            if not await _restart_exposure(exposure, child, heal, listener):
                return 1
            continue
        if heal.attempt and heal.clock() - heal.opened_at >= heal.stable_seconds:
            heal.attempt = 0
            _event(f"{exposure.prefix}_stable; restart attempts reset")
        await heal.sleep(heal.poll_seconds)
    return 0


async def serve(
    prepared: Prepared,
    *,
    run: Runner = run_tailscale,
    start_process: Callable[..., windows_process.OwnedWindowsProcess] = windows_process.start,
    environ: Mapping[str, str] = os.environ,
    poll_seconds: float = 0.5,
    restart_delays: Sequence[float] = _RESTART_DELAYS,
    stable_seconds: float = _RESTART_STABLE_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    gateway = prepared.gateway
    if gateway.funnel:
        await asyncio.to_thread(check_tailscale, gateway, run)
    try:
        # Next to the gateway's broker key file, in the same private directory.
        store = GatewayStore(
            default_store_path(gateway.broker_env_file),
            subject=gateway.allowed_google_sub,
            resource=canonical_resource(gateway.public_url + MCP_PATH),
        )
    except GatewayStoreError as error:
        raise GatewayLaunchError(error.reason) from None
    google_http = httpx2.AsyncClient(trust_env=False, follow_redirects=False)
    relay = BrokerRelay(prepared.broker.url, prepared.broker.token, gateway.broker_env_file)
    app, _provider = create_gateway_app(
        gateway, prepared.google_secret, http=google_http, mcp_app=relay, store=store
    )
    server = _Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=gateway.port,
            access_log=False,
            log_level="warning",
            proxy_headers=False,
            server_header=False,
            lifespan="off",
        )
    )
    listener = asyncio.create_task(_listen(server))
    child = _Child()
    code = 0
    try:
        while not server.started:
            if listener.done():
                raise GatewayLaunchError("gateway_listen_failed")
            await asyncio.sleep(0.05)
        _event(f"gateway_listening 127.0.0.1:{gateway.port}")
        if gateway.funnel:
            exposure = _Exposure(
                prefix="gateway_funnel",
                noun="the gateway",
                arguments=funnel_arguments(gateway),
                cwd=str(gateway.tailscale_exe.parent),
                status_args=[str(gateway.tailscale_exe), "funnel", "status", "--json"],
                preflight=lambda: check_tailscale(gateway, run),
                confirmed=lambda config: funnel_confirmed(config, gateway),
                refuse=lambda _config: None,
                open_event="gateway_funnel_open " + gateway.public_url,
            )
            heal = _Healing(
                run=run,
                start_process=start_process,
                environ=environ,
                poll_seconds=poll_seconds,
                confirm_seconds=_FUNNEL_CONFIRM_SECONDS,
                delays=restart_delays,
                stable_seconds=stable_seconds,
                sleep=sleep,
                clock=clock,
            )
            await _open_exposure(exposure, child, heal)
            heal.opened_at = clock()
            code = await _watch_exposure(exposure, child, heal, listener)
        else:
            _event("gateway_funnel_disabled; listening on loopback only")
            while not listener.done():
                await asyncio.sleep(poll_seconds)
    except GatewayLaunchError as error:
        _event(str(error))
        code = 1
    except windows_process.WindowsProcessError:
        _event("gateway_funnel_process_error")
        code = 1
    finally:
        funnel = child.process
        if funnel is not None:
            # Close the public entry before the listener.
            try:
                stopped = await asyncio.to_thread(funnel.stop, 5.0)
                if stopped:
                    funnel.close()
            except windows_process.WindowsProcessError:
                stopped = False
            if stopped:
                _event("gateway_funnel_closed")
            else:
                _event("gateway_funnel_stop_unconfirmed; check `tailscale funnel status`")
                code = 1
        server.should_exit = True
        try:
            await listener
        except Exception:
            pass
        await relay.aclose()
        await google_http.aclose()
        store.close()
        _event("gateway_stopped")
    return code


def run(config: Path) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    logging.getLogger("cardputer_mcp.gateway").setLevel(logging.INFO)
    try:
        prepared = prepare(config)
    except GatewayLaunchError as error:
        _event(str(error))
        return 1
    try:
        with FileLock(str(lock_path()), timeout=0):
            return asyncio.run(serve(prepared))
    except Timeout:
        _event("gateway_already_running")
        return 1
    except GatewayLaunchError as error:
        # Preflight refusals happen before anything listens or starts.
        _event(str(error))
        return 1
    except KeyboardInterrupt:
        return 0
