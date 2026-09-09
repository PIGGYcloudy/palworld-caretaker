[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('start', 'stop', 'restart', 'status')][string]$Action,
    [string]$ServiceName = 'PalServer',
    [string]$ConfigDir = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'config'),
    [switch]$WhatIf
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Caretaker.Common.psm1') -Force
if ($WhatIf) { Write-Output "WHATIF $Action $ServiceName"; exit 0 }
$lock = $null
try {
    $config = Get-CaretakerConfig $ConfigDir
    $paths = Get-PalworldPaths $config
    $serverPrefix = $paths.Server.TrimEnd('\') + '\'
    $processes = @(Get-Process -Name 'PalServer', 'PalServer-Win64-Shipping', 'PalServer-Win64-Shipping-Cmd', 'PalServer-Win64-Test', 'PalServer-Win64-Test-Cmd' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($serverPrefix, [StringComparison]::OrdinalIgnoreCase) })
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($Action -eq 'status') {
        if ($processes.Count) { 'RUNNING' }
        elseif ($service) { $service.Status.ToString().ToUpperInvariant() }
        else { 'STOPPED' }
        exit 0
    }
    if ($Action -ne 'status') { $lock = Enter-CaretakerOperationLock }
    if (-not $service) {
        if ($Action -ne 'start') { throw 'Use the panel safe shutdown operation for a native process.' }
        if ($processes.Count) { 'ALREADY RUNNING'; exit 0 }
        $executable = Join-Path $paths.Server 'PalServer.exe'
        Assert-RealFile $executable 'Palworld server executable'
        & (Join-Path $PSScriptRoot 'render-settings.ps1') -ConfigDir $ConfigDir
        if (-not $?) { throw 'Could not render game settings.' }
        $publicPort = Get-ConfigValue $config 'PUBLIC_PORT' '8211'
        $queryPort = Get-ConfigValue $config 'QUERY_PORT' '27015'
        Start-Process -FilePath $executable -WorkingDirectory $paths.Server -WindowStyle Hidden `
            -ArgumentList @("-port=$publicPort", "-publicport=$publicPort", "-queryport=$queryPort") | Out-Null
        'STARTED'
        exit 0
    }
    switch ($Action) {
        'start' { & (Join-Path $PSScriptRoot 'render-settings.ps1') -ConfigDir $ConfigDir; Start-Service -Name $ServiceName -ErrorAction Stop; Write-Output 'STARTED' }
        'stop' { Stop-Service -Name $ServiceName -ErrorAction Stop; Write-Output 'STOPPED' }
        'restart' { Restart-Service -Name $ServiceName -ErrorAction Stop; Write-Output 'RESTARTED' }
        'status' { (Get-Service -Name $ServiceName -ErrorAction Stop).Status.ToString().ToUpperInvariant() }
    }
} finally {
    Exit-CaretakerOperationLock $lock
}
