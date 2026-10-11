# Serial failure diagnostics update for v0.2

This patch corrects misleading failure classification. It does not claim to fix
an unverified hardware/USB fault and does not change any serial control signal,
command sequence, timeout, approval requirement or recovery gate.

Previously every serial validation/timeout failure was collapsed into
`device_disconnected_no_retry`. That result did not prove a physical disconnect.
New failures preserve a fixed safe category such as `serial_marker_timeout`,
`serial_read_failed`, `serial_write_failed` or `serial_open_failed`.

`job` now includes a `diagnostic` object with:

- `code`: allowlisted error category
- `step`: fixed logical operation, e.g. `initial_home`
- `expected_marker`: a safe marker name, e.g. `HOME_READY`
- `received_bytes`, `received_lines`, `recognized_markers`: bounded counts

Counts cover the current operation, including pre-command draining. They are not
raw log content and do not prove a fresh command acknowledgement. No raw firmware
line, OS error, credential, path or serial identity is copied into these fields.
A new `serial_wait_<step>` event means the host's one-byte write returned the
expected count; it does not prove that the device processed that command.

The existing initial q-to-HOME_READY contract is unchanged. Pinned firmware source
22f6d54 maps q to Back and emits HOME_READY when Back is handled on ordinary HOME.
Standard `I (123) shell: HOME_READY` lines, with optional ANSI color and fragmented
reads, are covered by tests. A HOME display alone does not verify the USB path.

## Apply the small source update

1. Confirm the broker has no active job and close its server console normally
2. Extract the diagnostic update ZIP into the existing MCP project folder, the
   folder containing `pyproject.toml`, replacing its four matching source files
3. Run `Start-Cardputer.ps1` again from the same project setup

The update ZIP has no extra enclosing project directory. It contains only
`src/cardputer_mcp/backend.py`, `broker.py`, `serial_backend.py`, `store.py`, and this
guide. It does not contain private config, credentials, SQLite data or a launcher.
There are no new dependencies. The normal editable uv project uses the replaced
source after restart; no firmware build, flash or device operation is part of update.

SQLite automatically adds a nullable diagnostic field. Existing jobs and review
requirements are retained. An old failure's `diagnostic` is null: information that
was not saved cannot be reconstructed. Do not clear review or retry just to fill
that field. Any later physical operation still requires an explicit reviewed plan
and fresh approval. This patch performs no automatic retry, reset or unquarantine.
