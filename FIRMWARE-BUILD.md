# External ESP-IDF build and shared logs

This opt-in feature runs project build code as the trusted local OS user. Register
projects and trusted export profiles in the private broker TOML. MCP accepts only
an existing project ID, never shell commands, profile paths, log paths or argv.
Cardputer is pinned to external ESP-IDF 6.0.1; a mismatch fails with no fallback.
No flash, reset, USB open, install, account creation or credential creation occurs.

Example paths are placeholders, not the user's private configuration:

```toml
[build_projects.cardputer]
project_dir = 'C:\example\firmware'
trusted_profile = 'C:\example\trusted-idf-export.ps1'
expected_idf = '6.0.1'
# Optional, only after explicit local authorization; default is inherit:
# profile_execution_policy = 'RemoteSigned'
target = 'esp32s3'
# Optional dedicated short output root, registered locally:
# build_root = 'C:\example\build-output'
# build_min_free_bytes = 536870912
```

The registered profile must support `-e`: output literal KEY=VALUE lines and
return. It is invoked with the system PowerShell `-NoProfile -File profile -e`,
never dot-sourced. Malformed or duplicate assignments, stderr, excessive output
or a nonzero exit fail closed. Execution policy is inherited by default. Local registration may explicitly select
`profile_execution_policy = "RemoteSigned"`; this adds only the child process
`-ExecutionPolicy RemoteSigned` flag. Group Policy remains authoritative. Bypass,
Unrestricted, scope writes and shell fallbacks are not supported. MCP cannot set
this policy. Its choice is disclosed in the plan/GUI and bound into the fingerprint.
Profile refusal persists `profile_export_rejected_or_failed`; compiler preflight
nonzero exit persists `external_toolchain_diagnostic_failed` and never starts build.
Neither fixed diagnostic contains native stderr or private paths.
Only an explicit environment allowlist reaches the
child. Ambient credentials and arbitrary exported variables are excluded.

Call `submit` with `kind="build"`,
`project_id="cardputer"`, a fresh `job_id` and optionally an owned source_receipt.
A current authenticated control principal is required. Omit session: build and
source fetch do not acquire a USB lease. The existing local project/profile
registration authorizes execution within those registered inputs; no additional
grant, approval screen or per-job APPROVE is required. Execute the exact returned
plan_hash with `execute(op=run)`. There is no approval argument for these registered-profile jobs.
No remote API can register projects, change paths/tools or expand repositories.

Registering a build project trusts its scripts to run as the broker's local OS
user, including code from selected commits in its registered repository. Public
source acquisition only parses and stores bounded data; compiling it executes
project code. Neither an environment allowlist nor Windows Job Object is a
filesystem/network sandbox. Registered output roots constrain broker-managed
output; they cannot constrain every write/read made by trusted build scripts.
The existing free-space checks, artifact and log bounds are not a hard disk quota
for arbitrary scripts. Do not register untrusted code for this execution mode.

Exact plans, source/profile/tool fingerprints, current principal, receipt ownership
and integrity are still checked. The registration digest covers what executes (project
directory, profile, tool paths/hashes, repositories, allow flags) but not output
locations such as `build_root` or the source `cache_root` (see SOURCE-SNAPSHOTS.md). Repeated execute calls return the durable job
rather than rebuilding. A connection disconnect does not cancel or replay work.
The whole build job has an 1800-second execution deadline independent of transport
or physical lease lifetime. Principal revocation/expiry, registration change,
explicit cancellation or shutdown requests stop; actual process-tree exit and
output-lock release are confirmed before allowing another job. Final manifest
publication is performed on the broker loop after fresh authorization/configuration
checks, without yielding before the durable success commit.

Known preflight/compiler/source failures with confirmed cleanup are terminal
failed jobs. Confirmed cancellation is terminal cancelled. They do not latch global
review, so a corrected new job can run without restart or operator intervention.
Uncertain process exit, output-lock release, durable state or final publication
still fails closed as needs_review. Restart never replays jobs: new queued profile
jobs are cancelled; interrupted running work requires review because completion
and ownership are not established. Old approvals/needs_review records are neither
deleted nor automatically acknowledged.

The fixed build argument vector is external Python, external `tools/idf.py`,
explicit `--ccache`/`--no-ccache`, `-B`,
the broker-generated directory, `build`. Canonical registered project identity plus source/profile hashes and external
Python/idf.py/compiler/environment fingerprints partition build directories. The
expected target is fixed through IDF_TARGET and checked against the generated
project description. Diagnostics require the actual `idf.py --version` to report
exactly ESP-IDF v6.0.1 and require a registered-environment ESP32-S3 compiler.

`status(view=logs, stream=build)` requires current read authorization and the original job
principal. Default output is progress plus warnings/errors, with two lines of
context. Explicit levels: error, warning, info, debug, unknown. Stderr is a stream,
not an error severity. Notes and multiline text remain available through context
or unknown-level reads. Each reader owns its epoch/sequence cursor; reads do not
consume entries. Retention is at most eight jobs, 2,048 lines / 256 KiB per job and
five minutes since the last appended line. Oversized process lines are omitted after 4,096 bytes rather than split into
potential credential fragments.
Cursor gaps, restart epoch mismatch and expiry are explicit. Raw output is never
written; no caller-specified path can be opened. Each redacted line is also
appended to the job's durable log, `<state_dir>/job-logs/<job_id>.jsonl` (same
format and bounds as described in DEVICE-LOGS.md, with `stream` `build`,
`source` or `flash`). When the RAM output has expired or the broker restarted,
`status(view=logs, stream=build)` serves the owner's durable entries with the
same level/context/limit filtering, `"source": "durable_job_log"`, `expired=false`
and a separate cursor epoch; a RAM cursor then reports `gap=true` and restarts
from the beginning. Known credential patterns and known
ambient secret values are removed; arbitrary application secrets cannot be
universally recognized, so the explicit execution request includes log delivery.

Cancellation of a running build requests termination and returns before shutdown
is confirmed; poll `status(view=job)`. Confirmed termination results in cancelled, never
success/replay; unconfirmed termination remains needs_review.
Windows assigns the suspended child atomically to a kill-on-close owned Job Object
and inherits only NUL plus the two output pipes. Leader exit does not establish
subtree exit. Failed teardown retains the owner and blocks broker lock release.
No process-name/PID search or termination of unrelated processes is used.

A successful build writes broker-manifest.json under its private build directory
and returns its public manifest with the logs. It includes schema version, exact
IDF version, target, input/environment fingerprints, artifact relative names,
lengths and SHA-256. `flash_authorized=false` is explicit. A later flash feature
must reverify artifacts and obtain independent immutable-plan execution;
this feature provides no flash operation or permission. Old interrupted build
jobs are not marked recovered or successful.

## Cross-generation compiler cache

Build output slots remain partitioned by the complete source/receipt/profile and
resolved tool/environment fingerprints. A compiler cache can share unchanged
compilation results between those slots; it never substitutes a source receipt,
build workspace, generated configuration, artifact hash or diagnostic proof.
Existing source validation, generated target/security checks, artifact publication,
owner checks, cancellation and process cleanup still run on warm builds.

The existing trusted IDF export profile may emit `IDF_CCACHE_ENABLE=1`. The broker
then resolves `ccache.exe` from that profile's PATH and uses IDF's `--ccache` switch.
No new broker setting, grant, source registration or dependency installation is
introduced. ESP-IDF 6.0.1 lists ccache 4.12.1 for Windows; this contract supports
ccache 4.8 through 4.x. A missing/disabled switch, missing executable or unsupported
version selects explicit `--no-ccache` with a fixed reason. An invalid switch,
failed executable diagnostic or executable mutation fails the build preflight.
A compilation failure remains a failed build: there is no automatic uncached
retry. The broker does not install ccache or change the local IDF environment.

An enabled build records the cache executable's version/content/path digests,
configuration digest and normalization contract in its environment identity.
The cache root is `state_dir/compiler-cache/<project-and-owner-digest>`.
Namespaces additionally separate the exact resolved environment and detail/DMA
modes, while excluding generation-specific source hashes from that namespace.
Ccache itself keys each compilation by compiler content, arguments and source/
included-header inputs. Different sources remain different compilation inputs.
The root's native ccache cleanup target is **2 GiB**, shared across its namespaces;
this is approximate cache cleanup, not an instantaneous hard filesystem quota or
a global broker storage limit. Source payload and build retention are unchanged.

Only the current build directory is normalized with `CCACHE_BASEDIR` and
`-fdebug-prefix-map` (both native and generic Windows path spellings). Debug paths
under that directory become `/broker-build`. The broker also passes these generated
flags through explicit `idf.py -D CMAKE_C_FLAGS/CMAKE_CXX_FLAGS` definitions: IDF
6.0.1 seeds its toolchain response flags before CMake considers environment flags.
Environment `CFLAGS` alone does not establish this path. Native backslashes are
escaped for the GCC response-file consumer, including inside quoted space-bearing
paths; Windows process-argument quoting is not used as the response-file encoding. `hash_dir` stays enabled; compiler
content checks stay enabled. Source absolute paths remain significant, so changed
application source locations may miss; stable IDF source locations with identical
headers/configuration can hit. This does not promise all cross-generation units
will hit or that an entire cached build equals the old uncached recipe bytewise.
The warm/cold equivalence contract compares the same explicit cache recipe.

A per-build UTF-8/LF configuration disables hard links, inode shortcuts, sloppiness
and remote storage. Ambient `CCACHE_*` options are not forwarded. Per-build config,
output isolation and namespaces protect technical cache correctness; they do not
add a separate authorization policy. The source/profile remain trusted executable
inputs under the existing local project model.

The additive `compiler_cache` manifest record reports policy and best-effort
before/after hit/miss counter deltas from the real build. Counters are cache-root
statistics and can include concurrent use; they are not performance acceptance or
artifact provenance. Missing/malformed/reset counters are marked unavailable;
owned-process cancellation/cleanup failures still propagate. No cache is reset
and no extra benchmark build is started. Manifest/export/flash-plan hashes bind
the new record normally. Retained manifests/receipts require no data migration;
new plans use the new recipe identity, so pending plans may need preparation again.
Rolling back the executable leaves old receipts and firmware bytes intact; cache
files are disposable acceleration data and are not recovery artifacts.

Host acceptance includes actual ccache/GCC cross-slot hits, changed-header misses,
mode namespace separation, dependency-file path checks and warm/forced-cold object
identity. A real CMake regression reproduces early toolchain cache initialization
and verifies that explicit definitions preserve the generated flags. Native paths
with and without spaces also pass through CMake-generated response bytes and the
real GCC response parser; exact native/generic prefix values must survive. Linux CI installs a test-only ccache fixture; it does not install an SDK.
Windows path handling and actual ESP-IDF hit counts/timing must also be checked
on the next genuinely required firmware build. Synthetic host results alone do
not establish Omnibook speedup or device behavior.

References: [ESP-IDF 6.0.1 idf.py options](https://docs.espressif.com/projects/esp-idf/en/v6.0.1/esp32s3/api-guides/tools/idf-py.html),
[ccache configuration and statistics](https://ccache.dev/manual/4.12.1.html).

## Historical validation in the delegated cloud environment

These results describe the original implementation and dependencies. Former
approval paths below are historical; see README.md for current explicit execution.

- 2026-10-12: compileall succeeds.
- Dependency-free unittest suite: 24 passed, two skipped because pyserial/filelock
  are not installed. With cloud-only lock/port dependency facades injected, all
  26 tests pass. This exercises the actual Broker/SQLite/PlanApprovals/PlanReview
  serial and mock prepare-submit-GUI-approve paths, principal mismatch, no replay
  and cancellation to durable needs_review, but does not validate native locks. Includes mock profile/diagnostics/build/manifest/read traversal,
  output flood, multiple readers, unknown/stderr/context, expiry/epoch/gap,
  input fingerprints, POSIX interruption, and synthetic Windows owned-job exit
  and uncertain shutdown. No real hardware or actual IDF build was used.
- One dependency install attempt failed; no network/proxy/access bypass or
  repeated install attempts were performed.
- Full pytest/ruff/mypy, native Windows Job Object pipe capture, actual trusted
  profile export and real external ESP-IDF build remain for Actions and local
  Windows verification. Tunnel validation follows after those checks.
- No AGENTS.md is present in the source commit tree. Distribution EXE/ZIP/Actions
  changes belong to the separate packaging task and are not included here.


## Review lifecycle corrections

Polling expired build output preserves the redaction set for later output from
that live job. Before every launch (and before resuming a suspended Windows child)
stop requests are checked. Post-build fingerprint/hash work checks cancellation;
accepted cancellation is serialized against publishing the final manifest. A
cancel after the final success commit is reported as not accepted rather than
promising review after successful work. Source files called build or build-* are
hashed normally; only repository metadata and explicitly identified broker state
output roots are excluded. Different registered checkouts cannot share a builddir.
The native GUI accepts serial build plans and displays code-execution/log-delivery
warnings for both serial and mock builds, naming the actual recipient.


## Live project and dependency preparation boundary

The plan is immutable metadata. This implementation builds the registered live
checkout; it does not make an isolated immutable source snapshot or prevent a
trusted local writer from modifying files after the final check. Source/profile/
lock fingerprints are checked before diagnostics, again immediately before build
launch, and after build. Do not edit that checkout during execution. A diagnostic-
time source change prevents launching build; a build-time change prevents success
publication and requires review. This is detection around trusted project code,
not a filesystem sandbox or a universal guarantee about every input a script reads.

The inspected Cardputer checkout 1b4524e3 has dependencies.lock references to older
.cache/components and removed UI dependencies, while current component manifests
use the vendored components/pocketjs_guest and components/quickjs-ng. CMake also
uses fetched .cache BMI270/Opus/minimp3 sources. The lock and these actual source
bytes are included in fingerprints. Initial dependency preparation/configuration
can update the lock or materialize sources and invalidate an earlier plan.

Use a separately explicitly authorized local preparation/configuration step for
that registered checkout under the same external IDF 6.0.1, inspect the resulting
lock/source changes, then prepare and explicitly execute a fresh build plan. This feature
adds no arbitrary dependency-install MCP command or automatic approval. If a
requested build itself changes dependency inputs, inspect and acknowledge its
needs_review result before creating a new job/plan; never reinterpret that old
old approval record or unsuccessful job as success. Dependency locks and .cache source
content must not be excluded from hashing merely to make a first build pass.

Actions packaging run 38295992731 succeeded for PR head 6bf848df. It checked out
synthetic merge source 60e8339ff98c8f551d43c7656f40590e0fdeef0c, whose tree equals
that head's tree (8aac63bacc4dd3eb85b28fbf54471e5f9de8828f). Extracted EXE authenticated
mock status/log smoke passed. New modules are static imports in the successful
broker startup and are also covered by collect_submodules(cardputer_mcp). Direct
new-tool catalog/schema and artifact byte inspection remain independent checks.


## Explicit external Node/Git registration

SYSTEM_PATH and ambient PATH are not merged into the child. Optional node_tool
and git_tool registrations each contain an absolute executable, SHA-256 receipt
and exact --version output. For example (placeholder path/digest/version):

```toml
[build_projects.cardputer.node_tool]
executable = 'C:\example\node\node.exe'
sha256 = '0000000000000000000000000000000000000000000000000000000000000000'
version = 'v22.0.0'
```

Git uses the same fields under git_tool, with git.exe and an exact version string
such as git version 2.51.0.windows.1. Obtain these receipts through separately
explicitly requested local diagnostics. Preparation rejects mismatched executable hashes;
execution uses absolute --version argv, then prepends only registered executable
parent directories to the profile-exported IDF PATH. It verifies resolution points
back to the registered executable. Node found on the exported PATH without explicit
registration is refused; version mismatch or ambiguous resolution prevents build.
Successful diagnostic versions are recorded in bounded logs and success manifests,
without private executable paths. Tool receipts are included in the plan fingerprint.
No arbitrary extra tools/commands/search paths are accepted from MCP.

Frozen validation run 38296963003 succeeded on PR head f2339055, actual checkout
merge source d46077e7a77a636e79fa8cdbf8c736b05fea5b0a. The extracted EXE passed
its authenticated catalog/build/log API probe without executing a firmware build.
Artifact ID 11767609431 has service-reported archive SHA-256
41ff4648d0c4ef70d073e3a2a06f2c73aca99bfd456ab0b689d767fcee74bc85.
This archive hash is not the inner EXE or distribution ZIP hash. Cloud download
of the separate diagnostics archive failed once with CONNECT 503; no retry/bypass.


## Windows architecture environment and canonical format

The explicit OS child environment preserves PROCESSOR_ARCHITECTURE and
PROCESSOR_ARCHITEW6432. Both are non-secret platform descriptors used by external
Python/ESP-IDF platform.machine() detection; ambient PATH/SYSTEM_PATH remains
excluded. A regression asserts the architecture variables reach the profile and
all diagnostic/build subprocess calls while unrelated values are absent.

The remaining Tunnel transport expected tool catalog includes status(view=logs, stream=build).
Fifteen changed src/tests files were formatted by lockfile-matched Ruff 0.16.10 in
cloud Actions run 38299610752. The formatter's post-format check passed and cloud
import verified AST equivalence for every returned file. That temporary formatter
bridge intentionally did not publish a distribution; it has been removed, along
with its temporary Ruff build dependency. The final head uses the ordinary Windows
packaging workflow and its extracted-EXE build/log API probe.


## Additive manifest-v1 flash receipts

New admitted success manifests publish flash_layout={name:'flasher_args.json',bytes,
sha256} and flash_security={name:'config/sdkconfig.json',bytes,sha256}. Exact generated
bytes are bounded (64 KiB / 1 MiB), target-checked, duplicate-key rejected and
contained under the build directory. Linked/reparse paths and layout references
without matching BIN size/hash receipts are rejected. Generated argv is never
executed; flash_authorized remains false.

The generated SDK config is not published. Only its receipt and fixed boolean
security categories are returned; arbitrary config/env/secret values are absent.
flash_security_summary.flash_admission is not_evaluated. The separate flash gate
must verify the attested snapshot and refuse forbidden policies, including secure
boot/encryption/antirollback. A secure build itself does not grant flash permission.

schema_version remains 1 and existing artifact name/bytes/sha256 receipts are
unchanged. Older v1 without the new receipts remains readable for diagnostics but
is not flash eligible; no offsets/security policies are inferred or upgraded from
old BIN files. New build_job_id/build_plan_hash fields bind the manifest to the
requested succeeded job and immutable plan. Private callers must establish that
job/principal authority before offline flash admission.

The producer was tested against flash contract a385bfd: its layout and generated-
config receipts were accepted, while legacy v1 without the layout receipt was
refused. No device, flash, eFuse or boot operation was performed. The optional
short output root, slot ownership and storage diagnostics are described below.


### IDF 6.0.1 dependency-aware security interpretation

The shared pure build_security.interpret_idf601_security function is intended for
both producer summaries and offline flash admission. It is pinned to the actual
6.0.1 bootloader Kconfig definitions. SECURE_BOOT and SECURE_FLASH_ENC_ENABLED must
be explicit booleans. Missing BOOTLOADER_APP_ANTI_ROLLBACK is normalized disabled
only if BOOTLOADER_APP_ROLLBACK_ENABLE is explicitly false; enabled/unknown/missing
dependency is refused. This is not a general missing=false rule.

Exact capability/preference flags (V1_SUPPORTED, V2_RSA_SUPPORTED,
V2_ECC_SUPPORTED, V2_PREFERRED and the ROM-mode choice default helper) do not mean
security is activated. Actual enable fields still set risk categories; unknown
true security flags or malformed types are refused. Ordinary real projection with
rollback=false, antirollback absent, V2_PREFERRED/RSA_SUPPORTED=true is covered by a
regression. A flash caller should use the same interpreter on the hash-verified
config and refuse any secure_boot/flash_encryption/anti_rollback/additional_security_
enabled category. It must not substitute the public summary for the attested bytes.
Sources: v6.0.1 components/bootloader/Kconfig.app_rollback and Kconfig.projbuild.

## Optional short output root and read-only storage diagnosis

A locally registered project may set `build_root` to a dedicated absolute short
output directory outside its source tree. MCP requests still supply only a
registered project ID. Omitting the setting keeps the original
`state/firmware-builds/<full-input-hash>/<full-environment-hash>` layout.
An explicit root uses one full SHA256 slot computed from a domain-separated
canonical JSON object containing both complete hashes. No hash is truncated.

The logical manifest remains schema version 1. New manifests add
`build_location={"layout_version":2,"slot":"<full-sha256>"}` for explicit roots,
or `{"layout_version":1}` for the original layout. Private absolute roots are
never placed in the manifest or public diagnostics. A local identity receipt
binds the complete input/environment identities and the registered root digest.
Existing directories without a matching receipt are rejected; they are never
adopted or deleted. Old manifests remain readable via the original resolver.

Flash integrations call
`resolve_build_manifest(state_dir, registered_build_root, manifest) -> Path`
from `cardputer_mcp.build_locations`. The resolver verifies the persisted
manifest and identities, rejects links/reparse points below the pinned build root
(a registered `build_root` is resolved once per call; the operator may deliberately point
it through a symlink/junction, while state-derived output directories stay strict), and never silently falls
back from a compact location to a legacy root. It grants no flash permission;
consumer artifact hashes, security checks, successful job/plan checks and a separate explicit
execution request are still mandatory. Older manifests lacking flash/security receipts
remain diagnostic-only.

Preparation reports a bounded read-only storage snapshot: free/total bytes,
owned bytes/slots, whether the inventory is complete, the local minimum free
space, and `cleanup_enabled=false`. Only receipt-verified owned slots count.
The default minimum free space is 512 MiB and can be registered locally with
`build_min_free_bytes`. The check runs before output writes. Insufficient space
returns `build_output_insufficient_space`. There is no deletion, retention
executor or automatic cleanup. Unrelated files are neither counted as owned
nor changed.

Windows output directories over 200 UTF-16 code units fail early with
`build_output_path_too_long`. Choose a short dedicated root; this conservative
preflight does not guarantee that every external project-generated path fits
its toolchain. Output slot locks are held through build, hashing and publication,
and remain held when owned process termination is uncertain.

## Upgrade with existing registrations

Existing local profiles become the execution scope of current control principals;
there is no new grant database, first-run screen or automatic profile registration.
Deploying this behavior is a separate operator action. Retain private SDK/tool,
cache/output-root settings. No production configuration is edited by this change.
Old one-job source/build approvals stay recorded but are not converted into new
plans or reused; prepare a fresh registered-project plan. Old needs_review records
still require their existing explicit acknowledgement and are never cleared by
upgrade. New normal failures/cancellations need no acknowledgement. No source,
build artifact, backup or durable history GC is introduced.

## Console diagnostics

The broker also emits job start, terminal state and elapsed time, plus redacted
BuildLogs warnings/errors and progress sampled at most once every two seconds per
job. Structured ROM read progress is included. Output is UTF-8 on stderr only;
stdio stdout remains JSON-RPC. This adds no authorization or execution action.

Console output is best effort: one daemon writer uses a queue of at most 64
messages, each at most 2 KiB. A full queue drops older console messages to keep recent status; a closed/broken
stderr disables this writer. Slow or unread stderr cannot hold up a job or process
shutdown. No raw exception, plan, environment or result payload is printed.
Control characters are escaped after existing BuildLogs redaction. Native project
output can still contain application secrets that existing redaction cannot
recognize. Console output is local diagnostic data, not a new durable audit log;
MCP log filtering, ownership, cursors, bounds and retention remain authoritative.

## Durable successful-build references

New successful builds retain a bounded reference per job in the existing private
history.sqlite3 state database. The reference binds the owner, project and build
plan to the output location identity and canonical manifest SHA-256; it stores no
absolute output path or artifact bytes. The RAM cache remains limited to eight
builds, but cache eviction and broker restart no longer invalidate a retained
reference. Publication before job completion is inert: only a durable succeeded
build job with the same owned plan can resolve it. Interrupted, failed, cancelled
and needs_review jobs cannot become flash inputs through this reference.

Every lookup uses the currently registered project/output root, checks the stored
location identity and disk manifest digest, and rehashes all recorded binary/ELF,
flash-layout and security-config files. Missing, changed, linked or out-of-scope
files fail closed. Existing flash admission, security/device checks and separate
explicit execution still apply; lookup never opens USB or starts a build/flash/restore.

Only builds published with this reference support restart lookup. Historical
success rows and arbitrary old manifests are not adopted; explicitly rebuild an
older job if it has no reference. Rebuilding in the same output slot can supersede
the old bytes; old references then fail hash/manifest validation. References do not
promise immutable artifact storage. No rollback, retry, GC, retention migration or
file deletion is added. Preserve the existing state working directory and local
project/output registrations across upgrades. ESP-IDF remains external 6.0.1.


## Durable receipt reads independent of log retention

For a successful owned build, `status(view=logs, stream=build)` revalidates the persisted build
reference and returns its public `manifest` even after log expiry, eviction or
broker restart. When RAM output is gone and no durable job log exists, the result
still reports `expired=true`, `gap=true`, empty entries and the existing
missing-history reason; otherwise the durable entries are returned as above. It
never reruns the build or changes the job's outcome.

`artifact_status` is `verified` only after registered output/root identity,
manifest/plan/fingerprint binding and every binary/ELF/layout/security receipt
size/hash check succeeds. `unavailable` means no manifest is returned (including
missing, corrupt or unregistered artifacts); a stale RAM manifest is never used
instead. `busy` means another receipt hash read is in progress and no manifest is
returned by this read. Hash IO runs off the broker event loop, with one concurrent
receipt hash reader per builder. Bounded log reads and other broker reads remain
available. A subsequent explicit read can retry a busy receipt lookup.

Current read authorization and the original build principal remain mandatory.
The grant, broker/job identity and registration are checked again after hash IO,
before delivery. No absolute output path or raw filesystem error is exposed.
The manifest contains hashed relative artifact/layout/security receipts and grants
no flash permission. Source and flash log paths keep their existing behavior.
This read adds no schema, configuration, authentication-scope or dependency changes.

Synthetic regression coverage includes authenticated MCP broker restart, expired
RAM logs, corrupted manifests/identity/layout/security/binary/ELF bytes, file loss,
unsuccessful jobs, another principal, concurrent-reader limits, event-loop progress
and grant/registration/job invalidation during hash IO. Existing durable path/link
and hash checks are reused; no native build, USB or production DB is involved.

## Fixed diagnostic recipes and ON/OFF comparison

`submit.request.derby_detail_mode` accepts only `ON` or `OFF` for a
`kind=build` (`firmware_build`) with a registered project's owned `source_receipt`. An explicit
single detail selector chooses the built-in `j1-fixed-v1` recipe. Supplying both
`derby_detail_mode` and `derby_dma_mode` chooses `derby-dma-diagnostic-v1`.
The latter requires both typed axes; it does not infer missing values or fall
back after invalid evidence. No arbitrary CMake arguments or commands are accepted.

There is no diagnostic source-commit admission catalogue. Compatible new commits
use the same recipe without updating the broker. Full source/tree/submodule and
registered project/profile/tool identities remain bound to the existing owned
source receipt and build plan. A source SHA identifies bytes, not firmware quality
or execution authority. Ordinary builds without diagnostic selectors remain
independent of optional diagnostics and CI status.

Both requested axes, when present, are frozen into the plan, fingerprint, isolated
build directory, build-local sdkconfig and durable manifest. Diagnostic recipe
tool requirements apply to explicitly selected diagnostic builds. Existing
registered tool/environment restrictions still apply to ordinary builds.

The recipe adapter checks mandatory bounded producer proof against actual BIN/ELF
bytes, mode and hardware-seed symbols, fixed completion contract and IDF 6.0.1
BIN/ELF pairing. DMA evidence additionally binds actual cache settings and native
submit/finish functions. An enabled DMA image cannot be relabelled using nested
J1-only proof. Missing or inconsistent evidence fails validation; a contract ID
alone cannot certify compatibility or quality.

The existing `diagnostic_entry_sha256` wire field remains an identity digest of
source plus selected recipe, using its historical descriptor encoding. It is not
an entry in an approval list. Saved hashes/plans/receipts are not rewritten.
Only the exact original J1 repository/commit/contract without a DMA axis may omit
this field in legacy records. Present wrong/null digests are rejected. Unknown
contracts are unsupported, known-contract evidence can be incompatible, and an
unavailable requested feature is not inferred from a source commit.

Successful manifests retain proof and canonical hashes. `status(view=logs, stream=build)` can
revalidate retained exact bytes after log expiry/restart, independently of missing
RAM output. Changed or missing artifacts make evidence unavailable; expiry/gap
still describe lost logs. Artifact verification does not mean CI or hardware pass.
Raw CMake cache remains private and bounded to 128 KiB; export v1 publishes only
bounded cache hash/size and commits the projection through the immutable flash
plan. Existing original-manifest, export, owner and success-plan checks remain.

For a receipt-bound trial, `submit(kind="derby_fixed", flash_job_id=... )`
uses the principal's successful verified flash that remains the registered device's
current firmware receipt. Retained build/source/proof/artifact identities select
the comparison, not a caller hash or configured expected hash. Existing leases,
recovery, explicit execution, current authority, END/EXIT/STOP/HOME/quiet and
confirmed-close requirements remain. Legacy unbound trials do not become verified
receipt-bound comparisons.

Firmware CI owns quality/regression and diagnostic-contract validation. Its
source/configuration/run/job/attempt and exact artifact identities can accompany
retained evidence. Source-only CI success is not proof that a different local
rebuild's bytes passed artifact tests. Runtime execution requires no live CI API
or new approval/attestation service. CI-unrun local development remains possible
within registered capabilities; quality evidence is reported as unverified when
not established. No CI/host/mock result substitutes for physical acceptance.

Historical synthetic DMA fixtures use commit
`ac7cce7d20eb34fdd54659e6678d672d23e5d2d1` and descriptor digest
`e40c3f8972225ae25b3a4765e54650a7d2876bfdd4690989ca4383e0cf6ed5d8`.
Their bytes and hashes remain compatible; they no longer require a test patch
or a production admission entry. Local registration, deployment and device use
remain separate explicit operations. This change performs none of them.


### Source cache scan time budget

Each fresh cache traversal has a finite **30 second** cooperative deadline and
the existing 1,000,000 entry cap. The earlier 5 second guard was introduced with
the owned SOURCE read worker, when the payload cache allowed eight snapshots;
the cache later grew to sixteen without a corresponding metadata-I/O budget
change. The introducing change does not establish five seconds with a Windows
cold-cache benchmark. Thirty seconds provides sixfold time headroom while
keeping the traversal finite; it is a policy allowance, not a measured minimum
or a guarantee that every filesystem/load completes within that time.

The timer starts inside `_usage`, after registration and cache guard acquisition,
and covers the fresh directory walk, no-follow metadata checks and evidence-only
receipt classification. Ordinary source payload bytes are not hashed by this
capacity walk. Deadline/cancellation checks run between entries and directories
and again before returning totals. A blocked OS operation cannot be preempted by
this cooperative timer. A reported cutoff such as 5001 ms identifies the first
observed exhausted budget, not the duration needed to finish the scan.

The change does not alter the 16 payload / 2 GiB cache bounds, entry cap, evidence
limits, reference/receipt protection, source hash checks or the separate 180
second fetch-operation deadline. A deadline failure still returns only bounded
incomplete counters and publishes no capacity result or source receipt.
Read/preflight workers pass a stop event and check cancellation between entries.
An in-fetch scan does not receive that event: its next live/authority recheck can
be delayed until traversal completes or hits the cooperative 30-second deadline.
Ownership and the cache lock remain held throughout; final authority is still
required before publication. Same-cache exclusion remains unchanged.

Cold/warm Windows timings and phase/entry counts should be measured in the
separate deployment verification. Stage budgets, progress reporting and a
no-progress timeout could improve diagnosis later; they do not replace the
finite overall limit. Size-adaptive budgets need measured metadata throughput,
and cross-call walk/hash caches need mutation and reference-authority proofs.
Those algorithm changes are outside this time-budget adjustment.

### Offline manifest export contract

Successful owned build reads additionally return `manifest_export` and
`manifest_export_sha256`. The export has exactly four fields: strict integer
`schema_version: 1`, `contract: "broker-build-manifest-export-v1"`, lowercase
64-hex `original_manifest_sha256`, and `manifest` (the existing private-cache
projection). Unknown versions, extra fields, null/boolean versions, duplicate
JSON keys and invalid cache evidence reject without selecting a legacy format.
Canonical JSON is UTF-8 with sorted keys, compact `,`/`:` separators, ASCII
escapes (`ensure_ascii=True`) and no nonfinite numbers (`allow_nan=False`).
The complete canonical export is bounded to 1 MiB. `cache_evidence` has exactly
`retained_private: true`, strict integer `bytes` in 1..131072, and lowercase
64-hex `sha256`; the hash must match the typed proof's DMA and both detail cache
hashes. Raw cache text, cached paths and secret values remain private.

New flash plans commit `manifest_export_sha256` in their existing immutable plan
hash, alongside the unchanged internal `manifest_sha256`. Prepare revalidation,
dispatch and retained collector reads recheck that commitment. The successful
flash job and planned/captured collector bindings still bind the exact flash
plan hash and original manifest hash, owner, jobs, device, source, modes, entry,
proof and BIN/ELF identities. The export hash is not a replacement image digest
or an execution permission. The read's existing final current-owner, job,
registration and epoch checks cover the export too.

Durable export generation revalidates the original owned receipt and local bytes;
log expiry and a broker restart do not change its digest. Busy/unavailable reads
return no export. Damaged durable plans/references (including duplicate keys or
a plan payload whose hash changed) do not fall back to RAM. Exporting performs
no state migration or receipt rewrite.

Existing plans and receipts are never supplemented. Exact legacy J1 manifests
remain usable through the original full-manifest hash check. A secret-redacted
legacy DMA export without a committed export hash cannot pass the new offline
contract. A present malformed commitment must reject, not fall back to legacy.
The original digest remains the value bound by the successful plan: the redacted
export cannot recompute a hash that includes the omitted private cache text.
Consumers verify the export hash against a fixed trusted flash plan and its
successful job; checking a rewritten export's own hash is insufficient. This
establishes offline consistency within the existing owner-authorized durable
record trust boundary, not authenticity of an entirely forged JSON archive.

The response field is `manifest_export_sha256`; `export_sha256` is not an alias.
The same name in a flash plan remains part of that plan's immutable commitment.
The pinned FW consumer from commit `824e9f91b52069c08df68e967d83b44e9f7d5240`
is retained unchanged as `tests/fixtures/derby_manifest_export_pr38.py.txt`
(original blob `7f4c42db24c69855c10c4b03304d62792b7a7ef1`, SHA256
`fb275ee8e763e89a59d5deea132529e9d18787d731fae0f27b84acd06f1fc726`).
Tests pass unmodified producer and durable `status(view=logs, stream=broker)` responses through its
`read_export_response` and `bind_export` functions for all four detail/DMA choices,
including fresh logs, restart, log expiry and fixed-anchor tampering refusals.
The dedicated pinned-FW junction workflow also checks out that exact FW head and
the exact producer PR head on Linux and Windows. It exercises the complete host
record chain and the existing HTTP authorization/restart/corruption tests under
frozen dependencies. All images and durable records in these tests are synthetic;
no production registry, deployment, device session or hardware action is involved.
It adds no source admission, authorization scope, credential, device operation,
deployment or automatic replay.


## Failure diagnostics

Known broker-authored failures retain fixed result codes such as
`build_inputs_changed`, `idf_build_failed_needs_review`, `build_target_mismatch`
and `build_artifacts_missing`. Unknown exceptions remain `firmware_build_failed`;
native exception text, command arguments, environment values and paths are never
copied into persisted diagnostics.

For recognized build failures, `status(view=job).diagnostic.subprocess_exit_codes`
contains at most three numeric entries: `profile_export`, `build_diagnostic`
(the latest completed diagnostic command), and `idf_build`. Entries appear only
when that subprocess returned a status; a missing entry does not imply zero.
The private bounded build log also records a fixed-label exit-status line after
each completed subprocess. Existing log redaction, retention and gap semantics
still apply. No raw profile output or new persistent log stream is added.

An IDF exit code of zero is not a successful build receipt. Input fingerprint,
target, artifacts, flash layout/security, cancellation and final publication
checks must also pass. For example, a live project's first `sdkconfig` generation
can change the source fingerprint after preparation. These diagnostics identify
the observed guard failure; they do not establish the cause of historical jobs.
Cancellation, authority loss and uncertain cleanup retain their existing terminal
outcomes rather than being replaced by subprocess status. No automatic retry or
input-guard relaxation is introduced. Existing history is not backfilled.
