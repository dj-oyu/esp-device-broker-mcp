"""Claude gateway OAuth: DCR, Google login with sub check, tokens bound to /mcp."""

import base64
import hashlib
import json
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from starlette.responses import JSONResponse

from cardputer_mcp import gateway_oauth as gw
from cardputer_mcp.config import GatewaySettings

PUBLIC = "https://gw-a1.tail1234.ts.net"
CLIENT_ID = "123-abc.apps.googleusercontent.com"
OWNER = "1111111111"
PRIVATE = Path(tempfile.gettempdir())
SETTINGS = GatewaySettings(
    public_url=PUBLIC,
    google_client_id=CLIENT_ID,
    allowed_google_sub=OWNER,
    broker_env_file=PRIVATE / "gateway-broker.env",
    google_client_secret_file=PRIVATE / "gateway-google.env",
)


class FakeGoogle:
    """Google's token and JWKS endpoints; checks PKCE, issues a signed ID token."""

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig"})
        self.published = self.key  # the key served from the JWKS endpoint
        self.jwks_hits = 0
        self.sub = OWNER
        self.claims_override: dict[str, object] = {}
        self.challenges: dict[str, tuple[str, str]] = {}  # code -> (challenge, nonce)
        self.redirect_uri = PUBLIC + gw.GOOGLE_CALLBACK_PATH

    def approve(self, authorize_url: str) -> tuple[str, str]:
        query = parse_qs(urlparse(authorize_url).query)
        code = secrets.token_urlsafe(8)
        self.challenges[code] = (query["code_challenge"][0], query["nonce"][0])
        return code, query["state"][0]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == gw.GOOGLE_JWKS_URL:
            self.jwks_hits += 1
            return httpx2.Response(200, json=KeySet([self.published]).as_dict(private=False))
        assert str(request.url) == gw.GOOGLE_TOKEN_URL
        form = parse_qs(request.content.decode())
        assert form["client_secret"] == ["google-secret"]
        assert form["redirect_uri"] == [self.redirect_uri]
        challenge, nonce = self.challenges.pop(form["code"][0])
        digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
        if base64.urlsafe_b64encode(digest).decode().rstrip("=") != challenge:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": self.sub,
            "iat": now,
            "exp": now + 3600,
            "nonce": nonce,
            **self.claims_override,
        }
        token = jwt.encode({"alg": "RS256", "kid": self.key.kid}, claims, self.key)
        return httpx2.Response(200, json={"id_token": token, "access_token": "unused"})


async def _ok_mcp(scope, receive, send):
    user = scope["user"]
    await JSONResponse({"ok": True, "scopes": user.scopes})(scope, receive, send)


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
async def client(google):
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(google.handler))
    app, provider = gw.create_gateway_app(SETTINGS, "google-secret", http=http, mcp_app=_ok_mcp)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC) as c:
        c.provider = provider
        yield c
    await http.aclose()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


async def _register(client) -> dict:
    response = await client.post(
        "/register",
        json={"redirect_uris": [gw.CLAUDE_REDIRECT_URI], "client_name": "Claude"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _start(client, reg, challenge, **extra) -> httpx2.Response:
    params = {
        "response_type": "code",
        "client_id": reg["client_id"],
        "redirect_uri": gw.CLAUDE_REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "claude-state",
        "resource": PUBLIC + "/mcp",
        **extra,
    }
    return await client.get("/authorize", params=params)


async def _login(client, google) -> tuple[dict, str, str]:
    reg = await _register(client)
    verifier, challenge = _pkce()
    response = await _start(client, reg, challenge)
    assert response.status_code == 302
    assert response.headers["location"].startswith(gw.GOOGLE_AUTHORIZATION_URL)
    code, state = google.approve(response.headers["location"])
    callback = await client.get(gw.GOOGLE_CALLBACK_PATH, params={"code": code, "state": state})
    return reg, verifier, callback.headers.get("location", "")


async def _token(client, reg, **form) -> httpx2.Response:
    return await client.post(
        "/token",
        data={"client_id": reg["client_id"], "client_secret": reg["client_secret"], **form},
    )


async def test_metadata_and_unauthenticated_mcp(client):
    prm = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert prm["resource"] == PUBLIC + "/mcp"
    assert prm["authorization_servers"] == [PUBLIC]
    asm = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["registration_endpoint"] == PUBLIC + "/register"
    response = await client.post("/mcp", json={})
    assert response.status_code == 401
    assert (
        'resource_metadata="' + PUBLIC + '/.well-known/oauth-protected-resource/mcp"'
        in response.headers["www-authenticate"]
    )
    # Trailing slash is not redirected (a redirect would drop the bearer token).
    assert (await client.post("/mcp/", json={})).status_code == 404


async def test_full_flow_issues_resource_bound_tokens_and_rotates_refresh(client, google):
    reg, verifier, location = await _login(client, google)
    query = parse_qs(urlparse(location).query)
    assert location.startswith(gw.CLAUDE_REDIRECT_URI)
    assert query["state"] == ["claude-state"]
    form = {
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "code_verifier": verifier,
        "redirect_uri": gw.CLAUDE_REDIRECT_URI,
        "resource": PUBLIC + "/mcp",
    }
    tokens = (await _token(client, reg, **form)).json()
    assert tokens["expires_in"] == gw.ACCESS_TTL
    assert set(tokens["scope"].split()) == {"read", "control"}

    headers = {"Authorization": "Bearer " + tokens["access_token"]}
    response = await client.post("/mcp", json={}, headers=headers)
    assert response.status_code == 200 and response.json()["ok"] is True

    # The authorization code is single use.
    again = await _token(client, reg, **form)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"

    refreshed = await _token(
        client, reg, grant_type="refresh_token", refresh_token=tokens["refresh_token"]
    )
    assert refreshed.status_code == 200
    new = refreshed.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    reused = await _token(
        client, reg, grant_type="refresh_token", refresh_token=tokens["refresh_token"]
    )
    assert reused.json()["error"] == "invalid_grant"

    # Revoking the refresh token also revokes access tokens of the same grant.
    await client.post(
        "/revoke",
        data={
            "token": new["refresh_token"],
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    for access in (tokens["access_token"], new["access_token"]):
        response = await client.post("/mcp", json={}, headers={"Authorization": "Bearer " + access})
        assert response.status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"sub": "2222222222"},
        {"claims": {"aud": "other.apps.googleusercontent.com"}},
        {"claims": {"iss": "https://evil.example"}},
        {"claims": {"nonce": "replayed"}},
        {"claims": {"exp": int(time.time()) - 3600}},
    ],
)
async def test_login_denied_unless_owner_and_valid_id_token(client, google, change):
    google.sub = change.get("sub", OWNER)
    google.claims_override = change.get("claims", {})
    _reg, _verifier, location = await _login(client, google)
    query = parse_qs(urlparse(location).query)
    assert location.startswith(gw.CLAUDE_REDIRECT_URI)
    assert query["error"] == ["access_denied"] and query["state"] == ["claude-state"]
    assert "code" not in query


async def test_id_token_signed_by_unknown_key_is_denied_after_one_refetch(client, google):
    google.published = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    _reg, _verifier, location = await _login(client, google)
    assert parse_qs(urlparse(location).query)["error"] == ["access_denied"]
    # The cached key set is fetched again once, in case Google rotated its keys.
    assert google.jwks_hits == 2


async def test_rotated_google_key_is_picked_up(client, google):
    await _login(client, google)  # caches the first key set
    google.key = google.published = RSAKey.generate_key(2048, parameters={"kid": "k2"})
    _reg, _verifier, location = await _login(client, google)
    assert "code" in parse_qs(urlparse(location).query)


async def test_callback_state_is_single_use_and_required(client, google):
    assert (await client.get(gw.GOOGLE_CALLBACK_PATH, params={"code": "x"})).status_code == 400
    reg = await _register(client)
    _verifier, challenge = _pkce()
    response = await _start(client, reg, challenge)
    code, state = google.approve(response.headers["location"])
    first = await client.get(gw.GOOGLE_CALLBACK_PATH, params={"code": code, "state": state})
    assert first.status_code == 302
    replay = await client.get(gw.GOOGLE_CALLBACK_PATH, params={"code": code, "state": state})
    assert replay.status_code == 400
    assert replay.json() == {"error": "invalid_login_state"}


async def test_google_error_redirects_access_denied(client, google):
    reg = await _register(client)
    _verifier, challenge = _pkce()
    response = await _start(client, reg, challenge)
    _code, state = google.approve(response.headers["location"])
    callback = await client.get(
        gw.GOOGLE_CALLBACK_PATH, params={"error": "access_denied", "state": state}
    )
    assert parse_qs(urlparse(callback.headers["location"]).query)["error"] == ["access_denied"]


async def test_authorize_rejects_other_resource(client):
    reg = await _register(client)
    _verifier, challenge = _pkce()
    response = await _start(client, reg, challenge, resource="https://elsewhere.example/mcp")
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["error"] == ["invalid_target"]


async def test_resource_spelling_is_canonicalized():
    assert gw.canonical_resource("HTTPS://GW.example:443/mcp/") == "https://gw.example/mcp"
    assert gw.canonical_resource("https://gw.example:8443/mcp") == "https://gw.example:8443/mcp"


async def test_registration_only_accepts_claude_redirect(client):
    response = await client.post(
        "/register", json={"redirect_uris": ["https://attacker.example/cb"]}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"
    response = await client.post(
        "/register",
        json={"redirect_uris": [gw.CLAUDE_REDIRECT_URI, "https://attacker.example/cb"]},
    )
    assert response.status_code == 400


async def test_registration_flood_never_evicts_a_working_client(client, google):
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
    for _ in range(gw.MAX_UNUSED_CLIENTS * 2):
        await _register(client)
    assert len(client.provider.clients) <= gw.MAX_UNUSED_CLIENTS + 1
    refreshed = await _token(
        client, reg, grant_type="refresh_token", refresh_token=tokens["refresh_token"]
    )
    assert refreshed.status_code == 200


async def test_expired_access_token_is_rejected(google):
    clock = [time.time()]
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(google.handler))
    app, _provider = gw.create_gateway_app(
        SETTINGS, "google-secret", http=http, mcp_app=_ok_mcp, now=lambda: clock[0]
    )
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC) as c:
        reg, verifier, location = await _login(c, google)
        code = parse_qs(urlparse(location).query)["code"][0]
        tokens = (
            await _token(
                c,
                reg,
                grant_type="authorization_code",
                code=code,
                code_verifier=verifier,
                redirect_uri=gw.CLAUDE_REDIRECT_URI,
            )
        ).json()
        headers = {"Authorization": "Bearer " + tokens["access_token"]}
        assert (await c.post("/mcp", json={}, headers=headers)).status_code == 200
        clock[0] += gw.ACCESS_TTL + 1
        assert (await c.post("/mcp", json={}, headers=headers)).status_code == 401
    await http.aclose()


async def test_unconfigured_relay_is_503_only_after_auth(google):
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(google.handler))
    app, _provider = gw.create_gateway_app(SETTINGS, "google-secret", http=http)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC) as c:
        assert (await c.post("/mcp", json={})).status_code == 401
        reg, verifier, location = await _login(c, google)
        code = parse_qs(urlparse(location).query)["code"][0]
        tokens = (
            await _token(
                c,
                reg,
                grant_type="authorization_code",
                code=code,
                code_verifier=verifier,
                redirect_uri=gw.CLAUDE_REDIRECT_URI,
            )
        ).json()
        headers = {"Authorization": "Bearer " + tokens["access_token"]}
        response = await c.post("/mcp", json={}, headers=headers)
        assert response.status_code == 503
        assert response.json() == {"error": "relay_not_configured"}
    await http.aclose()


def test_operator_and_web_paths_are_not_served():
    app, _provider = gw.create_gateway_app(SETTINGS, "google-secret")
    paths = {getattr(route, "path", "") for route in app.routes}
    assert paths == {
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource/mcp",
        "/authorize",
        "/token",
        "/register",
        "/revoke",
        gw.GOOGLE_CALLBACK_PATH,
        "/mcp",
    }


def test_gateway_section_loads_from_toml_without_secrets(tmp_path):
    from pydantic import ValidationError

    from cardputer_mcp.config import load_settings

    path = tmp_path / "device.toml"
    path.write_text(
        "[gateway]\n"
        f'public_url = "{PUBLIC}"\n'
        f'google_client_id = "{CLIENT_ID}"\n'
        f'allowed_google_sub = "{OWNER}"\n'
        f"broker_env_file = {json.dumps(str(SETTINGS.broker_env_file))}\n"
        f"google_client_secret_file = {json.dumps(str(SETTINGS.google_client_secret_file))}\n",
        encoding="utf-8",
    )
    valid = path.read_text("utf-8")
    assert load_settings(path).gateway == SETTINGS
    assert SETTINGS.funnel is False  # Funnel stays closed unless explicitly enabled
    path.write_text(valid + 'google_client_secret = "x"\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)
    relative = valid.replace(json.dumps(str(SETTINGS.broker_env_file)), '"relative.env"')
    path.write_text(relative, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)
    path.write_text(valid + 'funnel = "yes"\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)
