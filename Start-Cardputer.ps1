#requires -Version 5.1
<#
Start ONE authenticated broker from a packaged cardputer.exe and a broker profile TOML.
  & 'C:\path\to\Start-Cardputer.ps1' -Exe 'C:\path\to\cardputer.exe' -Profile 'C:\path\to\device.local.toml'
Run it from your existing PowerShell prompt and keep that prompt for clients. Do not
double-click or invoke in another powershell -File process: a child cannot update its
parent's environment. No USB operation is started. Every hardware job still needs local
plan approval.
This script never reads the profile; the exe loads and validates it and refuses with its own
reason (shown in the broker console). Because the script cannot know the profile's host/port,
the pre-start listener check and the readiness check use -Url (default: the exe's own client
default http://127.0.0.1:8765/mcp). A profile with another host/port needs a matching -Url.
--enable-serial is passed by default (a mock profile accepts it); -NoSerial leaves it out and
a serial profile is then refused with serial_runtime_opt_in_required. Relative paths inside the
profile resolve against the profile's own folder. The exe's own lock still stops a second
broker for the same device; this script does not probe lock files.
#>
[CmdletBinding()]
param(
    [string]$Exe = '',
    [string]$Profile = '',
    [string]$Url = 'http://127.0.0.1:8765/mcp',
    [switch]$NoSerial,
    [ValidateRange(10,60)][int]$ReadyTimeoutSeconds = 20,
    [string]$KeyRegistry = ''
)

$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This launcher is for Windows PowerShell 5.1 or PowerShell 7 on Windows.'
}
if ([string]::IsNullOrWhiteSpace($Exe) -or [string]::IsNullOrWhiteSpace($Profile)) {
    throw 'Both -Exe (path to cardputer.exe) and -Profile (broker profile TOML) are required.'
}
if (!(Test-Path -LiteralPath $Exe -PathType Leaf)) { throw 'The -Exe file was not found.' }
$exePath = (Resolve-Path -LiteralPath $Exe).ProviderPath
if ([IO.Path]::GetExtension($exePath).ToLowerInvariant() -ne '.exe') {
    throw '-Exe must be the packaged cardputer.exe.'
}
if (!(Test-Path -LiteralPath $Profile -PathType Leaf)) { throw 'The -Profile file was not found.' }
$profilePath = (Resolve-Path -LiteralPath $Profile).ProviderPath
if ([IO.Path]::GetExtension($profilePath).ToLowerInvariant() -ne '.toml') {
    throw '-Profile must be a broker profile .toml file.'
}
$brokerUri = $null
if (![Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$brokerUri) -or $brokerUri.Scheme -ne 'http' -or
    @('127.0.0.1', 'localhost') -notcontains $brokerUri.Host -or $brokerUri.Port -lt 1) {
    throw '-Url must be a loopback http URL such as http://127.0.0.1:8765/mcp.'
}
# The broker and the readiness check run from the profile folder, so a relative state_dir resolves there.
$profileDirectory = [IO.Path]::GetDirectoryName($profilePath)
if ($KeyRegistry) {
    if (!(Test-Path -LiteralPath $KeyRegistry -PathType Leaf)) { throw 'The -KeyRegistry file was not found.' }
    $KeyRegistry = (Resolve-Path -LiteralPath $KeyRegistry).ProviderPath
}
$shellName = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' }
$shellPath = Join-Path $PSHOME $shellName

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
function Test-Listener {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $connect = $client.BeginConnect('127.0.0.1', $brokerUri.Port, $null, $null)
        return ($connect.AsyncWaitHandle.WaitOne(300) -and $client.Connected)
    } catch { return $false } finally { $client.Close() }
}
# One bounded authenticated `status` (the existing status(view=device) call): no lease, USB or device command.
function Get-BrokerStatus {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $exePath
    $info.Arguments = '--url ' + (Quote-FileArgument $Url) + ' --auth-source environment status'
    $info.WorkingDirectory = $profileDirectory
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
        if (!$probe.WaitForExit(5000)) {
            # Only our own read-only status call is stopped; never the broker or another process.
            $probe.Kill()
            [void]$probe.WaitForExit(2000)
            return $null
        }
        $text = $stdout.GetAwaiter().GetResult()
        [void]$stderr.GetAwaiter().GetResult()
        try { return ConvertFrom-Json -InputObject $text -ErrorAction Stop } catch { return $null }
    } finally { $probe.Dispose() }
}

$tokensChanged = $false
$ready = $false
$serverOpened = $false
$previousLocal = [Environment]::GetEnvironmentVariable('CARDPUTER_LOCAL_TOKEN', 'Process')
$previousRemote = [Environment]::GetEnvironmentVariable('CARDPUTER_REMOTE_TOKEN', 'Process')
try {
    if (Test-Listener) {
        throw "Startup refused (listener_exists on $($brokerUri.Host):$($brokerUri.Port)). If your own broker is running, check that no job is active, then stop it in its console; if the profile uses another port, pass -Url. No existing broker or credentials were changed."
    }
    # Both fresh values are made together, only after the existing-listener check.
    $newLocal = New-RunToken
    do { $newRemote = New-RunToken } while ($newLocal -eq $newRemote)
    $tokensChanged = $true
    [Environment]::SetEnvironmentVariable('CARDPUTER_LOCAL_TOKEN', $newLocal, 'Process')
    [Environment]::SetEnvironmentVariable('CARDPUTER_REMOTE_TOKEN', $newRemote, 'Process')
    $newLocal = $null
    $newRemote = $null

    # EncodedCommand contains paths/commands only. Tokens travel only via inherited environment.
    # --enable-serial only permits the backend; USB opens only on approved device work.
    $serverCommand = '$ErrorActionPreference = ''Stop''; Set-Location -LiteralPath ' +
        (Quote-PowerShellLiteral $profileDirectory) + '; & ' + (Quote-PowerShellLiteral $exePath) +
        ' --auth-source environment serve --config ' + (Quote-PowerShellLiteral $profilePath) +
        $(if ($KeyRegistry) { ' --key-registry ' + (Quote-PowerShellLiteral $KeyRegistry) } else { '' }) +
        ' --require-auth' + $(if (!$NoSerial) { ' --enable-serial' } else { '' }) + '; ' +
        '$brokerExit = $LASTEXITCODE; Write-Host (''Broker exited (code {0}). Review messages above.'' -f $brokerExit)'
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($serverCommand))
    [void](Start-Process -FilePath $shellPath -WorkingDirectory $profileDirectory -PassThru -ArgumentList (
        '-NoLogo -NoProfile -NoExit -EncodedCommand ' + $encoded))
    $serverOpened = $true
    $deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
    $status = $null
    $lastReason = 'readiness_timeout'
    while ([DateTime]::UtcNow -lt $deadline) {
        $status = Get-BrokerStatus
        if ($null -ne $status -and $status.ok -eq $true -and $status.backend) {
            if ($status.active_job -or $status.owner -or $status.hardware_connected) {
                $lastReason = 'broker_not_idle'
            } else { $ready = $true }
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (!$ready) {
        throw "Authenticated startup not confirmed ($lastReason). Read the server console for the exe's own refusal (for example serial_runtime_opt_in_required, or a profile error); if --enable-serial was the cause rerun with -NoSerial; if the profile uses another port, pass -Url."
    }
    Write-Host "Ready: authenticated $($status.backend) broker is idle. USB is closed; no device command was sent."
    Write-Host "Profile: $profilePath"
    Write-Host 'Use THIS original PowerShell window for client commands:'
    Write-Host ('& ' + (Quote-PowerShellLiteral $exePath) + ' --url ' + (Quote-PowerShellLiteral $Url) +
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
}
