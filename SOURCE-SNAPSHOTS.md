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
recovery-review latch. A corrected new job can run; the old job never replays. Retained sources are not automatically
removed. Capacity exhaustion rejects another fetch and reports current usage;
there is no hidden unbounded cache or forced deletion of referenced sources.

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


## Current resource inventory and explicit cleanup review

`read_source_resources` accepts only a registered `project_id` and requires the
current read principal. It performs one fresh bounded traversal for totals and
per-snapshot byte/entry counts. Totals expose remaining slots/bytes; timestamps
come from that principal's durable source jobs, not guessed filesystem creation
or expiry times. Retained snapshots have no automatic lifetime or deletion.
Receipts belonging to other principals and unknown filesystem names stay private;
only their aggregate count is returned. Unknown entries still consume capacity.

The read-only report marks build-plan/artifact evidence, current LKG evidence,
and recovery history separately. Restoring a retained bundle preserves its
original source/build association. The bounded DB reference inventory explicitly
reports truncation; missing/unknown references are never proof of disposability.
No returned snapshot is cleanup-authorized. Initial full backups, LKG/recovery
objects, referenced build outputs and source evidence must be retained. An
operator considering cleanup must stop relevant writers, reconcile all owned and
unknown references offline, preserve recovery evidence, and obtain a separate
explicit cleanup request. This tool implements no delete command or automatic GC.

The same unchanged scan limits apply: 5 seconds and 1,000,000 entries, including
empty directories. `source_cache_scan_limit` now reports whether elapsed time or
entry count exhausted the scan, with bounded counters and `complete=false`.
Incomplete/previous totals are not returned as current capacity. Having 13 of 16
slots occupied does not imply that the filesystem can be scanned within 5 seconds.
Every file's current no-follow metadata and each directory's ancestors are still
checked. String-based ancestor traversal avoids rebuilding Path tuples without
omitting any check; it does not make path checks and directory enumeration atomic
against a hostile external process swapping paths between syscalls.

`read_build_logs` retains its compatible default capacity request. Frequent log
pollers should set `include_source_cache=false`; use `read_source_resources` when
fresh capacity/reference information is needed. A capacity-scan failure is
reported as unavailable alongside the source job/log result and does not turn a
successful source job into a failed log request. Prepare/fetch capacity gates
still require a successful fresh scan. Cached/partial capacity is never substituted.

The new tool/input schema needs the normal connection Refresh and client metadata
verification after a separately authorized deployment; this development change
does not refresh or deploy the running connection.
