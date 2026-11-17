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
        f'mode = "{"ingress" if ingress else "off"}"\n'
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
    # The listener can answer before serve() notices it started; wait for its report.
    out = ""
    for _ in range(500):
        out += capsys.readouterr().out
        if "gateway_funnel_disabled" in out:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    out += capsys.readouterr().out
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
        gateway_launch.serve(
            prepared,
            run=run,
            start_process=start_process,
            poll_seconds=0.01,
            restart_delays=(),
        )
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


# Self-healing: the Funnel child ends by itself (a Tailscale update restarts tailscaled) -------


class _Clock:
    """Virtual time: waits are recorded, not slept; waits of a second or more can block."""

    def __init__(self, *, block_backoff=False):
        self.now = 0.0
        self.slept = []
        self.block_backoff = block_backoff

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.block_backoff and seconds >= 1:
            await asyncio.Event().wait()
        await asyncio.sleep(0)

    def __call__(self):
        return self.now

    def backoffs(self):
        return [value for value in self.slept if value >= 1]


class _Healing:
    """A fake Tailscale: ``children`` are the Funnel processes started so far."""

    def __init__(self, prepared, *, status=None, stop_ok=True):
        self.prepared = prepared
        self.children = []
        self.args = []
        self.status = status or (lambda: _status())
        self.stop_ok = stop_ok
        self.events = []

    def run(self, args):
        if args[1] == "status":
            return self.status()
        if any(child.exit_code is None for child in self.children):
            return _open_funnel(self.prepared.gateway)
        return "{}"

    def start_process(self, args, env, cwd, *, executables):
        child = FakeFunnel(self.events)
        stop = child.stop
        child.stop = lambda timeout=5.0: stop(timeout) and self.stop_ok
        self.children.append(child)
        self.args.append(args)
        return child


async def _out_until(capsys, marker, out=""):
    for _ in range(1000):
        out += capsys.readouterr().out
        if marker in out:
            return out
        await asyncio.sleep(0.01)
    raise AssertionError(f"never saw {marker}: {out}")


def _heal_serve(prepared, fake, clock, **kwargs):
    return gateway_launch.serve(
        prepared,
        run=fake.run,
        start_process=fake.start_process,
        poll_seconds=0.01,
        sleep=clock.sleep,
        clock=clock,
        **kwargs,
    )


def _funnel_prepared(tmp_path):
    return gateway_launch.prepare(
        _config(tmp_path, gateway_port=_free_port(), extra="funnel = true\n")
    )


async def test_funnel_is_started_again_after_it_ends_by_itself(tmp_path, capsys):
    prepared = _funnel_prepared(tmp_path)
    fake, clock = _Healing(prepared), _Clock()
    task = asyncio.create_task(_heal_serve(prepared, fake, clock, restart_delays=(5.0, 10.0)))
    out = await _out_until(capsys, "gateway_funnel_open")
    fake.children[0].exit_code = 0
    out = await _out_until(capsys, "gateway_funnel_restarted attempt 1/2", out)
    assert "gateway_funnel_exited exit_code=0; restarting" in out
    assert "gateway_funnel_restarting attempt 1/2 in 5s" in out
    assert clock.backoffs() == [5.0] and not task.done()
    assert len(fake.children) == 2 and fake.children[0].closed
    # Same command, same arguments; the old child was closed before the new one existed.
    assert fake.args[0] == fake.args[1] == gateway_launch.funnel_arguments(prepared.gateway)
    assert fake.events == ["funnel_stopped"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.children[1].closed and "gateway_stopped" in capsys.readouterr().out


async def test_restart_waits_grow_and_stop_after_the_last_attempt(tmp_path, capsys):
    prepared = _funnel_prepared(tmp_path)
    down = {"value": False}

    def status():
        if down["value"]:
            raise gateway_launch.GatewayLaunchError("tailscale_not_running")
        return _status()

    fake, clock = _Healing(prepared, status=status), _Clock()
    task = asyncio.create_task(_heal_serve(prepared, fake, clock, restart_delays=(1.0, 2.0, 4.0)))
    await _out_until(capsys, "gateway_funnel_open")
    down["value"] = True
    fake.children[0].exit_code = 0
    assert await asyncio.wait_for(task, 10) == 1
    out = capsys.readouterr().out
    assert clock.backoffs() == [1.0, 2.0, 4.0]
    assert "gateway_funnel_restart_failed attempt 3/3 reason=tailscale_not_running" in out
    assert "gateway_funnel_restart_gave_up attempts=3; stopping the gateway" in out
    assert len(fake.children) == 1  # nothing started while Tailscale was unavailable


async def test_a_temporary_failure_is_retried_until_it_works(tmp_path, capsys):
    prepared = _funnel_prepared(tmp_path)
    answers = iter(["down", "down", "up"])
    state = {"armed": False}

    def status():
        if state["armed"] and next(answers, "up") == "down":
            raise gateway_launch.GatewayLaunchError("tailscale_query_failed")
        return _status()

    fake, clock = _Healing(prepared, status=status), _Clock()
    task = asyncio.create_task(_heal_serve(prepared, fake, clock, restart_delays=(1.0, 2.0, 4.0)))
    await _out_until(capsys, "gateway_funnel_open")
    state["armed"] = True
    fake.children[0].exit_code = 0
    out = await _out_until(capsys, "gateway_funnel_restarted attempt 3/3")
    assert out.count("gateway_funnel_restart_failed") == 2 and len(fake.children) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "status,reason",
    [
        (lambda: _status(name="other.tail1234.ts.net."), "gateway_public_url_not_this_machine"),
        (lambda: _status(caps=("https",)), "tailscale_funnel_not_permitted"),
    ],
)
async def test_changed_identity_or_permission_is_not_retried(tmp_path, capsys, status, reason):
    prepared = _funnel_prepared(tmp_path)
    changed = {"value": False}

    def current():
        return status() if changed["value"] else _status()

    fake, clock = _Healing(prepared, status=current), _Clock()
    task = asyncio.create_task(_heal_serve(prepared, fake, clock))
    await _out_until(capsys, "gateway_funnel_open")
    changed["value"] = True
    fake.children[0].exit_code = 0
    assert await asyncio.wait_for(task, 10) == 1
    out = capsys.readouterr().out
    assert f"gateway_funnel_restart_refused reason={reason}; stopping the gateway" in out
    assert len(fake.children) == 1 and clock.backoffs() == [5.0]


async def test_an_unconfirmed_old_child_blocks_a_second_one(tmp_path, capsys):
    prepared = _funnel_prepared(tmp_path)
    fake, clock = _Healing(prepared, stop_ok=False), _Clock()
    task = asyncio.create_task(_heal_serve(prepared, fake, clock))
    await _out_until(capsys, "gateway_funnel_open")
    fake.children[0].exit_code = 0
    assert await asyncio.wait_for(task, 10) == 1
    out = capsys.readouterr().out
    assert "gateway_funnel_restart_refused reason=gateway_funnel_stop_unconfirmed" in out
    assert len(fake.children) == 1 and not fake.children[0].closed


async def test_cancel_while_waiting_to_restart_stops_cleanly(tmp_path, capsys):
    prepared = _funnel_prepared(tmp_path)
    fake, clock = _Healing(prepared), _Clock(block_backoff=True)
    task = asyncio.create_task(_heal_serve(prepared, fake, clock))
    await _out_until(capsys, "gateway_funnel_open")
    fake.children[0].exit_code = 0
    await _out_until(capsys, "gateway_funnel_restarting attempt 1/")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(fake.children) == 1 and fake.children[0].closed
    assert "gateway_stopped" in capsys.readouterr().out


async def test_stable_running_resets_the_attempt_counter(tmp_path, capsys):
    prepared = _funnel_prepared(tmp_path)
    fake, clock = _Healing(prepared), _Clock()
    task = asyncio.create_task(
        _heal_serve(prepared, fake, clock, restart_delays=(1.0, 2.0), stable_seconds=1.0)
    )
    await _out_until(capsys, "gateway_funnel_open")
    fake.children[0].exit_code = 0
    out = await _out_until(capsys, "gateway_funnel_stable; restart attempts reset")
    fake.children[1].exit_code = 0
    for _ in range(1000):
        out += capsys.readouterr().out
        if out.count("gateway_funnel_restarted attempt 1/2") == 2:
            break
        await asyncio.sleep(0.01)
    assert out.count("gateway_funnel_restarting attempt 1/2") == 2
    assert "attempt 2/2" not in out
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_event_lines_are_timestamped(capsys):
    gateway_launch._event("gateway_funnel_open x")
    line = capsys.readouterr().out.rstrip("\n")
    stamp, _, name = line.partition(" gateway_funnel_open")
    assert name == " x" and len(stamp) == len("2026-11-18 02:35:12,345")
    assert stamp[4] == "-" and stamp[10] == " " and stamp[19] == ","
