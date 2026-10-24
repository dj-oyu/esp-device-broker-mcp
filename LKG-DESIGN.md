# Last-known-good firmware: implementation in progress

Runtime integration is in progress; this PR is not ready for hardware use.

- Keep the existing complete device-bound backup as an immutable initial rescue
  baseline of type `full_backup`; never confuse it with firmware-only recovery.
- Persist the exact written BIN bytes and manifest/security/full ELF evidence in
  private content-addressed storage. Reuse identical objects by SHA-256. Build RAM
  cache eviction or build-folder cleanup must not remove retained recovery bytes.
- Promote only after successful write, exact readback, same-device expected
  IDF/ELF boot evidence, confirmed close, and final lease/principal checks.
- The broker's final successful-job transaction updates the durable pointer;
  the IO worker only stages immutable candidates. Cancel, crash, failed verification
  or uncertain close keeps the prior successful version.
- Freeze recovery receipt/hash at prepare time, including a restore's implicit
  choice of the current receipt. Explicitly execute that exact target, never dynamic latest.
- A successful explicit restore makes its verified target current. Build time
  does not choose the current version. Restoration is never executed implicitly.
- Prioritize the same partition/security layout and a single factory app. Do not
  initialize or overwrite NVS, filesystem or otadata in firmware-only recovery.
  Unknown/layout/security changes stop for separate validation; no data migration
  mechanism is part of this work. Firmware rollback cannot promise data compatibility.

## Flashing without a recovery point

Since 2026-10-25 (owner decision) a flash does not require an LKG or a backup. A
flash that names neither `lkg_receipt` nor `backup_job_id` uses
`recovery_kind: "none"`: no LKG/backup lookup, no recovery-bytes check, no
pre-write fence and no previous-layout match; data compatibility is not checked.
The device identity comes from the pre-write probe. Write, full readback, boot
ELF/IDF, confirmed close, final authority and the atomic LKG commit are unchanged,
so the flashed build becomes the device's LKG with the flash job id as its
`baseline`. A pre-write recovery fence applies only when a baseline is named
explicitly. Restore and the initial full backup are unchanged.

Implemented: partition fences, private hash objects, atomic successful-job selection,
frozen recovery targets and the flash/restore vertical path. Remaining: expanded
failure/retention regression and terminal CI/local review.
