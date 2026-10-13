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

1. Acquire/renew the existing authenticated lease. Prepare test
   firmware_fetch_source with project_id and source_commit (full 40 hex).
   Prepare does not download. Storage shows cache counts/bytes and fixed limits.
2. Use the existing exact-plan local approval and execute_test contract. Source
   fetch needs no new device attestation phrase. No second source-specific
   approval mechanism is introduced. Existing build/flash approvals remain separate.
3. Read_build_logs for the succeeded fetch job returns source.source_receipt,
   repository/commit/tree/source hashes, file count/bytes and source_cache usage.
   The receipt is available after RAM-log expiry/restart to the same principal.
4. Prepare firmware_build with project_id and source_receipt. A succeeded owned
   fetch is mandatory. Review/execute the existing build plan. Read_build_logs
   returns the existing hashed manifest plus source_provenance, linking the exact
   receipt, repository, commit, Git tree and SHA-256 source manifest.
5. Prepare/approve/execute firmware_flash from that successful build job and
   existing registered device/recovery receipt. Readback/expected boot verification
   and LKG behavior are unchanged. Logs/capture use their existing permissions.

Every snapshot file is rehashed during build preparation, again before invoking
build and after it completes. Extra/missing/changed files fail; source changes
require a new fetch/plan. A receipt cannot be borrowed from another principal.

## Source format, limits and target compatibility

Public HTTPS only, fixed api.github.com/codeload.github.com URLs constructed by
the broker. Redirects are rejected. No caller URL, shell, Git hooks, credentials,
submodule update or LFS download. The archive's root must name the exact commit.
Git blob/tree SHA-1 is reconstructed from extracted bytes/modes and matched to
that commit's GitHub tree metadata; SHA-256 receipts additionally protect retained
bytes. This uses trusted HTTPS GitHub commit metadata, not a GPG signature claim.

Bounds: 64 MiB archive, 1 MiB commit metadata, 256 MiB expanded files, 100,000
files/200,000 entries, 180 seconds, 10-second network-read timeout, 8 snapshots
and 2 GiB cache. Traversal, links/reparse paths, encrypted ZIP entries, Windows
reserved names/ADS/case aliases, collisions, and long Windows paths are rejected.
Cancellation/lease/principal loss is checked around network/extraction/publication;
failed fetches grant no build permission. Retained sources are not automatically
removed. Capacity exhaustion rejects another fetch and reports current usage;
there is no hidden unbounded cache or forced deletion of referenced sources.

Target checked: cardputer-adv-pocketjs commit
6a139780b7eeececfaa17cf2ddee576a10a027f5 (1,524 files, about 28 MB). No gitlinks,
.gitmodules or LFS pointers are required. QuickJS-NG/PocketJS and other components
are vendored. Node 16+ is required and must already be registered. The tracked
dependencies.lock contains old .cache references: receipt builds explicitly use
IDF_COMPONENT_MANAGER=0 (no registry fetch) and SDKCONFIG in the build output,
using tracked sdkconfig.defaults. No source rewrite or SDK 6.1 fallback is used.

## Acceptance and Windows deployment verification

Cloud CI checks synthetic archives, receipt ownership/tampering/limits, actual
HTTP/MCP local approval, source-to-build manifest provenance with fake processes,
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
