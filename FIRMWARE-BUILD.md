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

Call `prepare_test` with `test="firmware_build"`,
`project_id="cardputer"`, a fresh `job_id` and optionally an owned source_receipt.
A current authenticated control principal is required. Omit session: build and
source fetch do not acquire a USB lease. The existing local project/profile
registration authorizes execution within those registered inputs; no additional
grant, approval screen or per-job APPROVE is required. Execute the exact returned
plan_hash. wait_for_approval has no effect on these registered-profile jobs.
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
and integrity are still checked. Repeated execute calls return the durable job
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

The fixed build argument vector is external Python, external `tools/idf.py`, `-B`,
the broker-generated directory, `build`. Canonical registered project identity plus source/profile hashes and external
Python/idf.py/compiler/environment fingerprints partition build directories. The
expected target is fixed through IDF_TARGET and checked against the generated
project description. Diagnostics require the actual `idf.py --version` to report
exactly ESP-IDF v6.0.1 and require a registered-environment ESP32-S3 compiler.

`read_build_logs` requires current read authorization and the original job
principal. Default output is progress plus warnings/errors, with two lines of
context. Explicit levels: error, warning, info, debug, unknown. Stderr is a stream,
not an error severity. Notes and multiline text remain available through context
or unknown-level reads. Each reader owns its epoch/sequence cursor; reads do not
consume entries. Retention is at most eight jobs, 2,048 lines / 256 KiB per job and
five minutes since the last appended line. Oversized process lines are omitted after 4,096 bytes rather than split into
potential credential fragments.
Cursor gaps, restart epoch mismatch and expiry are explicit. Raw output stays in
RAM; no caller-specified path can be opened. Known credential patterns and known
ambient secret values are removed; arbitrary application secrets cannot be
universally recognized, so the explicit execution request includes log delivery.

Cancellation of a running build requests termination and returns before shutdown
is confirmed; poll `job_status`. Confirmed termination results in cancelled, never
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

The remaining Tunnel transport expected tool catalog includes read_build_logs.
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
manifest and identities, rejects links/reparse points, and never silently falls
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

For a successful owned build, `read_build_logs` revalidates the persisted build
reference and returns its public `manifest` even after log expiry, eviction or
broker restart. The result still reports `expired=true`, `gap=true`, empty entries
and the existing missing-history reason when RAM output is gone. It does not
reconstruct lost logs, rerun the build or change the job's outcome.

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

## Fixed J1 detail ON/OFF comparison

`prepare_test.request.derby_detail_mode` accepts only `ON` or `OFF` for a
`firmware_build` with a registered project's `source_receipt`. This limited
implementation accepts the public `dj-oyu/cardputer-adv-pocketjs` source commit
`b561aea1c0798feb28055a00d36681fae81ac58a`. It selects only the fixed
`POCKET_DERBY_DETAIL_MEASURE` boolean; no caller-supplied CMake options or commands
are accepted. Telemetry, measurement and the fixed workload remain enabled in
that source revision.

The selected mode is frozen into the plan, fingerprint, isolated build directory,
build-local sdkconfig and durable manifest. Both modes use the same source
receipt. A stale cache in the other mode's directory cannot select the output.
The mandatory source proof must be present and bounded. The broker checks its
mode and artifact hashes against the actual BIN/ELF, the linked detail-mode and
hardware-seed symbols, the fixed owner's completion contract and the IDF 6.0.1
BIN/ELF pairing. Missing or inconsistent proof fails the build. The manifest
retains `diagnostic_modeproof` with the JSON proof and its canonical SHA256;
`read_build_logs` can return this verified receipt after log expiry or restart.
Changing or removing the retained proof or image makes the receipt unavailable.
Synthetic proof is not a physical test result.

For a verified comparison trial, pass a typed `flash_job_id` to
`prepare_test` with `test="derby_fixed_workload"`. It must identify this
principal's successful, verified flash that remains the registered device's
current firmware receipt. The broker reads retained firmware bytes and manifest,
checks the build/source/modeproof chain, and freezes the expected ELF hash and
receipt identities into the collector plan. A hash argument, an old configuration
hash, a failed flash or another device/principal cannot supply that identity.
The existing lease, recovery, explicit approval and v2 END/EXIT/STOP/HOME/quiet/
close checks continue to apply. Legacy trials without `flash_job_id` retain their
unverified provenance and do not qualify a receipt-bound ON/OFF comparison.

Existing registered project/device configuration is reused; this change does not
add a variant registration or alter SDK, authentication or operational settings.
Before actual use, a separately authorized operator must deploy the reviewed
broker, refresh client tool metadata, confirm the existing allowlisted project
and device registration, then explicitly fetch/build/flash and run each mode's
trial. Deployment, configuration edits, hardware writes and merging are separate
operations; none are performed by these synthetic tests.

Diagnostic admission is centralized in a closed source registry containing only
that J1 commit. New detail-build plans, fingerprints, manifests and collector
bindings include the selected entry's `diagnostic_entry_sha256`. It hashes that
entry's exact source/contract policy, independently of unrelated registry entries;
source byte/tree/submodule identities remain verified by the existing source
receipt and build/flash provenance chain. An unrelated entry cannot change an
existing entry's digest. The production registry adds no source heads or DMA
selection, and has no configuration or MCP editing interface.

Successful J1 manifests and collector history from before this field existed
remain readable through the original proof and receipt checks. Only the original
J1 entry may omit the new digest. A present wrong/null digest is rejected.
Previously prepared detail-build plans require preparation again because their
fingerprint now includes the entry digest. Adding a new runtime source head or
changing a proof contract remains a separate reviewed acceptance change.

### Isolated DMA consumer contract

The consumer also understands the separate `derby-dma-diagnostic-v1` contract.
This support does **not** add an admitted source: the production diagnostic registry
still contains only the original J1 head. `derby_dma_mode` is a separate typed
`ON`/`OFF` selector; it is rejected for that J1 entry. A future DMA-contract entry
requires both `derby_detail_mode` and `derby_dma_mode` explicitly, with no defaults,
null values, arbitrary CMake arguments or fallback to the J1 detail-only proof.

Both axes and the per-entry digest bind preparation, fingerprint/build slot,
SDKCONFIG location, exact command, successful manifest, flash receipt and collector.
The separate producer proof must match both requested axes, actual cache settings,
linked native submit/finish functions, nested detail proof and actual BIN/ELF bytes.
Missing, duplicate, modified or mismatched evidence fails the build. Raw cache
bytes are bounded to 128 KiB and retained privately in durable evidence and flash
snapshots; public build-log manifests return only their hash/size summary. This
keeps cached paths or credentials out of MCP responses. Broker manifests have a
1 MiB read bound; the smaller identity receipts keep their 64 KiB bound.

Prospective admission requires a separate reviewed registry change and local
registration authorization. The exact proposed entry (currently test-only) is:

```python
DiagnosticEntry(
    repository="dj-oyu/cardputer-adv-pocketjs",
    commit="ac7cce7d20eb34fdd54659e6678d672d23e5d2d1",
    contract="derby-dma-diagnostic-v1",
    modes=("ON", "OFF"),
    dma_modes=("ON", "OFF"),
)
```

The proposed entry digest is `e40c3f8972225ae25b3a4765e54650a7d2876bfdd4690989ca4383e0cf6ed5d8`.
The FW source tree is `f14367d2c73e5531823b209117a470f8df64db49`.
[Producer PR33](https://github.com/dj-oyu/cardputer-adv-pocketjs/pull/33) has four
successful workflows / twelve jobs at that exact head:
[fixed](https://github.com/dj-oyu/cardputer-adv-pocketjs/actions/runs/39393775102),
[DMA host and four target combinations](https://github.com/dj-oyu/cardputer-adv-pocketjs/actions/runs/39393775090),
[detail](https://github.com/dj-oyu/cardputer-adv-pocketjs/actions/runs/39393775135),
and [home](https://github.com/dj-oyu/cardputer-adv-pocketjs/actions/runs/39393775132).
The full capture-contract evidence belongs to the separate integration PR32;
PR33's path-filtered checks do not repeat that workflow. Target logs contain typed
proof receipts, but they do not replace an independently retained source receipt
or downloaded-image verification. The existing source archive/tree/submodule byte
checks remain mandatory. No archive source hash is guessed from a branch or tree.

Before admission or device use, review the exact entry digest and consumer CI,
source fetch/build receipts and four actual typed proofs; authorize registry/local
registration and deployment separately. Physical B0/B1/D1 comparisons and display,
recovery and normal-exit acceptance remain separate hardware gates. This consumer
change performs none of those operations.
