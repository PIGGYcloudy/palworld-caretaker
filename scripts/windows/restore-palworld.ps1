[CmdletBinding()]
param(
    [string]$ConfigDir = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'config'),
    [string]$Version,
    [string]$ServiceName = 'PalServer',
    [switch]$NoServiceControl,
    [switch]$Force,
    [switch]$List
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Caretaker.Common.psm1') -Force
$lock = Enter-CaretakerOperationLock
$wasRunning = $false
$restoreStaging = $null
$safety = $null
$liveReplacementStarted = $false
try {
    $config = Get-CaretakerConfig $ConfigDir; $paths = Get-PalworldPaths $config
    if ($List) { Get-ChildItem -LiteralPath $paths.Backup -Directory -ErrorAction SilentlyContinue | Where-Object { Test-SnapshotName $_.Name } | Sort-Object Name -Descending | Select-Object -ExpandProperty Name; return }
    if (!(Test-SnapshotName $Version)) { throw 'Backup version name is invalid.' }
    $snapshot = Join-Path $paths.Backup $Version
    Assert-SafeTree $snapshot 'Backup snapshot'
    Assert-SafeTree (Join-Path $snapshot 'savegames') 'Backup savegames directory'
    Assert-SafeTree (Join-Path $snapshot 'config') 'Backup config directory'
    Assert-RealFile (Join-Path $snapshot 'config\LinuxServer\PalWorldSettings.ini') 'Backup settings file'
    # Capture the validated inventory now.  The later staging verification
    # intentionally does not reread this manifest, closing the check/copy
    # window if the snapshot pathname is replaced while copying.
    $manifest = Assert-SnapshotManifest $snapshot
    Assert-SafeTree $paths.Save 'Palworld save directory'
    Assert-SafeTree $paths.Config 'Palworld config directory'
    if (!$Force) { $answer = Read-Host "Type RESTORE $Version to continue"; if ($answer -ne "RESTORE $Version") { throw 'Restore confirmation did not match.' } }
    if (!$NoServiceControl) { $wasRunning = (Get-Service -Name $ServiceName -ErrorAction Stop).Status -eq 'Running'; if ($wasRunning) { Stop-Service -Name $ServiceName -ErrorAction Stop } }
    $safety = Join-Path $paths.LocalBackup ("pre-restore-" + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [guid]::NewGuid().ToString('N'))
    if (Test-Path -LiteralPath $safety) { throw "Pre-restore safety copy already exists: $safety" }
    New-Item -ItemType Directory -Path $safety -ErrorAction Stop | Out-Null
    Copy-Item -LiteralPath $paths.Save -Destination (Join-Path $safety 'savegames') -Recurse -Force
    Copy-Item -LiteralPath $paths.Config -Destination (Join-Path $safety 'config') -Recurse -Force
    $restoreStaging = Join-Path $paths.LocalBackup (".restore-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + "-$PID")
    New-Item -ItemType Directory -Path $restoreStaging | Out-Null
    Copy-Item -LiteralPath (Join-Path $snapshot 'savegames') -Destination (Join-Path $restoreStaging 'savegames') -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $snapshot 'config') -Destination (Join-Path $restoreStaging 'config') -Recurse -Force
    $stagedManifest = Get-SnapshotManifest $restoreStaging
    if ($stagedManifest.files.Count -ne $manifest.files.Count) { throw 'Restore staging does not match the validated backup manifest.' }
    foreach ($name in $manifest.files.Keys) {
        if (-not $stagedManifest.files.ContainsKey($name) -or
            [int64]$stagedManifest.files[$name].size -ne [int64]$manifest.files[$name].size -or
            -not [string]::Equals([string]$stagedManifest.files[$name].sha256, [string]$manifest.files[$name].sha256, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Restore staging does not match the validated backup manifest.'
        }
    }
    $liveReplacementStarted = $true
    Remove-Item -LiteralPath $paths.Save, $paths.Config -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $restoreStaging 'savegames') -Destination $paths.Save -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $restoreStaging 'config') -Destination $paths.Config -Recurse -Force
    Write-Output "Restore completed from $Version."
    Write-Output "Current pre-restore safety copy: $safety"
} catch {
    $failure = $_
    if ($liveReplacementStarted -and $safety) {
        try {
            Remove-Item -LiteralPath $paths.Save, $paths.Config -Recurse -Force -ErrorAction SilentlyContinue
            Copy-Item -LiteralPath (Join-Path $safety 'savegames') -Destination $paths.Save -Recurse -Force
            Copy-Item -LiteralPath (Join-Path $safety 'config') -Destination $paths.Config -Recurse -Force
            throw "Restore failed and the pre-restore safety copy was restored: $($failure.Exception.Message)"
        } catch {
            if ($_.Exception.Message -like 'Restore failed and*') { throw }
            throw "Restore failed and automatic rollback also failed; safety copy remains at $safety. Original error: $($failure.Exception.Message)"
        }
    }
    throw
} finally {
    if ($restoreStaging -and (Test-Path -LiteralPath $restoreStaging)) { Remove-Item -LiteralPath $restoreStaging -Recurse -Force }
    if ($wasRunning) { Start-Service -Name $ServiceName -ErrorAction Stop }
    Exit-CaretakerOperationLock $lock
}
