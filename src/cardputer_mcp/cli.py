import argparse
import asyncio
import json
import os
import re
import socket
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn

from .auth import AuthError, load_client_token, load_environment_token
from .broker import BrokerError
from .broker_link import DEFAULT_BROKER_URL
from .client import call, connect, validate_url
from .config import load_settings
from .history_archive import DEFAULT_KEEP_LATEST, HistoryArchiveError, archive_history
from .key_operator import KeyOperatorError
from .key_operator import run as manage_keys
from .models import Origin, TestName
from .operator import acknowledge_device_request_recovery, acknowledge_recovery
from .operator_client import OperatorClient, OperatorClientError
from .ports import inventory_json
from .server import create_app, end_operator_streams
from .surface import KIND_TEST

STARTUP_FAILURE = 3


class _Server(uvicorn.Server):
    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        # Uvicorn waits for open responses before lifespan shutdown; operator
        # event streams never finish by themselves, so end them first.
        end_operator_streams(self.config.app)
        await super().shutdown(sockets)


def _serve(app: Any, **options: Any) -> None:
    """``uvicorn.run`` for one process, with operator streams ended at shutdown."""
    server = _Server(uvicorn.Config(app, **options))
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    if not server.started:
        sys.exit(STARTUP_FAILURE)


RETIRED_COMMANDS = frozenset(
    {
        "approve",
        "plan-review",
        "approve-live",
        "approve-and-start",
        "image-decision",
        "home-recovery-review",
        "home-recovery-approve",
        "operator-launch-request",
        "derby-operator-request",
        "home-recovery-operator-request",
    }
)


def output(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True), flush=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Single-owner Cardputer MCP (mock default, opt-in serial)"
    )
    root.add_argument("--version", action="version", version=version("cardputer-device-mcp"))
    root.add_argument("--url", default=DEFAULT_BROKER_URL)
    root.add_argument(
        "--auth-source", choices=("none", "environment", "file", "key-environment"), default="none"
    )
    root.add_argument("--auth-file", type=Path)
    root.add_argument("--client", choices=list(Origin), default=Origin.LOCAL.value)
    sub = root.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Start ONE resident broker; never opens USB")
    serve.add_argument("--config", type=Path)
    serve.add_argument(
        "--key-registry", type=Path, help="Override the standard private app-state key database"
    )
    serve.add_argument(
        "--enable-serial",
        action="store_true",
        help="Explicit hardware-mode opt-in; authenticated control scope still required",
    )
    serve.add_argument(
        "--require-auth",
        action="store_true",
        help="Require authenticated clients in mock mode; choose --auth-source explicitly",
    )
    keys = sub.add_parser("keys", help="Local-only key management; never an MCP tool")
    keys.add_argument("key_command", choices=("issue", "list", "verify", "revoke", "rotate"))
    keys.add_argument("--registry", type=Path, help="Override the standard private key database")
    keys.add_argument("--label")
    keys.add_argument("--origin", choices=list(Origin), default=Origin.REMOTE.value)
    keys.add_argument("--control", action="store_true", help="Explicitly grant read + control")
    keys.add_argument(
        "--ttl-seconds",
        type=int,
        help="Lifetime of the new key. Default: issue 3600; rotate keeps the current "
        "key's original lifetime (at most 30 days)",
    )
    keys.add_argument("--key-id", help="Public ID only; never put the secret key in arguments")
    keys.add_argument(
        "--write-env",
        type=Path,
        metavar="ENV_FILE",
        help="issue/rotate: write the new key into this existing private stdio env file "
        "(absolute path) instead of displaying it",
    )
    gui = sub.add_parser("gui", help="Local operator queue window; uses the resident broker only")
    gui.add_argument("--config", type=Path)
    stdio = sub.add_parser("stdio", help="Forward MCP stdio to the existing authenticated broker")
    stdio.add_argument(
        "--env-file", type=Path, required=True, help="Absolute private env-file path"
    )
    gateway = sub.add_parser(
        "gateway",
        help="Claude gateway (OAuth + MCP relay to the broker); opens Tailscale Funnel only "
        "when the TOML's [gateway] sets funnel = true",
    )
    gateway.add_argument("--config", type=Path, required=True, help="Broker TOML with [gateway]")
    sub.add_parser(
        "http-diagnostics",
        help="Read safe HTTP denial counters from the resident broker; local environment auth only",
    )
    create_work = sub.add_parser("work-create", help="Create a logical work session")
    create_work.add_argument("--owner", choices=list(Origin), default=Origin.LOCAL.value)
    create_work.add_argument("--seconds", type=int, default=3600)
    for work_name in ("work-resume", "work-request", "work-cancel", "work-close"):
        work = sub.add_parser(work_name)
        work.add_argument(
            "--resume-env",
            default="CARDPUTER_WORK_RESUME_TOKEN",
            help="Process variable holding the private resume token",
        )
        if work_name == "work-request":
            work.add_argument("--lease-seconds", type=int, default=30)
            work.add_argument("--wait-seconds", type=int, default=300)
        if work_name == "work-cancel":
            work.add_argument("--request-id", required=True)
    review_request = sub.add_parser("review-request", help="OFFLINE restart-request review only")
    review_request.add_argument("--state-dir", type=Path, default=Path(".state"))
    review_request.add_argument("--request-id", required=True)
    sub.add_parser("status")
    logs = sub.add_parser("logs")
    logs.add_argument("--cursor", help="JSON next_cursor from the previous result")
    logs.add_argument("--limit", type=int, default=40)
    acquire = sub.add_parser("acquire")
    acquire.add_argument("--owner", choices=list(Origin), default=Origin.LOCAL.value)
    acquire.add_argument("--seconds", type=int, default=30)
    for name in ("release", "renew", "prepare", "execute", "cancel"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--session", required=True, help="Private in-memory lease capability")
        if name == "renew":
            cmd.add_argument("--seconds", type=int, default=30)
        if name == "prepare":
            cmd.add_argument("--job", required=True)
            cmd.add_argument("--test", choices=list(TestName), default=TestName.DIAGNOSTICS.value)
        if name == "execute":
            cmd.add_argument("--plan", required=True)
            cmd.add_argument(
                "--tool",
                choices=["execute", "flash"],
                default="execute",
                help="MCP tool to queue the plan with: flash is required for flash/restore plans",
            )
            cmd.add_argument(
                "--wait-for-approval",
                action="store_true",
                help=argparse.SUPPRESS,
            )
        if name == "cancel":
            cmd.add_argument("--job", required=True)
    job = sub.add_parser("job")
    job.add_argument("job_id")
    device_logs = sub.add_parser("device-logs", help="Read your completed device log report")
    device_logs.add_argument("job_id")
    for retired in sorted(RETIRED_COMMANDS):
        sub.add_parser(retired, help="Retired; never executes an operation")
    image_review = sub.add_parser("image-review", help="Preview a completed retained image")
    image_review.add_argument("--job", required=True)
    review = sub.add_parser("review", help="OFFLINE acknowledgement; never retries a job")
    review.add_argument("--state-dir", type=Path, default=Path(".state"))
    review.add_argument("--job", required=True)
    archive = sub.add_parser(
        "archive",
        help="OFFLINE: move old finished history to an archive file; keeps referenced rows",
    )
    archive.add_argument("--state-dir", type=Path, default=Path(".state"))
    archive.add_argument(
        "--keep-latest",
        type=int,
        default=DEFAULT_KEEP_LATEST,
        help="Also keep this many of the newest registrations (default %(default)s)",
    )
    archive.add_argument(
        "--dry-run", action="store_true", help="Report what would move; change nothing"
    )
    sub.add_parser("ports", help="Explicit read-only USB metadata inventory; never opens a port")
    demo = sub.add_parser("demo", help="Two actual MCP clients compete for the same MOCK device")
    demo.add_argument(
        "--recording", action="store_true", help="Pace synthetic demo for ~20 seconds"
    )
    return root


async def demo(
    url: str,
    recording: bool = False,
    *,
    local_token: str | None = None,
    remote_token: str | None = None,
) -> None:
    async def beat(text: str) -> None:
        print(text, flush=True)
        if recording:
            await asyncio.sleep(3)

    await beat("MOCK DEVICE / REAL MCP HTTP: one broker, two clients")
    if local_token is None or remote_token is None:
        raise AuthError("demo_requires_authenticated_clients")
    async with connect(url, local_token) as local, connect(url, remote_token) as remote:
        initial = await call(local, "status", {"view": "device"})
        if initial.get("backend") != "mock":
            raise ValueError("Demo requires a mock broker; hardware is never used")
        acquired = await call(
            local, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 60}
        )
        if not acquired.get("ok"):
            raise ValueError("Demo needs an idle, reviewed mock broker")
        session = acquired["session"]
        await beat("1. Local CLI owns the whole flash -> boot -> test session")
        prepared = await call(
            local,
            "submit",
            {
                "kind": "workflow",
                "session": session,
                "job_id": f"demo-{uuid4().hex}",
            },
        )
        if not prepared.get("ok"):
            raise ValueError("Could not prepare mock workflow")
        job = await call(
            local,
            "execute",
            {"op": "run", "session": session, "plan_hash": prepared["plan_hash"]},
        )
        if not job.get("ok"):
            raise ValueError("Could not enqueue mock workflow")
        conflict = await call(
            remote, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
        )
        if conflict.get("error") != "device_busy":
            raise ValueError("Expected a single-owner conflict")
        await beat("2. Second MCP client: device_busy (same broker, no second USB reader)")
        while (await call(local, "status", {"view": "job", "job_id": job["id"]}))["state"] in (
            "queued",
            "running",
        ):
            await asyncio.sleep(0.05)
        done = await call(remote, "status", {"view": "job", "job_id": job["id"]})
        if done["state"] != "succeeded":
            raise ValueError("Mock workflow did not succeed")
        one = await call(local, "status", {"view": "logs", "stream": "broker"})
        two = await call(remote, "status", {"view": "logs", "stream": "broker"})
        if one["entries"] != two["entries"]:
            raise ValueError("Log readers did not see the same cached events")
        await beat("3. MOCK workflow passed; both clients read identical cached logs")
        retry = await call(
            local,
            "execute",
            {"op": "run", "session": session, "plan_hash": prepared["plan_hash"]},
        )
        if not retry.get("deduplicated"):
            raise ValueError("Retry was not deduplicated")
        await beat("4. Retrying the job ID returns the original result; no replay")
        await call(local, "session", {"action": "release", "session": session})
        second = await call(
            remote, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
        )
        if not second.get("ok"):
            raise ValueError("Ownership handover failed")
        await beat("5. Local releases; second MCP client acquires the device")
        await call(remote, "session", {"action": "release", "session": second["session"]})
        await beat("PASS: exclusive ownership, shared logs, durable jobs, safe retry")
    print("No USB / real flash / Tunnel connection was used", flush=True)


_TEST_KIND = {test.value: kind.value for kind, test in KIND_TEST.items()}


def _resume_token(args: argparse.Namespace) -> str:
    resume_token = os.environ.get(args.resume_env, "")
    if re.fullmatch(r"[a-f0-9]{64}", resume_token) is None:
        raise ValueError("invalid_work_resume_environment")
    return resume_token


def surface_request(command: str, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Map one CLI sub-command to the MCP tool and typed request that implements it."""
    if command == "work-create":
        return "session", {"action": "open", "owner": args.owner, "ttl_seconds": args.seconds}
    if command == "work-resume":
        return "status", {"view": "session", "resume_token": _resume_token(args)}
    if command == "work-request":
        return "session", {
            "action": "request",
            "resume_token": _resume_token(args),
            "lease_seconds": args.lease_seconds,
            "wait_seconds": args.wait_seconds,
        }
    if command == "work-cancel":
        return "cancel", {
            "target": "request",
            "resume_token": _resume_token(args),
            "request_id": args.request_id,
        }
    if command == "work-close":
        return "session", {"action": "close", "resume_token": _resume_token(args)}
    if command == "status":
        return "status", {"view": "device"}
    if command == "logs":
        request: dict[str, Any] = {"view": "logs", "stream": "broker", "limit": args.limit}
        if args.cursor:
            request["cursor"] = json.loads(args.cursor)
        return "status", request
    if command == "acquire":
        return "session", {"action": "open", "owner": args.owner, "lease_seconds": args.seconds}
    if command == "job":
        return "status", {"view": "job", "job_id": args.job_id}
    if command == "device-logs":
        return "status", {"view": "logs", "stream": "device", "job_id": args.job_id}
    if command == "release":
        return "session", {"action": "release", "session": args.session}
    if command == "renew":
        return "session", {
            "action": "renew",
            "session": args.session,
            "lease_seconds": args.seconds,
        }
    if command == "prepare":
        return "submit", {
            "kind": _TEST_KIND[str(args.test)],
            "job_id": args.job,
            "session": args.session,
        }
    if command == "execute":
        if args.tool == "flash":
            return "flash", {"session": args.session, "plan_hash": args.plan}
        return "execute", {"op": "run", "session": args.session, "plan_hash": args.plan}
    return "cancel", {"target": "job", "job_id": args.job, "session": args.session}


async def run_client(args: argparse.Namespace) -> None:
    url = validate_url(args.url)
    if args.command == "demo":
        if args.auth_source != "environment":
            raise AuthError("demo_requires_environment_credentials")
        await demo(
            url,
            args.recording,
            local_token=load_environment_token(Origin.LOCAL),
            remote_token=load_environment_token(Origin.REMOTE),
        )
        return
    command = args.command
    tool, request = surface_request(command, args)
    token = None
    if args.auth_source == "environment":
        token = load_environment_token(Origin(args.client))
    elif args.auth_source == "key-environment":
        token = os.environ.get("CARDPUTER_CLIENT_KEY", "")
        if re.fullmatch(r"cpk_[a-f0-9]{32}\.[a-f0-9]{64}", token) is None:
            raise AuthError("invalid_client_key_environment")
    elif args.auth_source == "file":
        if args.auth_file is None:
            raise ValueError("auth_file_required")
        token = load_client_token(args.auth_file, Origin(args.client))
    async with connect(url, token) as client:
        result = await call(client, tool, request)
        output(result)
        if not result.get("ok"):
            raise SystemExit(1)


def main() -> None:
    arguments = parser()
    args, unknown = arguments.parse_known_args()
    if args.command in RETIRED_COMMANDS:
        output({"ok": False, "error": "product_approval_retired"})
        raise SystemExit(1)
    if unknown:
        arguments.error("unrecognized arguments: " + " ".join(unknown))
    try:
        if args.command == "serve":
            if args.auth_source == "key-environment":
                raise AuthError("client_only_auth_source")
            settings = load_settings(args.config)
            if settings.backend == "serial" and args.auth_source == "none":
                raise AuthError("serial_auth_source_required")
            if (args.require_auth or args.key_registry) and args.auth_source == "none":
                raise AuthError("auth_source_required")
            _serve(
                create_app(
                    settings,
                    serial_enabled=args.enable_serial,
                    require_auth=args.require_auth or args.auth_source != "none",
                    key_registry_path=args.key_registry,
                    auth_source=args.auth_source if args.auth_source != "none" else "environment",
                ),
                host=settings.host,
                port=settings.port,
                access_log=False,
                log_level="warning",
                proxy_headers=False,
            )
        elif args.command == "keys":
            output(
                manage_keys(
                    args.key_command,
                    args.registry,
                    label=args.label,
                    origin=Origin(args.origin),
                    control=args.control,
                    ttl_seconds=args.ttl_seconds,
                    key_id=args.key_id,
                    write_env=args.write_env,
                )
            )
        elif args.command == "stdio":
            from .client_environment import ClientEnvironmentError, load_client_environment
            from .stdio_adapter import StdioAdapterError, run_stdio

            if (
                args.auth_source != "none"
                or args.auth_file is not None
                or args.url != DEFAULT_BROKER_URL
                or args.client != Origin.LOCAL.value
            ):
                print("stdio_adapter_use_private_env_file_only", file=sys.stderr)
                raise SystemExit(1)
            try:
                private = load_client_environment(args.env_file)
                asyncio.run(run_stdio(private.url, private.token, args.env_file))
            except (ClientEnvironmentError, StdioAdapterError) as error:
                print(str(error), file=sys.stderr)
                raise SystemExit(1) from None
        elif args.command == "gateway":
            from .gateway_launch import run as run_gateway

            if (
                args.auth_source != "none"
                or args.auth_file is not None
                or args.url != DEFAULT_BROKER_URL
                or args.client != Origin.LOCAL.value
            ):
                print("gateway_use_config_files_only", file=sys.stderr)
                raise SystemExit(1)
            raise SystemExit(run_gateway(args.config))
        elif args.command == "gui":
            from .gui import run as run_gui

            if args.auth_source != "environment":
                raise AuthError("gui_requires_local_environment_credentials")
            gui_url = args.url
            if args.config is not None:
                gui_settings = load_settings(args.config)
                gui_url = f"http://127.0.0.1:{gui_settings.port}/mcp"
            run_gui(
                validate_url(gui_url),
                load_environment_token(Origin.LOCAL),
                broker_config=args.config,
                project=Path.cwd(),
            )
        elif args.command == "http-diagnostics":
            if args.auth_source != "environment" or args.client != Origin.LOCAL.value:
                raise AuthError("http_diagnostics_requires_local_environment_credentials")
            output(OperatorClient(args.url, load_environment_token(Origin.LOCAL)).diagnostics())
        elif args.command == "image-review":
            if args.auth_source != "environment" or args.client != Origin.LOCAL.value:
                raise AuthError("operator_cli_requires_local_environment_credentials")
            output(OperatorClient(args.url, load_environment_token(Origin.LOCAL)).image(args.job))
        elif args.command == "review-request":
            output(acknowledge_device_request_recovery(args.state_dir, args.request_id))
        elif args.command == "ports":
            output(inventory_json())
        elif args.command == "review":
            output(acknowledge_recovery(args.state_dir, args.job))
        elif args.command == "archive":
            output(
                archive_history(args.state_dir, keep_latest=args.keep_latest, dry_run=args.dry_run)
            )
        else:
            asyncio.run(run_client(args))
    except KeyboardInterrupt:
        pass
    except (
        AuthError,
        BrokerError,
        OperatorClientError,
        HistoryArchiveError,
        KeyOperatorError,
    ) as error:
        # Each of these carries only a fixed, sanitized message.
        print(f"Request failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # Avoid leaking client capabilities or credentials in HTTP/OS exceptions.
        print(
            "Request failed. Check broker status, arguments and local configuration.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
