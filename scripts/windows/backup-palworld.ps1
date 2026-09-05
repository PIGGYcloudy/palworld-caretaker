[CmdletBinding()]
param(
    [string]$ConfigDir = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'config'),
    [string]$ServiceName = 'PalServer',
    [switch]$NoServiceControl,
    # A Windows Task Scheduler trigger may invoke this every minute. The
    # schedule gate then uses the same BACKUP_TIME syntax as Linux.
    [switch]$Scheduled
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Caretaker.Common.psm1') -Force

$lock = Enter-CaretakerOperationLock
$staging = $null
$wasRunning = $false
try {
    $config = Get-CaretakerConfig $ConfigDir
    if ($Scheduled) {
        if ((Get-ConfigValue $config 'PALWORLD_BACKUP_SCHEDULE_ENABLED' 'true').ToLowerInvariant() -ne 'true') {
            Write-Output 'Scheduled backup is disabled by PALWORLD_BACKUP_SCHEDULE_ENABLED.'
            return
        }
        $schedule = Get-ConfigValue $config 'BACKUP_TIME' 'daily-04:30'
        $now = Get-Date
        $due = switch -Regex ($schedule) {
            '^off$' { $false; break }
            '^daily-(?<time>(?:[01][0-9]|2[0-3]):[0-5][0-9])$' { $now.ToString('HH:mm') -eq $Matches.time; break }
            '^(?<time>(?:[01][0-9]|2[0-3]):[0-5][0-9])$' { $now.ToString('HH:mm') -eq $Matches.time; break }
            '^every-(?<hours>2|4|6|12)h$' { $interval = [int]$Matches['hours']; $now.Minute -eq 0 -and ($now.Hour % $interval -eq 0); break }
            default { throw 'BACKUP_TIME must be daily-HH:MM, every-2h, every-4h, every-6h, every-12h, or off.' }
        }
        if (-not $due) { return }
    }
    $paths = Get-PalworldPaths $config
    Assert-SafeTree $paths.Save 'Palworld save directory'
    Assert-SafeTree $paths.Config 'Palworld config directory'
    Assert-RealFile (Join-Path $paths.Config 'WindowsServer\PalWorldSettings.ini') 'PalWorldSettings.ini'
    if (-not (Get-ChildItem -LiteralPath $paths.Save -Directory -Recurse -Filter backup | Select-Object -First 1)) { throw 'Palworld built-in backup directory was not found.' }
    if (!$NoServiceControl) {
        $service = Get-Service -Name $ServiceName -ErrorAction Stop
        $wasRunning = $service.Status -eq 'Running'
        if ($wasRunning) { Stop-Service -Name $ServiceName -ErrorAction Stop }
    }
    New-Item -ItemType Directory -Path $paths.Backup -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $snapshot = Join-Path $paths.Backup "palworld-$stamp"
    if (Test-Path -LiteralPath $snapshot) { throw "Backup version already exists: $stamp" }
    $staging = Join-Path $paths.Backup ".incomplete-$stamp-$PID"
    New-Item -ItemType Directory -Path (Join-Path $staging 'savegames'), (Join-Path $staging 'config'), (Join-Path $staging 'metadata') | Out-Null
    Get-ChildItem -LiteralPath $paths.Save -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $staging 'savegames') -Recurse -Force
    }
    Get-ChildItem -LiteralPath $paths.Config -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $staging 'config') -Recurse -Force
    }
    $manifestPath = Join-Path $staging 'metadata\manifest.json'
    $manifestJson = Get-SnapshotManifest $staging | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($manifestPath, $manifestJson, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $staging -Destination $snapshot
    $retention = [int](Get-ConfigValue $config 'BACKUP_RETENTION_COUNT' '14')
    if ($retention -lt 1) { throw 'BACKUP_RETENTION_COUNT must be at least 1.' }
    $snapshots = @(Get-ChildItem -LiteralPath $paths.Backup -Directory |
        Where-Object { Test-SnapshotName $_.Name } |
        Sort-Object Name)
    $excessSnapshots = [Math]::Max(0, $snapshots.Count - $retention)
    $snapshots |
        Select-Object -First $excessSnapshots |
        Remove-Item -Recurse -Force
    Write-Output "Created backup version: $(Split-Path $snapshot -Leaf)"
} finally {
    if ($staging -and (Test-Path -LiteralPath $staging)) { Remove-Item -LiteralPath $staging -Recurse -Force }
    if ($wasRunning) { Start-Service -Name $ServiceName -ErrorAction Stop }
    Exit-CaretakerOperationLock $lock
}
