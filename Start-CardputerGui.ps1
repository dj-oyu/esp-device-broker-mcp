#requires -Version 5.1
<#
Start one broker and its local queue-manager window. Default: authenticated MOCK.
Use -EnableSerial with an existing private config only when intentionally enabling
serial mode. Startup never opens USB and cannot approve/execute a hardware plan.
#>
[CmdletBinding()]
param([string]$Config = '', [switch]$EnableSerial, [string]$KeyRegistry = '')
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This launcher requires Windows PowerShell 5.1 or PowerShell 7 on Windows.'
}
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).ProviderPath
if (!$Config) {
    if ($EnableSerial) { $Config = 'device.local.toml' }
    else {
        $Config = 'device.mock.local.toml'
        $privateMock = Join-Path $projectRoot $Config
        if (!(Test-Path -LiteralPath $privateMock)) {
            # Generic mock metadata only. CreateNew refuses races; never overwrite settings.
            $source = [IO.File]::OpenRead((Join-Path $projectRoot 'examples\tunnel-mock.toml'))
            try {
                $target = [IO.File]::Open($privateMock, [IO.FileMode]::CreateNew,
                    [IO.FileAccess]::Write, [IO.FileShare]::None)
                try { $source.CopyTo($target) } finally { $target.Dispose() }
            } finally { $source.Dispose() }
        }
    }
}
if (![IO.Path]::IsPathRooted($Config)) { $Config = Join-Path $projectRoot $Config }
$arguments = @{ Config = $Config }
if (!$EnableSerial) { $arguments.Mock = $true }
if ($KeyRegistry) { $arguments.KeyRegistry = $KeyRegistry }
& (Join-Path $projectRoot 'Start-Cardputer.ps1') @arguments
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
Write-Host 'Opening local manager. Tunnel stays disconnected until you click Connect.'
Write-Host 'Closing asks to stop this GUI-owned Tunnel only; broker and device jobs continue.'
& $python -m cardputer_mcp.cli --auth-source environment gui --config $Config
if ($LASTEXITCODE -ne 0) {
    throw 'Queue manager could not start. Check that this Python includes Tkinter; the broker remains running.'
}
