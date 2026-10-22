"""Explicit host recipe through the same HTTP broker for HELLO and STRESS.

Run against a separately started mock broker first. This example never starts a
server, opens serial, flashes firmware or certifies benchmark quality.
"""

import argparse
import asyncio
import os
from uuid import uuid4

from cardputer_mcp.app_client import AppClientError, AppSessionClient
from cardputer_mcp.app_models import AppSessionOptions
from cardputer_mcp.client import call, connect


async def run(url: str, app_id: str) -> None:
    async with connect(url, os.environ.get("CARDPUTER_KEY")) as connection:
        lease = await call(
            connection, "acquire_session", {"owner": "local_cli", "lease_seconds": 60}
        )
        if lease.get("ok") is False:
            raise AppClientError("lease_rejected")
        session = lease["session"]
        client = AppSessionClient(connection)
        catalog = await client.list_apps()
        print("Catalog freshness:", catalog.get("freshness", "unobserved"))
        await client.start(
            session,
            "app-example-" + uuid4().hex[:12],
            AppSessionOptions(app_id=app_id, allowed_actions=["accept"], duration_ms=30000),
        )
        await asyncio.sleep(0.1)
        await client.tap("accept")
        # Existing STRESS parser/statistics and quality gates remain separate.
        # consumed means owner input delivery, not JS/render/save completion.
        await asyncio.sleep(0.1)
        terminal = await client.stop()
        print("Transport outcome:", terminal.get("state"), "App quality: unassessed")
        if terminal.get("state") in ("succeeded", "cancelled"):
            await call(connection, "release_session", {"session": session})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Existing loopback broker /mcp URL")
    parser.add_argument("--app", choices=("local.hello", "local.stress"), default="local.hello")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.url, args.app))
    except AppClientError as exc:
        # Fixed client errors only: never print credentials or remote exceptions.
        parser.exit(1, str(exc) + "; device TTL remains the agreed cleanup boundary\n")


if __name__ == "__main__":
    main()
