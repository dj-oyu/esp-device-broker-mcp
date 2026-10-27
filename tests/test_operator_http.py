"""Local operator HTTP routes share broker state and cannot expose capabilities."""

import asyncio

import httpx2
from test_authenticated_mock import authenticated_mock as authenticated_mock
from test_http_integration import service as service
from test_registry_http import RegistryService
from test_registry_http import registry_service as registry_service

from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import Broker
from cardputer_mcp.client import call, connect
from cardputer_mcp.models import Origin

LOCAL = "a" * 64
REMOTE = "b" * 64


async def test_operator_is_locked_on_unauthenticated_mock(service: str) -> None:
    async with httpx2.AsyncClient(trust_env=False) as http:
        response = await http.get(service.removesuffix("/mcp") + "/operator/state")
        assert response.status_code == 401


async def test_operator_requires_local_control_for_every_route(
    registry_service: RegistryService,
) -> None:
    service = registry_service
    read_only = service.registry.issue("Local reader", Origin.LOCAL, scopes={"read"})
    base = service.url.removesuffix("/mcp") + "/operator"
    async with httpx2.AsyncClient(trust_env=False) as http:
        for credential in (REMOTE, service.keys["second"].token, read_only.token):
            headers = {"Authorization": f"Bearer {credential}"}
            state = await http.get(base + "/state", headers=headers)
            if credential == read_only.token:
                # A local read-only key may read; every action below still needs control.
                assert state.status_code == 200 and state.json()["ok"]
            else:
                assert state.status_code == 403
            for route, payload in (
                ("cancel", {"request_id": "unknown"}),
                ("reorder", {"request_id": "unknown", "position": 1}),
                ("release", {"lease_id": "unknown"}),
            ):
                response = await http.post(base + "/" + route, json=payload, headers=headers)
                assert response.status_code == 403
        response = await http.get(base + "/state", headers={"Authorization": f"Bearer {LOCAL}"})
        assert response.status_code == 200 and response.json()["ok"]
        assert response.headers["cache-control"] == "no-store"
    assert service.resident.session is None


async def test_operator_snapshot_reorder_cancel_and_release_share_resident(
    authenticated_mock: tuple[str, Broker],
) -> None:
    url, resident = authenticated_mock
    base = url.removesuffix("/mcp") + "/operator"
    async with (
        connect(url, LOCAL) as local,
        connect(url, REMOTE) as remote,
        httpx2.AsyncClient(trust_env=False, headers={"Authorization": f"Bearer {LOCAL}"}) as http,
    ):
        held = await call(
            local, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 30}
        )
        first = await call(remote, "session", {"action": "open", "owner": "remote_mcp"})
        second = await call(remote, "session", {"action": "open", "owner": "remote_mcp"})
        one = await call(
            remote, "session", {"action": "request", "resume_token": first["resume_token"]}
        )
        two = await call(
            remote, "session", {"action": "request", "resume_token": second["resume_token"]}
        )
        one_id, two_id = one["request"]["request_id"], two["request"]["request_id"]
        response = await http.get(base + "/state")
        snapshot = response.json()
        assert snapshot["ok"] and snapshot["backend"] == "mock"
        assert [row["request_id"] for row in snapshot["waiters"]] == [one_id, two_id]
        assert [row["position"] for row in snapshot["waiters"]] == [1, 2]
        for secret in (
            held["session"],
            first["resume_token"],
            second["resume_token"],
            LOCAL,
            REMOTE,
        ):
            assert secret not in response.text
        lease_id = snapshot["owner"]["lease_id"]
        stale_revision = snapshot["queue_revision"]
        for _ in range(2):
            changed = await http.post(
                base + "/reorder",
                json={
                    "request_id": two_id,
                    "position": 1,
                    "expected_revision": snapshot["queue_revision"],
                },
            )
            assert changed.json()["ok"]
            stale_reorder = await http.post(
                base + "/reorder",
                json={"request_id": one_id, "position": 1, "expected_revision": stale_revision},
            )
            assert stale_reorder.json() == {"ok": False, "error": "stale_operator_snapshot"}
            snapshot = (await http.get(base + "/state")).json()
            assert [row["request_id"] for row in snapshot["waiters"]] == [two_id, one_id]
        snapshot = (await http.get(base + "/state")).json()
        assert [row["request_id"] for row in snapshot["waiters"]] == [two_id, one_id]
        cancelled = await http.post(base + "/cancel", json={"request_id": two_id})
        assert cancelled.json()["ok"] and cancelled.json()["cancelled"]
        repeated = await http.post(base + "/cancel", json={"request_id": two_id})
        assert repeated.status_code == 200
        snapshot = (await http.get(base + "/state")).json()
        assert [row["request_id"] for row in snapshot["waiters"]] == [one_id]
        released = await http.post(base + "/release", json={"lease_id": lease_id})
        assert released.json()["ok"] and released.json()["released"]
        resumed = await call(
            remote, "status", {"view": "session", "resume_token": first["resume_token"]}
        )
        assert resumed["lease"] is not None
        assert resident.session is not None
        assert resident.session.id == resumed["lease"]["lease_id"]
        stale = await http.post(base + "/release", json={"lease_id": lease_id})
        assert stale.json() == {"ok": False, "error": "lease_not_found"}
        assert resident.session.id == resumed["lease"]["lease_id"]
        await call(remote, "session", {"action": "close", "resume_token": first["resume_token"]})
        await call(remote, "session", {"action": "close", "resume_token": second["resume_token"]})


async def test_operator_cannot_release_an_active_job(
    authenticated_mock: tuple[str, Broker],
) -> None:
    url, resident = authenticated_mock
    assert isinstance(resident.backend, MockBackend)
    backend = resident.backend
    backend.gate = asyncio.Event()
    base = url.removesuffix("/mcp") + "/operator"
    try:
        async with (
            connect(url, LOCAL) as local,
            httpx2.AsyncClient(
                trust_env=False, headers={"Authorization": f"Bearer {LOCAL}"}
            ) as http,
        ):
            held = await call(
                local, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 30}
            )
            prepared = await call(
                local,
                "submit",
                {
                    "kind": "diagnostics",
                    "session": held["session"],
                    "job_id": "operator-active",
                },
            )
            executed = await call(
                local,
                "execute",
                {"op": "run", "session": held["session"], "plan_hash": prepared["plan_hash"]},
            )
            assert executed["ok"]
            async with asyncio.timeout(5):
                while backend.active != 1:
                    await asyncio.sleep(0.01)
            snapshot = (await http.get(base + "/state")).json()
            assert snapshot["owner"]["release_blocked"]
            lease_id = snapshot["owner"]["lease_id"]
            rejected = await http.post(base + "/release", json={"lease_id": lease_id})
            assert rejected.json() == {"ok": False, "error": "active_job_cannot_release"}
            assert backend.active == 1
            assert resident.session is not None and resident.session.id == lease_id
            backend.gate.set()
            await resident.queue.join()
            done = await call(local, "status", {"view": "job", "job_id": "operator-active"})
            assert done["state"] == "succeeded"
            assert (await http.post(base + "/release", json={"lease_id": lease_id})).json()["ok"]
    finally:
        backend.gate.set()


async def test_operator_strict_json_limits_and_loopback_defenses(
    authenticated_mock: tuple[str, Broker],
) -> None:
    url, resident = authenticated_mock
    base = url.removesuffix("/mcp") + "/operator"
    marker = "SYNTHETIC_RAW_INPUT_NEVER_ECHO"
    async with httpx2.AsyncClient(
        trust_env=False, headers={"Authorization": f"Bearer {LOCAL}"}
    ) as http:
        for body in (
            "{}",
            f'{{"request_id":"unknown","extra":"{marker}"}}',
            f'{{"request_id":"unknown","request_id":"{marker}"}}',
            marker,
            "null",
        ):
            response = await http.post(
                base + "/cancel", content=body, headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 400
            assert marker not in response.text
        for position in (0, -1, True, "1", 1.0):
            response = await http.post(
                base + "/reorder",
                json={"request_id": "unknown", "position": position, "expected_revision": LOCAL},
            )
            assert response.status_code == 400
        for payload in (
            {"request_id": "unknown", "position": 1},
            {"request_id": "unknown", "position": 1, "expected_revision": "invalid"},
            {"request_id": "unknown", "position": 1, "expected_revision": True},
        ):
            assert (await http.post(base + "/reorder", json=payload)).status_code == 400
        too_big = await http.post(
            base + "/cancel", content=" " * 2049, headers={"Content-Type": "application/json"}
        )
        assert too_big.status_code == 413
        query = await http.get(base + "/state", params={"token": marker})
        assert query.status_code == 400 and marker not in query.text
        for headers in (
            {"Origin": "https://hostile.invalid"},
            {"X-Forwarded-For": "127.0.0.1"},
            {"Host": "hostile.invalid"},
        ):
            assert (await http.get(base + "/state", headers=headers)).status_code == 403
        duplicate = await http.get(
            base + "/state",
            headers=[("Origin", "http://localhost"), ("Origin", "http://localhost")],
        )
        assert duplicate.status_code == 403
        cookie = await http.get(
            base + "/state", headers={"Authorization": "", "Cookie": f"token={LOCAL}"}
        )
        assert cookie.status_code == 401
        wrong_type = await http.post(base + "/cancel", content="{}")
        assert wrong_type.status_code == 403
        assert (await http.get(base + "/release")).status_code == 405
    assert resident.session is None
