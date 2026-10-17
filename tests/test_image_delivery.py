"""Default image delivery needs a separate local decision on synthetic pixels only."""

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


def decide(broker: Broker, preview: dict[str, Any], decision: str) -> dict[str, Any]:
    return broker.operator_decide_image(
        preview["job_id"], preview["sha256"], preview["delivery_revision"], decision
    )


def pending(broker: Broker, job_id: str) -> None:
    with pytest.raises(BrokerError, match="^capture_delivery_approval_required$"):
        broker.read_capture(job_id, PRINCIPAL)


async def completed_preview(broker: Broker, job_id: str = "review") -> dict[str, Any]:
    session = acquire(broker)
    enqueue(broker, session, job_id)
    await broker.queue.join()
    return broker.operator_image(job_id)


async def test_final_recipient_callback_cannot_change_delivery_revision(tmp_path: Path) -> None:
    async with running(tmp_path) as broker:
        preview = await completed_preview(broker)

        def changed_epoch(*args):
            broker.epoch = "9" * 32
            return True

        broker.principal_valid = changed_epoch
        with pytest.raises(BrokerError, match="^stale_image_preview$"):
            decide(broker, preview, "approve")
        assert broker.captures[preview["job_id"]].delivery_state == "pending"


async def test_completed_capture_stays_private_until_exact_local_delivery_approval(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as broker:
        assert broker.operator_images()["images"] == []
        assert broker.backend.calls == 0
        preview = await completed_preview(broker)
        pending(broker, "review")
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
        assert preview["delivery_state"] == "pending"
        rows = broker.operator_images()["images"]
        assert len(rows) == 1
        assert "image_base64" not in rows[0] and "metadata" not in rows[0]
        assert preview["image_base64"] not in json.dumps(rows)
        for _ in range(3):
            broker.operator_images()
            broker.operator_image("review")
            pending(broker, "review")
        assert broker.backend.calls == 1
        assert decide(broker, preview, "approve") == {
            "job_id": "review",
            "delivery_state": "approved",
        }
        assert broker.read_capture("review", PRINCIPAL).png == pixels
        unavailable(broker, "review", "another-principal")
        assert broker.backend.calls == 1 and broker.queue.empty()


@pytest.mark.parametrize("decision,state", [("approve", "approved"), ("reject", "rejected")])
async def test_delivery_decision_is_final_and_idempotent_after_lost_reply(
    tmp_path: Path, decision: str, state: str
) -> None:
    async with running(tmp_path) as broker:
        preview = await completed_preview(broker)
        # The client may lose the response after the resident broker commits it.
        decide(broker, preview, decision)
        for _ in range(2):
            assert decide(broker, preview, decision) == {
                "job_id": "review",
                "delivery_state": state,
            }
        opposite = "reject" if decision == "approve" else "approve"
        with pytest.raises(BrokerError, match="^image_decision_final$"):
            decide(broker, preview, opposite)
        if state == "rejected":
            for _ in range(2):
                with pytest.raises(BrokerError, match="^capture_delivery_rejected$"):
                    broker.read_capture("review", PRINCIPAL)
        else:
            assert broker.read_capture("review", PRINCIPAL).png
        unavailable(broker, "review", "another-principal")
        assert broker.backend.calls == 1 and broker.queue.empty()


@pytest.mark.parametrize("field", ["sha256", "delivery_revision", "job_id"])
async def test_delivery_rejects_wrong_digest_revision_or_job_without_mutation(
    tmp_path: Path, field: str
) -> None:
    async with running(tmp_path) as broker:
        preview = await completed_preview(broker)
        altered = {**preview, field: "0" * 64 if field != "job_id" else "unknown"}
        error = "capture_not_available" if field == "job_id" else "stale_image_preview"
        with pytest.raises(BrokerError, match=f"^{error}$"):
            decide(broker, altered, "approve")
        pending(broker, "review")
        assert broker.backend.calls == 1


@pytest.mark.parametrize("decision", ["", "yes", "approved", "rejected", "Approve"])
async def test_delivery_requires_an_explicit_supported_decision(
    tmp_path: Path, decision: str
) -> None:
    async with running(tmp_path) as broker:
        preview = await completed_preview(broker)
        with pytest.raises(BrokerError, match="^invalid_image_decision$"):
            decide(broker, preview, decision)
        pending(broker, "review")


@pytest.mark.parametrize("changed", ["pixels", "recipient", "origin", "born", "deadline", "epoch"])
async def test_delivery_preview_binds_cache_content_recipient_lifetime_and_broker(
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
        with pytest.raises(BrokerError, match="^stale_image_preview$"):
            decide(broker, preview, "approve")
        assert broker.captures["review"].delivery_state == "pending"
        assert broker.backend.calls == 1


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_delivery_preview_timeout_never_extends_five_minute_retention(
    tmp_path: Path, decision: str
) -> None:
    clock = Clock()
    async with running(tmp_path, clock=clock) as broker:
        preview = await completed_preview(broker)
        clock.now = 399.999
        assert broker.operator_image("review")["remaining_seconds"] == 0.001
        assert broker.captures["review"].deadline == 400.0
        clock.now = 400.0
        with pytest.raises(BrokerError, match="^capture_not_available$"):
            decide(broker, preview, decision)
        assert broker.operator_images()["images"] == []
        unavailable(broker, "review")
        assert broker.job("review")["state"] == "succeeded"
        assert broker.backend.calls == 1


async def test_delivery_approval_and_repeated_previews_do_not_refresh_expiry(
    tmp_path: Path,
) -> None:
    clock = Clock()
    async with running(tmp_path, clock=clock) as broker:
        preview = await completed_preview(broker)
        clock.now = 399.0
        decide(broker, preview, "approve")
        assert broker.operator_image("review")["remaining_seconds"] == 1
        assert broker.read_capture("review", PRINCIPAL).png
        assert broker.captures["review"].deadline == 400.0
        clock.now = 400.0
        unavailable(broker, "review")
        assert broker.backend.calls == 1


@pytest.mark.parametrize("state", ["pending", "approved", "rejected"])
async def test_restart_discards_pixels_and_image_decisions_without_replay(
    tmp_path: Path, state: str
) -> None:
    async with running(tmp_path) as original:
        preview = await completed_preview(original)
        epoch = original.epoch
        if state != "pending":
            decide(original, preview, "approve" if state == "approved" else "reject")
    async with running(tmp_path) as restarted:
        assert restarted.epoch != epoch
        assert restarted.operator_images()["images"] == []
        with pytest.raises(BrokerError, match="^capture_not_available$"):
            decide(restarted, preview, "approve")
        unavailable(restarted, "review")
        assert restarted.job("review")["state"] == "succeeded"
        await asyncio.sleep(0)
        assert restarted.backend.calls == 0 and restarted.queue.empty()


@pytest.mark.parametrize("mutation", ["missing", "failed"])
async def test_no_delivery_decision_when_completed_job_is_absent_or_not_succeeded(
    tmp_path: Path, mutation: str
) -> None:
    async with running(tmp_path) as broker:
        preview = await completed_preview(broker)
        if mutation == "missing":
            broker._store().db.execute("DELETE FROM jobs WHERE id=?", ("review",))
        else:
            broker._store().set_job("review", "failed", "synthetic_failure")
        with pytest.raises(BrokerError, match="^capture_not_available$"):
            decide(broker, preview, "approve")
        unavailable(broker, "review")
        assert broker.captures["review"].delivery_state == "pending"


@pytest.mark.parametrize("outcome", [False, None, 1, "true", "exception"])
async def test_image_recipient_validation_fails_closed(tmp_path: Path, outcome: object) -> None:
    calls = []
    async with running(tmp_path) as broker:
        preview = await completed_preview(broker)

        def validate(principal: str, origin: str, scopes: frozenset[str]) -> object:
            calls.append((principal, origin, scopes))
            if outcome == "exception":
                raise RuntimeError("SYNTHETIC_CALLBACK_ERROR_NOT_DISCLOSED")
            return outcome

        broker.principal_valid = validate
        with pytest.raises(BrokerError, match="^image_recipient_not_authorized$"):
            decide(broker, preview, "approve")
        assert calls == [(PRINCIPAL, Origin.REMOTE.value, frozenset({"read"}))]
        pending(broker, "review")
        assert decide(broker, preview, "reject")["delivery_state"] == "rejected"
        assert broker.backend.calls == 1


async def test_delivery_inventory_stays_bounded_and_evicted_preview_cannot_approve(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as broker:
        session = acquire(broker)
        for index in range(9):
            enqueue(broker, session, f"review-{index}")
            await broker.queue.join()
            if index == 0:
                oldest = broker.operator_image("review-0")
            assert len(broker.operator_images()["images"]) <= 8
        assert len(broker.operator_images()["images"]) == 8
        with pytest.raises(BrokerError, match="^capture_not_available$"):
            decide(broker, oldest, "approve")
        assert broker.backend.calls == 9


async def test_delivery_http_local_preview_approval_then_remote_read(
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
        inventory = await operator.get(base + "/images")
        assert inventory.status_code == 200 and inventory.json()["images"] == []
        assert service.backend.calls == 0
        tools = {tool.name for tool in (await remote.list_tools()).tools}
        assert "read_capture" in tools
        assert not any(
            "approv" in tool or "operator" in tool or "image_decision" in tool for tool in tools
        )
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        plan = await capture(remote, "http-review", lease["session"])
        await service.resident.queue.join()
        before = await read_capture(remote, "http-review")
        assert_unavailable(before, "capture_delivery_approval_required")
        preview_response = await operator.post(base + "/image", json={"job_id": "http-review"})
        assert preview_response.status_code == 200
        assert preview_response.headers["cache-control"] == "no-store"
        preview = preview_response.json()
        pixels = base64.b64decode(preview["image_base64"], validate=True)
        assert pixels.startswith(b"\x89PNG\r\n\x1a\n")
        assert preview["recipient_principal"] == service.keys["first"].metadata.principal_id
        assert preview["sha256"] == hashlib.sha256(pixels).hexdigest()
        assert preview["delivery_state"] == "pending"
        inventory = await operator.get(base + "/images")
        assert inventory.headers["cache-control"] == "no-store"
        assert "image_base64" not in inventory.text
        assert_unavailable(
            await read_capture(remote, "http-review"), "capture_delivery_approval_required"
        )
        payload = {field: preview[field] for field in ("job_id", "sha256", "delivery_revision")}
        payload["decision"] = "approve"
        for _ in range(2):
            response = await operator.post(base + "/image-decision", json=payload)
            assert response.json() == {
                "ok": True,
                "job_id": "http-review",
                "delivery_state": "approved",
            }
        assert assert_image(await read_capture(remote, "http-review")) == pixels
        replay = await call(
            remote, "execute_test", {"session": lease["session"], "plan_hash": plan["plan_hash"]}
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
        for path in service.resident.settings.state_dir.rglob("*"):
            # A shorter file cannot contain the complete PNG. In particular,
            # Windows denies reads of the broker's actively locked one-byte file.
            if path.is_file() and path.stat().st_size >= len(pixels):
                assert pixels not in path.read_bytes()


@pytest.mark.parametrize("invalidation", ["revoke", "expire", "rotate"])
async def test_delivery_http_rechecks_current_recipient_at_approval_time(
    registry_service: RegistryService, monkeypatch: pytest.MonkeyPatch, invalidation: str
) -> None:
    service = registry_service
    monkeypatch.setattr(server_module, "time", lambda: service.now[0])
    key = service.keys["first"]
    base = service.url.removesuffix("/mcp") + "/operator"
    async with connect(service.url, key.token) as remote:
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        await capture(remote, "changed-recipient", lease["session"])
        await service.resident.queue.join()
    async with httpx2.AsyncClient(
        trust_env=False, headers={"Authorization": f"Bearer {LOCAL}"}
    ) as operator:
        preview = (
            await operator.post(base + "/image", json={"job_id": "changed-recipient"})
        ).json()
        if invalidation == "revoke":
            assert service.registry.revoke(key.metadata.key_id)
        elif invalidation == "expire":
            service.now[0] += 11
        else:
            replacement = service.registry.rotate(key.metadata.key_id)
            async with connect(service.url, replacement.token) as stranger:
                assert_unavailable(await read_capture(stranger, "changed-recipient"))
        payload = {field: preview[field] for field in ("job_id", "sha256", "delivery_revision")}
        response = await operator.post(
            base + "/image-decision", json={**payload, "decision": "approve"}
        )
        assert response.json() == {"ok": False, "error": "image_recipient_not_authorized"}
        assert service.resident.captures["changed-recipient"].delivery_state == "pending"
        rejected = await operator.post(
            base + "/image-decision", json={**payload, "decision": "reject"}
        )
        assert rejected.json()["delivery_state"] == "rejected"
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


@pytest.mark.parametrize("mutation", ["expire", "remove", "recipient", "decision", "job"])
async def test_delivery_revalidates_after_recipient_metadata_lookup(
    tmp_path: Path, mutation: str
) -> None:
    clock = Clock()
    async with running(tmp_path, clock=clock) as broker:
        preview = await completed_preview(broker)

        def metadata_lookup(principal: str, origin: str, scopes: frozenset[str]) -> bool:
            retained = broker.captures["review"]
            if mutation == "expire":
                clock.now = retained.deadline
            elif mutation == "remove":
                del broker.captures["review"]
            elif mutation == "recipient":
                broker.captures["review"] = replace(retained, principal_id="changed-recipient")
            elif mutation == "decision":
                broker.captures["review"] = replace(retained, delivery_state="rejected")
            else:
                broker._store().set_job("review", "failed", "synthetic_failure")
            return True

        broker.principal_valid = metadata_lookup
        with pytest.raises(BrokerError):
            decide(broker, preview, "approve")
        assert all(item.delivery_state != "approved" for item in broker.captures.values())
        assert broker.backend.calls == 1


async def test_delivery_http_rejection_blocks_remote_pixels_without_recapture(
    registry_service: RegistryService,
) -> None:
    service = registry_service
    base = service.url.removesuffix("/mcp") + "/operator"
    async with (
        connect(service.url, service.keys["second"].token) as remote,
        httpx2.AsyncClient(
            trust_env=False, headers={"Authorization": f"Bearer {LOCAL}"}
        ) as operator,
    ):
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        await capture(remote, "rejected-image", lease["session"])
        await service.resident.queue.join()
        preview = (await operator.post(base + "/image", json={"job_id": "rejected-image"})).json()
        payload = {field: preview[field] for field in ("job_id", "sha256", "delivery_revision")}
        for _ in range(2):
            response = await operator.post(
                base + "/image-decision", json={**payload, "decision": "reject"}
            )
            assert response.json() == {
                "ok": True,
                "job_id": "rejected-image",
                "delivery_state": "rejected",
            }
            assert_unavailable(
                await read_capture(remote, "rejected-image"), "capture_delivery_rejected"
            )
        response = await operator.post(
            base + "/image-decision", json={**payload, "decision": "approve"}
        )
        assert response.json() == {"ok": False, "error": "image_decision_final"}
        assert service.backend.calls == 1 and service.resident.queue.empty()


async def test_delivery_http_strict_input_never_implicitly_approves_or_echoes_pixels(
    registry_service: RegistryService,
) -> None:
    service = registry_service
    base = service.url.removesuffix("/mcp") + "/operator"
    async with connect(service.url, service.keys["second"].token) as remote:
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        await capture(remote, "strict-image", lease["session"])
        await service.resident.queue.join()
    async with httpx2.AsyncClient(
        trust_env=False, headers={"Authorization": f"Bearer {LOCAL}"}
    ) as operator:
        preview = (await operator.post(base + "/image", json={"job_id": "strict-image"})).json()
        payload = {field: preview[field] for field in ("job_id", "sha256", "delivery_revision")}
        payload["decision"] = "approve"
        for invalid in (
            {key: value for key, value in payload.items() if key != "decision"},
            {**payload, "decision": True},
            {**payload, "decision": "approved"},
            {**payload, "delivery_revision": "invalid"},
            {**payload, "sha256": None},
            {**payload, "image_base64": preview["image_base64"]},
        ):
            response = await operator.post(base + "/image-decision", json=invalid)
            assert response.status_code == 400
            assert preview["image_base64"] not in response.text
        body = json.dumps(payload).removesuffix("}") + ',"decision":"reject"}'
        response = await operator.post(
            base + "/image-decision", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400
        assert (await operator.get(base + "/image-decision")).status_code == 405
        assert (await operator.get(base + "/image")).status_code == 405
        assert (await operator.post(base + "/images", json={})).status_code == 405
        assert (
            await operator.get(base + "/images", params={"job_id": "strict-image"})
        ).status_code == 400
        assert service.resident.captures["strict-image"].delivery_state == "pending"
        assert service.backend.calls == 1


async def test_identical_pixels_in_another_job_need_their_own_delivery_decision(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as broker:
        session = acquire(broker)
        for job_id in ("first-image", "second-image"):
            enqueue(broker, session, job_id)
            await broker.queue.join()
        first = broker.operator_image("first-image")
        second = broker.operator_image("second-image")
        assert first["sha256"] == second["sha256"]
        assert first["delivery_revision"] != second["delivery_revision"]
        with pytest.raises(BrokerError, match="^stale_image_preview$"):
            decide(broker, {**first, "job_id": "second-image"}, "approve")
        decide(broker, first, "approve")
        assert broker.read_capture("first-image", PRINCIPAL).png
        pending(broker, "second-image")
        assert broker.backend.calls == 2 and broker.queue.empty()
