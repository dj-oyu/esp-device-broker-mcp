#requires -Version 5.1
<#
Run from your existing PowerShell prompt, not a child powershell -File process.
Default: authenticated local MOCK preparation only. -Connect explicitly starts
outbound OpenAI Tunnel polling with an already-provisioned key entered privately.
No download, installation, account creation, USB job, or firewall change is done.
#>
[CmdletBinding()]
param(
    [switch]$Connect,
    [switch]$PromptApiKey,
    [string]$Config = '',
    [switch]$EnableSerial,
    [string]$KeyRegistry = '',
    [switch]$UseClientKey,
    [switch]$Gui
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This launcher requires Windows PowerShell 5.1 or PowerShell 7 on Windows.'
}
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).ProviderPath
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'First run uv sync --frozen in this project. No client was started.'
}
if ($Gui) {
    # GUI supervises its own client; never start an unmonitored console client beside it.
    $guiArguments = @{}
    if ($Config) { $guiArguments.Config = $Config }
    if ($EnableSerial) { $guiArguments.EnableSerial = $true }
    if ($KeyRegistry) { $guiArguments.KeyRegistry = $KeyRegistry }
    Write-Host 'GUI mode: configure [tunnel] in the broker TOML and click Connect in its Tunnel panel.'
    Write-Host 'No automatic cloud connection or key prompt is performed by this launcher.'
    & (Join-Path $projectRoot 'Start-CardputerGui.ps1') @guiArguments
    return
}
if (!$Config) {
    if (Test-Path -LiteralPath (Join-Path $projectRoot 'device.mock.local.toml')) {
        $Config = 'device.mock.local.toml'
    } elseif (Test-Path -LiteralPath (Join-Path $projectRoot 'tunnel.local.toml')) {
        $Config = 'tunnel.local.toml' # Legacy explicit console mode remains supported.
    } else { $Config = 'device.mock.local.toml' }
}
if ($EnableSerial -and !$Connect) { throw '-EnableSerial requires the explicit -Connect path.' }
if ($UseClientKey -and (!$Connect -or [string]::IsNullOrWhiteSpace($KeyRegistry))) {
    throw '-UseClientKey requires -Connect and an existing -KeyRegistry path.'
}
if ($KeyRegistry) {
    if (![IO.Path]::IsPathRooted($KeyRegistry)) { $KeyRegistry = Join-Path $projectRoot $KeyRegistry }
    $KeyRegistry = (Resolve-Path -LiteralPath $KeyRegistry).ProviderPath
}
$brokerConfig = Join-Path $projectRoot 'examples\tunnel-mock.toml'
$url = 'http://127.0.0.1:8765/mcp'
$backend = 'mock'
if ($Connect) {
    if (![IO.Path]::IsPathRooted($Config)) { $Config = Join-Path $projectRoot $Config }
    $configPath = (Resolve-Path -LiteralPath $Config).ProviderPath
    $preflightArgs = @('-m', 'cardputer_mcp.tunnel', 'preflight', '--project', $projectRoot,
        '--config', $configPath)
    if ($EnableSerial) { $preflightArgs += '--enable-serial' }
    $resultText = & $pythonPath @preflightArgs
    $preflightExit = $LASTEXITCODE
    if ($preflightExit -ne 0) { throw 'Tunnel preflight refused. Check private metadata, official ZIP/exe, or an existing listener/client. Nothing was stopped.' }
    $check = ConvertFrom-Json -InputObject ($resultText -join "`n")
    if (!$check.ok) { throw 'Tunnel preflight refused.' }
    $brokerConfig = $check.broker_config
    $url = $check.url
    $backend = $check.backend
}
# The shared launcher checks existing brokers BEFORE generating new process tokens.
$brokerArgs = @{ Config = $brokerConfig }
if ($backend -eq 'mock') { $brokerArgs.Mock = $true }
if ($KeyRegistry) { $brokerArgs.KeyRegistry = $KeyRegistry }
& (Join-Path $projectRoot 'Start-Cardputer.ps1') @brokerArgs
if (!$Connect) {
    & $pythonPath -m cardputer_mcp.tunnel check --project $projectRoot --config $brokerConfig --url $url
    if ($LASTEXITCODE -ne 0) { throw 'Authenticated remote-identity discovery failed. Inspect broker console.' }
    Write-Host 'Local preparation passed. No cloud client, API key, or USB operation was used.'
    Write-Host 'For account-backed setup later, read TUNNEL-WINDOWS.md before using -Connect.'
    if ($Gui) { & $pythonPath -m cardputer_mcp.cli --auth-source environment gui --config $brokerConfig }
    return
}

function Quote-TunnelArgument([string]$Value) {
    if ($Value.Contains('"') -or $Value.EndsWith('\')) { throw 'Invalid launcher file argument.' }
    return '"' + $Value + '"'
}
$shellName = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' }
$shellPath = Join-Path $PSHOME $shellName
$info = New-Object Diagnostics.ProcessStartInfo
$info.FileName = $pythonPath
$info.Arguments = '-m cardputer_mcp.tunnel launch-console --project ' +
    (Quote-TunnelArgument $projectRoot) + ' --config ' + (Quote-TunnelArgument $configPath) +
    ' --shell ' + (Quote-TunnelArgument $shellPath)
if ($EnableSerial) { $info.Arguments += ' --enable-serial' }
$info.WorkingDirectory = $projectRoot
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
    Write-Host 'Verify actual app discovery and a read-only device_status call before any device work.'
} finally {
    $runtimeApi = $null
    if ($apiPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer) }
    if ($mcpPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($mcpPointer) }
    $info.EnvironmentVariables.Clear()
    if ($secureApi) { $secureApi.Dispose() }
    if ($secureMcp) { $secureMcp.Dispose() }
    if ($process) { $process.Dispose() }
}

if ($Gui) { & $pythonPath -m cardputer_mcp.cli --auth-source environment gui --config $brokerConfig }
