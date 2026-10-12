# HOME render-buffer capture

`capture_home` is a bounded job in the existing queue. Version 0.8 adds
[GUI approvals and optional capture permissions](CAPTURE-PERMISSIONS.md). `read_capture` reads its
completed PNG without opening USB. Mock mode returns an image with **TEST / NOT
DEVICE** visibly drawn into the pixels. The serial implementation is covered by
synthetic-port tests. One user-run Windows HOME capture has succeeded; this does
not establish general device/driver reliability or explain earlier marker timeouts.

## Updating an existing Windows folder

Version 0.7 includes the shared stdio adapter and HOME capture. No dependency was
added. Finish active work, release sessions, and confirm there are no waiters
before stopping your own GUI/Tunnel/broker. Replace the archive's source files in
the same MCP project folder, preserving private `*.local.toml`, `.state`, key
registries, and your external client env file. Do not delete the project folder.
Run `uv sync --frozen` there, then start the usual GUI in mock mode first.

A restart invalidates live session capabilities and does not replay old work.
Existing `needs_review` records remain subject to explicit local review. Tunnel
connection remains a separate user action; installing this source opens no USB
and creates no access keys.

## Effects and evidence

Real capture requires the operator to verify the physical device is already at
HOME. It sends one fixed Esc (`0x1b`) and waits for a fresh `HOME_READY`, then
establishes an empty quiet serial boundary and sends one
fixed `s`. There is no raw serial or arbitrary key tool. The same serial worker
receives the entire legacy frame, detects fault markers, and closes the port.
Only a confirmed close and complete validated frame can publish the image.
If the initial state is wrong, Esc can exit an app/editor and lose unsaved state.
It is never repeated to force navigation. The text screen treats `q` as text,
so it is not used as a Back command here. Opening/closing USB can still glitch
driver control lines or reset the device.

The inspected PocketJS firmware emits a 240×135 RGB565 render buffer using
`CAPTURE_BEGIN`, 135 ordered `PIX` rows, and `CAPTURE_END`. The decoder validates
exact dimensions, row order, length, and bounds before producing PNG. A successful
image proves that the software buffer was received. It does not prove a coherent
instantaneous snapshot or that the LCD accepted/displayed that frame. Metadata
reports `coherent_frame` and `lcd_transfer_ack` as `unknown`.

This legacy protocol has no request nonce and no cryptographic firmware identity.
A fresh HOME acknowledgement and quiet boundary narrow stale-data confusion;
they cannot attest the physical display. The local operator must verify the
configured firmware and close competing serial tools before approving a plan.
No frame is returned after a timeout, invalid frame, unexpected app marker,
disconnect, or uncertain close. Such jobs require review and are never retried.

## Mock first, then an explicitly approved physical job

1. Start the authenticated mock broker through the normal GUI/launcher. Obtain a
   logical work session and device lease with your authenticated client.
2. Call `prepare_test` with `request.session`, a new `request.job_id`, and
   `request.test = "capture_home"`. Inspect the returned plan.
3. If approval is required, the local operator approves its exact hash using
   `cardputer approve --state-dir PRIVATE_STATE_DIRECTORY --plan PLAN_HASH`.
   Capture may also be permitted by an explicit operator key policy; otherwise
   use the job's inline approval in the GUI timeline or this CLI. The existing explicit mock setting still applies.
4. Call `execute_test` with the lease and plan hash, then inspect `job_status`.
   A repeated execution request for the same job is deduplicated.
5. On success, approve the actual image in the same GUI timeline card, unless an explicit
   operator policy already permits image delivery for this key. Then call
   `read_capture` with `request.job_id`. Its MCP result contains
   ImageContent plus metadata; stdio forwards both. The host application decides
   how to display or attach the PNG. A JSON-only CLI status call does not save it.
6. Release the idle lease explicitly. Reading the retained image remains possible
   with the same current authenticated identity until it expires.

For real hardware, select the serial backend explicitly, configure the expected
firmware hash and USB fingerprint, and inspect the exact capture plan locally.
Approval requires `DEVICE HOME AND FIRMWARE VERIFIED` and expressly acknowledges
Esc/navigation and unsaved-state risk, USB-open risk, and allowing the
requesting client to retrieve/transmit rendered pixels to its AI service.
The broker never creates its own permission grants. Existing explicit operator
policies may permit a capture plan; otherwise use the GUI timeline's inline approval or the local CLI. Existing `needs_review` history is not
cleared by upgrading, switching tools, reading images, or restarting.

## Privacy and retention

The broker retains at most eight images, each at most 200,000 PNG bytes, for five
minutes from successful completion. Images are kept only in process memory, not
SQLite, event logs, configuration files, or automatic filesystem exports. New
captures evict the oldest retained image when full. Expiry, eviction, and restart
return `capture_not_available`; reading never triggers a replacement capture.
The job's fixed success/failure result remains in ordinary durable history.

Every MCP image read requires a currently valid authenticated key with read scope,
the same principal that owned the capture job, and either exact image approval or
a currently valid operator image-delivery policy. Revoked or expired keys cannot
retrieve images. A replacement key is a new principal and does not inherit old
images. Two tools deliberately sharing one key can read one another's captures;
distinct work sessions are not distinct security identities. The trusted local operator can preview retained images through the local-only
operator API. The MCP image-read tool remains bound to the original principal.

After the client retrieves a PNG, this broker cannot control copies retained by
the client, chat service, or user. Approve capture only when the intended output
may be shared with that client. Cache limits do not promise secure RAM erasure.

No current-app capture, firmware modification, flash, upload, OCR, remote file
write, automatic image publishing, or physical-device execution is included in
this implementation's verification.

## Diagnosing an unsuccessful capture

Version 0.9.2 adds a fixed `receive_summary` to serial failures. It contains the
last allowlisted command category, whether its write completed, receive counts
before and after that command, and the byte count of an incomplete trailing line.
The two receive groups count complete lines, standard ESP log prefixes, ANSI
formatting, the literal HOME marker, boot/reset text patterns, capture record
prefixes, binary/control text, and unclassified lines. These indicators can
overlap. A line belongs to the group in which its newline was received. Before
any command, received data appears in the initial after-command group with
`last_command = "none"` and `command_write_completed = false`.

These are diagnostic counts only. A HOME token inside an unrecognized line never
satisfies the fresh exact `HOME_READY` requirement. Boot/reset text can be old
buffered output and does not prove a new reset. A successful host write does not
prove the firmware processed the byte. USB control-line behavior is unchanged.
No raw text, arbitrary prefix/tag values, device identifiers, pixels, or exception
messages are retained by these counters. Earlier failures cannot be reconstructed
because their original serial text was not stored.

For a failure with `expected_marker = "HOME_READY"`, the frame command has not
yet been sent. A missing image in that case means no usable image was acquired,
not that the five-minute image retention period elapsed. Interrupted/restarted
jobs can instead have an unknown outcome. Existing review requirements remain;
upgrading, viewing diagnostics, or waiting never retries the job.

## Optional one-line local diagnosis

Version 0.9.3 offers an unchecked option beside the manual **serial capture**
approval: keep one HOME diagnostic line locally for this job. The operator must
still review the exact plan and enter the HOME/firmware attestation. Existing
capture policies never enable this diagnostic by themselves. The setting is not
persistent, cannot be requested over MCP, and does not execute a job.

Only the first complete line containing the fixed HOME token, received after
the approved Esc write and before the HOME wait ends, is eligible. At most
1,024 wire bytes are retained in RAM, with a 120-second deadline from execution
start. A longer candidate consumes the single selection but stores no excerpt;
the GUI reports **line too long**, not a truncated complete line. Capture/pixel
record families are rejected. Before-command traffic is never selected.

After the job ends, the same local timeline card shows the selected line and its
remaining lifetime. Spaces, tabs, CR, ANSI escapes, backslashes and non-ASCII bytes
are displayed using visible escape sequences; the GUI never interprets them.
The fixed shape summary distinguishes padding, token position and repeated
prefixes without exposing their text. This does not change the strict HOME
acknowledgement or turn arbitrary token-containing messages into valid markers.

The line may contain private application text or credentials. It is available
only through the authenticated local operator route, bound to the exact job,
plan, broker epoch and original principal. Ordinary MCP status, jobs, logs,
timeline metadata and SQLite contain no line text. There is no automatic save,
clipboard copy, upload, or cloud-sharing command. The operator can inspect it
locally and manually share only a selected, checked portion when appropriate.

Expiry, broker shutdown/restart, or loss of the original principal's required
scopes clears the retained line. GUI expiry, stale/failed authentication refresh,
identity changes and closing clear its display. Lease release alone does not
extend or shorten the remaining diagnostic lifetime. Closing a busy device still
holds ownership until the existing safe boundary; diagnostic expiry never
interrupts hardware work. Copies manually shared by the operator are outside
this lifetime, and RAM cleanup is not a promise of forensic secure erasure.

This is a diagnostic feature, not a fix for an unverified wire format. No failed
job is retried, no review record is cleared, and no control-line setting is
changed by enabling it. Existing private TOML, state and client keys remain in
place during the usual source update.

## Protocol sources

- [Pinned text-screen and Esc handling](https://github.com/dj-oyu/cardputer-adv-pocketjs/blob/22f6d54a5c2fad6f7f40a8d5aa7cea7d7f371c51/main/main.c#L211-L225)
- [Pinned ordinary Esc mapping](https://github.com/dj-oyu/cardputer-adv-pocketjs/blob/22f6d54a5c2fad6f7f40a8d5aa7cea7d7f371c51/main/main.c#L360-L362)
- [Pinned render-buffer framing](https://github.com/dj-oyu/cardputer-adv-pocketjs/blob/22f6d54a5c2fad6f7f40a8d5aa7cea7d7f371c51/main/hal/board.c#L419-L439)
