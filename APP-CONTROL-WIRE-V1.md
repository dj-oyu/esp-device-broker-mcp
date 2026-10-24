# App control v1 (2026-10-22)

Firmware and broker normative shared contract. Native endpoint available at HOME;
never a guest bridge session. No arbitrary source/path/raw serial.

## Transport

GS (0x1d), ASCII `C`, lowercase hex(body+CRC32), LF. Legacy GS `B` unchanged.
Reuse bounded bridge codec/CRC and usb_console_frame writer. Body header:
u8 version=1, u8 kind, u16 payload_length, u32 control_session, u32 command_id.
All integers little endian. CRC32 reflected 0xedb88320 over header+payload.
Max frame496/body492/payload480/wire995. No JSON dependency in firmware.

Kinds: CATALOG=1 OPEN=2 START=3 TAP=4 STATUS=5 SUSPEND=6 STOP=7 CLOSE=8;
response kind=128. Requests exactly60 bytes:
offset0 boot_id:u64; 8 run_id:u32; 12 seq:u32; 16 lease_fence:u32;
20 ttl_ms:u32; 24 app_id:char[32] (ASCII,NUL padded,max31);
56 arg:u8; 57 reserved[3]=0.
CATALOG uses seq as descriptor index (0-based), other fields zero, session/id0.
It returns current boot identity and descriptor/count; no serial open by status(view=apps).
OPEN uses known boot, run0, seq0, nonzero random session and nonzero lease fence,
ttl1..90000, command_id0, app_id empty, arg0. Repeated identical OPEN is idempotent,
never extends TTL; another session/fence is busy until cleanup completes.
START uses arg0=fresh(default),1=resume, registered app_id. Fresh discards dormant
guest, creates new run; resume requires same app and instance, else explicit rejection.
TAP arg:1 accept,2 back,3 up,4 right,5 down,6 left. No held or physical edges.
SUSPEND explicitly preserves guest/run or returns unsupported; STOP confirms guest
destruction. HOME_READY alone proves neither. CLOSE only succeeds after stop.
STATUS command_id0 reads session/run state; nonzero queries that command's ledger.
STATUS is read-only and never applies/replays input. Echo request id in header.
For START/TAP/SUSPEND/STOP/CLOSE positive command_id and increasing seq from1.
Identity includes boot/session/fence/run; START fresh requests run0, resume known run.
STOP/CLOSE remain permitted as bounded cleanup after command budget exhausted.

Response payload exactly120 bytes:
0 boot:u64;8 run:u32;12 command_seq:u32;16 fence:u32;
20 result:u8;21 state:u8;22 original_op:u8;23 arg:u8;
24 next_seq:u32;28 remaining_ttl_ms:u32;
32 catalog_index:u16;34 catalog_count:u16;36 flags:u32;
40 app_id:char[32];72 title:char[48] (NUL padded ASCII).
flags bit0 available; bit1 suspend supported; bit2 action.tap supported.
CATALOG descriptors bind actual launch sources; unsupported build-gated vmprobe
descriptor must be unavailable and START rejected. Unknown id=unknown_app.

Results: ok0, accepted1, consumed2, unknown_app3, unsupported4, stale_boot5,
stale_session6, stale_lease7, stale_run8, out_of_order9, conflict10, queue_full11,
blocked12, busy13, outcome_unknown14, limit15, expired16, invalid17,
teardown_failed18. States: no_session0,ready1,running2,suspended3,stopped4,needs_review5.
Accepted means admitted to finite owner queue; consumed means owner delivered to
defined action path, not JS/render/save completion. Recheck blocked/identity at
consumption. Same command id+identical bytes returns cached outcome; changed bytes
conflict. Sequence high-water survives ledger lifetime; no evicted command accepted.

## Limits and lifecycle

One session, request inbox4, dedup ledger64 entries/no eviction during session,
max60 user commands/actions per plan (reserves lifecycle slots),10 commands/s,
TTL<=90000ms, appid<=31 ASCII, no session extension. Broker one outstanding wire
command; retransmission is STATUS only after lost ACK. Never auto replay/reopen/
relaunch after disconnect/restart; unknown evidence=outcome_unknown.
Device TTL always tears down active or suspended controlled guest from UI owner.
Cancellation/timeout stops new input, requests bounded STOP then CLOSE under the
original authority/cleanup agreement; unconfirmed teardown/port close=needs_review.
Console flood does not evict ledger/ACK state; ACKs share serialized frame writer.

Broker retains existing auth/immutable plan/lease/jobs/one serial worker and
principal-bound bounded log cache. Add kind=app_session to the submit/execute workflow,
status(view=apps) (cached catalog with freshness, never opens device), execute(op=app_input),
status(view=logs, stream=app) (cursor,oldest_seq,next_cursor,has_more,gap,dropped_count,expiry).
Separate bounded control events from logs; 128 control events and256 log events,
per-line<=1024B,total logs<=262144B,retention<=existing bounded log TTL.
Existing redaction applies. Untagged console attribution=observed_during_run.
Plan fixes app/start/allowed actions/max_commands/rate/duration/log delivery and
end=stop/teardown including dormant eviction. No per-key approval.
Use common mock/host client for local.stress and ordinary local.hello; preserve
legacy benchmark parsers/quality gates, no quality-pass claim from transport.

## Review amendments (normative, supersede earlier offer wording)

control_session is DEVICE issued: CATALOG response header returns current offer.
Initial offer is random nonzero, increments on close/TTL/end; exhaustion/wrap
refuses new OPEN until reboot. OPEN must match offered value; only an ACTIVE
identical OPEN is idempotent. Never reopen a retired offer or reset its ledger.
Retain stopped ledger for STATUS until next new OPEN. Closed/expired requests
cannot acquire another guest. Match existing ledger id/content before current
run validation, so START(run0) duplicates remain queryable after actual run1.

CATALOG special index0xffff returns metadata: reply app_id[32] contains RAW
firmware app_elf_sha256 bytes (sole exception to ASCII), title[48] holds version.
Descriptor count is still present. Cache canonical descriptor snapshot plus
firmware hash/version/time. If no previously observed cache, prepare explicitly
binds source=unobserved/expected=null; handshake validates and records actual
firmware/catalog, without claiming prior firmware verification. Observed cached
binding is immutable in plan and must match handshake before START.

Non-frame console stdout/stderr escapes raw GS as visible `\\x1d`. Only framed
writer may emit raw GS. Thus log bytes are not a command/ACK channel; CRC alone
is not authentication. Preserve framed legacy B semantics and add host tests.
Inbox-full has a reserved rejection response path; a silent drop is not an
explicit queue_full acknowledgement. Control ledger never shares log capacity.
