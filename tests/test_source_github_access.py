"""GitHub access for source fetch: rate-limit diagnostics and the optional API token."""

import email.message
import io
import time
import urllib.error

import pytest

from cardputer_mcp import source_snapshot
from cardputer_mcp.firmware_source import FirmwareSource
from cardputer_mcp.source_snapshot import (
    GITHUB_TOKEN_ENVIRONMENT,
    SOURCE_ERROR_CODES,
    RateLimited,
    SourceError,
    download,
    github_token,
)

TOKEN = "ghp_" + "A1b2C3d4E5" * 3
API = "https://api.github.com/repos/o/r/git/commits/" + "a" * 40
ZIP = "https://codeload.github.com/o/r/zip/" + "a" * 40


class Response:
    status = 200

    def __init__(self, body: bytes = b"ok") -> None:
        self.body = io.BytesIO(body)

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.body.read(size)


class Opener:
    def __init__(self, outcome: object) -> None:
        self.outcome, self.requests = outcome, []

    def open(self, request: object, timeout: int) -> Response:
        self.requests.append(request)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        assert isinstance(self.outcome, Response)
        return self.outcome


def http_error(code: int, **headers: str) -> urllib.error.HTTPError:
    message = email.message.Message()
    for name, value in headers.items():
        message[name.replace("_", "-")] = value
    return urllib.error.HTTPError("https://api.github.com/", code, "x", message, None)


def run(monkeypatch, outcome: object, url: str = API) -> Opener:
    opener = Opener(outcome)
    monkeypatch.setattr(source_snapshot.urllib.request, "build_opener", lambda *a: opener)
    download(url, time.monotonic() + 30, lambda: True, 1024)
    return opener


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv(GITHUB_TOKEN_ENVIRONMENT, raising=False)


def test_anonymous_requests_carry_no_authorization(monkeypatch):
    opener = run(monkeypatch, Response())
    assert opener.requests[0].get_header("Authorization") is None


@pytest.mark.parametrize("url", [API, ZIP])
def test_token_is_sent_only_to_the_two_github_hosts(monkeypatch, url):
    monkeypatch.setenv(GITHUB_TOKEN_ENVIRONMENT, TOKEN)
    opener = run(monkeypatch, Response(), url)
    assert opener.requests[0].get_header("Authorization") == "Bearer " + TOKEN


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/x",
        "https://api.github.com.evil.test/x",
        "http://api.github.com/x",
        "https://evil.test/api.github.com",
    ],
)
def test_token_is_never_sent_to_other_hosts_or_plain_http(monkeypatch, url):
    monkeypatch.setenv(GITHUB_TOKEN_ENVIRONMENT, TOKEN)
    opener = run(monkeypatch, Response(), url)
    assert opener.requests[0].get_header("Authorization") is None


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_token_means_anonymous(monkeypatch, value):
    monkeypatch.setenv(GITHUB_TOKEN_ENVIRONMENT, value)
    assert github_token() is None


@pytest.mark.parametrize("value", ["short", "has space " + "x" * 30, "ghp_" + "é" * 30, "x" * 300])
def test_malformed_token_fails_closed_without_echoing_it(monkeypatch, value):
    monkeypatch.setenv(GITHUB_TOKEN_ENVIRONMENT, value)
    with pytest.raises(SourceError) as caught:
        download(API, time.monotonic() + 30, lambda: True, 1024)
    assert caught.value.code == "source_github_token_invalid"
    assert value not in str(caught.value) and value not in repr(caught.value)


def test_rate_limit_is_reported_with_bounded_fields(monkeypatch):
    reset = int(time.time()) + 351
    error = http_error(
        403,
        X_RateLimit_Limit="60",
        X_RateLimit_Remaining="0",
        X_RateLimit_Reset=str(reset),
    )
    with pytest.raises(RateLimited) as caught:
        run(monkeypatch, error)
    failure = caught.value
    assert failure.code == "source_rate_limited"
    assert (failure.limit, failure.remaining, failure.authenticated) == (60, 0, False)
    assert 345 <= failure.resets_in <= 351
    assert failure.log_detail.startswith("error: github rate limit: authenticated=no limit=60")
    assert "explanation" in failure.public()


def test_rate_limit_reports_authenticated_flag_and_never_the_token(monkeypatch):
    monkeypatch.setenv(GITHUB_TOKEN_ENVIRONMENT, TOKEN)
    error = http_error(403, X_RateLimit_Limit="5000", X_RateLimit_Remaining="0")
    with pytest.raises(RateLimited) as caught:
        run(monkeypatch, error)
    assert caught.value.authenticated is True
    assert TOKEN not in caught.value.log_detail and TOKEN not in str(caught.value.public())


def test_secondary_limit_uses_retry_after(monkeypatch):
    with pytest.raises(RateLimited) as caught:
        run(monkeypatch, http_error(429, Retry_After="42"))
    assert caught.value.resets_in == 42


def test_absurd_header_values_are_clamped(monkeypatch):
    error = http_error(403, X_RateLimit_Remaining="0", Retry_After="99999999999")
    with pytest.raises(RateLimited) as caught:
        run(monkeypatch, error)
    assert caught.value.resets_in == 86_400


@pytest.mark.parametrize("code", [403, 404, 500])
def test_other_http_errors_stay_download_failed(monkeypatch, code):
    with pytest.raises(SourceError) as caught:
        run(monkeypatch, http_error(code))
    assert caught.value.code == "source_download_failed"
    assert not isinstance(caught.value, RateLimited)


def test_rejected_token_is_distinguished_from_anonymous_401(monkeypatch):
    with pytest.raises(SourceError) as anonymous:
        run(monkeypatch, http_error(401))
    assert anonymous.value.code == "source_download_failed"
    monkeypatch.setenv(GITHUB_TOKEN_ENVIRONMENT, TOKEN)
    with pytest.raises(SourceError) as rejected:
        run(monkeypatch, http_error(401))
    assert rejected.value.code == "source_github_token_rejected"
    assert TOKEN not in str(rejected.value.public())


def test_new_codes_are_registered_and_have_explanations():
    for code in (
        "source_rate_limited",
        "source_github_token_rejected",
        "source_github_token_invalid",
    ):
        assert code in SOURCE_ERROR_CODES
        assert "explanation" in SourceError(code).public()


def test_job_log_gets_detail_before_message_and_stays_idempotent():
    source = FirmwareSource(object(), lambda: None)
    plan = {"job_id": "j1", "source_principal": "owner"}
    detail = RateLimited(60, 0, 351, False).log_detail
    source.record_failure(plan, "source_rate_limited", detail)
    source.record_failure(plan, "source_rate_limited")  # the broker records it a second time
    texts = [entry["text"] for entry in source.logs["j1"].entries]
    assert texts == [
        "error: github rate limit: authenticated=no limit=60 remaining=0 resets_in_seconds=351",
        "error: source fetch failed: source_rate_limited",
    ]


def test_foreign_detail_text_is_never_logged():
    source = FirmwareSource(object(), lambda: None)
    plan = {"job_id": "j2", "source_principal": "owner"}
    source.record_failure(plan, "source_rate_limited", "Authorization: Bearer secret")
    source.record_failure(plan, "source_download_failed", "error: github rate limit: x")
    texts = [entry["text"] for entry in source.logs["j2"].entries]
    assert all("secret" not in text and "rate limit: x" not in text for text in texts)
