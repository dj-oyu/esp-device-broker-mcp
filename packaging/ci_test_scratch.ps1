# CI only: put Windows test temp files on the runner temp drive.
#
# Hosted Windows runners default TMP/TEMP to the C: user profile. Tests create
# many short-lived SQLite, lock and key files; moving them to a dedicated
# directory under RUNNER_TEMP (the D: work drive) roughly halved Windows test
# step time in CI. Later steps see TMP/TEMP pointing at that directory (via
# GITHUB_ENV), so pytest's tmp_path base and tempfile users land there.
#
# Nothing is excluded from antivirus scanning. Real-time protection state is
# logged on one line because it bears on Windows test timing.
param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

$ErrorActionPreference = 'Continue'
$scratch = (New-Item -ItemType Directory -Force -Path $Path).FullName

try {
    $realtime = (Get-MpComputerStatus -ErrorAction Stop).RealTimeProtectionEnabled
    Write-Host "Defender real-time protection enabled: $realtime"
} catch {
    Write-Host "Defender real-time protection state unavailable: $($_.Exception.Message)"
}

if ($env:GITHUB_ENV) {
    "TMP=$scratch" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
    "TEMP=$scratch" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
}
Write-Host "TMP/TEMP for later steps: $scratch"
exit 0
