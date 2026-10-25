"""`require_principal`: the one principal/scope check behind the MCP handlers.

The table pins the existing split (redaction-auth.md B6): status, logs, job status,
lease actions and acquire skip authentication when the server does not require it;
everything else always authenticates, even on a mock server without credentials.
"""

import ast
from collections import defaultdict
from pathlib import Path

import pytest

from cardputer_mcp.broker import BrokerError
from cardputer_mcp.models import Origin
from cardputer_mcp.server import Principal, require_principal

SRC = Path(__file__).resolve().parents[1] / "src" / "cardputer_mcp"

READ = Principal("key:read", Origin.REMOTE, frozenset({"read"}))
CONTROL = Principal("key:control", Origin.LOCAL, frozenset({"read", "control"}))
CONTROL_ONLY = Principal("key:odd", Origin.LOCAL, frozenset({"control"}))

CREDENTIALS = {"missing": None, "read": READ, "control": CONTROL, "control_only": CONTROL_ONLY}


def expected(scope, credential, *, auth_required, honor):
    if honor and not auth_required:
        return None
    owner = CREDENTIALS[credential]
    if owner is None:
        return "authentication_required"
    if scope not in owner.scopes:
        return "insufficient_scope"
    return owner


CASES = [
    pytest.param(scope, credential, auth_required, honor, id=f"{scope}-{credential}-{a}-{h}")
    for scope in ("read", "control")
    for credential in CREDENTIALS
    for auth_required, a in ((True, "auth"), (False, "noauth"))
    for honor, h in ((True, "honor"), (False, "always"))
]


@pytest.mark.parametrize(("scope", "credential", "auth_required", "honor"), CASES)
def test_require_principal_table(scope, credential, auth_required, honor):
    seen = []

    def authenticate(header):
        seen.append(header)
        return CREDENTIALS[credential]

    want = expected(scope, credential, auth_required=auth_required, honor=honor)
    if isinstance(want, str):
        with pytest.raises(BrokerError, match=f"^{want}$"):
            require_principal(
                authenticate,
                "Bearer x",
                scope,
                auth_required=auth_required,
                honor_auth_required=honor,
            )
    else:
        got = require_principal(
            authenticate, "Bearer x", scope, auth_required=auth_required, honor_auth_required=honor
        )
        assert got is want
    # A skipped check never even consults the authenticator.
    assert seen == ([] if want is None else ["Bearer x"])


# --- every call site keeps its kind --------------------------------------------------------

# op/helper -> sorted (scope, honor_auth_required) of each require() call inside it.
SITES = {
    "server.py": {
        "client_identity": [("read", False)],
        "list_registered_targets": [("read", False)],
        "read_source_resources": [("read", False)] * 2,
        "device_status": [("read", True)],
        "read_logs": [("read", True)],
        "acquire_session": [("control", True)],
        "renew_session": [("control", True)],
        "release_session": [("control", True)],
        # deferred, profile, device (always + require_owner), then the gated owned check,
        # and the source reauthorize callback.
        "prepare_test": [("control", False)] * 4 + [("control", True)],
        "execute_test": [("control", False)] * 2 + [("control", True)],
        "request_home_recovery": [("control", False)],
        "cancel_home_recovery": [("control", False)],
        "job_status": [("read", False)] * 4 + [("read", True)],
        "read_build_logs": [("read", False)] * 3,
        "list_apps": [("read", False)],
        "app_command": [("control", False)],
        "read_app_events": [("read", False)],
        "read_device_logs": [("read", False)],
        "read_capture": [("read", False)],
        "cancel_job": [("control", False), ("control", True)],
        "create_work_session": [("control", False)],
        "resume_work_session": [("read", False)],
        "request_device": [("control", False)],
        "cancel_device_request": [("control", False)],
        "close_work_session": [("control", False)],
    },
    "surface.py": {
        "plan_is_callers": [("control", False)],
        # status(view=job) authenticates once; view=jobs|recovery use _reader, which
        # honours auth_required like view=job|device (owned_by_caller only).
        "_reader": [("read", True)],
        "status": [("read", False)],
        "cancel": [("control", False)] * 3,
    },
}


def require_calls(path):
    found = defaultdict(list)

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            names = stack
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                names = [*stack, child.name]
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "require"
            ):
                (honor,) = [k for k in child.keywords if k.arg == "honor_auth_required"]
                found[names[1]].append((child.args[1].value, honor.value.value))
            walk(child, names)

    walk(ast.parse(path.read_text(encoding="utf-8")), [])
    return {name: sorted(calls) for name, calls in found.items()}


@pytest.mark.parametrize("module", sorted(SITES))
def test_every_call_site_keeps_its_authentication_kind(module):
    assert require_calls(SRC / module) == {k: sorted(v) for k, v in SITES[module].items()}


def test_the_old_wrappers_are_gone():
    # HTTP guards (LoopbackOnly, TunnelIngress, operator API) authenticate on their own
    # and are out of scope; MCP handlers only use require().
    text = (SRC / "server.py").read_text(encoding="utf-8")
    for wrapper in ("principal", "read", "owned", "acquire_owned", "work_action"):
        assert f"    def {wrapper}(" not in text
    assert "def create_work_owned(" not in text
    for module in SITES:
        assert "principal(ctx" not in (SRC / module).read_text(encoding="utf-8")
