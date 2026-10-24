# Registered app sessions (initial version)

`submit(kind="app_session", app={...})` fixes the registered app, fresh
launch, typed action allowlist, count/rate/duration/log limits and stop/teardown
impact. `execute(op=run)` runs it in the existing authenticated lease/job/worker.
There is no app-specific MCP tool, arbitrary source/path/shell or raw serial.

`status(view=apps)` reads a cached catalog without opening USB. Before the first session
it explicitly returns `source=unobserved`; the immutable plan binds this fact.
Execution reads every actual descriptor plus the device-reported firmware hash
and version before OPEN/START. Subsequent plans bind a canonical catalog hash,
boot, firmware hash/version and observation time, then recheck them at handshake.
A mismatch fails that job without START; the newly observed cache supports a new
explicit plan. Device-reported identity is not cryptographic attestation.

`execute(op=app_input)` targets one owned running job with expected boot/run/control-session,
command ID and sequence. Supported operations are `action.tap`, `status`,
`suspend`, `resume` and `stop`. Allowed actions are accept/back/up/right/down/left.
START uses ID/sequence 1; client commands begin at 2. Status never applies input.
Queued and sent describe broker/transport stages; accepted and consumed are device
ACKs. Consumed means delivery to the owner action path, not JavaScript, drawing,
saving or benchmark success. Duplicate same-ID same-content commands return their
state, different content conflicts, stale identities/order/queue/rate/budget are
explicit errors. Unknown ACK evidence causes STATUS-only recovery, never replay.

The broker permits up to **59 client commands**, reserving the firmware's first
user slot for START (firmware maximum 60 including START), plus STOP/CLOSE cleanup
slots in its 64-entry ledger. Normal broker pending input is bounded to 4, with
one additional reserved stop slot. Stop is exempt from the action budget/rate.
The firmware owner inbox remains 4. Both sides retain one outstanding wire command.

Initial launch is **fresh only**. A prepared initial `start="resume"` returns
`unsupported`: a new control session cannot import a dormant guest safely yet.
Within one job, suspend preserves the run and resume targets that same instance.
Back is an application action, not proof of teardown. Stop requires explicit
stopped state; HOME_READY is never termination evidence. Device-issued session
offers retire on close/TTL, preventing delayed OPEN/START from reacquiring a guest.

Timeout/cancel/authority loss stops new input and requests the pre-agreed bounded
STOP/CLOSE cleanup. Device TTL is at most 90 seconds; host input runs at most
87 seconds, reserving cleanup time. Uncertain command/teardown/port close yields
needs_review. A close failure retains the existing serial ownership barrier.
No disconnect/restart reopens, relaunches or replays input. No DERBY recovery is
used as a generic stop guarantee.

`status(view=logs, stream=app)` returns private control events and redacted logs on separate
cursors. Control is bounded to 128 entries; logs to 256 entries/256 KiB, each
line to 1024 UTF-8 bytes, retention to 300 seconds. Cursor responses include
oldest sequences, next cursor, has_more, gap, dropped counts and expiry. Console
flood cannot evict control ACK state. Untagged text is `observed_during_run`.
Known secret patterns are redacted through the existing principal-bound log cache.
Raw text is not persisted in public job history or shared status(view=logs, stream=broker).

`AppSessionClient` and `examples/app_session.py` use the same MCP calls for
local.hello and local.stress. The example connects to a separately started
broker, and never starts services or opens serial itself. Existing benchmark
parsers and quality gates are unchanged. VM PROBE availability is advertised by
the actual firmware build; unavailable descriptors return unsupported honestly.
No GUI overhaul, physical key edges/held input, strict timing tapes or arbitrary
artifact installation are included.

See [the versioned wire contract](APP-CONTROL-WIRE-V1.md). Source changes and
synthetic tests do not establish physical operation. Firmware must first pass
its own CI and explicit device validation. Deployment, flash, reboot, server
restart and MCP host metadata Refresh are separate future authorized steps;
this change performs none of them. Existing stdio metadata remains static until
the separately documented deployment/Refresh/new-conversation procedure is used.
