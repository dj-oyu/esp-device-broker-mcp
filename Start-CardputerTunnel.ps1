#requires -Version 5.1
<#
Start the broker AND its outbound OpenAI Tunnel from a packaged cardputer.exe and ONE broker
profile TOML. Run from your existing PowerShell prompt, not a child powershell -File:
  & 'C:\path\to\Start-CardputerTunnel.ps1' -Exe 'C:\path\to\cardputer.exe' -Profile 'C:\path\to\device.local.toml' [-Url <broker url>] [-NoTunnel] [-NoSerial]
This script never reads the profile; a profile whose broker is not on the default
http://127.0.0.1:8765/mcp needs a matching -Url (see Start-Cardputer.ps1). Nothing from [tunnel] is passed as an argument:
tunnel_id, client_exe, client_archive, health_port and ingress_enabled are read and validated
by the exe's Tunnel module, which refuses with its own reason (for example
tunnel_id_placeholder or tunnel_metadata_incomplete). -NoTunnel starts the broker alone.
--enable-serial is passed by default; -NoSerial omits it (a serial profile is then refused).
The runtime API key comes from CARDPUTER_TUNNEL_API_KEY (or CONTROL_PLANE_API_KEY) in this
process, or a private prompt; it is never printed or saved. No download, installation,
account creation, USB job, or firewall change is done.
#>
[CmdletBinding()]
param(
    [string]$Exe = '',
    [string]$Profile = '',
    [string]$Url = 'http://127.0.0.1:8765/mcp',
    [switch]$NoTunnel,
    [switch]$NoSerial,
    [switch]$PromptApiKey,
    [string]$KeyRegistry = '',
    [switch]$UseClientKey
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This launcher requires Windows PowerShell 5.1 or PowerShell 7 on Windows.'
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
# The Tunnel module resolves relative profile paths against this base folder.
$profileDirectory = [IO.Path]::GetDirectoryName($profilePath)
if ($UseClientKey -and [string]::IsNullOrWhiteSpace($KeyRegistry)) {
    throw '-UseClientKey requires an existing -KeyRegistry path.'
}
if ($KeyRegistry) {
    if (!(Test-Path -LiteralPath $KeyRegistry -PathType Leaf)) { throw 'The -KeyRegistry file was not found.' }
    $KeyRegistry = (Resolve-Path -LiteralPath $KeyRegistry).ProviderPath
}

function Quote-TunnelArgument([string]$Value) {
    if ($Value.Contains('"') -or $Value.EndsWith('\')) { throw 'Invalid launcher file argument.' }
    return '"' + $Value + '"'
}
# One bounded, read-only helper call that returns the exe's JSON line; secrets are never involved.
function Invoke-ExeJson([string]$Arguments, [string]$Failure) {
    $helper = New-Object Diagnostics.ProcessStartInfo
    $helper.FileName = $exePath
    $helper.Arguments = $Arguments
    $helper.WorkingDirectory = $profileDirectory
    $helper.UseShellExecute = $false
    $helper.CreateNoWindow = $true
    $helper.RedirectStandardOutput = $true
    $helper.RedirectStandardError = $true
    $run = New-Object Diagnostics.Process
    $run.StartInfo = $helper
    try {
        [void]$run.Start()
        $out = $run.StandardOutput.ReadToEndAsync()
        $err = $run.StandardError.ReadToEndAsync()
        if (!$run.WaitForExit(15000)) { throw "$Failure (timed out; nothing was stopped)." }
        $text = $out.GetAwaiter().GetResult()
        [void]$err.GetAwaiter().GetResult()
        try { $parsed = ConvertFrom-Json -InputObject $text -ErrorAction Stop; if ($null -eq $parsed) { throw "empty" }; return $parsed }
        catch { throw "$Failure (no result; check -Exe and -Profile)." }
    } finally { $run.Dispose() }
}

$serialArgument = if (!$NoSerial) { ' --enable-serial' } else { '' }
$tunnelSuffix = ' --project ' + (Quote-TunnelArgument $profileDirectory) +
    ' --config ' + (Quote-TunnelArgument $profilePath)
if (!$NoTunnel) {
    $tunnelCheck = Invoke-ExeJson ('-m cardputer_mcp.tunnel preflight' + $tunnelSuffix + $serialArgument) 'Tunnel preflight failed'
    if ($tunnelCheck.url -and $tunnelCheck.url -ne $Url) {
        throw "The profile's broker URL ($($tunnelCheck.url)) differs from -Url. Rerun with -Url '$($tunnelCheck.url)'. Nothing was started or stopped."
    }
    if (!$tunnelCheck.ok) { throw "Tunnel preflight refused ($($tunnelCheck.error)). Fix the profile's [tunnel] section, the official ZIP/exe, or an existing listener/client; or rerun with -NoTunnel for the broker alone (-NoSerial if --enable-serial was refused). Nothing was started or stopped." }
}
# The shared launcher checks for an existing listener BEFORE generating new process tokens.
$brokerArgs = @{ Exe = $exePath; Profile = $profilePath; Url = $Url }
if ($NoSerial) { $brokerArgs.NoSerial = $true }
if ($KeyRegistry) { $brokerArgs.KeyRegistry = $KeyRegistry }
& (Join-Path $PSScriptRoot 'Start-Cardputer.ps1') @brokerArgs
if ($NoTunnel) {
    Write-Host 'Tunnel skipped (-NoTunnel). The broker is running.'
    return
}

$shellName = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' }
$shellPath = Join-Path $PSHOME $shellName
$info = New-Object Diagnostics.ProcessStartInfo
$info.FileName = $exePath
$info.Arguments = '-m cardputer_mcp.tunnel launch-console' + $tunnelSuffix +
    ' --shell ' + (Quote-TunnelArgument $shellPath) + $serialArgument
$info.WorkingDirectory = $profileDirectory
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
# Never pass local-operator credentials, arbitrary profiles, proxy settings, or commands.
$info.EnvironmentVariables.Clear()
foreach ($name in @('SystemRoot','WINDIR','SystemDrive','TEMP','TMP','COMSPEC')) {
    $value = [Environment]::GetEnvironmentVariable($name, 'Process')
    if ($value) { $info.EnvironmentVariables[$name] = $value }
}
$secureApi = $null
$secureMcp = $null
$apiPointer = [IntPtr]::Zero
$mcpPointer = [IntPtr]::Zero
$process = $null
$runtimeApi = $null
try {
    Write-Host 'Use a process runtime API key or enter it privately. It is never printed or saved.'
    Write-Host 'This starts outbound polling for your configured tunnel. Keep the new console open.'
    if (!$PromptApiKey) {
        $runtimeApi = [Environment]::GetEnvironmentVariable('CARDPUTER_TUNNEL_API_KEY', 'Process')
        if (!$runtimeApi) {
            $runtimeApi = [Environment]::GetEnvironmentVariable('CONTROL_PLANE_API_KEY', 'Process')
        }
    }
    if (!$runtimeApi) {
        $secureApi = Read-Host 'Tunnel runtime API key' -AsSecureString
        $apiPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureApi)
        $runtimeApi = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiPointer)
    }
    $info.EnvironmentVariables['CARDPUTER_TUNNEL_API_KEY'] = $runtimeApi
    $runtimeApi = $null
    if ($UseClientKey) {
        $secureMcp = Read-Host 'Previously issued Cardputer client key' -AsSecureString
        $mcpPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureMcp)
        $info.EnvironmentVariables['CARDPUTER_TUNNEL_BEARER'] = 'Bearer ' +
            [Runtime.InteropServices.Marshal]::PtrToStringBSTR($mcpPointer)
    } else {
        $info.EnvironmentVariables['CARDPUTER_TUNNEL_BEARER'] = 'Bearer ' +
            [Environment]::GetEnvironmentVariable('CARDPUTER_REMOTE_TOKEN', 'Process')
    }
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    $bootstrapOut = $process.StandardOutput.ReadToEndAsync()
    $bootstrapErr = $process.StandardError.ReadToEndAsync()
    if (!$process.WaitForExit(10000)) {
        throw 'Console startup is uncertain. Inspect windows before retrying; no process was stopped.'
    }
    [void]$bootstrapOut.GetAwaiter().GetResult()
    [void]$bootstrapErr.GetAwaiter().GetResult()
    if ($process.ExitCode -ne 0) { throw 'Tunnel console could not be started. Broker remains running.' }
    Write-Host 'Tunnel console opened. Its local readiness is not proof of cloud connectivity.'
    Write-Host 'Verify actual app discovery and a read-only status(view=device) call before any device work.'
} finally {
    $runtimeApi = $null
    if ($apiPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer) }
    if ($mcpPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($mcpPointer) }
    $info.EnvironmentVariables.Clear()
    if ($secureApi) { $secureApi.Dispose() }
    if ($secureMcp) { $secureMcp.Dispose() }
    if ($process) { $process.Dispose() }
}

