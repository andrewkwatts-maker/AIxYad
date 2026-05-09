#Requires -Version 5.1
<#
.SYNOPSIS
    Continuous-mode poller — runs sprints back-to-back with NO inter-cycle
    Gaussian nap. One python sprint_runner per lane; multiple lanes can be
    launched in separate windows for parallel throughput.

.DESCRIPTION
    Each iteration:
      1. Settle for $InterCycleSeconds (default 10s — basically zero gap)
      2. Run run_autonomous.py for one project with full --max-minutes budget
      3. Loop forever (Ctrl-C to stop)

    Multiple instances of this script can run in parallel against the SAME
    project DB. SQLite serialises task reservation, so two lanes won't pick
    the same task. Caveat: agentic Claude bypasses the advisory file_locks
    table, so cross-lane file conflicts are possible — they'll show up as
    git merge conflicts. Run with eyes open.

.PARAMETER LaneId
    Identifier for this lane (used in logs to disambiguate). Default 'A'.

.PARAMETER Project
    Project directory. Default H:\Github\Nova3D

.PARAMETER Provider
    Automatica provider name. Default Claude-Opus

.PARAMETER MaxMinutesPerCycle
    Per-cycle wall budget. Default 110.

.PARAMETER InterCycleSeconds
    Settle delay between cycles. Default 10. Set to 0 for hottest loop.

.EXAMPLE
    # Single lane, foreground:
    .\scripts\nova_poll_continuous.ps1

    # Three parallel lanes:
    Start-Process powershell -ArgumentList '-NoExit','-File','...nova_poll_continuous.ps1','-LaneId','A'
    Start-Process powershell -ArgumentList '-NoExit','-File','...nova_poll_continuous.ps1','-LaneId','B'
    Start-Process powershell -ArgumentList '-NoExit','-File','...nova_poll_continuous.ps1','-LaneId','C'
#>
[CmdletBinding()]
param(
    [string]$LaneId             = 'A',
    [string]$Project            = 'H:\Github\Nova3D',
    [string]$Provider           = 'Claude-Opus',
    [int]   $MaxMinutesPerCycle = 110,
    [int]   $InterCycleSeconds  = 10
)

$AutomaticaDir = 'H:\Github\Automatica'
$LogFile       = Join-Path $AutomaticaDir "logs\nova_poll_lane_$LaneId.log"

New-Item -ItemType Directory -Path (Split-Path $LogFile) -Force | Out-Null

function Write-Log([string]$msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] [LANE-$LaneId] $msg"
    Write-Host $line
    for ($i = 0; $i -lt 3; $i++) {
        try {
            [System.IO.File]::AppendAllText($LogFile, $line + "`r`n",
                                            [System.Text.UTF8Encoding]::new($false))
            return
        } catch { Start-Sleep -Milliseconds (50 * ($i + 1)) }
    }
}

Write-Log "=== Lane $LaneId starting | project=$Project provider=$Provider max=${MaxMinutesPerCycle}min gap=${InterCycleSeconds}s ==="

$cycle = 0
while ($true) {
    $cycle++
    Write-Log "--- Cycle #$cycle start ---"

    if ($InterCycleSeconds -gt 0 -and $cycle -gt 1) {
        Start-Sleep -Seconds $InterCycleSeconds
    }

    $proc = Start-Process `
        -FilePath 'python' `
        -ArgumentList @(
            (Join-Path $AutomaticaDir 'run_autonomous.py'),
            '--Project', $Project,
            '--provider', $Provider,
            '--max-minutes', $MaxMinutesPerCycle,
            '--max-sprints', 9999
        ) `
        -WorkingDirectory $AutomaticaDir `
        -NoNewWindow -Wait -PassThru

    Write-Log "--- Cycle #$cycle end | exit=$($proc.ExitCode) ---"
}
