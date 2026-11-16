# Private composition consumer boundary

`private_workspace_plan.prepare_workspace` prepares immutable bytes from an
already resolved public source and an owned private receipt. It does not mutate
public snapshots, create filesystem trees, run a build, or grant authority.

## Required integration order

1. Resolve the authenticated caller's public source receipt with the existing
   source service. Verify project, exact commit, registration and cache contents.
   Do not accept a source inventory or registration from MCP parameters.
2. Resolve private input through the trusted locally registered input ID and
   `PrivateInputStore`. The store rechecks owner, registration and payload hashes.
3. Prepare composition. Reject collisions (including case aliases and file-parent
   collisions); bind source receipt, private receipt, commit and recipe identity.
4. Allocate an exclusive, broker-owned per-job staging directory outside the
   public source cache. Write byte copies, never hardlinks or symlinks. Use the
   platform no-follow adapter for every component and revalidate file identities.
   Do not copy `.git` metadata or inherit executable checkout hooks.
5. Verify the complete composed inventory before exposing a ready workspace.
   Publish the private job receipt only after successful staging. Failure or
   cancellation exposes no ready receipt; cleanup must touch only the owned job
   directory. Hold ownership through child-process shutdown before cleanup.
6. Run only the registered recipe and bind output provenance to the composition
   identity, not merely the public commit. Outputs and logs are private by default.

A private directory is not a sandbox: build code can read input and emit it in
binaries/logs. A trusted recipe/commit and explicit output disclosure policy are
required. This checkpoint does not prove hostile-build containment, Windows
filesystem race resistance, source receipt ownership integration, or real builds.

## Synthetic checks

The planner tests cover input/store non-mutation, deterministic identity, receipt
binding, owner and commit rejection, changed source bytes, mutable payload rejection,
and case/file-parent collisions. All payloads are synthetic. Filesystem creation,
atomic promotion, cancellation cleanup and Windows no-follow behavior remain
integration tests for the materializer; no real private data is used here.
