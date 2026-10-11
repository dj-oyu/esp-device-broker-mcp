# Verification

## v0.12 Submitted execution and immediate local approval dispatch

Final aggregate: **3028 tests passed** (77.46 seconds). Ruff lint and format checks
passed for 119 Python files; strict mypy passed for 40 source modules. Frozen
offline uv sync and wheel/source-distribution builds passed. Package version is
0.12.0; dependency versions did not change.

An explicit `execute_test(wait_for_approval=true)` request can create an inert
`awaiting_approval` job. The local GUI reviews its immutable plan and execution
intent together; **Approve & start** atomically consumes approval and queues the
exact submitted job. No further client call or approval polling is needed.
Prepared-only approval remains inert. The original submission lease deadline is
not extended by later renewal, and unstarted requests can be cancelled. Restart
cancels approval-waiting requests without replay; existing queued/running restart
recovery remains fail-closed.

New coverage includes 44 broker lifecycle cases, 20 HTTP/MCP and CLI contract
cases, 57 GUI/timeline cases and five legacy CLI consent cases. Independent review
reproduced and verified fixes for cancellation, expiry, revocation and validator
removal during pre-execution callbacks. Final checks occur after all such
callbacks and before backend work; the independent probes record zero backend
calls for invalidated requests. The review also verified physical ownership,
uncertain-close blocking, exact review meaning, no automatic capture-policy
promotion of a pending manual request, and absence of capabilities in history.

The legacy SQLite approval CLI rejects submitted requests in favor of the live
GUI review. Prepared-only CLI approval checks the unchanged plan and job state
inside a write transaction after its prompt, rejecting concurrent submission.

The firmware parser now recognizes the official ESP-IDF 6.0.1 startup format of
8–16 lowercase hexadecimal prefix characters followed by `...`. Configuration
defaults to nine characters; the startup buffer caps it at sixteen. Synthetic
boundary, malformed-input, chunk-splitting and GUI metadata checks preserve the
distinction between an observed unverified prefix and a full firmware digest.

All new checks used synthetic data, clocks, authentication and serial backends.
No real USB operation, user credential or Tunnel account was used. Native Windows
rendering and physical approval-to-device latency were not measured here.

Version 0.2 verified on 2026-10-02 in a Linux cloud workspace, Python 3.12,
locked MCP Python SDK 2.2.0. **No actual USB device was opened or operated.**

Passed:

- `uv sync --frozen`
- `uv run ruff check src tests`
- `uv run ruff format --check src tests`
- `uv run mypy src` (strict, all 14 source modules)
- `uv run pytest -q` (208 tests)
- `uv build` (wheel and source distribution)
- Actual localhost Streamable HTTP server + original mock CLI demo
- Actual HTTP + real serial-adapter code + synthetic serial port: authentication,
  owner binding, exact local approval, HELLO marker validation and ownership handover

The suite includes 81 original mock/broker/HTTP cases, 41 authentication cases,
20 serial-adapter tests, 26 independent synthetic serial safety regressions and
one authenticated serial-backend HTTP integration case. Credentials and serial
identities in tests are deliberately synthetic; no external-service credential
or hardware setup was performed.

Serial review specifically reproduced and fixed stale buffered acknowledgements,
startup ordering, duplicate coalesced teardown, unconfirmed port close, and
cancellation of a worker wrapper while its underlying reader still ran. The
review suite passes after those fixes.

The original mock status/demo was separately reported successful by a user on
Windows 11. That validates the mock workflow there, not the new serial adapter,
Windows authentication setup, driver/reset behavior or the full test suite.

Not run or not implemented:

- Actual Windows USB open/read/write/close, driver control-line behavior and
  physical HELLO execution with this adapter
- Real firmware image attestation, flash/reboot, subprocess-tree handoff, GRID,
  fault injection or screenshot capture
- Tunnel authentication/deployment or cloud-controller access
- Firmware build/ESP-IDF or existing GUI integration
- Remote Git publication or CI execution

Read SERIAL.md before any user-authorized hardware operation. A successful
synthetic test is not proof of physical-device success.

## Windows launcher increment

Start-Cardputer.ps1 was added without changing application backend/auth code.
Its 15 tests cover listener/lock refusal, generic errors, bounded readiness, idle
state checks and an actual authenticated HTTP status check against a synthetic
serial backend, with zero port opens. The full suite passes (184 tests).
Independent static review checked PowerShell 5.1/7 constructs, path quoting,
environment inheritance, failure cleanup and console visibility. No PowerShell
executable was available here, so native parsing/Windows launch remains unverified.

## Sanitized failure-diagnostics increment

The full suite now passes 208 tests. New tests distinguish silence, noisy
unrecognized prefixes, read/write errors and marker timeout; retain safe logical
step/expected-marker/count fields; migrate older history without clearing review;
and reject raw error/secret reflection. Pinned firmware source 22f6d54 and ordinary
ESP_LOG/ANSI fixtures confirm that being on HOME alone is not a reason to bypass
the initial Back acknowledgement. No hardware or control-signal changes were run.

## v0.3 scoped keys and Tunnel preparation checkpoint

444 tests pass; Ruff and strict mypy (17 source modules) pass. Includes actual
loopback HTTP tests for current MCP and three legacy versions, a synthetic relay
contract, authenticated mock mode, registry/operator security, and mocked native
client supervision. The new tests use synthetic credentials only. Independent
reviews fixed foreign-owned ancestor handling, interrupted key disclosure, Windows
console allocation and console credential lifetime. Native PowerShell 5.1/7,
official client execution, account-backed Tunnel discovery and hardware were not run.
The official v0.0.15 Windows AMD64 archive was read-only inspected and its published
ZIP digest/member digest checked; it is not installed or included in this package.

No logical cross-client waiting queue or management GUI is included in this
checkpoint; those are separate subsequent work. Existing device_busy behavior stays.

## v0.4 logical sessions, FIFO and native queue GUI

Final aggregate: **608 tests passed**, Ruff lint/format passed, strict mypy passed
for all 20 source modules, and `uv sync --frozen --offline` installed the local
0.4.0 package successfully. No dependency expansion was needed for Tkinter.

New coverage includes separate resume/per-grant capabilities, same-key distinct
work sessions, reconnect, FIFO/legacy fairness, cancellation races, waiter versus
lease expiry, live revocation/scope checks, active/uncertain-close holds, stale
lease and plan rejection, restart invalidation/quarantine, bounded history/views,
slow-metadata TTL expiry, local-only operator auth and stale-reorder revision CAS.
Independent review found and fixed an old-owner dequeued job being spuriously
quarantined after handover even though no backend operation began.

An actual native **Linux** GUI was exercised on the cloud desktop against an
actual authenticated loopback HTTP broker with a synthetic backend: three logical
sessions, one owner, two waiters; select/reorder/cancel; reject the release dialog;
confirm idle release; observe exactly the next waiter receive a fresh lease.
The final ASCII labels were visually checked. Clicking Close window left the
broker reachable via authenticated HTTP before the disposable fixture cleaned up
its own broker. The GUI itself did not stop broker work.

This is not Windows-native GUI/PowerShell verification, a cloud Tunnel connection,
or physical Cardputer validation. No real API key or persistent user access was
issued, no real serial port was opened, no hardware job retried, and no repository
was published. The pre-existing diagnostic and needs_review behavior is preserved.

## v0.5 GUI-owned Tunnel supervision and shared TOML

Final aggregate: **822 tests passed**, Ruff lint/format and strict mypy (23 source
modules) passed. New tests cover atomic Windows job assignment and restricted
handle inheritance through a synthetic Win32 API, subtree accounting and uncertain
cleanup, process loss/restart, auth-failure stop, repeated operations, secret
lifetime/redaction, role introspection, canonical/legacy conflict detection,
Unicode TOML round trips, atomic metadata save and external-config changes.
Health tests include real loopback slow-header and slow-body responses to verify
the total timeout covers headers as well as streamed body.

Native Linux Tk smoke used a **simulated Tunnel manager** and a real authenticated
mock broker with one gated running job. Verified masked credential entry, visible
unexpected exit code, no automatic reconnect, fresh input on reconnect/manual
restart, and close confirmation. After connect -> simulated exit -> reconnect ->
manual restart -> GUI close, authenticated broker state still reported the same
active job and backend execution count1. The fixture then completed and stopped
its own synthetic broker. The official tunnel-client was not executed.

Windows PowerShell, the Job Object implementation on actual Windows, official
client startup, account-backed cloud polling/app discovery and real USB were not
validated. No user key was issued, no account/Tunnel created, no network/security
setting changed, and no hardware operation retried. Existing private configs,
job history and needs_review remain outside the distribution archive.


## v0.5.1 HTTP denial diagnostics

Final aggregate: **932 tests passed** on CPython 3.12.14; Ruff lint/format passed
(58 Python files), strict mypy passed (24 source modules), and frozen offline uv
sync installed v0.5.1. No dependency versions changed.

Fixed-size process-local denial counters preserve existing guard/auth decisions
and error bodies. The new GET operator endpoint requires local control authority
and has no MCP-tool counterpart. Tests cover each denial gate and its precedence,
unchanged responses, downstream redaction, no body reads, bounded/copied/saturating
snapshots, scope restrictions, exact paths/methods, real authenticated mock HTTP,
CLI failure redaction, and GUI stale/closed/repeated-action guards.

Cloud Linux Tk smoke visibly displayed a synthetic origin rejection as
origin_not_allowed=1, layer=guard, route=mcp, HTTP=403. Its other counters were zero.
The follow-on native dialog dismissal/reopen step was not verified after the
computer-action approval block. Unit tests cover repeat and close/result behavior.
Native Windows PowerShell/Tk and real hosted Tunnel request reproduction were not
run here. No real USB or account operation was performed.

See [HTTP-DIAGNOSTICS.md](HTTP-DIAGNOSTICS.md) for the idle-only source update and
GUI baseline/comparison steps. Counts start at broker startup and cannot recover
previous failures or prove individual request identity.


## v0.5.2 Explicit authenticated Tunnel ingress

Final aggregate: **1080 tests passed** on CPython 3.12.14 (30.92 seconds); Ruff
lint/format passed (60 Python files), strict mypy passed (24 source modules),
and frozen offline uv sync installed v0.5.2. No dependency versions changed.

An explicit strict boolean `[tunnel] ingress_enabled = true` enables exactly
`/tunnel/mcp` on the same authenticated loopback broker. Default remains false.
The five documented proxy metadata headers are discarded only after raw guard
checks and current remote/read authentication; the original guard and MCP auth
run again. No forwarding metadata becomes identity, URL, peer or permission.
Direct MCP/operator guards, device ownership and local plan approval remain.

81 dedicated ingress tests cover raw guard order, exact routing and mount/query
rejection, duplicates, unknown forwarding names, scope and live-key rejection,
revocation between gates, immutable ASGI scope/body/header forwarding, actual
SDK legacy/modern discovery over mock HTTP, shared leases/work sessions, approval
denial, operator isolation and no leaked input. All real HTTP fixtures assert
zero backend execution. Config tests cover default-off/strict booleans, legacy
conflicts, GUI preservation, derived child URL and stale broker refusal. Console
startup now also refuses local operator keys before launching its child.

Independent safety review found no remaining blocker after the console identity
and PowerShell configuration fixes. A separate real mock HTTP smoke verified
that forwarded metadata succeeds on the enabled alias, stays rejected on direct
/mcp, and bad Origin stays rejected. No real Tunnel account, Windows runtime,
USB operation or changed user configuration was exercised here. The actual
cloud request's precise forwarding names remain uncollected; unknown names and
non-JSON POSTs intentionally remain rejected. Cloud discovery is still subject
to the operator's next explicit mock validation.


## v0.5.3 Bounded denial context

Final aggregate: **1161 tests passed** on CPython 3.12.14 (32.50 seconds); Ruff
lint/format passed (61 Python files), strict mypy passed (24 source modules).
Frozen offline uv sync passed; only project version metadata changed in the lock.

Diagnostics add fixed physical entry/stage/method/media categories, a finite
known forwarding/duplicate-name vocabulary and a 16-entry process-local ring.
Unknown header names become a fixed other bucket. No values, body, raw paths,
keys, client IDs or arbitrary header names are retained. Post-normalization
observations describe the stripped scope at that rejection stage. The internal
ASGI entry marker affects diagnostic labels only, never identity or routing.
The five-header acceptance list and all existing security checks are unchanged.

189 focused core/ingress/diagnostic tests passed, including private-value/name
redaction, copied/bounded history, direct-vs-ingress classification and unchanged
POST JSON rejection. Client/GUI tests cover older snapshots, malformed contexts,
fixed output and four-row presentation. Independent review found no security
blocker; its stage-wording clarification is included in the guide. No native
Windows GUI or live account/device test was run for this update.


## v0.5.4 Discard the approved XFCC metadata on authenticated ingress

Final aggregate: **1167 tests passed** (33.54 seconds); Ruff lint/format passed
(61 Python files), strict mypy passed (24 modules), frozen offline uv sync passed.
No dependency versions changed.

The one policy delta is X-Forwarded-Client-Cert joining the dedicated ingress's
metadata discard list. Its contents are never parsed as certificates or used for
identity, and TLS verification settings are untouched. Existing raw guards and
remote/read authentication run before stripping. Tests include certificate-like
claims with no key, invalid key or local operator key, valid remote identity, and
continued direct/operator rejection. Other unknown forwarding names stay denied.
Recent GUI diagnostic rows now include each event's method and media category.

This fixes the observed rejected header category; real hosted discovery and
Windows execution still require the operator's next mock check. No actual account,
credential or device action was performed in these tests.


## v0.6.0 Shared local stdio adapter

Final aggregate: **1331 tests passed** (51.08 seconds) on CPython 3.12.14; Ruff
lint/format passed (66 Python files), strict mypy passed (26 source modules),
and frozen offline uv sync passed. No dependency versions changed. Includes
the v0.5.4 approved XFCC compatibility change.

The adapter forwards the current SDK-valid tools catalog and full typed tool
results to/from an existing authenticated loopback broker, including text/image
content, structured content, metadata and pagination. Real stdio subprocess tests
cover legacy and modern protocol exchanges, clean protocol stdout, fixed errors,
expired/revoked/local-key denial, redirects, bounded concurrency/timeouts, lost
responses without operation replay, cancellation, and EOF while a resident mock
job continues to hold its device ownership. No second broker or USB owner starts.

105 private env-file tests cover strict grammar, size, links/identity races,
repository/install exclusion, POSIX ownership/modes and safe diagnostics. CLI
wiring tests verify no implicit broker, no global credential merge, empty stdout
on startup failure, and equivalent nonsecret Claude/Codex registration templates.
Cloud test workspace Git boundaries prevent a genuine repository-external secret
file under its writable roots, so loader location tests use isolated filesystem
fixtures while protocol subprocess tests supply only synthetic keys directly.
No actual private client file or persistent user key was created.

Independent review reproduced one SDK logging issue: the unqualified client
logger could reveal malformed-notification payloads. The fix suppresses/restores
that logger too; an end-to-end regression and independent recheck pass. No
remaining blocker was reported for the current broker scope. Future tools with
x-mcp-header annotations require additional schema-loading compatibility work;
resources/prompts/tasks/input-required continuation are not advertised here.

Official Claude Code and Codex project configuration/trust documentation was
checked for the supplied templates. Actual Windows ACLs, native stdio execution,
Claude/Codex registration and user key setup remain operator-side validation.
Separately, the operator reported successful Windows hosted plugin registration
with v0.5.4. A separate read-only hosted mock status/identity check subsequently
succeeded; this is not an automated Windows-client or physical-device pass.

Broker status now reports tunnel=external_lifecycle_unknown and its configured
tunnel_ingress_enabled boolean. It no longer labels an externally managed Tunnel
not_configured, and it does not infer cloud connectivity from local ingress.

## v0.9.2 Capture receive diagnostics and failure text

Final aggregate: **2242 tests passed** (81.51 seconds) on CPython 3.12.14.
Ruff lint/format, strict mypy (37 source modules), frozen offline uv sync, and
diff whitespace checks passed. No dependency versions changed.

Fixed-schema counters distinguish traffic before and after the last allowlisted
write, complete versus incomplete lines, standard ESP log formatting, HOME token
appearance versus exact normalization, and known boot/reset/capture text shapes.
Fault, malformed-frame, byte-limit, and late-returned chunks are counted even
when rejected. A late HOME line is still rejected; diagnostic classification
never authorizes protocol progress. Ninety pure counter/sanitizer tests plus
serial-adapter regressions cover chunking, bounds, arbitrary-object rejection,
privacy, and receive phase accounting. Independent review rechecked the corrected
rejection ordering and reported no remaining blocker.

Timeline regressions verify that execution clears obsolete approval-waiting copy,
capture failure does not imply image expiry, and uncertain/restart outcomes are
explicitly unknown. Existing one-click approval, immutable-plan binding, expiry,
image permission, and accordion controls remain unchanged.

All serial inputs in this verification are synthetic. No real port, hardware job,
Tunnel account, or user credential was operated. Native Windows GUI behavior and
the cause of the reported physical capture timeout remain unverified. This update
adds evidence for a later separately authorized diagnostic run; it does not
replay work, clear review history, reset a device, or bypass HOME verification.

## v0.9.3 One-line local diagnosis and reviewed-history display

Final aggregate: **2434 tests passed** (81.76 seconds), Ruff lint/format passed
(105 Python files), strict mypy passed (39 source modules), and frozen offline
uv sync passed. Package version is 0.9.3; dependency versions did not change.

The manual serial-capture approval path can explicitly retain one HOME candidate
line, bounded to 1,024 bytes and 120 seconds in RAM. Tests cover default-off
behavior, exact plan/job/principal/epoch binding, consumed approval handoff,
repeat consent, rejected automatic-policy use, stale callback/clock/lease cases,
oversized first candidates, visible control-character escaping, preview gating
until job completion, and expiry/restart/revocation cleanup. Actual authenticated
ASGI MCP/operator integration confirms that only the local preview route returns
the escaped text; ordinary tool results, logs, timeline metadata and SQLite do not.

GUI tests cover unchecked consent, press/release identity changes, one bounded
inline preview, late results, stale/failed authentication refreshes, expiry and
window close. Reviewed historical failures display as reviewed while unrelated
current recovery guards continue to block acquisition. Independent review found
and verified fixes for stale GUI text, final callback expiry/lease validation,
and exception-frame retention. Blocked-close tests retain USB ownership while
clearing transient Reader copies and expiring the explicit private probe.

These checks used invented serial data and synthetic authentication fixtures.
No real USB port, hardware action, user credential or Tunnel account was operated.
Native Windows rendering remains unverified for this change. The update provides
local evidence for a separately approved diagnostic run; it does not establish
the real wire-format cause or relax the strict HOME acknowledgement.
