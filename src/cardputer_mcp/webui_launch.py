"""``cardputer webui``: run the read-only web UI and, only when configured, its serve or Funnel.

Startup order: validate the broker TOML's ``[webui]`` section, the broker key file
and the Google secret file; confirm the broker key reads the operator views and
cannot act; check Tailscale (this machine's name, nothing on the serve port);
listen on 127.0.0.1; then run ``tailscale serve --https=<port>`` (or, only with
``funnel = true``, ``tailscale funnel --https=<port>``) in the foreground as an owned
Job Object child and confirm that it lists our target and that the port is Funnel
exactly when Funnel was asked for. Shutdown closes it first, then the listener.

Nothing here starts or stops the broker, the gateway, the OpenAI Tunnel or any
device work. Output is fixed event names; no secrets, tokens or file contents.
"""

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx2
import uvicorn
from filelock import FileLock, Timeout

from . import client_environment, windows_process
from .broker_link import canonical_broker_port
from .config import Settings, WebUISettings, load_settings
from .gateway_launch import (
    GatewayLaunchError,
    Runner,
    _event,
    _json,
    _listen,
    _Server,
    _walk,
    funnel_environment,
    run_tailscale,
)
from .webui import BrokerReader, create_webui_app

_SERVE_CONFIRM_SECONDS = 15.0

LaunchError = GatewayLaunchError


@dataclass(frozen=True)
class Prepared:
    settings: Settings
    webui: WebUISettings
    broker: client_environment.ClientEnvironment = field(repr=False)
    google_secret: str = field(repr=False)


def prepare(config: Path) -> Prepared:
    try:
        settings = load_settings(config)
    except (OSError, ValueError):
        raise LaunchError("webui_config_invalid") from None
    webui = settings.webui
    if webui is None:
        raise LaunchError("webui_config_missing")
    taken = {settings.port}
    if settings.tunnel is not None:
        taken.add(settings.tunnel.health_port)
    if settings.gateway is not None:
        taken.add(settings.gateway.port)
    if webui.port in taken:
        raise LaunchError("webui_port_conflict")
    if settings.gateway is not None and _serve_port(webui) == 443:
        raise LaunchError("webui_port_conflict")
    try:
        broker = client_environment.load_client_environment(webui.broker_env_file)
    except client_environment.ClientEnvironmentError:
        raise LaunchError("webui_broker_env_invalid") from None
    if canonical_broker_port(broker.url) != settings.port:
        raise LaunchError("webui_broker_port_mismatch")
    try:
        secret = client_environment.load_gateway_secret(webui.google_client_secret_file)
    except client_environment.ClientEnvironmentError:
        raise LaunchError("webui_google_secret_invalid") from None
    return Prepared(settings, webui, broker, secret)


async def check_broker_key(reader: BrokerReader) -> None:
    """The key must read the operator state and must not reach a control-only route."""
    status, payload = await reader.get("/operator/state")
    if status != 200 or payload.get("ok") is not True:
        rejected = payload.get("error") == "broker_key_rejected"
        raise LaunchError("webui_broker_key_rejected" if rejected else "webui_broker_unavailable")
    probed = await reader.probe("/operator/images")
    if probed is None:
        raise LaunchError("webui_broker_unavailable")
    if probed != 403:
        # 200 (or anything but the read-only refusal) means this key can act.
        raise LaunchError("webui_broker_key_not_read_only")


# Tailscale ------------------------------------------------------------------------


def _host(webui: WebUISettings) -> str:
    return urlparse(webui.public_url).hostname or ""


def _serve_port(webui: WebUISettings) -> int:
    return urlparse(webui.public_url).port or 443


def serve_target(webui: WebUISettings) -> str:
    return f"http://127.0.0.1:{webui.port}"


def check_tailscale(webui: WebUISettings, run: Runner = run_tailscale) -> None:
    exe = webui.tailscale_exe
    if exe.name.casefold() not in windows_process.TAILSCALE_EXECUTABLES or not exe.is_file():
        raise LaunchError("tailscale_executable_missing")
    status = _json(run([str(exe), "status", "--json"]))
    me = status.get("Self") if isinstance(status, dict) else None
    if not isinstance(me, dict) or status.get("BackendState") != "Running":
        raise LaunchError("tailscale_not_running")
    name = str(me.get("DNSName", "")).rstrip(".").lower()
    if name != _host(webui):
        raise LaunchError("webui_public_url_not_this_machine")
    if webui.funnel:
        capabilities = [str(item) for item in me.get("Capabilities") or []]
        capabilities += [str(key) for key in (me.get("CapMap") or {})]
        if "funnel" not in capabilities and not any(
            item.startswith("https://tailscale.com/cap/funnel") for item in capabilities
        ):
            raise LaunchError("tailscale_funnel_not_permitted")
    if _port_in_use(_json(run([str(exe), "serve", "status", "--json"])), webui):
        raise LaunchError("tailscale_serve_port_already_configured")


def _port_in_use(config: Any, webui: WebUISettings) -> bool:
    port = str(_serve_port(webui))
    for node in _walk(config):
        if isinstance(node, dict):
            if port in (node.get("TCP") or {}):
                return True
            web = node.get("Web") or {}
            if isinstance(web, dict) and f"{_host(webui)}:{port}" in web:
                return True
    return False


def serve_confirmed(config: Any, webui: WebUISettings) -> bool:
    key = f"{_host(webui)}:{_serve_port(webui)}"
    proxied = any(
        isinstance(node, dict)
        and isinstance(node.get("Web"), dict)
        and any(
            isinstance(handlers, dict)
            and any(
                isinstance(h, dict) and h.get("Proxy") == serve_target(webui)
                for h in (handlers.get("Handlers") or {}).values()
            )
            for web_key, handlers in node["Web"].items()
            if web_key == key
        )
        for node in _walk(config)
    )
    return proxied and serve_is_public(config, webui) is webui.funnel


def serve_is_public(config: Any, webui: WebUISettings) -> bool:
    key = f"{_host(webui)}:{_serve_port(webui)}"
    return any(
        isinstance(node, dict) and (node.get("AllowFunnel") or {}).get(key) is True
        for node in _walk(config)
    )


def serve_arguments(webui: WebUISettings) -> list[str]:
    # Foreground: the config lives only as long as this owned process. Funnel only on request.
    return [
        str(webui.tailscale_exe),
        "funnel" if webui.funnel else "serve",
        f"--https={_serve_port(webui)}",
        "--yes",
        serve_target(webui),
    ]


# Running --------------------------------------------------------------------------


def lock_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "esp-device-broker-mcp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "webui.lock"


async def serve(
    prepared: Prepared,
    *,
    run: Runner = run_tailscale,
    start_process: Callable[..., windows_process.OwnedWindowsProcess] = windows_process.start,
    environ: Mapping[str, str] = os.environ,
    poll_seconds: float = 0.5,
    broker_http: httpx2.AsyncClient | None = None,
) -> int:
    webui = prepared.webui
    reader = BrokerReader(prepared.broker.url, prepared.broker.token, http=broker_http)
    google_http = httpx2.AsyncClient(trust_env=False, follow_redirects=False)
    try:
        await check_broker_key(reader)
        if webui.serve or webui.funnel:
            await asyncio.to_thread(check_tailscale, webui, run)
    except LaunchError:
        await reader.aclose()
        await google_http.aclose()
        raise
    app, _ui = create_webui_app(webui, prepared.google_secret, reader, http=google_http)
    server = _Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=webui.port,
            access_log=False,
            log_level="warning",
            proxy_headers=False,
            server_header=False,
            lifespan="off",
        )
    )
    listener = asyncio.create_task(_listen(server))
    process: windows_process.OwnedWindowsProcess | None = None
    code = 0
    try:
        while not server.started:
            if listener.done():
                raise LaunchError("webui_listen_failed")
            await asyncio.sleep(0.05)
        _event(f"webui_listening 127.0.0.1:{webui.port}")
        if webui.serve or webui.funnel:
            try:
                process = start_process(
                    serve_arguments(webui),
                    funnel_environment(environ),
                    str(webui.tailscale_exe.parent),
                    executables=windows_process.TAILSCALE_EXECUTABLES,
                )
            except windows_process.WindowsProcessError as error:
                process = error.owned
                raise LaunchError("webui_serve_start_failed") from None
            deadline = time.monotonic() + _SERVE_CONFIRM_SECONDS
            status = [str(webui.tailscale_exe), "serve", "status", "--json"]
            while True:
                if process.poll() is not None:
                    raise LaunchError("webui_serve_exited")
                config = _json(await asyncio.to_thread(run, status))
                if serve_is_public(config, webui) and not webui.funnel:
                    raise LaunchError("webui_serve_is_public")
                if serve_confirmed(config, webui):
                    break
                if time.monotonic() > deadline:
                    raise LaunchError("webui_serve_not_confirmed")
                await asyncio.sleep(poll_seconds)
            if webui.funnel:
                _event("webui_funnel_open " + webui.public_url + " (public; Google login required)")
            else:
                _event("webui_serve_open " + webui.public_url + " (tailnet only)")
        else:
            _event("webui_serve_disabled; listening on loopback only")
        while not listener.done():
            if process is not None and process.poll() is not None:
                _event("webui_serve_exited; stopping the web UI")
                code = 1
                break
            await asyncio.sleep(poll_seconds)
    except LaunchError as error:
        _event(str(error))
        code = 1
    except windows_process.WindowsProcessError:
        _event("webui_serve_process_error")
        code = 1
    finally:
        if process is not None:
            try:
                stopped = await asyncio.to_thread(process.stop, 5.0)
                if stopped:
                    process.close()
            except windows_process.WindowsProcessError:
                stopped = False
            if stopped:
                _event("webui_serve_closed")
            else:
                _event("webui_serve_stop_unconfirmed; check `tailscale serve status`")
                code = 1
        server.should_exit = True
        try:
            await listener
        except Exception:
            pass
        await reader.aclose()
        await google_http.aclose()
        _event("webui_stopped")
    return code


def run(config: Path) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    logging.getLogger("cardputer_mcp.webui").setLevel(logging.INFO)
    try:
        prepared = prepare(config)
    except LaunchError as error:
        _event(str(error))
        return 1
    try:
        with FileLock(str(lock_path()), timeout=0):
            return asyncio.run(serve(prepared))
    except Timeout:
        _event("webui_already_running")
        return 1
    except LaunchError as error:
        # Preflight refusals happen before anything listens or starts.
        _event(str(error))
        return 1
    except KeyboardInterrupt:
        return 0
