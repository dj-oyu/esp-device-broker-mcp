"""cardputer webui: config checks, broker key check, tailnet serve ownership and order."""

import asyncio
import json
import os
import socket

import httpx2
import pytest
from test_webui import KEY, FakeBroker

from cardputer_mcp import webui_launch
from cardputer_mcp import windows_process as win
from cardputer_mcp.webui import BrokerReader

HOST = "gw-a1.tail1234.ts.net"
UI = f"https://{HOST}:8443"
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


def _config(tmp_path, *, port=None, public=UI, broker_port=8765, gateway=False, extra=""):
    broker_env = _private(
        tmp_path / "webui-broker.env",
        f"CARDPUTER_CLIENT_KEY={KEY}\nCARDPUTER_BROKER_URL=http://127.0.0.1:8765/mcp\n",
    )
    secret = _private(tmp_path / "google.env", f"CARDPUTER_GATEWAY_GOOGLE_CLIENT_SECRET={SECRET}\n")
    tailscale = tmp_path / "tailscale.exe"
    tailscale.write_bytes(b"MZ")
    gateway_section = (
        "[gateway]\n"
        f'public_url = "https://{HOST}"\n'
        'google_client_id = "123-abc.apps.googleusercontent.com"\n'
        'allowed_google_sub = "1111111111"\n'
        f"broker_env_file = {json.dumps(str(broker_env))}\n"
        f"google_client_secret_file = {json.dumps(str(secret))}\n"
        if gateway
        else ""
    )
    path = tmp_path / "device.toml"
    path.write_text(
        f"port = {broker_port}\n"
        "[tunnel]\n"
        "health_port = 8766\n" + gateway_section + "[webui]\n"
        f'public_url = "{public}"\n'
        f"port = {port or 8771}\n"
        'google_client_id = "123-abc.apps.googleusercontent.com"\n'
        'allowed_google_sub = "1111111111"\n'
        f"broker_env_file = {json.dumps(str(broker_env))}\n"
        f"google_client_secret_file = {json.dumps(str(secret))}\n"
        f"tailscale_exe = {json.dumps(str(tailscale))}\n" + extra,
        encoding="utf-8",
    )
    return path


def test_prepare_loads_both_private_files(tmp_path):
    prepared = webui_launch.prepare(_config(tmp_path))
    assert prepared.broker.token == KEY and prepared.google_secret == SECRET
    assert KEY not in repr(prepared) and SECRET not in repr(prepared)


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"port": 8765}, "webui_port_conflict"),
        ({"port": 8766}, "webui_port_conflict"),
        ({"port": 8770, "gateway": True}, "webui_port_conflict"),
        ({"public": f"https://{HOST}:443", "gateway": True}, "webui_port_conflict"),
        ({"broker_port": 9000}, "webui_broker_port_mismatch"),
        ({"public": f"https://{HOST}"}, "webui_config_invalid"),  # the serve port is explicit
        ({"public": f"http://{HOST}:8443"}, "webui_config_invalid"),
    ],
)
def test_prepare_refuses_inconsistent_setup(tmp_path, kwargs, error):
    with pytest.raises(webui_launch.LaunchError, match=error):
        webui_launch.prepare(_config(tmp_path, **kwargs))


def test_prepare_requires_the_section(tmp_path):
    plain = tmp_path / "plain.toml"
    plain.write_text("port = 8765\n", encoding="utf-8")
    with pytest.raises(webui_launch.LaunchError, match="webui_config_missing"):
        webui_launch.prepare(plain)


# Broker key -------------------------------------------------------------------------


def _reader(broker):
    return BrokerReader(
        "http://127.0.0.1:8765/mcp",
        KEY,
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(broker.handler)),
    )


async def test_broker_key_must_read_and_must_not_act():
    broker = FakeBroker()
    reader = _reader(broker)
    await webui_launch.check_broker_key(reader)
    broker.control = True
    with pytest.raises(webui_launch.LaunchError, match="webui_broker_key_not_read_only"):
        await webui_launch.check_broker_key(reader)
    broker.reject = True
    with pytest.raises(webui_launch.LaunchError, match="webui_broker_key_rejected"):
        await webui_launch.check_broker_key(reader)
    await reader.aclose()


async def test_unreachable_broker_is_refused():
    def down(request):
        raise httpx2.ConnectError("refused")

    reader = BrokerReader(
        "http://127.0.0.1:8765/mcp",
        KEY,
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(down)),
    )
    with pytest.raises(webui_launch.LaunchError, match="webui_broker_unavailable"):
        await webui_launch.check_broker_key(reader)
    await reader.aclose()


# Tailscale --------------------------------------------------------------------------


def _status(name=HOST + ".", state="Running"):
    return json.dumps({"BackendState": state, "Self": {"DNSName": name}})


def _runner(status, serve="{}"):
    calls = []

    def run(args):
        calls.append(args[1:])
        return status if args[1] == "status" else serve

    run.calls = calls
    return run


def _webui(tmp_path, **extra):
    return webui_launch.prepare(_config(tmp_path, **extra)).webui


def _served(webui, *, port=None, public=False):
    target = f"http://127.0.0.1:{port or webui.port}"
    session = {
        "TCP": {"8443": {"HTTPS": True}},
        "Web": {f"{HOST}:8443": {"Handlers": {"/": {"Proxy": target}}}},
    }
    if public:
        session["AllowFunnel"] = {f"{HOST}:8443": True}
    return json.dumps({"Foreground": {"s": session}})


def test_tailscale_preflight_accepts_this_machine(tmp_path):
    run = _runner(_status())
    webui_launch.check_tailscale(_webui(tmp_path), run)
    assert run.calls == [["status", "--json"], ["serve", "status", "--json"]]


@pytest.mark.parametrize(
    "status,serve,error",
    [
        (_status(state="Stopped"), "{}", "tailscale_not_running"),
        (_status(name="other.tail1234.ts.net."), "{}", "webui_public_url_not_this_machine"),
        (
            _status(),
            '{"TCP": {"8443": {"HTTPS": true}}}',
            "tailscale_serve_port_already_configured",
        ),
        ("not json", "{}", "tailscale_output_invalid"),
    ],
)
def test_tailscale_preflight_refusals(tmp_path, status, serve, error):
    with pytest.raises(webui_launch.LaunchError, match=error):
        webui_launch.check_tailscale(_webui(tmp_path), _runner(status, serve))


def test_serve_is_confirmed_only_for_our_target_and_never_public(tmp_path):
    webui = _webui(tmp_path)
    assert webui_launch.serve_confirmed(json.loads(_served(webui)), webui)
    assert not webui_launch.serve_confirmed(json.loads(_served(webui, port=1)), webui)
    assert not webui_launch.serve_confirmed(json.loads(_served(webui, public=True)), webui)
    assert webui_launch.serve_is_public(json.loads(_served(webui, public=True)), webui)
    assert not webui_launch.serve_confirmed({}, webui)


def test_serve_command_is_tailnet_https_in_the_foreground(tmp_path):
    webui = _webui(tmp_path)
    args = webui_launch.serve_arguments(webui)
    assert args == [
        str(webui.tailscale_exe),
        "serve",
        "--https=8443",
        "--yes",
        "http://127.0.0.1:8771",
    ]
    assert "--bg" not in args and "funnel" not in args
    native = [r"C:\Program Files\Tailscale\tailscale.exe", *args[1:]]
    win._validate(native, {}, r"C:\Program Files\Tailscale", win.TAILSCALE_EXECUTABLES)


# Running ----------------------------------------------------------------------------


class FakeServe:
    def __init__(self, events):
        self.events = events
        self.exit_code = None
        self.closed = False

    def poll(self):
        return self.exit_code

    def stop(self, timeout=5.0):
        self.events.append("serve_stopped")
        self.exit_code = 1
        return True

    def close(self):
        self.closed = True


def _broker_http(broker=None):
    return httpx2.AsyncClient(transport=httpx2.MockTransport((broker or FakeBroker()).handler))


async def _wait_for(capsys, marker):
    out = ""
    for _ in range(500):
        out += capsys.readouterr().out
        if marker in out:
            return out
        await asyncio.sleep(0.01)
    raise AssertionError(f"never saw {marker}")


async def test_loopback_only_without_serve_and_cancel_stops(tmp_path, capsys):
    port = _free_port()
    prepared = webui_launch.prepare(_config(tmp_path, port=port))

    def never(*_args, **_kwargs):
        raise AssertionError("Tailscale must not be touched when serve is off")

    task = asyncio.create_task(
        webui_launch.serve(prepared, run=never, start_process=never, broker_http=_broker_http())
    )
    out = await _wait_for(capsys, "webui_serve_disabled")
    async with httpx2.AsyncClient(trust_env=False) as client:
        response = await client.get(
            f"http://127.0.0.1:{port}/api/state", headers={"host": f"{HOST}:8443"}
        )
    assert response.status_code == 401
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    out += capsys.readouterr().out
    assert "webui_listening" in out and "webui_stopped" in out
    assert KEY not in out and SECRET not in out


async def test_serve_opens_after_listen_and_closes_before_listener(tmp_path, capsys):
    port = _free_port()
    prepared = webui_launch.prepare(_config(tmp_path, port=port, extra="serve = true\n"))
    events = []
    process = FakeServe(events)

    def run(args):
        if args[1] == "status":
            return _status()
        if "serve_started" not in events:
            return "{}"
        return _served(prepared.webui)

    def start_process(args, env, cwd, *, executables):
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        assert executables == win.TAILSCALE_EXECUTABLES
        assert args == webui_launch.serve_arguments(prepared.webui)
        events.append("serve_started")
        return process

    task = asyncio.create_task(
        webui_launch.serve(
            prepared,
            run=run,
            start_process=start_process,
            poll_seconds=0.01,
            broker_http=_broker_http(),
        )
    )
    await _wait_for(capsys, "webui_serve_open")
    process.exit_code = 0
    assert await asyncio.wait_for(task, 5) == 1
    assert events == ["serve_started", "serve_stopped"] and process.closed


async def test_public_serve_is_closed_and_fails(tmp_path, capsys):
    port = _free_port()
    prepared = webui_launch.prepare(_config(tmp_path, port=port, extra="serve = true\n"))
    events = []
    process = FakeServe(events)

    def run(args):
        if args[1] == "status":
            return _status()
        if not events:
            return "{}"
        return _served(prepared.webui, public=True)

    def start_process(*_args, **_kwargs):
        events.append("serve_started")
        return process

    code = await webui_launch.serve(
        prepared,
        run=run,
        start_process=start_process,
        poll_seconds=0.01,
        broker_http=_broker_http(),
    )
    assert code == 1 and events == ["serve_started", "serve_stopped"]
    assert "webui_serve_is_public" in capsys.readouterr().out


async def test_control_key_never_listens(tmp_path):
    port = _free_port()
    prepared = webui_launch.prepare(_config(tmp_path, port=port, extra="serve = true\n"))
    broker = FakeBroker()
    broker.control = True

    def never(*_args, **_kwargs):
        raise AssertionError("nothing starts with a control key")

    with pytest.raises(webui_launch.LaunchError, match="webui_broker_key_not_read_only"):
        await webui_launch.serve(
            prepared, run=never, start_process=never, broker_http=_broker_http(broker)
        )
    with pytest.raises(httpx2.HTTPError):
        async with httpx2.AsyncClient(trust_env=False) as client:
            await client.get(f"http://127.0.0.1:{port}/", timeout=1)


def _sub(tmp_path, name):
    (tmp_path / name).mkdir()
    return tmp_path / name


def test_funnel_is_explicit_and_exclusive(tmp_path):
    with pytest.raises(webui_launch.LaunchError, match="webui_config_invalid"):
        webui_launch.prepare(_config(_sub(tmp_path, "a"), extra="serve = true\nfunnel = true\n"))
    with pytest.raises(webui_launch.LaunchError, match="webui_config_invalid"):
        webui_launch.prepare(
            _config(_sub(tmp_path, "b"), public=f"https://{HOST}:9443", extra="funnel = true\n")
        )
    webui = _webui(_sub(tmp_path, "c"), extra="funnel = true\n")
    assert webui.funnel and not webui.serve
    args = webui_launch.serve_arguments(webui)
    assert args[1:3] == ["funnel", "--https=8443"]
    assert webui_launch.serve_arguments(_webui(_sub(tmp_path, "d")))[1] == "serve"


def test_funnel_needs_the_funnel_capability(tmp_path):
    webui = _webui(tmp_path, extra="funnel = true\n")
    with pytest.raises(webui_launch.LaunchError, match="tailscale_funnel_not_permitted"):
        webui_launch.check_tailscale(webui, _runner(_status()))
    capable = json.dumps(
        {"BackendState": "Running", "Self": {"DNSName": HOST + ".", "CapMap": {"funnel": []}}}
    )
    webui_launch.check_tailscale(webui, _runner(capable))


def test_funnel_is_confirmed_only_when_public(tmp_path):
    webui = _webui(tmp_path, extra="funnel = true\n")
    assert webui_launch.serve_confirmed(json.loads(_served(webui, public=True)), webui)
    assert not webui_launch.serve_confirmed(json.loads(_served(webui)), webui)
    plain = _webui(_sub(tmp_path, "e"))
    assert not webui_launch.serve_confirmed(json.loads(_served(plain, public=True)), plain)


async def test_funnel_opens_and_closes_with_the_process(tmp_path, capsys):
    port = _free_port()
    prepared = webui_launch.prepare(_config(tmp_path, port=port, extra="funnel = true\n"))
    events = []
    process = FakeServe(events)
    capable = json.dumps(
        {"BackendState": "Running", "Self": {"DNSName": HOST + ".", "CapMap": {"funnel": []}}}
    )

    def run(args):
        if args[1] == "status":
            return capable
        return _served(prepared.webui, public=True) if events else "{}"

    def start_process(args, env, cwd, *, executables):
        assert args[1] == "funnel"
        events.append("serve_started")
        return process

    task = asyncio.create_task(
        webui_launch.serve(
            prepared,
            run=run,
            start_process=start_process,
            poll_seconds=0.01,
            broker_http=_broker_http(),
        )
    )
    await _wait_for(capsys, "webui_funnel_open")
    process.exit_code = 0
    assert await asyncio.wait_for(task, 5) == 1
    assert events == ["serve_started", "serve_stopped"] and process.closed
