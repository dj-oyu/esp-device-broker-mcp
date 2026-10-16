# One-capture supervisor CLI entry

This optional Windows helper finishes the existing live CLI launch path when the
resident supervisor alone holds the local operator token. It is disabled unless
trusted launcher code explicitly opens one user-authorized capture window for the
current broker epoch. It does not start a broker, alter credentials,
edit configuration/state, drive GUI events, or expose an MCP/TCP approval route.

The slot permits one `plan-review`, followed by at most one `approve-and-start`
attempt. The executable/hash, loopback URL and working directory are supplied by
the launcher, never the request. The exact broker epoch, job, plan hash and
requesting principal may be fixed in advance. Otherwise, the first authenticated
same-user review selects that exact tuple once; the fixed CLI must verify it is a
live, already-submitted capture for that principal and epoch. This explicitly
allows same-account callers to select the one target within the short window;
it is not proof of user presence. IPC cannot retarget, extend or replenish a slot.
A new window requires a new explicit local authorization; never automatically
construct windows from incoming requests.

## Security boundary

- A fresh, local-only named pipe has an explicit protected DACL for the current
  Windows user SID, one instance, a non-inherited handle and remote-client rejection.
  Generic pipe write rights are avoided because they also allow instance creation.
  The server additionally verifies the connecting process token's user SID; the
  sender checks the server process SID. There is no client impersonation.
- Same-user processes are not separate security principals. This cannot prove an
  interactive human click, distinguish local agents from other code running under
  that account, or sandbox registered build scripts. Such code can select the one
  target if it was not preselected, then request only that frozen tuple. The pipe
  name is public metadata, not an authorization
  secret. Administrators and a compromised supervisor/account are outside this
  boundary. [Windows pipe access documentation](https://learn.microsoft.com/windows/win32/ipc/named-pipe-security-and-access-rights).
- Requests are bounded strict JSON. Unknown fields, mismatched targets, another
  SID, stale revision/epoch, non-capture or unsubmitted reviews, expired authority
  and wrong HOME attestation are refused. Scope lifetime defaults to 30 seconds,
  caps at 60, and is conservatively shortened to the original intent/lease expiry.
  The broker still performs all of its final current-authority checks.
- Approval is consumed **before** creating its child, including timeout, failure
  or a lost response. No automatic retry, renewal, prepared-to-submitted upgrade,
  policy creation or image-retention extension occurs. Image delivery needs both
  the launcher's one-image allowance and the request's explicit flag.
- Only OS allowlisted variables plus `CARDPUTER_LOCAL_TOKEN` reach the fixed CLI
  child. Remote/client/Tunnel/cloud tokens, proxies, PATH and Python overrides do
  not. The parent environment is unchanged. No token appears in arguments, IPC,
  files, receipts or displayed diagnostics. Mutable child environment blocks are
  cleared; this does not promise secure erasure of Python strings from RAM.
- Existing `BuildProcess` ownership starts the Windows child tree inside its job
  and reaps only that tree. Output is bounded and stderr is not relayed. An uncertain
  result requires inspection of the original job; it never authorizes a retry.

## Integrate into the existing launcher

Deploy only after independent verification. Keep the existing installed EXE,
verified hash, broker URL, config path and **state working directory**. A current
EXE that already supports `plan-review` and `approve-and-start` can remain the
privileged child; the new request client may come from the new verified package.
Load this reusable module from the exact verified repository source/package, not
from a user-writable request path. Do not modify the existing window-reopen file
into an approval trigger.

After broker readiness and explicit local authorization, the launcher can open
one bounded window without a second arming API or a request file:

```python
from cardputer_mcp.operator_launch import CaptureLaunchScope, OperatorLaunchSlot
from cardputer_mcp.operator_launch_pipe import WindowsOperatorLaunchPipe, current_user_sid

slot = OperatorLaunchSlot(
    EXE, verified_exe_sha256, CWD, url, local, current_user_sid(), None,
    broker_epoch=device_status["broker_epoch"],
    allow_image_delivery=explicit_one_image_consent,
    lifetime_seconds=30,
)
entry = WindowsOperatorLaunchPipe(slot)
# Only entry.name may be added to the existing nonsecret runtime receipt.
```

If the authorized tuple is already known, pass
`CaptureLaunchScope.model_validate(authorized_capture)` instead of `None`; then
even the first request must match it. The epoch is always pinned at construction,
so a broker restart cannot move an existing window to another epoch. A failed
first review consumes the window; no replacement plan is selected automatically.

Poll `await asyncio.to_thread(entry.poll)` in the existing supervisor loop. Catch
`OperatorLaunchError` within that branch, publish only a fixed refusal/uncertain
status, and leave the broker/Tunnel running. Successful review does not start a
job. On approval, expiry or refusal, leave the slot consumed; do not recreate it.
The module stores no request/response files. Avoid copying full operator replies
to ordinary logs; display the review only to the same-user requesting operator.

Close the entry on supervisor shutdown. If `entry.close()` returns false, retain
the slot/owned-process handle, report uncertain CLI shutdown and refuse new slots
until its own tree is confirmed stopped. Do not route this into a generic cleanup
that kills or restarts the independent broker/Tunnel, clears review state or
replays a capture. The module never searches process names or unrelated PIDs.

The unprivileged caller needs no operator token:

```powershell
cardputer operator-launch-request --pipe PIPE_NAME --action plan-review --epoch EPOCH --job JOB_ID --plan PLAN_HASH --principal PRINCIPAL_ID
cardputer operator-launch-request --pipe PIPE_NAME --action approve-and-start --epoch EPOCH --job JOB_ID --plan PLAN_HASH --principal PRINCIPAL_ID --review-revision REVIEW_REVISION --attestation "DEVICE HOME AND FIRMWARE VERIFIED" --allow-image-delivery
```

Use the revision from that exact review. Real-device attestation must reflect
actual HOME/firmware verification; it is not inferred from requesting the command.
Mock uses the explicitly empty attestation. A lost approval reply is ambiguous:
inspect the original job using the existing authenticated client; never prepare
another job or retry automatically. The slot does not retrieve or transmit pixels;
the original principal uses existing MCP `read_capture` after successful capture.

## Verification and deployment boundary

Synthetic tests cover exact scope/revision, deadlines, replay/concurrency, unrelated
environment secrets, path/hash changes, native SID/DACL checks, real formal CLI
children and authenticated mock HTTP. Package CI additionally exercises the
extracted request EXE and fixed privileged EXE through a real pipe against a fresh
mock-only broker. These checks neither operate hardware nor validate physical HOME,
firmware identity, USB reliability or user-session behavior. Production integration,
restart and a real capture remain separate authorized operations.
