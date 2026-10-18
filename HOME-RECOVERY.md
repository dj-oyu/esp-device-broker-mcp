# Bounded DERBY HOME recovery

This is a separate, explicitly requested operation for a DERBY capture that failed
after the app launched. It sends exactly one `q`, requires a fresh `APP_STOPPED`
then `HOME_READY`, confirms serial close, and checks authority again before
resolving the original capture job's review barrier. It does not capture an image.

## Eligibility and effects

The broker must have recorded confirmed serial close for that exact serial
`derby_smoke` plan, with a capture timeout or invalid capture result and a completed
capture command. Only new failures observed by this implementation can acquire
that durable receipt. Existing historical failures are not backfilled from a log,
an operator assertion, or a guessed device state.

The requesting principal must own the source failure and retain read/control
access. The old lease must be released; no lease, queued/running job, build,
uncertain ownership, or other unreviewed job/request may remain. Device identity,
port, configured firmware hash, broker epoch, locks and current requesting-principal authority
are checked before and during I/O and again after close. Old capture policies and approval records grant no recovery authority.

`q` exits DERBY and may save app state. Serial open/close can cause a driver or
firmware reset even with DTR/RTS disabled. The operation sends no reset command,
Esc, app launch, flash, backup or restore command; it never reconnects or retries.
Unexpected boot or app-launch output fails the operation. Expected device and firmware configuration remain bound to the source plan;
configuration is not cryptographic evidence of the actual running firmware.

## Explicit authenticated recovery request

1. Inspect the failed source job and release its idle lease through ordinary MCP.
2. Call `request_home_recovery` with a new `job_id` and eligible `source_job_id`.
   The current source-owning principal must have read and control scopes. This
   explicit request validates and durably consumes the source's single recovery
   attempt, then starts bounded recovery immediately. It is not a proposal waiting
   for a local click, keyword, attestation, arming window or launcher.
3. Poll `job_status` with the recovery job ID. If the response was lost, inspect
   this existing job rather than creating another recovery attempt.
4. `cancel_home_recovery` takes the job ID and plan hash. Cancellation stops further
   input and retains ownership until serial close is confirmed.

The old review/approval CLI, operator routes and HomeRecovery launch-slot flow
are retired. Legacy mutation endpoints check authentication/scopes and return
`410 Gone` without execution. No old approval row is converted into authority.
An explicit new request may use a valid previously recorded source-close receipt;
upgrade does not create one or automatically request recovery.

## Freshness, limits and failure behavior

Admission expires after 30 seconds, without renewal. Execution must finish within
15 seconds of explicit request and before admission expires. The sole reader drains old
input to an empty physical receive boundary before sending `q`. Preconnection,
cached or preinput HOME markers cannot establish success. A partial pending line
or a nonquiet stream fails. After the write, a fresh ordered pair of exactly one
`APP_STOPPED` and one `HOME_READY` is required within eight seconds, followed by a
quiet empty boundary. The entire operation reads at most 16 KiB, including drains.
No input buffer reset discards evidence. Raw receive buffers are cleared when
the worker ends and are not persisted or returned as images/logs.

Observed HOME alone is insufficient: confirmed close and final live authority
must also pass. Success atomically marks only the original failed capture job
reviewed and the recovery job succeeded, with its final audit record. The original
failed state, result and diagnostics remain available. No flash, backup, restore,
other review barrier or last-known-good firmware is changed.

Any failure leaves the original review unresolved and creates a failed recovery
review. The consumed source never becomes reusable, including after cancellation, crash,
restart or lost reply. Cancellation/expiry before input performs no input. An uncertain close retains device ownership and locks. A blocking driver
close can outlast the protocol deadline: cancellation does not pretend it closed
or release the reservation. Further operator investigation is required; this API
does not acknowledge those failures or clear them broadly.

## Validation and deployment

Automated verification must use synthetic serial devices and clocks and authenticated
HTTP. It does not establish physical HOME, serial-driver behavior or firmware
compatibility. Native Windows and physical validation remain separate evidence.

This changes the execution behavior of the existing HOME recovery MCP tool. After an authorized
deployment/restart, compare actual `tools/list` schemas/annotations against the
cloud client's callable metadata. For a custom ChatGPT connection, use Plugins
Refresh, verify metadata, then a new conversation as described in `AGENTS.md`.
No dynamic metadata refresh is implemented here.
