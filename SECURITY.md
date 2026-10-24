# Security boundary

Mock mode is a trusted-local-machine engineering demo. Opt-in serial mode requires
per-client bearer authentication, authenticated lease ownership and explicit
execution of an immutable registered plan. Neither mode is a supported public Tunnel deployment. Do not
expose this listener to a public proxy/tunnel or assume a client label grants cloud
access. Host/origin checks and loopback binding are defense in depth.

A local OS user who can inspect process environments or read/write state can obtain
credentials, modify history or bypass application checks. Do not
grant an untrusted MCP agent shell/database access to those resources and then
claim a product confirmation prompt protects against that agent.

Windows uses fresh process-scoped credentials for each broker launch; no registry
or persistent user environment writes are performed. POSIX runtime credential
files enforce owner/mode/symlink checks and are normally removed on shutdown.
Automatically managed runtime-auth.json credential-file mode is intentionally
unsupported on Windows. See SERIAL.md. The explicit, user-managed stdio client
env file described below is a separate mechanism.

Serial observation, HOME/DERBY capture, restart and explicit firmware
backup/flash/restore require separate explicit client requests. Automated hardware tests use
synthetic devices; packaging success does not prove physical operation.
Opening/closing a serial port may cause driver control-line glitches. Plans
state this risk; HELLO additionally states that it exits the current application.
No arbitrary shell, raw serial command, automatic flash/restore or rollback API is exposed.

Cancellation waits for the actual serial worker to finish. An unconfirmed port
close latches ownership and prevents broker lock release. Uncertain results require
review and are never replayed automatically. Review acknowledgement is not evidence
of successful physical recovery.

Do not share runtime databases, authentication/lease tokens, raw device logs,
serial numbers, local filesystem paths or credentials in public issues. Reproduce
with synthetic data and include only version, platform and minimal steps.

## Registered profile execution

Existing locally registered build/source profiles define the scope for current
authenticated control principals. Source/build require no additional permission
record or individual operator approval. The MCP caller cannot register projects,
expand repository/submodule allowlists, select arbitrary paths or change tools.
Prepare binds exact inputs/settings and execute deduplicates the durable job.
Revocation/expiry, cancellation and registration changes are checked during work
and again at success publication. Connection lifetime grants no execution authority.

Public source fetch parses bounded data under strict archive/tree checks. Build
executes trusted registered project/profile code as the local OS user, including
selected repository commits; it is not a filesystem/network sandbox. See
FIRMWARE-BUILD.md before registering code. Normal failures with confirmed cleanup
end without global review. Uncertain process/lock/persistence remains fail closed.
Device leases, registered operation capabilities and flash/readback checks remain enforced. Existing legacy
review records must still be explicitly acknowledged; installation clears none.

## Optional scoped keys and Tunnel preparation

The local `keys` commands explicitly create/verify/revoke/rotate credentials.
Issue/rotate require terminal confirmation and reveal a token once; terminal
scrollback is not erased. A failed/interrupted disclosure revokes the new key.
Only SHA-256 verifiers and metadata are stored. The registry path is opt-in and
must be in an OS-private directory; Windows ACLs are an operator prerequisite.
Distinct keys bind distinct principals to leases. Read-only keys cannot prepare
or execute work. Control keys can explicitly execute plans within their registered
scope; no additional product approval is required.
Revocation/expiry never force device handover during active or uncertain work.

The Tunnel launcher never installs software or creates external account access.
Default mode is local authenticated mock discovery. Connect is explicit and uses
operator-supplied official client/ZIP metadata and private interactive runtime key
input. The native client receives an OS-variable allowlist plus remote credentials,
never the local operator token. No credentials are in argv or saved configuration.
Its raw stdout/stderr are discarded. The vendor's loopback admin UI has an internal
redacted log ring, so there is no claim of universal vendor-memory sanitization.
The parent console and process memory remain within the trusted local OS boundary.

Forwarded Authorization overrides static Tunnel Authorization. One shared static
key is one security principal regardless of terminal count; physical source identity
is unavailable. Invalid overrides are rejected, but connector-specific forwarding
and actual cloud compatibility require account-backed validation. `/readyz` is not
proof of successful OpenAI authentication or polling. No public broker bind or
network-security setting changes are made by these launchers.

## Logical sessions and local queue management

Logical resume capabilities and physical lease capabilities are different,
unguessable memory-only values. Every grant has a new generation. Durable records
contain public metadata only. Restart invalidates both kinds and quarantines
prior waits/owners rather than restoring authority. Current principal/scope/expiry
is rechecked at every request and before background grant. Cached enqueue auth
is not sufficient for a later grant.

Lease or session expiry, revoked authorization and queued cancellation never hand
the device over while a backend worker or uncertain close remains active. Legacy
immediate acquisition cannot bypass FIFO. Operator reorder uses a queue revision
bound to the broker epoch; idle release uses the exact public lease generation.
GUI actions use the same broker's local-control-only HTTP routes, not tunneled MCP
tools. GUI closing never requests broker termination. There is no force-stop action.

## GUI-owned Tunnel lifecycle (v0.5)

No lifecycle endpoint is added to remote MCP. GUI commands run in a separate
worker and operate only on an explicitly created client. The Windows adapter uses
suspended creation with atomic job-list assignment before execution, kill-on-job-
close, and an inherited-handle allowlist containing only NUL. Ownership is by
handles/job object, never a searched process name or stale PID. Unconfirmed
cleanup retains handles and the launcher lock, blocking another launch.

Native Windows behavior remains unverified here; tests substitute the Win32 API.
Official client bytes remain pinned. Runtime API and remote MCP credentials are
one-operation inputs, omitted from argv/files/logs/repr, with no local operator
credential in the child's environment. A read-only authenticated status(view=identity)
check rejects local-origin keys before launching. No keys are automatically issued.

Canonical [tunnel] metadata rejects secret and auto-connect fields. Explicit Save
validates the complete serialized TOML and preserves non-Tunnel semantic values
before atomic replacement; comments/formatting may change. Conflicting legacy
metadata and concurrent/unsafe file changes fail closed. GUI connection requires
the canonical config receipt to match; external edits require reload.

Health uses fixed loopback endpoints, disabled proxies/redirects/compression,
a byte cap and a whole-operation timeout. Native text is never displayed. A
recent successful control-plane poll is only transport evidence, not application
registration or MCP invocation. Observed cloud 401/403 stops the owned client;
there is no automatic restart. Disconnect/restart never calls a broker mutation.

## Local stdio adapter (v0.6)

The adapter is a tools-only transport client of the existing loopback broker. It
never starts another broker, opens serial, acquires/releases a device, or cancels
a broker job merely because its stdio transport is cancelled or closed. Failed
calls are not replayed; an unknown outcome must be checked against existing job
state. Future explicitly requested operations can establish a new connection.

The selected env file is absolute, bounded and outside repositories/install roots.
Only literal CARDPUTER_CLIENT_KEY and optional CARDPUTER_BROKER_URL assignments are
accepted. The adapter does not evaluate expressions, merge environment values,
or write the key. Issued cpk_ keys only; every upstream session verifies a current
remote/read principal. Local operator credentials are refused. Two local clients
sharing this key share a security principal; work sessions remain explicit and
have separate resume capabilities. Use a separate cloud connector key.

Registered storage roots (source `cache_root`, build `build_root`) are pinned to their
resolved directory so an operator-made symlink/junction is allowed; containment is enforced
below the pinned root and a retargeted root fails closed. Executable/tool identity checks
remain strict about links.

POSIX file privacy is checked. Windows regular-file/reparse/identity checks do not
prove private ACLs; the operator must establish and verify those permissions.
The adapter does not change ACLs or issue actual credentials. Same-account and
administrator access remains trusted. Default logger suppression includes the
SDK's unqualified client logger because malformed protocol notifications can
otherwise expose payload-bearing validation errors. Stdout is MCP-only.
