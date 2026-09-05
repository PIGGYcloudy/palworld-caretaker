[CmdletBinding()]
param([string]$ConfigDir = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'config'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Caretaker.Common.psm1') -Force

function ConvertTo-IniValue([string]$Name, [string]$Value, [bool]$Quoted = $false) {
    if ($Value.IndexOfAny([char[]]"`r`n,()'`"") -ge 0 -or $Value.EndsWith('\')) { throw "$Name contains an INI-reserved character." }
    if ($Quoted) {
        if ($Value.Contains('"')) { throw "$Name contains an INI-reserved character." }
        return '"' + $Value + '"'
    }
    return $Value
}

function ConvertTo-IniBoolean([string]$Name, [string]$Value) {
    if ($Value -ieq 'true') { return 'True' }
    if ($Value -ieq 'false') { return 'False' }
    throw "$Name must be true or false."
}

# Split only top-level commas, preserving unknown quoted/nested world values.
function Split-IniFields([string]$Body) {
    $start = 0; $depth = 0; $quote = [char]0; $escaped = $false
    for ($i = 0; $i -lt $Body.Length; $i++) {
        $char = $Body[$i]
        if ($quote -ne [char]0) {
            if ($escaped) { $escaped = $false }
            elseif ($char -eq '\') { $escaped = $true }
            elseif ($char -eq $quote) { $quote = [char]0 }
        }
        elseif ($char -eq '"' -or $char -eq "'") { $quote = $char }
        elseif ($char -eq '(') { $depth++ }
        elseif ($char -eq ')') {
            $depth--
            if ($depth -lt 0) { throw 'Unbalanced OptionSettings tuple.' }
        }
        elseif ($char -eq ',' -and $depth -eq 0) {
            $Body.Substring($start, $i - $start)
            $start = $i + 1
        }
    }
    if ($quote -ne [char]0 -or $depth -ne 0) { throw 'Unterminated OptionSettings value.' }
    if ($Body.Length -gt 0) { $Body.Substring($start) }
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
    DayTimeSpeedRate = ConvertTo-IniValue 'DAY_TIME_SPEED_RATE' (Get-ConfigValue $config 'DAY_TIME_SPEED_RATE' '1.0')
    NightTimeSpeedRate = ConvertTo-IniValue 'NIGHT_TIME_SPEED_RATE' (Get-ConfigValue $config 'NIGHT_TIME_SPEED_RATE' '1.0')
    ExpRate = ConvertTo-IniValue 'EXP_RATE' (Get-ConfigValue $config 'EXP_RATE' '1.0')
    PalCaptureRate = ConvertTo-IniValue 'PAL_CAPTURE_RATE' (Get-ConfigValue $config 'PAL_CAPTURE_RATE' '1.0')
    CollectionDropRate = ConvertTo-IniValue 'COLLECTION_DROP_RATE' (Get-ConfigValue $config 'COLLECTION_DROP_RATE' '1.0')
    EnemyDropItemRate = ConvertTo-IniValue 'ENEMY_DROP_ITEM_RATE' (Get-ConfigValue $config 'ENEMY_DROP_ITEM_RATE' '1.0')
    PalDamageRateAttack = ConvertTo-IniValue 'PAL_DAMAGE_RATE_ATTACK' (Get-ConfigValue $config 'PAL_DAMAGE_RATE_ATTACK' '1.0')
    PalDamageRateDefense = ConvertTo-IniValue 'PAL_DAMAGE_RATE_DEFENSE' (Get-ConfigValue $config 'PAL_DAMAGE_RATE_DEFENSE' '1.0')
    PlayerDamageRateAttack = ConvertTo-IniValue 'PLAYER_DAMAGE_RATE_ATTACK' (Get-ConfigValue $config 'PLAYER_DAMAGE_RATE_ATTACK' '1.0')
    PlayerDamageRateDefense = ConvertTo-IniValue 'PLAYER_DAMAGE_RATE_DEFENSE' (Get-ConfigValue $config 'PLAYER_DAMAGE_RATE_DEFENSE' '1.0')
    PalStaminaDecreaceRate = ConvertTo-IniValue 'PAL_STAMINA_DECREACE_RATE' (Get-ConfigValue $config 'PAL_STAMINA_DECREACE_RATE' '1.0')
    PlayerStaminaDecreaceRate = ConvertTo-IniValue 'PLAYER_STAMINA_DECREACE_RATE' (Get-ConfigValue $config 'PLAYER_STAMINA_DECREACE_RATE' '1.0')
    PalAutoHPRegeneRate = ConvertTo-IniValue 'PAL_AUTO_HP_REGENE_RATE' (Get-ConfigValue $config 'PAL_AUTO_HP_REGENE_RATE' '1.0')
    PlayerAutoHPRegeneRate = ConvertTo-IniValue 'PLAYER_AUTO_HP_REGENE_RATE' (Get-ConfigValue $config 'PLAYER_AUTO_HP_REGENE_RATE' '1.0')
    PalAutoHpRegeneRateInSleep = ConvertTo-IniValue 'PAL_AUTO_HP_REGENE_RATE_IN_SLEEP' (Get-ConfigValue $config 'PAL_AUTO_HP_REGENE_RATE_IN_SLEEP' '1.0')
    PlayerAutoHpRegeneRateInSleep = ConvertTo-IniValue 'PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP' (Get-ConfigValue $config 'PLAYER_AUTO_HP_REGENE_RATE_IN_SLEEP' '1.0')
    PalStomachDecreaceRate = ConvertTo-IniValue 'PAL_STOMACH_DECREACE_RATE' (Get-ConfigValue $config 'PAL_STOMACH_DECREACE_RATE' '1.0')
    PlayerStomachDecreaceRate = ConvertTo-IniValue 'PLAYER_STOMACH_DECREACE_RATE' (Get-ConfigValue $config 'PLAYER_STOMACH_DECREACE_RATE' '1.0')
    GuildPlayerMaxNum = ConvertTo-IniValue 'GUILD_PLAYER_MAX_NUM' (Get-ConfigValue $config 'GUILD_PLAYER_MAX_NUM' '20')
    PalSpawnNumRate = ConvertTo-IniValue 'PAL_SPAWN_NUM_RATE' (Get-ConfigValue $config 'PAL_SPAWN_NUM_RATE' '1.0')
    DropItemMaxNum = ConvertTo-IniValue 'DROP_ITEM_MAX_NUM' (Get-ConfigValue $config 'DROP_ITEM_MAX_NUM' '3000')
    DropItemAliveMaxHours = ConvertTo-IniValue 'DROP_ITEM_ALIVE_MAX_HOURS' (Get-ConfigValue $config 'DROP_ITEM_ALIVE_MAX_HOURS' '1.0')
    PalEggDefaultHatchingTime = ConvertTo-IniValue 'PAL_EGG_DEFAULT_HATCHING_TIME' (Get-ConfigValue $config 'PAL_EGG_DEFAULT_HATCHING_TIME' '72.0')
    BaseCampWorkerMaxNum = ConvertTo-IniValue 'BASE_CAMP_WORKER_MAX_NUM' (Get-ConfigValue $config 'BASE_CAMP_WORKER_MAX_NUM' '15')
    WorkSpeedRate = ConvertTo-IniValue 'WORK_SPEED_RATE' (Get-ConfigValue $config 'WORK_SPEED_RATE' '1.0')
    ItemWeightRate = ConvertTo-IniValue 'ITEM_WEIGHT_RATE' (Get-ConfigValue $config 'ITEM_WEIGHT_RATE' '1.0')
    EquipmentDurabilityDamageRate = ConvertTo-IniValue 'EQUIPMENT_DURABILITY_DAMAGE_RATE' (Get-ConfigValue $config 'EQUIPMENT_DURABILITY_DAMAGE_RATE' '1.0')
    DeathPenalty = ConvertTo-IniValue 'DEATH_PENALTY' (Get-ConfigValue $config 'DEATH_PENALTY' 'Item')
    BuildObjectHpRate = ConvertTo-IniValue 'BUILD_OBJECT_HP_RATE' (Get-ConfigValue $config 'BUILD_OBJECT_HP_RATE' '1.0')
    BuildObjectDamageRate = ConvertTo-IniValue 'BUILD_OBJECT_DAMAGE_RATE' (Get-ConfigValue $config 'BUILD_OBJECT_DAMAGE_RATE' '1.0')
    BuildObjectDeteriorationDamageRate = ConvertTo-IniValue 'BUILD_OBJECT_DETERIORATION_DAMAGE_RATE' (Get-ConfigValue $config 'BUILD_OBJECT_DETERIORATION_DAMAGE_RATE' '1.0')
    AutoResetWorkerPalWhenServerRestart = ConvertTo-IniBoolean 'AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART' (Get-ConfigValue $config 'AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART' 'false')
    BaseCampMaxNumInGuild = ConvertTo-IniValue 'BASE_CAMP_MAX_NUM_IN_GUILD' (Get-ConfigValue $config 'BASE_CAMP_MAX_NUM_IN_GUILD' '10')
    bIsUseBackupSaveData = 'True'
    RESTAPIEnabled = 'True'
    RESTAPIPort = ConvertTo-IniValue 'PALWORLD_REST_API_PORT' (Get-ConfigValue $config 'PALWORLD_REST_API_PORT' '8212')
}
$text = [System.IO.File]::ReadAllText($settings)
# Restrict edits to exactly one target section and tuple. Ambiguous or broken
# input must fail before any write, rather than discard world configuration.
$sections = [regex]::Matches($text, '(?m)^[ \t]*\[([^]\r\n]+)\][ \t]*\r?$')
$targets = @($sections | Where-Object { $_.Groups[1].Value -ceq '/Script/Pal.PalWorldSettings' })
if ($targets.Count -ne 1) { throw 'Expected exactly one PalWorldSettings section.' }
$start = $targets[0].Index + $targets[0].Length
$end = $text.Length
foreach ($section in $sections) {
    if ($section.Index -gt $start) { $end = $section.Index; break }
}
$body = $text.Substring($start, $end - $start)
$options = [regex]::Matches($body, '(?m)^[ \t]*OptionSettings[ \t]*=[^\r\n]*')
if ($options.Count -ne 1) { throw 'Expected exactly one OptionSettings tuple in the PalWorldSettings section.' }
$match = [regex]::Match($options[0].Value, '^([ \t]*OptionSettings[ \t]*=[ \t]*)\((.*)\)([ \t]*)$')
if (-not $match.Success) { throw 'OptionSettings must be a complete tuple on one line.' }
$present = @{}
$rendered = @(
    foreach ($field in @(Split-IniFields $match.Groups[2].Value)) {
        $keyMatch = [regex]::Match($field, '^([ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=)(.*)$')
        if (-not $keyMatch.Success) { throw 'Invalid OptionSettings field.' }
        $key = $keyMatch.Groups[2].Value
        if ($present.ContainsKey($key)) { throw "Duplicate OptionSettings field: $key" }
        $present[$key] = $true
        if ($fields.Contains($key)) { $keyMatch.Groups[1].Value + $fields[$key] }
        else { $field }
    }
    foreach ($entry in $fields.GetEnumerator()) {
        if (-not $present.ContainsKey($entry.Key)) { "$($entry.Key)=$($entry.Value)" }
    }
)
$option = $match.Groups[1].Value + '(' + ($rendered -join ',') + ')' + $match.Groups[3].Value
# Concatenation treats dollar replacement tokens as literal data.
$offset = $start + $options[0].Index
$text = $text.Substring(0, $offset) + $option + $text.Substring($offset + $options[0].Length)
$temporary = "$settings.$PID.tmp"
try { [System.IO.File]::WriteAllText($temporary, $text, [System.Text.UTF8Encoding]::new($false)); Move-Item -LiteralPath $temporary -Destination $settings -Force }
finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
Write-Output "Rendered settings: $settings"
