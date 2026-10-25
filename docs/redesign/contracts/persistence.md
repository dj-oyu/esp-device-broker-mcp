# Persistence contract inventory — esp-device-broker-mcp 0.13.0 (main 5d79d2d)

This inventory comes from reading the source only. I did not open any installed state, run any server or touch hardware.
All paths are relative to `src/cardputer_mcp/` unless they say otherwise. Anything I inferred rather than read in the code is marked **[speculation]**.

**Refreshed after redesign stage 4 (DESIGN-REDESIGN §5.8).** `timeline_order.py` was merged into `store.py` (`initialize_timeline_order`), and the LKG envelope/metadata and source receipt readers now also check validate-only models (`flash_lkg_models.py`, `source_receipt_model.py`). Rows that these changes touch name the function; every other file:line reference is still from `5d79d2d`.

---

## 0. Roots and how they resolve

| Root | Origin | Resolution |
|---|---|---|
| `state_dir` | `Settings.state_dir: Path = Path(".state")` config.py:144 | The config never resolves or absolutizes it. Every use is `settings.state_dir / ...` (broker.py:248,279,290,299; server.py:482). A relative value therefore resolves against **process cwd**. The config-file directory and the exe directory play no part. `Start-Cardputer.ps1:50,82,132` and `Start-CardputerTunnel.ps1:63,108` set the WorkingDirectory to the **profile's folder**, and `Start-CardputerGui.ps1:34` Push-Locations there. Docs say the same: README.md:331,429, WINDOWS-DISTRIBUTION.md:41, START-WINDOWS.md:11-12. The offline CLI `review`/`review-request` default `--state-dir .state` is also cwd-relative (cli.py:107,148). |
| Hidden location-coupling to `state_dir` | | Three things hash the resolved state_dir path. (a) The source-cache lock owner is `sha256(normcase(state.resolve()))` (source_cache_lock.py:62); moving the state dir gives `source_cache_owner_conflict`. (b) Builds without `build_root` live under `state_dir/firmware-builds` (build_locations.py:49), and their identity receipt `root_id` = sha256(casefold(output_base)) (build_locations.py:124). (c) `build_artifact_references.root_sha256` = sha256(str(root.resolve())) (build_artifacts.py:90), checked at firmware_build.py:1143. |
| `cache_root` | `SourceRepository.cache_root` (absolute, config.py:61) | `pin_root()` resolves it (source_snapshot.py:29). It goes through `BackupStore(cache_root)` (mkdir 0700 and private-ACL confirmation, flash_backup.py:31). |
| `build_root` | `BuildProject.build_root` (absolute or None, config.py:117) | `build_root.resolve()` pins it (build_locations.py:41-56). When it is None, `state_dir/firmware-builds` is used. |
| `backup_directory` | `FlashDevice.backup_directory` (absolute, flash_models.py:56) | It holds the backups, `lkg/` and `work/`. |
| keys registry | `%LOCALAPPDATA%\DeviceBroker\keys.sqlite3` (POSIX: `$XDG_STATE_HOME` or `~/.local/state`) | registry_defaults.py:9-22. It is **independent of state_dir and cwd**. |
| device lock | `%TEMP%\cardputer-mcp-<sha16(user)>\<backend>-device.lockfile` | broker.py:182-202. It sits outside state_dir. |

---

## 1. `state_dir/history.sqlite3` (main durable store)

- **Opened by** `Store.__init__` store.py:49-153 with `sqlite3.connect(path)`, `journal_mode=WAL` and `synchronous=FULL`. There is no busy timeout beyond the sqlite3 default (5s) and no `trusted_schema` setting.
- **Second writer connection.** `BuildArtifacts._connect` (build_artifacts.py:36-39) opens separate `mode=rw` / `mode=ro` URI connections from worker threads.
- The broker calls Store at broker.py:298. The offline operator calls it under `broker.lockfile` (operator.py:32,59).
- **Versioning: none.** There is no `PRAGMA user_version`; it stays 0. The schema evolves through `CREATE TABLE IF NOT EXISTS` plus three ad-hoc mechanisms:
  1. A column probe: `ALTER TABLE jobs ADD COLUMN diagnostic TEXT` when that column is missing (store.py:149-151).
  2. Marker rows in `operation_migrations(version)`. The only marker is `explicit_client_requests_v1` (store.py:155-176).
  3. A trigger-existence probe for `timeline_order` (`store.initialize_timeline_order`; moved from the former timeline_order.py in stage 4e).
- **Indexes:** no `CREATE INDEX` exists anywhere. The only indexes are the implicit PK and UNIQUE autoindexes.
- **Time encodings are mixed.** Some `created`/`finished` columns are TEXT from `CURRENT_TIMESTAMP` (`YYYY-MM-DD HH:MM:SS` UTC; parsed at continuation.py:243). `continuation_tickets.created`, `chain_started` and `deferred_jobs.created` are REAL `time.time()` values.

### Tables (all DDL is in store.py:56-148 unless noted)

| Table | Columns | Writers | Readers | JSON payload columns |
|---|---|---|---|---|
| `jobs` | id PK, plan_hash NN, state NN, stage, result, diagnostic, reviewed INT NN DEF 0, created TEXT DEF CURRENT_TIMESTAMP, finished | store.py:415 add_job, 424 enqueue_requested, 459 set_job, 475 finish_fixed_trial, 614 finish_firmware, 316 finish_home_recovery, 692 set_stage, 155/178 migrate/recover. **operator.py:37** (stray). | store.job 406; broker.py:482; build_artifacts.py:46,136; derby_provenance.py:148; home_recovery.py:178; operator_timeline.py:506; source_resources.py:28; source_retention.py:37,48 | `diagnostic`: free-form JSON object (see §1.2) |
| `plans` | hash PK, payload NN | store.put_plan 367 (callers: broker.py:933,1776; profile_jobs.py:280,331; home_recovery.py:260) | store.plan 402; build_artifacts.py:46; derby_provenance.py:155; plan_approval.py:69; profile_jobs.py:243 (json_extract); operator_timeline.py:426,532; source_retention.py:37; store.initialize_timeline_order (backfill and the `timeline_plan_registered` trigger) | `payload`: plan JSON (§1.1) |
| `execution_requests` | job_id PK, plan_hash, principal_id, created | store.py:449 | store 287,537,566; broker.py:1084; derby_provenance.py:148; derby_reports.py:43; operator_api.py:216 | — |
| `execution_audit` | sequence PK, job_id, plan_hash, principal_id NULL, event, created | store.py:165,453 | none found in src (audit only) | — |
| `operation_migrations` | version PK, created | store.py:175 | store.py:159 | — |
| `approvals` | hash PK | **none (dead legacy)** | none | — |
| `home_recovery_approvals` | recovery_job PK, principal_id, review_revision | **none (dead legacy)** | none | — |
| `home_closed_sources` | source_job PK, payload, attempted INT DEF 0 | store.py:255 (`json.dumps(sort_keys)`), 299 | store.home_source 241 | `payload`: {plan_hash, principal_id, origin, device_fingerprint, expected_firmware_sha256, port, …} (home_recovery.py:100) |
| `home_recovery_audit` | sequence PK, source_job, recovery_job, event, created | store.home_audit 247 (cap 50000) | server.py:954 | — |
| `home_recovery_executions` | recovery_job PK, source_job UNIQUE, source_hash, recovery_hash, principal_id, proof, created | store.py:306 | store.py:328 | `proof` = JSON copy of the home_closed payload |
| `derby_fixed_trials` | job_id PK, plan_hash, principal_id, raw BLOB, sha256, receipt, parsed, valid INT | store.py:541 | store.fixed_trial 564, then derby_reports.py:62 | `receipt` = {complete, bytes, duration_ms, sha256, gaps, overflow, truncated, partial_tail, disconnect, reset, refused}; `parsed` = parse_collected output or NULL |
| `work_sessions` | id PK, epoch, owner, principal_id, scopes, state, reason, created | store.py:721,737,190 | store.session_history 798 | `scopes` = sorted JSON list |
| `device_requests` | id PK, session_id, lease_id, state, lease_seconds, reason, reviewed, created, finished | store.py:744,760,187,790 | store.request 776, recovery_requests 780; home_recovery.py:183 | — |
| `firmware_lkg` | device_id PK, principal, receipt, kind, baseline, completed_job | store.finish_firmware 657 (UPSERT) | store.py:600; source_resources.py:82; source_retention.py:142 | — (`kind` ∈ full_backup, artifact_bundle) |
| `firmware_lkg_history` | receipt PK, device_id, principal, kind, baseline, completed_job | store.py:646 | store.py:607; source_resources.py:76; source_retention.py:138 | — |
| `firmware_job_summaries` | job_id PK, principal, payload (≤4096 B) | store.py:636 | store.firmware_summary 807; collector_binding.py:100 | `payload` = flash summary (readback_verified, passive_boot_verified, last_known_good_receipt, backup_receipt, …) |
| `device_writes` | id PK CHECK(id=1), seq | seed at store.py:136; store.py:644 | store.device_write_seq 689 | — |
| `build_artifact_references` | job_id PK, principal, payload | build_artifacts.py:96 (separate rw conn); DELETE at :135 | build_artifacts.py:109 | `payload` = {project_id, build_plan_hash, fingerprint, environment_fingerprint, build_location, manifest_sha256, root_sha256, [diagnostic_entry_sha256]} (≤4096 B) |
| `source_publications` | receipt PK, job_id UNIQUE, project_id, principal | store.record_source_publication 374 | source_retention.py:177 | — |
| `continuation_tickets` | ticket_id PK, parent_job_id UNIQUE, principal_id, epoch, state, reason, hold, consumed_by_job, chain_started REAL, chain_total, fence_seq, created REAL | continuation.py:103,113,122,129,150,159,175,184 | continuation.py:60,75,81,86,167,195,203; broker.py:1042 | `hold` ∈ NULL, 'none', 'ended' (broker.py:714,1048) |
| `deferred_jobs` | job_id PK, plan_hash, parent_job_id, principal_id, origin, epoch, ticket_id, wait_id, state, reason, resolved_plan_hash, created REAL | continuation.py:291,308,313(DELETE),325,335 | continuation.py:266,273,287 | — |
| `timeline_order` (`store.initialize_timeline_order`) | sequence INTEGER PK AUTOINCREMENT, job_id UNIQUE, created TEXT NULL | Triggers `timeline_plan_registered` (AFTER INSERT ON plans) and `timeline_job_registered` (AFTER INSERT ON jobs), created by the same function, which also runs the one-time backfill. | operator_timeline.py:520,534,643 | — |

**Job states persisted:** awaiting_approval, queued, running, succeeded, failed, cancelled, needs_review (operator_timeline.py:275). Plans may also carry legacy GUI labels approval_required, approved and ready (gui_job_status.py:16-19).

### Legacy rows and variants still read, and the in-place mutations applied to them

- **Old `jobs` without a `diagnostic` column.** The column is added on open (store.py:149). Test: tests/test_failure_diagnostics.py::test_old_history_gets_nullable_diagnostic_without_clearing_review.
- **Old `awaiting_approval`/`queued`/`running` rows.** One-time migration `explicit_client_requests_v1` rewrites them to cancelled or needs_review, with audit rows (store.py:155-176). Every startup `recover()` then also rewrites awaiting_approval/queued/running (store.py:178-193). Note: this *mutates* existing state on open, so it is a migration that already exists. Tests: tests/test_explicit_request_store.py::test_migration_never_replays_or_invents_an_execution_request and ::test_migration_failure_rolls_back_cancellation_audit_and_marker. This file's fixture builds a legacy DB with an `approvals` table (test_explicit_request_store.py:24-26).
- **Old plans without jobs (unexecuted).** These get an *undated* prefix in timeline_order with created=NULL (`store.initialize_timeline_order`), and the GUI shows "time unknown (legacy history)" (gui_live.py:762). Pinned on a pre-existing DB by tests/test_store_timeline_order.py (stage 4e).
- **Legacy `approvals` and `home_recovery_approvals` tables** are kept as historical evidence. Nothing reads them.

### 1.1 `plans.payload` — plan JSON shapes

- **Storage and identity.** The writer stores `json.dumps(payload, sort_keys=True)` with the *default* `", "`/`": "` separators (store.py:370). The identity hash, however, is `sha256(json.dumps(plan, sort_keys=True, separators=(",",":")))` (broker.py:177). Every reader re-canonicalizes and rehashes: store.py:228,436,532; build_artifacts.py:53; derby_provenance.py:103,160; derby_reports.py:36; plan_approval.py:78; source_retention.py:72.
- **⚠ Pydantic hazard.** A model must validate the *parsed dict* and leave the hash computed on the original dict. Dumping back through pydantic could change float repr (`source_plan_expires_at`/`source_plan_deadline` are floats, profile_jobs.py:257), add defaults or reorder keys, and any of these breaks every stored hash.
- **Variants** (discriminated by `version` and `execution_context`/`test`):
  - **v1 device plan:** {version:1, broker_epoch, session_id, device, backend, job_id, test, …details, [log_delivery]} (broker.py:1758-1769).
  - **v1 profile plans:** build, source, flash, backup, restore. They carry `execution_context` for profile jobs (store.py:236 treats a non-None execution_context as non-device). Fields include project_id, registration_sha256, fingerprint, build_principal, source_receipt, source_principal, source_commit, source_registration_sha256, source_plan_expires_at, source_plan_deadline, limits, effects, stages (plan_approval.py:107-135).
  - **v2 deferred plans:** {version:2, execution_context:'device_deferred' or 'registered_project_deferred', broker_epoch, job_id, test, principal_id, origin, lease_seconds, params or project_id/registration_sha256, continues:{parent_job_id}, [derby_detail_mode, derby_dma_mode]} (broker.py:911-922; profile_jobs.py:313-326).
  - **Home recovery plan:** source_plan is embedded, plus command, fresh_response, effects… (home_recovery.py:240-258).
  - **Flash plans:** FLASH_FIELDS (firmware_flash.py:91,183), optional `manifest_export_sha256`. Legacy flash plans that lack `manifest_export_sha256` are accepted unchanged (manifest_export.py:150-161; plan_approval.py:104-105).
  - **DERBY fixed workload:** the legacy `protocol: pocketjs-derby-fixed-v1` + `fixed_workload == LEGACY_PLAN` (derby_fixed.py:34-42) is still read by derby_reports.py:69-71,99-133 as `legacy_v1_no_owner_exit_proof`. The current form is `pocketjs-derby-fixed-v2` + FIXED_PLAN (derby_fixed.py:43-48). Writers only accept v2 (store.py:530).
  - **Collector binding inside DERBY fixed plans:** `collector_binding` with `schema_version: Literal[1]` (collector_binding.py:37,189). The J1 historical *omitted* `diagnostic_entry_sha256` is allowed only for commit `b561aea…` with no dma axis (diagnostic_legacy.py:40-61; collector_binding.py:60-62).
- **Hand-written validators of plans:**
  - plan_approval.py:86 `_schema` (v1 only)
  - firmware_flash `validate_flash_plan`
  - store.py:204 `profile_needs_review`, 424 `enqueue_requested`, 530 fixed-trial
  - source_retention.py:66-118
  - derby_provenance.py:121-166
  - collector_binding.py:57

### 1.2 `jobs.diagnostic` shapes (a free JSON object, NULL when empty; store.py:470)

- `{"code": ...}`, used for most failures.
- `BuildPreflightFailure.public_diagnostic()` (firmware_build.py:90).
- `_bounded_read_diagnostic` for flash failures (broker.py:2725).
- `{"source": summary}` for a successful source fetch (firmware_source.py:153). source_retention.py:99 and flash_binding.py:226 read `source.source_receipt`.
- `{"derby_observation": {..., "diagnostics": ...}, "derby_diagnostics_provenance": DerbyProvenance}` (broker.py:2851; derby_reports.py:13,180-200). DerbyProvenance is already a strict pydantic model with `schema_version` 1..1 (derby_provenance.py:54-87).
- `device_fault_category` / `device_fault_detail`, extracted with json_extract (operator_timeline.py:513-516).
- **Legacy:** old DERBY rows without `diagnostics` or provenance keep their shape and never get synthesized details (derby_reports.py:183-184). Test: tests/test_derby_diagnostic_reports.py::test_legacy_row_and_missing_plan_never_acquire_or_leak_new_details and ::test_persisted_history_keeps_legacy_anonymous_summary_and_owned_restart_read.

---

## 2. `keys.sqlite3` (API key registry)

- **Location:** `%LOCALAPPDATA%/DeviceBroker/keys.sqlite3` (registry_defaults.py:9-22).
- **Writer and reader:** `KeyRegistry` (key_registry.py:180-480). Each call opens a short connection with `mode=rw`, `trusted_schema=OFF` and `synchronous=FULL` (:256-260). On create it uses `journal_mode=DELETE` and `max_page_count=2048` (:209-210).
- **Schema** (key_registry.py:211-232):
  - `api_keys(key_id PK, verifier BLOB CHECK len=32, label, origin CHECK IN('local_cli','remote_mcp'), scopes CHECK IN('read','control,read'), issued_at REAL, expires_at REAL, revoked_at REAL, CHECK expiry>issued, CHECK ttl≤2592000)`
  - `key_audit(sequence PK AUTOINCREMENT, event CHECK IN('key_issued','key_revoked','key_rotated'), key_id, at REAL)`, trimmed to `_MAX_AUDIT_ENTRIES` at :310
  - `key_lineage(key_id PK, predecessor, owner_id, CHECK key_id!=predecessor)`: optional and additive within version 1. Created on registry creation or inside the first `rotate` transaction; one row per rotated replacement, `owner_id` is the chain's root key. Absent table = no lineage (exact principals). Read by `KeyRegistry.same_owner`.
- **Versioning:** `PRAGMA user_version=1`. Opening accepts 0 or 1 when creating, and rejects other values (:197-202). Every connection after init requires exactly 1 (:260).
- **Row validator:** `_metadata` at key_registry.py:279. File and ancestor safety checks: `_safe_file` / `_safe_directory`.
- **Tests:** tests/test_key_registry.py::test_schema_version_not_silently_downgraded and ::test_open_existing_never_creates_missing_database; tests/test_registry_defaults.py. There is no "legacy" format to stay compatible with.

---

## 3. Other files under `state_dir`

| File | Writer | Reader | Format | Atomic method | Validator |
|---|---|---|---|---|---|
| `broker.lockfile` | filelock (broker.py:279; operator.py:26,53) | — | lock only | — | — |
| `runtime-auth.json` (POSIX only; Windows raises `private_runtime_credentials_unsupported`, auth.py:40) | AuthState._write_runtime auth.py:190-233 | auth.py:86,252,275 | `{"version":1,"epoch":str,"tokens":{"local":hex,"remote":hex}}`, compact | O_EXCL temp `.runtime-auth-<uuid>.tmp`, fsync, `os.replace` via dir_fd, fsync dir | auth.py:123 `_decode` (exact key set; version==1) |
| `firmware-builds/<fp64>/<env64>/` (layout v1, when build_root is None) | build_locations.claim_build_directory :131 | resolve_build_manifest :282 | build dir | mkdir | — |
| `firmware-builds/.locks/<slot>.lock` (or `<build_root>/.locks/`) | filelock (build_locations.py:270; firmware_build.py:845) | — | lock | — | — |
| `compiler-cache/<sha256([project,principal])>/` | build_ccache.py:93-97 | ccache | ccache dir | — | — |
| `job-logs/<job_id>.jsonl` (schema_version 1) | job_logs.JobLogStore.begin / JobLog.append / JobLog.record | JobLogStore.read / build_read / device_report / app_events_read / records | header `{"schema_version":1,"job_id","principal_id","kind","created"}`, then stream entries `{"seq","t","stream","level","text"}` (streams `source`, `build`, `flash`, `device`, `app`) and structured records `{"seq","t","record","data"}` (`flash_outcome`, `app_event`) | O_APPEND + flush per line, fsync on close; a crash leaves at most one partial final line, which readers skip | `_load` (header schema_version==1, job_id/principal_id match the requester) |
| `captures/<job_id>/capture.png` + `capture.json` (schema_version 1) | capture_store.CaptureStore.save (broker worker, after the capture job's port is confirmed closed) | CaptureStore.load (broker.read_capture after the RAM copy is gone) | exact PNG bytes; metadata `{"schema_version":1,"job_id","principal_id","origin","mime_type","size_bytes","sha256","width","height","source","captured_utc"}` | temp file + fsync + `os.replace`, PNG first, metadata last | `load` (schema_version, owner, mime, source, size and sha256 re-verified) |
| `history-archive/<stamp>-<id>/history.sqlite3` (+ `job-logs/`, `captures/`) | offline `cardputer archive` only (history_archive.py; `Store.archive_rows`), under `broker.lockfile` | none in the broker (offline inspection only) | `CREATE TABLE ... AS SELECT *` copies of the archived rows of the tables in `store.ARCHIVE_TABLES`, plus `archive_info(key,value)`; moved job-log/capture files unchanged | copy commits to the new file (journal DELETE, synchronous FULL), re-read and integrity-checked, then one live transaction deletes exactly the copied rows | row counts and `PRAGMA integrity_check` |

### 3.1 Durable job logs, app-session records, flash outcomes and captures

Saving is unconditional and never changes a job's outcome; a store failure
marks the log unavailable (non-flash jobs get the same-state
`job_log_unavailable` annotation). Everything written passes the job-log
redaction (`build_logs.redact_log_text`, plus the job's env-secret
redactions); structured records are redacted value by value. No record holds
firmware bytes, device output beyond the redacted log lines, absolute paths or
credentials.

- **App sessions** (`app_session` jobs). The broker opens the job's log before
  the session starts and passes it to `AppSessions.execute`. `AppEvents` tees
  every control event as an `app_event` record (the RAM event dict, including
  its `sequence`) and every app log line to the `app` stream, for every
  outcome (stopped/timeout/cancelled/authority lost/teardown unconfirmed). The
  broker closes the log in the worker's `finally`. `read_app_events` serves
  RAM while the run is retained and not expired; after expiry, eviction from
  the bounded app job table or a restart it serves the durable copy to the
  job's own principal only, for a terminal job (`source:
  "durable_job_log"`, a durable cursor epoch, `state`/`result` added).
- **Flash outcome** (`firmware_flash` / `firmware_backup` / `firmware_restore`
  jobs the worker ran). After the terminal transition and before the logs close,
  the broker appends one `flash_outcome` record (`schema_version` 1):
  state, reason code, last stage, phase reached and phase times,
  `write_started` / `close_confirmed` (the same evidence the failure
  classification uses), plan hash, device id / fingerprint / expected and bound
  device identity, referenced `lkg_receipt` / `backup_job_id` /
  `backup_sha256` / `build_job_id` / `manifest_sha256`, the backup receipt, the
  staged LKG receipt and whether it was selected, bounded verification fields
  and timings. It is written with a small reserve beyond the 8 MiB per-job cap,
  so it survives a truncated stream. `read_firmware_flash_logs` returns the
  latest one as `flash_outcome` to any read-scoped caller (served through the
  owner's reader, see surface.md), after RAM loss and restart. A
  broker crash mid-flash leaves no `flash_outcome`; the `-- <stage>` lines in
  the `flash` stream still show the phase reached. Flash jobs cancelled while
  still queued never ran and get no record.
- **Captures.** The worker persists a validated capture before placing it in
  the five-minute RAM cache (also while closing). It is served only for a
  succeeded `capture_home` / `derby_smoke` job, to any read-scoped caller
  (loaded under the recorded principal, see surface.md), after
  the same per-request `principal_valid` check against the recorded origin;
  a quarantined (unconfirmed-close) capture is never saved. The operator GUI
  timeline card and `operator_image` fall back to the saved copy once the RAM
  copy is gone (`retention: "saved"`, `remaining_seconds: null`; metadata only
  on the card, no origin or path); `operator_images` still lists only the RAM
  cache.
- **Retention.** Job logs: 500 files / 512 MiB total, 8 MiB per job. Queued /
  running jobs are never pruned. Eviction is by retention tier, then oldest
  first: tier 0 succeeded non-flash jobs (and logs with no job row), tier 1
  other ordinary jobs, tier 2 `needs_review`, any flash/backup/restore job and
  any job that produced an LKG (`firmware_lkg` / `firmware_lkg_history`
  `completed_job`). Captures: 200 directories / 64 MiB total, oldest first.
- **Compatibility.** The job-log schema stays 1: the `app` stream and the
  record lines are additive. Stream readers select by `stream`, so records are
  skipped by older brokers, and older logs (no records) read unchanged.

## 4. Build output (`build_root/<slot>/` or `state_dir/firmware-builds/<fp>/<env>/`)

| File | Writer | Reader(s) | Schema/version | Atomic | Validator |
|---|---|---|---|---|---|
| `broker-build-identity.json` | build_locations.py:141-146 (`open("x")`, json.dump sort_keys) | build_locations.py:139,197,312 | `{receipt_version:1, location:{layout_version:1} or {layout_version:2, slot}, fingerprint, environment_fingerprint, root_id}` (:117-127) | exclusive create, no fsync | equality with recomputed `identity_receipt` |
| `broker-manifest.pending.json` → `broker-manifest.json` | firmware_build.py:1023-1036 (`write_text(indent=2)` then `Path.replace`); deferred commit at :1068 | firmware_build.py:1147 (`_json` limit 1 MiB); build_locations.py:297,314; flash_manifest.verify_bundle :76 | `schema_version:1`, build_job_id, build_plan_hash, idf_version "6.0.1", target, fingerprint, environment_fingerprint, compiler_cache, artifacts[{name,bytes,sha256}], registered_tool_versions, flash_layout, flash_security, flash_security_summary, build_location, flash_authorized:false, [source_provenance{…, submodules, dependency_policy}], [derby_detail_mode, diagnostic_modeproof, diagnostic_entry_sha256, derby_dma_mode] (firmware_build.py:987-1020) | replace with no fsync. **[speculation]** Not durable across a power loss; the DB reference hash detects that. | build_locations.resolve_build_manifest :282; firmware_build.resolve_flash_manifest :1121; `_verify_owned_artifacts` :1189; flash_manifest.verify_bundle :76; manifest_export._validate_public_manifest :32 |
| `broker-ccache.conf` | build_ccache.py:107-118 (NamedTemporaryFile + replace) | ccache | text | temp+replace | — |
| `diagnostic-mode-proof.json` / `dma-diagnostic-mode-proof.json` | IDF build (recipe) | diagnostic_modeproof.py:216; diagnostic_dma_modeproof.py:186 | `schema_version:1` (diagnostic_modeproof.py:139-149; dma :122-132) | n/a | same lines |
| `flasher_args.json`, `config/sdkconfig.json`, `project_description.json` | ESP-IDF | firmware_build.py:196,260,948; flash_manifest.py:102,118 | IDF-owned | n/a | `_unique_layout_pairs` firmware_build.py:183 |

**Legacy variants.** A manifest **without a `build_location` key** is treated as layout v1 under `state_dir/firmware-builds` (build_locations.py:276-305 docstring "Old schema-v1 manifests without a build_location"; build_artifacts.py:88). **No test explicitly pins a manifest without `build_location`.** test_build_locations.py only uses `manifest(True)`. Some fixtures write manifests without build_location (tests/test_source_history_retention.py:62, tests/test_manifest_export_fw_consumer.py:60) **[speculation: indirect coverage only]**.

## 5. Source snapshot cache (`cache_root/`)

| Path | Writer | Reader | Schema | Atomic | Validator |
|---|---|---|---|---|---|
| `.source-cache.lock` | source_cache_lock.py:29-71 | same | 64 ascii hex = sha256(normcase(state_dir.resolve())) | O_CREAT + msvcrt/flock | size==64 and bytes match (:68) |
| `pending-<uuid32>/reservation.json` | source_snapshot.py:1028-1040 (`open("xb")`, fsync) | source_payloads._pending :190-218 | canonical `{job_id, project_id, principal, registration_sha256}`. Unknown or legacy reservations are skipped (:197). | exclusive + fsync | ad hoc .get checks |
| `<receipt64>/receipt.json` (receipt = sha256 of the canonical bytes; validate-only models `ResolvedSourceReceipt` / `ListedSourceReceipt` in source_receipt_model.py, checked on each reader's success path only) | source_snapshot.py:1049-1091 (`xb`, fsync, then `pending.rename(target)`) | source_snapshot._resolve :1146-1196; source_payloads.metadata :28-79; `_evidence_only` :1200 | **schema_version 1** {fetch_job_id, principal, project_id, repository, commit, tree_sha1, archive_sha256, source_sha256, files{name:{bytes,mode,git_sha1,sha256?}}, registration_sha256}; **schema_version 2** adds `submodules`, `root_source_sha256` | fsync file; directory rename (no dir fsync) | :1161-1174 (schema ∈{1,2}, owner, project, registration digest set per schema_version); source_payloads.py:66-77 |
| `<receipt64>/source/` | rename from pending | `_resolve` re-hashes every file | tree | rename | file_receipt equality |
| `<receipt64>/reclaim.json` + `reclaiming/` | source_payloads._reclaim :160-176 (`xb`, fsync) | :143-152 | canonical `{receipt, project_id, principal}`, ≤1024 B, byte-equality | exclusive + fsync | byte equality |
| evidence-only dir (only receipt.json left after a payload is reclaimed) | — | `_evidence_only` :1200; admit_source restore path source_payloads.py:244 | — | — | — |

**Registration digest legacy set** (registration_digest.py:55-82). A receipt or reservation is accepted if its `registration_sha256` is any of:
- the current path-independent digest;
- the pre-submodule shape (only when schema_version ≠ 2);
- the old location-bound `_PreLocationSourceRegistration` digest, computed for `cache_root` and each `previous_cache_roots` entry, with or without `submodule_repositories`.

Tests:
- tests/test_registration_digest.py::test_old_style_digests_are_reproduced_exactly, ::test_only_listed_previous_roots_are_trusted, ::test_old_style_receipt_is_rejected_after_a_move_unless_listed, ::test_new_receipt_survives_a_move_without_any_listing
- tests/test_source_snapshot.py::test_old_receipt_survives_allowlist_addition_without_mutation (schema_version 1 receipt stays readable)
- tests/test_source_lifecycle.py::test_current_unexecuted_legacy_plan_and_active_job_protected_terminal_released, ::test_restart_recovers_interrupted_reclamation_preserving_receipt, ::test_terminal_reservation_reconciled_on_restart_active_and_unknown_preserved

## 6. Flash backup / LKG store (`backup_directory/`)

| Path | Writer | Reader | Schema | Atomic | Validator |
|---|---|---|---|---|---|
| `<job_id>.bin` | flash_backup.BackupStore.save :51-93 (O_EXCL, fsync) | BackupStore.load :95-140 | raw flash image | exclusive + fsync, no rename | size and sha check |
| `<job_id>.json` | same (`json.dumps(asdict(RetainedBackup), sort_keys)`) | load :105 `RetainedBackup(**json.loads(...))` | {job_id, principal, device_sha256, device_fingerprint, flash_bytes, sha256}, ≤4096 B | exclusive + fsync | dataclass kwargs: a new or missing key fails as `backup_missing_or_changed` |
| `lkg/<sha256>` (content-addressed objects) | flash_lkg_store.FirmwareSnapshots.put :47-75 (`<digest>.pending` O_EXCL, fsync, MoveFileExW WRITE_THROUGH no-replace / rename + dir fsync) | get :77-98 | raw bytes | yes | sha256 equality |
| `lkg/<receipt>` envelope | stage :100-109 | `FirmwareSnapshots.load`; `FirmwareFlash._known` | `{schema_version:1, metadata, files{name:digest}}` | as above | `flash_lkg_models.LkgEnvelope` (exact key set, version `== 1`); metadata checks `flash_lkg_models.bound_lkg_metadata` |
| `work/` (temp `job.json` for the esptool worker) | flash_driver.py:261-272 | flash_worker.py:279 | transient | — | — |

**LKG metadata variants inside the envelope:**
- `kind:"full_backup"` written at flash time has **no `compat` key** (firmware_flash.py:667-679).
- `kind:"full_backup"` written via restore has `compat: None` (:553-567).
- `kind:"artifact_bundle"` has `bundle`, `manifest` and `compat` (:536-551).

Readers use `flash_lkg_models.lkg_compat` (the same value as `.get("compat")`), so both full_backup shapes stay readable. The permissive `LkgMetadata` view allows the key to be absent and keeps that distinct from null through `compat_stored`. Pinned by tests/test_flash_lkg_models.py::test_metadata_view_keeps_compat_absent_distinct_from_null_and_keeps_extras and ::test_artifact_bundle_lkg_without_compat_skips_the_layout_check.

## 7. Configuration: `device.local.toml` / broker profile TOML

- **Loader:** `load_settings` (config.py:166-170): `tomllib`, then `Settings.model_validate`. It is also loaded by tunnel.py:118,152,231 and rewritten by `save_gui_config` (tunnel.py:203-267). That rewrite uses NamedTemporaryFile in the same directory, fsync, a compare-original check, `os.replace`, and a round-trip equality check.
- **All config models use `extra="forbid"` and `frozen=True`:**
  - `Settings` (config.py:139)
  - `SerialSettings` :15
  - `TunnelMetadata` :24
  - `RegisteredBuildTool` :43
  - `SourceRepository` :59 (`previous_cache_roots` is documented "keep this field last", :65-67)
  - `BuildProject` :110 (`expected_idf: Literal["6.0.1"]`, `target: Literal["esp32s3"]`)
  - `FlashDevice` (flash_models.py:47)
  - `FlashPython` :12
  - `UnattendedFlash` :26-44 (`enabled` strict bool, principal_id, device_id, project_id, `operation: Literal["firmware_flash"]`, source_receipt, source_repository, source_commit)
  - `DeviceSelector` (ports.py:21, StrictModel)
- **Registration digests** (registration_digest.py), all computed from `model_dump_json`:
  - `source_digest` excludes location fields {cache_root, private_acl_confirmed, previous_cache_roots}.
  - `project_digest` excludes build_root and the source location fields. `project_digests` also accepts the old full dump minus `previous_cache_roots` (:85-90).
  - `flash_digest` excludes {backup_directory, private_backup_acl_confirmed}. `flash_digests` also accepts the full dump (:93-97).
  - Uses: broker.py:2574; collector_binding.py:135; firmware_flash.py:407; flash_binding.py:27; profile_jobs.py:98,104; source_payloads.py:74,209; source_snapshot.py:1167.
  - ⚠ **Any field added to these pydantic config models changes `model_dump_json`, and with it every new and every "old-style" digest.** Durable plans and receipts bound to the old digests then fail closed. Before the redesign touches config models, the exact field set and order of each model has to be frozen as a contract. **[speculation]** Adding `unattended_flash` (default None, which dumps as `null`) to FlashDevice would already have changed `flash_digests`, unless that release accepted the break.
- **Legacy tunnel config.** A flat `tunnel.local.toml` (`TunnelSettings`, tunnel.py:41-49,75-84,127-150) is still read, merged and conflict-checked. Example: examples/tunnel.legacy.example.toml. Tests: tests/test_tunnel_config.py::test_conflicting_legacy_metadata_fails_closed_in_both_entry_points (fixture `legacy_file`, :37), plus tests/test_tunnel_launcher.py and tests/test_tunnel_ingress_config.py.

## 8. Packaging / deployment receipts

- **Release zip** (packaging/release_windows.py): members `cardputer.exe`, LICENSE, NOTICE, WINDOWS-DISTRIBUTION.md, examples/mock-safe.toml, `build-runtime.json`, `manifest.json` and `SHA256SUMS.txt` (:15-22).
  - `build-runtime.json` keys: schema_version, python_version, uv_version, source_commit, uv_lock_sha256, build_requirements_sha256, exe_sha256 (:23-31; written by record_build_runtime :34 as `<exe>.build.json`).
  - `manifest.json` is `{schema_version:1, version, source_commit, source_ref, platform:"windows-x64", python_version, uv_version, build_runtime_sha256, uv_lock_sha256, build_requirements_sha256, files{name:sha256}}` (:150-162).
  - The verifier `verify_archive` (:182-218) does **not** check `manifest["schema_version"]`.
  - Test: `python -m unittest tests/test_windows_release.py`.
- **Deployment receipts** with `state_working_directory`, as described in AGENTS.md:99-103, are **not produced or read by any tracked code**. I found no matches in src/, packaging/ or the Start-*.ps1 scripts. They are a local operator artifact; their format is not owned by this repo and is untested **[speculation: written by a hand or agent deploy procedure]**. The untracked `.deployment/` folder in the worktree holds DB snapshots (history-before.sqlite3 …), not a receipt schema.

---

## 9. SQL outside store.py

Every one of these goes through `broker._store().db` (the shared loop connection) unless noted.

| File:line | Statement | Note |
|---|---|---|
| **operator.py:37** | `UPDATE jobs SET reviewed=1 WHERE id=?` | **The stray one.** The offline review path writes directly instead of through a Store method. The job equivalent of `Store.review_request` (store.py:790) does not exist. Guarded only by the Python-side `job["state"]=="needs_review"` check at :35 and by holding `broker.lockfile`. The SQL has no `AND state='needs_review'`. |
| broker.py:482 | SELECT jobs | running binding check |
| broker.py:1084 | SELECT execution_requests | |
| build_artifacts.py:46,96,109,135-136 | SELECT jobs⋈plans, INSERT/SELECT/DELETE build_artifact_references | **separate sqlite3 connections** (rw/ro URI), from workers |
| continuation.py:60-335 (23 stmts) | all CRUD on continuation_tickets / deferred_jobs, including `DELETE FROM deferred_jobs` :313 | DDL is in store.py |
| derby_provenance.py:148,155 | SELECT jobs⋈execution_requests, plans | |
| derby_reports.py:43 | SELECT execution_requests | |
| home_recovery.py:178,183 | SELECT jobs, device_requests | |
| operator_api.py:216 | SELECT execution_requests | |
| operator_timeline.py:280,426,455,506,532,643 | SELECTs with json_valid/json_extract over plans/jobs/timeline_order | needs the SQLite JSON1 functions |
| plan_approval.py:69 | SELECT plans | |
| profile_jobs.py:243 | `SELECT payload FROM plans WHERE json_extract(payload,'$.job_id')=?` | full scan (no index) |
| server.py:954 | SELECT home_recovery_audit | |
| source_resources.py:28,76,82 | SELECT jobs⋈plans, firmware_lkg(_history) | |
| source_retention.py:37,48,138,142,177 | SELECTs | |
| key_registry.py:197-470 | keys.sqlite3 (separate DB) | |

The `timeline_order` DDL, backfill and triggers used to live in timeline_order.py. Stage 4e moved them verbatim into `store.initialize_timeline_order`, so they are no longer SQL outside store.py.

---

## 10. Hand-written validators of persisted data (candidates for strict pydantic)

1. auth.py:123 `_decode`: runtime-auth.json
2. key_registry.py:279 `_metadata`: api_keys rows
3. flash_backup.py:95-140 `BackupStore.load`: backup sidecar json
4. `FirmwareSnapshots.load`: LKG envelope; `FirmwareFlash._known`: LKG metadata. **Stage 4b:** validate-only `LkgEnvelope` / `LkgMetadata` / `bound_lkg_metadata` in flash_lkg_models.py; never dumped back.
5. `SourceSnapshots._resolve`: receipt.json v1/v2. **Stage 4c:** also checks `source_receipt_model.ResolvedSourceReceipt` on its success path; the ordered hand-written checks still decide the reason code.
6. `source_payloads.metadata` (**stage 4c:** also checks `ListedSourceReceipt` on its success path); `_pending` reservation.json; `_reclaim` reclaim.json
7. build_locations.py:89-114 `_json` + :117 identity receipt + :282-320 `resolve_build_manifest`
8. build_artifacts.py:42-58 `_plan`; :104-127 `reference`
9. firmware_build.py:1121-1187 `resolve_flash_manifest`; :1189-1215 `_verify_owned_artifacts`
10. flash_manifest.py:76 `verify_bundle`
11. manifest_export.py:32 `_validate_public_manifest`; :84 `validate_manifest_export`
12. store.py:475-509 `finish_fixed_trial` receipt; derby_reports.py:62-143 `private_job` fixed-trial record
13. store.py:204-239 `profile_needs_review`; store.py:424-445 `enqueue_requested`
14. plan_approval.py:86 `_schema` (plans v1)
15. source_retention.py:14-195 `retention_evidence` (plans and diagnostics)
16. diagnostic_modeproof.py:115-205; diagnostic_dma_modeproof.py:92-175: proof json
17. collector_binding.py:57 `validate_collector_schema` (CollectorBinding is already pydantic, schema_version Literal[1])
18. operator_timeline.py:307-330 `observation_summary` (schema_version 1 metadata)

Already pydantic: DerbyProvenance (derby_provenance.py:54), DerbyDiagnostics, all config models, TunnelSettings.

---

## 11. Read-compat test matrix

| Legacy format / variant | Pinning test(s) |
|---|---|
| jobs table without `diagnostic` column | tests/test_failure_diagnostics.py::test_old_history_gets_nullable_diagnostic_without_clearing_review |
| awaiting_approval/queued/running legacy rows + `approvals` table | tests/test_explicit_request_store.py::test_migration_never_replays_or_invents_an_execution_request, ::test_migration_failure_rolls_back_cancellation_audit_and_marker |
| Old-style registration digests (source / project / flash) and previous_cache_roots | tests/test_registration_digest.py::test_old_style_digests_are_reproduced_exactly, ::test_only_listed_previous_roots_are_trusted, ::test_old_style_receipt_is_rejected_after_a_move_unless_listed |
| Source receipt schema_version 1 after submodule allowlist added | tests/test_source_snapshot.py::test_old_receipt_survives_allowlist_addition_without_mutation |
| Source receipt schema_version 2 | tests/test_source_snapshot.py::test_fixed_gitlinks_recursive_receipt_and_build_provenance (current-format, not legacy) |
| Source receipt v1/v2 in both readers (`_resolve`, `metadata`), extra keys, equality-compared versions | tests/test_source_receipt_model.py::test_v1_receipt_reads_in_both_readers_without_rewriting_bytes, ::test_v2_receipt_reads_in_both_readers_without_rewriting_bytes, ::test_v2_without_valid_submodules_differs_between_readers, ::test_bool_and_float_schema_versions_compare_equal_today |
| LKG envelope schema_version 1 (exact keys, `== 1`, stored bytes never rewritten) | tests/test_flash_lkg_models.py::test_envelope_requires_every_key, ::test_envelope_version_check_is_equality_with_one, ::test_reading_never_rewrites_stored_objects |
| LKG metadata with absent `compat` vs `compat: null`, extra keys | tests/test_flash_lkg_models.py::test_metadata_view_keeps_compat_absent_distinct_from_null_and_keeps_extras, ::test_artifact_bundle_lkg_without_compat_skips_the_layout_check, ::test_extra_metadata_key_is_accepted_and_kept |
| `timeline_order` backfill on a pre-existing DB (undated legacy prefix, trigger probe) | tests/test_store_timeline_order.py::test_pre_timeline_history_gets_undated_plan_prefix_then_dated_jobs, ::test_incomplete_timeline_backfills_after_existing_rows, ::test_complete_triggers_skip_backfill_on_reopen |
| Interrupted reclaim / reservation reconciliation | tests/test_source_lifecycle.py::test_restart_recovers_interrupted_reclamation_preserving_receipt, ::test_terminal_reservation_reconciled_on_restart_active_and_unknown_preserved |
| DERBY fixed v1 (LEGACY_PLAN) trial evidence | tests/test_device_faults.py::test_legacy_evidence_is_readable_without_upgrading_old_success, ::test_owner_exit_contract_and_explicit_legacy_read |
| Legacy DERBY job rows without new diagnostics/provenance | tests/test_derby_diagnostic_reports.py::test_legacy_row_and_missing_plan_never_acquire_or_leak_new_details, ::test_persisted_history_keeps_legacy_anonymous_summary_and_owned_restart_read |
| Legacy DERBY photo-timing fallback plan | tests/test_derby_photo_timing.py::test_legacy_fallback_plan_cannot_open_or_execute_new_behavior |
| J1 legacy descriptor hash / omitted collector digest | tests/test_diagnostic_registry.py::test_legacy_descriptor_hash_and_exact_omission_boundary_are_preserved, ::test_binding_null_and_mode_downgrade_never_select_legacy; tests/test_dma_collector_binding.py::test_collector_dma_axis_has_no_legacy_default |
| Flash plan without manifest_export_sha256 | tests/test_manifest_export.py::test_fixed_plan_commitment_rejects_rehashed_other_job_or_manifest (legacy block :152-155); tests/test_manifest_export_binding.py::test_commitment_damage_cannot_reuse_a_fixed_trusted_plan_hash |
| Job log schema_version 1 written before records / `app` stream existed | tests/test_durable_session_flash_records.py::test_pre_record_v1_job_log_still_reads |
| keys.sqlite3 user_version | tests/test_key_registry.py::test_schema_version_not_silently_downgraded |
| Legacy flat tunnel.local.toml | tests/test_tunnel_config.py::test_conflicting_legacy_metadata_fails_closed_in_both_entry_points |
| Durable build reference after restart | tests/test_build_artifact_persistence.py::test_flash_owned_context_uses_durable_reference_after_restart |
| Old prepared plan after restart (timeline) | tests/test_operator_timeline.py::test_old_prepared_plan_after_restart_never_executes_from_reading_timeline |

### Formats with no read-compat test found

Stage 4 removed three entries from this list (timeline_order backfill, absent vs null LKG `compat`, LKG envelope v1); they are now rows in the matrix above.

- **Build manifest without `build_location`** (implicit layout v1). It has indirect fixtures only.
- **Backup sidecar `<job>.json`.** Its schema is only exercised by the current writer.
- **`broker-build-identity.json` receipt_version 1.** Current-format only.
- **`runtime-auth.json` version 1.** Current-format only; POSIX.
- **Plans JSON stored with non-compact separators.** Nothing pins that a stored row's text differs from its hash bytes, which is what lets a re-dump break the identity.
- **Old `version:1` plans that lack keys added later** (e.g. `log_delivery`, `source_plan_deadline`). These rely on `.get()` throughout, and nothing pins them systematically.
- **Release `manifest.json` schema_version.** It is not even checked by `verify_archive`.
- **Deployment receipts (`state_working_directory`).** No code and no test.
- **Legacy dead tables `approvals` / `home_recovery_approvals`.** They are only present in the explicit-request migration fixture.
