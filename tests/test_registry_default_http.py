"""Default registry discovery through the real authenticated localhost API."""

import itertools
import os

import httpx2
import pytest
from test_registry_http import LOCAL, REMOTE
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.key_registry import KeyRegistry
from cardputer_mcp.models import Origin


@pytest.mark.parametrize("registry_service", ["automatic"], indirect=True)
async def test_auto_registry_live_mutations_and_independent_tokens(registry_service):
    service = registry_service
    reader = service.keys["reader"]
    async with connect(service.url, reader.token) as client:
        assert (await call(client, "device_status"))["ok"]
        refused = await call(client, "acquire_session", {"owner": "remote_mcp"})
        assert refused == {"ok": False, "error": "insufficient_scope"}
        service.registry.revoke(reader.metadata.key_id)
        async with httpx2.AsyncClient(trust_env=False) as http:
            rejected = await http.post(
                service.url, json={}, headers={"Authorization": "Bearer " + reader.token}
            )
            assert rejected.status_code == 401
    sequence = itertools.count(50)
    path = service.registry._path
    writer = KeyRegistry(
        path, clock=lambda: service.now[0], random_bytes=lambda n: bytes([next(sequence)]) * n
    )
    issued = writer.issue("new synthetic client", Origin.REMOTE)
    async with connect(service.url, issued.token) as client:
        assert (await call(client, "device_status"))["ok"]
    writer.close()
    # Missing -> newly created store is detected by the same running broker.
    service.registry.close()
    removed = path.with_name("retained-old.sqlite3")
    os.replace(path, removed)
    async with httpx2.AsyncClient(trust_env=False) as http:
        response = await http.get(
            service.url, headers={"Authorization": "Bearer " + issued.token}
        )
        assert response.status_code == 401
    replacement = KeyRegistry(
        path, clock=lambda: service.now[0], random_bytes=lambda n: b"\x63" * n
    )
    new = replacement.issue("replacement synthetic", Origin.REMOTE)
    async with connect(service.url, new.token) as client:
        assert (await call(client, "device_status"))["ok"]
    replacement.close()
    # A corrupt replacement denies issued credentials, never anonymous fallback.
    path.write_bytes(b"invalid sqlite content")
    async with httpx2.AsyncClient(trust_env=False) as http:
        for token in (new.token, "invalid"):
            response = await http.get(service.url, headers={"Authorization": "Bearer " + token})
            assert response.status_code == 401
        assert (await http.get(service.url)).status_code == 401
    for token in (LOCAL, REMOTE):
        async with connect(service.url, token) as client:
            assert (await call(client, "device_status"))["ok"]
