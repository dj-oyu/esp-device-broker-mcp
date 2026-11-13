# Contract inventory — redaction, public errors, auth, child env

Source: `main` @ 5d79d2d (0.13.0), read-only. Paths relative to repo root `src/cardputer_mcp/` unless prefixed `tests/`.
Labels: **[obs]** = read in code; **[verified]** = reproduced by a short local Python call (no server, no hardware);
**[spec]** = speculation / inference, not confirmed.

---

## A. Redaction and public errors

### A1. Exception → public error ladders (the five the gate will replace)

Stage 3c (2026-10-25): the four `server.py` ladders below are now `surface._result` / `surface._result_async`
(both `public_errors.refusal`); `server.py:<line>` references in this file are 5d79d2d lines. See
`surface.md` §0.1 for where each former op lives.

All five are identical in shape: `BrokerError → str(exc)` passthrough; `SourceError → exc.public()`;
any other `ValueError → public_reason(exc)` (allowlist, else `invalid_request`). Everything else propagates.

| Site | Scope | Notes |
|---|---|---|
| `server.py:448-456` `_result()` | wraps almost every op | canonical ladder |
| `server.py:705-710` | `read_source_resources` | inline copy, same order except SourceError first (no behavioral diff — disjoint classes) |
| `server.py:826-831` | `prepare_test` (test=SOURCE path only) | inline copy |
| `server.py:1090-1095` | `read_build_logs` | inline copy |
| `surface.py:300-312` `_convert()` | building internal request models from the 6-tool requests | **different**: pydantic `ValidationError` → `public_reason(ctx.error)` if allowlisted, else `argument_invalid` + `field` = first str in `loc` |

Other ladders that must keep behaviour (not in the user's list):
- `server.py:1166-1175` `read_capture`: catches **only** `BrokerError` → `is_error=True` CallToolResult with `{"ok":false,"error":str(exc)}`. Any other exception falls to SafeMCPServer (below). [obs]
- `surface.py:462-466` `enqueue`: `BrokerError` from `issue_ticket` → `continuation_error: str(exc)` merged into an otherwise successful body. [obs]
- `surface.py:681-688, 707-712`: cancel ticket / cancel_job_only → `_refuse(str(exc))`. [obs]
- `surface.py:502-507, 516-519, 698-703`: `BrokerError` swallowed (deferred lookups; fall through to normal handler). [obs]
- `SafeMCPServer.call_tool` `server.py:169-192`: pre-validates the 6 tools via `input_diagnostics.validate_arguments`; **any** exception from SDK/handler (`ToolError`, `UnexpectedToolError`, KeyError, sqlite3.Error, …) → `{"ok":false,"error":"invalid_tool_request","reason":"invalid_arguments","fields":[]}`, `is_error=True`. [obs; SDK `mcpserver/server.py:524-539` raises rather than formatting, confirmed in venv]
  - Contract to keep: no exception text ever. Note that unexpected internal crashes are currently **misreported as `invalid_arguments`** — a gate may reclassify (e.g. `internal_error`), but that is a visible behaviour change; decide explicitly.
- `input_diagnostics.py:71-104`: fixed `MODEL_REASONS`/`TYPE_REASONS`; field names filtered through `SAFE_FIELDS` (schema-owned names only). [obs]
- `operator_api.py:399-407`: `BrokerError → 200 {"ok":false,"error":str(e)}`; `ValueError → 400 invalid_request` (**no allowlist**: everything collapses); `Exception → 200 operator_request_failed`. `_job_logs` (122-130) collapses everything except `not_authorized` to `unavailable`. [obs]
- `plan_approval.py:47-56` `_fixed_errors()`: non-BrokerError → `plan_validation_unavailable`; `validate_current` inner preflight `except Exception → backend_preflight_failed` (559-560). [obs]
- `broker.py:1752-1757` prepare: `BuildLocationError → exc.code`; `FlashFailure|FlashAdmissionError → str(exc)`; other ValueError → `backend_preflight_failed`. [obs]
- `stdio_adapter.py:121-144`: all upstream/transport exceptions → fixed `stdio_adapter_*` codes; `create_stdio_server` returns them as text (`:217-223`) or `MCPError(INTERNAL_ERROR, code)` for list (`:209-215`). [obs]
- `cli.py:430-441`: `AuthError|BrokerError|OperatorClientError → "Request failed: {error}"` on stderr; other → fixed sentence. `cli.py:396-398` stdio errors fixed codes. [obs]

### A2. `public_reasons.py`

- `PUBLIC_REASON_CODES` (`:10-86`) — exact-string allowlist; `public_reason()` (`:110-115`) requires `len(exc.args)==1` and `str`; anything else → `invalid_request`.
- `SUPPRESSED_REASON_CODES` (`:90-107`) — **not referenced at runtime anywhere** in `src/`. Its only consumer is `tests/test_public_reasons.py` as a classification ledger (`:43-45`, `:57-59`): every literal `raise ValueError("code")` in `REQUEST_PATH_MODULES` (`tests/test_public_reasons.py:16-31`) must be in exactly one set. Preserve as a review ledger, or fold into the gate's classification table.
- Ledger blind spots [obs]: the regex (`test_public_reasons.py:32`) only sees `raise ValueError("…")` in 14 listed modules. It does **not** scan `app_sessions`, `app_events`, `continuation`, `home_recovery`, `collector_binding`, `source_snapshot`, `firmware_source`, `flash_*`, nor non-ValueError subclasses.

### A3. `BrokerError` passthrough — public by convention only

`BrokerError` (`broker.py:72-73`) is "fixed strings" by docstring; nothing enforces it. All direct constructions are string literals except these **converters that bypass the ValueError allowlist** by re-wrapping `str(exc)`:

| Site | Source exception | Codes that become public today |
|---|---|---|
| `broker.py:895, 985, 1114, 1521`; `profile_jobs.py:292, 382` | `TicketError` (`continuation.py:31`) | `ticket_limit, ticket_invalid, ticket_consumed, hold_expired, ticket_void, queue_full` (`continuation.py:84-289`) |
| `broker.py:2207` | `ValueError` from `AppRun.command` | `stale_boot, stale_run, stale_session, conflict, app_input_stopped, action_not_in_plan, out_of_order, limit, queue_full, rate_limit` (`app_sessions.py:111-151`) |
| `broker.py:2216` | `ValueError` from app events read | `app_events_not_available, cursor_ahead` (`app_events.py:52,59`) |
| `broker.py:1753` | `BuildLocationError.code` | build_locations codes |
| `broker.py:1755` | `FlashFailure`/`FlashAdmissionError` | flash codes (convention-only, `flash_lifecycle.py:63`, `flash_manifest.py:11`) |
| `server.py:1056` | `ValueError` from `FirmwareBuild.read_logs` | `build_log_not_available, cursor_ahead` (currently literal; any future non-literal text would leak verbatim) |

None of the TicketError/app codes are in `PUBLIC_REASON_CODES`. A single gate that applies the allowlist to `BrokerError` too would turn them into `invalid_request` — **behaviour change**; `ticket_invalid` is pinned by `tests/test_chain_http.py::test_rules_for_after_and_wrong_tool`.

`SourceError` (`source_snapshot.py:182-219`): code forced through `SOURCE_ERROR_CODES` by `safe_source_code`; `public()` adds fixed `explanation` for 5 codes and bounded `scan` counters for `CacheScanLimit`. `server.py:1007-1009` additionally copies `exc.code` / `scan` into `source_cache` usage on ancillary failure.

`RecoveryPointRefusal` (`broker.py`, a `BrokerError`): a flash/restore prepare refused for its recovery
point (`verified_lkg_or_initial_backup_required`, `successful_owned_backup_required`,
`backup_registered_device_mismatch`) keeps its code and `to_public` adds `flash_baseline`, `lkg_recorded`,
a fixed `next_step` id and its fixed `explanation` (`public_reasons.recovery_point_details`). The gate
re-validates the exact key set, `FLASH_BASELINES`, a bool and `RECOVERY_POINT_NEXT_STEPS`; anything else
yields the bare code. No job id, principal or path is ever included.
Pinned by `tests/test_flash_recovery_point_refusal.py`.

### A4. Gate bypass found [verified]

`surface._convert` echoes caller-chosen dictionary keys: `StatusRequest.cursor` is `dict[str, int|str]` (`surface.py:155`), then `LogCursor.model_validate(cursor)` / `AppEventCursor.model_validate(cursor)` runs **inside** `_convert` (`surface.py:551-553, 565, 578`). An extra key yields `loc=("<caller key>",)` and `_refuse("argument_invalid", "<caller key>")`. Reproduced locally:
`_convert(lambda: LogRequest(cursor=LogCursor.model_validate({'epoch':'a'*32,'sequence':1,'C:/secret/path':1})))` → `{'ok': False, 'error': 'argument_invalid', 'field': 'C:/secret/path'}`.
It reflects only the caller's own input, but it violates the `input_diagnostics` rule "caller-chosen dictionary names are never echoed" (`input_diagnostics.py:97-100`). No test covers it. The gate should filter `field` through `SAFE_FIELDS`.

### A5. Every data egress and what is redacted

| Egress | Mechanism | What is kept out | Where |
|---|---|---|---|
| MCP tool results (6 tools) | ladders above; `_wrap` JSON (`surface.py:394-398`) | exception text, paths, tokens | A1 |
| MCP validation errors | `SafeMCPServer`, `input_diagnostics` | raw arguments | `server.py:169-192` |
| HTTP 4xx bodies (MCP + operator) | `LoopbackOnly.safe_send` replaces **every** ≥400 body with `{"error":"request_rejected"}`, forces JSON content-type | SDK envelope echoes | `server.py:308-341` |
| HTTP guard/auth rejections | fixed bodies `local_only`, `authentication_required`, `invalid_tunnel_route`, `remote_read_principal_required` | header values | `server.py:267-307, 371-433` |
| HTTP diagnostics | `DenialReason` + `request_context` categories only | names/values | `http_diagnostics.py`; `server.py:255-266, 314-325` |
| Operator HTTP (local) | fixed codes; `cache-control: no-store` | exception text | `operator_api.py:226-240, 399-409` |
| Broker event log (`status view=logs stream=broker`) | `LogBook.append` stores only enum event + stage | caller strings, capabilities, USB ids, firmware text | `logs.py:34-45` |
| Build/source/flash/app logs (shared logs) | `redact_log_text`: exact `redactions` tuple, `_SECRETS` regex (cpk keys, `bearer …`, `password|token|secret|api_key[:=]…`), any `cpk_[hex.]*` prefix, 4096-char cap | credentials; **paths only for build** | `build_logs.py:13-25, 58-60` |
| — build | `redactions` = env values whose *name* matches token/secret/password/key/bearer (len≥8) **+ `project_dir`, `trusted_profile`, IDF path, python env root** | private absolute paths | `firmware_build.py:651-655, 743-746` |
| — flash | env-name heuristic only; **no path redactions** [obs; whether paths can appear in flash logs is spec] | — | `firmware_flash.py:626-630` |
| — source | `BuildLogs(...)` with default empty redactions [obs `firmware_source.py:65,142`] | regex only | |
| — app | `redact_log_text(text, logs.redactions)` | regex | `app_events.py:40` |
| Device logs (`stream=device`) | `device_observation._CREDENTIAL`, PEM blocks → `[REDACTED: credential-like content]`; documented best-effort (`REDACTION_LIMITATIONS`) | known credential patterns | `device_observation.py:20-47, 427` |
| Console (stderr) | `console_line` after BuildLogs redaction; control chars escaped, 2048 cap; uvicorn `access_log=False`, `log_level=warning` | | `console_logs.py:29-38`; `cli.py:355-368` |
| stdio adapter | SDK loggers silenced (`_quiet_dependencies`), fixed codes on stderr, upstream results passed losslessly | key, bearer | `stdio_adapter.py:45-70` |
| Job status | `shared_job` strips `derby_diagnostics`, `fixed_trial`, provenance, observation diagnostics; private view only to bound principal with post-read recheck | private DERBY data | `derby_reports.py:16-27`; `server.py:918-946` |
| Registered targets | IDs only, filtered through `Identifier` | paths/selectors | `server.py:601-653` |
| Client identity | origin/principal_id/scopes/ingress flag | token | `server.py:612-627` |
| Work sessions | `resume_token` returned **only** by `create_work_session` (`broker.py:1475`); `resume_work_session`/`request_device` return `_work_result` (no resume token) but **do** return the lease capability `lease.session` when caller+work have `control` (`broker.py:1425-1438`) | resume token | |
| `device_status` | no session capability, aggregate hold (no ticket/job/principal) | | `broker.py:1155-1161, 1234-1287` |
| Captures | `metadata()` + PNG; mock = `source:"synthetic_fixture"`, `label:"TEST / NOT DEVICE"`, `is_device_capture:false`, PNG tEXt "NOT A DEVICE CAPTURE", visible raster glyphs | real-hardware claim | `capture.py:34-56, 219-244` |
| GUI | **No path-stripping function exists.** GUI uses fixed-message substitution: `ConnectionPanels.refresh` → default "Information unavailable." (`gui_connection_panels.py:187-192`); Tunnel worker never echoes exceptions, `TunnelError` mapped through `CONFIG_MESSAGES` else `CONFIG_FAILED` (`gui_tunnel.py:459-473`); live transport fixed `AUTH_FAILED`/`DISCONNECTED`; image metadata validated against fixed `SOURCES` (`gui_images.py:28, 212-230`). GUI deliberately displays local Tunnel metadata incl. `client_exe`/`client_archive` paths (`gui_tunnel.py:376-383`) — local operator only. | | |
| CLI | `ports` prints USB inventory; `argparse.error` echoes unknown argv (`cli.py:345`) — local only | | |
| Reprs | `AuthState(<redacted>)`, `KeyRegistry(<…>)`, `IssuedKey.token repr=False`, `ClientEnvironment.token repr=False`, `Session.capability repr=False`, `WorkSession.resume_token repr=False`, windows process handles | | `auth.py:167`; `key_registry.py:68,238`; `client_environment.py:54`; `broker.py:78,91`; `windows_process.py:76,111` |

Dead code: `RequestAuth.list_principals` (`server.py:106-139`) has no caller (retired policy options). It returns key metadata but never secrets.

---

## B. Authentication and authorization

### B1. Principals and scopes

- `Origin` = `local_cli` | `remote_mcp` (`models.py:17`). `Scope` = `read` | `control`; `control` always implies `read` (`key_registry.py:79-90`).
- Ephemeral principals: `AuthState` holds two independent 64-hex bearer tokens (local/remote), must differ, constant-time compare (`auth.py:112-182`). Principal `ephemeral:<origin>`, scopes `{read, control}` (`server.py:95-99`). Source: process env `CARDPUTER_LOCAL_TOKEN`/`CARDPUTER_REMOTE_TOKEN` (`auth.py:283-290`) or POSIX-only runtime file (`auth.py:39-41, 184-266`). Real auth requires explicit `enable_real_auth` (`auth.py:148-165`).
- Issued keys: `cpk_<32hex>.<64hex>`; principal `key:<key_id>`; only SHA-256 verifier stored; dummy digest compare for unknown ids (`key_registry.py:384-416`).
- Resolution order: ephemeral first, then registry (`server.py:95-104`). Re-resolved **per request**, never cached.

### B2. Key read / revocation / audit

- `KeyRegistry._connection` re-validates directory chain, file, and `-journal/-wal/-shm` sidecars on **every** operation; `mode=rw` so a deleted DB is never silently recreated (`key_registry.py:241-267`).
- `authenticate` fails closed (None) on any registry error (`:415-416`); `LiveKeyRegistry` (default path) also fails closed and re-opens per call (`registry_defaults.py:35-77`); missing default DB ⇒ no issued keys, ephemeral unaffected.
- Revocation: `revoke` idempotent, `BEGIN IMMEDIATE` (`:428-441`); `rotate` atomic issue+revoke+audit (`:443-463`). No remote issuance; management is local CLI only (`key_operator.py`, TTY + typed confirmation, show-once with revoke-on-display-failure `:129-147`).
- `audit_events` (`:465-478`): bounded to 4000 rows (`_audit` prunes, `:306-313`); returns `event,key_id,at` only.
- Grant recheck for queued/long work: `RequestAuth.principal_valid` (`server.py:141-166`) — metadata-only via `list_keys()`, requires matching origin, not revoked, in-window, `required_scopes ⊆ key.scopes`; installed into broker at `server.py:481`. Broker consumers: `broker.py:569-573, 626, 644-647, 721-725, 809, 890, 963, 1862-1876, 1976-1977, 2063-2071, 2192-2225, 2345-2428, 2504-2516, 2579-2591`; `home_recovery.py:195-204`; `profile_jobs.py:85`. Pattern to preserve: capture `valid = self.principal_valid`, then re-check identity `self.principal_valid is valid` after awaits.

### B3. `PlanApprovals.validate_current` (`plan_approval.py:509-570`)

Called only from `broker.py:1853-1856` (execute path). Rebuilds plan from DB with size cap 16 KiB, duplicate-key rejection, `digest_plan(plan)==hash`, strict schema (`:63-83, 86-441`); checks backend/serial config identity, epoch, session id, device, per-test principal binding (`flash_principal`, `build_principal`, `source_principal`, `log_delivery`, collector binding), job not already started (`:443-507`); re-runs the per-test preflight and requires the recomputed digest to equal the hash (`:513-569`). All non-Broker errors fixed (`_fixed_errors`). `approve()` → `approval_interface_retired`; `list_pending()` → empty/retired (`:572-576`). `OperatorAPI` constructs it only for timeline/app-log binding (`operator_api.py:103, 201`).

### B4. Windows key-file permissions — not enforced (by design)

- `key_registry._safe_file/_safe_directory` (`:126-153`): link/reparse/hardlink/size checks on all OSes; **owner/mode checks only on POSIX**. Module docstring (`:8-13`) states no ACL guarantee on Windows; operator must use an OS-user-private directory. `prepare_default_key_directory` does no ACL repair (`registry_defaults.py:25-32`); `mkdir(mode=0o700)` is a no-op for ACLs on Windows.
- Runtime auth file is **refused** on Windows (`auth.py:39-41` → `private_runtime_credentials_unsupported`).
- Contrast: flash backup / LKG / source cache require an explicit `windows_private_acl_confirmed` / `private_acl_confirmed` operator attestation on Windows (`flash_backup.py:32-35`; `source_snapshot.py:820-821`). Key registry has no such attestation.

### B5. "Key registry refusal inside a git checkout" — does not exist for the key registry

The `.git` ancestor refusal is in **`client_environment._check_location`** (`client_environment.py:64-86`) for the stdio adapter's private client env file: rejects files under the adapter install/source tree, PyInstaller `_MEIPASS`, or any ancestor containing `.git` (dir, worktree file, or dangling link) of the path, its resolved path, the adapter roots, or cwd. `key_registry.py` and `registry_defaults.py` have **no** git-checkout check [obs]. If the redesign wants it for the registry, it is new behaviour.

### B6. Every principal/scope check call site

`principal(ctx, scope)` is defined at `server.py:484-491` (raises `authentication_required` / `insufficient_scope`). **36 calls** (37 lines incl. the def). It authenticates **unconditionally** — even on a mock server with `auth_required=False` it raises `authentication_required` (no credentials configured). Only the `read`/`owned`/`acquire_owned` wrappers gate on `auth_required`. Preserve this split.

Wrappers (5 in server.py + 1 in surface):

| Wrapper | Def | Scope | auth_required-gated? | Extra |
|---|---|---|---|---|
| `read` | `server.py:493-496` | read | yes | — |
| `owned` | `server.py:498-508` | control | yes | `require_owner(session, origin, principal_id)` |
| `acquire_owned` | `server.py:510-516` | control | yes | `request.owner == origin` else `authenticated_owner_mismatch` |
| `work_action` | `server.py:518-524` | caller-given | **no** (always) | — |
| `create_work_owned` | `server.py:526-534` | control | **no** | owner==origin |
| `plan_is_callers` | `surface.py:416-425` | control | n/a (BrokerError → False) | plan principal keys `PLAN_PRINCIPAL_KEYS` (`surface.py:391`) |

Direct calls (wrapper = "direct" unless noted):

| file:line | Op | Scope | Wrapper / follow-up |
|---|---|---|---|
| server.py:495 | (read) | read | inside `read` |
| server.py:504 | (owned) | control | inside `owned` |
| server.py:512 | (acquire_owned) | control | inside `acquire_owned` |
| server.py:524 | (work_action) | param | inside `work_action` |
| server.py:529 | (create_work_owned) | control | inside `create_work_owned` |
| server.py:616 | client_identity | read | direct |
| server.py:641 | list_registered_targets | read | direct |
| server.py:669 / 681 | read_source_resources | read | direct, re-auth after await + identity compare |
| server.py:789 | prepare_test (after, device) | control | direct → `prepare_deferred` |
| server.py:792 | prepare_test (build/source) | control | direct → `profile_jobs.prepare` |
| server.py:806 | prepare_test (device tests) | control | direct + `require_owner`, then **also** `owned` (double auth) |
| server.py:816 | prepare_test SOURCE `current_owner` | control | direct + redundant `"read" in scopes` (`:817-818`); passed as `reauthorize` |
| server.py:873 | execute_test (build/source) | control | direct |
| server.py:878 | execute_test (device_deferred) | control | direct |
| server.py:880 | execute_test (device) | control | `owned` |
| server.py:896 | request_home_recovery | control | direct |
| server.py:907 | cancel_home_recovery | control | direct |
| server.py:923 / 932 | job_status DERBY | read | direct; swallowed when `!auth_required` → shared view; re-auth + `binding_unchanged` |
| server.py:948 | job_status home_recovery | read | direct; plan principal match |
| server.py:960 | job_status registered_project | read | direct; plan principal match |
| server.py:965 | job_status | read | **also** wrapped in `read` |
| server.py:986 / 1010 / 1059 | read_build_logs | read | direct; per-test principal key match; re-auth after await |
| server.py:1105 | list_apps | read | direct |
| server.py:1119 | app_command | control | direct + `require_owner` |
| server.py:1135 | read_app_events | read | direct |
| server.py:1152 | read_device_logs | read | direct |
| server.py:1167 | read_capture | read | direct |
| server.py:1204 | cancel_job (build/source) | control | direct; else `owned` (`:1206`) |
| server.py:715, 720 | device_status, read_logs | read | `read` |
| server.py:725 | acquire_session | control | `acquire_owned` |
| server.py:730, 735 | renew/release | control | `owned` |
| server.py:1215 | create_work_session | control | `create_work_owned` |
| server.py:1223 | resume_work_session | **read** | `work_action` |
| server.py:1238, 1253, 1268 | request_device, cancel_device_request, close_work_session | control | `work_action` |
| surface.py:419 | `plan_is_callers` (wrong-tool check) | control | swallowed |
| surface.py:504 | status view=job (deferred) | read | swallowed |
| surface.py:517 | status view=job (chain) | read | swallowed |
| surface.py:682 | cancel target=ticket | control | `_refuse(str(exc))` |
| surface.py:700 | cancel deferred | control | swallowed |
| surface.py:709 | cancel_job_only | control | `_refuse(str(exc))` |

Scope/origin checks **outside** `principal()`:
- `LoopbackOnly.__call__` `server.py:276-307`: any authenticated principal (no scope) when `auth_required`.
- `TunnelIngress.__call__` `server.py:415-433`: must be `REMOTE` and have `read`.
- `OperatorAPI.__call__` `operator_api.py:253-259, 326-332, 342-352`: must be `LOCAL` + `control`; re-auth after body read and after live wait. `_retained_job_logs` `:135` read; `_app_job_logs` `:177` LOCAL + read + control.
- `broker._authorized_work` `broker.py:~1370-1388` (work.scopes ⊇ required, principal match); `_valid_scopes` `broker.py:1456`.
- `stdio_adapter._verify_identity` `stdio_adapter.py:157-168`: identity view must be ok/authenticated/`remote_mcp`/has `read` — **depends on the `status view=identity` response shape.**
- `create_app` `server.py:1302-1303`: tunnel ingress requires authenticated broker.

---

## C. Child process environments and command/path guards

### C1. Environment allowlists

| Child | Allowlist | Where |
|---|---|---|
| IDF build (profile export, idf.py) | starts from `OS_ENV` = `ALLOWED_ENV` minus IDF/PATH keys (`firmware_build.py:45-81`); profile output parsed by `parse_profile` — only `ALLOWED_ENV` keys survive, duplicates/invalid names rejected, requires IDF_PATH/IDF_PYTHON_ENV_PATH/PATH (`:281-301`); `SYSTEMROOT` must locate trusted powershell (`:662-666`) | `firmware_build.py:662` |
| Flash worker (`python -I -B`) | `OS_ENV` | `flash_driver.py:313-325` |
| Tunnel client (supervisor / GUI manager) | `child_environment`: `SAFE_ENV` = SYSTEMROOT, WINDIR, SYSTEMDRIVE, TEMP, TMP, COMSPEC + `CARDPUTER_TUNNEL_API_KEY` + `CARDPUTER_TUNNEL_BEARER` (bearer must match fixed regex) | `tunnel.py:35, 367-376`; used `tunnel.py:549-557`, `tunnel_manager.py:425-481` |
| Owned Windows process (Tunnel) | `_validate`: env keys ⊆ `_ENV_KEYS`, no NUL, executable must be absolute `tunnel-client.exe`, credentials must not appear in argv; credential args only fixed `env:` refs | `windows_process.py:35-52, 204-230` |
| Supervisor console | parent env vars cleared in PS `finally`; `_consume_runtime_api_key` pops runtime keys from `os.environ` | `tunnel.py:360-364, 540-558` |
| Broker launcher | `Start-Cardputer.ps1:108-164` sets fresh process-scoped `CARDPUTER_LOCAL/REMOTE_TOKEN`, restores previous values | (not inspected in depth) |
| HTTP clients | `trust_env=False` everywhere (`client.py:47`, `stdio_adapter.py:107`, `operator_client.py:221`, `gui_live_transport.py:127`, `mcp_diagnostics.py:64`, `tunnel.py:480`, `tunnel_manager.py:111`) | |

Source fetch spawns no child (urllib zip download in `source_snapshot.py`) [obs].

### C2. "No arbitrary shell commands / unrestricted project/profile paths"

- MCP request models carry no paths/commands: `Identifier` `^[a-zA-Z0-9_-]{1,64}$`, `Capability`/`Digest` 64-hex, `StrictModel extra=forbid` (`models.py:8-14`); 6-tool request models (`surface.py:147-269`) with per-discriminator required/forbidden sets (`surface.py:286-297, 315-389`).
- Projects/profiles/tools only from local TOML: `BuildProject` absolute paths, no `..` (`config.py:110-137`); `RegisteredBuildTool` absolute `.exe` + sha256 + version (`:41-54`); `SourceRepository` GitHub name regex, submodule allowlist (`:57-107`); `expected_idf: Literal["6.0.1"]`, `target: Literal["esp32s3"]`.
- Lookups by registered id only: `FirmwareBuild._project` → `registered_build_project_required` (`firmware_build.py:377-382`); `profile_jobs.py:97-125`.
- Project must not be inside broker state, must be an IDF project (`firmware_build.py:424-429`); links rejected in fingerprint/hash (`:304-330`).
- `list_registered_targets` drops names outside `Identifier` (`server.py:601-609`).
- Instructions string advertises "No arbitrary shell/raw serial commands" (`server.py:584`).
- Operator API: no paths accepted; strict models (`operator_api.py:47-76`); retired approval routes → 410 (`:18-33, 296-300`).

---

## D. Tests pinning each guard

### D1. Pinned (file::name)

- Allowlist/flattening: `tests/test_public_reasons.py::test_every_request_path_reason_is_classified`, `::test_suppressed_reason_stays_invalid_request`, `::test_unregistered_text_is_flattened`, `::test_source_error_keeps_its_own_public_form`; `tests/test_mcp_input_http.py::test_only_typed_allowlisted_source_errors_escape_generic_value_error_mask`.
- Input echo: `tests/test_http_security.py::test_tool_validation_never_echoes_raw_input`, `::test_http_envelope_validation_never_echoes_raw_input`; `tests/test_input_diagnostics.py::InputDiagnosticTests::test_field_failures_use_only_fixed_reasons_and_fields`, `::test_request_models_cover_exactly_the_six_surface_tools`; `tests/test_mcp_input_http.py::test_http_tool_validation_is_actionable_and_never_echoes_input`, `::test_http_submit_rule_failures_return_fixed_codes_and_never_echo_input`.
- Loopback/HTTP guard: `tests/test_http_security.py::test_http_rejects_untrusted_hosts|origins|proxy_headers|remote_or_unidentified_clients|non_json_post`, `::test_http_untrusted_header_bytes_are_rejected_without_exception`; `tests/test_denial_request_context.py::*` (9 tests).
- Tunnel ingress: `tests/test_tunnel_ingress.py::test_raw_guards_run_before_authentication_or_stripping`, `::test_missing_local_or_readless_principal_never_reaches_inner_guard`, `::test_revocation_between_outer_gate_and_inner_auth_is_not_cached`, `::test_read_only_ingress_key_keeps_control_scope_restriction`, `::test_ingress_cannot_reach_operator_or_supply_approval`, `::test_no_body_header_query_token_or_principal_leaks_in_errors_or_diagnostics`, `::test_enabled_ingress_requires_real_authentication_configuration`.
- Auth state: `tests/test_auth.py::*` incl. `::test_auth_repr_hides_tokens`, `::test_runtime_windows_fails_closed`, `::test_environment_auth_requires_explicit_opt_in`.
- Key registry: `tests/test_key_registry.py::test_only_hash_persisted_no_token_in_repr_or_audit`, `::test_other_connection_revocation_immediate`, `::test_rotation_preserves_capabilities_and_revokes_old_atomically`, `::test_rotation_late_failure_rolls_back_both_changes`, `::test_database_and_error_fail_closed`, `::test_file_permissions` (POSIX only), `::test_symlink_registry_rejected`, `::test_unsafe_labels_never_echoed`, `::test_invalid_key_ids_never_echo`; `tests/test_registry_defaults.py::test_uncommitted_revocation_and_rollback_do_not_cache_grants`, `::test_default_registry_permissions_fail_closed` (skipped on nt), `::test_windows_private_directory_supports_default_registry` (nt, positive only); `tests/test_key_operator.py`.
- Principal recheck: `tests/test_queued_principal_recheck.py::test_explicit_queued_job_rechecks_current_principal_before_start`; `principal_valid` used in ~33 test modules.
- Plan validation: `tests/test_plan_approval.py::test_preflight_is_read_only_and_approval_surface_is_retired`, `::test_changed_or_malformed_plan_cannot_enqueue`, `::test_current_backend_preflight_is_rechecked_without_reflecting_exceptions`, `::test_serial_device_firmware_and_deadline_binding_rechecked_before_enqueue`.
- Operator: `tests/test_operator_late_auth.py::test_operator_reauthenticates_after_fragmented_body`, `::test_retained_job_cancel_uses_final_authenticated_actor_after_body_wait`; `tests/test_operator_refusals.py::*`; retired routes in `tests/test_operator_launch_http.py`, `tests/test_capture_policy_http.py`.
- Identity / targets: `tests/test_client_identity.py::test_identity_reports_request_principal_without_acquiring`; `tests/test_registered_targets.py::test_registered_targets_requires_current_read_scope`, `::test_registered_targets_exact_ids_only_and_current_settings`.
- Ownership: `tests/test_authenticated_mock.py::test_authenticated_mock_binds_all_session_actions_to_origin` (also asserts bearer tokens + lease capability absent from status device/job/logs); `tests/test_work_session_http.py::test_work_session_scope_and_same_origin_key_isolation`.
- Client env file: `tests/test_client_environment.py::test_git_checkout_files_rejected`, `::test_adapter_source_tree_rejected`, `::test_permission_error_does_not_leak_os_message`, `::test_parent_reparse_directory_rejected`, `::test_posix_permissions_must_be_private` (POSIX only).
- Child env: `tests/test_tunnel_launcher.py::test_environment_is_allowlisted_and_only_contains_remote_credentials`, `::test_supervisor_suppresses_native_output_and_clears_child_env`; `tests/test_tunnel_runtime_env.py::*`; `tests/test_windows_process.py::test_validation_precedes_any_native_action`, `::test_credential_options_allow_only_fixed_environment_references`, `::test_repr_does_not_contain_arguments_paths_credentials_or_handles`; `tests/test_firmware_build.py::test_profile_strict_filter`, `::test_registered_plan_no_paths`, `::test_local_policy_opt_in_only` (asserts "private-path" absent from build logs on profile denial), `::test_logs_multireader_filters_context_redaction`; UNTRUSTED_TOKEN not in build env (inside `_exercise_build`, `tests/test_firmware_build.py:~461`).
- Logs: `tests/test_console_logs.py`, `tests/test_app_events.py`, `tests/test_operator_app_logs.py`, `tests/test_device_observation.py`.

### D2. The six requested checks

| Guard | Status | Evidence |
|---|---|---|
| Private-path stripping in GUI display errors | **No such mechanism**; GUI uses fixed-message substitution. Partially tested with token/URL markers, **not with a private absolute path in a GUI error** | `tests/test_gui_live_state.py::test_disconnect_unknown_or_raw_reason_never_exposes_transport_data` (URL with `/private/path`), `tests/test_gui_tunnel.py::test_unknown_config_error_values_are_not_echoed`, `::test_unknown_errors_and_snapshot_text_are_never_exposed`, `tests/test_gui_live_transport.py::test_client_startup_failure_is_bounded_and_never_prints_private_error`, `tests/test_gui_mcp_diagnostics.py`, `tests/test_gui_cancel_receipt_review.py` |
| Mock image must not appear as real hardware | **Tested** at producer + HTTP; GUI consistency check **partially** (label/lcd tampering, not `is_device_capture`/`source` swap) | `tests/test_capture.py::test_synthetic_image_is_deterministic_and_visibly_labeled`; `tests/test_capture_http.py::test_capture_http_delivers_image_content_hash_and_principal_isolation`; `tests/test_gui_images.py::test_valid_preview_contains_exact_fixture_bytes_and_safe_source_label`, `::test_untrusted_metadata_is_rejected_with_fixed_message`; mock `hardware_connected is False` in `tests/test_authenticated_mock.py::test_authenticated_mock_rejects_missing_invalid_and_external_requests` (line 118) |
| No resume token / plan secret in 6-tool status output | **Untested at tool level.** Broker-level only. Tool-level test covers bearer + lease capability, not `resume_token`. "Plan secret" has no defined object in code [spec: caller may mean plan principal/port fields] | `tests/test_session_safety_review.py::test_resume_token_is_bound_to_principal_and_snapshot_never_discloses_it` (broker.status/operator_snapshot/logs/DB); `tests/test_authenticated_mock.py::test_authenticated_mock_binds_all_session_actions_to_origin` |
| Windows key file permissions | **Not enforced, so not tested.** POSIX tests skip on nt; Windows has a positive-path test only | `tests/test_key_registry.py::test_file_permissions` (skip nt); `tests/test_registry_defaults.py::test_windows_private_directory_supports_default_registry` |
| Key registry refusal in git checkout | **Does not exist** for the key registry. Exists for client env file and is tested | `tests/test_client_environment.py::test_git_checkout_files_rejected` |
| stdio adapter annotation passthrough | **Tested at adapter-object level** (full `Tool.model_dump()` incl. annotations, and equality with the real broker's `list_tools`); **real stdio wire test checks tool names only** | `tests/test_stdio_adapter.py::test_catalog_and_results_are_lossless`, `::test_actual_broker_catalog_matches_and_connections_do_not_acquire`, `::test_real_stdio_frames_are_clean_and_eof_has_no_side_effects` (names + `listChanged: false`) |

### D3. Other untested guards found

- `wrong_execute_tool` / `wrong_flash_tool` refusal (`surface.py:427-439`) — the execute/flash split. No test asserts either code (`tests/test_chain_http.py::test_rules_for_after_and_wrong_tool` covers `after` rules and `ticket_invalid` despite its name).
- Caller-key echo through `_convert` cursor (A4) — untested and currently leaky.
- `project_must_not_be_inside_broker_state` (`firmware_build.py:427`) — no test references it.
- `absolute_registered_path_required` / `absolute_registered_executable_required` config validators — no direct test reference found [spec: may be exercised indirectly].
- Flash log path redaction — none exists (A5); no test.
- BrokerError passthrough of TicketError/app codes (A3) — only `ticket_invalid` pinned; app codes not in the ledger.
- `SafeMCPServer` mapping of unexpected handler crashes to `invalid_arguments` — no test asserting that a non-ValueError internal error yields the generic body [spec: covered incidentally by echo tests].
- `RequestAuth.list_principals` — dead, untested.

---

## Must-preserve summary for the PublicError gate

1. BrokerError/TicketError/app codes currently public must stay public (or the change must be deliberate and pinned) — add them to the allowlist rather than flattening.
2. ValueError allowlist semantics: exact single-arg str membership; everything else `invalid_request`.
3. SourceError `public()` shape incl. `explanation` and `scan`; `RecoveryPointRefusal` shape incl.
   `flash_baseline`, `lkg_recorded`, `next_step` and `explanation` (fixed vocabulary only).
4. `_convert` → `argument_invalid` + schema-owned `field` (fix the cursor-key echo).
5. `read_capture` error result is `is_error=True` while other ops return `ok:false` with no `is_error`; status tool always `_wrap` without `is_error`.
6. HTTP layers keep their own fixed bodies and 4xx body replacement; operator API keeps 200-for-BrokerError and 400-for-ValueError.
7. `principal()` unconditional vs. `auth_required`-gated wrappers; post-await re-auth + identity comparisons; `principal_valid` identity capture.
8. `status view=identity` shape (stdio adapter depends on it).
