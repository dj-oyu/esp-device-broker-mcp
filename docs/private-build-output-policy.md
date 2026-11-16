# Private build output isolation

Compilation does not declassify assets. Object files, firmware, diagnostics,
compiler caches and CMake outputs can embed original model/source bytes. The
firmware execution gate remains closed. Log selection and cache suppression are
now wired into the executor behind that gate; no MCP private-input job is enabled.

## Logs

Private jobs must use producer-side suppression of arbitrary child output before
RAM, durable job logs, console and notification callbacks. Shared read-scoped log
access makes owner-only lookup insufficient. `PrivateBuildLogs.append` drops raw
lines; `event` admits only fixed broker-authored phase messages. Secret-pattern
redaction is not a substitute: compiler errors can quote ordinary private text.
Internal bounded diagnostic parsers may inspect generated output but must never
forward arbitrary strings to status, exception messages or another sink.

## Compiler cache and build output

Private jobs must bypass cache probing, configuration and statistics entirely,
pass IDF `--no-ccache`, and use `no_private_compiler_cache` after profile export.
It removes local/remote cache configuration and compiler-launcher variables and
sets disable flags without mutating the caller environment. This is not protection
against trusted build scripts deliberately executing their own cache command.
No private output may reuse an ordinary public output slot or cache. The private
artifact record derives a namespace from principal, exact public/private receipts,
binding, composition and build fingerprint; a namespace hash is not authorization.

## Artifact provenance and export

`private_artifact_provenance` constructs only identity fields, not source filenames
or private input names. Store it under the `private_build` manifest key. Artifact
lookup must separately verify the owned successful job and immutable manifest,
retain outputs in a protected owner-scoped root, and include raw firmware/object
bytes in the private classification. Publication must be atomic with job success;
cancellation cannot publish a new usable receipt. Retention cannot remove active
or recovery-referenced artifacts. No download or sharing endpoint is added here.

The existing general manifest projection and export validator now reject private
markers, including malformed/null markers, rather than projecting them through the
legacy public export. Ordinary manifest behavior is unchanged. A future explicit
owner-authorized private delivery contract must be separate and must not treat
public repository push permission as asset disclosure permission.

## Remaining gates

Wire policy at every subprocess/log callback, status and durable-history projection;
verify private output-root ownership/cleanup and native process lifetime; implement
private artifact admission/provenance and cancellation-atomic final publication;
verify no public cache reuse; then run native generated-data integration tests.
Windows scratch ownership and cross-user/child-lock evidence remain separate gates.
No actual private input, runtime configuration or device is changed by this work.


## Runtime wiring checkpoint

`FirmwareBuild` selects `PrivateBuildLogs` before attaching the durable tee and
before running a child. The private cache branch sanitizes the post-profile
environment, never probes a cache executable, skips `configure_ccache` (which would
remove disable flags), and returns a disabled policy so statistics are skipped
and the existing IDF command chooses `--no-ccache`. Ordinary branches are unchanged.

The general `BuildArtifacts` publication/lookup path now refuses private markers,
including malformed/null ones, and private selection in stored job plans. This is
a fail-closed boundary, not an implemented private artifact store. Dedicated
protected storage and cancellation-atomic publication are still required before
the execution gate can open. Tests exercise branches with generated data only.
