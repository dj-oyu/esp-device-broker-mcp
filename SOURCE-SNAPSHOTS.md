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
  "cache_root": "C:\\brksrc"
}
```

On Windows the operator must keep the cache folder private to the account (ACLs are
not inspected; the former `private_acl_confirmed` declaration was removed, and a legacy
`true` is ignored while `false` stops startup). The application does not change authentication, connection
permissions, Git credentials or user worktrees. Choose a short root outside the
project, broker state, and build output roots. Paths/origin are registered locally;
MCP accepts only the project ID, full lowercase 40-hex commit, or an opaque receipt.

### Pinned root boundary

The registered `cache_root` may itself be (or sit behind) a symlink or junction that
the operator deliberately created. Each operation resolves the root once to its
concrete directory (the pinned root) and uses only that resolved path. The boundary is
the pinned root: it must not be a filesystem root and must not overlap the project,
state or build roots (compared on resolved paths). Everything below it keeps the strict
no-follow rules (links, reparse points, hardlinked payload files, unknown entries), and
reclamation or cleanup never follows a link or leaves the pinned root. The ancestors of
the resolved path are rechecked, and the pinned root is re-resolved on each access: if the
link is retargeted while an operation holds the cache guard it fails closed with
`source_cache_root_retargeted`. Two paths that resolve to the same directory share one
process guard and one owner lock. Same-account processes remain trusted (see KEYS.md);
this is containment under the registered root, not protection against a same-account
process that can already rewrite the cache or the broker state.

## Explicit cloud MCP requests

1. With a current authenticated control principal, submit kind=fetch
   with project_id, a fresh job_id and source_commit (full 40 hex). Omit session:
   source/build jobs require neither a USB lease nor individual operator approval.
   Prepare does not download. Storage shows cache counts/bytes and fixed limits.
2. Execute that exact plan_hash. The locally registered profile defines the
   allowed project/repositories/cache/output roots and tools. No separate grant
   registration or first-run approval is required. MCP cannot expand that scope.
3. Read_build_logs for the succeeded fetch job returns source.source_receipt,
   repository/commit/tree/source hashes, file count/bytes and source_cache usage.
   The receipt is available after RAM-log expiry/restart to the same principal.
4. Submit kind=build with project_id and source_receipt. A succeeded owned
   fetch is mandatory. Execute the exact build plan without operator approval or
   USB lease. Read_build_logs
   returns the existing hashed manifest plus source_provenance, linking the exact
   receipt, repository, commit, Git tree and SHA-256 source manifest.
5. Submit kind=flash and queue it with the `flash` tool, from that successful build job and
   existing registered device/recovery receipt. Readback/expected boot verification
   and LKG behavior are unchanged. Logs/capture use their existing permissions.

Every snapshot file is rehashed during build preparation, again before invoking
build and after it completes. Extra/missing/changed files fail; source changes
require a new fetch/plan. A receipt cannot be borrowed from another principal.

## GitHub rate limit and API token

Unauthenticated GitHub API requests are limited to 60 per hour per IP address. A fetch
spends several (root commit and each fixed submodule commit, plus tree/blob requests when
a snapshot is rebuilt from `.gitmodules`), and failed attempts count too, so a few fetches
in an hour can exhaust the quota.

- When GitHub answers 403/429 with an exhausted quota, the job fails with
  `source_rate_limited` (not `source_download_failed`). The job log gets one line before
  the usual error line: `error: github rate limit: authenticated=<yes|no> limit=<n>
  remaining=<n> resets_in_seconds=<n>`. Only those bounded numbers are logged, never
  headers or response bodies. Wait `resets_in_seconds` before retrying; retries meanwhile
  only spend quota.
- Set `CARDPUTER_GITHUB_TOKEN` in the environment of the broker process to authenticate
  (5,000 requests per hour). No token scope is needed for public repositories; use a
  fine-grained token with no repository permissions. The token is read from the
  environment on each request; it is not a registration/profile field, is not part of any
  receipt or digest, is sent only as `Authorization: Bearer` to `api.github.com` and
  `codeload.github.com` over https (redirects stay rejected), and is not in the build
  or Tunnel child environments (those are allowlists). A GUI/helper started by
  `Start-CardputerStack.ps1` clears `CARDPUTER_*`, so only the broker sees it. An already
  open shell keeps its old environment: set the variable, then start the broker from a
  new shell.
- A blank value means anonymous access. A set but malformed value fails closed with
  `source_github_token_invalid`; a token GitHub rejects (401) fails with
  `source_github_token_rejected`. Neither value is ever logged.

## Source format, limits and target compatibility

Public HTTPS only, fixed api.github.com/codeload.github.com URLs constructed by
the broker. Redirects are rejected. No caller URL, shell, Git hooks, registered or
caller-supplied credentials, submodule update or LFS download. The only credential is the
optional process-environment token described in "GitHub rate limit and API token" below.
The archive's root must name the exact commit.
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

### Self-healing of changed unreferenced payloads

A payload can stop matching its receipt without any tampering: a tool run from inside the
cache can add a file (for example Python writing `__pycache__/*.pyc`), or antivirus/sync
software can touch one. Build preparation still fails for such a payload (`Extra/missing/
changed files fail`), and a referenced payload is never deleted. For a payload that
nothing references, a fetch no longer stops at it:

- Eviction (cache full): a candidate whose regular files differ from the receipt (extra,
  missing or modified) is reclaimed like an intact one. The fetch stage is
  `source_reclaim_corrupt_payload` instead of `source_reclaim_unreferenced_payload`, so
  the event stays visible in the job/operator timeline.
- Same commit: fetching the commit of a changed, unreferenced payload removes that payload
  and re-downloads it as a restore, so the new bytes are verified against the original
  receipt; the receipt itself is never rewritten.
- Only the payload goes. The receipt, durable job evidence and ownership remain, and a
  later fetch of the same commit restores verified bytes.
- Still fail closed, with no deletion: links or hard links, non-regular entries, unknown
  names in the receipt directory, an unreadable or invalid receipt, incomplete reference
  evidence, and anything a plan, unfinished job, recovery or LKG association protects
  (`source_cache_protected`, `source_snapshot_changed`).

This changes the earlier rule that a tampered payload is never deleted: it now holds only
for referenced payloads and for entries that are not plain files. There is no retry loop and
no device, build or flash action; only the cache the fetch already manages is touched.

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
The new submit kind fetch and source_commit/source_receipt
inputs must exist on both sides; status(view=logs, stream=build) must expose receipt/manifest.
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


## Registration binding is content, not location

Plans and receipts are bound to a digest of the approved registration facts that
decide what executes or which provenance is trusted: the GitHub repository, the
submodule allowlist, tool identities (path and SHA-256), profiles, allow flags and
device selectors. Location-only fields are **not** bound, so moving a folder does not
invalidate durable evidence: `cache_root`, `build_root`, and
the flash `backup_directory`. Tool or profile changes,
a different repository, a different submodule allowlist, or a different esptool
(path or hash) still invalidate bound plans and receipts, and unknown digests fail closed.

New receipts and plans carry the path-independent digest. Receipts written by earlier
releases carry a digest that included `cache_root` and `private_acl_confirmed` (and, for flash
bindings, the former `allow_*` flags and `unattended_flash.enabled`); they stay valid in
place: the accepted set also holds the digest recomputed for every legacy value of the
removed booleans. After moving a cache, list the old location(s) in the registration so
those receipts are recognised again (nothing else is trusted; at most 8 absolute paths):

```toml
[build_projects.<id>.source_repository]
cache_root = 'D:
ew\esp-src'
previous_cache_roots = ['C:\old\esp-src']
```

`previous_cache_roots` is operator metadata only: it never changes a digest, is not
accepted from MCP, and does not move or copy any files. Historical flash plans keep
verifying through their original full-model digest while the flash registration is
unchanged; moving the flash `backup_directory` no longer affects new flash plans.

## Bounded source lifecycle and resource inventory

The cache keeps at most 16 resident source payloads within the existing 2 GiB
budget. `firmware_fetch_source` first verifies and reuses a successful owned
receipt for the exact project, principal, registration and full commit, even
when all slots are occupied. A new fetch job records that original receipt; its
original successful job remains the authorization evidence. Failed or uncertain
publication is never reusable. Duplicate fetch jobs do not duplicate payloads.

The 16-payload / 2 GiB budget is shared by every principal of a project. If new
bytes need space, the explicit fetch reclaims the oldest eligible payloads, whichever
principal fetched them, under an OS cache lock. Only the `source` payload of another
principal's receipt is reclaimed: its immutable receipt, durable job evidence and
ownership are unchanged, and reuse/build authority still belongs to the receipt's
own principal (a receipt cannot be borrowed). Issuing a new key therefore no longer
strands the capacity used by earlier keys. Current executable source-build plans, unfinished jobs,
executable/unfinished firmware operations and the current LKG source association
protect their payloads. Historical LKG associations retain provenance, receipts
and firmware binaries without permanently reserving their old source payloads.
Their original flash/build lineage is still validated; it also resolves the
current LKG's source when a later operation selected an existing binary.
New source-build plans have a visible
30-minute lease bound to the broker epoch and monotonic clock; wall-clock
changes cannot expire or revive it. `source_plan_expires_at` is display-only.
Expired unexecuted plans cannot execute and must be prepared again. Preparing unchanged inputs during a live lease does not renew it. Once a
job is queued its protection lasts through completion, regardless of lease expiry.
Device-bound unexecuted plans cease reserving payloads when their exact lease ID
has a completed `released` or `expired` device-request transition. Every grant
gets a fresh lease ID, and execution requires the plan's original ID; renewal
cannot revive an ended lease. This also applies to legacy device-bound plans.
An absent lease, a passed deadline, a missing job, or a request in review is not
proof of permanent non-executability. Missing or ambiguous lifecycle evidence
keeps protection; queued/running/review jobs stay protected even after their lease
ends. No plan retirement API or cross-principal execution authority is added.
Other legacy unexecuted plans remain protected for their current broker epoch. Old
broker epochs cannot execute. Completed build history alone is provenance, not
a permanent source reservation. Recovery objects and build artifacts are never
removed by this mechanism.

Publishing a flash plan for a source-backed build takes the source cache's
non-waiting guard through the durable plan commit. A concurrent fetch/reclamation
returns `source_fetch_busy` to preparation; it never waits on the resident loop.
If preparation commits first, retention sees the executable plan. If reclamation
owns the guard first, preparation cannot publish a reference after the final
retention read and before payload removal. Retained-artifact preparation remains
available after reclamation; execution still requires fresh source verification.

Reclamation removes only the `source` payload, retaining the exact original
`receipt.json`, full file manifest, hashes and durable job evidence. Receipt-only
entries consume bytes but no resident payload slot. Evidence is independently
bounded at 4096 receipts / 128 MiB, still within the same total 2 GiB budget;
admission reserves the existing maximum 32 MiB receipt headroom. When an explicit
fetch finds the evidence budget full, it first rotates it: the oldest receipt-only
entries (payload already reclaimed) that no unfinished job, executable plan or
current LKG protects are removed, oldest fetch first, until one new receipt fits.
The selection is made in full before anything is removed, and only with complete
reference evidence. Durable job records, recovery objects and build artifacts are
untouched. A later build against a rotated receipt reports `source_snapshot_unavailable`
and needs an explicit fetch. If the unprotected entries cannot make room, the fetch
fails with `source_cache_evidence_full` and removes nothing; archival is never unlimited.
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
the existing 10,000-reference scan bound, with at most one receipt per fetch job.
That bound limits complete reference inspection, not history storage or retention;
exceeding it disables reclamation rather than deleting history.

The root OS lock is released by process death and rejects concurrent threads or
processes without waiting. Its owner digest binds the cache to one state-store
location; another state store gets `source_cache_owner_conflict`. Read-only
inventory never creates a cache or lock. First explicit fetch claims a legacy
root. SQLite reference reads run on the resident event loop, not the file worker.
Disk reserve, finite evidence headroom and complete reference inventory are
checked before reclamation. Source download failure can leave an old payload
retired, but never removes its receipt or recovery evidence.

`status(view=source)` performs one fresh bounded traversal and returns owned
resident/receipt-only entries, counts/bytes and eligibility. The additive
`payload_protection_reasons` field lists reclamation blockers;
`provenance_reasons` separately reports immutable receipts and historical LKG
associations. The existing `protection_reasons` field retains its receipt-evidence
reason and current blockers. History-only provenance is not a payload blocker.
Other principals' entries are never listed (no names, jobs or receipts); they are
counted in aggregate in `other_or_unclassified_snapshots`, in
`reclamation_candidate_payloads` and in `protection_reason_counts` (capacity scope
`project_shared_all_principals`). Unknown names stay private and consume capacity.
This read never reclaims or grants cleanup.
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

`status(view=logs, stream=build)` retains its compatible default capacity request. Frequent log
pollers should set `include_source_cache=false` (the GUI does this for selected
job logs); use `status(view=source)` when
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
On Windows, link/reparse checks below the pinned root (and on the ancestors of its
resolved path) query GetFileAttributesW instead of requesting full stat structures. This
API reports the link's own attributes; every such component is still queried on every
check and every reparse point is rejected.
Only the native function binding is cached, never file metadata. Unsupported or
failed attribute queries fall back to the original no-follow lstat, preserving
missing/denied/UNC root semantics. Per-entry no-follow metadata, complete traversal,
current byte/entry counts, cancellation, deadline and final budget checks remain.

[Microsoft's attribute-query contract](https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getfileattributesw)
documents link and mount-point behavior. This optimization needs native Windows
measurement against the retained cache; CI/synthetic tests do not prove that any
particular cache always completes within thirty seconds under OS load. A timeout
still fails closed and publishes no new receipt. No automatic retry is introduced.
