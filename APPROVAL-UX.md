# Operator consent and unattended flash design

Manual approval is the default. The local operator reviews the exact target,
operation, effects and recovery reference, then clicks Approve. There is no
phrase to copy or type in either GUI. The offline CLI displays the full plan and
asks for one `APPROVE`; the legacy `APPROVE <plan hash>` answer remains accepted.
A click on an already submitted request can queue it immediately. Approval of a
prepared-only plan does not submit execution. Image delivery remains a separate
permission, including the existing explicit capture delivery policies.

Consent records an operator decision; it is not physical-device or running-
firmware verification. The broker still binds the immutable plan and review
revision, current authorization, lease, owner and exclusive device use. Flash
still verifies artifact hashes, allowed regions and its bound recovery target.
A failure or uncertain close stops further work; approval does not undo partial
writes, imply data compatibility, retry writes or restore automatically.
The internal operator HTTP attestation enum remains wire compatible. GUI code
selects that existing enum for the displayed plan only after an explicit click;
it is no longer presented as a human password or claimed as machine evidence.
No MCP tool schema or remote approval permission changes are made.

## Explicit unattended flash registration

The dependent unattended-flash implementation adds local registration and
transient per-execution permission. `allow_flash` continues to enable the
operation; it never implies permission to skip manual approval. Missing/disabled
unattended registration preserves manual approval. Prepare reports eligibility;
only an explicit client execute request can queue work. Eligibility never
synthesizes an operator approval row. Restart does not replay the permission.

Local-only `unattended_flash` registration has an explicit strict
`enabled = false` default and exact pins for authenticated `principal_id`,
`device_id`, `project_id`, `operation = "firmware_flash"`, public source repository
and immutable source commit. The exact successful owned source receipt must also
be registered; a second receipt or a different build origin is ineligible. No wildcard principals, client paths, commands,
remote policy edits or silent enablement. A policy mismatch falls back to manual
review before enqueue; revocation after enqueue cancels rather than falling back
to another approval or retry.

Eligibility proves a successful owned source fetch receipt, its currently
verified repository/commit/tree/content digest, the same owner's successful build
from that receipt, and the retained manifest/artifact hashes. A build from an
arbitrary local worktree is ineligible. Pin the current local project/device
registration as well as the policy; mutable configuration cannot silently widen
a queued authorization.

Only flash with an existing owned verified artifact LKG recovery receipt is
eligible in the first version. Initial full backup, full-backup recovery inputs
and restore remain manually approved, separate operations. No automatic backup,
restore, retry or migration. Keep existing hash, region, security, same-device,
exclusive ownership and current-auth checks. Keep LKG promotion atomic with job
success only after readback, expected boot, confirmed close and final authority.
Failure, cancellation, crash or uncertainty preserves the previous LKG and the
existing stop/review behavior.

The implementation binds a transient authorization to the exact plan, broker
epoch, original lease/principal and registered provenance. Complete provenance is rechecked before enqueue and dispatch; live authority and
policy are rechecked after callbacks and immediately before final atomic success.
The source tree is not rehashed on each progress event. Restart cannot replay
transient permission.

Fake-device coverage: default manual despite `allow_flash`; exact-match
success without an approval row; every pin/receipt-owner/provenance mismatch;
local-worktree and backup/restore exclusions; tampered bytes/regions; revoked
policy or principal, expired/replaced lease and callback reentrancy; failed or
cancelled write/readback/close retaining LKG and stopping further work. CI exercises these cases with synthetic ROM drivers and private temporary state.
No production configuration, deployment or device operation is part of this work.

## Local configuration shape

This inert example illustrates a nested registration under an existing device.
The sample receipt is a placeholder; do not enable it without selecting and
reviewing the actual owned source receipt and established artifact LKG locally.
The normal existing device/tool/project registration remains required.

```toml
[flash_devices.example.unattended_flash]
enabled = false
principal_id = "example-principal"
device_id = "example"
project_id = "example-project"
operation = "firmware_flash"
source_receipt = "0000000000000000000000000000000000000000000000000000000000000000"
source_repository = "example/public-firmware"
source_commit = "0000000000000000000000000000000000000000"
```

The source receipt and retained manifest must agree on repository, commit, tree
and content digest, and the fetch/build must remain successful and owned by that
principal. Full provenance is checked before queueing and again before dispatch.
After current-auth callbacks, checks bind the same lease object, original intent
deadline, exact plan, current device/project registration and durable source/build
records. During device work and final atomic LKG commit, the broker rechecks
policy, authority and binding without repeatedly hashing the whole source tree.
Changing any registration invalidates previously prepared unattended plans.

A queued invalidation cancels with no device writes; a running invalidation stops
through the existing flash lifecycle and may require review for partial writes.
No fallback to manual consent occurs after an unattended job was queued. Existing
manually approved jobs retain their manual approval semantics.

The existing atomic success transaction invokes its authority guard twice: before
updates and after the provisional job/LKG updates. A private commit-only check
recognizes that provisional successful job only inside the open transaction;
normal device-work checks reject completed jobs. Both checks still revalidate
policy and current authority. Tests cover second-guard revocation rolling back
both updates and rejection of a completed job outside that transaction.
