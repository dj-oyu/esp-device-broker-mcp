#requires -Version 5.1
<#
Run from your existing PowerShell prompt: & "path\to\MCP\Start-Cardputer.ps1"
Keep this original prompt for clients. Do not double-click or invoke in another
powershell -File process: a child cannot update its parent's environment.
No USB operation is started. Every hardware job still needs local plan approval.
#>
[CmdletBinding()]
param([string]$Config = '', [ValidateRange(10,60)][int]$ReadyTimeoutSeconds = 20, [switch]$Mock, [string]$KeyRegistry = '')

$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This launcher is for Windows PowerShell 5.1 or PowerShell 7 on Windows.'
}
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).ProviderPath
if (!(Test-Path -LiteralPath (Join-Path $projectRoot 'pyproject.toml') -PathType Leaf)) {
    throw 'Put Start-Cardputer.ps1 inside your cardputer-device-mcp folder, beside pyproject.toml.'
}
if ([string]::IsNullOrWhiteSpace($Config)) {
    $Config = if ($Mock) { 'examples/tunnel-mock.toml' } else { 'device.local.toml' }
}
if (![IO.Path]::IsPathRooted($Config)) { $Config = Join-Path $projectRoot $Config }
$configPath = (Resolve-Path -LiteralPath $Config).ProviderPath
if ($KeyRegistry) {
    if (![IO.Path]::IsPathRooted($KeyRegistry)) { $KeyRegistry = Join-Path $projectRoot $KeyRegistry }
    $KeyRegistry = (Resolve-Path -LiteralPath $KeyRegistry).ProviderPath
}
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Project environment is missing. First run: uv sync --project '$($projectRoot.Replace("'", "''"))' --frozen"
}
$uvMatches = @(Get-Command -Name uv -CommandType Application -All -ErrorAction Stop)
$uvPath = $uvMatches[0].Path
$shellName = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' }
$shellPath = Join-Path $PSHOME $shellName

# This temporary file contains generic probe CODE ONLY, never credentials.
$probeSource = @'
import asyncio
import json
import socket
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from filelock import FileLock, Timeout
from cardputer_mcp.broker import device_lock_path
from cardputer_mcp.config import load_settings
from cardputer_mcp.auth import load_environment_token
from cardputer_mcp.client import call, connect
from cardputer_mcp.models import Origin


def preflight(settings, mock=False):
    expected = "mock" if mock else "serial"
    if settings.backend != expected:
        return {"ok": False, "reason": "serial_config_required"}
    with socket.socket() as sock:
        sock.settimeout(0.3)
        if sock.connect_ex((settings.host, settings.port)) == 0:
            return {"ok": False, "reason": "listener_exists"}
    paths = [device_lock_path(settings.backend)]
    if settings.state_dir.exists():
        paths.append(settings.state_dir / "broker.lockfile")
    try:
        with ExitStack() as stack:
            for path in paths:
                stack.enter_context(FileLock(str(path), timeout=0))
    except Timeout:
        return {"ok": False, "reason": "broker_locked"}
    return {"ok": True, "url": f"http://{settings.host}:{settings.port}/mcp"}


async def readiness(settings, seconds):
    url = f"http://{settings.host}:{settings.port}/mcp"
    token = load_environment_token(Origin.LOCAL)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            async with asyncio.timeout(1.5):
                async with connect(url, token) as client:
                    status = await call(client, "device_status")
            if not status.get("ok") or status.get("backend") != settings.backend:
                return {"ok": False, "reason": "unexpected_broker"}
            if status.get("active_job") or status.get("owner") or status.get("hardware_connected"):
                return {"ok": False, "reason": "broker_not_idle"}
            return {"ok": True, "needs_review": bool(status.get("needs_review"))}
        except Exception:
            await asyncio.sleep(0.2)
    return {"ok": False, "reason": "readiness_timeout"}


def main():
    try:
        settings = load_settings(Path(sys.argv[2]))
        result = preflight(settings, len(sys.argv) > 4 and sys.argv[4] == "mock") if sys.argv[1] == "preflight" else asyncio.run(
            readiness(settings, float(sys.argv[3])))
    except Exception:
        result = {"ok": False, "reason": "probe_failed_check_config_and_uv_sync"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
'@

function Quote-PowerShellLiteral([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }
function Quote-FileArgument([string]$Value) {
    if ($Value.Contains('"') -or $Value.EndsWith('\')) { throw 'Invalid file argument.' }
    return '"' + $Value + '"'
}
function New-RunToken {
    [byte[]]$bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}
function Invoke-Probe([string]$Mode, [int]$TimeoutMilliseconds) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $pythonPath
    $info.Arguments = (Quote-FileArgument $probeFile) + ' ' + $Mode + ' ' +
        (Quote-FileArgument $configPath) + ' ' + ($ReadyTimeoutSeconds - 3) + ' ' + $(if ($Mock) { 'mock' } else { 'serial' })
    $info.WorkingDirectory = $projectRoot
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $probe = New-Object Diagnostics.Process
    $probe.StartInfo = $info
    try {
        [void]$probe.Start()
        $stdout = $probe.StandardOutput.ReadToEndAsync()
        $stderr = $probe.StandardError.ReadToEndAsync()
        if (!$probe.WaitForExit($TimeoutMilliseconds)) {
            # Only our own read-only probe is stopped; never the broker or another process.
            $probe.Kill()
            [void]$probe.WaitForExit(2000)
            throw 'Read-only startup probe timed out. Inspect the server console; no broker was stopped.'
        }
        $text = $stdout.GetAwaiter().GetResult()
        [void]$stderr.GetAwaiter().GetResult()
        if ($probe.ExitCode -ne 0) { throw 'Startup probe failed. Check config and run uv sync --frozen.' }
        return ConvertFrom-Json -InputObject $text -ErrorAction Stop
    } finally { $probe.Dispose() }
}

$probeFile = [IO.Path]::GetTempFileName()
$tokensChanged = $false
$ready = $false
$serverOpened = $false
$previousLocal = [Environment]::GetEnvironmentVariable('CARDPUTER_LOCAL_TOKEN', 'Process')
$previousRemote = [Environment]::GetEnvironmentVariable('CARDPUTER_REMOTE_TOKEN', 'Process')
try {
    [IO.File]::WriteAllText($probeFile, $probeSource, (New-Object Text.UTF8Encoding($false)))
    $check = Invoke-Probe 'preflight' 10000
    if (!$check.ok) {
        throw "Startup refused ($($check.reason)). If your own broker is running, check that no job is active, then stop it in its console. No existing broker or credentials were changed."
    }
    # Both fresh values are made together, only after the existing-broker checks.
    $newLocal = New-RunToken
    do { $newRemote = New-RunToken } while ($newLocal -eq $newRemote)
    $tokensChanged = $true
    [Environment]::SetEnvironmentVariable('CARDPUTER_LOCAL_TOKEN', $newLocal, 'Process')
    [Environment]::SetEnvironmentVariable('CARDPUTER_REMOTE_TOKEN', $newRemote, 'Process')
    $newLocal = $null
    $newRemote = $null

    # EncodedCommand contains paths/commands only. Tokens travel only via inherited environment.
    $serverCommand = '$ErrorActionPreference = ''Stop''; Set-Location -LiteralPath ' +
        (Quote-PowerShellLiteral $projectRoot) + '; & ' + (Quote-PowerShellLiteral $uvPath) +
        ' run --project ' + (Quote-PowerShellLiteral $projectRoot) +
        ' --no-sync cardputer --auth-source environment serve --config ' +
        (Quote-PowerShellLiteral $configPath) + $(if ($KeyRegistry) { ' --key-registry ' + (Quote-PowerShellLiteral $KeyRegistry) } else { '' }) + $(if ($Mock) { ' --require-auth; ' } else { ' --enable-serial; ' }) +
        '$brokerExit = $LASTEXITCODE; Write-Host (''Broker exited (code {0}). Review messages above.'' -f $brokerExit)'
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($serverCommand))
    [void](Start-Process -FilePath $shellPath -WorkingDirectory $projectRoot -PassThru -ArgumentList (
        '-NoLogo -NoProfile -NoExit -EncodedCommand ' + $encoded))
    $serverOpened = $true
    $status = Invoke-Probe 'ready' ($ReadyTimeoutSeconds * 1000)
    if (!$status.ok) { throw "Authenticated startup not confirmed ($($status.reason)). Inspect the server console." }
    $ready = $true
    Set-Location -LiteralPath $projectRoot
    Write-Host $(if ($Mock) { 'Ready: authenticated MOCK broker is idle. No USB backend is enabled.' } else { 'Ready: authenticated serial broker is idle. USB is closed; no device command was sent.' })
    Write-Host "Current project: $projectRoot"
    Write-Host 'Use THIS original PowerShell window for client commands:'
    Write-Host ('uv run --project ' + (Quote-PowerShellLiteral $projectRoot) +
        ' --no-sync cardputer --url ' + (Quote-PowerShellLiteral $check.url) +
        ' --auth-source environment status')
    if ($status.needs_review) { Write-Warning 'Earlier work needs offline review before new device jobs can run.' }
} catch {
    if ($serverOpened) {
        Write-Warning 'The new server console stays open for diagnosis. Do not rerun until you inspect it and stop your own failed/late-starting broker; no broker was killed.'
    }
    throw
} finally {
    if ($tokensChanged -and !$ready) {
        [Environment]::SetEnvironmentVariable('CARDPUTER_LOCAL_TOKEN', $previousLocal, 'Process')
        [Environment]::SetEnvironmentVariable('CARDPUTER_REMOTE_TOKEN', $previousRemote, 'Process')
    }
    Remove-Item -LiteralPath $probeFile -ErrorAction SilentlyContinue
}
