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
startup information, an ordinary HELLO cycle, and approved HOME render-buffer
capture for the inspected PocketJS firmware protocol. Automated verification uses
synthetic serial devices; one user-run Windows HOME capture has succeeded. These paths require explicit
startup opt-in, authenticated clients and exact local per-job approval. See
[SERIAL.md](SERIAL.md) for effects, limitations and user-run Windows instructions.

Mock remains the default. `mock_flash`, `mock_boot`, and `mock_test` are synthetic.
Registered external [ESP-IDF 6.0.1 builds](FIRMWARE-BUILD.md) and separately approved
[backup/flash/restore](FIRMWARE-FLASH.md) are implemented. Flash defaults to disabled;
physical device behavior remains unvalidated. Completion uses readback and passive
boot IDF/ELF evidence, with no HELLO key commands.
[HOME capture](CAPTURE.md) returns a bounded MCP PNG after confirmed serial close;
its mock image is visibly labeled TEST / NOT DEVICE. A [local queue GUI](GUI-WINDOWS.md) and [logical work sessions/FIFO](SESSIONS.md)
are now implemented and mock-tested. The optional [Windows Tunnel preparation](TUNNEL-WINDOWS.md) launcher
and [local scoped-key management](KEYS.md) are available. A user-run Windows mock Tunnel registration and read-only MCP calls
were verified; cloud lifecycle remains external to broker status. Nothing opens USB automatically at startup.

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
approved and never follow automatically from successful build.

## Windows queue GUI

Version 0.12 lets a client submit an exact plan before local approval. Call
`execute_test` with `wait_for_approval: true`; a job still requiring manual
approval returns `awaiting_approval`. An existing valid approval or automatic
permit continues to allow immediate queueing.
The GUI then shows **Approve & start**, and approval queues that submitted job
without waiting for another agent request. Preparation alone does not submit
execution. Waiting never extends its original lease deadline, and cancellation,
revocation or restart cannot replay the request. Physical work still waits for
safe exclusive ownership; approval is not a promise of zero hardware latency.

Version 0.11 adds a separate, manually approved `restart_and_observe` job for
native ESP32-S3 USB Serial/JTAG. It issues one fixed reset sequence, keeps the
same device lease, and captures bounded boot logs. A disconnected port may be
reopened once only after confirmed close and exact USB identity checks.
Control-line completion does not prove application boot; lost early logs and
reopen gaps remain explicit. No real reset or flash was run during automated
verification. See [restart and logs](DEVICE-LOGS.md#restart-once-and-observe-boot-output).

Version 0.10 adds a manually approved `firmware_observe` job and
`read_device_logs`. One serial owner receives for up to 30 seconds without sending
commands. A bounded, redacted report is kept in RAM for five minutes and is
readable by the authenticated principal named in the approved plan. Firmware
fields come only from observed ESP-IDF startup logs; missing logs mean unknown,
and the printed ELF digest prefix is not a firmware binary hash.
See [device logs and firmware observation](DEVICE-LOGS.md).
This version also restores the local HOME diagnostic text after image approval
by re-fetching only under fresh local authorization and the original expiry.

Version 0.9.3 adds an unchecked, per-job local HOME-line diagnostic option beside
manual serial-capture approval. It keeps at most one bounded candidate line in RAM
for 120 seconds and shows it only in the local timeline. It never enters MCP
results, durable history, or automatic exports. Reviewed historical failures now
display **Failed · reviewed**. See [local diagnosis](CAPTURE.md#optional-one-line-local-diagnosis).

Version 0.9.2 corrects capture-failure text and adds bounded receive diagnostics.
An executed job no longer asks the operator to execute it, and an unsuccessful
capture is distinguished from a successful image that is no longer retained.
See [capture diagnostics](CAPTURE.md#diagnosing-an-unsuccessful-capture).

Version 0.9.1 uses compact accordion cards with clearer borders and status badges.
Completed cards start collapsed; requests awaiting a decision show their relevant
controls. Manual expansion choices survive background refreshes. Technical hashes,
raw JSON and diagnostic codes live under details; a thumbnail can be enlarged
inside the same card. Required operation effects and image recipients remain
beside their approval actions.

Version 0.9 presents each job in one main-window timeline: request and effects,
inline plan approval, execution state, captured-image preview, and image-delivery
approval stay together. Details expand inside the card; approval only starts a
job when its client has explicitly submitted execution. The Pending badge highlights
new manual requests. Expired images
remain recognizable as completed captures with unavailable pixels, not absent jobs.
The existing queue controls remain beside the timeline; permissions and Tunnel
configuration stay in their settings panels. See [CAPTURE-PERMISSIONS.md](CAPTURE-PERMISSIONS.md).

Version 0.7.1 adds **MCP info**: a five-second, read-only live catalog/version probe
using the GUI's existing in-memory authentication. It displays no credentials or
complete tool payloads and never starts jobs. See [GUI-WINDOWS.md](GUI-WINDOWS.md).

Version 0.5 adds an integrated **Tunnel...** panel: metadata editing in the
broker TOML's `[tunnel]` section, explicit connect/disconnect/manual restart,
owned-process monitoring, and separate local/cloud evidence. Runtime keys are
masked and never saved. No automatic connection or restart occurs. Windows
process ownership is covered by synthetic API tests, not native Windows validation.
See [TUNNEL-WINDOWS.md](TUNNEL-WINDOWS.md).


Run `& .\Start-CardputerGui.ps1` after `uv sync --frozen` for authenticated mock mode.
See [GUI-WINDOWS.md](GUI-WINDOWS.md). Real mode requires explicit opt-in and approval.

## Windows serial startup

The bundled [Start-Cardputer.ps1](Start-Cardputer.ps1) now handles project location,
both process-only credentials, a separate visible server console and authenticated
readiness. See [START-WINDOWS.md](START-WINDOWS.md) for the one-command setup. This
helper starts no USB operation and leaves every hardware approval requirement intact.

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

Keep that terminal running. In a second terminal in the same project directory:

```sh
uv run cardputer status
uv run cardputer demo
```

Expected demo result:

```text
MOCK DEVICE / REAL MCP HTTP: one broker, two clients
1. Local CLI owns the whole flash -> boot -> test session
2. Second MCP client: device_busy (same broker, no second USB reader)
3. MOCK workflow passed; both clients read identical cached logs
4. Retrying the job ID returns the original result; no replay
5. Local releases; second MCP client acquires the device
PASS: exclusive ownership, shared logs, durable jobs, safe retry
No USB / real flash / Tunnel connection was used
```

`uv run cardputer demo --recording` spaces the visible results across about
21 seconds, suitable for a short screen recording. Show the MOCK label: the demo
proves actual MCP transport and arbitration, not an actual firmware flash or a
Tunnel connection. It does not print private lease capabilities. Stop the server
with Ctrl+C; shutdown drains any active mock workflow before releasing the lock.

`examples/mock-demo.toml` is an explicit **local-operator opt-in for synthetic
operations only**. The default policy requires individual local approval.

## Safer approval workflow

Start `uv run cardputer serve --config examples/mock-safe.toml` instead. MCP
clients can prepare plans but cannot grant approvals. In PowerShell:

```powershell
$lease = uv run cardputer acquire --owner local_cli --seconds 300 | ConvertFrom-Json
$plan = uv run cardputer prepare --session $lease.session --job trial-001 --test diagnostics | ConvertFrom-Json
uv run cardputer approve --state-dir .state/safe --plan $plan.plan_hash
# Inspect the printed immutable plan, then type the exact requested APPROVE phrase
uv run cardputer execute --session $lease.session --plan $plan.plan_hash
uv run cardputer job trial-001
uv run cardputer release --session $lease.session
```

`approve` is a local operator command, not an MCP tool. It only writes an approval
record to the local SQLite database while the broker is running. The broker
atomically consumes that approval and inserts the exact job in one transaction.
The SHA-256 binds the broker epoch, session, synthetic device, backend, job ID,
test, and ordered stages. Changing any field invalidates it. Approvals cannot be
reused for another job, lease, or broker restart. MCP annotations are descriptive;
actual enforcement is in the broker. If an agent is given shell/database access,
that agent is inside this local-operator trust boundary; an `APPROVE` prompt is
not a security boundary against that agent.

For the GUI workflow, submit before reviewing: `execute_test` with the same
session and plan hash plus `wait_for_approval: true`, or the CLI's
`execute --wait-for-approval`. This returns immediately with an `awaiting_approval`
job. After inspecting **Approve & start**, one local click approves and queues
that exact request. The original lease deadline also bounds the intent, even if
the client later renews the lease. Use `cancel_job` to cancel an unstarted request.
Repeated submission cannot create another execution. Without this explicit
option, the earlier approve-then-execute workflow remains available.
Submitted requests require the live GUI review; the legacy SQLite `approve` CLI
rejects them with `submitted_plan_requires_gui_approval`. Its existing
prepared-only mode remains available. Changing a plan's submission state while
that CLI consent prompt is open invalidates the approval.

Lease capabilities authorize operations within a session. Keep them out of
public videos, chat, shared terminal history and screenshots. Display names
`local_cli` and `remote_mcp` are informational labels in mock mode. Serial mode
binds ownership to distinct authenticated clients.

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
  read `job_status` instead of trying to execute an old capability
- Restart cancels unstarted registered-profile jobs and quarantines interrupted
  running jobs and legacy device work as needs_review. Nothing is replayed.
  Existing unacknowledged reviews remain blocking; installation clears none
- Device disconnects and uncertain adapter errors quarantine work for review.
  Ordinary source/build failure with confirmed cleanup is terminal without review.
  There is no implicit retry, port substitution, reset, flash or recovery operation
- Cache reads use `{epoch, sequence}` cursors. Different clients read identical
  events without consuming them. Gaps caused by eviction or restart are explicit
- There is one device/session, up to 32 queued jobs by default, 256 cached events,
  a maximum 100 events per read, a 16 KiB HTTP body limit, and a 10,000-record cap
  for each of plans and jobs. History is never silently pruned: archive/reset
  offline as an explicit operator action when the cap is reached

### Interrupted work

1. Inspect `uv run cardputer job JOB_ID` and `uv run cardputer status`
2. Stop the broker
3. Run `uv run cardputer review --state-dir YOUR_STATE_DIR --job JOB_ID`
4. Inspect the saved result and type the exact `REVIEW JOB_ID` phrase
5. Restart the broker, acquire a new lease and prepare a new job if appropriate

Review acknowledgement does **not** rerun or mark the original action successful.
Do not delete the database to hide an interrupted operation. In serial mode, an
operator must establish the real physical state before acknowledgement.

## MCP tools

The endpoint is `http://127.0.0.1:8765/mcp`. Tools with arguments accept a nested
`request` object. All request models reject extra fields.

| Tool | Input summary | Effect |
| --- | --- | --- |
| `device_status` | none | Read backend/device/broker status |
| `read_logs` | optional cursor, limit | Read shared cached events |
| `read_device_logs` | job ID | Read your approved, completed device log report from RAM |
| `acquire_session` | owner, lease_seconds | Obtain private session capability |
| `renew_session` | session, lease_seconds | Extend an unexpired lease |
| `release_session` | session | Release idle ownership, cancel queued work |
| `prepare_test` | session, job_id, test | Persist immutable allowlisted plan |
| `execute_test` | plan_hash, optional session / wait_for_approval | Run a registered source/build plan; device operations retain lease and approval |
| `job_status` | job_id | Read durable job result |
| `read_capture` | job_id | Read your completed PNG from five-minute memory cache |
| `cancel_job` | session, job_id | Cancel queued work only |

Mock tests: `diagnostics`, `firmware_workflow`, `capture_home`. Serial tests:
`passive_observe`, `firmware_observe`, `restart_and_observe`, `hello`, `capture_home`,
with mandatory local approval and
authentication. Capture requires an authenticated principal-bound lease even in
mock mode. No generic shell, raw serial, arbitrary file path, command upload,
or fault injection tool exists. Flash accepts only registered device IDs and
successful owned build/backup jobs with independent exact local approval. Restart is a fixed, individually
approved job, not an arbitrary serial command tool. Domain failures use structured `{ok:false,error:...}`;
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
  exceptions are not saved in SQLite/event logs. Separately approved device log
  reports are bounded, redacted, principal-bound and retained only in RAM; see
  [DEVICE-LOGS.md](DEVICE-LOGS.md)
- Config `state_dir` is relative to the directory from which you launch the server.
  Use a user-owned local directory on Windows; POSIX new directories use 0700 and
  the database uses 0600. Avoid shared/synchronized runtime-state folders

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
expiry, queue cancellation, approval consumption, retry deduplication, recovery
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
`capture_home` job succeeds. Pixels remain only in the broker's bounded memory
cache for five minutes and are accessible to the requesting authenticated
principal with current read permission. See [CAPTURE.md](CAPTURE.md) for the
HOME-only behavior and render-buffer evidence limits. No synthetic bitmap is
presented as device evidence. Arbitrary current-app screenshots are unavailable.
