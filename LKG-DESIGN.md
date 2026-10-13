# Last-known-good firmware: implementation in progress

This draft does not yet change the live flash/restore contract.

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
- Freeze recovery receipt/hash at prepare time, including an implicit choice of
  the current receipt. Execute and approve that exact target, never dynamic latest.
- A successful explicit restore makes its verified target current. Build time
  does not choose the current version. Restoration is never executed implicitly.
- Prioritize the same partition/security layout and a single factory app. Do not
  initialize or overwrite NVS, filesystem or otadata in firmware-only recovery.
  Unknown/layout/security changes stop for separate validation; no data migration
  mechanism is part of this work. Firmware rollback cannot promise data compatibility.

Completed: independent synthetic single-factory partition validation.
Pending: persistent hash objects/pins, successful-job transaction, flash/restore
integration, promotion/cancel/crash/retention regressions, tool/docs updates and CI.
