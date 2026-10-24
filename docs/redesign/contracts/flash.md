# Firmware safety contract inventory — flash / LKG / restore / backup

Source: `main` @ 5d79d2d (0.13.0). Read-only inventory; no code was run. Paths are relative to
`src/cardputer_mcp/` unless they start with `tests/` or are a doc name. "SPEC:" marks speculation.
Test coverage was mapped by grepping test files for failure-code fragments and test names, so a
gap marked "no test found" can still be covered indirectly by a broader test.

---

## 0. Layering (who calls whom)

```
surface.py  submit(kind=backup|flash|restore) ─► broker.prepare ─► FirmwareFlash.prepare
surface.py  flash(plan_hash)  [flash/restore]  ─┐
surface.py  execute(op=run)   [backup]         ─┴► broker.execute (enqueue gate)
broker worker loop (dispatch gate) ─► flash_live closure ─► FirmwareFlash.execute (loop)
   └─ asyncio.to_thread(FirmwareFlash._execute) ─► FlashLifecycle.run ─► NativeFlashDriver ─► flash_worker.py (subprocess, esptool 5.1.0)
broker (loop, no await) ─► FirmwareFlash.commit_success ─► Store.finish_firmware (single SQLite txn)
```

The write call must not change: `flash_lifecycle.py:294` `self.driver.write(segments)`.

---

## 1. Flash sequence (operation = "flash")

### 1a. Prepare (submit). Nothing touches the device. The plan is frozen here.
| # | Gate | file:line |
|---|---|---|
| P1 | surface rules; `backup_job_id` and `lkg_receipt` are mutually exclusive (`argument_conflict`) | surface.py:601-609 |
| P2 | Auth is required for FLASH_TESTS (`authentication_required`); `_execution_uncertain` → `recovery_review_required` | broker.py:1686-1704 |
| P3 | `FirmwareFlash.prepare` is called | broker.py:1717-1720 |
| P4 | `_registration`: device registered, `allow_<op>` is True; for the real driver: serial backend, selector == settings.serial.selector, python sha256, esptool tree fingerprint. **The tool-hash checks are skipped when `driver_factory` is injected (all tests).** | firmware_flash.py:227-246 |
| P5 | principal is required; USB fingerprint comes from `backend.prepare(RESTART)` (or a mock hash) | firmware_flash.py:341-346 |
| P6 | Recovery input A, `backup_job_id`: `_backup` needs a succeeded `firmware_backup` job of the same principal; `BackupStore.load` re-hashes the bytes; the backup's device fingerprint and capacity must match | firmware_flash.py:248-262, 353-360 |
| P6' | Recovery input B, LKG: `_known` uses an explicit `lkg_receipt` (history row) or the **current pointer, frozen into `lkg_receipt`**. The snapshot is loaded and its metadata must match device_id, principal, selector_sha256, flash_bytes and kind | firmware_flash.py:270-289, 361-373 |
| P7 | Build: `_build_context` (succeeded, owned, `firmware_build`), project == registration.project_id, `verify_bundle`, manifest export digest, app BIN + ELF receipts present, ELF file readable | firmware_flash.py:319-336, 374-393 |
| P8 | `OperationPlan` digest (sha256 of `json.dumps(asdict)`) → `fingerprint` | firmware_flash.py:407-421; flash_lifecycle.py:93-110 |
| P9 | Plan dict with fixed `FLASH_EFFECTS` / `FLASH_STAGES` / `argv_template` | firmware_flash.py:422-445, 43-88 |
| P10 | `validate_flash_plan`: exact key set, effects/stages/argv equality, hex formats, digest == fingerprint | firmware_flash.py:130-191 |

### 1b. Enqueue (MCP `flash` tool)
| # | Gate | file:line |
|---|---|---|
| E1 | `wrong_tool`: a flash/restore plan only via `flash`, others only via `execute`. **Enforced only `if auth_required`.** | surface.py:427-439, 665-668, 634-637 |
| E2 | Dedup by job_id/plan_hash; `_execution_uncertain`; capacity; history size | broker.py:1840-1850 |
| E3 | `PlanApprovals.validate_current` | broker.py:1856 |
| E4 | `prepare_binding` (re-runs `prepare_plan`, requires plan equality, re-checks the build job/plan/principal/project, diagnostic selection, source provenance + fetch job, then `live_binding`) | broker.py:1857-1861; flash_binding.py:30-104 |
| E5 | principal_valid(read, control) | broker.py:1862-1873 |
| E6 | **Enqueue final gate (duplicate #1)**. No callbacks after it. | broker.py:1874-1891 |
| E7 | `enqueue_requested`, then the intent/runtime/flash bindings are stored | broker.py:1892-1899 |

### 1c. Dispatch (broker worker loop)
| # | Gate | file:line |
|---|---|---|
| D1 | lease principal still valid, `session is lease`, deadline | broker.py:2371-2385 |
| D2 | `validate_binding` (= `prepare_binding(...) == binding and live_binding`) | broker.py:2391-2393; flash_binding.py:148-153 |
| D3 | principal re-check | broker.py:2404-2417 |
| D4 | **Dispatch final gate (duplicate #2)** | broker.py:2418-2461 |
| D5 | `store.set_job(job_id, "running")`; `active_job = job_id` | broker.py:2467-2469 |
| D6 | `capture_principal == plan.flash_principal` | broker.py:2546-2547 |
| D7 | **`flash_live` closure (duplicate #3)**. It is the `live` callback for the whole IO path and, with `committing=True`, for the commit. | broker.py:2549-2603 |
| D8 | `await firmware_flash.execute(plan, on_stage, flash_live)` | broker.py:2605 |

### 1d. `FirmwareFlash.execute` (event loop) → `_execute` (thread)
| # | Gate | file:line |
|---|---|---|
| X1 | `validate_flash_plan`; `prepare_plan` again with **full equality** (`flash_inputs_changed_needs_review`) | firmware_flash.py:473-476, 447-468 |
| X2 | `_registration` again | firmware_flash.py:477 |
| X3 | `current = firmware_current(...)` gives the baseline. **This is the only dynamic read of "current" at execute time**, and it is used only for the baseline and a stricter layout check. | firmware_flash.py:485-486, 524-529 |
| X4 | Load the frozen LKG snapshot (`_known` with plan.lkg_receipt) → `recovery` tuple, or the backup bytes via metadata.backup_job_id | firmware_flash.py:487-504 |
| X5 | Build: `validate_plan_export`, `verify_bundle`, image bytes, snapshot_files (segments + layout + security + full ELF) | firmware_flash.py:505-522 |
| X6 | Layout fence: `compatibility()` (exactly bootloader/table/app, single factory app, ranges). Then `same_layout` against the current or known compat, or else `baseline_layout` (backup table bytes == new table) | firmware_flash.py:523-536; flash_lkg_bundle.py:13-70; flash_lkg_geometry.py:17-72 |
| X7 | Build the new artifact_bundle metadata (bytes are not written yet) | firmware_flash.py:537-551 |
| X8 | Thread hop: `worker_live` marshals `live()` to the loop with a 5 s timeout. A task cancel → `request_stop` + shield loop → raises `flash_cancelled_needs_review` even if the worker succeeded | firmware_flash.py:570-595 |
| X9 | `_execute`: validate again; `_cancelled_jobs` / live; busy/uncertain; driver constructed; **a new `FlashLifecycle` is created only after the busy/uncertain check and under the lock, with a cancellation re-check** | firmware_flash.py:597-622 |

### 1e. `FlashLifecycle.run` (flash_lifecycle.py:168-365). This is the core sequence.
| # | Step | line |
|---|---|---|
| L1 | Under the lock: engine/driver active or uncertain → `flash_device_busy_or_uncertain` | 183-185 |
| L2 | `requested_digest == plan.digest` and `reader_closed()` (backend not active/connected/uncertain) → else `flash_plan_or_handoff_required` | 186-187; reader_closed lambda firmware_flash.py:655-659 |
| L3 | flash requires a 64-hex `expected_elf_sha256` | 188-193 |
| L4 | `active = True`, `_committed = None` | 194-195 |
| L5 | `_live` → stage `flash_security_probe` → `driver.probe()` → `_live` → `SecurityReceipt.verify(expected_device, capacity)` (esp32s3, capacity, device hash == plan, SB/FE/SD all exactly False) | 198-202, 79-90 |
| L6 | non-backup needs `expected_device_sha256` | 217-221 |
| L7 | Recovery-bytes integrity: artifact_bundle → every LKG segment's size/sha must match (`lkg_bytes_changed`); full_backup → backup len/sha == plan.backup_sha256 | 222-236 |
| L8 | bundle/images match plan.manifest_sha256 and the per-segment size/sha (`flash_manifest_changed_needs_review`) | 237-248 |
| L9 | stage `flash_backup_prewrite_check`. **The current-firmware fence:** with LKG, every prior segment read back must equal the retained bytes (`lkg_current_firmware_changed_needs_review`). With a backup, every new-segment range rounded up to 4 KiB must equal the backup (`flash_backup_stale_needs_review`). `_live` runs before each read. | 249-267 |
| L10 | `segments` = (offset, image) for the bundle only | 268-271 |
| L11 | `_live` → stage `flash_write` → `_live` → **`driver.write(segments)`** → `_live` | 291-295 |
| L12 | stage `flash_readback_verify`: per segment `_live`, `read(offset,len)`, sha256 equality (`flash_readback_mismatch_needs_review`) | 296-301 |
| L13 | stage `flash_boot_observe`: `_live` → `driver.boot()` → `_live`; boot_observed, idf 6.0.1, target, 8-16 hex ELF prefix that is a prefix of the expected ELF (`flash_boot_unverified_needs_review`) | 302-322 |
| L14 | Bounded summary only (no raw output) | 323-341 |
| L15 | `_live` | 342 |
| L16 | Exception mapping: FlashFailure passes through; TimeoutError → `flash_timeout_needs_review`; others → `flash_failed_needs_review` | 343-348 |
| L17 | **Confirmed close**: `driver.close()`; driver still active/uncertain → `self.uncertain = True` and `flash_shutdown_uncertain_needs_review`, which replaces any earlier error. `active=False` only if not uncertain. | 349-358 |
| L18 | Post-close `_live`; cancelled under the lock → `flash_cancelled_needs_review`; `defer_commit=True`, so `_committed` is **not** set here | 359-365 |

### 1f. Staging (worker thread, after run)
| # | Step | firmware_flash.py |
|---|---|---|
| S1 | engine cancelled or `live()` false → `lkg_promotion_cancelled_needs_review` | 691-692 |
| S2 | `FirmwareSnapshots.stage(metadata, snapshot_files)` writes immutable CAS objects. **There is no pointer update.** | 693-697 |
| S3 | `self.promotions[job] = {device_id, principal, receipt, kind, baseline}`; `self.results[job] = summary` (RAM only) | 698-705 |
| S4 | The finally block retains the boot diagnostic (schema_version 1) in the principal-scoped RAM log | 726-765 |

### 1g. Atomic commit (event loop; **no `await` between the worker result and the commit**)
| # | Step | file:line |
|---|---|---|
| C1 | Success branch → `commit_success(plan, completed, partial(flash_live, committing=True))` | broker.py:2799-2821 |
| C2 | Guard = `live()` and engine exists and not cancelled and `not active` and `not uncertain` | firmware_flash.py:291-299 |
| C3 | `Store.finish_firmware`: | store.py:614-687 |
| | `BEGIN IMMEDIATE` | 625 |
| | job row must be `running` **and guard() True** (guard #1, inside the txn; `_running_job_binding_live(committing=True)` additionally requires `store.db.in_transaction`) | 626-630; broker.py:497 |
| | `UPDATE jobs SET state='succeeded', result=?` | 631-634 |
| | `INSERT INTO firmware_job_summaries` (≤4096 B, principal-scoped; flash/restore only) | 635-642 |
| | if promotion: `UPDATE device_writes SET seq=seq+1` (device write fence) | 643-645 |
| | `INSERT OR IGNORE INTO firmware_lkg_history (receipt, device_id, principal, kind, baseline, completed_job)` (keeps the *first* completed_job for a receipt) | 646-656 |
| | `INSERT INTO firmware_lkg ... ON CONFLICT(device_id) DO UPDATE` (current pointer) | 657-670 |
| | guard() again (guard #2; now the job row is `succeeded` with the exact result, which `_running_job_binding_live` accepts only while committing) | 671-672; broker.py:500-508 |
| | re-read the job row: state/plan_hash/result | 673-682 |
| | `COMMIT`; `except BaseException: rollback; raise` | 683-687 |
| C4 | Only after the commit: `engine._committed = job_id` (later `request_stop` returns False) and `results[...]["last_known_good_receipt"]` | firmware_flash.py:312-317; flash_lifecycle.py:154-160 |
| C5 | Commit failure → `execution_uncertain = True` (a latch), job `needs_review`/`firmware_final_commit_failed` | broker.py:2866-2885 |
| F1 | Any FlashFailure → `needs_review` + bounded read diagnostic; `_cancel_queued` | broker.py:2720-2730 |
| F2 | `active_job` is cleared only when nothing is active or uncertain (ownership is kept on an uncertain close) | broker.py:2896-2905 |
| F3 | Crash: `Store.recover` turns `running` into `needs_review` (`broker_restart_no_replay`). The pointer is untouched because only C3 writes it. | store.py:178-193 |

**The single SQLite write that makes LKG current is C3.** The CAS objects (S2) and backup files are
written earlier and are inert until C3 commits. A cancel between L18 and C3 sets `engine.cancelled`, so the guard
refuses and the rollback keeps the prior pointer.

### 1h. Per-native-call gates (driver/worker)
- `NativeFlashDriver._verified_port` before **every** probe/read/write/boot: authority/stop, `os.name=='nt'`,
  serial backend, port fingerprint == plan USB fingerprint, selector match, python not a symlink and sha256 match,
  esptool fingerprint match (flash_driver.py:209-231). Handoff check: flash_driver.py:243-244.
- Read monitor (authority, stall, deadline) during reads only (flash_driver.py:294-299, 330). **Write has no
  monitor; it runs with a fixed 300 s timeout** (flash_driver.py:241, 329-330).
- Temp work dir `backup_directory/work/flash-op-*` is removed only if the process is neither active nor uncertain (flash_driver.py:403-406).
- Worker: esptool `__version__ == "5.1.0"`, action allowlist, `COMn` port, capacity set, range checks
  (flash_worker.py:85-115). `detect_chip(default-reset, connect_attempts=1)`, ESP32-S3 and not stub (119-121).
  **`WRITE_FLASH_ATTEMPTS = 1`, `WRITE_BLOCK_ATTEMPTS = 1` (no write retry)** (124-125). Security flags and
  flash_crypt_cnt parity (126-137). Flash ID capacity (138-141). Device id = sha256(`esp32s3\0` + MAC); must equal
  the expected value (142-145). Write requires an expected device (216-217); ordered, 4 KiB-aligned, non-overlapping
  segments with per-file sha (222-251). Offset 0 must be an ESP32-S3 image (magic 0xE9, chip id 9) (244-249).
  `write_flash(keep, keep, keep, erase_all=False, encrypt=False, force=False, compress=False)` (254-264).
  Port close must be confirmed (266-270).

---

## 2. Backup and restore sequences

### 2a. Backup (operation = "backup"; queued via `execute`, not `flash`)
1. Prepare: no recovery or build inputs; `expected_device_sha256 = None` (firmware_flash.py:347-352). A backup cannot be
   deferred with `after` (surface; tests/test_deferred_device.py::test_session_and_after_are_mutually_exclusive_and_backup_cannot_be_deferred).
2. Same enqueue/dispatch/flash_live gates as §1b-1c.
3. Lifecycle: L1-L5, then `read(0, flash_bytes)` in full, length check, `BackupReceipt(device_sha256, fingerprint,
   flash_bytes, sha256)` (flash_lifecycle.py:203-215). Then close (L17).
4. `_execute`: `BackupStore.save` writes `<job>.bin` + `<job>.json` with O_EXCL, 0600 and fsync, **before** the commit
   (firmware_flash.py:670-674; flash_backup.py:51-94). No promotion (op == backup) (675).
5. Commit: `finish_firmware(..., promotion=None, summary=None)`: the job succeeds, nothing else changes and `device_writes` is
   not bumped (firmware_flash.py:304-311).
6. Inertness: the backup is usable only if its job is `succeeded`, test == `firmware_backup` and the principal matches
   (firmware_flash.py:248-258). `BackupStore.load` checks owner, nlink == 1, size, inode identity, mtime and sha (flash_backup.py:96-140).
   Note: the backup summary (`backup_receipt`) is RAM-only, not persisted (firmware_flash.py:309, 672).
7. Read deadline: `max(300, 60 + ceil(bytes*600/MiB))`, 60 s stall (flash_read_progress.py; FIRMWARE-FLASH.md:46-71).

### 2b. Restore (operation = "restore"; queued via `flash`)
Prepare selects one of three recovery kinds, and the choice is frozen into the plan:
- **R-full via `backup_job_id`**: recovery_kind `full_backup`, `backup_sha256` = backup hash,
  `expected_app_bin_sha256` = `backend.config.expected_firmware_sha256` ("0"*64 for mock),
  `expected_elf_sha256` = `registration.original_elf_sha256` (both required) (firmware_flash.py:353-360, 398-406).
- **R-LKG (implicit current or explicit `lkg_receipt`)**: `lkg_receipt` = the history row's receipt,
  `recovery_kind` = row.kind, `backup_sha256` = **the receipt itself** for artifact_bundle, or metadata.backup_sha256
  for full_backup. ELF/app digests come from the snapshot metadata (firmware_flash.py:361-373, 394-397).
- Receipt binding: `lkg_receipt`, `backup_sha256` and `recovery_kind` are part of the plan and the `OperationPlan` digest.
  `prepare_plan` reuses `plan["lkg_receipt"]` (firmware_flash.py:457) and execute requires full equality (474-476). The CAS
  receipt is the sha256 of a canonical envelope that lists the sha256 of every file, so receipt → bytes is fixed.

Execute and lifecycle:
1. X1-X4. artifact_bundle → `recovery = (bundle_from_record(metadata.bundle), files)` and
   `recovery_table_offset = metadata.compat.table_offset` (firmware_flash.py:493-498, 666-668). full_backup LKG → backup bytes
   are loaded via metadata.backup_job_id (499-502). R-full → backup bytes (503-504) and full_backup metadata (552-567).
2. Lifecycle: L1-L7. Then:
   - artifact_bundle: **only the partition table** is compared with the live device
     (`lkg_restore_layout_evidence_required` / `lkg_restore_partition_changed_needs_review`). The write covers all
     LKG segments (bootloader, table, app) (flash_lifecycle.py:272-287).
   - full_backup: `segments = ((0, backup),)`, which rewrites **the whole flash including NVS/data**. There is no pre-write compare
     (288-290) and the plan effects disclose this (firmware_flash.py:54).
   - L11 write, L12 readback (full flash for full_backup), L13 boot with the expected ELF.
3. Staging: restore with `lkg_receipt` → selected = `plan["lkg_receipt"]` (no new objects). R-full →
   `stage(full_backup metadata, {})` creates an envelope-only receipt (firmware_flash.py:693-697; flash_lkg_store.py:102-109).
4. C3 commit makes the **restored target current** (history row INSERT OR IGNORE keeps the original completed_job).

### 2c. Home recovery (DERBY exit; not a firmware write)
`home_recovery.py`: one `q`, a fresh `APP_STOPPED`→`HOME_READY`, confirmed close. The source attempt is consumed atomically
before IO (store.py:264-314). The final commit runs under BEGIN IMMEDIATE with a guard plus `matches()` twice (store.py:316-366).
It changes no flash/LKG/backup state (HOME-RECOVERY.md:59-63). The `_state` check refuses when `firmware_flash.active` or
`.uncertain` (home_recovery.py:143-144). Serial open/close may reset; there is no reopen or retry (home_recovery.py:408-474).

---

## 3. The three near-duplicate pre-write authority checks (broker.py)

The duplicates are (#1) the enqueue final gate **broker.py:1875-1891**, (#2) the dispatch final gate
**broker.py:2422-2451**, and (#3) `flash_live` **broker.py:2549-2603**. #3 is the only one evaluated between device
side effects, including immediately before and after `driver.write`. `running_authorized` (broker.py:2495-2529) is
#3's non-flash sibling. It is shown for comparison but is not used for flash.

| Condition | #1 enqueue | #2 dispatch | #3 flash_live | running_authorized |
|---|---|---|---|---|
| principal_valid(read,control) is True | 1864-1869 | 2404-2417 | 2583-2588 | 2505-2513 |
| principal_valid object unchanged | 1876 | 2428 | 2591 | 2516 |
| runtime binding == captured | 1877 (vs 1855) | 2429 (vs enqueue) | 2592-2593 (fresh *and* enqueue) | 2517 |
| flash `live_binding` | 1878 | 2430 (+ `validate_binding` 2391) | 2594-2595 (binding must be non-None) | — |
| `collector_live` | 1879 | 2431 | **absent** | 2518 |
| `session is lease` | 1880 | 2438 | 2558-2563, 2596 | 2519 |
| lease principal == capture principal | via intent | via intent | 2564 explicit | via lease binding |
| intent | `_execution_intent_binding_live` 1881 | `_execution_intent_live` (job awaiting/queued) 2448-2449 | `_execution_lease_binding_live` 2597 + `_running_job_binding_live` 2600-2602 | same, without committing |
| closing | 1882 | 2443 | 2560 | via lease binding (425) |
| `_execution_uncertain()` | 1883 | 2444 | via `_running_job_binding_live` with `allow_profile_cleanup=True` (**weaker**) | same |
| `_physical_busy()` / `hardware_connected` | — | 2445-2446 | — (it is the running job) | — |
| capacity / history size | 1884-1885 | — | — | — |
| job row | must not exist 1886 | `queued`, same plan_hash 2433-2434 | `running`, or `succeeded`+exact result when committing (broker.py:500-508) | `running` |
| stored plan == plan, digest | 1887 | 2435-2436 | via live_binding + running binding | 2524-2525 |
| broker epoch | via intent | 2437 | via lease binding (427) | same |
| lease deadline finite / not passed | via intent | 2439-2442 | 2565-2566 + lease binding | lease binding |
| device+state locks held | 1888-1889 | 2432, 2447 | 2577-2578, 2598-2599 | 2521-2522 |
| registration exists, `allow_<op>`, `registration_sha256 ∈ flash_digests` | (via prepare_binding) | (via validate_binding) | **2569-2576, only here** | — |
| `store.db.in_transaction` when committing | — | — | broker.py:497 | — |

Second near-duplicate triple (registration and tool integrity), if that was the one meant:
`FirmwareFlash._registration` (firmware_flash.py:227-246), `NativeFlashDriver._verified_port`
(flash_driver.py:213-231) and the registration block in `flash_live` (broker.py:2569-2576). How they differ:
`_registration` skips the hashes when a test driver is injected. `_verified_port` adds port fingerprint, `os.name`,
backend name, the python-symlink check and the authority/stop checks, and it runs only with the real driver. `flash_live`
checks only allow/digest and does no tool hashing.

There is a third, data-integrity repetition: the segment size/sha checks in lifecycle L7/L8, `verify_bundle`, and the
worker's per-file sha (flash_worker.py:238-243).

---

## 4. Persisted artifacts

### 4a. Filesystem (under `registration.backup_directory`, private: 0700 / Windows ACL confirmation required; flash_backup.py:31-44)
| Path | Content | Writer / reader |
|---|---|---|
| `<job_id>.bin` | full plaintext flash | flash_backup.py:69-84 (O_CREAT\|O_EXCL\|O_NOFOLLOW, 0600, fsync) |
| `<job_id>.json` | `RetainedBackup{job_id, principal, device_sha256, device_fingerprint, flash_bytes, sha256}`. No schema_version; strict `**json` constructor | flash_backup.py:14-21, 85-90, 105 |
| `lkg/<sha256>` | CAS object (bytes or envelope) | flash_lkg_store.py:47-99 |
| `lkg/<sha256>.pending` | staging file; published with `MoveFileExW(WRITE_THROUGH)` and no REPLACE on Windows, or rename + directory fsync on POSIX; an existing object is verified byte-equal, never replaced | flash_lkg_store.py:28-40, 55-75 |
| `work/flash-op-*/` | `job.json`, `segment-N.bin`, `read.bin`. Transient; left in place if the process is uncertain | flash_driver.py:245-272, 403-406 |

Envelope (`stage`): `{"schema_version":1,"metadata":{...},"files":{name: sha256}}`, canonical JSON
(`sort_keys`, `(",",":")`). Receipt = sha256(envelope). Limits: envelope ≤ 1 MiB, objects ≤ 64 MiB,
total ≤ 100 MiB. Empty files are allowed only for `full_backup` (flash_lkg_store.py:102-123).
Metadata keys:
- artifact_bundle (firmware_flash.py:537-551): kind, device_id, principal, expected_device_sha256, flash_bytes,
  selector_sha256, expected_elf_sha256, expected_app_bin_sha256, bundle (asdict FlashBundle), manifest (full), compat.
  Files: 3 segment BINs, flash_layout receipt, flash_security receipt, full ELF.
- full_backup (firmware_flash.py:552-567): the same identity keys + backup_sha256, backup_job_id, compat=None.
  A fallback copy at 676-690 **lacks `compat`**. That code is unreachable in practice, but readers use `.get("compat")`.

### 4b. SQLite (`history.sqlite3`, WAL, synchronous=FULL; store.py:51-147)
- `firmware_lkg(device_id PK, principal, receipt, kind, baseline, completed_job)`: the current pointer. **The PK is
  device_id only.** Reads filter by principal (store.py:600-605), so a different principal's flash replaces the
  row and the first principal then sees no current LKG.
- `firmware_lkg_history(receipt PK, device_id, principal, kind, baseline, completed_job)`: INSERT OR IGNORE.
- `firmware_job_summaries(job_id PK, principal, payload ≤4096)`: survives log expiry and restart (FIRMWARE-FLASH.md:109-117).
- `device_writes(id=1, seq)`: bumped only on promotion (store.py:136-139, 645, 689-690).
- `build_artifact_references(job_id, principal, payload ≤4096)` (build_artifacts.py:64-101).
- `jobs`, `plans` (canonical JSON plan, sha256 = plan_hash).

### 4c. Schema-version variants and readers
| Record | Versions | Writer | Readers |
|---|---|---|---|
| LKG envelope | 1 only (strict) | flash_lkg_store.py:108 | flash_lkg_store.py:113-123 |
| Build manifest | 1 | firmware_build.py:988 | flash_manifest.py:84-85; build_locations.py:286, 299-300; manifest_export.py:33-35 |
| Manifest export | 1 | manifest_export.py:111 | manifest_export.py:89-91, 150 |
| Source snapshot receipt | **1 or 2** (2 = submodules) | source_snapshot.py:1050 | source_snapshot.py:1163-1174; source_payloads.py:67; firmware_build.py:398-400 (dependency_policy); flash_binding.py:77-82 (**the v2 reader in the flash binding**); registration_digest.py:55-65 (v2 excludes the legacy digest shape) |
| Flash plan | `version: 1`; optional `manifest_export_sha256` (older plans without it are honoured and never supplemented) | firmware_flash.py:141-149, 462-467 | validate_flash_plan |
| Deferred device plan | `version: 2`, `execution_context="device_deferred"` | broker.py:912-923 | broker.py:936-949 |
| Boot diagnostic (RAM log) | 1 | firmware_flash.py:751-758 | logs |
| Registration digest | `flash_digests` = {location-excluded, full}. The plan stores `flash_digest`; `flash_live` accepts both | registration_digest.py:92-97 | broker.py:2574 |

### 4d. Retention and protection
- **No deletion code exists for backups or LKG objects.** The only unlink is the `.pending` cleanup (flash_lkg_store.py:75). They live under
  `backup_directory`, separate from build/state roots (FIRMWARE-FLASH.md:75-81).
- Build outputs: no pruning code. Re-building into the same fingerprint slot can supersede bytes
  (FIRMWARE-BUILD.md:446-449). That is why a flash copies bytes into the CAS at S2 and restore never reads build dirs.
  RAM caches hold 8 builds (build_artifacts.py:99-101) and 8 flash logs/results (firmware_flash.py:631-635).
- Source payload reclamation (source_payloads.py:175, 218) is gated by source_retention.py: **current LKG**
  protects its build's `source_receipt` (`current_lkg_evidence`). History LKG is provenance only (the payload is reclaimable,
  the receipt is kept). Executable or in-flight flash plans protect their build's source. Malformed lineage disables all
  reclamation (source_retention.py:137-175).
- `discard_deferred` deletes only the pending manifest, never artifacts (firmware_build.py:1078-1097).

---

## 5. Reset/reopen behaviour that must stay disclosed
Disclosure strings live in `FLASH_EFFECTS` (firmware_flash.py:51-88) and are **validated by exact equality**
(firmware_flash.py:162). Changing the text invalidates every stored plan.
- Every native probe/read/write is a separate esptool process using `connect_mode="default-reset"`, `connect_attempts=1`
  (flash_worker.py:119). Each can reset the device into ROM download mode. One flash therefore does probe, N pre-write
  reads, the write and N readback reads (N = 3 for the LKG path), each with its own connect/reset. (SPEC: inside one attempt, esptool's
  default-reset may still cycle several reset strategies. FIRMWARE-FLASH.md:18 says "bounded fallback" for initial chip detection.)
- Flash/restore completion: one normal-boot RTS pulse of 100 ms (DTR cleared, RTS asserted, then cleared)
  (serial_backend.py:1024-1038). Passive observation `FIRMWARE_OBSERVE_SECONDS` (30 s per the docs) (1023). On
  disconnect, **at most one** same-identity reopen within 8 s after a confirmed close, with no second pulse (1039, 1076-1118). A failed open
  still consumes the attempt (1101). A reopen marks a capture gap (1126).
- `GuardedObservationPort`: authority is checked before open/read/write/reset_input_buffer/asserting DTR or RTS.
  Deasserting is allowed after revocation (flash_driver.py:129-160).
- Uncertain close latches `ownership_uncertain` and is terminal (serial_backend.py:944-955).
- Backup: ROM resets only, no normal-boot reset (firmware_flash.py:75).
- No HELLO keys are sent during flash verification (FIRMWARE-FLASH.md:34-37; tests/test_flash_driver.py).
- Home recovery: serial open/close can reset; no reopen (HOME-RECOVERY.md:22-26).

---

## 6. Tests pinning each step (file::name). ✗ = no test found.

| Step | Tests |
|---|---|
| P4 registration disabled by default / allow gate | tests/test_flash_broker.py::test_registration_default_disables_flash_and_restore; tests/test_flash_policy.py::test_allow_flash_still_gates_operation; ::test_registration_is_explicit_strict_and_flash_only |
| P4 tool-hash changed (`registered_flash_tools_changed`) | ✗ (skipped with injected drivers) |
| P6 backup ownership / device mismatch | tests/test_flash_backup.py::test_private_receipt_roundtrip_and_principal_binding, ::test_tampering_fails_closed, ::test_unknown_backup_never_restores, ::test_remote_path_and_existing_backup_cannot_overwrite; `backup_registered_device_mismatch` ✗ |
| P6' LKG binding/kind changed | ✗ |
| P7 build provenance | tests/test_flash_policy.py::test_successful_owned_source_provenance_is_required; tests/test_flash_binding.py::* |
| E1 wrong tool for flash plans | ✗ (tests/test_chain_http.py::test_rules_for_after_and_wrong_tool tests only `after` rules) |
| E4-E6, D2-D4 binding/authority | tests/test_flash_binding.py::test_callback_free_binding_rejects_changed_identity, ::test_dispatch_rehashes_source_and_artifacts, ::test_authentication_callback_cannot_change_registered_target; tests/test_flash_policy.py::test_queued_authorization_is_rechecked_before_device_writes, ::test_revocation_in_last_auth_callback_cannot_start_device, ::test_last_auth_callback_cannot_replace_owned_fetch_receipt[dispatch/prewrite/commit]; tests/test_current_state_admission.py::test_current_uncertainty_after_preparation_or_enqueue_blocks_io |
| D7 running binding / lease | tests/test_flash_running_binding_review.py::test_running_flash_rechecks_job_identity_after_authentication_callback; tests/test_flash_lease_renewal.py::* |
| X1 `flash_inputs_changed_needs_review` | ✗ for flash specifically |
| X6 layout fences | tests/test_flash_lkg_geometry.py::test_factory_bounds_leave_nvs_outside_firmware, ::test_unknown_ota_encryption_and_bad_checksum_fail_closed; `same_layout` / `baseline_layout` rejections ✗ |
| L1 busy/uncertain | tests/test_flash_lifecycle.py::test_uncertain_shutdown_retains_busy_and_blocks_reuse |
| L2 digest/handoff | ::test_exact_request_digest_and_reader_closed_are_required; ::test_restore_requires_its_own_exact_plan_digest |
| L3 ELF required | ::test_elf_prefix_is_separate_from_bin_hash_and_required_for_flash |
| L5 security / device swap | ::test_security_flags_unknown_or_enabled_block_backup_and_flash, ::test_device_swap_blocks_write; tests/test_flash_worker.py::test_unknown_protected_or_encrypted_device_blocks_write, ::test_mismatched_device_and_non_s3_boot_image_never_write; tests/test_flash_security.py::* |
| L7 backup tampered | ::test_missing_or_tampered_backup_never_writes; `lkg_bytes_changed` ✗ |
| L8 manifest changed (lifecycle) | ✗ (manifest-level: tests/test_flash_manifest_contract.py::test_changed_binary_and_layout_are_rejected) |
| L9 pre-write fence (backup path) | ::test_changed_erase_sector_never_writes |
| L9 pre-write fence (LKG path, `lkg_current_firmware_changed`) | ✗ (tests/test_flash_http.py only asserts that a full read is avoided) |
| restore table fence | ::test_lkg_restore_rejects_changed_live_table_before_any_write; `lkg_restore_layout_evidence_required` ✗ |
| L11 write is one attempt / safe options | tests/test_flash_worker.py::test_write_uses_attested_bytes_and_never_dangerous_options |
| L12 readback mismatch | ✗ |
| L13 boot verification | ::test_bad_boot_requires_review_without_automatic_restore; tests/test_flash_driver.py::test_actual_restart_passively_verifies_boot_without_hello_commands, ::test_wrong_or_missing_boot_evidence_never_sends_hello_keys; tests/test_flash_boot_evidence.py::test_terminal_boot_failure_retains_owned_redacted_evidence |
| cancel / authority at stages | ::test_cancel_at_each_stage_requires_review_and_closes, ::test_revoked_authority_blocks_write, ::test_timeout_requires_review_without_native_error_details; tests/test_flash_process_monitor.py::*; tests/test_flash_read_driver.py::* |
| L17 confirmed close | ::test_uncertain_shutdown_retains_busy_and_blocks_reuse; tests/test_flash_driver.py::test_partial_transport_failure_and_failed_close_keep_primary_evidence |
| S1 `lkg_promotion_cancelled` | ✗ |
| C3 atomic commit / rollback | tests/test_flash_lkg_store.py::test_crash_before_commit_preserves_previous_successful_selection, ::test_failed_final_authority_check_keeps_old_pointer_and_running_job; tests/test_flash_policy.py::test_failed_or_late_revoked_flash_preserves_lkg_and_current_guards, ::test_second_atomic_commit_guard_rechecks_registration_and_rolls_back; tests/test_flash_running_binding_review.py::test_final_authentication_callback_cannot_commit_detached_flash_result; tests/test_flash_lease_renewal.py::test_renewal_preserves_authority_at_both_commit_guards |
| C3 `device_writes` not bumped on rollback | ✗ (not asserted) |
| summary persistence | tests/test_flash_lkg_store.py::test_job_summary_survives_restart_and_is_principal_scoped |
| CAS store | ::test_competing_pending_object_is_preserved, ::test_disk_full_does_not_publish_or_replace_verified_objects, ::test_missing_immutable_bytes_are_rejected; `lkg_object_changed` / `lkg_publication_failed` ✗ |
| E2E backup→flash→restore→LKG→restore-bundle→cancel | tests/test_flash_http.py::test_explicit_mcp_backup_flash_restore_and_cancel |
| backup / restore separation | tests/test_flash_policy.py::test_backup_restore_and_full_backup_require_separate_explicit_request; ::test_preparation_never_executes_and_no_policy_grant_is_needed; ::test_exact_owned_source_receipt_auto_flash_commits_lkg_once; tests/test_flash_broker.py::test_backup_executes_explicit_authenticated_request_without_local_approval |
| backup persistence failure (`backup_persistence_failed`) | ✗ |
| device write fence | tests/test_deferred_device.py::test_device_write_between_parent_and_child_is_refused, ::test_unchanged_device_lets_the_continuation_run_even_after_hold_expiry |
| collector requires current LKG | tests/test_collector_binding.py (parametrized `current`/`readback`) |
| retention protection | tests/test_source_history_retention.py::* (6 tests); tests/test_flash_http.py's "build retention" step only clears RAM `_records` (**weak**: no build-dir deletion or overwrite is simulated) |
| reset/reopen | tests/test_flash_driver.py::test_stop_before_reset_controls_never_opens, ::test_cancel_during_pulse_allows_deassertion_and_close, ::test_stop_during_disconnect_never_reopens; tests/test_serial_restart.py (reopen-once, second-disconnect, uncertain close …); tests/test_restart_observation_integration.py::* |
| worker ranges / port | tests/test_flash_worker.py::test_rom_limit_rejects_32mb_before_device_open, ::test_rom_crossing_read_and_write_rejected_before_device_open, ::test_network_or_arbitrary_port_and_command_are_rejected |
| home recovery | tests/test_home_recovery.py (18 tests); tests/test_home_recovery_http.py::test_closed_derby_recovery_requires_same_authenticated_requester |

Untested or unenforced invariants that need attention:
1. `expected_app_bin_sha256` is plan-bound but **never checked at runtime**. `NativeFlashDriver` stores it and never uses it
   (flash_driver.py:170, 183). For R-full it is the configured hash and is not compared with the backup bytes. For flash
   the app BIN hash is enforced indirectly through the bundle segment sha.
2. Readback mismatch, LKG pre-write mismatch and `lkg_bytes_changed` have no direct test.
3. The fence seq is not asserted to stay unchanged after a commit rollback (the code is correct because it is inside the txn).
4. The tool-class split (E1) for flash plans has no test and is conditional on `auth_required`. A FLASH_TESTS plan still needs auth
   (broker.py:1700-1702), so it is not exploitable today.

---

## 7. Risk points when refactoring callers
1. **No `await` between `firmware_flash.execute` returning and `commit_success`** (broker.py:2605 → 2819). Adding one
   lets cancel, lease or principal changes interleave. The guard would still catch most of these, but `_committed` and `promotions` are RAM state.
2. **Guard callbacks run inside `BEGIN IMMEDIATE`** (store.py:625-672). `flash_live(committing=True)` requires
   `store.db.in_transaction` (broker.py:497) and accepts a `succeeded` row only while committing (500-508).
   Moving the guard outside the txn, or using another connection (for example BuildArtifacts' own connection), breaks this or
   risks SQLITE_BUSY.
3. **`defer_commit=True` must remain.** If it is dropped, the lifecycle sets `_committed` before the DB commit and a later cancel then returns False
   (flash_lifecycle.py:154-160, 363-364).
4. **The engine lifetime is the uncertainty latch.** `FirmwareFlash.uncertain` reads the *latest* engine. Creating a new
   `FlashLifecycle` before the busy/uncertain check (firmware_flash.py:601-622) would silently clear an uncertain close.
5. **Plan/digest stability.** `OperationPlan.digest` = sha256(json(asdict)), so renaming or reordering fields changes every
   fingerprint. `FLASH_EFFECTS` / `FLASH_STAGES` / `argv_template` are matched exactly (firmware_flash.py:162-166), so text edits
   strand stored plans (they fail closed, but deduplicated replays and history break).
6. **`prepare_plan` runs four times** (submit, enqueue `prepare_binding`, dispatch `validate_binding`, execute). Each run
   re-hashes the build artifacts and backup (the full flash size). Collapsing them moves "freshness" earlier; keep the execute-time
   re-prepare and full equality (firmware_flash.py:474-476).
7. **Frozen vs dynamic current.** The plan freezes `lkg_receipt` at prepare time. A *deferred* flash/restore calls `prepare` at
   promotion time (broker.py:841-849), so the implicit "current" is resolved after the owner's request. It is mitigated
   only by the `device_writes` fence (broker.py:811-817), and the design doc states that external writes are not detected
   (DESIGN-CONTINUATION-TICKETS.md:41-42).
8. **Thread/loop split.** `live()` must run on the loop (`worker_live`, 5 s timeout; firmware_flash.py:570-574) because SQLite
   and registry access are loop-owned (comment at 472). The driver's `_authority` uses the same callback. Keep both.
9. **Close semantics.** `NativeFlashDriver.close()` closes nothing; it only asserts that nothing is active or uncertain
   (flash_driver.py:501-503). "Confirmed close" comes from the process/backend state. A refactor that adds real closing
   must keep "uncertain ⇒ retain ownership" (flash_lifecycle.py:349-358; broker.py:2896-2905).
10. **`firmware_lkg` PK is device_id only** (store.py:71-75), while lookups are by (device, principal). Do not "fix" this implicitly;
    any change is a durable-state migration.
11. **Staging before commit is intentional.** CAS objects and backup files are written in the worker and stay inert without
    C3. There is no GC, so do not add cleanup of "unreferenced" staged objects without a separate decision, because history references them.
12. **Error-code precedence.** The close failure replaces the primary error (flash_lifecycle.py:354-356). `_execute` whitelists which
    codes become `failure_code` for boot diagnostics (firmware_flash.py:710-718). The broker persists only `str(failure)` and
    bounded diagnostics (broker.py:2720-2730).
13. **The `wrong_tool` split is conditional on `auth_required`** (surface.py:634, 665). If the surface changes, keep the
    broker-side auth requirement for FLASH_TESTS (broker.py:1686-1702).
14. **Duplicated gates are not identical** (§3). If they are merged into one helper, keep the differences: the enqueue gate has
    capacity and job-must-not-exist; the dispatch gate has `_physical_busy`/queued/epoch; `flash_live` has registration allow/digest
    and the committing branch, has no `collector_live`, and uses weaker `_execution_uncertain(allow_profile_cleanup=True)`.
