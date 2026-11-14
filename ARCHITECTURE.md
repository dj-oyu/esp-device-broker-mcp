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

## Removed diagnostics

The legacy DERBY diagnostics were removed: the `derby_smoke` and
`derby_fixed_workload` jobs, their telemetry, reports and private results, the
`derby_detail_mode`/`derby_dma_mode` build options with their recipe proofs, and
the fixed-collector flash binding. The broker keeps its queue, worker,
authorization, cancellation and cleanup boundaries unchanged.

Durable history is still read. Stored plans and jobs of the removed kinds, and
build plans or manifests that carry the removed build selection, appear as
generic records in `status`, the history archive, the GUI job history and the
operator timeline. Their private extras are never shown. They cannot be prepared,
queued or executed again, and a build that carries the removed selection is not
resolved for flash. The `derby_fixed_trials` table is left inert in existing
state databases.

Existing wire APIs and records of every other kind remain compatible.
Rollback means selecting the previous code path, never replaying a physical
operation, rewriting durable history, or implicitly restoring firmware.

## Shared helpers added by redesign stage 4

These hold one copy of a check that several callers used to repeat. Each
caller still passes its own policy, so the deliberate differences between
callers stay visible (see `DESIGN-REDESIGN.md` section 5.8).

- `broker_link.py`: the default loopback broker URL and the shared URL
  parsing core with per-caller policies. `client_environment.py` stays
  standard-library standalone and keeps copies that a test compares.
- `tunnel_checks.py`: Tunnel configuration checks and the read-only broker
  probe sequence shared by the console launcher and the GUI manager. Process
  supervision and readiness loops are not shared.
- `flash_lkg_models.py`: validate-only models for the LKG envelope and its
  metadata.
- `source_receipt_model.py`: validate-only shape of source snapshot receipts.

The two model modules validate parsed JSON and are never dumped back, so
stored bytes and their content hashes are unchanged.
