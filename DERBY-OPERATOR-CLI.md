# Exact DERBY supervisor entry

This entry is separate from the capture-only slot. It adds no MCP approval tool,
arbitrary input, policy, token file, persistent permission or automatic arming.
The trusted supervisor must explicitly arm each window using the verified module,
installed executable/hash, existing broker URL, existing state working directory
and its existing local-only credential. Preserve configuration, DB, SDK, LKG and
artifacts. Neither this library nor the request CLI deploys or restarts anything.

## Execution window

After preparing a **new** `derby_smoke` plan and submitting it with
`wait_for_approval=true`, the trusted launcher constructs `DerbyLaunchScope` with
the authorized `broker_epoch`, `job_id`, `plan_hash`, requesting `principal_id`,
`device_fingerprint`, and `expected_firmware_sha256`. No target is selected by
the first IPC request. Never reuse a cancelled job or released lease.

```python
from cardputer_mcp.derby_operator_launch import DerbyLaunchScope, DerbyOperatorLaunchSlot
from cardputer_mcp.operator_launch_pipe import WindowsOperatorLaunchPipe, current_user_sid

scope = DerbyLaunchScope.model_validate(authorized_derby_scope)
slot = DerbyOperatorLaunchSlot(
    EXE, verified_exe_sha256, CWD, url, local, current_user_sid(),
    scope=scope, phase="execution", lifetime_seconds=30,
)
entry = WindowsOperatorLaunchPipe(slot)
```

Poll and close the entry through the existing supervisor ownership lifecycle.
Only the pipe name is public runtime metadata. The library deliberately does not
discover credentials or change an existing launcher. Scope must come from the
trusted launcher's authorization, never an incoming request file.

The token-free `derby-operator-request` CLI requires `--pipe`, `--epoch`, `--job`,
`--plan`, `--principal`, `--device-fingerprint`, and `--firmware-sha256` every time.
Its only execution actions are `--action derby-plan-review` then
`--action derby-approve-and-start --review-revision <returned-revision>
--attestation "DEVICE HOME AND FIRMWARE VERIFIED"`.

Review validates the actual immutable serial DERBY plan, fixed `SMOKE` definition,
target, firmware configuration, recipient, submission state and attestation. The
configured SHA and operator attestation are **not cryptographic device attestation**;
the operator must establish HOME and the exact normal firmware before approval.
Fixed navigation is Esc/a/u x16/d x12/e, then automatic demo observation up to
120 seconds, one s at photo or fallback, one q with fresh HOME_READY. Whole job
limit is 180 seconds. Serial open/close can reset the device; exit may save state.
Cancel or lease loss stops new writes, including q, and can leave the game running.
No replay command, arbitrary key, keytest build, flash, reopen or retry is added.

The window defaults to 30 seconds, caps at 60, and is shortened conservatively
from the start of review to the smaller remaining original intent/lease expiry.
It cannot be extended to wait for the race. The fixed local CLI re-reads the live
plan before approval; the broker atomically checks the exact review revision and
current authorization and retains its existing execution-time authority guards.
Changing epoch, lease, principal, work session, firmware or original intent cannot
reuse a stale review. A new valid review requires explicit new authorization.

## Image window after successful completion

After independently observing successful completion of that exact job, trusted
launcher code explicitly arms a **fresh** `phase="image"` slot with the identical
scope. Do not auto-arm a window from an IPC request or re-arm an uncertain decision.
This separate window needs no lease renewal and performs no device operation.

1. `--action derby-image-review` invokes existing local `image-review` for only
   that job. The broker supplies epoch/plan/recipient/device/firmware provenance.
   The slot checks that provenance, verifies the decoded PNG SHA, freezes its SHA
   and delivery revision, and shortens its deadline to image retention remaining.
   Pixels stay inside the local child/supervisor; IPC returns only bounded scope,
   SHA and revision. Local child output is bounded to 256 KiB for this review;
   other commands and IPC replies retain their existing 64 KiB bound.
   Image review alone also permits a stdout line up to 256 KiB, so the JSON's
   base64 line reaches the decoder intact. Normal stdout and all stderr retain
   the 4096-byte line limit. Oversized lines are omitted without partial content;
   the aggregate response limit and one-use failure semantics remain unchanged.
2. `--action derby-image-decision --sha256 <reviewed-sha>
   --delivery-revision <reviewed-revision> --decision approve` (or `reject`)
   invokes the existing local decision API. A missing/mismatched revision, SHA,
   scope, expired window, rejected/expired image or revoked recipient fails.
   The broker rechecks image identity and delivery revision after the recipient
   callback, immediately before recording the decision; an epoch change cannot
   commit a consent based on a stale revision.
3. The requesting principal uses existing MCP `read_capture`. Broker checks the
   successful job, five-minute RAM retention, exact image decision, requesting
   principal and current read authority. A render buffer does not prove physical
   LCD transfer or coherent capture; one image cannot prove temporal immobility.

Execution approval and image decision are each consumed **before** launching the
privileged child. Timeout, malformed response, failure and lost reply never reset
consumption. Inspect the original job/image after an uncertain reply; do not retry
the operation or automatically replenish a slot. Wrong/duplicate/reentrant review
also closes the window. Same-user IPC does not prove human presence or isolate
other processes running under that account. All SID, executable/hash/path,
environment filtering, child-tree cleanup and credential redaction protections of
the existing transport remain in force.
