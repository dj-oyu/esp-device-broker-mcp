# One-command Windows startup

Put `Start-Cardputer.ps1` directly in the existing **cardputer-device-mcp** project
folder beside `pyproject.toml`. Keep your private `device.local.toml` there too.
The existing v0.2 environment must already be installed (`uv sync --frozen`).
No application-code replacement or new ZIP extraction is needed for this helper.

From your existing PowerShell prompt, run:

```powershell
& 'C:\path to\cardputer-device-mcp\Start-Cardputer.ps1'
```

You can invoke it from any current directory. Replace the example path with your
actual MCP project path. To choose a different config:

```powershell
& 'C:\path to\cardputer-device-mcp\Start-Cardputer.ps1' -Config 'my-device.local.toml'
```

Relative config paths are resolved against the script's project folder.

The launcher:

1. Validates the serial config and refuses an existing listener/broker lock
2. Creates both fresh process-only tokens together, without printing or saving them
3. Opens a separate server console with matching inherited credentials
4. Performs a bounded, authenticated `device_status` readiness check
5. Leaves this original prompt in the correct project folder and prints an exact
   client status command, including your configured URL

**Keep using the original PowerShell prompt for clients.** It retains the matching
process environment. Do not double-click the script, use `powershell -File`, or
switch to an unrelated new terminal: children cannot change their parent's tokens.

Starting the broker and checking status do not open USB. Device jobs still require
authentication, an owned lease, an inspected immutable plan and explicit execute. Continue with
the passive-observation steps in [SERIAL.md](SERIAL.md) only when authorized.

If a broker is already present, the script refuses to change credentials or stop
anything. Check your existing broker's status and ensure no job is active before
you stop your own broker with Ctrl+C in its console. Then rerun the launcher.

If startup fails or times out, the new server console stays visible. The original
prompt's previous tokens are restored. Inspect that console and stop your own
failed/late-starting broker before retrying. The helper never kills a broker; only
its own read-only readiness probe can be terminated at its deadline.

When finished, stop the idle broker with Ctrl+C in the server console and clear
the original prompt's process-only credentials:

```powershell
Remove-Item Env:CARDPUTER_LOCAL_TOKEN, Env:CARDPUTER_REMOTE_TOKEN
```

Tested here: embedded probe logic with synthetic cases and real authenticated HTTP
against a synthetic serial backend, plus static PowerShell review. Native Windows
PowerShell 5.1/7 execution has not been verified in this environment.
