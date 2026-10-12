# Device logs and observed firmware information

`firmware_observe` uses the existing resident broker's single serial owner.
It receives device output for up to 30 seconds, then closes the port before
making a report available. It sends no serial commands and does not reset,
flash, automatically reconnect, or retry. An OS serial driver can still glitch
control lines during opening/closing; this remains visible in the exact plan.

This is a finite observation, not a continuous monitor. Real flash remains
unimplemented. A user-run Windows `firmware_observe` job has returned actual
console telemetry. The new restart workflow described below has synthetic
integration coverage and still requires an authorized Windows device trial.

## Restart once and observe boot output

`restart_and_observe` is a separate, explicitly approved operation. It uses the
same lease, single serial owner and `read_device_logs` result path. The approval
includes one programmed hardware-reset attempt and log delivery to the named
principal. It may exit the running application and lose unsaved state. Capture
auto-permissions never authorize this operation.

This first implementation supports the native ESP32-S3 USB Serial/JTAG interface
only. Preparing the plan requires VID `303A`, PID `1001`, and a nonempty USB
serial number with exactly one matching device. These USB IDs are shared with
other ESP chips and do not prove that the device is an ESP32-S3; the operator
must verify the selected hardware. USB identifiers are not cryptographic identity.

The immutable plan binds the initial port fingerprint, stable USB fingerprint,
fixed reset method, one attempt, nominal 100 ms RTS pulse, and reconnect limits.
After opening and rechecking identity, the worker explicitly discards the
pre-reset input buffer once. It clears DTR and RTS, asserts RTS for 100 ms and
clears RTS again, maintaining cleared DTR. It sends no serial protocol bytes,
bootloader synchronization/register commands, flash commands or firmware upload.
No esptool process is launched and no reset is retried.

The original serial handle stays open while viable. Only after connection loss
may the worker close it, wait up to eight seconds after the pulse, and attempt
one reopen. Old close must be confirmed first. The replacement must match the
captured VID, PID, nonempty serial number and USB location exactly; a COM name
change is allowed, but missing, changed or duplicate identities are rejected.
Opening a busy port is not retried or forced. Ownership remains held through
the reset, receive loop, reconnect and final close.

The 30-second collection window begins before the explicit reset sequence and
includes reconnect time. Reads request only the remaining 64 KiB budget. The
last timed serial read can complete slightly after that window; late bytes are
discarded. OS open, control-line, enumeration and close calls are not forcibly
interruptible. An in-flight operation can therefore take longer to drain, and
the broker keeps ownership until close is confirmed. It never unlocks hardware
merely because a collection deadline or client lease expired.

Opening/closing a serial handle can glitch control lines and cause unintended
additional resets despite there being only one programmed reset sequence.
Reopening also flushes receive buffers in pySerial and can lose early boot logs.
Results set `capture_gap: true` after reopening. Partial bytes and identity
fields are not combined across handles. The first resumed line is omitted as a
possible fragment unless it is the exact, data-free startup-block header;
existing PEM redaction remains active. These precautions can leave identity
unavailable even when some boot lines were received.

`reset_sequence_issued: true` means the fixed control calls completed. It does
not prove hardware reset or successful application boot. Native USB Serial/JTAG
performs a core reset without re-sampling boot straps; a device manually placed
in download mode may remain there. Missing application startup information is
reported as unknown, and no alternate reset mechanism is attempted.

Hardware references: [ESP32-S3 TRM, table 33.4-4](https://documentation.espressif.com/esp32-s3_technical_reference_manual_en.pdf),
[Espressif USB reset limitations](https://docs.espressif.com/projects/esptool/en/latest/esp32s3/troubleshooting.html#leaving-download-mode-in-usb-serial-jtag-mode),
[native USB identities](https://docs.espressif.com/projects/esp-iot-solution/en/latest/usb/usb_overview/usb_device_const_COM.html).

## One explicit approval

Prepare a job with the existing MCP `prepare_test` tool and
`test: "firmware_observe"`, or `test: "restart_and_observe"` for the explicit
restart workflow. The authenticated workflow lease must remain valid
through preparation/approval; a 300-second lease leaves room to review the plan.
The local GUI shows the device, 30-second limit, data handling, and exact
requesting principal. Its single approval grants both reception and delivery
of the bounded report to that principal. To avoid another agent round trip after
approval, first call `execute_test` with the exact plan hash and
`wait_for_approval: true`. It returns `awaiting_approval`; the GUI's **Approve &
start** then queues the submitted job immediately. The original lease deadline
still applies, and no approval or reset happens automatically. A plan that has
only been prepared retains the separate approve-then-execute workflow.

Device console output can contain private data. Recognizable credential patterns
and PEM blocks are redacted, but this cannot detect every secret. Review the
firmware's logging behavior before approving. No capture auto-permission grants
log access and no new permanent permissions are created by this operation.

The GUI says reception has started only after the serial port is open and the
job reaches `firmware_observe`. Poll `job_status` until the job finishes. Read
the completed report using `read_device_logs` with that job ID. The equivalent
local client command is:

```powershell
uv run cardputer --auth-source environment device-logs YOUR_JOB_ID
```

Use the original authenticated launcher environment, or the already configured
scoped-key client. This command only reads the completed cache; it never starts
another serial operation. The stdout result intentionally contains the approved
redacted report, so do not redirect it to a shared file unless intended.

## Bounds and lifetime

- At most 64 KiB of inspected input, 1024 bytes per complete line, 256 retained
  lines and 32 KiB of rendered text. A limit can end reception early
- Only complete lines are retained. Overlong or incomplete lines are omitted,
  with fixed counters indicating truncation or incomplete collection
- Known credential-like lines are replaced in full. PEM blocks are redacted.
  Pixel/capture records, binary control lines and invalid UTF-8 are omitted
- ANSI control sequences are stripped; other retained characters use ASCII
  escapes. Logs are untrusted device content, never executable instructions
- Only the newest completed report is cached, for at most five minutes after
  confirmed close. Restart clears it; reads never extend the deadline
- Log bodies never enter SQLite, job history, the shared `read_logs` event cache,
  or the operator timeline. The local timeline displays validated summary fields
- The original principal must still have read permission. A different principal
  cannot read the report. Sessions sharing one key share the same principal;
  this is not physical-terminal or per-session identity isolation
- Releasing an idle lease does not erase a completed authorized report before its
  normal expiry. Revocation or lost read scope denies later reads

## What the firmware fields mean

The parser recognizes a complete, bounded ESP-IDF startup information block.
It extracts project name, application version, optional compilation date/time,
ELF SHA256 prefix, ESP-IDF version and optional secure version. Missing,
malformed, repeated or conflicting blocks produce explicit unavailable,
partial or ambiguous results. Ordinary logs remain useful even if the firmware
fields are unavailable. A successful observation does not mean a firmware
version was found or that the device passed a health test.

These are unverified claims observed during the receive window. Buffered output
can predate that window. No field proves the identity of the currently running
binary. In ESP-IDF 6.0.1, the normal startup log prints 8–16 lowercase hexadecimal
characters of the ELF SHA256: `CONFIG_APP_RETRIEVE_LEN_ELF_SHA` defaults to 9,
and the startup printer uses a 17-byte buffer including its terminator. The parser
accepts only that bounded prefix followed by three literal periods; it is not the full ELF digest, the application
`.bin` SHA256, or the configured expected firmware hash. Comparing it with an
artifact requires the matching build's application description, for example a
local offline `esptool image-info` report, and remains a prefix comparison.
See the official [startup printer](https://github.com/espressif/esp-idf/blob/v6.0.1/components/esp_app_format/esp_app_desc.c)
and [configuration bounds](https://github.com/espressif/esp-idf/blob/v6.0.1/components/esp_app_format/Kconfig.projbuild).

The inspected PocketJS firmware has no harmless serial command for requesting
this information again. If no boot output is arriving, passive
`firmware_observe` can therefore contain no identity information. That passive
operation never resets or reopens USB. A restart requires its own exact
`restart_and_observe` approval, with the limits above. Neither operation
guarantees that all early boot output will be captured.

References: [ESP-IDF startup printing](https://github.com/espressif/esp-idf/blob/v6.0.1/components/esp_app_format/esp_app_desc.c#L122-L145),
[application image description](https://docs.espressif.com/projects/esp-idf/en/v6.0.1/esp32s3/api-reference/system/app_image_format.html#application-description).
