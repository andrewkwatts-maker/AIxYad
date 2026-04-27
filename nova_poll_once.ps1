#Requires -Version 5.1
<#
.SYNOPSIS
    Single-shot Automatica sprint runner — invoked by the Windows Scheduled Task
    "Automatica_Nova3D_Poll" every 2 hours. Exits when the cycle completes.

.PARAMETER Project
.PARAMETER Provider
.PARAMETER MaxMinutesPerCycle  Max time the sprint runner can spend in this cycle.
.PARAMETER IntervalHours       Logged for context; scheduling is owned by Task Scheduler.
#>
[CmdletBinding()]
param(
    [string]$Project            = "H:\Github\Nova3D",
    [string]$Provider           = "Claude-Sonnet",
    [int]   $MaxMinutesPerCycle = 110,
    [double]$IntervalHours      = 2.0
)

$AutomaticaDir = "H:\Github\Automatica"
$LogFile       = Join-Path $AutomaticaDir "logs\nova_poll.log"

New-Item -ItemType Directory -Path (Split-Path $LogFile) -Force | Out-Null

function Write-Log([string]$msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

Write-Log "=== Cycle start | project=$Project provider=$Provider max=${MaxMinutesPerCycle}min interval=${IntervalHours}h ==="

# run_autonomous.py resets stuck tasks at startup automatically
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

Write-Log "=== Cycle end | exit=$($proc.ExitCode) ==="
exit $proc.ExitCode
