"""Read-only web UI: Google login, session gate, CSRF, and broker passthrough."""

import json
import tempfile
from pathlib import Path

import httpx2
import pytest
from test_gateway_oauth import CLIENT_ID, OWNER, FakeGoogle

from cardputer_mcp import gateway_oauth as gw
from cardputer_mcp import webui
from cardputer_mcp.config import WebUISettings

UI = "https://gw-a1.tail1234.ts.net:8443"
KEY = "cpk_" + "2" * 32 + "." + "b" * 64
PRIVATE = Path(tempfile.gettempdir())
SETTINGS = WebUISettings(
    public_url=UI,
    google_client_id=CLIENT_ID,
    allowed_google_sub=OWNER,
    broker_env_file=PRIVATE / "webui-broker.env",
    google_client_secret_file=PRIVATE / "webui-google.env",
)
SNAPSHOT = {
    "ok": True,
    "schema_version": 1,
    "kind": "snapshot",
    "cursor": {"epoch": "e", "sequence": 1},
    "state": {"ok": True, "backend": "mock"},
    "timeline": {"ok": True, "cards": [], "has_more": False},
}


class FakeBroker:
    """The broker's operator routes as a local read-only key sees them."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.control = False
        self.reject = False
        self.leak: str | None = None  # a value the broker must never echo
        self.idle_ping = False  # the broker's idle SSE comment follows the snapshot

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        assert request.url.host == "127.0.0.1" and request.url.port == 8765
        assert request.headers["authorization"] == f"Bearer {KEY}"
        for name in ("cookie", "origin", "x-csrf-token", "x-forwarded-for", "forwarded"):
            assert name not in request.headers
        if self.reject:
            return httpx2.Response(403, json={"ok": False, "error": "local_operator_required"})
        path = request.url.path
        if self.leak is not None and path == "/operator/events":
            first = "data: " + json.dumps(SNAPSHOT) + '\n\ndata: {"x": "' + self.leak[:9]
            rest = self.leak[9:] + '"}\n\n'

            async def chunks():
                yield first.encode()
                yield rest.encode()

            return httpx2.Response(
                200, content=chunks(), headers={"content-type": "text/event-stream"}
            )
        if self.leak is not None:
            return httpx2.Response(200, json={"ok": True, "note": "x" + self.leak + "x"})
        if path == "/operator/state":
            return httpx2.Response(200, json={"ok": True, "backend": "mock", "owner": None})
        if path == "/operator/timeline":
            return httpx2.Response(200, json={"ok": True, "cards": [], "has_more": False})
        if path == "/operator/images":
            return httpx2.Response(200 if self.control else 403, json={"ok": True})
        if path == "/operator/job-logs":
            body = json.loads(request.content)
            return httpx2.Response(200, json={"ok": True, "entries": [], "echo": body})
        if path == "/operator/events":
            data = "id: e:1\ndata: " + json.dumps(SNAPSHOT) + "\n\n"
            if self.idle_ping:
                data += ": ping\n\n"
            return httpx2.Response(
                200, content=data.encode(), headers={"content-type": "text/event-stream"}
            )
        return httpx2.Response(404, json={"ok": False, "error": "not_found"})


class Clock:
    def __init__(self) -> None:
        self.value = 1_700_000_000.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def google():
    fake = FakeGoogle()
    fake.redirect_uri = UI + gw.GOOGLE_CALLBACK_PATH
    return fake


@pytest.fixture
async def ui(google):
    broker = FakeBroker()
    clock = Clock()
    google_http = httpx2.AsyncClient(transport=httpx2.MockTransport(google.handler))
    reader = webui.BrokerReader(
        "http://127.0.0.1:8765/mcp",
        KEY,
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(broker.handler)),
    )
    app, state = webui.create_webui_app(SETTINGS, "google-secret", reader, http=google_http)
    state.sessions._now = clock
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=UI, follow_redirects=False
    ) as client:
        client.broker = broker
        client.clock = clock
        client.ui = state
        yield client
    await reader.aclose()
    await google_http.aclose()


async def login(client, google) -> str:
    start = await client.get("/login")
    assert start.status_code == 302
    location = start.headers["location"]
    assert location.startswith(gw.GOOGLE_AUTHORIZATION_URL)
    code, state = google.approve(location)
    callback = await client.get(gw.GOOGLE_CALLBACK_PATH, params={"code": code, "state": state})
    assert callback.status_code == 200
    page = await client.get("/")
    assert page.status_code == 200
    marker = 'name="csrf-token" content="'
    return page.text.split(marker, 1)[1].split('"', 1)[0]


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/static/app.js"),
        ("GET", "/static/app.css"),
        ("GET", "/static/pip.woff2"),
        ("GET", "/api/state"),
        ("GET", "/api/timeline"),
        ("GET", "/api/events"),
        ("POST", "/api/job-logs"),
        ("POST", "/logout"),
        ("GET", "/anything-else"),
        ("GET", "/.well-known/oauth-protected-resource/mcp"),
    ],
)
async def test_without_a_session_nothing_is_served(ui, method, path):
    response = await ui.request(method, path)
    assert response.status_code == 401
    assert response.json() == {"error": "authentication_required"}
    assert ui.broker.requests == []
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


async def test_root_redirects_to_login_and_login_goes_to_google(ui):
    root = await ui.get("/")
    assert root.status_code == 302 and root.headers["location"] == "/login"
    start = await ui.get("/login")
    assert start.status_code == 302
    assert "scope=openid" in start.headers["location"]
    assert "code_challenge_method=S256" in start.headers["location"]
    assert ui.broker.requests == []


async def test_wrong_host_is_refused(ui):
    response = await ui.get("/login", headers={"host": "evil.example:8443"})
    assert response.status_code == 400 and response.json() == {"error": "invalid_host"}


async def test_login_sets_a_strict_host_cookie_and_continues_same_site(ui, google):
    start = await ui.get("/login")
    code, state = google.approve(start.headers["location"])
    callback = await ui.get(gw.GOOGLE_CALLBACK_PATH, params={"code": code, "state": state})
    assert callback.status_code == 200
    assert 'http-equiv="refresh"' in callback.text
    cookie = callback.headers["set-cookie"]
    assert cookie.startswith(webui.COOKIE + "=")
    lowered = cookie.lower()
    for part in ("httponly", "secure", "samesite=strict", "path=/"):
        assert part in lowered
    assert "domain=" not in lowered
    page = await ui.get("/")
    assert page.status_code == 200 and 'name="csrf-token"' in page.text
    # The state is single use.
    replay = await ui.get(gw.GOOGLE_CALLBACK_PATH, params={"code": code, "state": state})
    assert replay.status_code == 400


async def test_another_google_account_gets_no_session(ui, google):
    google.sub = "999"
    start = await ui.get("/login")
    code, state = google.approve(start.headers["location"])
    callback = await ui.get(gw.GOOGLE_CALLBACK_PATH, params={"code": code, "state": state})
    assert callback.status_code == 403 and "set-cookie" not in callback.headers
    assert (await ui.get("/api/state")).status_code == 401


async def test_reads_pass_through_with_the_broker_key_only(ui, google):
    await login(ui, google)
    state = await ui.get("/api/state")
    assert state.status_code == 200 and state.json()["backend"] == "mock"
    timeline = await ui.get("/api/timeline")
    assert timeline.status_code == 200 and timeline.json()["cards"] == []
    assert [r.url.path for r in ui.broker.requests] == ["/operator/state", "/operator/timeline"]
    script = await ui.get("/static/app.js")
    assert script.status_code == 200 and "textContent" in script.text
    assert "innerHTML" not in script.text


async def test_events_are_passed_through(ui, google):
    await login(ui, google)
    response = await ui.get("/api/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"kind": "snapshot"' in response.text


async def test_idle_comment_becomes_a_data_heartbeat(ui, google):
    await login(ui, google)
    ui.broker.idle_ping = True
    response = await ui.get("/api/events")
    # EventSource never shows comments; the page needs a data event to see a live stream.
    assert ": ping" not in response.text
    assert response.text.endswith('data: {"ok":true,"kind":"heartbeat"}\n\n')


async def test_job_logs_need_csrf_and_same_origin(ui, google):
    csrf = await login(ui, google)
    body = {"job_id": "job-1", "levels": ["error"], "context": 0, "limit": 10}
    headers = {"origin": UI, "x-csrf-token": csrf}
    missing = await ui.post("/api/job-logs", json=body, headers={"origin": UI})
    assert missing.status_code == 403
    foreign = await ui.post(
        "/api/job-logs", json=body, headers={"origin": "https://evil.example", "x-csrf-token": csrf}
    )
    assert foreign.status_code == 403
    invalid = await ui.post("/api/job-logs", json={"job_id": "job-1", "x": 1}, headers=headers)
    assert invalid.status_code == 400
    assert ui.broker.requests == []
    ok = await ui.post("/api/job-logs", json=body, headers=headers)
    assert ok.status_code == 200
    assert ok.json()["echo"]["job_id"] == "job-1" and ok.json()["echo"]["limit"] == 10


async def test_logout_needs_csrf_and_ends_the_session(ui, google):
    csrf = await login(ui, google)
    refused = await ui.post("/logout", data={"csrf": "wrong"}, headers={"origin": UI})
    assert refused.status_code == 403
    assert (await ui.get("/api/state")).status_code == 200
    done = await ui.post("/logout", data={"csrf": csrf}, headers={"origin": UI})
    assert done.status_code == 200
    ui.cookies.clear()
    assert (await ui.get("/api/state")).status_code == 401


async def test_session_expires(ui, google):
    await login(ui, google)
    ui.clock.value += webui.SESSION_TTL + 1
    assert (await ui.get("/api/state")).status_code == 401


async def test_broker_refusal_is_a_fixed_502(ui, google):
    await login(ui, google)
    ui.broker.reject = True
    response = await ui.get("/api/state")
    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "broker_key_rejected"}
    events = await ui.get("/api/events")
    assert events.status_code == 502


async def test_logs_never_carry_cookies_or_tokens(ui, google, caplog):
    caplog.set_level("INFO", logger="cardputer_mcp.webui")
    await login(ui, google)
    await ui.get("/api/state?secret=fixture")
    text = caplog.text
    assert "webui_login_accepted" in text and "webui_access GET /api/state 200" in text
    assert webui.COOKIE not in text and "fixture" not in text and KEY not in text


@pytest.mark.parametrize("value", [KEY, "google-secret"])
async def test_a_broker_answer_holding_a_credential_is_withheld(ui, google, value):
    csrf = await login(ui, google)
    ui.broker.leak = value
    for response in (
        await ui.get("/api/state"),
        await ui.get("/api/timeline"),
        await ui.post(
            "/api/job-logs",
            json={"job_id": "job-1"},
            headers={"origin": UI, "x-csrf-token": csrf},
        ),
    ):
        assert response.status_code == 502
        assert response.json() == {"ok": False, "error": "response_withheld"}
        assert value not in response.text
    events = await ui.get("/api/events")
    # The first, clean event is forwarded; the one carrying the value (split across
    # chunks) is stopped as a whole, so not even its first characters leave.
    assert '"kind": "snapshot"' in events.text
    assert value[:9] not in events.text and value not in events.text


async def test_nothing_served_carries_credentials_or_environment(ui, google, monkeypatch):
    monkeypatch.setenv("CARDPUTER_TUNNEL_API_KEY", "sk-env-fixture-value")
    csrf = await login(ui, google)
    bodies = [
        (await ui.get("/")).text,
        (await ui.get("/static/app.js")).text,
        (await ui.get("/static/app.css")).text,
        (await ui.get("/api/state")).text,
        (await ui.get("/nope")).text,
        (await ui.post("/logout", data={"csrf": csrf}, headers={"origin": UI})).text,
        (await ui.get("/login")).headers["location"],
    ]
    for text in bodies:
        for value in (KEY, "google-secret", "sk-env-fixture-value", OWNER, "CARDPUTER_"):
            assert value not in text


async def test_pip_font_needs_a_session_and_is_only_the_subset(ui, google):
    # Without a session the font is not served (see test_without_a_session_nothing_is_served).
    await login(ui, google)
    page = await ui.get("/")
    assert "font-src 'self'" in page.headers["content-security-policy"]
    font = await ui.get("/static/pip.woff2")
    assert font.status_code == 200
    assert font.headers["content-type"] == "font/woff2"
    # A WOFF2 subset of a few KB, not the 2 MB DotGothic16 file.
    assert font.content[:4] == b"wOF2" and len(font.content) < 16 * 1024
    assert ui.broker.requests == []


async def test_jobs_and_detail_are_swipeable_panels(ui, google):
    await login(ui, google)
    page = await ui.get("/")
    for needle in ('id="heads"', 'id="panels"', 'id="p0"', 'id="p1"', 'id="log-lines"'):
        assert needle in page.text
    # The bottom log section and its close button are gone: the detail is its own panel.
    assert 'id="close-logs"' not in page.text and 'id="logs"' not in page.text
    css = await ui.get("/static/app.css")
    assert "scroll-snap-type" in css.text
    # Selection is an outline only, so a selected card does not move its contents.
    selected = css.text.split(".jobs li.selected", 1)[1].split("}", 1)[0]
    assert "outline" in selected and "border" not in selected and "transform" not in selected
    script = await ui.get("/static/app.js")
    for needle in ("toggleSelection", "layoutHeads", "scrollLeft", "renderDetail"):
        assert needle in script.text


async def test_pip_is_drawn_locally_and_adds_no_request(ui, google):
    await login(ui, google)
    page = await ui.get("/")
    assert 'id="pip"' in page.text and 'id="pip-video"' in page.text
    script = await ui.get("/static/app.js")
    for needle in ("captureStream", "requestPictureInPicture", "webkitSetPresentationMode"):
        assert needle in script.text
    # Drawn from data the page already holds: no new route and no new fetch target.
    assert script.text.count("fetch(") == 2
    assert 'fetch("/api/job-logs"' in script.text and 'fetch("/api/state"' in script.text
    assert "innerHTML" not in script.text
    # Silence replaces the stream; the figure drawn tells a dead stream from a live one.
    for needle in ("STALE_MS", "visibilitychange", "pageshow", "dataAge", "停止 ", "resumePip"):
        assert needle in script.text
    # A job ending is shown by colour and pulses, and reduced motion turns the pulses off.
    for needle in (
        "noteChanges",
        "TONES",
        "prefers-reduced-motion",
        "shortKind",
        "drawIcon",
        "tickerText",
    ):
        assert needle in script.text
