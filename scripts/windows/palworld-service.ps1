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
    if ($Action -ne 'status') { $lock = Enter-CaretakerOperationLock }
    switch ($Action) {
        'start' { Start-Service -Name $ServiceName -ErrorAction Stop; Write-Output 'STARTED' }
        'stop' { Stop-Service -Name $ServiceName -ErrorAction Stop; Write-Output 'STOPPED' }
        'restart' { Restart-Service -Name $ServiceName -ErrorAction Stop; Write-Output 'RESTARTED' }
        'status' { (Get-Service -Name $ServiceName -ErrorAction Stop).Status.ToString().ToUpperInvariant() }
    }
} finally {
    Exit-CaretakerOperationLock $lock
}
