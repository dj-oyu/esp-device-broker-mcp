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

`restart_and_observe` is a separate, explicitly requested operation. It uses the
same lease, single serial owner and `status(view=logs, stream=device)` result path. The immutable plan
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

## Prepare and explicitly execute

Prepare `firmware_observe` or, for one fixed reset, `restart_and_observe` using
the existing `submit` tool (`kind=observe_firmware` or `kind=restart`) and a current authenticated owned lease.
Inspect the device, limit, effects and requesting principal, then submit the
exact `plan_hash` to `execute(op=run)`. It queues without local approval;
there is no approval argument. Preparation alone performs no I/O.

Console output can contain private data. Redaction cannot detect every secret;
consider firmware logging behavior before explicitly requesting reception and
retrieval by the requesting client. No capture policy or permanent grant is used.

Poll `status(view=job)`, then call `status(view=logs, stream=device)` with that job ID from the original
currently authenticated principal with read scope. The local equivalent is:

```powershell
uv run cardputer --auth-source environment device-logs YOUR_JOB_ID
```

This reads the completed bounded cache and never starts serial work. Its output
contains the redacted report; avoid shared files unless sharing is intended.

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
- Log bodies never enter SQLite, job history, the shared `status(view=logs, stream=broker)` event cache,
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
`restart_and_observe` execution request, with the limits above. Neither operation
guarantees that all early boot output will be captured.

References: [ESP-IDF startup printing](https://github.com/espressif/esp-idf/blob/v6.0.1/components/esp_app_format/esp_app_desc.c#L122-L145),
[application image description](https://docs.espressif.com/projects/esp-idf/en/v6.0.1/esp32s3/api-reference/system/app_image_format.html#application-description).
