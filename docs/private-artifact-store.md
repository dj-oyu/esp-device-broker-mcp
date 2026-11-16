# Private build storage and runtime boundary

Production constructors leave `FirmwareBuild.private_runtime = None`. Private
execution remains refused until the native compiler, protected storage and
integration checks are installed. Tests use generated data, never actual private
assets. No download, general export or flash path is added.

## Data and finalization

`PrivateArtifactStore` uses a caller-supplied protected SQLite connection. It
checks the exact running job/plan, derives producer provenance, and commits bounded
artifact bytes (32 files / 128 MiB), receipt and successful job state atomically.
Resolution rechecks owner, plan binding, receipt and payload hashes. An uncertain
response is reconciled with `receipt_for_job`, not by replaying publication.

`commit_private_candidate` uses the builder's cancellation lock. It rechecks
binding, fingerprint, authority and joined children before committing the receipt,
bytes, job, fixed audit event and recovery pin in the same transaction. The final
live check occurs while the row is running; subsequent validation/SQL contains no
external callbacks. False guards or SQL/COMMIT failures roll back. After COMMIT,
late stop requests are rejected and notifications cannot undo success.

ProfileJobs invokes this finalizer without a second ledger transition. Generated
integration tests cover real admission, worker, thread handoff and Store, including
success, cancellation and the default-closed path. Device/non-deferred private
execution remains refused. Ordinary jobs retain their existing path.

## Source, output and child lifetime

`NativePrivateProducer` composes source and output in separate protected Windows
scratch roots. A trusted compiler must use those roots and the private log sink;
raw compiler output is withheld and shared compiler caches must remain disabled.
Only bounded `.bin`/`.elf` files are captured by the handle-relative Windows reader
with manifest hash verification. Candidate payloads are excluded from repr.

Existing active/uncertain children block new work. Cancellation and authority must
be rechecked after callbacks and immediately before every child launch. Unconfirmed
termination retains both roots/handles and latches `output_uncertain`; explicit
recovery may release them only after confirmed join. Cleanup failure prevents a
success candidate. This is not a sandbox for hostile code running as the broker.

## Retention and remaining gates

Active/recovery references are durable pins checked in the same write transaction
as pruning. Unpinned pruning preserves job history. There is no automatic unpin API;
consumer reference acquisition/release and continuation integration remain required.
Pruning is logical deletion, not secure erasure: WAL, freelist and backups may keep
old bytes and must remain private.

The internal ESP-IDF callback now verifies profile/tool identities, runs each child
with authority/stop monitoring, disables shared compiler caches, and puts TEMP/TMP,
SDKCONFIG and build outputs under the private output root. A producer without a
test compiler callback uses this internal entry point. Production still installs
neither producer nor runtime adapter. Synthetic child tests are not actual IDF builds.

Before production installation: complete native generated compiler integration,
establish protected DB ownership, verify child monitoring and cleanup,
and connect consumer pin/continuation lifecycle. Host fixture success does not
prove native isolation or a successful firmware build. No real retention, deployment,
configuration change or private transfer has been performed by these checkpoints.
