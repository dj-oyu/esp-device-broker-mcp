# Bounded DERBY diagnostics

An explicitly requested `derby_smoke` collects supported numeric telemetry and
fixed camera/error categories through its existing single serial reader. There
is no extra mode, key, read loop, retry, reset or firmware operation. Missing
telemetry does not change the smoke's phase/capture/exit result.

Use the existing `job_status` with the observed job ID. The requesting principal,
with current read authority, receives `derby_diagnostics` when a new report was
persisted. Other readers retain the existing shared job state and earlier bounded
phase summary. The new details are removed from shared diagnostics, execute
deduplication and cancellation responses. Old jobs without these details remain
readable. Reading a report never replays the job.

## Report and bounds

The `observation` contains numeric samples, fixed camera/tier/error events,
per-camera/tier valid-window summaries and explicit quality information. It
retains at most 180 samples, 64 events and 120 KiB of serialized diagnostic
payload. The private report envelope, including provenance, is limited to
128 KiB. Once a limit is reached, retention stops and truncation is reported;
older rows are not silently overwritten. The existing receive, framing and
whole-operation limits still apply.

The wire contract is pinned to firmware commit
`9db14c8825b389fba98b541b4b7b712574f9346e`. This identifies the parser reference,
not an assertion that those firmware bytes are installed. `DERBY_TLM` must be a
complete bare version-1 line with exactly the emitted fields and ten cumulative
camera intervals. Camera/error messages additionally accept the firmware's
bounded `I/W/E (timestamp) js:` logging form and known ANSI colors. Arbitrary
prefixes are rejected. No raw console text, pixel rows, exception suffixes,
arbitrary plan names or credentials are retained in the diagnostic payload.

Fully received fatal events are recorded before the existing fault stop. A fatal
marker recognized before its newline still stops immediately; the collector
does not wait for more bytes and reports possible partial-line loss.

## Interpreting values

- WIDE panning cameras are 7, 8 and 9. A valid window requires the pinned firmware
  parser's identity, consecutive sequence, completed-context and cumulative
  camera-interval checks. Matching endpoint camera names alone is insufficient.
- `weighted_diagnostic_fps` is total valid completed intervals divided by their
  total device time. `max_window_diagnostic_fps` is the largest individual valid
  window. Neither is total LCD/render FPS or a full-run performance claim.
- `sampled_heap_lower_bound_bytes` is the maximum received guest-allocation
  sample, a lower bound on the true allocator peak. Evaluation checkpoints are
  separate measurements; zero means unavailable. They are not whole-device heap
  or unsampled peak measurements.
- Device microseconds and host receive milliseconds have different clocks.
  Sequence gaps, counter/run changes, refused output, observed limits, malformed
  rows and truncation are explicit. Missing data is not zero. The last received
  record cannot establish the outcome of its own output attempt.
- Instrumented USB output can block for approximately 100 ms plus tick rounding.
  Values describe this diagnostic firmware and transport; no correction or
  extrapolation to ordinary firmware is made. LCD transfer, continuous imagery
  and a complete visual oracle remain unverified.

## Provenance limits

Dispatch and completion capture the exact owned execution request, job/plan hash,
broker epoch, prepared device fingerprint, configured expected firmware hash and
serial-configuration digest. Changed evidence retains the original scope and is
marked changed. A historical read does not attach the current LKG to an older
observation.

Source/build/flash receipt association is currently `missing` or `ambiguous`:
the loop-owned database does not contain all manifest and source-snapshot proofs
needed to validate that chain without filesystem reads. No first candidate is
chosen, and equal app hashes do not establish installed identity or a receipt
chain. This report does not authorize any later device action.

Primary contract sources at the pinned firmware commit are
`main/pocket/derby_telemetry.c`, `main/ui/jsconsole.c`,
`tools/games/derby_observation.py` and `docs/platform/derby-telemetry.json`.
