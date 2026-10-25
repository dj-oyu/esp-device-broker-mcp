# CI only: give Windows test jobs a dedicated temp directory outside Defender's
# real-time scanning. Tests create many short-lived SQLite, lock and key files;
# on-access scanning of those on an ephemeral runner yields no detection report
# but can block the event loop for seconds. Only this scratch directory is
# excluded; the checkout, virtual environment and build outputs stay scanned, and
# shipped artifacts get an explicit scan in the package workflow.
#
# On windows-2022 hosted runners (observed 2026-10) real-time protection was
# already off, so the exclusion is a guard for images where it is on. The
# scratch directory under RUNNER_TEMP also moves test temp files off the
# default C: profile temp onto the runner's work disk.
#
# Later steps see TMP/TEMP pointing at the scratch directory (via GITHUB_ENV),
# so pytest's tmp_path base and tempfile users land inside the exclusion. A
# missing Defender or a refused exclusion is logged and never fails the job.
param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

$ErrorActionPreference = 'Continue'
$scratch = (New-Item -ItemType Directory -Force -Path $Path).FullName

Write-Host '--- Defender status before exclusion ---'
try {
    Get-MpComputerStatus -ErrorAction Stop |
        Select-Object RealTimeProtectionEnabled, AMServiceEnabled, AntivirusEnabled,
            OnAccessProtectionEnabled, AntivirusSignatureLastUpdated |
        Format-List | Out-String | Write-Host
} catch {
    Write-Host "::warning::Get-MpComputerStatus unavailable: $($_.Exception.Message)"
}

try {
    Add-MpPreference -ExclusionPath $scratch -ErrorAction Stop
    Write-Host "Defender exclusion added for test scratch: $scratch"
    try {
        $listed = @((Get-MpPreference -ErrorAction Stop).ExclusionPath)
        Write-Host "Exclusion listed by Get-MpPreference: $($listed -contains $scratch)"
    } catch {
        Write-Host "Get-MpPreference unavailable: $($_.Exception.Message)"
    }
} catch {
    Write-Host "::warning::Defender exclusion not added (continuing with scanning): $($_.Exception.Message)"
}

if ($env:GITHUB_ENV) {
    "TMP=$scratch" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
    "TEMP=$scratch" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
}
Write-Host "TMP/TEMP for later steps: $scratch"
exit 0
