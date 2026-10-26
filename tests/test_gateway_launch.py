"""cardputer gateway: config checks, Tailscale preflight, Funnel ownership and order."""

import asyncio
import json
import os
import socket

import httpx2
import pytest

from cardputer_mcp import client_environment, gateway_launch
from cardputer_mcp import windows_process as win
from cardputer_mcp.config import GatewaySettings

PUBLIC = "https://gw-a1.tail1234.ts.net"
HOST = "gw-a1.tail1234.ts.net"
KEY = "cpk_" + "1" * 32 + "." + "a" * 64
SECRET = "GOCSPX-abcdefghijklmnop"


def _private(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _config(tmp_path, *, broker_port=8765, gateway_port=None, ingress=True, extra="", env=None):
    broker_env = _private(
        tmp_path / "broker.env",
        env or f"CARDPUTER_CLIENT_KEY={KEY}\nCARDPUTER_BROKER_URL=http://127.0.0.1:8765/mcp\n",
    )
    secret = _private(tmp_path / "google.env", f"CARDPUTER_GATEWAY_GOOGLE_CLIENT_SECRET={SECRET}\n")
    tailscale = tmp_path / "tailscale.exe"
    tailscale.write_bytes(b"MZ")
    path = tmp_path / "device.toml"
    path.write_text(
        f"port = {broker_port}\n"
        "[tunnel]\n"
        f"ingress_enabled = {'true' if ingress else 'false'}\n"
        "[gateway]\n"
        f'public_url = "{PUBLIC}"\n'
        f"port = {gateway_port or 8770}\n"
        'google_client_id = "123-abc.apps.googleusercontent.com"\n'
        'allowed_google_sub = "1111111111"\n'
        f"broker_env_file = {json.dumps(str(broker_env))}\n"
        f"google_client_secret_file = {json.dumps(str(secret))}\n"
        f"tailscale_exe = {json.dumps(str(tailscale))}\n" + extra,
        encoding="utf-8",
    )
    return path


def test_prepare_loads_both_private_files(tmp_path):
    prepared = gateway_launch.prepare(_config(tmp_path))
    assert prepared.broker.token == KEY and prepared.google_secret == SECRET
    assert KEY not in repr(prepared) and SECRET not in repr(prepared)


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"ingress": False}, "gateway_requires_tunnel_ingress"),
        ({"gateway_port": 8765}, "gateway_port_conflict"),
        ({"gateway_port": 8766}, "gateway_port_conflict"),  # Tunnel health port
        ({"broker_port": 9000}, "gateway_broker_port_mismatch"),
        ({"env": "CARDPUTER_CLIENT_KEY=nope\n"}, "gateway_broker_env_invalid"),
    ],
)
def test_prepare_refuses_inconsistent_setup(tmp_path, kwargs, error):
    with pytest.raises(gateway_launch.GatewayLaunchError, match=error):
        gateway_launch.prepare(_config(tmp_path, **kwargs))


def test_prepare_requires_gateway_section_and_valid_secret(tmp_path):
    plain = tmp_path / "plain.toml"
    plain.write_text("port = 8765\n", encoding="utf-8")
    with pytest.raises(gateway_launch.GatewayLaunchError, match="gateway_config_missing"):
        gateway_launch.prepare(plain)
    config = _config(tmp_path)
    (tmp_path / "google.env").unlink()
    _private(tmp_path / "google.env", "CARDPUTER_GATEWAY_GOOGLE_CLIENT_SECRET=a b\n")
    with pytest.raises(gateway_launch.GatewayLaunchError, match="gateway_google_secret_invalid"):
        gateway_launch.prepare(config)


def test_gateway_secret_file_grammar(tmp_path):
    good = _private(tmp_path / "ok", f"# Google\nCARDPUTER_GATEWAY_GOOGLE_CLIENT_SECRET={SECRET}\n")
    assert client_environment.load_gateway_secret(good) == SECRET
    for index, text in enumerate(
        [
            "",
            f"CARDPUTER_GATEWAY_GOOGLE_CLIENT_SECRET={SECRET}\n"
            f"CARDPUTER_GATEWAY_GOOGLE_CLIENT_SECRET={SECRET}\n",
            f"CARDPUTER_CLIENT_KEY={KEY}\n",
            f'CARDPUTER_GATEWAY_GOOGLE_CLIENT_SECRET="{SECRET}"\n',
        ]
    ):
        bad = _private(tmp_path / f"bad{index}", text)
        with pytest.raises(client_environment.ClientEnvironmentError):
            client_environment.load_gateway_secret(bad)


# Tailscale preflight --------------------------------------------------------------


def _status(name=HOST + ".", state="Running", caps=("funnel", "https")):
    return json.dumps(
        {"BackendState": state, "Self": {"DNSName": name, "Capabilities": list(caps)}}
    )


def _runner(status, funnel="{}"):
    calls = []

    def run(args):
        calls.append(args[1:])
        return status if args[1] == "status" else funnel

    run.calls = calls
    return run


def _gateway(tmp_path, **extra):
    return gateway_launch.prepare(_config(tmp_path, **extra)).gateway


def test_tailscale_preflight_accepts_this_machine(tmp_path):
    run = _runner(_status())
    gateway_launch.check_tailscale(_gateway(tmp_path), run)
    assert run.calls == [["status", "--json"], ["funnel", "status", "--json"]]


@pytest.mark.parametrize(
    "status,funnel,error",
    [
        (_status(state="Stopped"), "{}", "tailscale_not_running"),
        (_status(name="other.tail1234.ts.net."), "{}", "gateway_public_url_not_this_machine"),
        (_status(caps=("https",)), "{}", "tailscale_funnel_not_permitted"),
        (_status(), '{"TCP": {"443": {"HTTPS": true}}}', "tailscale_https_443_already_configured"),
        (
            _status(),
            json.dumps({"Foreground": {"s": {"Web": {f"{HOST}:443": {}}}}}),
            "tailscale_https_443_already_configured",
        ),
        ("not json", "{}", "tailscale_output_invalid"),
    ],
)
def test_tailscale_preflight_refusals(tmp_path, status, funnel, error):
    with pytest.raises(gateway_launch.GatewayLaunchError, match=error):
        gateway_launch.check_tailscale(_gateway(tmp_path), _runner(status, funnel))


def test_tailscale_executable_must_be_tailscale_exe(tmp_path):
    gateway = _gateway(tmp_path)
    other = gateway.model_copy(update={"tailscale_exe": tmp_path / "missing" / "tailscale.exe"})
    with pytest.raises(gateway_launch.GatewayLaunchError, match="tailscale_executable_missing"):
        gateway_launch.check_tailscale(other, _runner(_status()))


def _open_funnel(gateway: GatewaySettings, port=None) -> str:
    target = f"http://127.0.0.1:{port or gateway.port}"
    return json.dumps(
        {
            "Foreground": {
                "session": {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {f"{HOST}:443": {"Handlers": {"/": {"Proxy": target}}}},
                    "AllowFunnel": {f"{HOST}:443": True},
                }
            }
        }
    )


def test_funnel_confirmation_needs_our_host_and_target(tmp_path):
    gateway = _gateway(tmp_path)
    assert gateway_launch.funnel_confirmed(json.loads(_open_funnel(gateway)), gateway)
    assert not gateway_launch.funnel_confirmed(json.loads(_open_funnel(gateway, 1)), gateway)
    assert not gateway_launch.funnel_confirmed({}, gateway)


def test_funnel_command_and_environment_carry_no_secrets(tmp_path):
    gateway = _gateway(tmp_path)
    args = gateway_launch.funnel_arguments(gateway)
    assert args == [str(gateway.tailscale_exe), "funnel", "--yes", "http://127.0.0.1:8770"]
    assert "--bg" not in args
    env = gateway_launch.funnel_environment(
        {"SystemRoot": "C:\\Windows", "CARDPUTER_LOCAL_TOKEN": "x", "PATH": "y", "TEMP": "t"}
    )
    assert env == {"SYSTEMROOT": "C:\\Windows", "TEMP": "t"}
    # windows_process accepts tailscale.exe only when the caller asks for it.
    native = [r"C:\Program Files\Tailscale\tailscale.exe", *args[1:]]
    win._validate(native, env, r"C:\Program Files\Tailscale", win.TAILSCALE_EXECUTABLES)
    with pytest.raises(win.WindowsProcessError):
        win._validate(native, env, r"C:\Program Files\Tailscale", win.TUNNEL_EXECUTABLES)


# Running --------------------------------------------------------------------------


class FakeFunnel:
    def __init__(self, events):
        self.events = events
        self.exit_code = None
        self.closed = False

    def poll(self):
        return self.exit_code

    def stop(self, timeout=5.0):
        self.events.append("funnel_stopped")
        self.exit_code = 1
        return True

    def close(self):
        self.closed = True


async def _wait_listening(port):
    async with httpx2.AsyncClient(trust_env=False) as client:
        for _ in range(200):
            try:
                response = await client.get(
                    f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource/mcp"
                )
                return response
            except httpx2.HTTPError:
                await asyncio.sleep(0.02)
    raise AssertionError("gateway never listened")


async def test_loopback_only_without_funnel_and_cancel_stops(tmp_path, capsys):
    port = _free_port()
    prepared = gateway_launch.prepare(_config(tmp_path, gateway_port=port))

    def never(*_args, **_kwargs):
        raise AssertionError("Tailscale must not be touched when funnel is off")

    task = asyncio.create_task(gateway_launch.serve(prepared, run=never, start_process=never))
    response = await _wait_listening(port)
    assert response.json()["resource"] == PUBLIC + "/mcp"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    out = capsys.readouterr().out
    assert "gateway_funnel_disabled" in out and "gateway_stopped" in out
    assert KEY not in out and SECRET not in out


async def test_funnel_opens_after_listen_and_closes_before_listener(tmp_path, capsys):
    port = _free_port()
    prepared = gateway_launch.prepare(_config(tmp_path, gateway_port=port, extra="funnel = true\n"))
    events = []
    funnel = FakeFunnel(events)
    statuses = iter(["{}", "{}", _open_funnel(prepared.gateway)])

    def run(args):
        if args[1] == "status":
            return _status()
        if args[1:] == ["funnel", "status", "--json"] and "funnel_started" not in events:
            return "{}"  # preflight: nothing on 443
        return next(statuses, _open_funnel(prepared.gateway))

    def start_process(args, env, cwd, *, executables):
        # The gateway is already listening when Funnel starts.
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        assert executables == win.TAILSCALE_EXECUTABLES
        assert args == gateway_launch.funnel_arguments(prepared.gateway)
        events.append("funnel_started")
        return funnel

    task = asyncio.create_task(
        gateway_launch.serve(prepared, run=run, start_process=start_process, poll_seconds=0.01)
    )
    for _ in range(500):
        if "gateway_funnel_open" in capsys.readouterr().out:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("funnel never confirmed")
    # The Funnel process exiting on its own stops the gateway.
    funnel.exit_code = 0
    assert await asyncio.wait_for(task, 5) == 1
    assert events == ["funnel_started", "funnel_stopped"] and funnel.closed
    with pytest.raises(httpx2.HTTPError):
        async with httpx2.AsyncClient(trust_env=False) as client:
            await client.get(f"http://127.0.0.1:{port}/mcp", timeout=1)


async def test_unconfirmed_funnel_is_closed_and_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gateway_launch, "_FUNNEL_CONFIRM_SECONDS", 0.05)
    port = _free_port()
    prepared = gateway_launch.prepare(_config(tmp_path, gateway_port=port, extra="funnel = true\n"))
    events = []
    funnel = FakeFunnel(events)

    def run(args):
        return _status() if args[1] == "status" else "{}"

    code = await gateway_launch.serve(
        prepared,
        run=run,
        start_process=lambda *a, **k: funnel,
        poll_seconds=0.01,
    )
    assert code == 1 and events == ["funnel_stopped"]
    assert "gateway_funnel_not_confirmed" in capsys.readouterr().out


async def test_preflight_failure_never_starts_funnel_or_listens(tmp_path, capsys):
    port = _free_port()
    prepared = gateway_launch.prepare(_config(tmp_path, gateway_port=port, extra="funnel = true\n"))

    def start_process(*_args, **_kwargs):
        raise AssertionError("no Funnel after a failed preflight")

    with pytest.raises(gateway_launch.GatewayLaunchError, match="tailscale_funnel_not_permitted"):
        await gateway_launch.serve(
            prepared, run=_runner(_status(caps=())), start_process=start_process
        )
    with pytest.raises(httpx2.HTTPError):
        async with httpx2.AsyncClient(trust_env=False) as client:
            await client.get(f"http://127.0.0.1:{port}/mcp", timeout=1)


async def test_port_in_use_fails_without_starting_funnel(tmp_path, capsys):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        prepared = gateway_launch.prepare(_config(tmp_path, gateway_port=port))

        def never(*_args, **_kwargs):
            raise AssertionError("no Tailscale when the listener failed")

        code = await gateway_launch.serve(prepared, run=never, start_process=never)
    assert code == 1
    assert "gateway_listen_failed" in capsys.readouterr().out
