# Cardputer Device MCP

Local Claude Code and Codex can share the same resident broker through the
tools-only stdio adapter. It reads one explicit private client-key file outside
repositories and forwards the broker's catalog/results without owning a device.
See [STDIO-CLIENTS.md](STDIO-CLIENTS.md) for Windows registration templates and
the distinction between shared-key identity and separate work sessions.

**A single-owner MCP/CLI broker with mock mode and an opt-in serial adapter.**
Two clients connect over Streamable HTTP and share one resident queue, one workflow
lease, durable job history, and the same bounded log cache.

The serial adapter offers bounded device console logs and observed firmware
startup information, an ordinary HELLO cycle, and HOME render-buffer
capture for the inspected PocketJS firmware protocol. Automated verification uses
synthetic serial devices; one user-run Windows HOME capture has succeeded. These paths require explicit
startup opt-in, authenticated clients and explicit execution. See
[SERIAL.md](SERIAL.md) for effects, limitations and user-run Windows instructions.

Mock remains the default. `mock_flash`, `mock_boot`, and `mock_test` are synthetic.
Registered external [ESP-IDF 6.0.1 builds](FIRMWARE-BUILD.md) and separately requested
[backup/flash/restore](FIRMWARE-FLASH.md) are implemented. Flash defaults to disabled;
physical device behavior remains unvalidated. Completion uses readback and passive
boot IDF/ELF evidence, with no HELLO key commands.
[HOME capture](CAPTURE.md) returns a bounded MCP PNG after confirmed serial close;
its mock image is visibly labeled TEST / NOT DEVICE. A [local queue GUI](GUI-WINDOWS.md) and [logical work sessions/FIFO](SESSIONS.md)
are now implemented and mock-tested. The optional [Windows Tunnel preparation](TUNNEL-WINDOWS.md) launcher
and [local scoped-key management](KEYS.md) are available. A user-run Windows mock Tunnel registration and read-only MCP calls
were verified; cloud lifecycle remains external to broker status. Nothing opens USB automatically at startup.

## Explicit client operations

`submit` persists an immutable, bounded plan and performs no device operation.
An authenticated client with the required scopes explicitly submits its exact
`plan_hash` to `execute` (or `flash` for flash/restore plans); the broker queues it immediately once current
principal, lease, device, firmware configuration and plan checks pass.
There is no approval argument.
Preparation alone never starts work. Repeated execution returns the original job;
connection loss never replays it. Inspect `status(view=job)` after an uncertain reply.
An explicitly requested DERBY smoke can retain [bounded numeric diagnostics](DERBY-DIAGNOSTICS.md)
for its requesting principal through the same read operation.

There is no product-level plan approval, image-delivery decision, capture policy,
arming window, confirmation keyword or operator-launch permission. Legacy
approval/policy/arming HTTP endpoints authenticate and check scopes, then return
`410 Gone` without effects. Legacy approval CLI/launcher entry points are retired;
they are not aliases for execution. `allow_mock_execution` and legacy unattended
flash permission settings grant no authority and are ignored.

Authentication and current scopes, registered operation capabilities, exact
principal/device/firmware bindings, exclusive ownership, bounded I/O, deadlines,
cancellation, verified flash readback and audit remain enforced. An expected
firmware hash is a configuration binding, not a measurement of running firmware.
Physical uncertainty remains a recovery barrier; removing approvals does not
acknowledge it. Agent/host platform approval controls are unchanged.

On upgrade, legacy approval-waiting and unstarted queued work is cancelled
without replay. Interrupted running work remains quarantined for review. Old
approval/grant records remain historical and cannot authorize new execution.
No approval clicks or manual database changes are needed for this migration.

## Console and job visibility

The broker sends bounded, best-effort job lifecycle and redacted warning/error/
progress diagnostics to stderr. Slow or closed consoles do not stop jobs; stdio
stdout stays JSON-RPC only. GUI firmware cards retain their operation, stage,
result and exact immutable plan, with explicit backup/write/restore effects.
See [console diagnostics and the artifact-lookup follow-up](FIRMWARE-BUILD.md#console-diagnostics).

## Registered source/build loop

A locally registered project/profile is the execution scope for authenticated
control principals. Prepare source fetch with project_id and the full commit SHA,
execute its exact plan_hash, then prepare/execute build with the owned source_receipt.
Omit session for these operations. There is no per-job operator approval, additional
grant registration, USB lease or local Codex execution intermediary. Normal
fetch/compiler failure or confirmed cancellation permits a corrected new job;
uncertain process/lock/durable state still stops for review. A connection loss
does not cancel or replay a job. Build executes trusted project code as the local
OS user, not in a sandbox. See [source snapshots](SOURCE-SNAPSHOTS.md) and
[external builds](FIRMWARE-BUILD.md). Device flash/restore/backup remain separately
requested and never follow automatically from successful build.

## Windows queue GUI

The GUI displays job plans, effects, lifecycle, results and retained-image previews
alongside session and queue controls. It has no approval buttons, pending-approval
badge or capture-permission panel. Submit execution through the authenticated
client. Cancellation and safe ownership controls remain available.

`submit kind=observe_firmware` receives bounded console output without commands;
`kind=restart` separately requests one fixed reset and bounded boot
observation. Neither happens at startup. See [device logs](DEVICE-LOGS.md).

Run `& .\Start-CardputerGui.ps1` after `uv sync --frozen` for authenticated mock
mode. See [GUI-WINDOWS.md](GUI-WINDOWS.md).

## Windows serial startup

The bundled [Start-Cardputer.ps1](Start-Cardputer.ps1) takes a packaged `cardputer.exe` and a broker profile (`-Exe`, `-Profile`) and handles
both process-only credentials, a separate visible server console and authenticated
readiness. See [START-WINDOWS.md](START-WINDOWS.md) for the one-command setup. This
helper starts no USB operation. Device work requires a separate authenticated execution request.

## Quick start (mock mode)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). The lockfile currently
resolves the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
to **2.2.0**, using its v2 `MCPServer` and `Client` APIs. No unreviewed shell script,
firmware toolchain, ESP-IDF installation, API key, or connected device is needed.

From this project directory, in PowerShell or a Linux terminal:

```sh
uv sync --frozen
uv run cardputer serve --config examples/mock-demo.toml
```

Keep that terminal running. In a second terminal, read status only:

```sh
uv run cardputer status
uv run cardputer logs
```

This unauthenticated mock startup is read-only. Execution requires a currently
valid authenticated control principal, including for synthetic jobs. For mock
execution, use the existing process-only credential setup in
[START-WINDOWS.md](START-WINDOWS.md), or the authenticated GUI launcher. With those
existing credentials available privately in the process environment:

```sh
uv run cardputer --auth-source environment serve --require-auth --config examples/mock-demo.toml
```

Start only one broker. Use an authenticated client in the same credential
environment and follow [prepare and explicit execute](#prepare-and-explicitly-execute).
Mock results establish synthetic transport/arbitration behavior; they do not
establish a physical flash, capture or Tunnel connection. Stop the owned server
with Ctrl+C when finished; active work keeps ownership until cleanup completes.

`examples/mock-demo.toml` selects synthetic operations only. The legacy
`allow_mock_execution` value is ignored; explicit execution requests are required.

## Connect Claude Code, Codex and Antigravity CLI

Use the existing authenticated broker on the same Windows machine. Start it with
the existing GUI/launcher and its client-key registry as described in
[STDIO-CLIENTS.md](STDIO-CLIENTS.md#1回だけ準備するもの).
The clients launch a tools-only **stdio adapter**, which forwards to the resident
Streamable HTTP endpoint `http://127.0.0.1:8765/mcp`; it does not start another
broker or acquire USB.

First complete the [private env-file setup](STDIO-CLIENTS.md#private-env-ファイル):
use an issued `remote_mcp` client key with `read` scope (and `control` only when
needed), from that broker's registry. The process-token mock setup above
is separate from this issued-key registration. The file contains `CARDPUTER_CLIENT_KEY` and,
optionally, `CARDPUTER_BROKER_URL`; keep it private and outside both the adapter
installation and Git checkouts. Registration stores its path, not the secret.
The following paths are placeholders for a stable source/venv installation;
substitute the actual paths locally.

### Claude Code

In PowerShell, after installing this project with `uv sync --frozen`:

```powershell
$AdapterPython = 'C:\Tools\cardputer-device-mcp\.venv\Scripts\python.exe'
$ClientEnvFile = Join-Path $env:LOCALAPPDATA 'DeviceBroker\local-client.env'
claude mcp add --transport stdio --scope local device-broker -- $AdapterPython -m cardputer_mcp.cli stdio --env-file $ClientEnvFile
claude mcp get device-broker
```

Run registration from the project where you will use Claude Code. The explicit
local scope is private to you in that project; project-shared JSON templates
remain available in [STDIO-CLIENTS.md](STDIO-CLIENTS.md#claude-code-のプロジェクト登録).
Start Claude Code there and inspect `/mcp`.
[Official Claude Code MCP commands/scopes](https://code.claude.com/docs/en/mcp).

### Codex

Use the same adapter and private file variables in PowerShell:

```powershell
codex mcp add device_broker -- $AdapterPython -m cardputer_mcp.cli stdio --env-file $ClientEnvFile
codex mcp list
```

This CLI registration uses the user MCP configuration. For project configuration
instead, use the existing [trusted-project TOML template](STDIO-CLIENTS.md#codex-のプロジェクト登録).
Start Codex in the intended project and inspect `/mcp`.
[Official Codex MCP guide](https://developers.openai.com/codex/mcp);
`codex mcp add --help` documents the command/argument separator.

### Antigravity CLI (Google)

The [official Antigravity CLI MCP guide](https://antigravity.google/docs/mcp)
documents stdio definitions in a workspace's `.agents/mcp_config.json`, or
globally in `~/.gemini/config/mcp_config.json`. Merge this entry into
`mcpServers`, preserving existing servers. Replace both absolute example paths;
the env-file path must point to the same private file prepared above, not a
literal placeholder. No secret belongs in this JSON.

```json
{
  "mcpServers": {
    "device-broker": {
      "command": "C:\\Tools\\cardputer-device-mcp\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "cardputer_mcp.cli",
        "stdio",
        "--env-file",
        "C:\\Private\\DeviceBroker\\local-client.env"
      ]
    }
  }
}
```

In the Antigravity CLI prompt, run:

```text
/mcp
```

Use its MCP manager to reload the configuration and inspect connection status.
This is the documented JSON/interactive-manager route; verify it against your
installed CLI version. Actual Antigravity client compatibility has not been
runtime-tested here.

For all three clients, begin with `status(view=identity)`, `status(view=device)` and
`status(view=logs, stream=broker)`. Connection and registration do not grant device ownership or
change the broker's registered operation capabilities. Separate work sessions and
shared-key identity are explained in [STDIO-CLIENTS.md](STDIO-CLIENTS.md).

## Prepare and explicitly execute

Use the existing authenticated broker and its private client environment:

```powershell
$lease = uv run cardputer --auth-source environment acquire --owner local_cli --seconds 300 | ConvertFrom-Json
$plan = uv run cardputer --auth-source environment prepare --session $lease.session --job trial-001 --test diagnostics | ConvertFrom-Json
# Inspect the immutable target and effects, then explicitly submit execution.
uv run cardputer --auth-source environment execute --session $lease.session --plan $plan.plan_hash
uv run cardputer --auth-source environment job trial-001
uv run cardputer --auth-source environment release --session $lease.session
```

The SHA-256 binds the epoch, session, backend, device, job and ordered stages.
Changing a bound field requires a new plan. Device execution keeps its original
submission deadline; renewal does not revive expired or cancelled intent.
Lease capabilities are private. Owner labels do not replace authenticated identity.

## Architecture and invariants

```text
Local CLI -------> one Streamable HTTP /mcp endpoint ----> resident broker
Second MCP client --^                                      |
Future authenticated Tunnel ingress (not connected) --------^
                                                        SQLite jobs
                                                        in-memory lease
                                                        single worker
                                                        mock or opt-in serial
                                                        bounded event cache
```

- The CLI always connects to the existing HTTP service. It never creates another
  broker, serial reader, or stdio-owned hardware process as a fallback
- A non-blocking process lock excludes cooperating brokers for the same OS user
  in the same OS temporary-directory namespace, across worktrees, HTTP ports and
  state directories. Another state lock protects database recovery/review
- One physical session owns a device operation sequence. Registered source/build
  jobs have their own serial execution lane and require no physical session
- Leases use monotonic time. Expiry cancels queued work and blocks further lease
  operations, but retains the active owner's device until the active adapter
  returns. A client disconnect does not cancel the worker
- Queued device jobs are cancelled by their lease owner. Source/build cancellation
  uses the authenticated original control principal and requests owned-worker
  shutdown. No process/lock is released until teardown is confirmed
- Job IDs are unique within the retained database. Retries of one immutable plan
  return its original job; a conflicting reuse is rejected. After lease expiry,
  read `status(view=job)` instead of trying to execute an old capability
- Restart cancels unstarted jobs and quarantines interrupted
  running jobs as needs_review. Nothing is replayed.
  Unacknowledged reviews remain diagnostic history; installation clears none
- Device disconnects and uncertain adapter errors retain diagnostic failure history.
  Ordinary source/build failure with confirmed cleanup is terminal without review.
  There is no implicit retry, port substitution, reset, flash or recovery operation
- Cache reads use `{epoch, sequence}` cursors. Different clients read identical
  events without consuming them. Gaps caused by eviction or restart are explicit
- There is one device/session, up to 32 queued jobs by default, 256 cached events,
  a maximum 100 events per read, a 16 KiB HTTP body limit, and a 10,000-record cap
  for each of plans, jobs, device requests and work sessions. History is never
  silently pruned: when a cap is reached new work is refused with
  `history_full_archive_offline`; run the offline `cardputer archive` command
  ([History archive](#history-archive)) as an explicit operator action

### Interrupted work

Historical `needs_review` jobs and restart-invalidated device requests remain in
SQLite with their original state, result, diagnostics and reviewed flag. They do
not block a fresh authenticated operation by themselves. `needs_review` reports
unacknowledged history; `execution_uncertain` reports current runtime uncertainty.
The `history` counts include reviewed jobs too.

New operations still require the current authenticated principal, explicit execution
request, live lease/intent and exact device/profile/artifact binding. Active work,
remaining owned processes, unconfirmed port close, process/output/cache uncertainty
and state-commit uncertainty continue to block the relevant resources. Failed work
is never replayed; already queued device jobs are cancelled after a failure. A new
job needs a new explicit request. Backup/LKG requirements and cancellation checks
are unchanged. Restart never clears history or proves a physical firmware state;
resource locks, target preflights and recovery receipt validation still apply.

Inspect `cardputer job JOB_ID` and `cardputer status` before deciding what to do
next. Retained device logs are memory-only, so inspect them before stopping a broker.
A historical failure does not establish the current physical device state or claim
that the failed action succeeded. An actual uncertain resource must be resolved by
its owning lifecycle; acknowledging history cannot release that resource.

While the broker runs, the local operator GUI can acknowledge a `needs_review` job
(**Acknowledge review...** on the selected job) or device request (**Acknowledge
request...** on the Connection tab). This calls the local-only `/operator/review`
route (local origin and `control` scope; not an MCP tool). It sets only the reviewed
flag; state, result and diagnostics stay as recorded, nothing is retried and the
device is not touched. An acknowledged row no longer blocks HOME recovery. A job that
is the source of a running HOME recovery cannot be acknowledged until it ends, and
acknowledging a capture failure gives up its HOME recovery.

`status` lists each set runtime latch under `safety.uncertainty` with a fixed reason
(for example `serial_ownership` / `serial_close_unconfirmed`). The GUI's **Re-check
resources...** button (local-only `/operator/recheck`) runs one concrete check per
latch and clears only latches whose check passes: the retained serial handle reports
closed (the existing confirmed-close helper; a port is never opened), an owned
build/flash process tree is confirmed exited (never killed), the flash driver's
confirmed close succeeds, or no job row is `running` and a SQLite write probe passes.
It runs nothing while device or registered-project work is active. Build output and
source cleanup latches have no side-effect-free check and are kept until restart.
It is never run automatically.

Optional history acknowledgement is also available offline:

1. Establish the physical/resource state and inspect the saved failure evidence.
2. Stop the broker using its existing owner/supervisor procedure.
3. Run `cardputer review --state-dir ABSOLUTE_EXISTING_STATE_DIR --job JOB_ID`.
   This explicit command immediately acknowledges the job; there is no keyword prompt.
   Restart-invalidated device requests instead use
   `cardputer review-request --state-dir ABSOLUTE_EXISTING_STATE_DIR --request-id REQUEST_ID`.
4. Restart with the same existing config, credentials and state working directory.

Use the deployed `cardputer.exe` for a Windows distribution, or `uv run cardputer`
for a source installation. Resolve relative `state_dir` against the deployment's
original working directory. Review returns `reviewed: true` and `replayed: false`;
it does not rerun or mark the original action successful. Do not delete or manually
alter the database to hide an interrupted operation. No acknowledgement is required
solely to start a safe new job.

### History archive

`cardputer archive` moves old finished history out of the live SQLite store so new
plans and jobs are accepted again. It is offline only, like `review`: it takes the
broker's state lock and refuses with `stop_broker_before_history_archive` while a
broker runs. It never touches a device, a backup file, an LKG object, a build output
or a source cache, and it replays nothing.

1. Stop the broker using its existing owner/supervisor procedure.
2. Run `cardputer archive --state-dir ABSOLUTE_EXISTING_STATE_DIR --dry-run`. The JSON
   report gives the counts that would move (`archive`), the counts that stay
   (`kept`) and why rows are protected (`protection`). Nothing is changed.
3. Run the same command without `--dry-run`. `--keep-latest N` (default 1000,
   0–10000) also keeps the newest N registrations.
4. Restart with the same existing config, credentials and state working directory.

Kept regardless of age, together with everything they reach (any string in a kept
plan or flash summary that names a job or plan, continuation/deferred links and HOME
recovery links; a linked group moves or stays whole): unfinished jobs, unreviewed
`needs_review` jobs and device requests, waiting/owning device requests and their
work sessions, every source fetch, backup, flash and restore job (with their plans,
so source receipts, backups and restore receipts stay resolvable), the current LKG
and all LKG history rows (`completed_job` and `baseline`), the newest succeeded build
of each build output slot with a durable artifact reference, pending deferred jobs,
issued continuation tickets, and any job whose plan is missing or unreadable.

Moved rows are copied to a new
`<state_dir>/history-archive/<UTC stamp>-<id>/history.sqlite3` (the same columns,
plus an `archive_info` table), verified by re-reading that file, and only then deleted
from the live store in one transaction; rows keyed by a moved job (execution
requests and audit, flash summaries, fixed-trial evidence, build references,
continuations, HOME recovery rows, timeline order) move with it. The moved jobs'
durable job logs (`job-logs/<job_id>.jsonl`) and captures (`captures/<job_id>/`)
move into the same directory; kept jobs keep theirs, and their status and log reads
are unchanged. A job log or capture that no live job owns (for example left by an
interrupted run) moves on the next run. An interruption after the copy leaves rows
in both places, never lost. The archive is a plain SQLite file for offline
inspection; the broker never reads it. Job IDs are unique within the live database
only: once archived, an old job ID is no longer deduplicated.

## Tool surface

The server exposes exactly six tools. Old tool names are removed with no alias.

| Tool | Safety class |
| --- | --- |
| `status` | read-only |
| `submit` | mutating, non-destructive |
| `execute` | mutating, non-destructive |
| `cancel` | mutating, non-destructive |
| `session` | mutating, non-destructive |
| `flash` | destructive: queues only flash and restore plans |

`submit` returns `execute_tool`: use `flash` for flash/restore plans, `execute` otherwise.
The broker derives the plan type from the stored plan and refuses a plan sent to the
wrong tool (`wrong_execute_tool` / `wrong_flash_tool`) before changing any state.
Backup, flash and restore remain separate explicit operations.

## MCP tools

The endpoint is `http://127.0.0.1:8765/mcp`. Tools with arguments accept a nested
`request` object. All request models reject extra fields.

| Tool | Distinguishing argument | Effect |
| --- | --- | --- |
| `status` | `view`: `identity`, `targets`, `source`, `device`, `session`, `job`, `logs` (with `stream`: `broker`, `build`, `device`, `app`), `capture`, `apps` | Read-only: backend/device/broker status, shared cached events, your completed device log report from RAM, durable job results, any read-scoped caller's view of a succeeded job's PNG (five-minute RAM copy, then the saved copy in state until pruned beyond 200 images / 64 MiB) |
| `submit` | `kind`: `fetch`, `build`, `backup`, `flash`, `restore`, `app_session`, `derby_fixed`, `derby`, `capture`, `restart`, `observe`, `observe_firmware`, `hello`, `diagnostics`, `workflow`; `job_id`, and `session` for device kinds | Persist an immutable allowlisted plan; performs no device operation |
| `execute` | `op`: `run` (`plan_hash`, optional `session`), `app_input`, `home_recovery` (`job_id`, `source_job_id`) | Queue the exact plan (any plan except flash/restore); device operations require an owned live lease. `home_recovery` requests one bounded DERBY exit after recorded capture failure/close |
| `flash` | `plan_hash`, optional `session` | Queue the exact flash or restore plan; refuses any other plan |
| `cancel` | `target`: `job` (`job_id`, optional `session`, `plan_hash`), `request` | Cancel your job (a bounded recovery keeps ownership until confirmed close) or a waiting device request |
| `session` | `action`: `open`, `request`, `renew`, `release`, `close` | Obtain a private session capability, request/renew/release the device lease (release cancels queued work), open/close logical work sessions |

See [bounded HOME recovery](HOME-RECOVERY.md) for exact eligibility, authenticated execution,
effects, freshness checks and single-use failure behavior. It does not clear other
review barriers or grant capture/flash authority.

Mock kinds: `diagnostics`, `workflow`, `capture`. Serial kinds:
`observe`, `observe_firmware`, `restart`, `hello`, `capture`,
with authentication and explicit execution. Capture requires an authenticated principal-bound lease even in
mock mode. No generic shell, raw serial, arbitrary file path, command upload,
or fault injection tool exists. Flash accepts only registered device IDs and
successful owned build/backup jobs and a separate explicit `flash` request. Restart is a fixed, separately
requested job, not an arbitrary serial command tool. Domain failures use structured `{ok:false,error:...}`;
SDK validation failures return a generic MCP tool error without reflecting inputs.

## Windows 11 USB boundary

The project has **read-only** pySerial inventory and pure device-identity matching:

```sh
uv run cardputer ports
```

This explicit command lists current port name, VID, PID, USB serial number,
location and description. It never opens a port. Port metadata can identify your
device, so keep real output private. Inventory is not called during startup/demo.
`DeviceSelector` requires VID/PID plus serial number and/or USB location; resolving
an absent or ambiguous device fails closed. The current COM name is discovered
anew rather than hard-coded. Tests cover a synthetic COM-name change and Linux
`/dev/ttyACM*` naming without touching USB.

The opt-in pySerial adapter supports bounded observation, HELLO and HOME capture; see
[SERIAL.md](SERIAL.md). Actual Windows driver/reset behavior, physical reconnect
identity and Linux permissions/exclusive-open behavior still need authorized
hardware validation. No bootloader PID substitution is available. The registered Windows ROM-only
flash worker is implemented and still requires authorized hardware validation. A broker lock cannot prevent an unrelated terminal or flash
program from opening a real device; all participating tools must integrate with
one broker and the adapter must also acquire OS-level serial exclusivity.

## Network and private/public separation

- Loopback-only binding is enforced. Non-loopback clients, untrusted Host/Origin,
  forwarded identity headers and non-JSON POSTs are rejected. Proxy headers are
  disabled; the local client ignores environment HTTP/SOCKS proxies
- Mock authentication is opt-in; GUI and Tunnel preparation require it. Serial
  mode always requires bearer authentication and principal-bound leases. Optional
  scoped keys add local hash-only issuance, expiry and revocation. The process
  lock coordinates trusted processes; it is not a hostile-OS-user boundary
- Do not expose the broker through an arbitrary public reverse proxy. The pinned
  official Tunnel client uses the same authenticated loopback endpoint, with
  local origin/scope/Host checks intact. Hosted connector routing and actual
  account-backed invocation remain unverified; no cloud access is provisioned
- Local CLI and authenticated Tunnel traffic terminate at this same broker.
  Never deploy a second broker per client or per worktree. Merely labeling a
  client `remote_mcp` does not establish a Tunnel connection
- Public source and examples contain synthetic values only. Runtime databases,
  approvals, private local configuration and `.env` files are ignored by Git
- The shared event cache contains structured synthetic events or exact allowlisted serial
  markers, not raw firmware logs. Unknown serial text, capabilities and raw driver
  exceptions are not saved in SQLite/event logs. Separately requested device log
  reports are bounded, redacted, principal-bound and retained only in RAM; see
  [DEVICE-LOGS.md](DEVICE-LOGS.md)
- Config `state_dir` is relative to the directory from which you launch the server.
  Use a user-owned local directory on Windows; POSIX new directories use 0700 and
  the database uses 0600. Avoid shared/synchronized runtime-state folders

## Claude gateway (Tailscale Funnel, optional)

`cardputer gateway --config <broker TOML>` lets a claude.ai custom connector reach
the broker. It runs an OAuth 2.1 server (Dynamic Client Registration for Claude's
callback only) whose single user is one Google account, and relays authenticated
MCP requests to the broker's `/tunnel/mcp` alias with its own `remote_mcp` key.
`/operator/*` is never exposed. Tokens live in memory: after a gateway restart,
Claude asks you to connect again.

Prerequisites (one-time, outside this repository):

- Tailscale: MagicDNS and HTTPS certificates enabled, and the `funnel` node
  attribute for this machine. Nothing else may be configured on HTTPS 443.
- Google Cloud: an OAuth web client with the redirect URI
  `<public_url>/oauth/google/callback`. Note your Google account's `sub`.
- A broker key: `cardputer keys issue --origin remote_mcp [--control] --write-env
  <private env file>` (the stdio env-file format; `keys rotate --write-env` updates
  it and the gateway re-reads it after the broker rejects the old key).
- A private file containing only `CARDPUTER_GATEWAY_GOOGLE_CLIENT_SECRET=<secret>`.

Both files must be outside repositories and the installation, like the stdio env
file. The broker TOML needs `[tunnel] ingress_enabled = true` and a section:

```toml
[gateway]
public_url = "https://<machine>.<tailnet>.ts.net"   # must be this machine's name
port = 8770
google_client_id = "<id>.apps.googleusercontent.com"
allowed_google_sub = "<your Google sub>"
broker_env_file = 'C:\path\to\private\gateway-broker.env'
google_client_secret_file = 'C:\path\to\private\gateway-google.env'
funnel = false   # true opens Funnel while the gateway runs
```

With `funnel = true` the gateway first checks Tailscale (machine name, Funnel
permission, nothing on HTTPS 443), listens on 127.0.0.1, then runs
`tailscale funnel --yes http://127.0.0.1:<port>` in the foreground as an owned
child and waits until Funnel lists that target. Stopping the gateway (Ctrl+C)
closes Funnel first; if Funnel exits, the gateway stops. In Claude, add a custom
connector with the URL `<public_url>/mcp`.

Unverified so far: a real Google login, a real Claude connection, and that a
force-terminated foreground Funnel always clears its configuration (check
`tailscale funnel status` after an abnormal exit).

## Verification and limits

```sh
uv sync --frozen
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest -q
```

Tests include real TCP/HTTP MCP discovery and tool calls, client disconnect and
reconnect, two origins sharing one owner, whole-workflow locking, active lease
expiry, queue cancellation, explicit execution, retry deduplication, recovery
review, disconnect quarantine, port changes, bounded log fan-out, rejection of
credential-like inputs in error messages, and HTTP trust boundaries.

Automated checks run in a Linux cloud environment using Python 3.12. The original
mock status/demo and later mock Tunnel registration/read-only calls were also
verified in a user-run Windows 11 setup. One user-run HOME render-buffer capture
has also succeeded. General driver control-line behavior, the new firmware/log
restart/log workflow, physical display evidence, and flash remain **unvalidated**.
A user-run passive device log observation has returned real telemetry. Existing
firmware projects and external IDF environments are registered through private
local build profiles; MCP never supplies arbitrary project/profile paths. Paths
and output-root overrides stay in that private configuration.

The capture API returns a bounded, authenticated MCP ImageContent after a
`capture` job (`capture_home` plan) succeeds. Pixels stay in the broker's bounded memory
cache for five minutes and are also saved under `<state_dir>/captures/<job_id>/`
(oldest pruned first beyond 200 images or 64 MiB), so they survive RAM expiry and
broker restarts. Any authenticated principal with current read permission can
read them, whoever requested the capture, while the capturing principal is still
valid. The local GUI preview falls back to the saved copy too. See [CAPTURE.md](CAPTURE.md) for the
HOME-only behavior and render-buffer evidence limits. No synthetic bitmap is
presented as device evidence. Arbitrary current-app screenshots are unavailable.
