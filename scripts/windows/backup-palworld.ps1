[CmdletBinding()]
param(
    [string]$ConfigDir = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'config'),
    [string]$ServiceName = 'PalServer',
    [switch]$NoServiceControl
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Caretaker.Common.psm1') -Force

$lock = Enter-CaretakerOperationLock
$staging = $null
$wasRunning = $false
try {
    $config = Get-CaretakerConfig $ConfigDir
    $paths = Get-PalworldPaths $config
    Assert-SafeTree $paths.Save 'Palworld save directory'
    Assert-SafeTree $paths.Config 'Palworld config directory'
    Assert-RealFile (Join-Path $paths.Config 'LinuxServer\PalWorldSettings.ini') 'PalWorldSettings.ini'
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
    (Get-SnapshotManifest $staging | ConvertTo-Json -Depth 20) | Set-Content -LiteralPath (Join-Path $staging 'metadata\manifest.json') -Encoding utf8NoBOM
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
