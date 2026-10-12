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
target = 'esp32s3'
```

The registered profile must support `-e`: output literal KEY=VALUE lines and
return. It is invoked with the system PowerShell `-NoProfile -File profile -e`,
never dot-sourced. Malformed or duplicate assignments, stderr, excessive output
or a nonzero exit fail closed. Only an explicit environment allowlist reaches the
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
the broker-generated directory, `build`. Source/profile hashes and external
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
- Dependency-free unittest suite: 10 passed, one skipped because pyserial is not
  installed. Includes mock profile/diagnostics/build/manifest/read traversal,
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
