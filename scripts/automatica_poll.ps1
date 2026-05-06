#Requires -Version 5.1
<#
.SYNOPSIS
    Automatica autonomous poll loop — runs sprint cycles on a fixed interval
    against a single project. run_autonomous.py recovers stuck tasks at startup.

.PARAMETER Project
    Project directory. Default: H:\Github\Pong

.PARAMETER Provider
    AI provider name. Default: Phi4 (local Ollama). Any provider name
    configured in ~/.automatica/automatica.db works.

.PARAMETER IntervalHours
    Hours between cycles. Default: 1.0

.PARAMETER MaxMinutesPerCycle
    Max sprint runtime per cycle in minutes. Default: 110

.EXAMPLE
    .\automatica_poll.ps1
    .\automatica_poll.ps1 -IntervalHours 0.5 -Provider Phi4
    .\automatica_poll.ps1 -Project H:\Github\Pong -Provider Phi4 -IntervalHours 0.5
#>
[CmdletBinding()]
param(
    [string]$Project            = "H:\Github\Pong",
    [string]$Provider           = "Phi4",
    [double]$IntervalHours      = 1.0,
    [int]   $MaxMinutesPerCycle = 110,
    [string]$ProjectGoal        = ""
)

$AutomaticaDir = "H:\Github\Automatica"
$LogFile       = Join-Path $AutomaticaDir "logs\automatica_poll.log"
$SleepSeconds  = [int]($IntervalHours * 3600)

New-Item -ItemType Directory -Path (Split-Path $LogFile) -Force | Out-Null

function Write-Log([string]$msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

$RunScript = Join-Path $AutomaticaDir "run_autonomous.py"

# ── Interactive setup ─────────────────────────────────────────────────────
# If no goal was supplied AND the project has no DB yet, walk the user
# through setup instead of silently looping over an empty backlog forever.
$ProjectDbPath = Join-Path $Project ".automatica\project.db"
if (($ProjectGoal -eq "") -and -not (Test-Path $ProjectDbPath)) {
    Write-Host ""
    Write-Host "==== Automatica setup ====" -ForegroundColor Cyan
    Write-Host "Project folder: $Project"
    if (-not (Test-Path $Project)) {
        Write-Host "  (folder does not exist; will be created)"
    }
    Write-Host "Provider:       $Provider"
    Write-Host "Cycle interval: ${IntervalHours}h"
    Write-Host ""
    Write-Host "No project goal set, and no project DB exists yet."
    Write-Host "Describe what you want built (one line, can be long):"
    $ProjectGoal = Read-Host "Goal"
    if ([string]::IsNullOrWhiteSpace($ProjectGoal)) {
        Write-Host ""
        Write-Host "No goal entered. Aborting — re-run with -ProjectGoal '<your goal>' or enter a goal at this prompt." -ForegroundColor Yellow
        exit 1
    }
    if (-not (Test-Path $Project)) {
        New-Item -ItemType Directory -Path $Project -Force | Out-Null
    }
    Write-Host ""
    Write-Host "Goal accepted. Starting poll loop." -ForegroundColor Green
    Write-Host ""
}

$cycle = 0
Write-Log "Automatica poll loop started -- project: $Project, provider: $Provider, interval: ${IntervalHours}h"

while ($true) {
    $cycle++
    Write-Log "=== Cycle #$cycle ==="

    # Use & invocation (not Start-Process) — PS 5.1's Start-Process mangles
    # ArgumentList items containing parens, colons, or quoted strings.
    Push-Location $AutomaticaDir
    try {
        if ($ProjectGoal -ne "") {
            & python $RunScript --Project $Project --provider $Provider `
                --max-minutes "$MaxMinutesPerCycle" --max-sprints "9999" `
                --ProjectGoal $ProjectGoal
        } else {
            & python $RunScript --Project $Project --provider $Provider `
                --max-minutes "$MaxMinutesPerCycle" --max-sprints "9999"
        }
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    Write-Log "=== Cycle #$cycle complete (exit $exitCode) ==="

    $wake = (Get-Date).AddSeconds($SleepSeconds)
    Write-Log "Sleeping until $($wake.ToString('yyyy-MM-dd HH:mm:ss')) (${IntervalHours}h)..."
    Start-Sleep -Seconds $SleepSeconds
}
