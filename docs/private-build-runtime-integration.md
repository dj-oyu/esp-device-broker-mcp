# Private build integration gates

The private pipeline is implemented as an opt-in internal runtime. Production
`FirmwareBuild.private_runtime` remains `None`: publishing these helpers or passing
synthetic tests does not enable private builds on a running broker.

## Implemented pipeline

1. Import only an existing local registration. Check caller authority, owned public
   source receipt, exact source commit, inventory digest and current recipe before
   native reading. Hold the protected database guard while issuing an owner-bound
   private input receipt. No request supplies a local path or uploaded bytes.
2. Bind the input ID, owned receipt and registration binding into the plan. Retain
   staging composition identity in artifact provenance. Recheck
   the current recipe and all binding fields before staging and execution.
3. Hold the source cache lease and native no-follow handles. Materialize source and
   private inputs into a separate protected workspace, rejecting collisions and
   overlaps. Keep the original source/cache unchanged.
4. Use a separate protected output root, temporary directory and disabled compiler
   cache. Drop raw compiler logs; publish only fixed diagnostic events. Read outputs
   relative to the held root, without reopening it with incompatible share modes.
5. Commit private artifact bytes, provenance, terminal job state, audit event and
   recovery pin atomically. Generic artifact export and archive paths must not copy
   private data or detach the associated job/plan records.
6. Clean workspaces only after all children join. Unknown child state retains roots
   and database protection until explicit recovery; it is not successful cleanup.

## Evidence and its limits

Windows generated-data tests exercise native registration reading, protected SQLite
state, staging/output reading, the real ProfileJobs worker and atomic finalization
in one pipeline (`test_private_native_profile_pipeline.py`). Separate native tests
cover reparse/hardlink refusal, locked ancestors, owner/DACL checks and cancellation.
The integrated fixture substitutes public source receipt/recipe resolution and the
compiler. It does **not** prove an actual ESP-IDF build or actual private asset use.
The compile callback has generated-process tests, not real compiler evidence.

## Production activation checklist

Before installing a runtime into a live broker:

- Identify the running broker version, authenticated caller, registered project and
  exact source commit/recipe. Do not infer these from a test checkout.
- Verify the actual state database and configured scratch container meet the native
  protection checks. Do not silently change existing ACLs or credentials.
- Identify an authorized existing local private input registration and exact
  inventory. Separate consent for build use from any specifically denied archive
  or transfer operation; do not route around a denied operation.
- Review actual build scripts and tools as code running under the broker identity.
  A protected directory is not a sandbox against those scripts or another process
  running as the same user. Decide the trust boundary before using real inputs.
- Use an owner-bound receipt and exact recipe, install the producer/runtime through
  a reviewed configuration path, then test an actual build with private logs,
  temporary files, cache and artifact isolation intact. Keep ordinary delivery and
  flash unavailable until their own private-consumer permissions are satisfied.
- Record exact head, source/input/recipe identities, build result and cleanup state.
  A firmware build is not device verification or an FPS measurement.

The current code has no automatic production runtime installation. A typed selector
alone must not open that gate. Deployment, real-input ingestion and real compilation
remain separate actions with separate evidence and applicable authorization.

### Read-only preparation diagnostic

`preflight_private_activation` checks an existing plan for the actual caller under
native database protection, reusing receipt/recipe verification without creating
private tables or invoking the configured store factory. Its fixed summary is
neither an activation token nor evidence of a real compiler run. The helper does
not establish live executable/config provenance, create registrations, install a
runtime, inspect or modify existing ACLs beyond the protection checks, or prove
scratch capacity/availability. Execution must repeat its checks under its own
operation guard; a successful diagnostic cannot be carried across changed state.

### Per-thread receipt readers

`PrivateReceiptFactory` captures a protected database path and native file ID on
the owner thread, then pins and compares that identity before opening a separate
`mode=ro` SQLite connection on each consumer thread. It returns only the existing
receipt reader, closes SQLite before unpinning, and never initializes tables.
The existing main connection is not passed into workers. Read-only SQL does not
promise that SQLite never creates or maintains WAL/SHM auxiliary files.

Capture does not prove that a previously opened main SQLite handle still names
the same file if the path was replaced before capture. Production requires a
lifetime guard established **before** opening Store (or an equivalent demonstrated
identity guarantee). This helper is not automatically installed, does not repair
existing state permissions, and does not establish that live broker prerequisite.

### Protected Store session

`PrivateStoreSession` pins an already protected database before initializing its
writable Store, and holds that lifetime pin until Store closes. This operation
writes schema/WAL and is not the read-only preparation diagnostic. Its worker
receipt factory rejects new leases after closing starts. Active leases keep the
Store and lifetime pin alive; the owner must join those workers and retry close.
Uncertain reader cleanup is latched rather than treated as successful release.

This is still an internal, uninstalled session primitive. It does not supervise
compiler processes: a production owner must retain it across the entire runtime
and join every child before closing, including children without receipt leases.
It does not migrate existing live state, alter ACLs, or create a protected database
from an unprotected one. Production ownership, source registration and actual
private asset readiness remain separate checks.

### Explicit runtime ownership and shutdown

`PrivateRuntimeSession` can explicitly attach a runtime and session receipt reader
to an idle builder. It is not called by production startup. Shutdown irreversibly
closes private runtime admission, rejects new reader leases, and requests stop.
It does not kill or wait for children, infer joins from a timeout, or clear any
uncertainty flags. Active workers, retained process resources and uncertain output
keep the Store and lifetime guard in place for explicit recovery.

A supervisor lease also prevents an outer Store session context from closing the
DB/pin after the runtime wrapper refuses shutdown. Only confirmed runtime cleanup
permits lease release and Store closure. An explicitly attached Broker adapter uses a two-phase shutdown: ProfileJobs
closes admission and joins its worker without closing Store; Broker releases the
adapter only at its final Store-close step, after remaining state writes. The
default adapter is None and startup does not construct or install one. A successful
synthetic shutdown is not permission to restart or replace a live broker.

The ProfileJobs drain task is shielded once started, so cancellation of a waiting
close caller does not cancel its worker or enqueue a second shutdown sentinel on
retry. Identity changes across awaits and unconfirmed child cleanup refuse release.
This does not promise autonomous completion of all Broker shutdown phases: a caller
cancelled before ProfileJobs drain starts still needs a subsequent close attempt;
the existing Store/guard remains retained in the meantime.
An explicit close retry may resume a failed drain only after the original
adapter/Store identity is restored and the recorded failure is the fixed ownership
mismatch. Sentinel submission is tracked so that this retry cannot enqueue a second
one. Other drain failures remain blocked; they are not treated as transient errors.

### Explicit protected Store startup

`Broker(..., private_store_session_factory=...)` is an internal, in-process
injection point. Its default is `None`; no configuration enables it automatically.
The factory receives the expected existing database path and the broker's
`on_commit` callback, and must return a new, unopened `PrivateStoreSession` for
that path. Startup enters the session before opening the writable Store, preserving
the callback and native lifetime pin. This path does not chmod the database or
change existing ACLs. The protected database must already exist.

The session alone does not install `PrivateRuntimeSession`, a private producer,
or receipt ingestion. Those remain explicit subsequent steps. Synthetic startup
and shutdown tests do not establish that actual private assets can build.

A known-idle startup failure closes Store before unpinning and releases the broker
locks. If a worker has started, a child is active/unknown, or cleanup is uncertain,
the broker retains the reachable session and locks instead. Reusing that Broker
for another start is refused. Explicit `Broker.close()` performs verified shutdown;
it may also retry partial Store-initialization cleanup when no worker/runtime/child
ownership remains unconfirmed. A failed SQLite close retains the pin for another
explicit attempt after the fault is resolved. A session's unknown-handle cleanup
latch is never cleared by this retry, so not every failed startup is recoverable
in-process. A factory-returned already-open session is refused without taking
ownership or closing it.

At final shutdown, a protected Store session is closed even when no runtime adapter
was installed. If both exist, the adapter must reference that same session and
Store. Captured identities are checked across shutdown awaits to prevent closing a
replacement database or releasing a different session's protection.
