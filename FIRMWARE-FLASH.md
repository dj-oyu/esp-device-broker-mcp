# Separately approved firmware operations

Flash registrations are local, disabled by default, and separate from the
ESP-IDF 6.0.1 build profile. Register an external Python executable with its SHA
and exact version, and a dedicated esptool 5.1.0 package directory with its SHA.
An existing IDF Python environment need not be downgraded. No SDK is bundled.

MCP `prepare_test` accepts `firmware_backup`, `firmware_flash`, and
`firmware_restore`, a registered `device_id`, and successful owned job references.
It accepts no executable path, serial command, offset, or arbitrary arguments.
Every operation requires exact local approval, including with mock automatic
execution enabled. Backup bytes stay in a private local directory.

Flash requires the approved build's attested layout, sdkconfig, BIN and ELF
receipts and a device-bound full backup. Restore requires its own approval and
the original firmware BIN and ELF receipts registered locally. Security enabled
or uncertain, encryption, existing stubs, and ROM ranges above 16 MiB fail closed.
Initial chip detection has bounded fallback before identity verification; writes
have one attempt and block attempt, without reconnect/reflash or automatic restore.

Keep command side effects simple and explicit: backup reads and preserves a
private receipt, flash installs and leaves the verified new firmware installed,
and restore writes the exact prior backup only when separately requested and
approved. No command silently invokes another MCP command. Failure stops for
review without rollback, repeat writes or automatically invoking restore.

Each native ROM probe/read/write connects in download mode and can reset the
selected device. Backup performs only probe/read and does not add a normal-boot
reset. Flash/restore completion performs one normal-boot RTS pulse (100 ms), then
up to 30 seconds of passive startup observation. A disconnect during that
observation permits at most one same-identity reopen after confirmed close,
without a second reset pulse. Cancellation/authority checks precede open/reopen,
reset assertion and reads; pulse deassertion and close remain allowed for cleanup.

Completion means exact readback, restart, and passive startup evidence matching
ESP-IDF 6.0.1 and the expected ELF prefix. It sends no HELLO keys. HELLO remains an
independent existing approval job. The ELF prefix is textual boot evidence;
readback independently verifies the complete written BIN bytes.

Cancellation and authority checks apply during passive reads as well as between
native phases. Interrupted or uncertain work requires review. Device ownership
is retained until owned processes and serial ports have certainly closed.
Shared filtered logs disclose bounded redacted progress and approved boot logs,
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
Use `read_build_logs` with the `info` level to see numeric progress. Logs remain
bounded/redacted; backup bytes and tool paths are not delivered. A complete
verified read is still required before retaining a backup receipt.

Read authority is rechecked during owned execution. Cancellation terminates the
owned process tree and retains ownership until teardown is confirmed; uncertainty
still needs review. Clients must explicitly renew a live lease for long reads.
No stub, retry, implicit renewal, automatic rollback or restore is added.
