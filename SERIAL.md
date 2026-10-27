# Opt-in serial adapter: device logs, observation, HELLO and HOME capture

Version 0.2 adds executable pySerial adapter code for the inspected
`pocketjs-hello-v1` firmware protocol. It is not a generic protocol for every
Cardputer firmware. Automated checks use synthetic serial objects. A user-run
Windows observation and HOME capture have succeeded, but this does not validate
all Windows driver behavior or the new firmware/log observation operation.
The original mock mode and demo remain available unchanged.

Supported operations:

- `passive_observe`: open the explicitly selected USB serial device, receive for
  a bounded configured duration, publish only recognized sanitized markers, close
- `firmware_observe`: receive up to 30 seconds of bounded device console output,
  close, and expose a redacted report plus any observed ESP-IDF startup fields
  only to the principal explicitly named in the immutable requested plan
- `restart_and_observe`: one explicitly requested native ESP32-S3 USB Serial/JTAG reset,
  bounded same-identity reconnect if necessary, and boot-log observation under
  the same owner; see [restart effects and limits](DEVICE-LOGS.md#restart-once-and-observe-boot-output)
- `hello`: exit the current app, select Apps category 0 and row 0, launch HELLO,
  press Enter once, validate startup/count/teardown markers, close
- `capture_home`: send one fixed s command with no navigation and receive one bounded
  render-buffer frame of the screen as it is now (HOME or a running app), close.
  A text editor takes s as input and does not answer, so that capture fails

This fixed serial backend has no flash, arbitrary reset command, JavaScript upload, generic shell, raw serial tool, GRID,
fault injection, arbitrary current-app screenshot, project build, ESP-IDF subprocess, or Tunnel setup
is included. See [CAPTURE.md](CAPTURE.md) for capture execution and evidence limits.
See [DEVICE-LOGS.md](DEVICE-LOGS.md) for log delivery, bounds and firmware identity limits.
The fixed HELLO key sequence is q, a, fourteen u presses, e, e, q.
It intentionally changes the running UI and exits the current application.

## Safety contract

1. Default startup is mock. Serial mode needs private configuration, the explicit
   `--enable-serial` runtime flag, and an explicit authentication source
2. Starting a serial-mode broker does **not** enumerate or open USB. Preparing a
   plan enumerates metadata only; only explicitly executing the exact plan opens it
3. Plans bind the backend, selected current port, complete USB-identity fingerprint,
   protocol, expected firmware configuration hash, effect description, session and
   job ID. Port or identity changes require a new plan and explicit execution
4. Both clients require distinct bearer tokens. Serial session ownership is bound
   to the authenticated client; changing an owner label or stealing another
   client's lease capability is insufficient to operate that lease
5. Serial jobs require a current authenticated control principal and its owned
   live lease. There is no per-job local approval, attestation keyword or capture
   policy. Verify HOME/menu expectations before explicit execution. A configured
   expected firmware hash is **not** proof of the running firmware hash.


6. One worker thread owns open/read/write/close. Event readers share that one
   stream and cannot open another port. Port access lasts only for the bounded
   requested job; idle broker operation does not continuously capture USB
7. DTR/RTS are set false before opening. Driver/OS opening or closing behavior can
   still glitch control lines or reset the device. This implementation makes no
   no-reset guarantee. Close all other serial terminals before execution
8. Cancellation waits for the actual reader/close to finish. A failed or unconfirmed
   close latches uncertain ownership, keeps the device marked connected/uncertain,
   and prevents lock release or further actions. It does not kill processes, steal
   busy ports or retry reset/close operations automatically
9. Any uncertain outcome becomes `needs_review`; never automatically rerun it.
   If close cannot be confirmed, stop the broker process yourself, verify device
   state, then use the offline review workflow. Do not merely reuse an old success
10. Logs expose exact allowlisted event names only. Raw serial bytes, unknown
    firmware messages, serial numbers and driver exception text are not persisted
    in the event cache. Raw bytes remain only in bounded process memory while
    validating a job. This is not a full serial-console recorder

The reader retains complete lines across arbitrary chunk boundaries, strips known
ANSI/ESP-IDF prefixes, drains preexisting buffered bytes before a command without
silently discarding fault evidence, rejects stale partial acknowledgements, and
checks ordered identity/readiness/frame and full settled MEM/stop/home sequences.
It has 90-second operation, 256 KiB byte, 16 KiB partial-line and 4,096-line bounds.
The underlying firmware has no command nonce; draining and strict ordering narrow
stale-response risk but cannot cryptographically bind a response to a key press.

## Windows 11: user-run setup

Recommended: use the bundled [one-command launcher](START-WINDOWS.md). It replaces
the manual token-generation and server-start steps 2 and 3 below, retaining the
same security policy. The manual procedure remains documented for troubleshooting.

Do not run these steps while hardware use is prohibited or another process owns
the device. Merely reading this guide does not grant hardware-operation permission.
The first test should be passive observation after reviewing its exact plan.

### 1. Configure the identity privately

Copy `examples/serial.example.toml` to `device.local.toml`. The example contains
synthetic VID/PID/serial values and cannot select a real device as written.

`uv run cardputer ports` explicitly reads metadata without opening a port. Keep
its real output private. Supply VID/PID plus USB serial number and/or location in
`device.local.toml`; do not add a hard-coded COM name. For HELLO, provide the hash
of the expected installed image and verify that image/14-row menu layout manually.
The broker checks app identity markers but cannot attest the running image itself.

### 2. Create fresh process-scoped authentication values

In the PowerShell window used for this one broker run:

```powershell
function New-RunToken {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}
$env:CARDPUTER_LOCAL_TOKEN = New-RunToken
$env:CARDPUTER_REMOTE_TOKEN = New-RunToken
```

Do not print/copy the values, save them in a profile, use `setx`, or put them in
user/system environment settings. They are process-scoped local credentials, not
OpenAI API keys. Generate a new pair for every launch; the application deliberately
does not modify persistent Windows environment or claim untested Windows file ACL
protection. A newly opened unrelated terminal will not have these variables.

### 3. Start one broker with inherited environment

Stop any mock broker already using port 8765 first. From that same PowerShell
window, start the serial broker in a separate console:

```powershell
$broker = Start-Process -FilePath 'uv' -ArgumentList @(
    'run', 'cardputer', '--auth-source', 'environment', 'serve',
    '--config', 'device.local.toml', '--enable-serial'
) -PassThru
```

The broker inherits those process environment values. Starting it does not open
USB. If the broker exits, check its console before continuing. Local process owners
can inspect their own environment; this is not protection against a hostile local
OS user or an agent already granted shell access.

### 4. Prepare and inspect passive observation

In the original PowerShell window:

```powershell
uv run cardputer --auth-source environment status
$lease = uv run cardputer --auth-source environment acquire --seconds 300 | ConvertFrom-Json
$plan = uv run cardputer --auth-source environment prepare --session $lease.session --job observe-001 --test passive_observe | ConvertFrom-Json
```

Inspect the target port, identity fingerprint, effects and bounded duration. Then explicitly submit execution:

```powershell
uv run cardputer --auth-source environment execute --session $lease.session --plan $plan.plan_hash
uv run cardputer --auth-source environment job observe-001
uv run cardputer --auth-source environment logs
uv run cardputer --auth-source environment release --session $lease.session
```

This is a genuine device operation: even passive open/close may affect control
signals. A successful observation means the bounded read/close completed; it is
not firmware, display or hardware-health verification.

### 5. Optional ordinary HELLO, separately requested

Acquire a new lease and prepare a **new job ID** with `--test hello`. Inspect the plan's UI changes and verify the expected device/firmware before
submitting its exact hash to execute. Expected markers
must match the configured firmware. Failure is quarantined without attempting a
recovery key, reset or flash. Do not treat an earlier manual success or an unrelated
GUI result as proof that this adapter has passed.

When finished and no job is active, stop the broker using Ctrl+C in its console,
then remove the process-scoped values from the original window:

```powershell
Remove-Item Env:CARDPUTER_LOCAL_TOKEN, Env:CARDPUTER_REMOTE_TOKEN
```

The second identity can be exercised locally using `--client remote_mcp`; it is
still a localhost client. **There is no OpenAI Tunnel connection in this release.**
No access to cloud accounts is created by these steps.

## Optional POSIX authentication file

On POSIX only, serial startup with `--auth-source file` generates fresh local
runtime credentials after obtaining broker locks. The state directory must be
owned by the current user with mode 0700; files are 0600, bounded, atomically
written and rejected if symlinked. Normal shutdown removes only its own unchanged
runtime file. A leftover file after a crash is not reusable against a new process.

A client uses `--auth-source file --auth-file .state/serial/runtime-auth.json`.
The Windows file mode fails closed; use the process-environment flow instead.
Do not publish runtime state or authentication files.

## Next required validation

- User-authorized Windows passive open/read/close with the intended hardware
- Device fingerprint stability and deliberate port re-enumeration, with no
  automatic substitution or busy-port recovery
- One explicitly requested HELLO cycle on the expected firmware; inspect physical
  behavior separately from serial markers
- Firmware backup/flash/restore are separate registered operations; validate their
  artifact, readback and ownership paths independently before physical use
- Authenticated Tunnel ingress and cloud access are separate future work. Do not
  expose this listener through a public proxy or tunnel as-is

## Fixed DERBY smoke

`derby_smoke` requires normal firmware, an appropriate HOME state,
an authenticated owner and explicit execution of its exact prepared plan. It opens once, sends Esc,
checks fresh HOME_READY, sends a, normalizes to row 0 with 16 u acknowledgements,
selects row 12 with 12 d acknowledgements, and sends e. Fresh APP_ID local.derby
must precede DERBY READY tones=true/false; wrong row/identity fails without retry.
The pinned normal firmware is cardputer-adv-pocketjs 6a139780. It prints DEMO
START/END and SCENE pad/gate/race/photo/res through the existing console. No
POCKET_KEYTEST build or new firmware write is required.

Observation is at most 120 seconds after READY, within a 180-second whole-job
reader budget. Auto-demo starts after 15 seconds of no input; a race's wall time
is frame-rate dependent. Capture sends at most one s only while the last observed
scene is photo, after a fresh quiet receive boundary still has that phase/race/seed.
There is no timed fallback or retry. Observation also stops early enough to reserve
18 seconds for the complete capture (including drains), 12 seconds for exit and
5 seconds margin within the existing 180 seconds. The original lease deadline remains enforced.
If no current photo is available in time, the broker sends its one planned q,
requires fresh HOME_READY and confirmed close, and completes with
`derby_photo_not_verified_no_capture`: no image and no claim that photo was tested.
Cancellation/authority loss still suppresses q. Image coherence and physical LCD
transfer remain unverified.
Only observed START, ordered gate/race/photo/res and END support the cycle result;
missing/out-of-order signals yield an image/time observation with limits, never
an inferred cycle success. Capture traffic can overlap phase text.

`status(view=job).diagnostic.derby_observation` separates `demo_cycle_observed`,
`photo_context_correlation`, and `image_acquired`. At most 64 structured events
record host milliseconds since the serial operation started, observed phase/race/
seed, photo observation, s request/completed write, received CAPTURE_BEGIN/END,
skip/exit reason and fresh HOME. Validated frame completion is a separate image
fact: a received marker alone cannot establish it. A phase/race/seed change during
the capture window prevents the correlated-context classification; absent or
truncated evidence never establishes a full cycle. No raw console or pixels are
persisted in these diagnostics. A matching serial context is temporal evidence,
not a firmware capture nonce or proof that the pixels depict a photo decision;
`photo_pixels_verified` remains false. Failure diagnostics retain the bounded
observations available before failure, including incomplete captures/failed close.

The immutable plan/effects now describe photo-only capture and reserved budgets.
Prepare and explicitly execute a fresh plan; a legacy fallback plan is rejected before USB
opens rather than silently executing the changed behavior.

One explicit q exit must produce fresh HOME_READY, then the owner closes serial.
Game exit can save the restored player state; this plan does not test player
input/save coverage. Cancellation, principal revocation or lease expiry blocks
all new sends, including q; the game may remain running. Close is always allowed
and uncertain close retains ownership/requires review. No arbitrary key, command,
retry/reopen or firmware write API is added. Rendered pixels use the existing
capture cache (five minutes in RAM, then the saved copy in state until
pruned beyond 200 images / 64 MiB); any read-scoped principal may retrieve it
while the original principal is still valid, without a separate image-delivery
decision;
raw game logs and unknown text are not persisted or emitted.

Firmware backup/flash/restore are implemented as separate explicitly requested broker operations; see FIRMWARE-FLASH.md.
