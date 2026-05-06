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
# Walk the user through a who/what/when/where/why setup if they launched
# without enough info — instead of silently looping over an empty backlog.
$ProjectDbPath = Join-Path $Project ".automatica\project.db"
$NeedsSetup = ($ProjectGoal -eq "") -and -not (Test-Path $ProjectDbPath)

if ($NeedsSetup) {
    Write-Host ""
    Write-Host "==== Automatica setup ====" -ForegroundColor Cyan
    Write-Host "Walking you through 5 quick choices. Press Enter to accept the default in [brackets]."
    Write-Host ""

    # WHERE: project folder
    Write-Host "WHERE - project folder" -ForegroundColor Yellow
    Write-Host "  Where should the code live? Will be created if it doesn't exist."
    $reply = Read-Host "  Project folder [$Project]"
    if (-not [string]::IsNullOrWhiteSpace($reply)) { $Project = $reply }
    Write-Host ""

    # WHO: provider/model
    Write-Host "WHO - which AI does the work" -ForegroundColor Yellow
    Write-Host "  Local Ollama choices: Phi4, Mistral, Llama3.1, DeepseekCoder"
    Write-Host "  Cloud (slower turnaround, costs $$): Claude-Sonnet, Claude-Opus"
    $reply = Read-Host "  Provider [$Provider]"
    if (-not [string]::IsNullOrWhiteSpace($reply)) { $Provider = $reply }
    Write-Host ""

    # WHAT: goal
    Write-Host "WHAT - the goal" -ForegroundColor Yellow
    Write-Host "  One line, as concrete as you can. The agent uses this to generate"
    Write-Host "  the initial backlog. Bad: 'make a game'. Good: 'Pong in pygame:"
    Write-Host "  W/S left, UP/DOWN right, ball bounces, score to 10 wins, ESC quits.'"
    $ProjectGoal = Read-Host "  Goal"
    if ([string]::IsNullOrWhiteSpace($ProjectGoal)) {
        Write-Host ""
        Write-Host "Goal is required. Aborting." -ForegroundColor Yellow
        exit 1
    }
    Write-Host ""

    # WHEN: interval
    Write-Host "WHEN - cycle cadence" -ForegroundColor Yellow
    Write-Host "  Hours between sprint cycles. 0.5 = every 30 min (active dev),"
    Write-Host "  1 = hourly, 4 = ~6x/day, 12 = twice/day."
    $reply = Read-Host "  Interval hours [$IntervalHours]"
    if (-not [string]::IsNullOrWhiteSpace($reply)) {
        try { $IntervalHours = [double]$reply } catch {
            Write-Host "  (couldn't parse '$reply' as a number; keeping $IntervalHours)" -ForegroundColor DarkGray
        }
    }
    Write-Host ""

    # WHY: optional notes / context
    Write-Host "WHY - context the agent should know (optional)" -ForegroundColor Yellow
    Write-Host "  Constraints, deadlines, audience, why this matters. Press Enter to skip."
    $WhyNotes = Read-Host "  Notes"
    Write-Host ""

    # Confirm
    Write-Host "Summary:" -ForegroundColor Cyan
    Write-Host "  WHERE:    $Project"
    Write-Host "  WHO:      $Provider"
    Write-Host "  WHAT:     $ProjectGoal"
    Write-Host "  WHEN:     every ${IntervalHours}h"
    if (-not [string]::IsNullOrWhiteSpace($WhyNotes)) {
        Write-Host "  WHY:      $WhyNotes"
    }
    Write-Host ""
    $confirm = Read-Host "Start the poll loop? [Y/n]"
    if ($confirm -match '^(n|no)$') {
        Write-Host "Aborted." -ForegroundColor Yellow
        exit 1
    }

    # Create folder if needed and persist the WHY notes for the agent to read.
    if (-not (Test-Path $Project)) {
        New-Item -ItemType Directory -Path $Project -Force | Out-Null
    }
    if (-not [string]::IsNullOrWhiteSpace($WhyNotes)) {
        $autoDir = Join-Path $Project ".automatica"
        New-Item -ItemType Directory -Path $autoDir -Force | Out-Null
        $notesFile = Join-Path $autoDir "PROJECT_CONTEXT.md"
        @"
# Project Context (set during initial setup)

$WhyNotes
"@ | Set-Content -Path $notesFile -Encoding UTF8
        Write-Host "  Saved context notes -> $notesFile" -ForegroundColor DarkGray
    }

    # Recompute SleepSeconds in case interval changed
    $SleepSeconds = [int]($IntervalHours * 3600)
    Write-Host ""
    Write-Host "Starting poll loop." -ForegroundColor Green
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
