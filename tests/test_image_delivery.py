"""Completed image reads require their current authenticated recipient; no decisions."""

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from test_capture_broker import PRINCIPAL, Clock, acquire, enqueue, running, unavailable
from test_capture_http import assert_image, assert_unavailable, capture, read_capture
from test_registry_http import LOCAL, REMOTE, RegistryService
from test_registry_http import registry_service as registry_service

from cardputer_mcp import server as server_module
from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.client import call, connect
from cardputer_mcp.models import LogRequest, Origin


async def completed_preview(broker: Broker, job_id: str = "review") -> dict[str, Any]:
    session = acquire(broker)
    enqueue(broker, session, job_id)
    await broker.queue.join()
    return broker.operator_image(job_id)


async def test_requested_capture_is_available_to_only_its_authenticated_recipient(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as broker:
        assert broker.operator_images()["images"] == []
        preview = await completed_preview(broker)
        unavailable(broker, "review", "another-principal")
        pixels = base64.b64decode(preview["image_base64"], validate=True)
        assert pixels.startswith(b"\x89PNG\r\n\x1a\n")
        assert preview["sha256"] == hashlib.sha256(pixels).hexdigest()
        assert preview["sha256"] == preview["metadata"]["sha256"]
        assert preview["source"] == "synthetic_fixture"
        assert preview["metadata"]["is_device_capture"] is False
        assert preview["recipient_principal"] == PRINCIPAL
        assert preview["born_utc"]
        assert preview["remaining_seconds"] == 300.0
        assert preview["delivery_state"] == "available"
        rows = broker.operator_images()["images"]
        assert len(rows) == 1
        assert "image_base64" not in rows[0] and "metadata" not in rows[0]
        assert preview["image_base64"] not in json.dumps(rows)
        for _ in range(3):
            broker.operator_images()
            broker.operator_image("review")
            assert broker.read_capture("review", PRINCIPAL).png == pixels
        assert broker.backend.calls == 1 and broker.queue.empty()


@pytest.mark.parametrize("decision", ["approve", "reject", "", "yes", "approved", "Approve"])
@pytest.mark.parametrize("field", [None, "sha256", "delivery_revision", "job_id"])
async def test_retired_decision_cannot_mutate_any_image_or_create_a_grant(
    tmp_path: Path, decision: str, field: str | None
) -> None:
    async with running(tmp_path) as broker:
        preview = await completed_preview(broker)
        if field is not None:
            preview[field] = "0" * 64 if field != "job_id" else "unknown"
        original = broker.captures["review"]
        for _ in range(2):
            with pytest.raises(BrokerError, match="^approval_interface_retired$"):
                broker.operator_decide_image(
                    preview["job_id"], preview["sha256"], preview["delivery_revision"], decision
                )
        assert broker.captures["review"] is original
        assert original.delivery_state == "available"
        assert broker.read_capture("review", PRINCIPAL).png
        unavailable(broker, "unknown")
        assert broker.backend.calls == 1


@pytest.mark.parametrize("changed", ["pixels", "recipient", "origin", "born", "deadline", "epoch"])
async def test_read_only_preview_revision_binds_content_recipient_lifetime_and_epoch(
    tmp_path: Path, changed: str
) -> None:
    async with running(tmp_path) as broker:
        preview = await completed_preview(broker)
        retained = broker.captures["review"]
        if changed == "epoch":
            broker.epoch += "-restarted"
        else:
            fields = {
                "pixels": {"image": replace(retained.image, png=retained.image.png + b"changed")},
                "recipient": {"principal_id": "replacement-principal"},
                "origin": {"origin": Origin.LOCAL.value},
                "born": {"born_utc": "different-capture-time"},
                "deadline": {"deadline": retained.deadline + 1},
            }[changed]
            broker.captures["review"] = replace(retained, **fields)
        assert broker.operator_image("review")["delivery_revision"] != preview["delivery_revision"]
        assert broker.backend.calls == 1


async def test_previews_and_authenticated_reads_never_refresh_retention(tmp_path: Path) -> None:
    clock = Clock()
    async with running(tmp_path, clock=clock) as broker:
        await completed_preview(broker)
        for now in (200.0, 399.0, 399.999):
            clock.now = now
            assert broker.operator_image("review")["remaining_seconds"] > 0
            assert broker.read_capture("review", PRINCIPAL).png
            assert broker.captures["review"].deadline == 400.0
        clock.now = 400.0
        unavailable(broker, "review")
        with pytest.raises(BrokerError, match="^capture_not_available$"):
            broker.operator_image("review")
        assert broker.operator_images()["images"] == []
        assert broker.job("review")["state"] == "succeeded"
        assert broker.backend.calls == 1


async def test_restart_discards_pixels_without_replay(tmp_path: Path) -> None:
    async with running(tmp_path) as original:
        await completed_preview(original)
        epoch = original.epoch
    async with running(tmp_path) as restarted:
        assert restarted.epoch != epoch
        assert restarted.operator_images()["images"] == []
        unavailable(restarted, "review")
        assert restarted.job("review")["state"] == "succeeded"
        await asyncio.sleep(0)
        assert restarted.backend.calls == 0 and restarted.queue.empty()


@pytest.mark.parametrize("mutation", ["missing", "failed"])
async def test_absent_or_unsuccessful_job_never_releases_pixels(
    tmp_path: Path, mutation: str
) -> None:
    async with running(tmp_path) as broker:
        await completed_preview(broker)
        if mutation == "missing":
            broker._store().db.execute("DELETE FROM jobs WHERE id=?", ("review",))
        else:
            broker._store().set_job("review", "failed", "synthetic_failure")
        unavailable(broker, "review")
        with pytest.raises(BrokerError, match="^capture_not_available$"):
            broker.operator_image("review")
        assert broker.operator_images()["images"] == []


@pytest.mark.parametrize("outcome", [False, None, 1, "true", "exception", "absent"])
async def test_current_recipient_authorization_requires_literal_true(
    tmp_path: Path, outcome: object
) -> None:
    async with running(tmp_path) as broker:
        await completed_preview(broker)
        calls = []

        def validate(principal: str, origin: str, scopes: frozenset[str]) -> object:
            calls.append((principal, origin, scopes))
            if outcome == "exception":
                raise RuntimeError("SYNTHETIC_CALLBACK_ERROR_NOT_DISCLOSED")
            return outcome

        broker.principal_valid = None if outcome == "absent" else validate
        unavailable(broker, "review")
        if outcome != "absent":
            assert calls == [(PRINCIPAL, Origin.REMOTE.value, frozenset({"read"}))]
        broker.principal_valid = lambda *args: True
        assert broker.read_capture("review", PRINCIPAL).png
        assert broker.backend.calls == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "expire",
        "remove",
        "recipient",
        "pixels",
        "origin",
        "deadline",
        "born",
        "job",
        "epoch",
        "closing",
        "replace-validator",
        "delete-job",
    ],
)
async def test_read_revalidates_after_recipient_callback(tmp_path: Path, mutation: str) -> None:
    clock = Clock()
    async with running(tmp_path, clock=clock) as broker:
        await completed_preview(broker)

        def validate(*args):
            retained = broker.captures["review"]
            if mutation == "expire":
                clock.now = retained.deadline
            elif mutation == "remove":
                del broker.captures["review"]
            elif mutation == "recipient":
                broker.captures["review"] = replace(retained, principal_id="changed-recipient")
            elif mutation == "pixels":
                broker.captures["review"] = replace(
                    retained, image=replace(retained.image, png=b"different-pixels")
                )
            elif mutation == "origin":
                broker.captures["review"] = replace(retained, origin=Origin.LOCAL.value)
            elif mutation == "deadline":
                broker.captures["review"] = replace(retained, deadline=retained.deadline + 1)
            elif mutation == "born":
                broker.captures["review"] = replace(retained, born_utc="replacement-time")
            elif mutation == "job":
                broker._store().set_job("review", "failed", "synthetic_failure")
            elif mutation == "epoch":
                broker.epoch += "changed"
            elif mutation == "closing":
                broker.closing = True
            elif mutation == "replace-validator":
                broker.principal_valid = lambda *args: True
            else:
                broker._store().db.execute("DELETE FROM jobs WHERE id=?", ("review",))
            return True

        broker.principal_valid = validate
        unavailable(broker, "review")
        assert broker.backend.calls == 1


async def test_inventory_is_bounded_and_evicted_pixels_cannot_be_read(tmp_path: Path) -> None:
    async with running(tmp_path) as broker:
        session = acquire(broker)
        for index in range(9):
            enqueue(broker, session, f"review-{index}")
            await broker.queue.join()
            assert len(broker.operator_images()["images"]) <= 8
        assert len(broker.operator_images()["images"]) == 8
        unavailable(broker, "review-0")
        with pytest.raises(BrokerError, match="^capture_not_available$"):
            broker.operator_image("review-0")
        assert broker.backend.calls == 9


async def test_identical_pixels_remain_bound_to_their_requested_job(tmp_path: Path) -> None:
    async with running(tmp_path) as broker:
        session = acquire(broker)
        for job_id in ("first-image", "second-image"):
            enqueue(broker, session, job_id)
            await broker.queue.join()
        first = broker.operator_image("first-image")
        second = broker.operator_image("second-image")
        assert first["sha256"] == second["sha256"]
        assert first["delivery_revision"] != second["delivery_revision"]
        assert broker.read_capture("first-image", PRINCIPAL).png
        assert broker.read_capture("second-image", PRINCIPAL).png
        unavailable(broker, "missing-image")
        assert broker.backend.calls == 2 and broker.queue.empty()


async def test_delivery_http_immediate_read_and_retired_decisions_without_recapture(
    registry_service: RegistryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = registry_service
    monkeypatch.setattr(server_module, "time", lambda: service.now[0])
    base = service.url.removesuffix("/mcp") + "/operator"
    async with (
        connect(service.url, service.keys["first"].token) as remote,
        httpx2.AsyncClient(
            trust_env=False, headers={"Authorization": f"Bearer {LOCAL}"}
        ) as operator,
    ):
        assert (await operator.get(base + "/images")).json()["images"] == []
        tools = {tool.name for tool in (await remote.list_tools()).tools}
        assert tools == {"status", "submit", "execute", "flash", "cancel", "session"}
        assert not any("approv" in tool or "operator" in tool for tool in tools)
        lease = await call(
            remote, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
        )
        plan = await capture(remote, "http-review", lease["session"])
        await service.resident.queue.join()
        pixels = assert_image(await read_capture(remote, "http-review"))
        response = await operator.post(base + "/image", json={"job_id": "http-review"})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        preview = response.json()
        assert preview["delivery_state"] == "available"
        assert preview["recipient_principal"] == service.keys["first"].metadata.principal_id
        assert base64.b64decode(preview["image_base64"], validate=True) == pixels
        inventory = await operator.get(base + "/images")
        assert inventory.headers["cache-control"] == "no-store"
        assert "image_base64" not in inventory.text
        payload = {field: preview[field] for field in ("job_id", "sha256", "delivery_revision")}
        for decision in ("approve", "reject", True, "unsupported"):
            response = await operator.post(
                base + "/image-decision", json={**payload, "decision": decision}
            )
            assert response.status_code == 410
            assert response.json() == {"error": "request_rejected"}
            assert preview["image_base64"] not in response.text
            assert assert_image(await read_capture(remote, "http-review")) == pixels
        replay = await call(
            remote,
            "execute",
            {"op": "run", "session": lease["session"], "plan_hash": plan["plan_hash"]},
        )
        assert replay["deduplicated"] and service.backend.calls == 1
        public = json.dumps(
            [
                service.resident.status(),
                service.resident.job("http-review"),
                service.resident.read_logs(LogRequest()),
            ]
        )
        history = "\n".join(service.resident._store().db.iterdump())
        for forbidden in (
            preview["image_base64"],
            pixels.hex(),
            lease["session"],
            *(key.token for key in service.keys.values()),
            LOCAL,
            REMOTE,
        ):
            assert forbidden not in public and forbidden not in history


@pytest.mark.parametrize("invalidation", ["revoke", "expire", "rotate"])
async def test_delivery_http_rechecks_recipient_credential_on_every_read(
    registry_service: RegistryService, monkeypatch: pytest.MonkeyPatch, invalidation: str
) -> None:
    service = registry_service
    monkeypatch.setattr(server_module, "time", lambda: service.now[0])
    key = service.keys["first"]
    async with connect(service.url, key.token) as remote:
        lease = await call(
            remote, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
        )
        await capture(remote, "changed-recipient", lease["session"])
        await service.resident.queue.join()
        assert_image(await read_capture(remote, "changed-recipient"))
    if invalidation == "revoke":
        assert service.registry.revoke(key.metadata.key_id)
    elif invalidation == "expire":
        service.now[0] += 11
    else:
        replacement = service.registry.rotate(key.metadata.key_id)
        async with connect(service.url, replacement.token) as stranger:
            assert_unavailable(await read_capture(stranger, "changed-recipient"))
    with pytest.raises(BrokerError, match="^capture_not_available$"):
        service.resident.read_capture("changed-recipient", key.metadata.principal_id)
    assert service.backend.calls == 1


async def test_delivery_http_all_routes_require_local_control_and_existing_loopback_guards(
    registry_service: RegistryService,
) -> None:
    service = registry_service
    read_only = service.registry.issue("Local image observer", Origin.LOCAL, scopes={"read"})
    base = service.url.removesuffix("/mcp") + "/operator"
    routes = [
        ("GET", "/images", None),
        ("POST", "/image", {"job_id": "unknown"}),
        (
            "POST",
            "/image-decision",
            {
                "job_id": "unknown",
                "sha256": "0" * 64,
                "delivery_revision": "0" * 64,
                "decision": "approve",
            },
        ),
    ]
    async with httpx2.AsyncClient(trust_env=False) as http:
        for method, path, payload in routes:
            for token in (
                REMOTE,
                service.keys["second"].token,
                service.keys["reader"].token,
                read_only.token,
            ):
                response = await http.request(
                    method, base + path, json=payload, headers={"Authorization": f"Bearer {token}"}
                )
                assert response.status_code == 403
            assert (await http.request(method, base + path, json=payload)).status_code == 401
            for guards in (
                {"Origin": "https://hostile.invalid"},
                {"Host": "hostile.invalid"},
                {"Forwarded": "for=127.0.0.1"},
                {"X-Forwarded-For": "127.0.0.1"},
            ):
                response = await http.request(
                    method,
                    base + path,
                    json=payload,
                    headers={"Authorization": f"Bearer {LOCAL}", **guards},
                )
                assert response.status_code == 403
    assert service.backend.calls == 0 and service.resident.queue.empty()
    assert not service.resident.captures
