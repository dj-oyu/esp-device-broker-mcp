# Registered source → receipt build → explicit flash

The broker performs source acquisition itself. Normal operation requires the MCP
server on Windows, the already registered ESP-IDF 6.0.1/Python/toolchain and
Node tools, and USB for device operations. No local Codex/agent, worktree sync,
or external helper script is required. Fetch never invokes build or flash.

## One-time local project registration

Keep the existing project's trusted profile, short build_root and registered
Node/Git tools. Add source_repository to that registered project (template only):

```json
"source_repository": {
  "github_repository": "dj-oyu/cardputer-adv-pocketjs",
  "cache_root": "C:\\brksrc",
  "private_acl_confirmed": true
}
```

The operator must actually create/verify a private cache ACL on Windows before
confirming it. The application does not change authentication, connection
permissions, Git credentials or user worktrees. Choose a short root outside the
project, broker state, and build output roots. Paths/origin are registered locally;
MCP accepts only the project ID, full lowercase 40-hex commit, or an opaque receipt.

## Explicit cloud MCP requests

1. With a current authenticated control principal, prepare firmware_fetch_source
   with project_id, a fresh job_id and source_commit (full 40 hex). Omit session:
   source/build jobs require neither a USB lease nor individual operator approval.
   Prepare does not download. Storage shows cache counts/bytes and fixed limits.
2. Execute that exact plan_hash. The locally registered profile defines the
   allowed project/repositories/cache/output roots and tools. No separate grant
   registration or first-run approval is required. MCP cannot expand that scope.
3. Read_build_logs for the succeeded fetch job returns source.source_receipt,
   repository/commit/tree/source hashes, file count/bytes and source_cache usage.
   The receipt is available after RAM-log expiry/restart to the same principal.
4. Prepare firmware_build with project_id and source_receipt. A succeeded owned
   fetch is mandatory. Execute the exact build plan without operator approval or
   USB lease. Read_build_logs
   returns the existing hashed manifest plus source_provenance, linking the exact
   receipt, repository, commit, Git tree and SHA-256 source manifest.
5. Prepare/execute firmware_flash from that successful build job and
   existing registered device/recovery receipt. Readback/expected boot verification
   and LKG behavior are unchanged. Logs/capture use their existing permissions.

Every snapshot file is rehashed during build preparation, again before invoking
build and after it completes. Extra/missing/changed files fail; source changes
require a new fetch/plan. A receipt cannot be borrowed from another principal.

## Source format, limits and target compatibility

Public HTTPS only, fixed api.github.com/codeload.github.com URLs constructed by
the broker. Redirects are rejected. No caller URL, shell, Git hooks, credentials,
submodule update or LFS download. The archive's root must name the exact commit.
Fixed allowlisted Git submodules are recursively acquired as described in
SOURCE-SUBMODULES.md. Git blob/tree SHA-1 is reconstructed from extracted bytes/modes and matched to
that commit's GitHub tree metadata; SHA-256 receipts additionally protect retained
bytes. This uses trusted HTTPS GitHub commit metadata, not a GPG signature claim.

Bounds: 64 MiB total downloads, 1 MiB per commit metadata, 256 MiB expanded files, 100,000
files/200,000 entries, 180 seconds, 10-second network-read timeout, 16 snapshots
and 2 GiB cache. Traversal, links/reparse paths, encrypted ZIP entries, Windows
reserved names/ADS/case aliases, collisions, and long Windows paths are rejected.
Cancellation, principal expiry/revocation and registration changes are checked
around network/extraction/publication. The whole-job 180-second deadline is
independent of a transport connection or device lease; failed fetches grant no
build permission. Confirmed normal failure/cancellation ends the job without a
recovery-review latch. A corrected new job can run; the old job never replays.
Explicit fetches first reuse a fully verified, durably successful owned receipt.
When capacity is needed they reclaim only proven unreferenced source payloads;
immutable receipts and protected source/recovery data remain retained. No limit
is raised, and there is no unbounded archive or forced removal of referenced data.

Historical pre-submodule target checked: cardputer-adv-pocketjs commit
6a139780b7eeececfaa17cf2ddee576a10a027f5 (1,524 files, about 28 MB). No gitlinks,
.gitmodules or LFS pointers are required. QuickJS-NG/PocketJS and other components
are tracked for QuickJS-NG/PocketJS; BMI270 and libopus remain external .cache
requirements (see the firmware clean-checkout inventory). Node 16+ is required and must already be registered. The tracked
dependencies.lock contains old .cache references: receipt builds explicitly use
IDF_COMPONENT_MANAGER=0 (no registry fetch) and SDKCONFIG in the build output,
using tracked sdkconfig.defaults. No source rewrite or SDK 6.1 fallback is used.

## Acceptance and Windows deployment verification

Cloud CI checks synthetic archives, receipt ownership/tampering/limits, actual
HTTP/MCP unattended registered-profile execution, source-to-build manifest provenance with fake processes,
and a real pinned public target archive/tree + Windows build preflight. SDK
compilation/device behavior must not be claimed from these fake/preflight checks.

Use the verified Windows ZIP/exe from the PR's exact head/run. Start that MCP
server with the existing config/authentication and external SDK registration.
Keep local Codex stopped. Through the actual Secure MCP Tunnel, compare tools/list
names/input schemas/annotations to the parent cloud client's callable definitions.
The new prepare_test enum firmware_fetch_source and source_commit/source_receipt
inputs must exist on both sides; read_build_logs must expose receipt/manifest.
Run steps 1–4 from the cloud and check commit/source hashes in the real build
manifest before separately authorizing any physical flash.

Official custom connection refresh: server deploy/restart → open the connection
in ChatGPT Plugins → Refresh → verify metadata changed → new conversation/retest.
https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata
The same flow applies to custom Secure MCP Tunnel connections. A tools/list result
or listChanged notification alone does not prove host metadata changed. Host
Refresh is not callable from this task's tool set; current-conversation refresh
has not been established. No permissions/authentication/re-registration workaround.

Formal fixed Git submodules are supported with a registered repository allowlist.
See [SOURCE-SUBMODULES.md](SOURCE-SUBMODULES.md) for the Gitlink contract,
shared bounds, receipt compatibility and remaining firmware dependencies.


## Bounded source lifecycle and resource inventory

The cache keeps at most 16 resident source payloads within the existing 2 GiB
budget. `firmware_fetch_source` first verifies and reuses a successful owned
receipt for the exact project, principal, registration and full commit, even
when all slots are occupied. A new fetch job records that original receipt; its
original successful job remains the authorization evidence. Failed or uncertain
publication is never reusable. Duplicate fetch jobs do not duplicate payloads.

If new bytes need space, the explicit fetch reclaims the oldest eligible payloads
under an OS cache lock. Current executable source-build plans, unfinished jobs,
executable/unfinished firmware operations and every current/history LKG source
association protect their payloads. New source-build plans have a visible
30-minute lease bound to the broker epoch and monotonic clock; wall-clock
changes cannot expire or revive it. `source_plan_expires_at` is display-only.
Expired unexecuted plans cannot execute and must be prepared again. Preparing unchanged inputs during a live lease does not renew it. Once a
job is queued its protection lasts through completion, regardless of lease expiry.
Legacy unexecuted plans remain protected for their current broker epoch. Old
broker epochs cannot execute. Completed build history alone is provenance, not
a permanent source reservation. Recovery objects and build artifacts are never
removed by this mechanism.

Reclamation removes only the `source` payload, retaining the exact original
`receipt.json`, full file manifest, hashes and durable job evidence. Receipt-only
entries consume bytes but no resident payload slot. Evidence is independently
bounded at 4096 receipts / 128 MiB, still within the same total 2 GiB budget;
admission reserves the existing maximum 32 MiB receipt headroom. Evidence capacity
exhaustion fails with `source_cache_evidence_full`, never unlimited archival.
An explicit fetch of a retired commit restores its original receipt only after
all downloaded source bytes/provenance match that immutable receipt. Build prepare
reports `source_snapshot_evicted_fetch_required`; it never implicitly downloads,
builds, flashes or replays work.

A private durable reclamation marker makes interrupted removals resumable. The
remaining files must still match the original manifest and current references
must permit reclamation. Failed removal marks the current broker uncertain.
Restart can resume validated unreferenced remnants; unknown files, symlinks,
reparse points and hardlinks are rejected. Recognized reservations left by
terminal owned fetch jobs are reconciled under the same lock; legacy/unknown
pending directories and unfinished/review jobs are retained. Failed/cancelled
publications can be reclaimed only with matching durable publication intent and
full payload verification; they never grant build authority. Publication intent
is committed on the resident SQLite loop before the filesystem rename. Intent
alone never grants success or build authorization. Legacy uncertain publications
without this binding remain unknown and are preserved. The ledger is limited by
the existing 10,000-job history bound, with at most one receipt per fetch job.

The root OS lock is released by process death and rejects concurrent threads or
processes without waiting. Its owner digest binds the cache to one state-store
location; another state store gets `source_cache_owner_conflict`. Read-only
inventory never creates a cache or lock. First explicit fetch claims a legacy
root. SQLite reference reads run on the resident event loop, not the file worker.
Disk reserve, finite evidence headroom and complete reference inventory are
checked before reclamation. Source download failure can leave an old payload
retired, but never removes its receipt or recovery evidence.

`read_source_resources` performs one fresh bounded traversal and returns owned
resident/receipt-only entries, counts/bytes, executable/unfinished/recovery
protection reasons and eligibility. Other principals and unknown names stay
private and consume capacity. This read never reclaims or grants cleanup.
Truncated, corrupt or unresolved reference evidence disables reclamation with
`source_cache_references_incomplete`. If no eligible payload can free enough space,
`source_cache_protected` explains the protected/unknown/unowned capacity blocker;
there is no forced cleanup or automatic retry. The read tool preserves its name
and input schema; its retention description/output now reflects this policy.

This implementation and synthetic tests do not clean existing installations.
Deployment/restart and any real-data validation are separate operations. GUI
placement, private configuration, ESP-IDF 6.0.1 and device operations are unchanged.

The cooperative scan time budget is 30 seconds; the unchanged 1,000,000 entry
limit includes empty directories. `source_cache_scan_limit` now reports whether elapsed time or
entry count exhausted the scan, with bounded counters and `complete=false`.
Incomplete/previous totals are not returned as current capacity. Having 13 of 16
slots occupied does not imply that the filesystem can be scanned within a fixed
time budget. See FIRMWARE-BUILD.md for the finite 30-second allowance and its
measurement boundary.
Every file's current no-follow metadata and each directory's ancestors are still
checked. String-based ancestor traversal avoids rebuilding Path tuples without
omitting any check; it does not make path checks and directory enumeration atomic
against a hostile external process swapping paths between syscalls.

`read_build_logs` retains its compatible default capacity request. Frequent log
pollers should set `include_source_cache=false` (the GUI does this for selected
job logs); use `read_source_resources` when
fresh capacity/reference information is needed. A capacity-scan failure is
reported as unavailable alongside the source job/log result and does not turn a
successful source job into a failed log request. Prepare/fetch capacity gates
still require a successful fresh scan. Cached/partial capacity is never substituted.

The new tool/input schema needs the normal connection Refresh and client metadata
verification after a separately authorized deployment; this development change
does not refresh or deploy the running connection.


### Execute-time fresh metadata cost

Execute performs fresh capacity traversals before admission and after reclamation under the cache guard;
it does not reuse prepare's counts. This check remains mandatory even when a
previous prepare succeeded and no receipt was published by a failed execution.
On Windows, ancestor link/reparse checks query GetFileAttributesW instead of
requesting full stat structures. This API reports the link's own attributes;
every ancestor is still queried on every check and every reparse point is rejected.
Only the native function binding is cached, never file metadata. Unsupported or
failed attribute queries fall back to the original no-follow lstat, preserving
missing/denied/UNC root semantics. Per-entry no-follow metadata, complete traversal,
current byte/entry counts, cancellation, deadline and final budget checks remain.

[Microsoft's attribute-query contract](https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getfileattributesw)
documents link and mount-point behavior. This optimization needs native Windows
measurement against the retained cache; CI/synthetic tests do not prove that any
particular cache always completes within thirty seconds under OS load. A timeout
still fails closed and publishes no new receipt. No automatic retry is introduced.
