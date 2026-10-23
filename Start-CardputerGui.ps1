#requires -Version 5.1
<#
Start one broker (via Start-Cardputer.ps1) and its local queue-manager window from a packaged
cardputer.exe and a broker profile TOML:
  & 'C:\path\to\Start-CardputerGui.ps1' -Exe 'C:\path\to\cardputer.exe' -Profile 'C:\path\to\device.local.toml' [-Url <broker url>] [-NoSerial]
The profile is read only by the exe; a non-default broker port needs a matching -Url. --enable-serial is passed by default; -NoSerial omits it.
Startup never opens USB and cannot approve/execute a hardware plan.
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
$arguments = @{ Exe = $exePath; Profile = $profilePath; Url = $Url }
if ($NoSerial) { $arguments.NoSerial = $true }
if ($KeyRegistry) { $arguments.KeyRegistry = $KeyRegistry }
& (Join-Path $PSScriptRoot 'Start-Cardputer.ps1') @arguments
Write-Host 'Opening local manager. Tunnel stays disconnected until you click Connect.'
Write-Host 'Closing asks to stop this GUI-owned Tunnel only; broker and device jobs continue.'
Push-Location -LiteralPath ([IO.Path]::GetDirectoryName($profilePath))
try { & $exePath --auth-source environment gui --config $profilePath } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) {
    throw 'Queue manager could not start; the broker remains running.'
}
