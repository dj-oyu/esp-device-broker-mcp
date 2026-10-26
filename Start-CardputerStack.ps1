#requires -Version 5.1
<#
Start the broker, the Claude gateway and the local queue-manager window, in that order:
  & 'C:\path\to\Start-CardputerStack.ps1' -Exe 'C:\path\to\cardputer.exe' -Profile 'C:\path\to\device.local.toml' [-Url <broker url>] [-NoSerial]
Same arguments as Start-CardputerGui.ps1. Run it with & from your usual PowerShell prompt
(not by double-click or -File): the broker's run tokens stay in this prompt, as with that script.

1. Start-Cardputer.ps1 starts the authenticated broker in its own console and waits until it is idle.
2. A second console runs `cardputer.exe gateway --config <profile>`. Funnel opens there only when the
   profile's [gateway] sets funnel = true; Ctrl+C in that console closes it. That console gets no
   CARDPUTER_* or OPENAI_* variables: the gateway reads its own key files named in the profile.
   Without [gateway] it prints gateway_config_missing and exits; the broker and GUI are unaffected.
3. The GUI runs in this prompt (the same command as Start-CardputerGui.ps1).

Closing the GUI does not stop the broker or the gateway. Nothing here stops an existing process.
See CLAUDE-GATEWAY.md for the gateway's one-time setup.
#>
[CmdletBinding()]
param(
    [string]$Exe = '',
    [string]$Profile = '',
    [string]$Url = 'http://127.0.0.1:8765/mcp',
    [switch]$NoSerial,
    [string]$KeyRegistry = ''
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This launcher requires Windows PowerShell 5.1 or PowerShell 7 on Windows.'
}
if ([string]::IsNullOrWhiteSpace($Exe) -or [string]::IsNullOrWhiteSpace($Profile)) {
    throw 'Both -Exe (path to cardputer.exe) and -Profile (broker profile TOML) are required.'
}
if (!(Test-Path -LiteralPath $Exe -PathType Leaf)) { throw 'The -Exe file was not found.' }
if (!(Test-Path -LiteralPath $Profile -PathType Leaf)) { throw 'The -Profile file was not found.' }
$exePath = (Resolve-Path -LiteralPath $Exe).ProviderPath
$profilePath = (Resolve-Path -LiteralPath $Profile).ProviderPath
$profileDirectory = [IO.Path]::GetDirectoryName($profilePath)

# 1. Broker (refuses if one is already listening; nothing is stopped or changed then).
$arguments = @{ Exe = $exePath; Profile = $profilePath; Url = $Url }
if ($NoSerial) { $arguments.NoSerial = $true }
if ($KeyRegistry) { $arguments.KeyRegistry = $KeyRegistry }
& (Join-Path $PSScriptRoot 'Start-Cardputer.ps1') @arguments

# 2. Gateway in its own console. The command holds paths only; no token or key is passed.
function Quote-PowerShellLiteral([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }
$gatewayCommand = '$ErrorActionPreference = ''Stop''; ' +
    'Get-ChildItem Env: | Where-Object { $_.Name -like ''CARDPUTER_*'' -or $_.Name -like ''OPENAI_*'' } | ' +
    'ForEach-Object { Remove-Item -LiteralPath (''Env:'' + $_.Name) }; ' +
    '$Host.UI.RawUI.WindowTitle = ''Cardputer gateway''; ' +
    'Set-Location -LiteralPath ' + (Quote-PowerShellLiteral $profileDirectory) + '; & ' +
    (Quote-PowerShellLiteral $exePath) + ' gateway --config ' + (Quote-PowerShellLiteral $profilePath) + '; ' +
    'Write-Host (''Gateway exited (code {0}). Review messages above.'' -f $LASTEXITCODE)'
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($gatewayCommand))
$shellName = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' }
[void](Start-Process -FilePath (Join-Path $PSHOME $shellName) -WorkingDirectory $profileDirectory -ArgumentList (
    '-NoLogo -NoProfile -NoExit -EncodedCommand ' + $encoded))
Write-Host 'Gateway console opened. With funnel = true, wait for gateway_funnel_open there; after a gateway restart, reconnect the connector in Claude.'

# 3. GUI in this prompt.
Write-Host 'Opening local manager. Tunnel connects once at startup when its API key and CARDPUTER_REMOTE_TOKEN are set; otherwise click Connect.'
Write-Host 'Closing asks to stop this GUI-owned Tunnel only; broker, gateway and device jobs continue.'
Push-Location -LiteralPath $profileDirectory
try { & $exePath --auth-source environment gui --config $profilePath } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) {
    throw 'Queue manager could not start; the broker and gateway remain running.'
}
