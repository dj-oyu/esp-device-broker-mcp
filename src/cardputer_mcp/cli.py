import argparse
import asyncio
import json
import os
import re
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn

from .auth import AuthError, load_client_token, load_environment_token
from .broker import BrokerError
from .client import call, connect, validate_url
from .config import load_settings
from .key_operator import run as manage_keys
from .models import Origin, TestName
from .operator import acknowledge_device_request_recovery, acknowledge_recovery, approve_plan
from .operator_client import OperatorClient, OperatorClientError
from .ports import inventory_json
from .server import create_app


def output(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True), flush=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Single-owner Cardputer MCP (mock default, opt-in serial)"
    )
    root.add_argument("--version", action="version", version=version("cardputer-device-mcp"))
    root.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    root.add_argument(
        "--auth-source", choices=("none", "environment", "file", "key-environment"), default="none"
    )
    root.add_argument("--auth-file", type=Path)
    root.add_argument("--client", choices=list(Origin), default=Origin.LOCAL.value)
    sub = root.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Start ONE resident broker; never opens USB")
    serve.add_argument("--config", type=Path)
    serve.add_argument("--key-registry", type=Path, help="Existing opt-in hash-only key database")
    serve.add_argument(
        "--enable-serial",
        action="store_true",
        help="Explicit hardware-mode opt-in; still requires per-job approval",
    )
    serve.add_argument(
        "--require-auth",
        action="store_true",
        help="Require authenticated clients in mock mode; choose --auth-source explicitly",
    )
    keys = sub.add_parser("keys", help="Local-only key management; never an MCP tool")
    keys.add_argument("key_command", choices=("issue", "list", "verify", "revoke", "rotate"))
    keys.add_argument("--registry", type=Path, required=True)
    keys.add_argument("--label")
    keys.add_argument("--origin", choices=list(Origin), default=Origin.REMOTE.value)
    keys.add_argument("--control", action="store_true", help="Explicitly grant read + control")
    keys.add_argument("--ttl-seconds", type=int, default=3600)
    keys.add_argument("--key-id", help="Public ID only; never put the secret key in arguments")
    gui = sub.add_parser("gui", help="Local operator queue window; uses the resident broker only")
    gui.add_argument("--config", type=Path)
    stdio = sub.add_parser("stdio", help="Forward MCP stdio to the existing authenticated broker")
    stdio.add_argument(
        "--env-file", type=Path, required=True, help="Absolute private env-file path"
    )
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
                "--wait-for-approval",
                action="store_true",
                help="Submit now; local approval starts the exact job without another client call",
            )
        if name == "cancel":
            cmd.add_argument("--job", required=True)
    job = sub.add_parser("job")
    job.add_argument("job_id")
    device_logs = sub.add_parser("device-logs", help="Read your completed device log report")
    device_logs.add_argument("job_id")
    approve = sub.add_parser("approve", help="LOCAL operator-only exact-plan approval")
    approve.add_argument("--state-dir", type=Path, default=Path(".state"))
    approve.add_argument("--plan", required=True)
    live_review = sub.add_parser(
        "plan-review", help="Review an exact plan on the live local broker"
    )
    live_review.add_argument("--plan", required=True)
    for action in ("approve-live", "approve-and-start"):
        live = sub.add_parser(
            action, help="Approve the exact live review; local environment auth only"
        )
        live.add_argument("--plan", required=True)
        live.add_argument("--review-revision", required=True)
        live.add_argument(
            "--attestation", required=True, help="Exact attestation text from plan-review"
        )
        live.add_argument(
            "--allow-image-delivery",
            action="store_true",
            help="Consent to this capture's one image for its requesting principal",
        )
    image_review = sub.add_parser(
        "image-review", help="Preview a completed image through the local API"
    )
    image_review.add_argument("--job", required=True)
    image_decision = sub.add_parser(
        "image-decision", help="Decide delivery of an exact reviewed image"
    )
    image_decision.add_argument("--job", required=True)
    image_decision.add_argument("--sha256", required=True)
    image_decision.add_argument("--delivery-revision", required=True)
    image_decision.add_argument("--decision", required=True, choices=("approve", "reject"))
    launch = sub.add_parser(
        "operator-launch-request", help="Request one explicitly enabled local capture CLI slot"
    )
    launch.add_argument("--pipe", required=True)
    launch.add_argument("--action", required=True, choices=("plan-review", "approve-and-start"))
    launch.add_argument("--epoch", required=True)
    launch.add_argument("--job", required=True)
    launch.add_argument("--plan", required=True)
    launch.add_argument("--principal", required=True)
    launch.add_argument("--review-revision")
    launch.add_argument("--attestation")
    launch.add_argument("--allow-image-delivery", action="store_true")
    review = sub.add_parser("review", help="OFFLINE acknowledgement; never retries a job")
    review.add_argument("--state-dir", type=Path, default=Path(".state"))
    review.add_argument("--job", required=True)
    sub.add_parser("ports", help="Explicit read-only USB metadata inventory; never opens a port")
    demo = sub.add_parser("demo", help="Two actual MCP clients compete for the same MOCK device")
    demo.add_argument(
        "--recording", action="store_true", help="Pace synthetic demo for ~20 seconds"
    )
    return root


async def demo(url: str, recording: bool = False) -> None:
    async def beat(text: str) -> None:
        print(text, flush=True)
        if recording:
            await asyncio.sleep(3)

    await beat("MOCK DEVICE / REAL MCP HTTP: one broker, two clients")
    async with connect(url) as local, connect(url) as remote:
        initial = await call(local, "device_status")
        if initial.get("approval_policy") != "operator_opted_in_to_mock":
            raise ValueError("Demo requires examples/mock-demo.toml (mock-only operator opt-in)")
        acquired = await call(local, "acquire_session", {"owner": "local_cli", "lease_seconds": 60})
        if not acquired.get("ok"):
            raise ValueError("Demo needs an idle, reviewed mock broker")
        session = acquired["session"]
        await beat("1. Local CLI owns the whole flash -> boot -> test session")
        prepared = await call(
            local,
            "prepare_test",
            {
                "session": session,
                "job_id": f"demo-{uuid4().hex}",
                "test": "firmware_workflow",
            },
        )
        if not prepared.get("ok"):
            raise ValueError("Could not prepare mock workflow")
        job = await call(
            local, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]}
        )
        if not job.get("ok"):
            raise ValueError("Could not enqueue mock workflow")
        conflict = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        if conflict.get("error") != "device_busy":
            raise ValueError("Expected a single-owner conflict")
        await beat("2. Second MCP client: device_busy (same broker, no second USB reader)")
        while (await call(local, "job_status", {"job_id": job["id"]}))["state"] in (
            "queued",
            "running",
        ):
            await asyncio.sleep(0.05)
        done = await call(remote, "job_status", {"job_id": job["id"]})
        if done["state"] != "succeeded":
            raise ValueError("Mock workflow did not succeed")
        one = await call(local, "read_logs", {})
        two = await call(remote, "read_logs", {})
        if one["entries"] != two["entries"]:
            raise ValueError("Log readers did not see the same cached events")
        await beat("3. MOCK workflow passed; both clients read identical cached logs")
        retry = await call(
            remote, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]}
        )
        if not retry.get("deduplicated"):
            raise ValueError("Retry was not deduplicated")
        await beat("4. Retrying the job ID returns the original result; no replay")
        await call(local, "release_session", {"session": session})
        second = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        if not second.get("ok"):
            raise ValueError("Ownership handover failed")
        await beat("5. Local releases; second MCP client acquires the device")
        await call(remote, "release_session", {"session": second["session"]})
        await beat("PASS: exclusive ownership, shared logs, durable jobs, safe retry")
    print("No USB / real flash / Tunnel connection was used", flush=True)


async def run_client(args: argparse.Namespace) -> None:
    url = validate_url(args.url)
    if args.command == "demo":
        await demo(url, args.recording)
        return
    command = args.command
    tools = {
        "status": "device_status",
        "logs": "read_logs",
        "acquire": "acquire_session",
        "release": "release_session",
        "renew": "renew_session",
        "prepare": "prepare_test",
        "execute": "execute_test",
        "cancel": "cancel_job",
        "job": "job_status",
        "device-logs": "read_device_logs",
        "work-create": "create_work_session",
        "work-resume": "resume_work_session",
        "work-request": "request_device",
        "work-cancel": "cancel_device_request",
        "work-close": "close_work_session",
    }
    request: dict[str, Any] | None = {}
    if command == "work-create":
        request = {"owner": args.owner, "ttl_seconds": args.seconds}
    elif command.startswith("work-"):
        resume_token = os.environ.get(args.resume_env, "")
        if re.fullmatch(r"[a-f0-9]{64}", resume_token) is None:
            raise ValueError("invalid_work_resume_environment")
        request = {"resume_token": resume_token}
        if command == "work-request":
            request.update(lease_seconds=args.lease_seconds, wait_seconds=args.wait_seconds)
        elif command == "work-cancel":
            request["request_id"] = args.request_id
    elif command == "status":
        request = None
    elif command == "logs":
        request = {"limit": args.limit, "cursor": json.loads(args.cursor) if args.cursor else None}
    elif command == "acquire":
        request = {"owner": args.owner, "lease_seconds": args.seconds}
    elif command in ("job", "device-logs"):
        request = {"job_id": args.job_id}
    else:
        request = {"session": args.session}
        if command == "renew":
            request["lease_seconds"] = args.seconds
        elif command == "prepare":
            request.update(job_id=args.job, test=args.test)
        elif command == "execute":
            request["plan_hash"] = args.plan
            if args.wait_for_approval:
                request["wait_for_approval"] = True
        elif command == "cancel":
            request["job_id"] = args.job
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
        result = await call(client, tools[command], request)
        output(result)
        if not result.get("ok"):
            raise SystemExit(1)


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "serve":
            if args.auth_source == "key-environment":
                raise AuthError("client_only_auth_source")
            settings = load_settings(args.config)
            if settings.backend == "serial" and args.auth_source == "none":
                raise AuthError("serial_auth_source_required")
            if (args.require_auth or args.key_registry) and args.auth_source == "none":
                raise AuthError("auth_source_required")
            uvicorn.run(
                create_app(
                    settings,
                    serial_enabled=args.enable_serial,
                    require_auth=args.require_auth,
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
                )
            )
        elif args.command == "stdio":
            from .client_environment import ClientEnvironmentError, load_client_environment
            from .stdio_adapter import StdioAdapterError, run_stdio

            if (
                args.auth_source != "none"
                or args.auth_file is not None
                or args.url != "http://127.0.0.1:8765/mcp"
                or args.client != Origin.LOCAL.value
            ):
                print("stdio_adapter_use_private_env_file_only", file=sys.stderr)
                raise SystemExit(1)
            try:
                private = load_client_environment(args.env_file)
                asyncio.run(run_stdio(private.url, private.token))
            except (ClientEnvironmentError, StdioAdapterError) as error:
                print(str(error), file=sys.stderr)
                raise SystemExit(1) from None
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
        elif args.command == "operator-launch-request":
            from .operator_launch import OperatorLaunchError
            from .operator_launch_pipe import request_operator_launch

            if (
                args.auth_source != "none"
                or args.client != Origin.LOCAL.value
                or args.auth_file is not None
                or args.url != "http://127.0.0.1:8765/mcp"
            ):
                raise AuthError("operator_launch_uses_os_identity_only")
            try:
                result = request_operator_launch(
                    args.pipe,
                    {
                        "action": args.action,
                        "broker_epoch": args.epoch,
                        "job_id": args.job,
                        "plan_hash": args.plan,
                        "principal_id": args.principal,
                        "review_revision": args.review_revision,
                        "attestation": args.attestation,
                        "allow_image_delivery": args.allow_image_delivery,
                    },
                )
                output(result)
                if result.get("ok") is not True:
                    raise SystemExit(1)
            except OperatorLaunchError as error:
                print(str(error), file=sys.stderr)
                raise SystemExit(1) from None
        elif args.command in (
            "plan-review",
            "approve-live",
            "approve-and-start",
            "image-review",
            "image-decision",
        ):
            from .operator_cli import approve_live, review_plan

            if args.auth_source != "environment" or args.client != Origin.LOCAL.value:
                raise AuthError("operator_cli_requires_local_environment_credentials")
            operator = OperatorClient(args.url, load_environment_token(Origin.LOCAL))
            if args.command == "plan-review":
                output(review_plan(operator, args.plan))
            elif args.command in ("approve-live", "approve-and-start"):
                output(
                    approve_live(
                        operator,
                        args.plan,
                        args.review_revision,
                        args.attestation,
                        start_submitted=args.command == "approve-and-start",
                        allow_image_delivery=args.allow_image_delivery,
                    )
                )
            elif args.command == "image-review":
                output(operator.image(args.job))
            else:
                output(
                    operator.decide_image(
                        args.job, args.sha256, args.delivery_revision, args.decision
                    )
                )
        elif args.command == "review-request":
            output(acknowledge_device_request_recovery(args.state_dir, args.request_id))
        elif args.command == "ports":
            output(inventory_json())
        elif args.command == "approve":
            output(approve_plan(args.state_dir, args.plan))
        elif args.command == "review":
            output(acknowledge_recovery(args.state_dir, args.job))
        else:
            asyncio.run(run_client(args))
    except KeyboardInterrupt:
        pass
    except (AuthError, BrokerError, OperatorClientError) as error:
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
