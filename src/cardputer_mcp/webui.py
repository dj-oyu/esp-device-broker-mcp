"""Read-only web UI: Google login for one account, then the broker's operator views.

Everything except the login start and Google's callback requires a session.
Without one, the page redirects to the login and every other path answers 401
with a fixed body: no broker state, job, error detail or version. Sessions live
in memory (a ``__Host-`` HttpOnly, Secure, SameSite=Strict cookie holding a
random token; the server keeps only its SHA-256), expire after a fixed time and
end at logout. POST also needs the page's CSRF token and a same-origin Origin.

The broker is read with a local read-only key: state, timeline, the live event
stream (passed through unchanged) and job logs. Nothing here can act on a job.
Logs name fixed events, methods, known routes and statuses only; never cookies,
tokens, query strings or Google responses.
"""

import hashlib
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import WebUISettings
from .gateway_oauth import GOOGLE_CALLBACK_PATH, GoogleLoginError, GoogleOIDC, _s256
from .models import BuildLogRequest
from .webui_assets import APP_CSS, APP_JS, page
from .webui_font import PIP_FONT_WOFF2

logger = logging.getLogger("cardputer_mcp.webui")

COOKIE = "__Host-cardputer-webui"
SESSION_TTL = 8 * 3600
LOGIN_TTL = 600
MAX_SESSIONS = 8
MAX_PENDING_LOGINS = 16
MAX_BODY = 4096
MAX_EVENT = 1024 * 1024
BROKER_TIMEOUT = 10.0
PUBLIC_PATHS = frozenset({"/login", GOOGLE_CALLBACK_PATH})
ROUTES = frozenset(
    {
        "/",
        "/login",
        "/logout",
        GOOGLE_CALLBACK_PATH,
        "/static/app.js",
        "/static/app.css",
        "/static/pip.woff2",
        "/api/state",
        "/api/timeline",
        "/api/events",
        "/api/job-logs",
    }
)
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cache-control": "no-store",
    "strict-transport-security": "max-age=31536000",
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class Session:
    subject: str
    csrf: str
    expires_at: float


@dataclass
class _Pending:
    nonce: str
    verifier: str
    expires_at: float


class Sessions:
    """In-memory sessions and pending logins, keyed by SHA-256 of their tokens."""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._sessions: dict[str, Session] = {}
        self._pending: dict[str, _Pending] = {}

    def _prune(self) -> None:
        now = self._now()
        self._sessions = {k: v for k, v in self._sessions.items() if v.expires_at > now}
        self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}

    def begin_login(self) -> tuple[str, _Pending]:
        self._prune()
        while len(self._pending) >= MAX_PENDING_LOGINS:
            self._pending.pop(next(iter(self._pending)))
        state = secrets.token_urlsafe(32)
        pending = _Pending(
            secrets.token_urlsafe(32), secrets.token_urlsafe(48), self._now() + LOGIN_TTL
        )
        self._pending[_digest(state)] = pending
        return state, pending

    def take_login(self, state: str) -> _Pending | None:
        self._prune()
        return self._pending.pop(_digest(state), None)

    def create(self, subject: str) -> str:
        self._prune()
        while len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].expires_at)
            del self._sessions[oldest]
        token = secrets.token_urlsafe(32)
        self._sessions[_digest(token)] = Session(
            subject, secrets.token_urlsafe(32), self._now() + SESSION_TTL
        )
        return token

    def get(self, token: str | None) -> Session | None:
        if not token or len(token) > 128:
            return None
        session = self._sessions.get(_digest(token))
        if session is None or session.expires_at <= self._now():
            return None
        return session

    def end(self, token: str | None) -> None:
        if token:
            self._sessions.pop(_digest(token), None)


class BrokerReader:
    """The broker's read-only operator routes, with a local read-only key."""

    def __init__(self, url: str, token: str, *, http: httpx2.AsyncClient | None = None) -> None:
        parsed = urlparse(url)
        self.base = f"http://127.0.0.1:{parsed.port}"
        self._token = token
        self._http = http or httpx2.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=BROKER_TIMEOUT
        )

    @property
    def credential(self) -> str:
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token}", "accept": "application/json"}

    async def get(self, path: str) -> tuple[int, dict[str, Any]]:
        return await self._call("GET", path, None)

    async def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return await self._call("POST", path, body)

    async def _call(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        try:
            response = await self._http.request(
                method, self.base + path, headers=self._headers(), json=body
            )
        except httpx2.HTTPError:
            return 502, {"ok": False, "error": "broker_unavailable"}
        if response.status_code in (401, 403):
            return 502, {"ok": False, "error": "broker_key_rejected"}
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code != 200 or not isinstance(payload, dict):
            return 502, {"ok": False, "error": "broker_unavailable"}
        return 200, payload

    async def probe(self, path: str) -> int | None:
        """The HTTP status of one GET, or None when the broker is unreachable."""
        try:
            response = await self._http.get(self.base + path, headers=self._headers())
        except httpx2.HTTPError:
            return None
        return response.status_code

    async def events(
        self,
    ) -> tuple[int, AsyncIterator[bytes] | dict[str, Any], Callable[[], Awaitable[None]]]:
        """Open the broker's SSE stream; on refusal return a fixed error body."""
        request = self._http.build_request(
            "GET",
            self.base + "/operator/events",
            headers={**self._headers(), "accept": "text/event-stream"},
            timeout=httpx2.Timeout(BROKER_TIMEOUT, read=None),
        )
        try:
            response = await self._http.send(request, stream=True)
        except httpx2.HTTPError:
            return 502, {"ok": False, "error": "broker_unavailable"}, _nothing
        content_type = response.headers.get("content-type", "")
        if response.status_code != 200 or not content_type.startswith("text/event-stream"):
            status = response.status_code
            await response.aclose()
            error = "broker_key_rejected" if status in (401, 403) else "broker_unavailable"
            return 502, {"ok": False, "error": error}, _nothing
        return 200, response.aiter_bytes(), response.aclose

    async def aclose(self) -> None:
        await self._http.aclose()


async def _nothing() -> None:
    return None


class _Guard:
    """Host check, security headers, session gate and access log for every request."""

    def __init__(self, app: ASGIApp, ui: "WebUI") -> None:
        self.app = app
        self.ui = ui

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = scope.get("path", "")
        status_holder = {"status": 0}

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.decode("latin-1").lower() not in SECURITY_HEADERS
                ]
                headers += [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                message = {**message, "headers": headers}
            await send(message)

        try:
            if request.headers.get("host", "").lower() not in self.ui.hosts:
                refused = JSONResponse({"error": "invalid_host"}, 400)
                await refused(scope, receive, send_with_headers)
                return
            if path not in PUBLIC_PATHS and self.ui.session(request) is None:
                if request.method == "GET" and path == "/":
                    response: Response = RedirectResponse("/login", 302)
                else:
                    response = JSONResponse({"error": "authentication_required"}, 401)
                await response(scope, receive, send_with_headers)
                return
            await self.app(scope, receive, send_with_headers)
        finally:
            route = path if path in ROUTES else "other"
            logger.info(
                "webui_access %s %s %s", scope.get("method", "?"), route, status_holder["status"]
            )


class WebUI:
    def __init__(
        self,
        settings: WebUISettings,
        google: GoogleOIDC,
        broker: BrokerReader,
        *,
        withheld: tuple[str, ...] = (),
        now: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.google = google
        self.broker = broker
        # Credentials this process holds. A broker answer that contains one is never
        # forwarded to the browser (defence in depth: the broker redacts already).
        self._withheld = tuple(value.encode() for value in withheld if value)
        self.sessions = Sessions(now)
        self._now = now
        parsed = urlparse(settings.public_url)
        self.origin = settings.public_url
        self.hosts = frozenset({parsed.netloc.lower(), (parsed.hostname or "").lower()})

    def _leaks(self, data: bytes) -> bool:
        return any(value in data for value in self._withheld)

    def _reply(self, payload: dict[str, Any], status: int) -> Response:
        if self._leaks(json.dumps(payload).encode()) or self._leaks(
            json.dumps(payload, ensure_ascii=False).encode()
        ):
            logger.error("webui_response_withheld")
            return JSONResponse({"ok": False, "error": "response_withheld"}, 502)
        return JSONResponse(payload, status)

    def session(self, request: Request) -> Session | None:
        return self.sessions.get(request.cookies.get(COOKIE))

    def _same_origin(self, request: Request) -> bool:
        return request.headers.get("origin") == self.origin

    async def login(self, request: Request) -> Response:
        state, pending = self.sessions.begin_login()
        url = self.google.authorization_url(state, pending.nonce, _s256(pending.verifier))
        return RedirectResponse(url, 302)

    async def callback(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        pending = self.sessions.take_login(state) if state else None
        if pending is None:
            logger.warning("webui_login_rejected reason=unknown_state")
            return _message("ログインをやり直してください。", 400)
        code = request.query_params.get("code")
        if not code or request.query_params.get("error"):
            logger.warning("webui_login_rejected reason=google_denied")
            return _message("ログインが取り消されました。", 403)
        try:
            subject = await self.google.verify_code(code, pending.verifier, pending.nonce)
        except GoogleLoginError as error:
            logger.warning("webui_login_rejected reason=%s", error.reason)
            return _message("ログインに失敗しました。", 403)
        if not secrets.compare_digest(subject, self.settings.allowed_google_sub):
            logger.warning("webui_login_rejected reason=account_not_allowed")
            return _message("このアカウントは許可されていません。", 403)
        token = self.sessions.create(subject)
        logger.info("webui_login_accepted")
        # Google's redirect is a cross-site navigation, so a SameSite=Strict cookie
        # set here would not ride a further 302. Continue from this page instead.
        response = HTMLResponse(page(None, redirect=True))
        response.set_cookie(
            COOKIE,
            token,
            max_age=SESSION_TTL,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    async def logout(self, request: Request) -> Response:
        session = self.session(request)
        form = await _bounded_form(request)
        if (
            session is None
            or not self._same_origin(request)
            or not secrets.compare_digest(form.get("csrf", ""), session.csrf)
        ):
            return JSONResponse({"error": "forbidden"}, 403)
        self.sessions.end(request.cookies.get(COOKIE))
        logger.info("webui_logout")
        response = HTMLResponse(page(None, logged_out=True))
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    async def index(self, request: Request) -> Response:
        session = self.session(request)
        assert session is not None
        return HTMLResponse(page(session.csrf))

    async def script(self, request: Request) -> Response:
        return Response(APP_JS, media_type="text/javascript; charset=utf-8")

    async def style(self, request: Request) -> Response:
        return Response(APP_CSS, media_type="text/css; charset=utf-8")

    async def font(self, request: Request) -> Response:
        return Response(PIP_FONT_WOFF2, media_type="font/woff2")

    async def state(self, request: Request) -> Response:
        status, payload = await self.broker.get("/operator/state")
        return self._reply(payload, status)

    async def timeline(self, request: Request) -> Response:
        status, payload = await self.broker.get("/operator/timeline")
        return self._reply(payload, status)

    async def job_logs(self, request: Request) -> Response:
        session = self.session(request)
        assert session is not None
        token = request.headers.get("x-csrf-token", "")
        if not self._same_origin(request) or not secrets.compare_digest(token, session.csrf):
            return JSONResponse({"error": "forbidden"}, 403)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return JSONResponse({"error": "invalid_request"}, 400)
        body = await _bounded_body(request)
        try:
            parsed = BuildLogRequest.model_validate_json(body)
        except (ValidationError, ValueError):
            return JSONResponse({"error": "invalid_request"}, 400)
        status, payload = await self.broker.post(
            "/operator/job-logs", parsed.model_dump(mode="json")
        )
        return self._reply(payload, status)

    async def events(self, request: Request) -> Response:
        status, stream, close = await self.broker.events()
        if not isinstance(stream, AsyncIterator):
            return self._reply(stream, status)
        token = request.cookies.get(COOKIE)

        async def body() -> AsyncIterator[bytes]:
            # Forward whole SSE events only, each checked in full before it leaves.
            pending = b""
            try:
                async for chunk in stream:
                    # Heartbeats arrive at least every 25 s: an ended session stops here.
                    if self.sessions.get(token) is None:
                        return
                    pending += chunk.replace(b"\r\n", b"\n")
                    if len(pending) > MAX_EVENT:
                        logger.error("webui_event_too_large")
                        return
                    while b"\n\n" in pending:
                        event, pending = pending.split(b"\n\n", 1)
                        if self._leaks(event):
                            logger.error("webui_response_withheld")
                            return
                        if event.startswith(b":"):
                            # A comment is invisible to EventSource; send a data event the
                            # page can see, so it can tell a quiet stream from a dead one.
                            event = b'data: {"ok":true,"kind":"heartbeat"}'
                        yield event + b"\n\n"
            finally:
                await close()

        return StreamingResponse(body(), media_type="text/event-stream; charset=utf-8")

    def app(self) -> ASGIApp:
        routes = [
            Route("/", self.index, methods=["GET"]),
            Route("/login", self.login, methods=["GET"]),
            Route(GOOGLE_CALLBACK_PATH, self.callback, methods=["GET"]),
            Route("/logout", self.logout, methods=["POST"]),
            Route("/static/app.js", self.script, methods=["GET"]),
            Route("/static/app.css", self.style, methods=["GET"]),
            Route("/static/pip.woff2", self.font, methods=["GET"]),
            Route("/api/state", self.state, methods=["GET"]),
            Route("/api/timeline", self.timeline, methods=["GET"]),
            Route("/api/events", self.events, methods=["GET"]),
            Route("/api/job-logs", self.job_logs, methods=["POST"]),
        ]
        starlette = Starlette(routes=routes)
        starlette.router.redirect_slashes = False
        return _Guard(starlette, self)


async def _bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            return b""
    return bytes(body)


async def _bounded_form(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0] != (
        "application/x-www-form-urlencoded"
    ):
        return {}
    raw = await _bounded_body(request)
    try:
        parsed = parse_qs(raw.decode("ascii"), strict_parsing=False)
    except UnicodeDecodeError:
        return {}
    return {key: values[0] for key, values in parsed.items() if values}


def _message(text: str, status: int) -> Response:
    return HTMLResponse(page(None, message=text), status)


def create_webui_app(
    settings: WebUISettings,
    google_client_secret: str,
    broker: BrokerReader,
    *,
    http: httpx2.AsyncClient | None = None,
    now: Callable[[], float] = time.time,
) -> tuple[ASGIApp, WebUI]:
    google_http = http or httpx2.AsyncClient(trust_env=False, follow_redirects=False)
    google = GoogleOIDC(
        settings.google_client_id,
        google_client_secret,
        settings.public_url + GOOGLE_CALLBACK_PATH,
        google_http,
        now,
    )
    ui = WebUI(
        settings, google, broker, withheld=(broker.credential, google_client_secret), now=now
    )
    return ui.app(), ui
