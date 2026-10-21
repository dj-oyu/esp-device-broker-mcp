# Broker application boundaries

Authenticated developers operate their locally registered projects and devices.
Preparing an operation does not execute it. Source, build, flash and restore are
separate explicit operations. The broker preserves current authority, ownership,
exact input and artifact identity, exclusive leases, bounded I/O, recovery and
durable results. Firmware CI owns firmware quality and diagnostic compatibility.

Dependencies point inward: transport/device/storage adapters implement ports
defined by the application. Inner rules do not import Broker, HTTP, SQLite,
SerialBackend, or a firmware commit registry. A request's owner, scope, contract
ID or self-hash cannot establish trusted authority or firmware quality.

## First vertical slice (M1)

`fixed_diagnostic.py` contains the receipt-bound fixed diagnostic prepare and
terminal-publication use cases, their small ports, immutable subject context,
and pure owned-flash/build-chain identity rules. `collector_binding.py` remains
the compatible outer facade for retained image/proof checks and the existing
serial preflight. The broker keeps the existing queue, worker, authorization,
cancellation and cleanup boundaries. Store publishes private capture and the
terminal job in its existing single transaction.

The context is constructed from the authenticated principal and current resident
lease/epoch, never from wire owner/scopes. It carries identity, not a new grant.
Existing current scope/lease/job/registration checks still run before dispatch,
during work, and before success or private-result publication. Callback-free
final checks and uncertain-cleanup ownership barriers remain in place.

| Requirement | Invariant | Implementation/acceptance |
| --- | --- | --- |
| U2 shared broker | I1 owner, I4 exclusive lease | Existing authenticated queue/worker guards; foreign evidence never reaches device preflight |
| U5 explicit operations | I2 exact plan, I3 no replay | Prepare port has no execute; existing execute deduplication retained |
| Exact artifact | I5 owned source/build/flash chain | Pure chain comparisons plus retained bytes/proof in outer adapter |
| Private results | I8 owner and atomic publication | One completion-port call; Store retains capture and terminal state atomically |
| Runtime safety | I6 recovery, I7 bounded I/O | Existing flash/recovery and serial implementation unchanged |
| Evidence scope | I9 quality vs identity, I12 no mock-to-hardware claim | This refactor does not certify firmware or physical execution |

## Following stages

M1 is structural separation only. M2 must remove exact-commit membership from
new diagnostic selection and use supported recipes plus actual artifact/config
bindings. Merely hiding a registry behind a port does not complete that work.
M3 isolates historical descriptor decoding without rewriting old plans, hashes,
receipts or recovery references.

The final acceptance is that compatible firmware commits work without broker
updates; ordinary local builds remain possible without CI; and contract changes
cannot bypass authority, target, lease or recovery checks. CI evidence can be
retained with exact artifacts. Execution must not require a live CI API, a new
approval mechanism, attestation service, or dynamic code-loading framework.

Migrate one flow at a time. Existing wire APIs and records remain compatible.
Rollback means selecting the previous code path, never replaying a physical
operation, rewriting durable history, or implicitly restoring firmware. No
deployment or hardware behavior is established by synthetic tests.
