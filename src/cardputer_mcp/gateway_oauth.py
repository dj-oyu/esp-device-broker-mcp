"""Claude gateway: an OAuth 2.1 authorization server whose only user is one Google account.

Claude's custom connector registers itself (DCR), sends the user to ``/authorize``,
and this server forwards the browser to Google. Google's answer comes back to
``/oauth/google/callback``; only an ID token whose signature, issuer, audience,
expiry and nonce check out (via ``joserfc``) and whose ``sub`` equals the
configured account produces an authorization code. Tokens are opaque, random,
short-lived and bound to the canonical ``<public_url>/mcp`` resource. Completed
connections are kept, tokens as hashes only, in a private store (``gateway_store``),
so a gateway restart does not disconnect Claude.

Nothing here relays MCP or holds a broker credential; the protected ``/mcp`` app is
passed in. Logs carry fixed event names only, never tokens, codes or Google values.
"""

import hashlib
import logging
import secrets
import time
from base64 import urlsafe_b64encode
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx2
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    ProviderTokenVerifier,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import GatewaySettings
from .gateway_store import GatewayStore

logger = logging.getLogger("cardputer_mcp.gateway")

CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_CALLBACK_PATH = "/oauth/google/callback"
MCP_PATH = "/mcp"
SCOPES = ("read", "control")

LOGIN_TTL = 600
CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
MAX_PENDING_LOGINS = 16
MAX_UNUSED_CLIENTS = 32
GOOGLE_TIMEOUT = 8.0
_URL = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True))


def _token() -> str:
    return secrets.token_urlsafe(32)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _s256(verifier: str) -> str:
    return urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _url(value: str) -> AnyHttpUrl:
    # RFC 8414 issuer comparison is exact: keep "https://host" free of a trailing slash.
    return _URL.validate_python(value)


def canonical_resource(value: str) -> str:
    """RFC 8707 resource as Claude sends it: lowercase scheme/host, no trailing slash."""
    url = AnyHttpUrl(value)
    port = "" if url.port in (None, 443 if url.scheme == "https" else 80) else f":{url.port}"
    path = (url.path or "").rstrip("/")
    return f"{url.scheme.lower()}://{(url.host or '').lower()}{port}{path}"


class GoogleLoginError(Exception):
    """A fixed reason code; never carries Google responses or token contents."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GoogleOIDC:
    """Authorization-code + PKCE login against Google; returns the verified ``sub``."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http: httpx2.AsyncClient,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._http = http
        self._now = now
        self._keys: KeySet | None = None

    def authorization_url(self, state: str, nonce: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": "openid",
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f"{GOOGLE_AUTHORIZATION_URL}?{query}"

    async def _key_set(self, refresh: bool) -> KeySet:
        if self._keys is None or refresh:
            response = await self._http.get(GOOGLE_JWKS_URL, timeout=GOOGLE_TIMEOUT)
            if response.status_code != 200:
                raise GoogleLoginError("google_jwks_unavailable")
            try:
                self._keys = KeySet.import_key_set(response.json())
            except (ValueError, JoseError) as error:
                raise GoogleLoginError("google_jwks_invalid") from error
        return self._keys

    async def _decode(self, id_token: str) -> jwt.Token:
        for refresh in (False, True):
            keys = await self._key_set(refresh)
            try:
                return jwt.decode(id_token, keys, algorithms=["RS256"])
            except JoseError:
                # An unknown key id can mean Google rotated its keys: fetch once more.
                if refresh:
                    break
        raise GoogleLoginError("id_token_signature_invalid")

    async def verify_code(self, code: str, code_verifier: str, nonce: str) -> str:
        try:
            response = await self._http.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": self.redirect_uri,
                    "client_id": self.client_id,
                    "client_secret": self._client_secret,
                },
                timeout=GOOGLE_TIMEOUT,
            )
        except httpx2.HTTPError as error:
            raise GoogleLoginError("google_token_unreachable") from error
        if response.status_code != 200:
            raise GoogleLoginError("google_token_rejected")
        try:
            id_token = response.json()["id_token"]
        except (ValueError, KeyError, TypeError) as error:
            raise GoogleLoginError("google_token_malformed") from error
        if not isinstance(id_token, str):
            raise GoogleLoginError("google_token_malformed")
        try:
            token = await self._decode(id_token)
        except httpx2.HTTPError as error:
            raise GoogleLoginError("google_jwks_unavailable") from error
        registry = jwt.JWTClaimsRegistry(
            now=lambda: int(self._now()),
            leeway=60,
            iss={"essential": True, "values": list(GOOGLE_ISSUERS)},
            aud={"essential": True, "value": self.client_id},
            exp={"essential": True},
            iat={"essential": True},
            nonce={"essential": True, "value": nonce},
            sub={"essential": True},
        )
        try:
            registry.validate(token.claims)
        except JoseError as error:
            raise GoogleLoginError("id_token_claims_invalid") from error
        subject = token.claims["sub"]
        if not isinstance(subject, str):
            raise GoogleLoginError("id_token_claims_invalid")
        return subject


@dataclass
class _PendingLogin:
    client_id: str
    params: AuthorizationParams
    scopes: list[str]
    nonce: str
    code_verifier: str
    expires_at: float


class GatewayProvider:
    """``OAuthAuthorizationServerProvider`` for one Google account.

    Logins in progress, authorization codes and never-used registrations are held in
    memory. Completed connections (client, grant, token digests) are kept in
    ``store`` so a gateway restart does not make Claude register again.
    """

    def __init__(
        self,
        settings: GatewaySettings,
        google: GoogleOIDC,
        now: Callable[[], float] = time.time,
        store: GatewayStore | None = None,
    ) -> None:
        self.resource = canonical_resource(settings.public_url + MCP_PATH)
        self.allowed_sub = settings.allowed_google_sub
        self.google = google
        self._now = now
        self.store = store or GatewayStore(None, subject=self.allowed_sub, resource=self.resource)
        self.store.purge(self._now())
        self.clients: dict[str, OAuthClientInformationFull] = self.store.clients()
        self._pending: dict[str, _PendingLogin] = {}
        self._codes: dict[str, AuthorizationCode] = {}

    # Housekeeping -----------------------------------------------------------------

    def _purge(self) -> None:
        now = self._now()
        for key in [k for k, v in self._pending.items() if v.expires_at < now]:
            del self._pending[key]
        for key in [k for k, v in self._codes.items() if v.expires_at < now]:
            del self._codes[key]
        self.store.purge(now)

    def _client_in_use(self, client_id: str) -> bool:
        return (
            self.store.has_grant(client_id)
            or any(c.client_id == client_id for c in self._codes.values())
            or any(p.client_id == client_id for p in self._pending.values())
        )

    # Clients ---------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(uri) for uri in client_info.redirect_uris or []]
        if uris != [CLAUDE_REDIRECT_URI]:
            raise RegistrationError("invalid_redirect_uri", "redirect URI is not allowed")
        if client_info.client_id is None:
            raise RegistrationError("invalid_client_metadata", "client_id missing")
        self._purge()
        # Anyone on the internet can register; only clients never used by the owner
        # are capped, so registrations cannot evict a working connection.
        unused = [cid for cid in self.clients if not self._client_in_use(cid)]
        while len(unused) >= MAX_UNUSED_CLIENTS:
            del self.clients[unused.pop(0)]
        self.clients[client_info.client_id] = client_info
        logger.info("gateway_client_registered")

    # Authorization ---------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource is not None and canonical_resource(params.resource) != self.resource:
            raise AuthorizeError("invalid_target", "unknown resource")
        if client.client_id is None:
            raise AuthorizeError("unauthorized_client", "client_id missing")
        self._purge()
        if len(self._pending) >= MAX_PENDING_LOGINS:
            oldest = min(self._pending, key=lambda key: self._pending[key].expires_at)
            del self._pending[oldest]
        scopes = params.scopes or (client.scope.split() if client.scope else list(SCOPES))
        state, nonce, verifier = _token(), _token(), _token()
        self._pending[_digest(state)] = _PendingLogin(
            client_id=client.client_id,
            params=params,
            scopes=scopes,
            nonce=nonce,
            code_verifier=verifier,
            expires_at=self._now() + LOGIN_TTL,
        )
        return self.google.authorization_url(state, nonce, _s256(verifier))

    async def google_callback(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        pending = self._pending.pop(_digest(state), None) if state else None
        if pending is None or pending.expires_at < self._now():
            logger.warning("gateway_login_rejected reason=unknown_state")
            return JSONResponse({"error": "invalid_login_state"}, status_code=400)
        redirect = str(pending.params.redirect_uri)
        code = request.query_params.get("code")
        if request.query_params.get("error") or not code:
            logger.warning("gateway_login_rejected reason=google_denied")
            return self._deny(redirect, pending)
        try:
            subject = await self.google.verify_code(code, pending.code_verifier, pending.nonce)
        except GoogleLoginError as error:
            logger.warning("gateway_login_rejected reason=%s", error.reason)
            return self._deny(redirect, pending)
        if not secrets.compare_digest(subject, self.allowed_sub):
            logger.warning("gateway_login_rejected reason=account_not_allowed")
            return self._deny(redirect, pending)
        if pending.client_id not in self.clients:
            logger.warning("gateway_login_rejected reason=client_gone")
            return self._deny(redirect, pending)
        auth_code = _token()
        self._codes[_digest(auth_code)] = AuthorizationCode(
            code=auth_code,
            scopes=pending.scopes,
            expires_at=self._now() + CODE_TTL,
            client_id=pending.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=self.resource,
            subject=subject,
        )
        logger.info("gateway_login_accepted")
        return RedirectResponse(
            construct_redirect_uri(redirect, code=auth_code, state=pending.params.state),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    @staticmethod
    def _deny(redirect: str, pending: _PendingLogin) -> Response:
        return RedirectResponse(
            construct_redirect_uri(redirect, error="access_denied", state=pending.params.state),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    # Tokens ----------------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(_digest(authorization_code))
        if code is None or code.client_id != client.client_id:
            return None
        return code

    def _issue(
        self, grant_id: int, scopes: list[str], old_refresh: str | None = None
    ) -> OAuthToken | None:
        now = int(self._now())
        access, refresh = _token(), _token()
        stored = self.store.issue(
            grant_id,
            scopes,
            (_digest(access), now + ACCESS_TTL),
            (_digest(refresh), now + REFRESH_TTL),
            old_refresh,
        )
        if not stored:
            return None
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(_digest(authorization_code.code), None) is None:
            raise TokenError("invalid_grant", "authorization code was already used")
        if authorization_code.subject is None or authorization_code.resource is None:
            raise TokenError("invalid_grant", "authorization code is incomplete")
        scopes = list(authorization_code.scopes)
        grant_id = self.store.create_grant(
            client, authorization_code.subject, scopes, authorization_code.resource
        )
        tokens = self._issue(grant_id, scopes)
        assert tokens is not None
        logger.info("gateway_token_issued")
        return tokens

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        self._purge()
        stored = self.store.token(_digest(refresh_token), "refresh")
        if stored is None or stored.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=stored.expires_at,
            resource=stored.resource,
            subject=stored.subject,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        key = _digest(refresh_token.token)
        stored = self.store.token(key, "refresh")
        # The SDK already refused scopes the refresh token does not hold.
        tokens = None if stored is None else self._issue(stored.grant_id, list(scopes), key)
        if tokens is None:
            raise TokenError("invalid_grant", "refresh token was already used")
        return tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = self.store.token(_digest(token), "access")
        if stored is None or stored.expires_at < self._now():
            return None
        return AccessToken(
            token=token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=stored.expires_at,
            resource=stored.resource,
            subject=stored.subject,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Revoking either token ends the whole connection (all its tokens).
        grant_id = self.store.grant_of(_digest(token.token))
        if grant_id is not None:
            self.store.revoke_grant(grant_id)

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "identity assertions are not accepted")


class _NotConfigured:
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"error": "relay_not_configured"}, status_code=503)(scope, receive, send)


def create_gateway_app(
    settings: GatewaySettings,
    google_client_secret: str,
    *,
    http: httpx2.AsyncClient | None = None,
    mcp_app: ASGIApp | None = None,
    now: Callable[[], float] = time.time,
    store: GatewayStore | None = None,
) -> tuple[Starlette, GatewayProvider]:
    """Build the Funnel-facing app: OAuth endpoints, Google callback and ``/mcp`` only."""
    issuer = _url(settings.public_url)
    google = GoogleOIDC(
        settings.google_client_id,
        google_client_secret,
        settings.public_url + GOOGLE_CALLBACK_PATH,
        http or httpx2.AsyncClient(),
        now,
    )
    provider = GatewayProvider(settings, google, now, store)
    resource_url = _url(provider.resource)
    protected = RequireAuthMiddleware(
        mcp_app or _NotConfigured(),
        required_scopes=["read"],
        resource_metadata_url=build_resource_metadata_url(resource_url),
    )
    routes = [
        *create_auth_routes(
            provider,
            issuer,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=list(SCOPES), default_scopes=list(SCOPES)
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        *create_protected_resource_routes(resource_url, [issuer], scopes_supported=list(SCOPES)),
        Route(GOOGLE_CALLBACK_PATH, provider.google_callback, methods=["GET"]),
        Route(MCP_PATH, protected, methods=["GET", "POST", "DELETE"]),
    ]
    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(
                    ProviderTokenVerifier(provider), resource_server_url=resource_url
                ),
            )
        ],
    )
    # ``/mcp/`` must not redirect to ``/mcp``: Claude would drop the bearer token.
    app.router.redirect_slashes = False
    return app, provider
