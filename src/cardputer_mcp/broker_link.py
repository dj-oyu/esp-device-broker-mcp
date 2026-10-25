"""One home for the loopback broker URL: its default and the shared parsing core.

Each caller keeps its own deliberately different policy (accepted hosts, ports
and paths, and its own public error). The policies here only decide; mapping a
rejection to a module's error type stays with that module. Standard library only.
"""

import re
from urllib.parse import SplitResult, urlsplit

DEFAULT_BROKER_URL = "http://127.0.0.1:8765/mcp"

# The only form a private/launch-time broker URL may take: literal 127.0.0.1,
# a non-privileged port and the /mcp path, with nothing else.
CANONICAL_BROKER_URL = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{3,4})/mcp\Z")

_MCP_HOSTS = ("127.0.0.1", "localhost")
_MCP_PATHS = ("/mcp",)
_OPERATOR_HOSTS = ("127.0.0.1",)
_OPERATOR_PATHS = ("", "/", "/mcp")


def split_loopback_http(
    url: str,
    *,
    hosts: tuple[str, ...],
    paths: tuple[str, ...],
    allow_empty_userinfo: bool,
) -> SplitResult | None:
    """Return the split URL when everything except the port is acceptable.

    The port is deliberately not read here: ``SplitResult.port`` raises
    ``ValueError`` for malformed or out-of-range ports, and callers differ in
    whether that happens before or after the other checks. ``urlsplit`` itself
    may also raise ``ValueError`` (for example an unclosed IPv6 bracket).
    """
    parsed = urlsplit(url)
    if allow_empty_userinfo:
        userinfo = bool(parsed.username or parsed.password)
    else:
        userinfo = parsed.username is not None or parsed.password is not None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in hosts
        or userinfo
        or parsed.query
        or parsed.fragment
        or parsed.path not in paths
    ):
        return None
    return parsed


def is_local_mcp_url(url: str) -> bool:
    """``http://{127.0.0.1|localhost}:PORT/mcp`` with any explicit port.

    Malformed/out-of-range ports and unparsable URLs raise urllib's ValueError.
    """
    parsed = split_loopback_http(url, hosts=_MCP_HOSTS, paths=_MCP_PATHS, allow_empty_userinfo=True)
    return parsed is not None and parsed.port is not None


def canonical_broker_port(url: object) -> int | None:
    """The port of an exactly canonical broker URL (1024..65535), else None."""
    match = CANONICAL_BROKER_URL.fullmatch(url) if isinstance(url, str) else None
    if match is None or not 1024 <= int(match[1]) <= 65535:
        return None
    return int(match[1])


def operator_broker_port(url: str) -> int | None:
    """The port of a 127.0.0.1 broker base or /mcp URL (1..65535), else None."""
    try:
        parsed = split_loopback_http(
            url, hosts=_OPERATOR_HOSTS, paths=_OPERATOR_PATHS, allow_empty_userinfo=False
        )
        port = None if parsed is None else parsed.port
    except ValueError:
        return None
    if port is None or not 1 <= port <= 65535 or any(character.isspace() for character in url):
        return None
    return port
