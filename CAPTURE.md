# HOME render-buffer capture

`capture_home` is a bounded job in the existing queue. `status(view=capture)` reads its
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
configured firmware and close competing serial tools before explicitly executing a plan.
No frame is returned after a timeout, invalid frame, unexpected app marker,
disconnect, or uncertain close. Such jobs require review and are never retried.

## Prepare, execute and retrieve

1. Use the authenticated broker and obtain an owned live device lease.
2. Call `submit` with `session`, a new `job_id` and `kind="capture"` (the `capture_home` plan).
   Inspect the exact device, effects and expected firmware configuration.
3. Call `execute(op=run)` with that lease and exact `plan_hash`. It queues directly;
   There is no approval argument. Poll `status(view=job)`.
4. On success, call `status(view=capture)` with the job ID using the original current
   authenticated principal and read scope. No image approval or capture policy
   is required. The MCP result contains ImageContent and metadata; the host
   decides how to display or attach it.
5. Release the idle lease. Retained-image reads remain possible until expiry.

Serial startup still requires explicit opt-in and registered USB/firmware
configuration. Establish that the target is at HOME and close competing serial
applications before execution: Esc may exit an app and lose unsaved state.
Configured hashes do not cryptographically attest the running firmware.
Legacy local approval, image-decision and supervisor arming commands are retired.
Installing this change does not acknowledge existing uncertain work.

## Privacy and retention

The broker keeps at most eight images, each at most 200,000 PNG bytes, in a
five-minute RAM cache from successful completion. Every validated image is also
saved, unconditionally, to private state: `<state_dir>/captures/<job_id>/`
holds `capture.png` (the exact bytes) and `capture.json` (`schema_version` 1,
job id, owning principal, origin, `mime_type`, `size_bytes`, `sha256`,
width/height, source, `captured_utc`). Images never enter SQLite, event logs or
configuration files. After RAM expiry, eviction or a restart, reads are served
from the saved copy after re-verifying its size and sha256; a missing or changed
copy returns `capture_not_available`. Reading never triggers a replacement
capture. Saved captures are pruned oldest-first beyond 200 images or 64 MiB;
delete the directory yourself if you need them gone sooner. An image from a job
whose close was not confirmed is never saved. The operator GUI preview still
shows only the RAM cache. The job's fixed success/failure result remains in
ordinary durable history.

Every MCP image read requires a currently valid authenticated key with read scope,
and a succeeded closed capture job. Since 2026-10-25 (owner decision) the reader
does not have to be the principal that requested the capture: any read-scoped
principal gets the same image and metadata the owner would get, from the RAM
cache or the saved copy, so a local agent can inspect a capture started by the
cloud client. The capture's own principal must also still be valid with read
scope; revoking it ends delivery to everyone. Revoked or expired keys cannot
retrieve images. Distinct work sessions are not distinct security identities.
The trusted local operator can preview retained images through the local-only
operator API.

After the client retrieves a PNG, this broker cannot control copies retained by
the client, chat service, or user. Explicitly request capture only when the intended output
may be shared with that client. Cache limits do not promise secure RAM or disk
erasure.

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

For HOME escape/quit commands, `receive_summary.home_marker_shape` optionally
describes the first complete token-containing line after the command baseline.
It contains only bounded numbers, booleans and fixed categories: consecutive
standard prefix count (saturates at 4), normalized token offset (also the number
of bytes before the first token), bytes after that token (both saturate at
16,384), and whether the surrounding bytes contain only spaces/tabs. Normalization
is `exact`, `horizontal_padding`, `repeated_prefix`, or `other_surrounding`.
`candidate_lines` saturates at 2: 2 means multiple candidates, so the first shape
cannot explain every received line. A line spanning the command boundary is
marked `cross_command_partial`; even an `exact` shape is not proof of fresh HOME.
Incomplete lines have no shape. Every command resets this single-record slot.
The metadata travels through existing failure diagnostics/history; it does not
enable raw previews, retain text/fragments/hashes, add reads or
commands, alter exact marker acceptance, or acknowledge a review.

For a failure with `expected_marker = "HOME_READY"`, the frame command has not
yet been sent. A missing image in that case means no usable image was acquired,
not that the five-minute image retention period elapsed. Interrupted/restarted
jobs can instead have an unknown outcome. Existing review requirements remain;
upgrading, viewing diagnostics, or waiting never retries the job.

## Retired local raw-line option

The approval-bound one-line HOME preview is retired with its approval UI. This
change does not silently enable raw-line collection or enlarge log retention.
The bounded metadata-only receive diagnostics above remain available. Historical
raw text was not persisted and cannot be recovered from diagnostic counts.

## Protocol sources

- [Pinned text-screen and Esc handling](https://github.com/dj-oyu/cardputer-adv-pocketjs/blob/22f6d54a5c2fad6f7f40a8d5aa7cea7d7f371c51/main/main.c#L211-L225)
- [Pinned ordinary Esc mapping](https://github.com/dj-oyu/cardputer-adv-pocketjs/blob/22f6d54a5c2fad6f7f40a8d5aa7cea7d7f371c51/main/main.c#L360-L362)
- [Pinned render-buffer framing](https://github.com/dj-oyu/cardputer-adv-pocketjs/blob/22f6d54a5c2fad6f7f40a8d5aa7cea7d7f371c51/main/hal/board.c#L419-L439)
