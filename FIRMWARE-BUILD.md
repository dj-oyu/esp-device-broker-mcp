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

Acquire an authenticated lease, then call `prepare_test` with
`test="firmware_build"`, `project_id="cardputer"` and a fresh `job_id`. Review and
approve the immutable plan locally, then call `execute_test` with its hash; or
submit `wait_for_approval=true` and approve via the existing GUI. Build requires
manual approval even when mock automatic execution is enabled. The approval
covers profile execution, Python/toolchain diagnostics, project build scripts and
bounded output delivery to the original principal. Repeated execute calls return
the durable job rather than rebuilding. Changed inputs invalidate the prepared
plan. Interrupted or failed jobs need review; restart never restores authority.

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
universally recognized, so local approval must consider log delivery.

Cancellation of a running build requests termination and returns before shutdown
is confirmed; poll `job_status`. It results in needs_review, never success/replay.
Windows assigns the suspended child atomically to a kill-on-close owned Job Object
and inherits only NUL plus the two output pipes. Leader exit does not establish
subtree exit. Failed teardown retains the owner and blocks broker lock release.
No process-name/PID search or termination of unrelated processes is used.

A successful build writes broker-manifest.json under its private build directory
and returns its public manifest with the logs. It includes schema version, exact
IDF version, target, input/environment fingerprints, artifact relative names,
lengths and SHA-256. `flash_authorized=false` is explicit. A later flash feature
must reverify artifacts and obtain independent immutable-plan/local approval;
this feature provides no flash operation or permission. Old interrupted build
jobs are not marked recovered or successful.

## Validation in the delegated cloud environment

- 2026-10-12: compileall succeeds.
- Dependency-free unittest suite: 20 passed, two skipped because pyserial/filelock
  are not installed. With cloud-only lock/port dependency facades injected, all
  22 tests pass. This exercises the actual Broker/SQLite/PlanApprovals/PlanReview
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
lock/source changes, then prepare and approve a fresh build plan. This feature
adds no arbitrary dependency-install MCP command or automatic approval. If an
approved build itself changes dependency inputs, inspect and acknowledge its
needs_review result before creating a new job/plan; never reinterpret that old
approval or unsuccessful job as success. Dependency locks and .cache source
content must not be excluded from hashing merely to make a first build pass.

Actions packaging run 38295992731 succeeded for PR head 6bf848df. It checked out
synthetic merge source 60e8339ff98c8f551d43c7656f40590e0fdeef0c, whose tree equals
that head's tree (8aac63bacc4dd3eb85b28fbf54471e5f9de8828f). Extracted EXE authenticated
mock status/log smoke passed. New modules are static imports in the successful
broker startup and are also covered by collect_submodules(cardputer_mcp). Direct
new-tool catalog/schema and artifact byte inspection remain independent checks.
