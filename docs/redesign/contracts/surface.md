# External contract inventory — the 6-tool MCP surface (0.13.0, main 5d79d2d)

Scope: `status`, `submit`, `execute`, `flash`, `cancel`, `session`. This is a read-only inventory, and no repo
file was edited. Evidence comes from three places: reading the code, an in-process `create_server(...).list_tools()`
dump (saved next to this file as `tools_list.json`), and in-process `call_tool` probes. The probes ran without the
lifespan, so no broker, store or hardware was started. Every probe used only argument/rule refusals, which happen
before any broker access.
Side effect to disclose: the first `uv run` rebuilt and reinstalled the editable `cardputer-device-mcp` package
in `.venv`. No tracked file changed.

Labels: **[code]** = read in the source, **[probe]** = observed in-process, **[inferred]** = derived from the code
but not executed, **[doc]** = stated in DESIGN-MCP-SURFACE.md.

---

## 0. Cross-cutting transport and registration contract

### 0.1 Registration and tools/list
- Only the 6 surface tools are MCP tools. They are registered by `register_surface()` in
  `src/cardputer_mcp/surface.py:401-819`, which `create_server` calls at `server.py:1277-1286`.
- The 25 legacy handlers are decorated with `@op` (`server.py:596-599`). `@op` only stores
  `ops[handler.__name__] = handler`, so the legacy handlers are **not** MCP tools.
  The 25 ops are: client_identity, list_registered_targets, read_source_resources, device_status, read_logs,
  acquire_session, renew_session, release_session, prepare_test, execute_test, request_home_recovery,
  cancel_home_recovery, job_status, read_build_logs, list_apps, app_command, read_app_events, read_device_logs,
  read_capture, cancel_job, create_work_session, resume_work_session, request_device, cancel_device_request,
  close_work_session.
- **Stage 3c (2026-10-25): the `@op` layer is gone.** `op`, `ops`, `_result`, `bind_lease` and `bind_origin`
  left `server.py`; the six tools in `surface.py` call the broker directly. `server.py:<line>` references
  below are still 5d79d2d lines. Where each former op now lives (`register_surface` closures):
  | former op | now |
  |---|---|
  | client_identity, list_registered_targets | `identity_body`, `targets_body` |
  | read_source_resources | `source_body` |
  | device_status, list_apps, resume_work_session | inline in `status` (view=device/apps/session) |
  | job_status | `job_body` |
  | read_logs, read_device_logs, read_app_events | inline in `_read_logs` (stream=broker/device/app) |
  | read_build_logs | `build_logs_body` |
  | read_capture | `capture_result` |
  | prepare_test | `prepare_body` |
  | execute_test | `execute_body` (called by `enqueue`) |
  | app_command, request_home_recovery | inline in `execute` |
  | cancel_job, cancel_home_recovery, cancel_device_request | inline in `cancel` |
  | acquire_session, renew_session, release_session, create_work_session, request_device, close_work_session | inline in `session` |
  | `server._result` | `surface._result` (sync) and `surface._result_async`, both through `public_errors.refusal` |

  Each body keeps its former `require(ctx, scope, honor_auth_required=...)` value; pinned per path by
  `tests/test_surface_gate_matrix.py`. tools/list is unchanged (`tests/fixtures/surface_tools_list.json`).
- Each tool has the signature `(request: <Model>, ctx)`. The wire arguments are therefore **always
  `{"request": {...}}`**: the top-level schema is `{"properties":{"request":{"$ref":"#/$defs/<Model>"}},
  "required":["request"],"title":"<tool>Arguments","type":"object"}` **[probe]**.
- tools/list order is status, submit, execute, flash, cancel, session (registration order) **[probe]**.
- The tools have no `title`. `description` is the handler docstring, verbatim (see §1–§6).
- `outputSchema`:
  - `status` has **none**, because it returns `CallToolResult`.
  - The other five have `{"additionalProperties":true,"title":"<tool>DictOutput","type":"object"}`, because they
    return `dict[str, Any]` **[probe]**.
- Measured size: 15,486 chars compact JSON (16,726 with default separators). Per tool: status 2607, submit 5134,
  execute 3272, flash 1450, cancel 1846, session 2405 **[probe]**. The doc says "13,350" (`DESIGN-MCP-SURFACE.md:14`)
  but gives no measurement method. **No test pins the size or a schema snapshot.** The stage-0 snapshot test that
  the design describes (§11) does not exist.
- `$defs` names are an internal-consumer contract. `mcp_diagnostics.py:95-131` reads
  `submit.inputSchema.$defs.Kind.enum` and `status.inputSchema.$defs.View.enum`. Renaming the enum classes `Kind`
  or `View` breaks GUI diagnostics.
- Internal callers that depend on the surface:
  - `client.py:13`: `SURFACE_TOOLS` and the `call()` helper, which wraps the payload in `{"request":…}`.
  - `tunnel.py:435-450` and `tunnel_manager.py:86-92`: identity + device status, and require SURFACE_TOOLS ⊆ tools.
  - `stdio_adapter.py:119-125`: identity preflight.
  - `app_client.py:132` (`op: app_input`).
  - `cli.py`.

### 0.2 Annotations (`server.py:590-592`)
| tool | readOnlyHint | destructiveHint | openWorldHint | idempotentHint |
|---|---|---|---|---|
| status | true | false | false | **absent** |
| submit | false | false | false | absent |
| execute | false | false | false | absent |
| flash | false | **true** | false | absent |
| cancel | false | false | false | absent |
| session | false | false | false | absent |

- `idempotentHint` is never set. The local variable is named `execution`, but it is passed as `destructive=` (`server.py:1285`).
- Pinned by:
  - `tests/test_mcp_input_http.py::test_tools_list_discloses_flash_request_contract`: only `flash` is
    destructive, status is read-only, and execute/cancel/session are not read-only and not destructive.
  - `tests/test_registered_targets.py::test_registered_targets_metadata_empty_input_and_read_only`: status
    open_world false, and `status` is the only read-only tool.
  - `tests/test_app_broker.py::test_mcp_app_tools_metadata_and_same_host_client_for_registered_apps`.

### 0.3 Capabilities / listChanged
- HTTP `SafeMCPServer` (MCPServer v2) initialization capabilities: `tools.list_changed=False`,
  `prompts.list_changed=False`, `resources.subscribe=False, list_changed=False`, `experimental={}`,
  `extensions={}` **[probe]**. Server name is `"Cardputer Device Broker"` and version is `"0.13.0"`.
  `instructions` is a fixed string (`server.py:569-589`). It still mentions the legacy names
  `execute_test` and `request_home_recovery`, which can no longer be called. This is a doc drift inside the contract.
- stdio adapter: low-level `Server("device-broker-adapter")` with only `on_list_tools` and `on_call_tool`
  (`stdio_adapter.py:225-230`) → `capabilities.tools == {"listChanged": False}`. Pinned by
  `tests/test_stdio_adapter.py::test_real_stdio_frames_are_clean_and_eof_has_no_side_effects` (line 421).
  No notification path exists.

### 0.4 Pre-validation gate (`SafeMCPServer.call_tool`, `server.py:169-192` + `input_diagnostics.py`)
- `validate_arguments(name, arguments)` (`input_diagnostics.py:71-104`) runs before the SDK. It covers only the 6
  names (`REQUEST_MODELS`, `input_diagnostics.py:16-23`). For any other name (legacy names included) it returns
  None, the SDK then raises unknown-tool, and the catch-all `except Exception` returns `invalid_arguments()`.
- Failure body (`isError=true`): `{"ok":false,"error":"invalid_tool_request","reason":<r>,"fields":[...]}`.
  - Missing `request` gives reason `required_field` with fields `["request"]`. A non-dict gives `invalid_field_type` with `["request"]`.
  - `TYPE_REASONS` (`:44-53`): missing→`required_field`, extra_forbidden→`unexpected_field`,
    string_pattern_mismatch→`invalid_field_format`, enum/literal_error→`invalid_field_value`,
    ge/le→`field_out_of_range`, too_long→`field_limit_exceeded`, `*_type`/`*_parsing`→`invalid_field_type`,
    anything else→`invalid_field`.
  - Any exception (including handler exceptions or assertion failures inside a tool) gives `invalid_arguments` with fields `[]`.
  - `fields` holds only names in `SAFE_FIELDS` (model field names plus request/epoch/sequence), at most 4, and
    falls back to `["request"]`. Extra key names are never echoed (extra → `["request"]`).
- **Dead code:** `MODEL_REASONS` (`input_diagnostics.py:26-43`) maps `PrepareRequest` validator codes. None of the
  six surface models has those validators; `SubmitRequest` has no model_validator. So this branch can never fire on
  the surface. Its only remaining validators are the nested `AppSessionOptions` ones, whose codes are not in MODEL_REASONS.
- Pinned by:
  - `tests/test_input_diagnostics.py::{test_field_failures_use_only_fixed_reasons_and_fields,
    test_request_models_cover_exactly_the_six_surface_tools}`
  - `tests/test_mcp_input_http.py::test_http_tool_validation_is_actionable_and_never_echoes_input`
  - `tests/test_registered_targets.py::test_registered_targets_metadata_empty_input_and_read_only` (extra/unknown keys)
  - `tests/test_pending_execution_http.py::{test_mcp_schema_has_no_approval_intent_or_operator_tool,
    test_mcp_rejects_retired_approval_intent_before_any_durable_submission}` (`wait_for_approval` is not in the
    schema and is rejected).

### 0.5 Surface-rule refusals (`surface.py:278-312`)
- `_refuse(error, field=None, **extra)` → `{"ok":false,"error":..., ["field":...], [extra...]}`, returned as a
  **non-error** result (`isError=false`).
- `_check()` (`:286-297`) compares `request.model_fields_set - {discriminator}` with the per-value rules. Missing
  fields are checked first, in sorted order, giving `argument_required`. Fields that are neither required nor
  optional then give `argument_not_allowed`. **An explicit JSON `null` counts as "present"**:
  - `{"view":"identity","job_id":null}` → `argument_not_allowed/job_id` **[probe]**.
  - A `null` value for a *required* field passes `_check`, then hits an `assert` (`_req`, `assert x is not None`)
    → caught by SafeMCPServer → `invalid_tool_request/invalid_arguments/[]`, `isError=true`. Probed on
    status/source, execute/home_recovery, cancel/request and session/renew.
  - This depends on assertions being enabled. Under `python -O` the behaviour would change. **[inferred]**
    (I did not check how the packaged exe is optimized.)
  - Exception: `submit` flash `session:null` reaches `PrepareRequest` → `device_session_required`
    (pinned at `test_mcp_input_http.py:89`).
- `_convert(build)` (`:300-312`) builds a legacy request model.
  - On ValidationError, it looks at the first issue: if its ctx error is a ValueError whose `public_reason` is not
    `invalid_request`, it returns `_refuse(<code>)` with no field.
  - Otherwise it returns `_refuse("argument_invalid", first str loc part or None)`. Model-level validator errors
    have an empty loc, so they give `{"ok":false,"error":"argument_invalid"}` with no field
    (probe: app_input `action.tap` without `action`).
- Output text encoding differs **[probe]**. This matters for byte-level preservation:
  - `status` → `_wrap()` (`:394-398`) emits `TextContent(json.dumps(body, sort_keys=True))`, which is compact,
    has default separators and sorted keys, plus `structured_content=body`.
  - The other 5 return dicts, which the SDK serializes as **indent=2 with insertion order**, plus `structuredContent`.
  - SafeMCPServer invalid → `json.dumps(sort_keys=True)`, `isError=true`.

### 0.6 HTTP vs stdio
- **HTTP** (`create_app`, `server.py:1290-1342`):
  - Streamable HTTP with `stateless_http=True`, `json_response=True`, `max_request_body_size=16384`, and DNS
    rebinding protection (allowed hosts/origins are `127.0.0.1:<port>` and `localhost:<port>`).
  - Wrapped by `OperatorAPI` → `LoopbackOnly` (`:195-341`):
    - Non-loopback peer, bad Host/Origin, duplicate or non-ASCII headers, any `forwarded`/`x-forwarded-*`
      header, or POST without JSON → 403 `{"error":"local_only"}`.
    - When auth is required, a missing or invalid bearer → 401 `{"error":"authentication_required"}`. This is
      checked at the HTTP layer, before any tool runs.
    - Any SDK status ≥400 has its body replaced with `{"error":"request_rejected"}`.
  - Optional `TunnelIngress` (`:344-445`) on `/tunnel/mcp`:
    - Path, query or root-path deviation → 400 `{"error":"invalid_tunnel_route"}`.
    - Missing bearer → 401 `authentication_required`.
    - Principal not `remote_mcp`, or lacking `read` scope → 403 `{"error":"remote_read_principal_required"}`.
    - On success it strips the forwarding headers and rewrites the path to `/mcp`.
  - Ingress without auth fails at startup with `ValueError("tunnel_ingress_requires_authenticated_broker")` (`:1302-1303`).
- **stdio** (`stdio_adapter.py`):
  - The adapter forwards `tools/list` and `tools/call` verbatim to `http://127.0.0.1:<1024-65535>/mcp`. The URL
    must match the regex at `:34`, and the key must match `cpk_[a-f0-9]{32}\.[a-f0-9]{64}` (`:33`); ephemeral
    keys are rejected.
  - Every request opens a fresh HTTP connection (max 4 concurrent, 30 s total) and first calls
    `status {"view":"identity"}`. The identity must show `ok:true`, `authenticated:true`, `origin=="remote_mcp"`
    and `"read" in scopes` (`:157-168`). So each adapter call produces **two** upstream status/identity+X calls.
  - Requests carrying `task`, `input_responses` or `request_state` are refused.
  - A non-`complete` result type gives `stdio_adapter_unsupported_tool_result`.
  - Errors:
    - list_tools → `MCPError(INTERNAL_ERROR, <code>)`.
    - call_tool → `CallToolResult(content=[TextContent(<code>)], isError=true)` with **no structuredContent**.
  - Instructions are copied from upstream at startup.
  - Failed calls are never replayed.
  - Pinned by `tests/test_stdio_adapter.py`: all 14 tests; key ones are `test_catalog_and_results_are_lossless`,
    `test_actual_broker_catalog_matches_and_connections_do_not_acquire`,
    `test_identity_must_prove_authenticated_remote_read_scope` and
    `test_lost_response_is_not_replayed_and_later_request_reconnects`.

### 0.7 Auth helper semantics (`server.py:484-534`)
- `principal(ctx, scope)` authenticates the `authorization` header on every call. The registry is never cached
  (`RequestAuth`, `:84-104`).
  - It raises `authentication_required` or `insufficient_scope` and does **not** depend on `auth_required`.
  - Ephemeral principals are `ephemeral:<origin>` with scopes {read, control}.
- `read(ctx, f)` checks the `read` scope **only if `auth_required`**.
- `owned(req, ctx, f)` checks `control` + `resident.require_owner(session, origin, principal_id)` **only if `auth_required`**.
- `work_action` always authenticates (scope as given).
- `auth_required = backend=="serial" or require_auth or key_registry_path is not None` (`:474`).

### 0.8 Legacy `_result` gate (`server.py:448-456`)
| exception | output |
|---|---|
| `BrokerError` | `{"ok":false,"error":str(exc)}` — **raw string, no allowlist** |
| `SourceError` | `{"ok":false, **exc.public()}`. The code is allowlisted by `SOURCE_ERROR_CODES` (`source_snapshot.py:115,182-185`); output can include `explanation` and `scan` |
| other `ValueError` | `{"ok":false,"error":public_reason(exc)}`: the allowlist, else `invalid_request` |

Success is `{"ok":true, **call()}`. Pinned by
`tests/test_mcp_input_http.py::test_only_typed_allowlisted_source_errors_escape_generic_value_error_mask`.

---

## 1. `status` (R) — `surface.py:469-582`

**Input** `StatusRequest` (StrictModel → `extra="forbid", frozen=True`, `models.py:13-14`; surface.py:147-162)
| field | type | notes |
|---|---|---|
| view | enum View {identity, targets, source, device, session, job, logs, capture, apps, jobs, recovery} | required |
| project_id | Identifier `^[a-zA-Z0-9_-]{1,64}$` \| null | |
| job_id | Identifier \| null | |
| resume_token | Capability `^[a-f0-9]{64}$` \| null | |
| stream | enum Stream {broker, build, device, app} \| null | |
| cursor | dict[str, int\|str] \| null | free-form; validated later as LogCursor / AppEventCursor |
| limit | int 1..100 \| null | |
| levels | list[Literal error/warning/info/debug/unknown] maxItems 5 \| null | |
| context | int 0..10 \| null | |
| include_source_cache | bool \| null | |

**Per-view rules** (`STATUS_RULES` `:315-328`; `STREAM_RULES` `:329-337`, re-checked at `:536-539`)
| view | required | optional | routes to (server.py) | scope |
|---|---|---|---|---|
| identity | – | – | `client_identity` :611-627 (+`tool_set_version: 2`, surface.py:61,484) | read, always |
| targets | – | – | `list_registered_targets` :629-653 | read, always |
| source | project_id | – | `read_source_resources` :655-710 (own try/except, same mapping as `_result`) | read, always (checked twice) |
| device | – | – | `device_status` :712-715 → `Broker.status` broker.py:1234 | read if auth_required |
| session | resume_token | – | `resume_work_session` :1217-1230 → broker.py:1477 | read, always. **Returns lease capability under a readOnly annotation** (design §13.2) |
| job | job_id | – | waiting path: `resident.deferred_job` broker.py:1116 + `chain_progress` :1163; else `job_status` server.py:910-965 → `shared_job`/`private_job` | read if auth_required (waiting path: read always, errors swallowed) |
| logs/broker | stream | cursor, limit | `read_logs` :717-720 | read if auth_required |
| logs/build | stream, job_id | cursor, limit, levels, context, include_source_cache | `read_build_logs` :967-1095 (routes to source/flash/build readers by plan test) | read, always |
| logs/device | stream, job_id | – | `read_device_logs` :1140-1155 → broker.py:2001 | read, always |
| logs/app | stream, job_id | cursor, limit | `read_app_events` :1127-1138 → broker.py:2209 | read, always |
| capture | job_id | – | `read_capture` :1157-1187 (returned raw, `cast(CallToolResult)`) | read, always |
| apps | – | – | `list_apps` :1097-1108 | read, always |
| jobs | – | cursor `{before}`, limit (default 20) | `job_reads.list_jobs` (surface) | read if auth_required |
| recovery | – | – | `job_reads.recovery` (surface) | read if auth_required |

**Cross-principal reads (2026-10-25, owner decision; supersedes the owner-only read rows above).**
Any authenticated caller with the read scope reads any job's status (`view=job`, including
waiting jobs, home recovery and registered-project jobs), logs (`stream=build|device|app`) and
capture, whoever started it. Each reader resolves the job's owner (`job_reads`) and calls the
unchanged owner-scoped reader with that owner's principal, so the body and its redaction are
exactly what the owner gets. This covers the durable fallbacks too: the saved capture copy
(owner from its `capture.json`, else the job owner), durable app-session events (RAM run, else
the queue record, else the job-log header) and `flash_outcome` / `flash_summary` on flash log
reads (plan `flash_principal`). The owner identity is never returned; `view=job` and `view=jobs`
carry `owned_by_caller: bool`. Still owner-only: `chain` on view=job, derby private extras
(`derby_diagnostics`/`fixed_trial`), and every mutation (cancel, execute/flash, session, app
input, home recovery). `owned_home_recovery_required` / `owned_registered_project_job_required`
are no longer raised by `job_status`. The local operator's `_retained_job_logs` uses the same
`job_reads.log_stream` lookup.

- view=jobs: `{"ok":true,"jobs":[{job_id,kind,state,result,created,finished,device_id,
  owned_by_caller}],"has_more","next_cursor":{"before":N}|null}`, newest first across all
  principals (stored jobs only; waiting deferred jobs are not listed). `kind` is the surface
  `Kind` value (or `home_recovery`/`other`). A bad cursor is `argument_invalid` field `cursor`.
- view=recovery: `{"ok":true,"devices":[{device_id,registered,current_lkg,lkg_history:[{receipt,
  kind,current,job_id,baseline_job_id,recorded,recorded_by_caller,firmware_sha256,elf_sha256,
  backup_sha256}],full_backups:[{job_id,created,finished,sha256,flash_bytes,receipt_available,
  owned_by_caller}]}]}`. History: newest 20 per device; backups: newest 20 succeeded
  `firmware_backup` jobs. Backup `sha256` comes from the retained receipt metadata only (never
  the bytes or a path), null when unreadable.

**Output**
- Always a `CallToolResult` built by `_wrap` (isError false). The exception is view=capture.
- view=identity: `{"ok":true,"authenticated":true,"origin","principal_id","scopes":[sorted],
  "tunnel_ingress_enabled","tool_set_version":2}`. Pinned by
  `tests/test_client_identity.py::test_identity_reports_request_principal_without_acquiring`.
  `tool_set_version` is added only when ok.
- view=job (stored job): `shared_job(store.job(...))`, i.e. the job row keys `id, plan_hash, state, stage, result,
  diagnostic, reviewed, created, finished` plus `review` (`store.py:406-413`), with derby private keys removed
  (`derby_reports.py:16-27`). Additions:
  - `recovery_audit` for home recovery;
  - `private_job` extras (`derby_diagnostics` / `fixed_trial`) for the owner of derby plans;
  - `chain` (keys `chain_position, chain_jobs_known, chain_open, chain_state, chain_total,
    hold_expires_in_seconds, chain_budget_remaining_seconds`, broker.py:1220-1230) when
    `job_principal(job_id)==caller`.
- view=job (waiting deferred job): `{"ok":true, **ContinuationStore.view(row)}`, i.e.
  `id, plan_hash, state(waiting_parent|waiting_for_device|<terminal>), stage:null, result, diagnostic:null,
  reviewed:0, created:null, finished:null, continues:{parent_job_id}`, `[resolved_plan_hash]`,
  `[device_queue_position]`, `[chain]` (`continuation.py:341-362`, broker.py:1116-1126).
  **Its shape differs from the stored-job view: it has no `review` key.**
- view=capture success: `CallToolResult(content=[TextContent(json sort_keys metadata), ImageContent(png base64)],
  structured_content={"ok":true,**metadata})`.
  - Failure: `isError=true`, text `json.dumps(body)` **without sort_keys**, body `{"ok":false,"error":str(BrokerError)}`.
  - **status/capture is the only legacy path that returns isError=true for a domain error.**
- [inferred] `chain` is never attached to registered-project (fetch/build) jobs. `job_principal()` reads
  `execution_requests` (broker.py:1080-1087), but `profile_jobs.execute` uses `store.add_job` (profile_jobs.py:501)
  and records no execution_request. Not exercised by any HTTP test (`test_deferred_build.py:140` checks only the broker-level `chain_progress`).

**Pinned by:**
- test_registered_targets.py (targets, metadata)
- test_client_identity.py
- test_chain_http.py::test_continuation_through_the_six_tools (job.chain, device.device_hold)
- test_capture_http.py
- test_derby_http.py
- test_work_session_http.py / test_operator_http.py / test_tunnel_ingress.py (view=session)
- test_source_preflight.py (view=source)
- stream=build: test_durable_build_logs.py, test_flash_http.py, test_source_http.py, test_flash_boot_evidence.py, test_input_diagnostics.py
- stream=device: test_device_observation_integration.py, test_restart_observation_integration.py, test_pending_execution_http.py
- stream=broker: test_authenticated_mock.py, test_http_integration.py, test_registry_http.py, test_serial_http.py
- No test for `view=apps` or `stream=app` through the surface, apart from the schema-presence check in test_app_broker.py and `AppSessionClient` in test_app_broker.py.

---

## 2. `submit` (M) — `surface.py:584-620`

**Input** `SubmitRequest` (`:165-192`, extra=forbid). Required: `kind`, `job_id`. Optional (nullable):
- `session` Capability
- `project_id`
- `source_commit` `^[a-f0-9]{40}$`
- `source_receipt` Digest
- `device_id`, `build_job_id`, `backup_job_id`, `flash_job_id`
- `lkg_receipt` Digest
- `derby_detail_mode` / `derby_dma_mode` (ON|OFF)
- `after` Crockford `^[0-9A-HJKMNP-TV-Z]{8}$`
- `lease_seconds` 1..300
- `app` AppSessionOptions (`app_models.py:21-41`, extra=forbid)

`kind` enum and its mapping to `TestName` (`KIND_TEST` :128-144):
| kind | test |
|---|---|
| fetch | firmware_fetch_source |
| build | firmware_build |
| backup | firmware_backup |
| flash | firmware_flash |
| restore | firmware_restore |
| app_session | app_session |
| derby_fixed | derby_fixed_workload |
| derby | derby_smoke |
| capture | capture_home |
| restart | restart_and_observe |
| observe | passive_observe |
| observe_firmware | firmware_observe |
| hello | hello |
| diagnostics | diagnostics |
| workflow | firmware_workflow |

**Rules** (`SUBMIT_RULES` :355-368)
| kind | required | optional |
|---|---|---|
| fetch | job_id, project_id, source_commit | – |
| build | job_id, project_id | source_receipt, derby_detail_mode, derby_dma_mode, after |
| backup | job_id, session, device_id | – |
| flash | job_id, session, device_id, build_job_id | backup_job_id, lkg_receipt |
| restore | job_id, session, device_id | backup_job_id, lkg_receipt |
| app_session | job_id, session, app | – |
| derby_fixed | job_id, session | flash_job_id |
| other device kinds | job_id, session | – |

`after` handling (`:592-600`):
- build: `after` stays optional.
- test ∈ `DEFERRED_DEVICE_TESTS` (all tests except source/build/backup/app_session; `models.py:42-44`): `session`
  is removed from required, `after` is added to required, and `lease_seconds` is added to optional. Flash and
  restore *are* deferrable.
- Otherwise `argument_not_allowed/after`.

Then:
- `backup_job_id` and `lkg_receipt` both non-null → `argument_conflict/lkg_receipt` (`:604-609`).
- Build `PrepareRequest(test=..., **fields_set)` via `_convert`. Its `PrepareRequest.flash_references` validator
  codes (`models.py:168-242`) surface as `{"ok":false,"error":<code>}` with no field, when public.
- Then `ops["prepare_test"]` (`server.py:737-832`) routes to:
  - `after` set and test not build/source → `resident.prepare_deferred` (broker.py:884-934). Scope: control, always.
  - build/source → `resident.profile_jobs.prepare` (control, always).
  - source → the separate async path `profile_jobs.prepare_source` (`:812-831`). Needs control **and** read, and
    has its own try/except with the same mapping as `_result`.
  - capture/derby/derby_fixed/firmware/restart/flash tests → control + `require_owner` always, then `owned()` → `resident.prepare` (broker.py:1682).
  - other device tests → `owned()`: control+owner only if auth_required.

**Output**
- Success: `{"ok":true,"plan_hash","plan":{...full plan...},["storage"],"approval_required":false,
  "execute_tool":"flash"|"execute"}`. `execute_tool` is `"flash"` iff test ∈ `DESTRUCTIVE_TESTS = {firmware_flash,
  firmware_restore}` (surface.py:65,618-619).
- The plan dict includes principal fields (`flash_principal`, `build_principal`, `source_principal`, `principal_id`,
  `log_delivery.principal_id`). Returning them is part of the current contract.

**Errors**
- Surface: `argument_required`, `argument_not_allowed`, `argument_conflict`, `argument_invalid`.
- PrepareRequest codes: `device_session_required`, `session_not_allowed_with_after`, `lease_seconds_only_with_after`,
  `after_only_for_registered_build_without_receipt`, `diagnostic_mode_requires_source_build`,
  `diagnostic_dma_requires_explicit_source_axes`, `flash_job_id_only_for_fixed_collector`,
  `source_fetch_requires_registered_project_and_full_commit`, `source_commit_only_for_fetch`,
  `source_receipt_only_for_registered_build`, `flash_references_only_for_flash_operations`,
  `registered_flash_device_required`, `project_id_not_allowed_for_flash_operations`,
  `backup_has_no_build_or_backup_input`, `choose_one_recovery_source`, `successful_build_job_required`,
  `restore_uses_backup_only`, `app_options_required_only_for_app_session`.
- Then broker BrokerError strings, e.g. `ticket_invalid`, `ticket_consumed`, `chain_broken`, `job_id_conflict`,
  `history_full_archive_offline`, `principal_not_authorized`, `registered_flash_operation_not_enabled`,
  `registered_flash_device_required`, `successful_owned_source_fetch_required`.
- `AppSessionOptions` validator errors (`duplicate_action`, `resume_requires_boot_and_run`,
  `fresh_requires_run_zero`) are caught first by pre-validation → `invalid_tool_request/invalid_field/["app"]`,
  `isError=true` **[probe]**.

**Pinned by:**
- test_mcp_input_http.py: `test_tools_list_discloses_flash_request_contract` (schema contains `flash|restore`,
  `fetch|build`, "Live lease for device kinds", "Nothing is queued", "execute_tool");
  `test_http_submit_rule_failures_return_fixed_codes_and_never_echo_input`;
  `test_http_flash_registration_and_operation_disabled_have_distinct_codes`;
  `test_http_source_receipt_requires_successful_owned_fetch_with_safe_code`.
- test_chain_http.py::test_rules_for_after_and_wrong_tool (backup+after → argument_not_allowed/after; session+after
  → argument_not_allowed; missing session → argument_required/session; unknown ticket → ticket_invalid).
- test_input_diagnostics.py.
- test_deferred_device.py::test_session_and_after_are_mutually_exclusive_and_backup_cannot_be_deferred
  (model-level).
- test_input_diagnostics.py::test_conditional_schema_requires_the_same_flash_references pins
  `PrepareRequest.model_json_schema()` allOf. **That schema is no longer published in tools/list**, so this is an
  internal-only contract now.

---

## 3. `execute` (M) — `surface.py:622-657`, helpers `wrong_tool` :427-439, `enqueue` :441-467

**Input** `ExecuteToolRequest` (`:195-219`, extra=forbid):
- `op` enum {run, app_input, home_recovery}, required.
- Nullable: `plan_hash` Digest, `session` Capability, `job_id`, `source_job_id`, `boot_id` U64 (strict, 1..2^64-1),
  `run_id`/`control_session`/`command_id`/`seq` U32 (strict, 0..2^32-1),
  `operation` Literal{action.tap,status,suspend,resume,stop}, `action` Literal{accept,back,up,right,down,left},
  `continues` bool, `chain_total_hint` 1..64.

**Rules** (`EXECUTE_RULES` :370-377)
| op | required | optional |
|---|---|---|
| run | plan_hash | session, continues, chain_total_hint |
| app_input | session, job_id, boot_id, run_id, control_session, operation | command_id, seq, action |
| home_recovery | job_id, source_job_id | – |

**op=run:**
1. If `auth_required`, `wrong_tool(plan_hash, …, "execute")`:
   - It loads the plan (`resident._store().plan`) and checks `plan_is_callers`. That means `principal(ctx,"control")`
     must succeed **and** every present key among `PLAN_PRINCIPAL_KEYS = (principal_id, flash_principal,
     build_principal, source_principal)` must equal the caller. If **no** such key is present, the plan is
     treated as not the caller's.
   - If the plan's test ∈ DESTRUCTIVE_TESTS → `{"ok":false,"error":"wrong_execute_tool","use":"flash"}`.
2. `enqueue()`:
   - `chain_total_hint` without `continues` → `argument_required/continues`.
   - `continues` on a plan without `session_id` that is not registered_project* or device_deferred →
     `argument_not_allowed/continues`. This plan lookup happens **before** any auth.
   - Then `ExecuteRequest(plan_hash, session)` → `ops["execute_test"]` (server.py:834-882), which routes to:
     - build/source plan → `profile_jobs.execute` (control);
     - `device_deferred` → `resident.execute_deferred` (broker.py:936-988; control);
     - else `owned()` → `resident.execute` (broker.py:1811-1902).

     The result is wrapped with `shared_job`.
   - On ok with `continues` → `body["continuation_ticket"] = resident.issue_ticket(id, hint)` (broker.py:1089-1114).
     On `BrokerError` → `body["continuation_error"] = str(exc)`, and the response stays `ok:true`.

**op=app_input:** `AppCommandRequest(**fields_set)` (`app_models.py:44-63`) → `ops["app_command"]`
(server.py:1110-1125: control + `require_owner` always) → `Broker.app_command` (broker.py:2177).
Its validator codes `action_only_for_tap`, `command_identity_required` and `reserved_cleanup_command` are not
public, so they surface as `{"ok":false,"error":"argument_invalid"}` with no field **[probe]**.

**op=home_recovery:** `RequestHomeRecovery(job_id, source_job_id)` → `ops["request_home_recovery"]`
(server.py:884-899; control always) → `resident.home_recovery.request`.

**Output**
- run: the job dict from `shared_job` plus `deduplicated` (bool). For deferred jobs it is the deferred view plus
  `deduplicated` (e.g. `state: waiting_parent|waiting_for_device`). Optionally `continuation_ticket` (8 chars) or
  `continuation_error`.
- app_input: app ACK dict.
- home_recovery: the recovery request dict.

**Pinned by:**
- test_chain_http.py::test_continuation_through_the_six_tools (`continues` → 8-char ticket; hint without continues
  → `argument_required/continues`; deferred `state`).
- test_pending_execution_http.py (`wait_for_approval` rejected; execute once; only the original control principal).
- test_flash_http.py::test_explicit_mcp_backup_flash_restore_and_cancel (backup via execute).
- test_home_recovery_http.py.
- test_app_client.py / test_app_broker.py (app_input).
- **There is no test for `wrong_execute_tool`.**

---

## 4. `flash` (D) — `surface.py:659-671`

**Input** `FlashToolRequest` (`:222-234`, extra=forbid): `plan_hash` Digest (required), `session` Capability|null,
`continues` bool|null, `chain_total_hint` 1..64|null. There is no discriminator and `_check` is not called.
Unknown or extra keys are handled only by pre-validation.

**Behaviour**
- If `auth_required`, `wrong_tool(…, "flash")`: a caller-owned plan whose test ∉ DESTRUCTIVE_TESTS →
  `{"ok":false,"error":"wrong_flash_tool","use":"execute"}`.
- Then the same `enqueue()` as op=run.

**Output:** same as execute op=run.

**Pinned by:**
- test_flash_http.py::test_explicit_mcp_backup_flash_restore_and_cancel (flash and restore via `flash`).
- test_pending_execution_http.py (no `wait_for_approval`).
- test_input_diagnostics.py (`required_field/plan_hash`, `invalid_field_format/plan_hash`).
- annotations test (§0.2).
- **There is no test for `wrong_flash_tool`.**

---

## 5. `cancel` (M) — `surface.py:673-733`

**Input** `CancelToolRequest` (`:237-250`, extra=forbid): `target` enum {job, request, ticket} (required); nullable
`job_id`, `session`, `plan_hash`, `resume_token`, `request_id`, `ticket` (8-char Crockford).
The description says "job or request" and omits `ticket`.

**Rules** (`CANCEL_RULES` :378-382)
| target | required | optional |
|---|---|---|
| job | job_id | session, plan_hash |
| request | resume_token, request_id | – |
| ticket | ticket | – |

**Routing**
- ticket: `principal(ctx,"control")` → `resident.cancel_ticket` (broker.py:1140-1154) →
  `{"ok":true,"cancelled":true}`. BrokerError → `_refuse(str(exc))`.
- request: `CancelDeviceRequest` → `ops["cancel_device_request"]` (server.py:1247-1260; control always) → broker.py:1536.
- job, in order:
  1. `resident.cancel_deferred(job_id, principal(ctx,"control"))` (broker.py:1128-1138). BrokerError is
     **swallowed**. A hit returns `{"ok":true, **deferred view}` (state `cancelled`, result `client_cancelled_waiting`).
  2. If `session is None`: `resident.cancel_job_only(job_id, principal(ctx,"control"))` (broker.py:990-995). This
     uses the broker's current lease when `lease.job_id == job_id` and `job_principal == caller`, then
     `Broker.cancel`. A BrokerError (including `authentication_required` / `insufficient_scope`) →
     `_refuse(str(exc))`. A hit returns `{"ok":true, **cancel result}`.
  3. `resident._store().job/plan` (direct store access).
     - `plan_hash` given and ≠ the job's → `job_plan_mismatch/plan_hash`.
     - A home_recovery plan requires `plan_hash` (`argument_required/plan_hash`) → `CancelHomeRecovery` →
       `ops["cancel_home_recovery"]` (server.py:901-908).
     - Otherwise `CancelRequest(job_id, session)` → `ops["cancel_job"]` (server.py:1189-1208): build/source →
       `profile_jobs.cancel` (control); else `owned()` → `Broker.cancel` (broker.py:1906-1955), wrapped in `shared_job`.

**Output:**
- Job dict (`state`, possibly `stop_requested`, `requires_review`), or the deferred view, or `{"cancelled":true}`.
- Errors: `only_queued_jobs_can_be_cancelled`, `job_session_mismatch`, `invalid_session`,
  `execution_intent_not_available`, `ticket_invalid`, `ticket_consumed`, `job_plan_mismatch`, etc.

**Behavioural nuances to preserve or decide on**
- [inferred] On an **unauthenticated** mock server, `target=job` without `session` always returns
  `authentication_required` from step 2, because `principal()` ignores `auth_required`. The legacy
  `cancel_job(session=None)` would instead have produced `invalid_session` / build-source behaviour.
- Step 3 reads the store and returns `job_plan_mismatch` before any identity check on the `session` path. This is
  an oracle across principals for "job_id exists with plan_hash X". The HTTP layer still requires some valid
  bearer when auth is on.

**Pinned by:**
- test_chain_http.py (`target=ticket` → `ticket_invalid`).
- test_work_session_http.py (`target=request`).
- test_flash_http.py (cancel).
- test_deferred_device.py / test_deferred_build.py (broker-level waiting cancel).
- **There are no tests for `job_plan_mismatch`, for the home-recovery cancel via the surface (check
  test_home_recovery_http.py), or for `cancel_job_only`.**

---

## 6. `session` (M) — `surface.py:735-819`

**Input** `SessionRequest` (`:253-269`, extra=forbid):
- `action` enum {open, request, renew, release, close}, required.
- Nullable: `owner` Origin {local_cli, remote_mcp}, `ttl_seconds` 1..86400, `lease_seconds` 1..300,
  `wait_seconds` **0**..3600, `after` 8-char ticket, `resume_token`, `session`.

**Rules** (`SESSION_RULES` :383-389)
| action | required | optional |
|---|---|---|
| open | owner | ttl_seconds, lease_seconds, wait_seconds |
| request | resume_token | lease_seconds, wait_seconds, after |
| renew | session | lease_seconds |
| release | session | – |
| close | resume_token | – |

**Routing**
- open without lease: `wait_seconds` present → `argument_required/lease_seconds`. Else `CreateWorkSessionRequest`
  (ttl default 3600) → `ops["create_work_session"]` (server.py:1210-1215). Needs control always, and
  `owner == principal.origin`, else `authenticated_owner_mismatch` → broker.py:1445.
- open with lease, `wait_seconds` 0 or absent: `ttl_seconds` present → `argument_not_allowed/ttl_seconds`. Else
  `AcquireRequest` → `ops["acquire_session"]` (`acquire_owned`: control + owner match only if auth_required) →
  broker.py:1289. **No work session is created** (a documented divergence, DESIGN-MCP-SURFACE.md:25-27). The
  output is the lease dict including the `session` capability. On a busy device the result is `device_busy`.
- open with lease and `wait_seconds` ≥ 1: create a work session, then `RequestDeviceRequest` →
  `ops["request_device"]`. The output is the **composite** `{**created, "device_request": <request_device body>}`.
  The top-level `ok` comes from `created`, so if the device request fails the call still has `ok:true` with
  `device_request.ok:false`, and the work session persists.
- request: `wait_seconds == 0` → `argument_invalid/wait_seconds` (the schema allows 0). Otherwise
  `RequestDeviceRequest` (defaults lease 30 / wait 300) → `ops["request_device"]` (control always) → broker.py:1489.
  The output includes `lease.session` when granted (test_chain_http uses `granted["lease"]["session"]`).
- renew: `RenewRequest` (default lease 30) → `ops["renew_session"]` (`owned`) → broker.py:1332.
- release: `LeaseRequest` → `ops["release_session"]` (`owned`) → broker.py:1342.
- close: `CloseWorkSessionRequest` → `ops["close_work_session"]` (control always) → broker.py:1559.

**Pinned by:**
- test_work_session_http.py (all 4 tests).
- test_authenticated_mock.py::test_authenticated_mock_binds_all_session_actions_to_origin.
- test_registry_http.py, test_tunnel_ingress.py (renew/release/close).
- test_chain_http.py (open + request).
- test_mcp_input_http.py (open with lease_seconds).
- test_input_diagnostics.py (`field_out_of_range/ttl_seconds`).

---

## 7. Public reason-code vocabulary (where defined)

1. **`PUBLIC_REASON_CODES`** (`public_reasons.py:10-86`), 72 codes. These are the only ValueError strings
   `public_reason()` lets through (`:110-115`).
   - Request shape: app_options_required_only_for_app_session, backup_has_no_build_or_backup_input,
     choose_one_recovery_source, device_session_required, diagnostic_dma_requires_explicit_source_axes,
     diagnostic_mode_requires_source_build, flash_job_id_only_for_fixed_collector,
     flash_references_only_for_flash_operations, project_id_not_allowed_for_flash_operations,
     registered_flash_device_required, restore_uses_backup_only, source_commit_only_for_fetch,
     source_fetch_requires_registered_project_and_full_commit, source_receipt_only_for_registered_build,
     after_only_for_registered_build_without_receipt, session_not_allowed_with_after,
     lease_seconds_only_with_after, successful_build_job_required, project_only_for_build,
     test_not_available_for_backend, explicit_request_invalid, approval_interface_retired,
     request_does_not_need_review.
   - Cursors/logs: cursor_ahead, build_log_not_available.
   - Source/shared resources: source_fetch_busy, source_cache_owner_conflict, source_linked_path_rejected,
     build_process_busy.
   - Registered build/flash prerequisites: artifact_count_limit, build_artifacts_missing, build_inputs_changed,
     build_target_mismatch, idf_project_required, linked_file_not_supported, linked_project_not_supported,
     profile_missing_environment, profile_output_limit, project_must_not_be_inside_broker_state,
     project_snapshot_limit, registered_build_project_required, registered_git_version_required,
     registered_node_16_required_for_source_project, registered_node_version_required,
     registered_tool_not_verified, external_windows_idf_required, absolute_registered_directory_required,
     registered_external_python_required, registered_github_repository_required, invalid_flash_plan,
     owned_build_artifact_not_available.
   - Offline review: build_interrupted_needs_review, build_output_release_unconfirmed, idf_build_failed_needs_review,
     build_tree_stop_unconfirmed, collector_binding_changed, fixed_trial_result_invalid, app_plan_invalid,
     profile_job_stopped.
   - Final commit / recovery: build_final_commit_not_authorized, build_security_configuration_not_authorized,
     firmware_final_commit_not_authorized, home_recovery_final_commit_not_authorized, home_recovery_history_full,
     home_recovery_source_changed, invalid_fixed_trial_commit, fixed_trial_job_binding_changed.
   - Fallback: `invalid_request`.
2. **`SUPPRESSED_REASON_CODES`** (`public_reasons.py:90-107`), 14 codes. They are documentary only, because
   `public_reason` never consults the set. **Inconsistency:** `profile_or_principal_no_longer_valid` is "suppressed"
   as a ValueError (firmware_build.py:558), but it is raised as a **BrokerError** in profile_jobs.py
   (150, 188, 267, 271, 359, 493), so `_result` returns it verbatim.
3. **Surface codes** (literals in surface.py): argument_required, argument_not_allowed, argument_conflict,
   argument_invalid, wrong_execute_tool (+`use:"flash"`), wrong_flash_tool (+`use:"execute"`), job_plan_mismatch,
   plus the `continuation_ticket` / `continuation_error` keys. The `unknown_view` / `unknown_kind` / … codes in the
   design (DESIGN-MCP-SURFACE.md §5.1) **were not implemented**. Unknown enum values give
   `invalid_tool_request/invalid_field_value`.
4. **Pre-validation codes** (`input_diagnostics.py:44-68`): error `invalid_tool_request`; reasons required_field,
   unexpected_field, invalid_field_format, invalid_field_value, field_out_of_range, field_limit_exceeded,
   invalid_field_type, invalid_field, invalid_arguments (and the dead MODEL_REASONS keys).
5. **BrokerError literals** (no central registry). There are 88 distinct literals across src, including:
   authentication_required, insufficient_scope, authenticated_owner_mismatch, principal_not_authorized,
   invalid_session, invalid_work_session, work_session_limit, work_session_not_open, lease_not_found,
   lease_expired_active_job_retains_ownership, device_busy, queue_full, wait_queue_full, job_not_found,
   job_id_conflict, job_session_mismatch, plan_not_found_or_changed, plan_session_mismatch, plan_backend_mismatch,
   execution_intent_not_available, recovery_review_required, history_full_archive_offline,
   only_queued_jobs_can_be_cancelled, only_waiting_requests_can_be_cancelled, request_not_found,
   request_session_mismatch, continuation_not_supported, chain_broken, ticket_invalid, ticket_consumed,
   build_log_not_available, flash_log_not_available, source_log_not_available, device_logs_not_available,
   capture_not_available, app_session_not_available, app_events_not_available, owned_home_recovery_required,
   owned_registered_project_job_required, home_recovery_*, broker_not_running, broker_closing,
   backend_preflight_failed, …. To regenerate the full list, run
   `grep -rhoE 'BrokerError\(\s*"[a-z0-9_]+"' src/cardputer_mcp`.
   - Re-wrapped dynamic strings: broker.py:895, 985, 1114, 1521 (TicketError: ticket_limit, ticket_invalid,
     ticket_consumed, hold_expired, ticket_void, queue_full; `continuation.py:84-289`); 1753-1755
     (BuildLocationError.code, FlashFailure / FlashAdmissionError str); 2207 (app run ValueError: stale_boot,
     stale_run, stale_session, conflict, app_input_stopped, action_not_in_plan, out_of_order, limit, queue_full,
     rate_limit; app_sessions.py:111-151); 2216 (app_events_not_available, cursor_ahead); profile_jobs.py:292,
     382; server.py:1056 (`builder.read_logs` ValueError).
6. **SourceError codes**: `SOURCE_ERROR_CODES` (`source_snapshot.py:115`), output via `.public()`, which can add
   `explanation` and `scan`.
7. **HTTP-layer bodies**: local_only, authentication_required, invalid_tunnel_route, remote_read_principal_required,
   request_rejected.
8. **stdio codes**: stdio_adapter_invalid_url, stdio_adapter_invalid_key, stdio_adapter_authentication_failed,
   stdio_adapter_endpoint_rejected, stdio_adapter_broker_unavailable, stdio_adapter_tool_outcome_unknown,
   stdio_adapter_remote_read_key_required, stdio_adapter_unsupported_tool_result,
   stdio_adapter_unsupported_tool_request, stdio_adapter_transport_failed.

---

## 8. Redaction-gate bypasses (surface.py and the legacy layer)

1. **`BrokerError` is never allowlisted.** `_result` (server.py:452), `read_source_resources` (:708),
   `prepare_test` source path (:827), `read_build_logs` (:1091), `read_capture` (:1170), and the surface's
   `_refuse(str(exc))` (surface.py:688, 712) and `continuation_error = str(exc)` (:466) all emit `str(exc)` verbatim.
   - Today every observed source is a literal or a fixed-code re-wrap (§7.5), but nothing enforces this.
   - `BrokerError(str(exc))` re-wraps arbitrary `ValueError` at broker.py:2207 and 2216 and server.py:1056. Any
     future non-literal ValueError there would leak. This is the main target for the planned
     `PublicError/to_public` gate.
2. **surface `cancel` → `cancel_job_only`** (surface.py:706-714) returns `{"ok":True, **Broker.cancel(...)}`
   **without `shared_job`**. The legacy path `cancel_job` (server.py:1208) applies `shared_job`, which strips
   `diagnostic[PROVENANCE_KEY]`, `derby_observation.diagnostics`, `derby_diagnostics` and `fixed_trial`.
   For a derby job cancelled through its own lease without `session`, the raw job row is returned. The caller is
   the job's principal, but this still diverges from the legacy redaction.
3. **surface `cancel_deferred`** (:698-705), **`cancel_ticket`** (:680-688) and **status waiting-job view**
   (:502-512) bypass `_result` / `read()` / `shared_job`. The data is low-risk: the deferred view has
   `diagnostic: null`.
4. **status view=job `chain`** (:514-527) is added after `shared_job`, gated by `job_principal == caller`.
5. **Direct store reads before authorization**:
   - `plan_for()` in `wrong_tool`. It is guarded by `plan_is_callers`.
   - `enqueue` `continues` check (:451-459), with no auth → oracle via `argument_not_allowed/continues`.
   - `cancel` step 3 (:715-720) → `job_plan_mismatch` oracle.
6. **status view=capture** returns the legacy `CallToolResult` unchanged (`isError=true` on error, unsorted JSON text).

## 9. Duplicated or divergent validation (6-tool layer vs legacy ops/models)

- **Required/forbidden per kind**: `SUBMIT_RULES` (surface.py:355-368) duplicates `PrepareRequest.flash_references`
  (models.py:168-242). Both check session-required, `after`/`session` exclusivity, `lease_seconds` only with `after`,
  backup/lkg exclusivity (surface `argument_conflict` vs model `choose_one_recovery_source`), and `after` only for
  build/deferred kinds (surface `argument_not_allowed/after` vs model `after_only_for_registered_build_without_receipt`).
  The surface check runs first, so the model codes are mostly unreachable through the surface. A few still reach
  the client, e.g. `device_session_required` when `session` is explicit null, and `diagnostic_mode_requires_source_build`.
- **Conflict semantics differ**: the surface `argument_conflict` fires only when both values are non-null. With
  explicit nulls (`lkg_receipt:null`) the call passes to PrepareRequest.
- **`after` rules are triplicated**: `submit` (surface.py:592-600), `PrepareRequest` (models.py:172-181), and
  `DEFERRED_DEVICE_TESTS` (models.py:42-44). The routing in `prepare_test` (server.py:785-790) is a fourth copy.
- **Safety-class routing**: `DESTRUCTIVE_TESTS` (surface.py:65) is separate from `FLASH_TESTS` (models.py:40,
  which also includes backup). The `execute_tool` hint and `wrong_tool` both use DESTRUCTIVE_TESTS. Problems:
  - `wrong_tool` runs **only when `auth_required`** (surface.py:634, 665), so it is off on an unauthenticated mock.
    [inferred] Flash plans cannot be prepared there anyway, because prepare requires a principal for FLASH_TESTS
    (broker.py:1700).
  - `wrong_tool` returns None for plans that carry **no** `PLAN_PRINCIPAL_KEYS`. Ordinary `broker.prepare` device
    plans carry none: diagnostics, hello, observe, workflow and capture put the principal only in nested
    `log_delivery` (broker.py:1758-1769, 1786-1797). [inferred] So `flash` would queue such a non-destructive plan
    instead of refusing it with `wrong_flash_tool`. The dangerous direction, a flash or restore plan through
    `execute`, stays guarded, because those plans carry `flash_principal` (firmware_flash.py:430) or `principal_id`
    (deferred, broker.py:917).
  - `plan_is_callers` ignores its `session` parameter.
- **wait_seconds**: schema allows 0..3600 for both `open` and `request`. `request` refuses 0 in code
  (surface.py:791-792); `open` maps 0 to the immediate acquire. The legacy `RequestDeviceRequest` requires ≥1.
- **ttl/lease defaults** come from the legacy models: ttl 3600, lease 30, wait 300, log limit 40, levels
  [error, warning], context 2, include_source_cache true.
- **Two error-shape regimes**:
  - Schema/type errors → `invalid_tool_request` (`isError=true`, reason+fields).
  - Rule errors → `argument_*` (`isError=false`, error+field).
  - Model-validator errors inside `_convert` → public code or `argument_invalid`.

  Some failures cross regimes: nested `app` validator errors land in the first; AppCommandRequest validator
  errors land in the third.
- **MODEL_REASONS** in input_diagnostics is a dead duplicate of the PrepareRequest codes (§0.4).
- **Auth checks are duplicated and inconsistent across ops**:
  - `read()` / `owned()` honour `auth_required`, but `principal()`, `work_action` and many ops authenticate
    unconditionally.
  - `prepare_test` runs `require_owner` twice for capture, derby and similar kinds (server.py:806-810: explicit,
    then inside `owned()`).
  - The surface calls `principal()` itself (wrong_tool, status/job, cancel) on top of the op's own checks.

## 10. Tests that pin the surface (summary)
| test | pins |
|---|---|
| tests/test_mcp_input_http.py (6 tests) | tool names, annotations, submit schema text/description, invalid_tool_request shape, submit rule codes (device_session_required, argument_not_allowed/project_id, argument_conflict/lkg_receipt), flash-disabled codes, source receipt code, `_result` masking |
| tests/test_input_diagnostics.py (3) | pre-validation reasons/fields for all 6 tools, REQUEST_MODELS == 6 tools, legacy names → None, PrepareRequest allOf schema |
| tests/test_chain_http.py (2) | continues/ticket/chain/device_hold, `after` rules, ticket cancel |
| tests/test_client_identity.py (1) | identity body incl. tool_set_version 2 |
| tests/test_registered_targets.py (3) | status schema/annotations, targets, argument_not_allowed/job_id, 401 bodies |
| tests/test_app_broker.py | 6-tool set, status/execute annotations, app_input/apps/app in schema |
| tests/test_pending_execution_http.py (4) | no approval/operator tool, wait_for_approval rejected on execute & flash |
| tests/test_stdio_adapter.py (14) | adapter catalog parity, listChanged false, fixed errors, no replay |
| tests/test_flash_http.py, test_work_session_http.py, test_capture_http.py, test_home_recovery_http.py, test_tunnel_ingress.py, test_authenticated_mock.py, test_derby_http.py, test_durable_build_logs.py, test_source_http.py, test_device_observation_integration.py, test_restart_observation_integration.py | end-to-end behaviour through the six tools |
| tests/test_mcp_diagnostics.py | GUI reads `$defs.Kind` / `$defs.View`, status(view=device) single call |

**Unpinned** (no test found by grep):
- `wrong_execute_tool` and `wrong_flash_tool`
- `argument_invalid`
- `job_plan_mismatch`
- `continuation_error`
- `status(view=apps)`, `stream=app` through the surface
- the `idempotentHint` absence
- `outputSchema` shapes
- text-content serialization format
- tools/list size or snapshot
- explicit-null semantics
- composite `session open+wait`
- `cancel_job_only`
