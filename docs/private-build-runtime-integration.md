# Private build integration gates

`stage_private_build` is a disconnected internal orchestration context, not an
MCP entry point and not called by `FirmwareBuild.execute`. Its generated-data tests
substitute source resolution, native source reading and Windows materialization.
They prove call ordering/contracts on the host, not combined Windows behavior.

## Implemented staging order

1. Copy the plan selection, check principal/cancellation, and re-prepare the exact
   public recipe plus all private binding fields/fingerprint.
2. Hold the existing public source cache lease through owned receipt resolution
   and native handle-relative reading. Reject source/scratch root overlap before
   staging. The verified receipt supplies file inventory, never request paths.
3. Resolve private bytes again in a caller-thread-owned store connection, then
   compose a separate workspace plan with collision and commit checks.
4. Re-prepare before staging and before yielding; create Windows scratch through
   the native backend; expose a scratch path separately from the original source
   identity and composition digest. Recheck on successful context exit.
5. Clean scratch on normal exit, cancellation or consumer failure. The consumer
   must stop/join all child processes before leaving the context.

## Gate still closed

Before calling this from execution, verify the combined context on native Windows
with generated data, ownership/cancellation during process execution, and artifact
provenance. The original source snapshot and public cache must stay unchanged.
The future executor must use staged `project_dir` only for execution, keep original
recipe identity for rechecks, and store composition/private-binding identity in the
private artifact receipt. No public-source-only fallback is permitted.

A protected workspace does not sandbox build scripts running as the broker user.
Shared logs, compiler caches and artifact delivery require explicit private-input
handling; ordinary public cache reuse must not leak generated private outputs.
The existing firmware execution gate remains closed until those paths and native
prerequisites are implemented and verified. No runtime configuration, actual
private assets, credentials or device state are changed by this checkpoint.
