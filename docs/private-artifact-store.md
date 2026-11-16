# Private artifact commit checkpoint

`PrivateArtifactStore` is disconnected from runtime startup and firmware execution.
It writes only a caller-supplied protected SQLite connection. Synthetic tests use
fresh generated fixture databases, never broker state or actual private assets.

A trusted producer supplies the owned immutable plan and the composition digest
from staging. The store derives the `private_build` provenance tag itself, checks
that the existing jobs/plans row matches that exact plan and is still running,
then commits artifact bytes, receipt and job success in one explicit transaction.
The bounded payload (32 files / 128 MiB) is hashed and read back before final guard.
False guard, cancellation or SQL/COMMIT failure rolls back the job and artifacts.
Python 3.12 autocommit and legacy transaction modes are covered. An uncertain
response is reconciled by `receipt_for_job`, not by blindly publishing again.

The caller must own/serialize cancellation through guard and commit, attest child
process termination and verify current execution authority. This helper is not an
authorization service; its guard callback must be a trusted read-only check. Its
producer must pass the actual staging digest, not caller-controlled metadata.
No code here opens the firmware execution gate or starts a build.

Resolution checks successful job binding, owner, receipt hash and all payload
hashes. These raw bytes remain private, including firmware and compiler outputs.
No external download, general export or flash path is added. The ordinary artifact
store continues to reject private artifacts.

Retention runs under the same database write transaction as pin inspection.
Active/recovery references are explicit durable pins and cannot be pruned. There
is deliberately no automatic unpin API: safe reference release and integration
with actual job/recovery lifecycle remain required. Unpinned pruning preserves
job history but removes payload/receipt; a later lookup reports unavailable.
No retention operation has been run against real data.

Remaining integration: native protected database/storage ownership; actual
producer-to-store wiring; broker success/audit/continuation bookkeeping in the
same finalization transaction; cancellation serialization; pin acquisition before
use and safe release; resource budgets; native generated-data integration tests.
Until those and Windows gates pass, runtime execution remains disabled.

Pruning is logical deletion, not secure erasure: SQLite WAL/freelist pages and
external backups can retain old bytes. The database and backups must stay private;
this checkpoint does not change storage encryption, checkpointing or wipe policy.

## Runtime finalization adapter (not installed)

`commit_private_candidate` uses the existing builder cancellation lock, rechecks
full binding/fingerprint/authority and confirms no active/uncertain child. It uses
the actual Store connection: job success, private receipt/bytes, fixed audit event
and a durable recovery pin are one SQL transaction. It does not call the ordinary
ledger transition (which commits separately). The final live check occurs while
the row is still running; after COMMIT the builder rejects a late stop. Store
notifications occur only after successful commit and cannot undo it. The producer
must call this synchronously on the broker loop after worker termination.

Generated tests exercise the real Store schema, cancellation first, commit first,
authority loss, active children, COMMIT failure and notification failure. This
adapter is not wired to ProfileJobs, the device job path or the execution gate.
Protected DB installation, producer output capture, retained workspace lifecycle,
consumer pin/continuation hookup and native integration remain prerequisites.
