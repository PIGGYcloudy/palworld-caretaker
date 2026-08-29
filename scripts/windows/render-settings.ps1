[CmdletBinding()]
param([string]$ConfigDir = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'config'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Caretaker.Common.psm1') -Force

function ConvertTo-IniValue([string]$Name, [string]$Value, [bool]$Quoted = $false) {
    if ($Value.IndexOfAny([char[]]"`r`n,()") -ge 0 -or $Value.EndsWith('\')) { throw "$Name contains an INI-reserved character." }
    if ($Quoted) {
        if ($Value.Contains('"')) { throw "$Name contains an INI-reserved character." }
        return '"' + $Value + '"'
    }
    return $Value
}

$config = Get-CaretakerConfig $ConfigDir; $paths = Get-PalworldPaths $config
$settings = Join-Path $paths.Config 'LinuxServer\PalWorldSettings.ini'
if (-not (Test-Path -LiteralPath $settings -PathType Leaf)) { throw 'PalWorldSettings.ini is missing; start the server once first.' }
$fields = [ordered]@{
    ServerPlayerMaxNum = ConvertTo-IniValue 'MAX_PLAYERS' (Get-ConfigValue $config 'MAX_PLAYERS' '10')
    ServerPassword = ConvertTo-IniValue 'SERVER_PASSWORD' (Get-ConfigValue $config 'SERVER_PASSWORD') $true
    AdminPassword = ConvertTo-IniValue 'ADMIN_PASSWORD' (Get-ConfigValue $config 'ADMIN_PASSWORD') $true
    ServerName = ConvertTo-IniValue 'SERVER_NAME' (Get-ConfigValue $config 'SERVER_NAME') $true
    ServerDescription = ConvertTo-IniValue 'SERVER_DESCRIPTION' (Get-ConfigValue $config 'SERVER_DESCRIPTION') $true
    PublicPort = ConvertTo-IniValue 'PUBLIC_PORT' (Get-ConfigValue $config 'PUBLIC_PORT' '8211')
    RESTAPIEnabled = 'True'
    RESTAPIPort = ConvertTo-IniValue 'PALWORLD_REST_API_PORT' (Get-ConfigValue $config 'PALWORLD_REST_API_PORT' '8212')
}
$option = 'OptionSettings=(' + (($fields.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ',') + ')'
$text = [System.IO.File]::ReadAllText($settings)
if ($text -notmatch '(?m)^\s*\[\/Script\/Pal\.PalWorldSettings\]\s*$') { throw 'PalWorldSettings.ini is missing the PalWorldSettings section.' }
if ($text -match '(?m)^\s*OptionSettings\s*=.*$') {
    # A MatchEvaluator returns data directly; values such as $&, $1, and ${x}
    # are never interpreted as .NET replacement-string syntax.
    $replacement = [System.Text.RegularExpressions.MatchEvaluator]({ param($match) $option }.GetNewClosure())
    $text = [regex]::Replace($text, '(?m)^\s*OptionSettings\s*=.*$', $replacement, 1)
}
else { $text = $text.TrimEnd("`r", "`n") + [Environment]::NewLine + $option + [Environment]::NewLine }
$temporary = "$settings.$PID.tmp"
try { [System.IO.File]::WriteAllText($temporary, $text, [System.Text.UTF8Encoding]::new($false)); Move-Item -LiteralPath $temporary -Destination $settings -Force }
finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
Write-Output "Rendered settings: $settings"
