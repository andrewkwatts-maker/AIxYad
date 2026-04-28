#Requires -Version 5.1
<#
.SYNOPSIS
    Single-shot Automatica sprint runner — invoked by the Windows Scheduled Task
    "Automatica_Nova3D_Poll". After the cycle completes, self-reschedules its
    next run with a random delay drawn from a configurable weighted distribution.

.PARAMETER Project
    Project directory. Default: H:\Github\Nova3D

.PARAMETER Provider
    Automatica provider name. Default: Claude-Opus

.PARAMETER MaxMinutesPerCycle
    Per-cycle wall-clock budget for the sprint runner. Default: 110

.PARAMETER IntervalHours
    Comma-separated list of candidate interval bins (in hours).
    Default: "1,2,3,4,5,6,7,8"

.PARAMETER IntervalWeights
    Comma-separated weights, same length as -IntervalHours. Will be auto-normalised
    so they need not sum to 1. Default geometric decay (each bin half as likely as
    the previous). With defaults this gives mean ≈ 1.99 hr.

.PARAMETER ConfigFile
    Optional path to a JSON file overriding any of the above. Useful for tuning
    the distribution without re-registering the scheduled task.
    Schema:
      {
        "Project": "...",
        "Provider": "...",
        "MaxMinutesPerCycle": 110,
        "IntervalHours":   [1,2,3,4,5,6,7,8],
        "IntervalWeights": [0.5,0.25,0.125,0.0625,0.03125,0.015625,0.0078125,0.0078125]
      }
    File path defaults to  H:\Github\Automatica\nova_poll_config.json  if it exists.

.EXAMPLE
    # Default: weighted-random 1-8 hr, mean ~2 hr
    .\nova_poll_once.ps1

    # Flat distribution 1-4 hr (mean 2.5)
    .\nova_poll_once.ps1 -IntervalHours "1,2,3,4" -IntervalWeights "1,1,1,1"

    # Override via JSON file
    .\nova_poll_once.ps1 -ConfigFile "C:\path\to\poll_config.json"
#>
[CmdletBinding()]
param(
    [string]$Project            = "H:\Github\Nova3D",
    [string]$Provider           = "Claude-Opus",
    [int]   $MaxMinutesPerCycle = 110,
    [string]$IntervalHours      = "1,2,3,4,5,6,7,8",
    [string]$IntervalWeights    = "0.5,0.25,0.125,0.0625,0.03125,0.015625,0.0078125,0.0078125",
    [string]$ConfigFile         = ""
)

$AutomaticaDir = "H:\Github\Automatica"
$LogFile       = Join-Path $AutomaticaDir "logs\nova_poll.log"
$TaskName      = "Automatica_Nova3D_Poll"
$DefaultConfig = Join-Path $AutomaticaDir "nova_poll_config.json"

New-Item -ItemType Directory -Path (Split-Path $LogFile) -Force | Out-Null

function Write-Log([string]$msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

# ── Optional config-file override ────────────────────────────────────────────
if (-not $ConfigFile -and (Test-Path $DefaultConfig)) {
    $ConfigFile = $DefaultConfig
}
if ($ConfigFile -and (Test-Path $ConfigFile)) {
    try {
        $cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
        if ($cfg.PSObject.Properties.Name -contains 'Project')            { $Project            = $cfg.Project }
        if ($cfg.PSObject.Properties.Name -contains 'Provider')           { $Provider           = $cfg.Provider }
        if ($cfg.PSObject.Properties.Name -contains 'MaxMinutesPerCycle') { $MaxMinutesPerCycle = [int]$cfg.MaxMinutesPerCycle }
        if ($cfg.PSObject.Properties.Name -contains 'IntervalHours')      { $IntervalHours      = ($cfg.IntervalHours   -join ',') }
        if ($cfg.PSObject.Properties.Name -contains 'IntervalWeights')    { $IntervalWeights    = ($cfg.IntervalWeights -join ',') }
        Write-Log "Loaded config overrides from $ConfigFile"
    } catch {
        Write-Log "WARN: Failed to parse $ConfigFile : $_"
    }
}

# Parse and validate the distribution
$hoursArr   = $IntervalHours   -split ',' | ForEach-Object { [int]$_.Trim()    }
$weightsArr = $IntervalWeights -split ',' | ForEach-Object { [double]$_.Trim() }
if ($hoursArr.Count -ne $weightsArr.Count -or $hoursArr.Count -eq 0) {
    throw "IntervalHours and IntervalWeights must be non-empty and the same length. Got hours=$($hoursArr.Count) weights=$($weightsArr.Count)."
}
$weightSum = ($weightsArr | Measure-Object -Sum).Sum
if ($weightSum -le 0) { throw "IntervalWeights must sum to > 0." }

# Normalise + compute the actual mean so we can log it
$normWeights = $weightsArr | ForEach-Object { $_ / $weightSum }
$meanHours = 0.0
for ($i = 0; $i -lt $hoursArr.Count; $i++) {
    $meanHours += $hoursArr[$i] * $normWeights[$i]
}

function Get-NextIntervalHours {
    $r   = Get-Random -Minimum 0.0 -Maximum 1.0
    $cum = 0.0
    for ($i = 0; $i -lt $hoursArr.Count; $i++) {
        $cum += $normWeights[$i]
        if ($r -le $cum) { return $hoursArr[$i] }
    }
    return $hoursArr[-1]
}

function Reschedule-NextRun([int]$hoursFromNow) {
    $nextRun = (Get-Date).AddHours($hoursFromNow)
    $trigger = New-ScheduledTaskTrigger -Once -At $nextRun
    Set-ScheduledTask -TaskName $TaskName -Trigger $trigger | Out-Null
    Write-Log "Next run scheduled: $($nextRun.ToString('yyyy-MM-dd HH:mm:ss')) ( +${hoursFromNow}h )"
}

# ── Run the cycle ────────────────────────────────────────────────────────────
Write-Log "=== Cycle start | project=$Project provider=$Provider max=${MaxMinutesPerCycle}min | dist mean=$([Math]::Round($meanHours,2))h, range $($hoursArr[0])-$($hoursArr[-1])h ==="

$proc = Start-Process `
    -FilePath "python" `
    -ArgumentList @(
        (Join-Path $AutomaticaDir "run_autonomous.py"),
        "--Project", $Project,
        "--provider", $Provider,
        "--max-minutes", $MaxMinutesPerCycle,
        "--max-sprints", 9999
    ) `
    -WorkingDirectory $AutomaticaDir `
    -NoNewWindow -Wait -PassThru

Write-Log "=== Cycle end   | exit=$($proc.ExitCode) ==="

# ── Schedule next firing with weighted-random interval ───────────────────────
try {
    $nextHours = Get-NextIntervalHours
    Reschedule-NextRun -hoursFromNow $nextHours
} catch {
    Write-Log "ERROR rescheduling next run: $_"
}

exit $proc.ExitCode
