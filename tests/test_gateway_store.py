"""Gateway connections survive a restart; only hashes of tokens reach the disk."""

import os
import sqlite3
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from test_gateway_oauth import (
    OWNER,
    PUBLIC,
    SETTINGS,
    _login,
    _ok_mcp,
    _pkce,
    _register,
    _start,
    _token,
)

from cardputer_mcp import gateway_oauth as gw
from cardputer_mcp.gateway_store import (
    FILE_NAME,
    GatewayStore,
    GatewayStoreError,
    default_store_path,
)

RESOURCE = PUBLIC + "/mcp"


@pytest.fixture
def google():
    from test_gateway_oauth import FakeGoogle

    return FakeGoogle()


@asynccontextmanager
async def _gateway(google, path, *, settings=SETTINGS, now=time.time):
    """One gateway process: its own store connection, closed when it stops."""
    store = GatewayStore(path, subject=settings.allowed_google_sub, resource=RESOURCE)
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(google.handler))
    app, provider = gw.create_gateway_app(
        settings, "google-secret", http=http, mcp_app=_ok_mcp, now=now, store=store
    )
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC) as c:
        c.provider = provider
        yield c
    await http.aclose()
    store.close()


async def _connect(client, google) -> tuple[dict, dict]:
    reg, verifier, location = await _login(client, google)
    code = parse_qs(urlparse(location).query)["code"][0]
    response = await _token(
        client,
        reg,
        grant_type="authorization_code",
        code=code,
        code_verifier=verifier,
        redirect_uri=gw.CLAUDE_REDIRECT_URI,
        resource=RESOURCE,
    )
    assert response.status_code == 200, response.text
    return reg, response.json()


async def _mcp(client, access: str) -> int:
    headers = {"Authorization": "Bearer " + access}
    return (await client.post("/mcp", json={}, headers=headers)).status_code


async def test_restart_keeps_client_and_tokens(tmp_path, google):
    path = tmp_path / FILE_NAME
    async with _gateway(google, path) as first:
        reg, tokens = await _connect(first, google)

    async with _gateway(google, path) as second:
        # The access token still works, the client is known, and refresh rotates.
        assert await _mcp(second, tokens["access_token"]) == 200
        assert reg["client_id"] in second.provider.clients
        refreshed = await _token(
            second, reg, grant_type="refresh_token", refresh_token=tokens["refresh_token"]
        )
        assert refreshed.status_code == 200
        new = refreshed.json()
        # Reconnecting with the same client starts a Google login (no re-registration).
        _verifier, challenge = _pkce()
        response = await _start(second, reg, challenge)
        assert response.headers["location"].startswith(gw.GOOGLE_AUTHORIZATION_URL)

    async with _gateway(google, path) as third:
        # The rotated-out refresh token stays used after another restart.
        reused = await _token(
            third, reg, grant_type="refresh_token", refresh_token=tokens["refresh_token"]
        )
        assert reused.json()["error"] == "invalid_grant"
        assert await _mcp(third, new["access_token"]) == 200


async def test_disk_holds_no_raw_token_or_code(tmp_path, google):
    path = tmp_path / FILE_NAME
    async with _gateway(google, path) as client:
        reg, verifier, location = await _login(client, google)
        code = parse_qs(urlparse(location).query)["code"][0]
        tokens = (
            await _token(
                client,
                reg,
                grant_type="authorization_code",
                code=code,
                code_verifier=verifier,
                redirect_uri=gw.CLAUDE_REDIRECT_URI,
            )
        ).json()
        refreshed = (
            await _token(
                client, reg, grant_type="refresh_token", refresh_token=tokens["refresh_token"]
            )
        ).json()
    data = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith(FILE_NAME))
    for secret in (
        tokens["access_token"],
        tokens["refresh_token"],
        refreshed["access_token"],
        refreshed["refresh_token"],
        code,
        verifier,
        "google-secret",
    ):
        assert secret.encode() not in data
    assert gw._digest(refreshed["refresh_token"]).encode() in data


async def test_unused_registrations_are_not_written(tmp_path, google):
    path = tmp_path / FILE_NAME
    async with _gateway(google, path) as client:
        for _ in range(3):
            await _register(client)
        assert len(client.provider.clients) == 3
    async with _gateway(google, path) as again:
        assert again.provider.clients == {}


async def test_revocation_survives_restart(tmp_path, google):
    path = tmp_path / FILE_NAME
    async with _gateway(google, path) as first:
        reg, tokens = await _connect(first, google)
        await first.post(
            "/revoke",
            data={
                "token": tokens["access_token"],
                "client_id": reg["client_id"],
                "client_secret": reg["client_secret"],
            },
        )
    async with _gateway(google, path) as second:
        assert await _mcp(second, tokens["access_token"]) == 401
        refreshed = await _token(
            second, reg, grant_type="refresh_token", refresh_token=tokens["refresh_token"]
        )
        # Refused as an unknown client: with no connection left, the registration is
        # not kept either.
        assert refreshed.status_code == 401
        assert reg["client_id"] not in second.provider.clients


@pytest.mark.parametrize(
    "change",
    [{"allowed_google_sub": "2222222222"}, {"public_url": "https://gw-b2.tail1234.ts.net"}],
)
async def test_other_owner_or_url_drops_stored_connections(tmp_path, google, change):
    path = tmp_path / FILE_NAME
    async with _gateway(google, path) as first:
        _reg, tokens = await _connect(first, google)
    settings = SETTINGS.model_copy(update=change)
    resource = gw.canonical_resource(settings.public_url + "/mcp")
    store = GatewayStore(path, subject=settings.allowed_google_sub, resource=resource)
    assert store.clients() == {}
    assert store.token(gw._digest(tokens["refresh_token"]), "refresh") is None
    store.close()


async def test_expired_connection_is_purged_at_startup(tmp_path, google):
    path = tmp_path / FILE_NAME
    clock = [time.time()]
    async with _gateway(google, path, now=lambda: clock[0]) as first:
        reg, tokens = await _connect(first, google)
    clock[0] += gw.REFRESH_TTL + 1
    async with _gateway(google, path, now=lambda: clock[0]) as second:
        assert reg["client_id"] not in second.provider.clients
    rows = sqlite3.connect(path).execute("SELECT COUNT(*) FROM tokens").fetchone()
    assert rows == (0,)


def test_store_location_is_next_to_the_broker_key_file(tmp_path):
    assert default_store_path(tmp_path / "gateway-broker.env") == tmp_path / FILE_NAME


def test_store_refuses_unsafe_locations(tmp_path):
    def open_at(path):
        return GatewayStore(path, subject=OWNER, resource=RESOURCE)

    with pytest.raises(GatewayStoreError, match="gateway_store_unsafe_path"):
        open_at(tmp_path / ".." / FILE_NAME)
    with pytest.raises(GatewayStoreError, match="gateway_store_unavailable"):
        open_at(tmp_path / "missing" / FILE_NAME)
    (tmp_path / "dir").mkdir()
    with pytest.raises(GatewayStoreError, match="gateway_store_unsafe_path"):
        open_at(tmp_path / "dir")
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(GatewayStoreError, match="gateway_store_unavailable"):
        open_at(bad)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_store_file_is_private_on_posix(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / FILE_NAME
    GatewayStore(path, subject=OWNER, resource=RESOURCE).close()
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    with pytest.raises(GatewayStoreError, match="gateway_store_unsafe_path"):
        GatewayStore(path, subject=OWNER, resource=RESOURCE)


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / FILE_NAME
    GatewayStore(path, subject=OWNER, resource=RESOURCE).close()
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.commit()
    db.close()
    with pytest.raises(GatewayStoreError, match="gateway_store_version_unsupported"):
        GatewayStore(path, subject=OWNER, resource=RESOURCE)
