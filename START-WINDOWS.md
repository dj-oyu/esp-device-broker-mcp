# One-command Windows startup

The launchers take exactly two required inputs and nothing else about your setup:

- `-Exe` — the packaged `cardputer.exe` (a version folder you chose; there is no implicit
  "latest version" lookup, so you always know which build runs)
- `-Profile` — your private broker profile `.toml`

No Python, `uv` or source checkout is needed, and the launchers use only commands the packaged
exe already has (`serve`, `status`, `-m cardputer_mcp.tunnel`). They never read the profile:
only the exe loads it, validates it and refuses with its own reason. Relative paths inside the
profile (for example a relative `state_dir`) resolve against the profile's own folder, because
the broker and the readiness check run from there.

Run from your **existing PowerShell prompt** (not a double-click, not `powershell -File`):

```powershell
# Broker + Tunnel (the usual start)
& 'C:\path\to\Start-CardputerTunnel.ps1' -Exe 'C:\path\to\cardputer.exe' -Profile 'C:\path\to\device.local.toml'

# Broker only
& 'C:\path\to\Start-Cardputer.ps1' -Exe 'C:\path\to\cardputer.exe' -Profile 'C:\path\to\device.local.toml'

# Broker + local queue-manager window
& 'C:\path\to\Start-CardputerGui.ps1' -Exe 'C:\path\to\cardputer.exe' -Profile 'C:\path\to\device.local.toml'
```

| Switch | Meaning |
|---|---|
| (default) | `--enable-serial` is passed to the exe, and `Start-CardputerTunnel.ps1` also starts the Tunnel |
| `-Url <broker url>` | loopback broker URL used for the pre-start listener check and the readiness check; default `http://127.0.0.1:8765/mcp` (the exe's own client default) |
| `-NoSerial` | omit `--enable-serial`; a serial profile is then refused with `serial_runtime_opt_in_required` |
| `-NoTunnel` | `Start-CardputerTunnel.ps1` starts the broker alone |
| `-KeyRegistry <file>` | use an existing key registry instead of the default one |
| `-UseClientKey` | (Tunnel script, needs `-KeyRegistry`) prompt privately for a previously issued client key as the Tunnel bearer |
| `-PromptApiKey` | (Tunnel script) always prompt for the Tunnel runtime API key |

`--enable-serial` only permits the serial backend; USB opens only for explicitly approved
device work, and a mock profile accepts and ignores it.

**Non-default host/port:** `serve` takes host and port from the profile, and the launcher cannot
read the profile, so a profile that does not use `127.0.0.1:8765` needs a matching `-Url`
(for example `-Url http://127.0.0.1:8770/mcp`). With a wrong `-Url` the broker still starts, the
launcher's readiness check times out and says so; nothing is stopped. For the Tunnel script the
exe's own Tunnel preflight reports the profile's broker URL, and a mismatch with `-Url` is
refused before anything starts.

## What the exe decides (and what you see when it refuses)

- Backend, host/port, state directory and `[tunnel]` come from the profile and are checked by
  the exe. The launcher prints the exe's own reason code.
- Tunnel preflight can refuse with, for example, `tunnel_metadata_incomplete` (no `[tunnel]`
  section), `tunnel_id_placeholder`, a release/ZIP mismatch, or a busy health port. The
  launcher stops **before** starting anything and tells you to fix the profile or use
  `-NoTunnel` (or `-NoSerial` if `--enable-serial` was the cause).
- The Tunnel API key comes from `CARDPUTER_TUNNEL_API_KEY` (or `CONTROL_PLANE_API_KEY`) in
  your current process; otherwise you are prompted privately. The prompt only appears after
  Tunnel preflight passed, and never with `-NoTunnel`.

## What the broker launcher does

1. Refuses with `listener_exists` if `-Url`'s host:port already accepts a TCP connection,
   before any credential is created. It does not probe lock files: `serve` itself enforces
   its own broker/device lock and refuses a second broker
2. Creates both fresh process-only tokens together, without printing or saving them
3. Opens a separate server console running `cardputer.exe --auth-source environment serve
   --config <profile> --require-auth [--enable-serial]` with the tokens inherited from the
   environment only
4. Polls `cardputer.exe --url <Url> --auth-source environment status` (the existing
   authenticated `status(view=device)` call, read-only) until the broker answers with `ok`, a backend,
   no active job/owner and no open hardware connection. On timeout it points you to the server
   console, where the exe prints its own refusal (for example `serial_runtime_opt_in_required`
   or a profile error), and suggests `-NoSerial` or `-Url`
5. Leaves this original prompt where it was and prints an exact client status command

**Keep using the original PowerShell prompt for clients.** It retains the matching process
environment. Children cannot change their parent's tokens.

Starting the broker and checking status do not open USB. Device jobs still require
authentication, an owned lease, an inspected immutable plan and explicit execute. Broker
startup does not prove a physical device is connected. Continue with the passive-observation
steps in [SERIAL.md](SERIAL.md) only when authorized.

If a broker is already present, the script refuses to change credentials or stop anything.
Check that no job is active, then stop your own broker with Ctrl+C in its console and rerun.
If startup fails or times out, the new server console stays visible and the original prompt's
previous tokens are restored. The launcher never kills a broker; only its own read-only probe
can be terminated at its deadline.

When finished, stop the idle broker (and Tunnel console) with Ctrl+C and clear the original
prompt's process-only credentials:

```powershell
Remove-Item Env:CARDPUTER_LOCAL_TOKEN, Env:CARDPUTER_REMOTE_TOKEN
```

Tested here: static PowerShell contract checks, PowerShell 5.1/7 parse checks, and native
PowerShell 7 runs with the staged packaged exe `v0.12.0-1f67fa3d` against a throwaway mock
profile on a non-default port (start with `-NoTunnel`, duplicate-start refusal, readiness
timeout with token restore, Tunnel preflight refusals for a profile without `[tunnel]` and with a
placeholder id). The real Tunnel client, serial hardware and a real profile were not run.
