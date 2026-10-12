# Logical work sessions and the device FIFO

Authentication principal, logical work session, MCP transport connection and
physical lease are different objects. A shared API key is one security principal,
regardless of how many terminals use it. One principal may create several work
sessions, each with an independent unguessable resume token.

## New authenticated tools

1. `create_work_session({owner, ttl_seconds})`: creates a logical session; returns
   its public ID plus private `resume_token`. Default TTL 1h, maximum 24h
2. `request_device({resume_token, lease_seconds, wait_seconds})`: joins the one
   authoritative physical-device FIFO. Default lease 30s/max 300s; wait 300s/max 3600s
3. `resume_work_session({resume_token})`: reconnects within the SAME broker epoch
   and reads the request. With valid control scope, returns the current private
   physical lease under `lease.session` when owning
4. Use existing prepare/approve/execute/renew/release tools with that physical
   lease. Every grant creates a new capability and lease generation
5. `cancel_device_request({resume_token,request_id})` cancels WAITING only;
   `close_work_session({resume_token})` closes an idle session, never interrupts active work

Each MCP request still needs valid authentication/scope and matching principal.
A resume token alone cannot authorize access. Read-only scope can inspect its
existing logical session but cannot obtain the private physical lease.
Repeated requests for an existing wait/ownership return the same request, do not
jump FIFO, and do not extend either deadline. Legacy acquire_session stays
immediate-only and cannot jump an existing waiting queue.

The background grant rechecks current key expiry/revocation/scopes from metadata.
Authorization loss cancels pending waits and queued jobs before they begin; active
work remains pinned until the backend truly finishes and closes. A lost HTTP
connection does not cancel a workflow. Resuming does not itself renew a TTL.

## Local CLI

`work-create`, `work-resume`, `work-request`, `work-cancel`, and `work-close` call
those same MCP tools. The create result contains a private resume capability;
keep that output private. Subsequent work commands read it from the process-only
CARDPUTER_WORK_RESUME_TOKEN variable, or a named `--resume-env`, not a secret argv.
Do not paste credentials or capabilities into bug reports or public logs.

## Bounds and restart

Default maximum 128 open work sessions and 32 WAITING requests. Public durable
history caps at 10,000 work sessions and 10,000 device requests; archiving is an
offline operator task rather than silent history deletion. Operator snapshots
show active sessions, a pinned expired owner, and 20 recent terminal sessions, and
report omitted history count. Logs remain bounded cached events.

Resume and lease secrets are memory-only and are never persisted as reusable
verifiers. After broker restart they cannot be resumed. Previously WAITING or
OWNING requests become needs_review, as do interrupted jobs; nothing is
automatically regranted or replayed. Clean shutdown cancels waits after safely
finishing any active device operation. An unconfirmed device close keeps ownership
uncertain and prevents handover.

For restart recovery, inspect the public request and related job/device first.
Stop the broker and explicitly acknowledge only the reviewed record:

```powershell
uv run cardputer review-request --state-dir '.state\tunnel-mock' --request-id PUBLIC_REQUEST_ID
```

The existing `review --job` command separately reviews interrupted jobs. Neither
command retries work or resurrects capabilities. Serial needs_review from earlier
runs is not cleared by installing this update.

## Local operator boundary

The GUI uses `/operator/state`, `/operator/cancel`, `/operator/reorder` and
`/operator/release` on the same loopback service. They are not MCP tools. Bearer
local_cli + control is required; forwarded headers, hostile Origin/Host, duplicate
headers, non-JSON/oversize bodies and query credentials are rejected. No cookies,
URL tokens or browser storage are used. Queue reordering requires the displayed
revision; owner release requires the exact current public lease generation.
