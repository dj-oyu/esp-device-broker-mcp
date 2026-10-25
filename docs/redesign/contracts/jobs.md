# Contract inventory: job lifecycle, tickets, hold, deferred jobs, ownership, cancellation

Source: `main` @ 5d79d2d (0.13.0). Read-only inventory. Paths are relative to
`src/cardputer_mcp/` unless they start with `tests/`. "SPEC:" marks speculation or
inference that the code alone does not settle. "UNTESTED" means a grep of `tests/`
for the reason code or behaviour found nothing.

Global premise (verified): every state change runs on the single asyncio loop. No
explicit lock covers job/ticket/hold state. The only serialization is that there is
**no `await` between a check and its write** (comments at broker.py:607, 1013, 1516,
1874, 2418; profile_jobs.py:500). Any redesign that adds an `await` inside a
check→write window breaks every guard listed below.

---

## 1. Job state machine as implemented

### 1.1 States

| State | Terminal | Written by | Notes |
|---|---|---|---|
| `queued` | no | `Store.enqueue_requested` store.py:424-457 (device lane, home recovery), `Store.add_job` store.py:415-419 (profile lane) | Only insertion states. Nothing inserts any other state. |
| `running` | no | `set_job(...,"running")` broker.py:2469, profile_jobs.py:621; guarded SQL store.py:297-300 (home recovery) | `set_job` running **clears result/diagnostic** (overwrites with NULL) and sets `finished=NULL` (store.py:466-470). |
| `succeeded` | yes | many (see 1.3) | |
| `failed` | yes | broker.py:2812 (app_session timeout), profile_jobs.py:718 | Device lane uses `failed` only for app_session timeout. |
| `cancelled` | yes | many | |
| `needs_review` | yes (`reviewed` flag 0→1 offline only) | many | `reviewed=1` only via operator.py:37 (offline, under the state lock) and store.py:360 (home recovery resolution of the *source* job). |
| `awaiting_approval` | legacy | **never written** by current code | `add_awaiting_job` / `approved_enqueue` / `approve` raise `approval_interface_retired` (store.py:421-422, 702-714). |
| `interrupted` | — | never written | Only in read checks: broker.py:1187, continuation.py:28 (`TERMINAL_FAILURES`, unused constant). |

`terminal` for the `finished` timestamp = state not in (`awaiting_approval`,`queued`,`running`) (store.py:466).
Pseudo states (no row in `jobs`): `deferred_jobs.state` ∈ {`pending`,`promoted`,`cancelled`};
`Deferred.view` maps `pending` to `waiting_parent` / `waiting_for_device` (continuation.py:341-362).

### 1.2 Legacy `awaiting_approval` handling (old DB rows)

- One-time migration `migrate_explicit_requests` (store.py:155-176), marker
  `explicit_client_requests_v1`, one `BEGIN IMMEDIATE` txn: audit rows
  (`legacy_running_quarantined` / `legacy_pending_cancelled`); `awaiting_approval|queued → cancelled`
  (`explicit_requests_migration_no_replay`); `running → needs_review, reviewed=0`
  (`explicit_requests_migration_running_uncertain`). Old `approvals` grants are kept, never read.
  Pinned: `tests/test_explicit_request_store.py::test_migration_never_replays_or_invents_an_execution_request`,
  `::test_migration_failure_rolls_back_cancellation_audit_and_marker`.
- Every start, `Store.recover` (store.py:178-193): `awaiting_approval → cancelled`,
  `queued → cancelled`, `running → needs_review reviewed=0`, all `broker_restart_no_replay`;
  `device_requests waiting|owning → needs_review`; `work_sessions open → needs_review`. This is
  **not one txn**: there are several `execute` calls and one `commit`. SQLite implicit txn
  makes it atomic in practice. SPEC: `isolation_level` is the default.
- Since restart always clears them, these read-side branches are **dead at runtime**. Keep or
  remove them on purpose: broker.py:402, 464, 517 (`pending_execution`, always None),
  533 (`_execution_capacity_full`, awaiting count always 0), 734, 876, 1037, 1912-1917
  (`Broker.cancel` awaiting branch), plan_approval.py:502, profile_jobs.py:304, 398, and the
  GUI/timeline labels (gui_job_status.py:17, 87; operator_timeline.py:24, 109, 276).
  `dispatch_pending_execution` raises `approval_interface_retired` (broker.py:1903-1904).

### 1.3 Every job-state write (file:line, from→to, reason, guard)

"Py-guard" means a state read in Python right before the write with no await between them.
"SQL-guard" means the UPDATE or its txn rechecks the state. `Store.set_job` (store.py:459-473)
has **no WHERE-state clause**, so every `set_job` is unguarded at SQL level.

**Device lane, `Broker._work` (broker.py:2330-2926)**

Since stage 3a these writes are in `Broker.admit()` (2342-2469), `Broker._finish_failure`
(2721-2798), the kinds' `commit` in `job_kinds.py` with `Broker._finish_success`
(2799-2889); see the note at section 5.

| Line | From→To | Result code | Guard |
|---|---|---|---|
| 2342 | queued→cancelled | `execution_intent_not_available` | Py: 2338 |
| 2357 | queued→cancelled | `session_not_available` | Py: 2349 (after `_expire()`) |
| 2361 | queued→needs_review | `plan_not_available` (+`_cancel_queued`) | Py: 2349 |
| 2368 | queued→cancelled | `principal_or_lease_no_longer_valid` (log_delivery) | Py: 2349 |
| 2383 | queued→cancelled | `principal_or_lease_no_longer_valid` (after auth callback) | **stale**: last read 2349, `principal_valid` callback runs in between |
| 2392/2402/2455 | queued→cancelled via `_cancel_execution_intent` | `flash_binding_invalid` / `collector_binding_changed` / `execution_intent_not_available` | Py inside 398-404 (state ∈ awaiting/queued, plan_hash match) |
| 2459 | queued→cancelled | `session_not_available` | dead (needs `queued_intent is None`, already excluded at 2341) |
| 2469 | queued→running | — | Py: 2433 final check |
| 2721 | running→needs_review | FlashFailure/FlashAdmissionError code | none |
| 2732 | running→needs_review | BuildPreflight/BuildLocation code | none |
| 2745 / 2747 | running→needs_review | source code / `backend_error_no_retry` | none |
| 2760 `finish_fixed_trial` | running→needs_review | failure.code | SQL-guard store.py:532-562 |
| 2771 | running→needs_review | `fixed_trial_persist_failed` (+`execution_uncertain=True`) | none. UNTESTED |
| 2773 | running→needs_review | DeviceOperationFailure code | none |
| 2782 | running→needs_review | `device_disconnected_no_retry` | none |
| 2789 / 2796 | running→needs_review | `source_fetch_failed` / `backend_error_no_retry` | none |
| 2812 | running→succeeded/failed/cancelled | app_session code | none |
| 2819 → store.py:614-687 `finish_firmware` | running→succeeded (+LKG, `device_writes.seq+1`) | `firmware_<op>_completed` | SQL-guard + `guard()` twice inside `BEGIN IMMEDIATE` |
| 2823 → firmware_source.py:149-154 | running→succeeded | source result | Py-guard by `live()` only (no state recheck) |
| 2837 → fixed_diagnostic.py:49-71 → store.py:475-562 | running→succeeded/needs_review | `derby_fixed_trial_verified` / `operation_interrupted_review_required` | SQL-guard |
| 2863 | running→succeeded | test code | none |
| 2875 / 2877 | (running or **already succeeded**)→needs_review | code / `fixed_trial_persist_failed` / `firmware_final_commit_failed` (+latch) | none. **Can overwrite a committed success** if the exception is raised after the commit (for example from flash `commit_success` lines 308-313 after `finish_firmware` committed). SPEC: those statements are dict operations and unlikely to raise. |
| 2915-2918 | — | outer except sets `execution_uncertain=True` only; the row may stay `running` until restart | |

**Device lane, other Broker paths**

| Line | From→To | Reason | Guard |
|---|---|---|---|
| 389 `_cancel_queued` | queued→cancelled | caller reason | Py: 388 |
| 404 `_cancel_execution_intent` | awaiting/queued→cancelled | caller reason | Py: 399-403 |
| 1947 `Broker.cancel` | queued→cancelled | `client_cancelled_queued` | Py: 1945. Does **not** pop `_execution_bindings` / `_flash_bindings` (the worker's `finally` at 2921-2925 does that later). |

**Profile lane (`ProfileJobs`)**

| Line | From→To | Reason | Guard |
|---|---|---|---|
| profile_jobs.py:513 | queued→cancelled | `client_cancelled` | Py: 512 |
| profile_jobs.py:558 | queued→cancelled | `operator_cancelled` | Py: 557 |
| profile_jobs.py:613 | queued→needs_review | `profile_plan_inconsistent` | Py: 609. UNTESTED |
| profile_jobs.py:616 | queued→cancelled | `profile_or_principal_no_longer_valid` | Py: 609 |
| profile_jobs.py:621 | queued→running | — | Py: 609 |
| profile_jobs.py:655 → firmware_source.py:153 | running→succeeded | source result | `live()` |
| profile_jobs.py:658 | running→succeeded | build result | none. Artifacts were **already published** to the filesystem at firmware_build.py:1069-1072, before this commit, so this is not atomic. |
| profile_jobs.py:718 | running→needs_review/cancelled/failed | code / stop reason | none |

**Home recovery (no lease; sets `active_job` itself)**

| Line | From→To | Reason | Guard |
|---|---|---|---|
| home_recovery.py:261 | ∅→queued | via `enqueue_requested` | txn |
| store.py:297-300 (from home_recovery.py:267) | queued→running | — | SQL-guard `WHERE state='queued'` + proof checks, BEGIN IMMEDIATE |
| home_recovery.py:269 | queued/running→cancelled | `home_recovery_not_admitted` | none. This can overwrite `running` if `consume_home_attempt` committed and something after it raised. SPEC: nothing after it in the try can raise. |
| home_recovery.py:295 / 305 / 316 | queued→cancelled | `home_recovery_cancelled` / `_expired` / `_shutdown` | in-memory `e.state=="queued"`. **Practically unreachable**: `request()` sets state `running` immediately at 271. |
| store.py:316-365 `finish_home_recovery` | running→succeeded (`fresh_home_closed`); source reviewed=1 | | SQL-guard + guard() |
| home_recovery.py:396 | running→needs_review | `home_recovery_failed_no_retry` | none |

**Startup and offline:** store.py:166-172 (migration), 179-186 (recover), operator.py:37 (reviewed=1).
`set_stage` (store.py:692) writes only `stage`.

### 1.4 Unguarded-transition summary (inputs for `JobLedger.transition()`)

1. SQL never enforces from-state in `set_job`. Correctness relies on Py-guard plus no-await.
2. 2383 writes after a callback without rereading state. The callback could have cancelled the job
   (for example via `maintenance_tick` → `_cancel_queued`). The result is cancelled→cancelled with
   a different reason (benign overwrite of the reason).
3. The running→terminal writes in `_work`'s except blocks have no state check. 2875/2877 can
   follow a committed success.
4. Profile 658 runs after a non-transactional filesystem publication.
5. Device-lane writes call `_console_state` and the `on_commit` notification. A `JobLedger`
   must keep commit-then-notify ordering (store.py:19-45, 473).
6. Deferred rows (`deferred_jobs`) and tickets have their **own** state machines with
   `WHERE state='pending'|'issued'` guards (continuation.py:110-117, 145-154, 316-329) and are
   not linked to the job row transactionally.

---

## 2. Tickets and the hold

### 2.1 Data

`continuation_tickets` (store.py:124-129): `state` ∈ {`issued`,`consumed`,`void`,`expired`};
`hold` column ∈ {NULL,`none`,`held`,`ended`} (an advisory marker; the real hold is in memory as
`Broker.hold: Hold|None`, broker.py:238). `parent_job_id UNIQUE`. `consumed_by_job` is the
chain link. `chain_started` / `chain_total` are inherited from the ticket the parent consumed.
`fence_seq` holds the device write seq.
Constants (continuation.py:22-27): 8 chars of Crockford base32, `HOLD_SECONDS=60`,
`CHAIN_BUDGET_SECONDS=1800`, `TICKET_TTL_SECONDS=1800`, `MAX_ISSUED_TICKETS=256`;
`MAX_PENDING_DEFERRED=64` (248); `DEVICE_WAIT_SECONDS=1800` (broker.py:69).

### 2.2 Issuance

- Only the surface `enqueue(..., continues=True)` issues tickets (surface.py:441-467), **after** the
  job was queued, in the same synchronous span: `ops["execute_test"]` is an `async def` with no
  internal await, and `_result` is synchronous (server.py:448, 866-881). There is a separate
  SQLite commit. A crash between them leaves a queued job with no ticket, which is harmless.
  `continuation_error` is returned rather than failing the enqueue (surface.py:465-466).
- `Broker.issue_ticket` (broker.py:1089-1114): the principal comes from `execution_requests`.
  For `registered_project` the principal comes from `plan.principal_id`, because `add_job` writes
  no `execution_requests` row. For a deferred child (`job is None`) the principal comes from the
  deferred row. Refused (`continuation_not_supported`) unless the plan has `session_id` or the
  context is registered/`device_deferred`. **Does not check job state.** A ticket for an
  already-terminal job is voided on the next tick.
- `Tickets.issue` (continuation.py:65-108): idempotent per parent. Limit of 256 issued tickets →
  `ticket_limit` (UNTESTED). Count, uniqueness and insert are not one txn. The UNIQUE
  constraint would raise `sqlite3.IntegrityError`, not `TicketError`. SPEC: unreachable without
  concurrency.

### 2.3 Validation and spending

- `peek` (continuation.py:132-143) error mapping: unknown or foreign principal → `ticket_invalid`
  (the same code for both); `consumed` → `ticket_consumed`; `expired` → `hold_expired`;
  `void` or a different epoch → `ticket_void`. Prepare-time (`prepare_deferred`
  broker.py:893, `_prepare_deferred` profile_jobs.py:290) only peeks.
- `consume` (145-154) = `peek` + `UPDATE ... WHERE state='issued'` **without a rowcount check**.
  It is single-use only because there is no await between peek and update.
- Spend sites:
  - (a) `request_device(after=)` broker.py:1515-1523. Runs `maintenance_tick` first, then
    consume, then `put_request`: **two commits**. A crash between them leaves the ticket spent
    with no request.
  - (b) `execute_deferred` broker.py:974-985 and (c) `ProfileJobs._execute_deferred`
    profile_jobs.py:366-382. Each does `deferred.add` (commit), `consume` (commit),
    `note_consumer` (commit), with a compensating `deferred.remove` only on `TicketError`.
    A crash after `consume` and before `note_consumer` leaves the `consumed_by_job` link NULL,
    which breaks `chain_jobs`/progress. Restart cancels the deferred row.
  - (d) `consumed_by_job` for a work-session lease is set at the **first `execute` under that
    lease** (broker.py:1893-1894, guard `consumed_by_job IS NULL`), whatever that job is.
- Spent-ticket handling: a consumed ticket is never re-evaluated by `_tickets_tick`, which iterates
  `issued()` only. `mark_state` is a no-op on non-issued rows (WHERE state='issued').
  `cancel_ticket` on a consumed ticket raises `ticket_consumed` **before** it touches the hold
  (broker.py:1144-1145).

### 2.4 Hold lifecycle (`_tickets_tick` broker.py:1012-1078, called from `maintenance_tick` 664)

`maintenance_tick` runs in this order: work expiry → waiter expiry/revocation → lease
shortening/expiry → `_release_finished_job_lease` → `_prune_execution_intents` → `_deferred_tick`
→ `_tickets_tick` → grant. The hold decision therefore always comes before the grant in the same
tick.

Releasing an existing hold (1017-1025, `_release_hold` 997-1010): missing row, monotonic
`now >= hold.deadline`, budget == 0, or `_execution_uncertain()` releases it. The ticket becomes
`expired` (`hold_expired`) if it is still issued, and the hold marker becomes `ended`. Waiters
holding that ticket move to the tail.

For each issued ticket:
- parent row missing: deferred parent pending or promoted → skip; otherwise → `void chain_broken`.
- parent queued/running/awaiting → skip. Parent not succeeded → `void ticket_void`.
- `hold` not NULL: if `none` and wall-clock `created` age exceeds 1800 → `expired hold_expired`
  (TTL counts from issue, not from parent finish). Tickets with `held`/`ended` while still
  `issued` have no TTL. SPEC: unreachable, because every path that sets `ended` also marks
  the ticket.
- parent plan without `session_id` (registered fetch/build) → `hold='none'`, no fence, no hold.
- `set_fence(device_write_seq)` (1050, first time only).
- `_execution_uncertain()` → `void`.
- another hold active or budget exhausted → `hold='none'`.
- busy, device queue not empty, or `_execution_intents` present → **skip and retry next tick**.
- current session exists and is not the parent's lease → `expired` + `ended`.
- wall-clock seconds since `jobs.finished` (UTC text, 1 s granularity) ≥ 60 → `expired`.
- otherwise `_end_ownership("released","chain_hold")` (skip if it raises), then
  `Hold(ticket, principal, now + 60 - elapsed)` and marker `held`.

Mixed clocks: the hold deadline uses the injected monotonic `Broker.clock`. Elapsed time, TTL and
budget use `time.time()` against SQLite `CURRENT_TIMESTAMP`, so tests cannot inject them (see
`tests/test_continuation.py::test_chain_budget_stops_the_reservation`, which edits the DB
directly). This differs from DESIGN §13.3(12) ("read one clock once").

Hold effects:
- `acquire` → `device_busy` (1297).
- Grant: only waiters whose `ticket_id == hold.ticket_id` (675-682). The hold ends when that
  waiter is granted (712-714, 833-835) or refused (798-800).
- `cancel_ticket` drops the hold without marking it expired; the ticket becomes `void`
  `client_cancelled` (1146-1152).
- Not cleared by `close()` (in memory only; restart loses it).
- No operator release of the hold (DESIGN notes it is unimplemented).
- `_end_ownership` does not look at `self.hold`.

**FIFO ordering:** `self.waiters` is a list. Append is the default. Index 0 is used when a follower
consumes the *held* ticket (`request_device` 1523-1528; deferred device wait 780-783).
`_release_hold` moves those followers to the tail. `operator_reorder_wait` can move any entry,
including job waits (1645-1669). **No sort-insert by chain key** (DESIGN §13.2(1) is not
implemented). Eligibility time is the order.

**Consumed-before-parent-finished nuance (important):** a ticket spent while the parent is still
running is never `issued` at parent finish. As a result:
- (i) no hold starts.
- (ii) the follower gets no priority. It is appended normally.
- (iii) **`fence_seq` is never set, so the device-write fencing check in `_grant_job_lease`
  (811-817) is skipped.**

The work-session `request_device(after=)` path has **no fence check at all**. DESIGN §13.3(10)
says the check has no bypass, so this is a gap. UNTESTED.

### 2.5 Budget

`budget_remaining` = 1800 − (wall now − `chain_started`), where `chain_started` is the first issue
in the chain (continuation.py:231-234). This is not occupancy time (DESIGN §13.2(8) differs; the
implementation note documents the simplification). When the budget runs out, the hold is released
or not started. The follower still runs, just without priority.

### 2.6 Deferred jobs (ahead of time)

- Device kinds: `prepare_deferred` (884-934) writes a plan `execution_context=device_deferred`,
  `version=2`, `lease_seconds` (default 60), with no `session_id`. `execute_deferred` (936-988)
  writes a `deferred_jobs` row and spends the ticket. There is no `jobs` row yet.
- `_deferred_tick` (740-747) → `_advance_device_deferred` (749-785):
  - wait cancelled or expired → `cancelled wait_cancelled` (UNTESTED).
  - parent pending → wait.
  - parent broken or plan hash mismatch → `cancelled chain_broken`.
  - waiters ≥ max → keep waiting.
  - `device_requests` ≥ 10000 → `cancelled history_full_archive_offline`.
  - otherwise create a `DeviceWait(job_id=..., session_id="job:"+id)` with a 1800 s deadline and
    a `device_requests` row (session NULL, state waiting).
- `_grant_job_lease` (787-864):
  - `refuse()` with `job_not_available`, `principal_not_authorized` or `device_state_changed`.
  - Creates a job-only `Session` (`job_id` set, no capability returned) and sets `self.session`.
  - Then calls `self.prepare()` and `self.execute()` **re-entrantly**. Both call `_lease` →
    `_expire` → `maintenance_tick`, so this is a **nested tick** inside the outer tick.
    `_release_finished_job_lease` tolerates it (873-875).
  - On exception: deferred `cancelled <code>` and `_end_ownership("released","promotion_failed")`
    (an ownership error is swallowed).
  - On success: deferred `promoted` + `resolved_plan_hash`.
- `_release_finished_job_lease` (866-882) ends the job lease once the job is terminal and the
  device is not busy. Exception: a succeeded job with an issued, not-yet-decided ticket is left to
  `_tickets_tick`, which converts the lease into the hold.
- Registered build after fetch: `ProfileJobs.promote` (387-434) prepares and executes with the
  receipt from the parent's diagnostic. `queue_full` is transient. Other errors → cancelled with
  that code. `ValueError` → `chain_broken`.
- Restart: `Store.recover` → `Tickets.void_stale` (`void broker_restart`, epoch ≠) →
  `Deferred.cancel_stale` (`cancelled broker_restart`) (broker.py:304-306). There are three
  separate commits; they are idempotent.
- SPEC/observed: a job wait in `waiting`, or a job lease in `owning`, at restart becomes a
  `device_requests needs_review reviewed=0` row via `recover()`. That row shows as historical
  review, and **blocks `home_recovery`** (home_recovery.py:181-186). It needs an offline
  `review-request`. UNTESTED for job waits.

---

## 3. Ownership (`self.session`)

### 3.1 Assignments

| Line | What |
|---|---|
| broker.py:232 | init `None` |
| broker.py:600 | `_end_ownership` → `None` (the only clear) |
| broker.py:715 | FIFO grant for a work-session waiter (in `maintenance_tick`) |
| broker.py:836 | job-only lease for a deferred device job (`_grant_job_lease`) |
| broker.py:1313 | immediate `acquire` (refused if session, waiters, hold or busy) |

Deadline mutations: 640 and 653 (principal or work-session revoked → `min(deadline, now)`), and
1334-1337 (`renew` replaces the deadline, capped by work deadline). There are no other writers in
`src/` (`derby_fixed.py:334` is an unrelated attribute). Readers outside broker.py:
home_recovery.py:134 (requires `None`), operator_live.py:107 (`_next_expiry` of the operator
event stream; was :128 under the long poll), operator_timeline.py:613.

### 3.2 `_end_ownership` (broker.py:587-604)

1. Returns if there is no session.
2. Raises `active_job_cannot_release` if `_physical_busy()`.
3. Cancels **all** `_execution_intents` with the release reason.
4. `set_request(state, reason)` and updates the in-memory request.
5. Sets `session=None`, clears capture delivery consents, bumps the queue revision, notifies, and
   logs EXPIRED or RELEASED.

It does not touch `self.queue` (the cancelled rows are skipped by the worker at 2338), `self.hold`,
or work-session state.

`_physical_busy` (552-565): `active_job`, `backend.hardware_connected` or `active`, unowned
source/build activity, build process uncertainty or output uncertainty, `profile_jobs.uncertain`,
flash active or uncertain, `backend.ownership_uncertain`. Profile-owned source/build activity is
deliberately **not** busy, because those jobs hold no USB.

### 3.3 "Never release device ownership while owned processes or device work remain active"

Callers and their guards:

| Caller | Guard / behaviour |
|---|---|
| `release` 1344 | explicit busy check, then `_cancel_queued`, then `_end_ownership` |
| `close_work_session` 1570 | explicit busy check |
| `operator_release_idle` 1675 | explicit busy check |
| maintenance expiry 655-657 | `_cancel_queued("lease_expired")` always; end ownership **only if not busy**, so an expired owner stays pinned |
| `_tickets_tick` 1071-1074 | try/except: no hold if busy |
| `_grant_job_lease` 859-862 | error swallowed |
| `_release_finished_job_lease` 869 | returns if busy |
| `close()` 353-360 | raises `device_close_unconfirmed`, keeping locks and store open |

`_lease` raises `lease_expired_active_job_retains_ownership` after the deadline (1230-1231).
`active_job` is cleared only when the backend, source, build and flash are all quiescent
(2897-2905); otherwise it stays set, and stays busy, until a later job's finally block or a
restart. SPEC: no other code clears a stuck `active_job`.

Pinned by: `tests/test_work_sessions.py::test_active_expiry_pins_owner_cancels_jobs_and_then_grants_fifo`,
`::test_unconfirmed_backend_hold_prevents_handover_even_without_job`,
`tests/test_session_safety_review.py::test_active_session_close_is_rejected_without_interrupting_worker`,
`::test_uncertain_backend_close_never_hands_device_to_waiter`, `::test_scheduler_checks_backend_even_without_active_job`,
`::test_invalidation_pins_complete_async_workflow_and_cancels_pending`,
`tests/test_review_invariants.py::test_review_expiry_cancels_pending_but_pins_active_owner`,
`tests/test_operator_http.py::test_operator_cannot_release_an_active_job`,
`tests/test_capture_broker.py::test_capture_pixels_unavailable_and_owner_held_until_close_completes`,
`::test_capture_unconfirmed_close_quarantines_pixels_and_preserves_device_lock`.
UNTESTED: refusal of `chain_hold` while busy (1071-1074); `promotion_failed` release path;
`job_complete` release; job-lease expiry during a long running deferred job.

SPEC risk: a job lease has a fixed `lease_seconds` (≤300, default 60) and its capability is never
returned, so it **cannot be renewed**. `running_authorized` and `flash_live` check the lease
deadline (`_execution_lease_binding_live` 430), so a deferred flash or DERBY that outlives its
job lease would be interrupted and moved to review.

---

## 4. Cancellation mechanisms

**Client-facing:**

| # | Entry | file:line | Cancels | Ownership check |
|---|---|---|---|---|
| 1 | `Broker.cancel` (tool `cancel target=job` with session; op `cancel_job`) | broker.py:1906-1951; server.py:1196-1208 | queued → `cancelled client_cancelled_queued`. Running: app_session → `cancelled` event; SOURCE → `firmware_source.request_stop`; DERBY/DERBY_FIXED → `backend.request_smoke_stop`; BUILD/flash → `controller.request_stop(job_id)`. Other running kinds (hello, observe, restart, capture, diagnostics) → `only_queued_jobs_can_be_cancelled`. | lease capability (`_lease`); `plan.session_id == lease.id`; server `owned()` → `require_owner` (origin + principal) when `auth_required` |
| 2 | `Broker.cancel_job_only` (cancel without session) | broker.py:990-995; surface.py:706-713 | delegates to #1 via the job lease | `lease.job_id == job_id` and `job_principal == caller`. UNTESTED |
| 3 | `Broker.cancel_deferred` | broker.py:1128-1138; surface.py:698-704 | pending deferred row → `cancelled client_cancelled_waiting`, ends the job wait | deferred row principal; pending only |
| 4 | `Broker.cancel_ticket` | broker.py:1140-1153; surface.py:680-688 | issued → `void client_cancelled`; drops the hold if it matches | ticket principal (`ticket_invalid` otherwise) |
| 5 | `Broker.cancel_device_request` | broker.py:1536-1557 | WAITING request → `cancelled client_cancelled_wait` | resume token + principal + `session_id` match |
| 6 | `close_work_session` / `release` | broker.py:1559-1581 / 1342-1349 | `_cancel_queued` + end ownership + cancel wait | resume token+principal / lease capability; refused if busy |
| 7 | `ProfileJobs.cancel` | profile_jobs.py:505-517; server.py:1199-1205 | queued → `cancelled client_cancelled`; running → `_stop` (stops dict + source/build `request_stop`) | `_authorized` + `_plan` (principal and origin bound). Returns `stop_requested: True` even when `request_stop` refused after commit |
| 8 | `HomeRecovery.cancel` | home_recovery.py:283-296; server.py:902-908; surface.py:721-729 | sets the stop event; queued branch effectively dead | plan_hash entry + `plan.principal_id` |

**Operator (local HTTP, `operator_api.py:358-396`, local+control auth):**

| # | Entry | file:line | Cancels | Ownership check |
|---|---|---|---|---|
| 9 | `ProfileJobs.operator_cancel` | profile_jobs.py:539-563 | registered_project queued/running only | local origin + epoch + plan_hash |
| 10 | `operator_cancel_wait` / `operator_release_idle` | broker.py:1634-1643 / 1671-1680 | a wait (including job waits) / an idle lease | exact lease id; busy refused |

**Internal / automatic:**

| # | Entry | file:line | Cancels / effect |
|---|---|---|---|
| 11 | `_cancel_queued(reason)` | broker.py:376-395 | all device-lane intents + all queued rows in the asyncio queue; preserves the shutdown sentinel. Reasons: `broker_shutdown` 348, `lease_expired` 655, `session_released` 1346, `work_session_closed` 1572, `operator_released_idle` 1677, `recovery_review_required` after every needs_review in `_work`. Does **not** touch the profile queue or deferred rows. |
| 12 | `_cancel_execution_intent` / `_prune_execution_intents` | broker.py:397-409 / 521-529 | `execution_intent_expired` / `execution_intent_not_available` |
| 13 | `maintenance_tick` waits | broker.py:621-633, 689-697 | `wait_expired` / `principal_not_authorized`; lease shortening on revocation 634-657 |
| 14 | Deferred/ticket automatic | 754, 760, 765, 794-800, 858 (broker.py); profile_jobs.py:392-433; tickets void/expired in `_tickets_tick` | |
| 15 | Authority-loss interruption | `running_authorized` 2495-2529, `flash_live` 2549-2603 (since stage 3b: the `RUNNING` / `FLASH_IO` / `FLASH_COMMIT` rows of `RULES` in `execution_authority.py`, evaluated by `ExecutionAuthority.check`), `ProfileJobs._watch` 588-598 (`execution_deadline_expired` / `profile_or_principal_no_longer_valid`), build monitor firmware_build.py:~556-560 | |
| 16 | Shutdown | `close()` 324-348: app runs `cancelled.set()`, `profile_jobs.close` (`_stop broker_shutdown`), `firmware_build.request_stop()`, `firmware_flash.request_stop()` with no job id, `home_recovery.close`, waits `broker_shutdown` | |
| 17 | Startup | `recover`, `void_stale`, `cancel_stale`, migration (section 1.2) | |

Stop primitives differ per executor:
- build: `request_stop` returns False after `_committed_job` (firmware_build.py:528-533).
- source: False after commit, else returns `active` (firmware_source.py:32-36).
- flash: queues cancellation for a non-current job id (firmware_flash.py:216-225).
- app: `asyncio.Event`.
- DERBY: `backend.request_smoke_stop`.
- home recovery: `threading.Event`.

---

## 5. Admission checks in `Broker._work` (order)

> **Since stage 3a** (DESIGN-REDESIGN §5.5) `Broker._work` is split; the line numbers in this
> section are still those of 5d79d2d. Steps 2-15 are `Broker.admit()` (same order, no `await`);
> step 16's executors are `JOB_KINDS[test].execute` in `job_kinds.py` and the post-result
> validation is `Broker._accept_result`; step 17 is `Broker.finish()` (`_finish_failure`, or
> `_finish_success` with the kind's synchronous `commit`) and `Broker._close_admitted` (the
> former inner `finally`). The outer `except`/`finally` stays in `_work`. The nested closures
> became methods that read the admitted job: `_running_authorized(run)`,
> `_flash_live(run, committing)`, `_derby_diagnostic(run, diagnostic)`, `_on_stage(run, name)`.
>
> **Since stage 3b** (DESIGN-REDESIGN §5.6) steps 13-14 are `ExecutionAuthority.check(Checkpoint.DISPATCH, ...)`
> (`execution_authority.py`): the second callback and the composite, in the same order. Step 4's dead
> flag was removed (it was always True after step 3).

1. 2334: sentinel `None` → return.
2. 2337-2339: `self.job(job_id)` (raises `job_not_found` → outer except → **`execution_uncertain=True`**); state ≠ queued → skip silently.
3. 2340-2343: no `ExecutionIntent` → `cancelled execution_intent_not_available`.
4. 2344-2346: `requires_current_principal` is always True (dead flag).
5. 2347-2348: snapshot `queued_lease = session.id`; `self._expire()` runs a full maintenance tick, which can cancel the job or expire the lease.
6. 2349-2350: reread; not queued → skip.
7. 2351-2358: no session, lease id changed, lease expired, or closing → `cancelled session_not_available`.
8. 2359-2363: plan missing or `plan.session_id ≠ session.id` → `needs_review plan_not_available` + `_cancel_queued`. Does **not** set the uncertainty latch. UNTESTED.
9. 2365-2370: FIRMWARE/RESTART `log_delivery` must match the current lease → else `cancelled principal_or_lease_no_longer_valid`. UNTESTED by code name.
10. 2371-2385: `principal_valid` callback; then `session is lease` and not expired → else the same cancel.
11. 2389-2393: flash `validate_binding` → `flash_binding_invalid` (`tests/test_flash_policy.py`).
12. 2394-2403: fixed collector `prepare_collector` re-derivation → `collector_binding_changed`. UNTESTED by code.
13. 2404-2417: second `principal_valid` callback.
14. 2422-2451: callback-free composite. Covers:
    - authorization, and the `principal_valid` identity being unchanged
    - `_runtime_binding` unchanged; flash `live_binding`; `collector_live`
    - both locks held
    - row still queued with the same hash; plan equal and digest equal; epoch
    - session identity and id; finite and unexpired deadline
    - not closing; not `_execution_uncertain`; not `_physical_busy`; not `hardware_connected`
    - `_execution_intent_live`

    Any failure → `_cancel_execution_intent(... execution_intent_not_available)`.
15. 2462-2469: pop intent; `firmware_build.begin()` / `firmware_source.begin()`; `active_job=job_id`; `set_job running`.
16. Executor dispatch 2537-2628. Per-kind pre-checks:
    - flash principal (2546)
    - build principal (2611)
    - SerialBackend for DERBY (2617)

    Post-result validation 2629-2719:
    - capture/derby/fixed/observation shape
    - retains private outputs only when not closing
17. Failure mapping 2720-2798 (all → needs_review + `_cancel_queued`); success commit 2799-2889; finally 2890-2914 (conditional `active_job=None`, `home_recovery.record_closed`, `_expire()`); outer except 2915-2919 latch; finally 2920-2926 pops consents/bindings/intents and `task_done`.

Dead code to drop deliberately: `local_home_probe` is always None (2386, 2452-2453, 2891-2893);
`capture_target` is always `""`; the 2458-2460 branch.

Pinned by:
- `tests/test_queued_principal_recheck.py::test_explicit_queued_job_rechecks_current_principal_before_start`
- `tests/test_pending_execution.py::test_queued_request_rechecks_authority_before_worker_dispatch`, `::test_auth_callback_races_cannot_start_a_queued_operation`, `::test_cancelled_job_and_released_lease_never_replay`, `::test_lease_renewal_does_not_extend_original_execution_deadline`, `::test_bounded_queue_cancel_and_no_second_operation`, `::test_request_callback_races_cannot_enqueue`
- `tests/test_current_state_admission.py::test_current_uncertainty_after_preparation_or_enqueue_blocks_io`, `::test_worker_state_failure_latches_current_uncertainty`
- `tests/test_work_sessions.py::test_prestart_invalidation_cancels_popped_job_and_hands_over`
- `tests/test_session_safety_review.py::test_invalidation_before_worker_starts_cancels_without_replay`
- `tests/test_flash_lease_renewal.py::*`
- `tests/test_worker_callback_bindings.py::*`
- `tests/test_flash_binding.py` (`execution_intent_not_available`)
- `tests/test_review_invariants.py::test_review_expired_lease_does_not_eat_shutdown_sentinel`

`Broker.execute` admission (1811-1901, for comparison):
- plan digest; lease; epoch and session; backend and device; log_delivery
- dedupe by job_id (returns the existing row even if it is terminal)
- uncertainty; capacity (`queue.qsize()+awaiting ≥ max_queue`); history cap
- `PlanApprovals.validate_current`; flash `prepare_binding`; principal callback
- callback-free final composite (1875-1891)
- `enqueue_requested` (txn) → `note_consumer` → intent and bindings → `queue.put_nowait`

---

## 6. Tests that pin these behaviours

**Tickets / hold:**
- `tests/test_continuation.py::test_success_starts_hold_and_only_the_holder_may_take_the_slot`
- `::test_ticket_is_single_use_and_owned_by_the_parent_principal`
- `::test_hold_expires_after_idle_period_and_late_ticket_is_refused`
- `::test_failed_or_cancelled_parent_voids_its_ticket`
- `::test_cancel_ticket_releases_the_hold`
- `::test_chain_budget_stops_the_reservation`
- `::test_progress_follows_the_parent_links_and_hides_nothing_aggregate`
- `::test_restart_voids_unspent_tickets`
- `tests/test_chain_http.py::test_continuation_through_the_six_tools`, `::test_rules_for_after_and_wrong_tool`

**Deferred device:**
- `tests/test_deferred_device.py::test_scenario_other_chain_holds_then_deferred_job_runs_without_a_client_lease`
- `::test_job_lease_becomes_the_reservation_when_the_job_names_a_child`
- `::test_parent_failure_cancels_the_waiting_job`
- `::test_device_write_between_parent_and_child_is_refused`
- `::test_unchanged_device_lets_the_continuation_run_even_after_hold_expiry`
- `::test_waiting_job_can_be_cancelled_and_leaves_the_queue`
- `::test_ticket_is_owner_bound_and_single_use_for_device_jobs`
- `::test_session_and_after_are_mutually_exclusive_and_backup_cannot_be_deferred`

**Deferred build:**
- `tests/test_deferred_build.py::test_build_waits_then_runs_the_exact_fetched_source`
- `::test_failed_parent_cancels_the_waiting_build`
- `::test_waiting_build_can_be_cancelled_and_never_queues`
- `::test_build_may_continue_a_chain_before_its_parent_finishes`
- `::test_restart_cancels_waiting_builds`

**Restart / migration:**
- `tests/test_explicit_request_store.py::*` (5 tests)
- `tests/test_work_sessions.py::test_restart_invalidates_secrets_and_marks_ownership_and_waiting_review`, `::test_clean_shutdown_does_not_leave_ownership_review`
- `tests/test_session_safety_review.py::test_restart_quarantines_every_waiting_or_owning_request`, `::test_restart_has_no_resume_no_grant_and_no_automatic_replay`
- `tests/test_review_invariants.py::test_review_recovery_never_replays_and_allows_fresh_acquire`
- `tests/test_capture_broker.py::test_capture_restart_retains_history_but_never_pixels_or_pending_replay`
- `tests/test_profile_jobs.py::test_restart_does_not_replay_or_clear_old_review`

**Cancellation:**
- `tests/test_broker.py::test_cancel_only_queued_and_capacity`
- `tests/test_capture_broker.py::test_capture_cancelled_queue_never_runs_or_replays`, `::test_capture_shutdown_waits_for_active_close_and_cancels_queued_work`
- `tests/test_profile_jobs.py::test_running_profile_stop_is_terminal_without_review`, `::test_queue_cancel_read_scope_config_and_old_review_boundaries`
- `tests/test_operator_job_cancel.py::*` (6 tests)
- `tests/test_home_recovery.py::test_cancellation_or_loss_during_io_never_reinputs_or_commits`, `::test_close_in_progress_retains_reservation_even_after_cancel`
- `tests/test_flash_lease_renewal.py::test_renewal_cannot_revive_cancelled_running_flash`, `::test_renewal_cannot_revive_explicitly_cancelled_queued_flash`
- `tests/test_work_sessions.py::test_wait_cancellation_expiry_and_session_expiry`, `::test_close_waiting_and_idle_sessions_only`, `::test_operator_wait_only_controls_and_idle_owner_release`
- `tests/test_session_safety_review.py::test_cancelled_wait_cannot_reappear_or_cancel_new_request`, `::test_late_wait_cancel_cannot_release_promoted_lease`

**FIFO / grant:**
- `tests/test_work_sessions.py::test_same_key_distinct_logical_sessions_fifo_and_reconnect`, `::test_stale_operator_reorder_never_moves_any_waiter`, `::test_reorder_revision_detects_aba_and_restart_epoch`, `::test_periodic_maintenance_advances_without_client_poll`, `::test_slow_identity_lookup_cannot_grant_an_expired_wait`, `::test_revoked_waiter_and_owner_are_failclosed`
- `tests/test_session_safety_review.py::test_revoked_first_waiter_is_skipped_without_blocking_valid_successor`, `::test_waiter_and_live_session_limits_are_enforced`

**Atomic commits:**
- `tests/test_home_recovery.py::test_exact_recovery_records_atomic_resolution_and_cannot_repeat`, `::test_resolution_audit_failure_rolls_back_the_source_review`, `::test_consumption_survives_fresh_store_and_manager`
- flash LKG tests (`test_flash_lkg_store.py`, `test_flash_lifecycle.py`; not inspected in detail)

### Untested invariants and reason codes

grep of `tests/` found nothing for these:

- **Cancellation paths:** `cancel_job_only`; `operator_released_idle`, `work_session_closed`, `session_released` as reason strings.
- **Job-lease lifecycle:** `job_complete`, `promotion_failed`, `wait_cancelled`, `chain_hold` refusal while busy; job-lease expiry during long running deferred work.
- **Ticket limits and expiry:** `ticket_limit` (256), `MAX_PENDING_DEFERRED` / `queue_full` for deferred jobs, `TICKET_TTL` (1800 s for hold=`none`).
- **Admission reason codes:** pinned since stage 0.5 by `tests/test_stage05_admission.py`.
- **Failure codes:** `profile_plan_inconsistent`; home-recovery `cancelled` / `expired` / `shutdown` job writes.
  (`fixed_trial_persist_failed` and `firmware_final_commit_failed` are pinned since stage 3a by
  `tests/test_stage3a_fixed_trial_persist.py` and `tests/test_stage3a_finish_characterization.py`.)
- **Fencing:** no test that fencing is skipped for early-consumed tickets, or for the `request_device(after=)` path.
- **Hold ordering:** hold not started while queue or intents are non-empty, then retried next tick; the hold priority move to the tail on release (`_release_hold` reordering).
- **Restart leftovers:** a job wait that is `waiting` at restart becomes a `device_requests` needs_review row and blocks home recovery.
- **Not implemented, so not tested:** DESIGN §13.3 invariants 7 ("needs_review blocks other chains until review") and 16 (single terminal exit).
- **Covered only indirectly:** `void_stale` (via the restart test) and `cancel_stale` (one deferred-build test).
- **Partly covered:** the no-await serialization assumption (§13.3(12)). Since stage 3a,
  `tests/test_stage3a_sync_sections.py` checks in the AST that `admit`, `finish` and every
  `@sync_section` function contain no `await`/`async with`/`async for`, and that `_work` awaits
  only the queue, the flash-binding prefetch and the executor. Other check-then-write functions
  are covered only by `inspect` in `tests/test_stage05_admission.py` and by race tests.
