"""Independent adversarial review: synthetic processes/streams, never cloud or USB."""

import threading
from datetime import UTC, datetime

import pytest

from cardputer_mcp import tunnel_manager
from cardputer_mcp.gui_tunnel import TunnelController, TunnelView, perform_tunnel
from cardputer_mcp.tunnel import TunnelSettings
from cardputer_mcp.windows_process import WindowsProcessError

API_KEY = "SYNTHETIC_REVIEW_RUNTIME_KEY"
BEARER = "Bearer " + "d" * 64
PRIVATE = "SYNTHETIC_REVIEW_OPERATOR_SECRET"
NOW = 1_800_000_000.0


class ReviewProcess:
    def __init__(self):
        self.exit_code = None
        self.members = 1
        self.stoppable = True
        self.close_fails = False
        self.stops = 0
        self.closes = 0

    def poll(self):
        return self.exit_code

    def active_count(self):
        return self.members

    def stop(self, timeout):
        assert timeout <= 5
        self.stops += 1
        if self.stoppable:
            self.exit_code, self.members = 0, 0
        return self.stoppable

    def close(self):
        self.closes += 1
        assert self.members == 0
        if self.close_fails:
            raise RuntimeError(PRIVATE)


class ReviewLock:
    def __init__(self):
        self.held = False
        self.releases = 0
        self.release_fails = False

    def acquire(self, *, timeout):
        assert timeout == 0
        assert not self.held
        self.held = True

    def release(self):
        self.releases += 1
        if self.release_fails:
            raise RuntimeError(PRIVATE)
        self.held = False


def identity():
    # A running broker job is deliberately part of every positive fixture.
    return {
        "ok": True,
        "authenticated": True,
        "origin": "remote_mcp",
        "scopes": ["read"],
        "tunnel_ingress_enabled": False,
        "backend": "mock",
        "status": {
            "ok": True,
            "backend": "mock",
            "active_job": "review-existing-job",
            "owner": {"lease_id": "review-existing-lease"},
            "needs_review": True,
            "hardware_connected": True,
        },
    }


@pytest.fixture
def review_manager(tmp_path, monkeypatch):
    broker = tmp_path / "broker.toml"
    broker.write_text('backend = "mock"\nport = 28765\n', encoding="utf-8")
    settings = TunnelSettings(
        client_exe=tmp_path / "tunnel-client.exe",
        client_archive=tmp_path / "client.zip",
        broker_config=broker,
        tunnel_id="tunnel_" + "1" * 32,
        health_port=28766,
    )
    process, lock, launches, events = ReviewProcess(), ReviewLock(), [], []

    def spawn(args, env, cwd):
        assert lock.held
        assert PRIVATE not in repr(env)
        assert API_KEY not in repr(args) and BEARER not in repr(args)
        launches.append((args, env, cwd))
        return process

    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", PRIVATE)
    manager = tunnel_manager.TunnelManager(
        process_factory=spawn,
        broker_probe=lambda url, bearer: identity(),
        health_probe=lambda port: {"ready_status": 200},
        verify_client=lambda settings: None,
        listener_check=lambda port: None,
        lock_factory=lambda: lock,
        clock=lambda: NOW,
        monotonic=lambda: 100.0,
        on_event=events.append,
    )
    result = manager.configure(settings, "http://127.0.0.1:28765/mcp")
    assert result["configured"] and not launches
    yield manager, settings, process, lock, launches, events
    process.stoppable = True
    process.close_fails = False
    lock.release_fails = False
    assert manager.close()


def test_overlapping_connect_configure_poll_and_close_never_duplicate_launch(review_manager):
    manager, settings, process, lock, launches, _ = review_manager
    entered, release = threading.Event(), threading.Event()
    completed = []

    def blocked_probe(url, bearer):
        entered.set()
        assert release.wait(3), "review fixture was not released"
        return identity()

    manager._broker_probe = blocked_probe
    thread = threading.Thread(target=lambda: completed.append(manager.connect(API_KEY, BEARER)))
    thread.start()
    try:
        assert entered.wait(2)
        assert manager.snapshot()["busy"]
        assert manager.connect(API_KEY, BEARER)["status_code"] == "tunnel_busy"
        changed = settings.model_copy(update={"health_port": 28767})
        assert manager.configure(changed, "http://127.0.0.1:28765/mcp")["busy"]
        assert manager.poll()["busy"]
        assert manager.disconnect()["busy"]
        assert manager.close() is False
        assert not launches and process.stops == 0 and lock.held
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert completed[0]["process_alive"]
    assert len(launches) == 1
    assert "--health.listen-addr=127.0.0.1:28766" in launches[0][0]
    assert launches[0][1] == {}  # Child env object is cleared after launch.
    assert manager.close()
    assert process.stops == 1 and process.closes == 1 and not lock.held


def test_uncertain_partial_launch_keeps_owner_and_lock_until_retry(review_manager):
    manager, settings, process, lock, launches, events = review_manager
    process.stoppable = False

    def failed_spawn(args, env, cwd):
        launches.append((args, env, cwd))
        raise WindowsProcessError(PRIVATE, owned=process)

    manager._factory = failed_spawn
    result = manager.connect(API_KEY, BEARER)
    assert result["ownership_uncertain"] and result["owned"] and lock.held
    assert PRIVATE not in repr(result) + repr(events) + repr(manager)
    assert launches[0][1] == {}
    assert manager.configure(settings, "http://127.0.0.1:28765/mcp")["owned"]
    assert manager.connect(API_KEY, BEARER)["owned"]
    assert len(launches) == 1 and lock.releases == 0
    assert manager.close() is False
    assert process.closes == 0 and lock.held
    process.stoppable = True
    assert manager.close()
    assert process.closes == 1 and lock.releases == 1 and not lock.held


@pytest.mark.parametrize("failure", ["handle_close", "lock_release"])
def test_release_failure_is_not_success_and_prevents_new_launch(review_manager, failure):
    manager, _, process, lock, launches, events = review_manager
    assert manager.connect(API_KEY, BEARER)["process_alive"]
    process.close_fails = failure == "handle_close"
    lock.release_fails = failure == "lock_release"
    result = manager.disconnect()
    assert result["ownership_uncertain"] and result["owned"] and lock.held
    assert not result["cloud_poll_observed"] and not result["local_readiness"]
    assert PRIVATE not in repr(result) + repr(events)
    manager.connect(API_KEY, BEARER)
    assert len(launches) == 1
    process.close_fails = lock.release_fails = False
    assert manager.close()


def test_exit_during_health_request_discards_success_and_stops_remaining_subtree(review_manager):
    manager, _, process, lock, launches, _ = review_manager
    assert manager.connect(API_KEY, BEARER)["process_alive"]
    timestamp = datetime.fromtimestamp(NOW, UTC).isoformat().replace("+00:00", "Z")

    def stale_health(port):
        process.exit_code = 23
        process.members = 2  # Descendants outlive the exited leader.
        return {
            "ready_status": 200,
            "control_status": 200,
            "control_plane": {
                "schema_version": 1,
                "component": "control-plane",
                "snapshot_at": timestamp,
                "status": "ok",
                "state": "polling",
                "details": {"last_success": timestamp, "consecutive_failures": 0},
            },
        }

    manager._health_probe = stale_health
    result = manager.poll()
    assert not result["process_alive"] and not result["owned"]
    assert not result["local_readiness"] and not result["cloud_poll_observed"]
    assert not result["app_verified"]
    assert process.stops == process.closes == 1 and not lock.held
    for _ in range(3):
        manager.poll()
    assert len(launches) == 1  # A completed poll never schedules reconnect.


def test_real_manager_gui_contract_retains_uncertain_owner_and_hides_credentials(review_manager):
    manager, settings, process, lock, _, _ = review_manager
    model = TunnelController()
    model.view = TunnelView.parse(manager.snapshot())
    operation = model.connect(API_KEY, BEARER)
    assert operation is not None
    assert API_KEY not in repr(operation) and BEARER not in repr(operation)
    result = perform_tunnel(
        manager, operation, "http://127.0.0.1:28765/mcp", settings.broker_config.parent
    )
    assert operation.api_key == operation.bearer == ""
    assert model.accept(result)
    assert not model.failed and model.view.process_alive and model.view.owned
    assert model.view.application_verified is None
    assert model.needs_close_confirmation() and not model.can_edit()

    process.stoppable = False
    operation = model.restart()
    assert operation is not None and model.restart() is None
    result = perform_tunnel(
        manager, operation, "http://127.0.0.1:28765/mcp", settings.broker_config.parent
    )
    assert model.accept(result)
    assert model.failed and model.view.needs_review and model.stop_unconfirmed
    assert not model.prompt_after_stop and not model.can_connect() and lock.held
    assert PRIVATE not in repr(result) + model.message
