# Fixed native diagnostic operation

`submit(kind="native_diagnostic", job_id=..., session=...)` prepares a device job;
`execute(op="run", plan_hash=..., session=...)` runs that exact plan. It requires
an authenticated owner, a live device lease, the registered serial device and
configured expected firmware hash. It is serial-only. Existing mock `diagnostics`
and DERBY operations keep their previous behavior. There is no raw key, shell,
path or arbitrary configuration input.

Build the registered source through its owned source receipt with
`native_diagnostic_mode="ON"` to select `CONFIG_KSN_DEVICE_PROBE=y`, or explicitly
`OFF` to disable it. The selector, source identity, fingerprint, isolated output,
compiler cache namespace and actual sdkconfig hash remain bound together.
A configured firmware hash and the protocol's enabled flag do not attest installed
firmware or prove performance. CI plus physical 20fps remain separate acceptance.

## Wire contract

All integers below describe bytes. GS is `0x1d`; LF is `0x0a`. Frames are ASCII,
without CR. IDs are 32 lowercase hexadecimal characters; boot nonces are 16.
The current boot nonce must be nonzero. Enabled is ASCII `0` or `1`.

```text
GS D1 REQUEST|STATUS|CANCEL <id32> <boot16> LF
GS ND1 <id32> <state> <flags2hex> <boot16> <enabled> LF
```

Host frames fit 64 bytes (maximum 62); firmware replies fit 80 (maximum 77).
Canonical valid vectors are in `tests/fixtures/native_diagnostic_v1.json`.
The broker ignores ordinary console text and bounds partial reserved frames.
Malformed ND1 responses, contradictory flags, wrong IDs and boot changes fail
closed without retaining raw protocol data in public diagnostic fields.

`STATUS <zero-id> <zero-boot>` is a read-only bootstrap query. Its response is
`REFUSED 00` with the current nonzero boot nonce and enabled flag. Zero ID is
reserved for that query. Unknown STATUS/CANCEL, reserved-ID REQUEST/CANCEL and
boot mismatch return `REFUSED 00`; boot mismatch changes no request record.
A disabled firmware consumes complete reserved frames and refuses REQUEST with
`REFUSED 20`; malformed/truncated/oversized input is discarded to LF or next GS,
never delivered as ordinary keys.

Firmware admits at most one work item and retains eight request identities per
boot, without eviction. Terminal and admission-refused IDs remain recorded and
never execute again. Once full, new REQUESTs receive `REFUSED 20`. The broker
sends exactly one REQUEST, deriving its identity from the immutable prepared job,
including lease and broker epoch. It fixes the bootstrap nonce for that run;
there is no REQUEST retry, automatic reopen, reset or fallback key sequence.

## Completion and cancellation

Flags are: `01` work finished; `02` output drained; `04` checked HOME restored;
`08` borrows released; `10` owner released; `20` work not started. Flags `01`
and `20` are mutually exclusive.

Only `COMPLETED 1f` succeeds. `CANCELLED 1f` confirms completed work followed by
cancelled terminal cleanup; `CANCELLED 3e` confirms skipped work plus the full
admission/acquire/drain/checked-HOME/final-drain/release path. `REFUSED 20` is an
ordinary admission failure with work not started. `REFUSED 00`, `QUARANTINED`,
malformed proofs and missing terminal responses never count as completion.

A client cancel sets a request event; it does not finish the job. Before REQUEST,
that event can cancel locally without starting firmware work. This records
`NOT_REQUESTED` and confirmed serial close, with HOME/drain/borrow/owner proof
false. Once REQUEST may have been sent, the broker sends at most one typed
CANCEL while authority remains valid, polls STATUS and waits for terminal proof.
CANCEL_REQUESTED is an acknowledgement, not completion. A completion racing a
cancel may already have completed successfully.

REQUEST admission and client cancellation share a short control lock, after the
last loop authority check. Cancellation winning that cutoff prevents REQUEST;
admission winning establishes that work may start and must reach firmware
terminal proof. The lock spans neither a loop callback nor serial IO.

The entire reader budget is 90 seconds, including a three-second bootstrap
budget; STATUS polls are at most once per 250ms. Authority guards apply to every
open/read/write. Loss of authority blocks CANCEL and STATUS too; it never grants
an exception for cleanup writes. OS close is always allowed and is confirmed
separately. No blocking serial work runs on the resident event loop.

Missing work/ownership terminal proof retains the existing backend uncertainty
latch and device ownership even when OS close succeeds. Close failure also holds
ownership. The job needs review and queued successors are cancelled. Existing
explicit operator recovery requires confirmation that device work has stopped;
OS close by itself does not prove HOME or firmware owner release.

Firmware status/cancel must remain responsive while UI diagnostic work blocks:
the firmware owns its atomic snapshot/cancel path and a bounded emitter suitable
for its 2KiB input-task stack. Broker host tests do not establish that firmware
concurrency, stack safety, installed capability or physical performance.
