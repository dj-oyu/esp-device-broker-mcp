# Separately requested firmware operations

Flash registrations are local, disabled by default, and separate from the
ESP-IDF 6.0.1 build profile. Register an external Python executable with its SHA
and exact version, and a dedicated esptool 5.1.0 package directory with its SHA.
An existing IDF Python environment need not be downgraded. No SDK is bundled.

MCP `submit` accepts `kind=backup`, `kind=flash`, and
`kind=restore`, a registered `device_id`, and successful owned job references.
It accepts no executable path, serial command, offset, or arbitrary arguments.
Every operation requires authenticated explicit execution of its exact prepared
plan. No local approval, unattended grant or mock opt-in is required. Backup bytes stay in a private local directory.

Flash requires the successful owned build's attested layout, sdkconfig, BIN and ELF
receipts. It does not require an LKG or a backup (see "Flashing without a recovery
point"); naming a device-bound full backup or a retained verified LKG adds a
pre-write recovery fence. Restore requires its own explicit request and
the original firmware BIN and ELF receipts registered locally. Security enabled
or uncertain, encryption, existing stubs, and ROM ranges above 16 MiB fail closed.
Initial chip detection has bounded fallback before identity verification; writes
have one attempt and block attempt, without reconnect/reflash or automatic restore.

Keep command side effects simple and explicit: backup reads and preserves a
private receipt, flash installs and leaves the verified new firmware installed,
and restore writes the exact prior backup only when separately requested. No command silently invokes another MCP command. Failure stops
without rollback, repeat writes or automatically invoking restore. Whether it also
needs review depends on what is known about the device; see
[Failure outcomes](#failure-outcomes).

Each native ROM probe/read/write connects in download mode and can reset the
selected device. Backup performs only probe/read and does not add a normal-boot
reset. Flash/restore completion performs one normal-boot RTS pulse (100 ms), then
up to 30 seconds of passive startup observation. A disconnect during that
observation permits at most one same-identity reopen after confirmed close,
without a second reset pulse. Cancellation/authority checks precede open/reopen,
reset assertion and reads; pulse deassertion and close remain allowed for cleanup.

Completion means exact readback, restart, and passive startup evidence matching
ESP-IDF 6.0.1 and the expected ELF prefix. It sends no HELLO keys. HELLO remains an
independent explicitly requested job. The ELF prefix is textual boot evidence;
readback independently verifies the complete written BIN bytes.

Cancellation and authority checks apply during passive reads as well as between
native phases. Interrupted work after a write began, or uncertain work, requires
review. Device ownership is retained until owned processes and serial ports have
certainly closed.
Shared filtered logs disclose bounded redacted progress and bounded boot logs,
never raw backups or private tool paths. Mock verification does not authorize
physical device access or prove a hardware flash succeeded.

## Bounded ROM read duration

The first 8 MiB hardware backup stopped after about 302 s with a 300 s native
limit. Progress, bytes read and native exit code were not recorded, so a deadline
is strongly suspected, not conclusively established. esptool 5.1 reads ROM flash
in [64-byte commands](https://github.com/espressif/esptool/blob/v5.1.0/esptool/targets/esp32.py#L352)
and offers a callback every KiB. A UART payload of 8 MiB at 115200 bit/s with
10-bit framing alone would take about 728 s; native USB need not obey that baud.

Only read operations use the fixed policy `max(300, 60 + ceil(bytes * 600 / MiB))`:
2/4/8/16 MiB receive hard limits of 1260/2460/4860/9660 s. This is a bounded
conservative allowance for ROM command overhead, not a measured throughput or
completion guarantee. Progress never extends the hard deadline. No advancing
progress for 60 s stops the owned process. Probe and write stay at 300 s.

The worker suppresses native output and emits only advancing byte counts/total.
The principal-bound shared log records start limits, counts, elapsed seconds,
returned native exit code, and explicit deadline/stall/cancel/authority stop codes.
Use `status(view=logs, stream=build)` with the `info` level to see numeric progress. Logs remain
bounded/redacted; backup bytes and tool paths are not delivered. A complete
verified read is still required before retaining a backup receipt.

Read authority is rechecked during owned execution. Cancellation terminates the
owned process tree and retains ownership until teardown is confirmed; uncertainty
still needs review. A lease must be live when its job is admitted (an expired
lease cancels a queued job before it starts). Once the job is running it keeps the
lease and device ownership until it finishes, even past the lease deadline:
the lease clock no longer revokes it. This matters for jobs requested ahead of
time, whose internal job lease (60 s default, 300 s maximum) is never returned to
the client and so can never be renewed. Principal revocation or scope loss,
explicit cancellation, broker shutdown, a broker epoch change and uncertainty still
stop a running job, and its owner can still cancel it after the deadline. The
lease is not extended: no stub, retry, implicit renewal, automatic rollback or
restore is added, and an expired lease ends as soon as its job has finished.

## Failure outcomes

A failed flash, restore or backup ends `failed` when the device state is known,
and `needs_review` when it is not. `needs_review` cancels every queued job
(`recovery_review_required`) and must be reviewed; `failed` keeps its result code
and leaves other queued work alone. The result code itself is never changed (some
codes keep their historical `_needs_review` suffix). Neither outcome promotes LKG.
The rule is in `src/cardputer_mcp/device_failure_outcome.py`.

| Where it failed | Driver/port close | Outcome |
| --- | --- | --- |
| Before `driver.write` was called: plan/input checks, probe, security, identity, pre-write fence (`lkg_current_firmware_changed_needs_review`, `flash_backup_stale_needs_review`, ...), pre-write read errors, authority lost or cancelled before the write | confirmed | `failed` |
| Same | not confirmed | `needs_review` |
| Backup (read only) read error or authority loss | confirmed | `failed` |
| `driver.write` started (or may have started), readback, boot verification, LKG staging/promotion, authority lost or cancelled after the write began | any | `needs_review` |
| Uncertain codes: shutdown uncertain, device busy/uncertain, reader handoff unconfirmed, backup persistence failed | any | `needs_review` |
| Unknown exception (state not established) or a failed final commit | any | `needs_review` |

"Write started" is recorded by the lifecycle immediately before it calls
`driver.write`; `driver.write` and `Store.finish_firmware` are unchanged.

## Verified last-known-good recovery

A successful flash automatically retains the exact BIN bytes plus manifest,
security and full ELF evidence in private SHA-256 objects. Identical objects are
shared. They are independent of the eight-entry build RAM cache and build output
retention. No automatic cleanup removes these recovery objects or the initial
full-device backup. The initial full backup remains a separate immutable rescue
baseline; no short boot prefix is expanded into a full ELF hash.

The IO worker stages immutable objects but never updates the LKG pointer. After
write/readback, same-device expected IDF/ELF boot evidence, confirmed close and
final lease/principal/cancel checks, one SQLite transaction records the successful
job and selects its receipt. Failure/cancel/crash before that commit preserves the
previous target. A committed successful job stays successful if the client loses
its reply. Successful explicit restore selects the version actually restored;
build timestamp does not select a newer version behind the user's back.

A flash that omits both `backup_job_id` and `lkg_receipt` names no recovery point
(below). Pass `lkg_receipt` to flash on top of a verified LKG with its fence.
Restore may omit recovery inputs to select the current target,
or pass `lkg_receipt` to select a previously verified exact target. Prepare freezes
that receipt/hash; execute never substitute dynamic latest. A
`backup_job_id` explicitly selects full-device recovery, including data.

The fast artifact path supports one factory app with exactly bootloader, table
and app BIN regions. MD5, bounds, labels, flags, non-overlap and erase-sector fences
are checked. Partition bytes, security settings and SPI geometry must be unchanged.
NVS/filesystem/otadata are not firmware write targets. When a flash names a recovery
point, current firmware bytes are checked against that LKG (or the named full
backup) before the write. Firmware-only restore also checks the current partition table; an
unreadable/changed table stops for separate recovery validation.

## Flashing without a recovery point

Decision of 2026-10-25, confirmed by the device owner. A `kind=flash` request
without `backup_job_id` and `lkg_receipt` prepares a plan with
`recovery_kind: "none"` and `lkg_receipt`, `backup_sha256`, `backup_job_id` and
`expected_device_sha256` all null. Neither prepare nor execute looks up an LKG or
a backup. Reason: ESP32-S3 ROM download mode cannot be bricked by app-region
writes, and recovery from a bad build is flashing a fixed build; demanding exact
recovery bytes and a pre-write fence only created dead ends (for example a device
whose earlier write did not commit no longer matches its LKG, and with backup and
restore disabled nothing could be flashed).

Such a flash has no recovery-bytes check, no pre-write comparison of current
device bytes and no partition-layout match against a previous LKG: the partition
table is written as part of the bundle, and partition/NVS/data compatibility is
not checked (the summary records `data_compatibility: "not_checked"`). Unchanged:
registration, `allow_flash` and tool hashes, the owned verified build and manifest
binding, the security probe, cancellation and authority checks, the single write,
full readback, boot ELF/IDF check, confirmed close, final authority and the atomic
commit that makes the build the device's LKG (`artifact_bundle`). The device
identity is bound at execution time from the probe just before the write; a probe
that cannot identify the device stops before any write. That identity is the only
one accepted for the write, every readback and the LKG metadata, and the boot
reconnect still requires the same USB device. The summary records it as
`bound_device_sha256` with `device_identity_basis: "probed_before_write"`. The
committed `firmware_lkg.baseline` is the flash job's own id (the column is NOT
NULL and no earlier baseline is involved). The plan's `effects` say that no
recovery point is required or kept; stored plans keep their original effects.

Flashes that name `lkg_receipt` or `backup_job_id`, restore and backup are
unchanged, including the pre-write fence. `status(view=targets)` reports
`flash_baseline: "not_required"` whenever flash is enabled, plus `lkg_recorded`.

This protects the MCP write ranges, not subsequent firmware behavior. Firmware
may erase or modify NVS/filesystems after boot, and firmware rollback cannot promise
data compatibility or undo those changes. No implicit restore, data migration,
retry, SDK fallback or additional MCP command is added.

Successful flash/restore stores a bounded, principal-scoped job summary atomically
with success. `boot_log_matches_expected_artifact` means required IDF 6.0.1 and
observed ELF prefix matched the expected artifact after full segment SHA-256
readback. `observed_elf_sha256_prefix` is 8–16 lowercase hex characters;
`observed_idf_version` records the matched IDF. `verification_basis` describes
readback plus boot prefix. These fields survive shared-log expiry and broker restart.
`boot_text_identity_verified=false` remains for compatibility: serial boot text
alone is not authenticated identity or cryptographic proof. No raw boot log is
retained, and no verification criterion or USB behavior changes.

## MCP submit input and safe diagnostics

All submit fields are nested under request. kind=flash requires job_id,
the flash kind, the caller's live owned session, registered device_id and a
successful owned build_job_id. Do not pass project_id: the registered device
selects its project. The public schema expresses these conditional requirements.
Optional project_id=null remains accepted. backup_job_id and lkg_receipt are
mutually exclusive; for a flash, leaving both absent names no recovery point, and
for a restore it selects the current verified LKG (preflight rejects unavailable recovery). No URL, port, path or raw argv is accepted.
kind=backup requires session/device_id and no build/recovery inputs;
kind=restore requires session/device_id and verified recovery, no build_job_id.
Current principal/device/security checks, static registered operation capabilities,
exclusive ownership and verified readback still apply.

Malformed tool input returns is_error with a bounded structured response:
{ok:false,error:invalid_tool_request,reason:<fixed code>,fields:[<safe field names>]}.
Only fixed model/type reasons and schema-owned field names are returned; values,
unknown extra-key names, patterns, raw exception messages and credentials are not.
Unknown validation failures retain a generic fixed diagnostic. This is diagnostic
metadata, not permission to retry or execute. Valid input still follows normal SDK
parsing and backend authorization.

Preflight distinguishes registered_flash_device_required (no matching registered
device) from registered_flash_operation_not_enabled (operation disabled). A build
whose source receipt lacks a matching successful owned fetch returns the fixed
successful_owned_source_fetch_required code rather than invalid_request. Source
errors expose only the existing fixed allowlist; arbitrary ValueError stays masked.

The tools/list descriptions and submit schema change. Flash and restore plans are queued only by the `flash` tool; every other plan by `execute`. After a separately
coordinated deployment, refresh the ChatGPT connection metadata and verify it in a
new conversation as documented in AGENTS.md. A server-side catalog test does not
prove that an existing host conversation refreshed its imported tool definitions.

## Failed boot observation evidence

`status(view=logs, stream=build)` retains bounded, redacted boot lines and a `boot_diagnostic`
entry after a failed flash/restore, including when serial transport or close
fails. The entry records the fixed operation/primary failure categories, startup
parse or ELF/IDF mismatch reason, actual UTC attempt times, attempt duration,
observation-window duration, inspected bytes/lines, and gap/truncation markers.
Observation time excludes final close; attempt time includes it. Discarded bytes
outside the collection window are not counted as inspected bytes. Console text
is not copied to broker stderr or the durable job/firmware summary.

These private logs use the existing requesting-principal read boundary and RAM
cache: at most eight jobs, 256 KiB/2,048 entries per job, expiring 300 seconds
after the last appended entry. The collector remains limited to 64 KiB input,
1,024 bytes per line, 256 lines and 32 KiB rendered text. Recognizable credentials,
PEM blocks and configured process secrets are redacted; unknown/obfuscated secrets
remain subject to the collector's documented best-effort limitations. Boot text
is still unverified device-reported evidence, not cryptographic identity proof.

Restart, eviction or expiry loses this diagnostic text; reads then report
`expired`/`gap`. No post-restart durability or automatic replay/recovery is added.
Failure still requires review and never promotes LKG. ESP-IDF 6.0.1, expected ELF
prefix, full readback, final authority and confirmed close checks remain required.
A slash in a plausible app version is accepted within the existing 31-byte ASCII
field bound; arbitrary controls, overlength or malformed values remain rejected.
