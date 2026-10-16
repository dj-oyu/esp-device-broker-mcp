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

## Follow-up: explicit unattended flash registration (not implemented here)

This PR changes consent presentation only. Unattended flash requires a separate
reviewed implementation because it changes authority at enqueue, device dispatch
and final LKG commit. There is no functional unattended option or enabled example
in this PR. `allow_flash` continues to enable the operation; it never implies
permission to skip manual approval. Missing/disabled unattended registration must
preserve manual approval.

Proposed local-only `unattended_flash` registration has an explicit strict
`enabled = false` default and exact pins for authenticated `principal_id`,
`device_id`, `project_id`, `operation = "firmware_flash"`, public source repository
and immutable source commit. No wildcard principals, client paths, commands,
remote policy edits or silent enablement. A policy mismatch falls back to manual
review before enqueue; revocation after enqueue cancels rather than falling back
to another approval or retry.

Eligibility must prove a successful owned source fetch receipt, its currently
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

Implementation must bind a transient authorization to the exact plan, broker
epoch, original lease/principal and registered provenance. Recheck complete
provenance before enqueue and dispatch; recheck live authority and policy after
callbacks and immediately before final atomic success. Do not rehash an entire
source tree on each progress event. Restart cannot replay transient permission.

Required fake-device coverage: default manual despite `allow_flash`; exact-match
success without an approval row; every pin/receipt-owner/provenance mismatch;
local-worktree and backup/restore exclusions; tampered bytes/regions; revoked
policy or principal, expired/replaced lease and callback reentrancy; failed or
cancelled write/readback/close retaining LKG and stopping further work. CI must
exercise those boundaries before this option is exposed in a settings schema.
No production configuration, deployment or device operation is part of this work.
