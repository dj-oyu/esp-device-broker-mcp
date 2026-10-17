# Bounded DERBY HOME recovery

This is a separate, explicitly approved operation for a DERBY capture that failed
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
port, configured firmware hash, broker epoch, locks and local operator authority
are checked before and during I/O and again after close. A source capture policy
does not grant recovery approval.

`q` exits DERBY and may save app state. Serial open/close can cause a driver or
firmware reset even with DTR/RTS disabled. The operation sends no reset command,
Esc, app launch, flash, backup or restore command; it never reconnects or retries.
Unexpected boot or app-launch output fails the operation. The operator verifies
the exact device and firmware, without claiming the device is already HOME.

## Request and local approval

1. Read the failed job and release its idle lease through the ordinary MCP tools.
2. Call `request_home_recovery` with new `job_id` and the eligible `source_job_id`.
   This persists a fixed plan but performs no USB I/O.
3. The authenticated local operator runs `home-recovery-review --plan HASH`.
   Inspect the source job, principal, epoch, device fingerprint, firmware hash,
   effects and remaining authority.
4. Run `home-recovery-approve --plan HASH --review-revision REVISION
   --attestation "DEVICE AND FIRMWARE VERIFIED FOR ONE DERBY EXIT"` through the
   existing local CLI authentication flow. This consumes the source's one attempt
   durably before starting a worker. The generic plan approval path cannot approve
   this plan. A remote MCP client cannot approve it.
5. Poll `job_status` with the recovery job ID. The same authorized principal can
   inspect bounded stage/audit events and the approving principal/review revision.
   `cancel_home_recovery` takes that job ID and plan hash; cancellation stops further
   input but retains ownership until serial close is confirmed.

The token-free Windows path uses `home-recovery-operator-request` with an explicitly
armed `HomeRecoveryLaunchSlot` and `WindowsOperatorLaunchPipe`. Trusted local code
must supply the executable hash, cwd, endpoint, local token, owner SID, and exact
`HomeRecoveryLaunchScope`: epoch, job ID, plan hash, principal, source job ID, device
fingerprint and firmware hash. There is no first-request target selection. It
permits one dedicated review and one approval attempt only, never capture, image
delivery or arbitrary actions. It uses the existing same-user pipe ACL and child
environment/output limits. Same-account processes share this authority; the pipe
does not prove human presence. A lost approval reply consumes the window: inspect
the persisted job instead of retrying. The slot is a trusted-launcher API; merely
installing the server does not arm it.

## Freshness, limits and failure behavior

Admission expires after 30 seconds, without renewal. Execution must finish within
15 seconds of approval and before admission expires. The sole reader drains old
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
review. The consumed source never re-arms, including after cancellation, crash,
restart or lost reply. Admission cancellation/expiry before consumption performs
no input. An uncertain close retains device ownership and locks. A blocking driver
close can outlast the protocol deadline: cancellation does not pretend it closed
or release the reservation. Further operator investigation is required; this API
does not acknowledge those failures or clear them broadly.

## Validation and deployment

Automated tests use synthetic serial devices and clocks, authenticated local HTTP,
and the native Windows pipe to the formal CLI. They do not establish physical
HOME, serial-driver behavior or firmware compatibility. Review and packaged
Windows verification remain separate deployment gates.

This adds two MCP tools and dedicated local CLI/routes. After an authorized
deployment/restart, compare actual `tools/list` schemas/annotations against the
cloud client's callable metadata. For a custom ChatGPT connection, use Plugins
Refresh, verify metadata, then a new conversation as described in `AGENTS.md`.
No dynamic metadata refresh is implemented here.
