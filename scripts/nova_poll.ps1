#Requires -Version 5.1
<#
.SYNOPSIS
    Nova3D autonomous poll loop — runs Automatica sprint cycles on a fixed interval.
    run_autonomous.py already recovers stuck tasks on each invocation.

.PARAMETER Project
    Project directory. Default: H:\Github\Pong

.PARAMETER Provider
    AI provider name. Default: Phi4 (local Ollama). Override with any provider
    name configured in ~/.automatica/automatica.db. Other reasonable
    Ollama-driven choices: Mistral, Llama3.1, DeepseekCoder.

.PARAMETER IntervalHours
    Hours between cycles. Default: 1

.PARAMETER MaxMinutesPerCycle
    Max sprint runtime per cycle in minutes. Default: 110

.EXAMPLE
    .\nova_poll.ps1
    .\nova_poll.ps1 -IntervalHours 2 -Provider Phi4
    .\nova_poll.ps1 -Project H:\Github\Nova3D -Provider Claude-Sonnet
#>
[CmdletBinding()]
param(
    [string]$Project            = "H:\Github\Pong",
    [string]$Provider           = "Phi4",
    [double]$IntervalHours      = 1.0,
    [int]   $MaxMinutesPerCycle = 110
)

$AutomaticaDir = "H:\Github\Automatica"
$LogFile       = Join-Path $AutomaticaDir "logs\nova_poll.log"
$SleepSeconds  = [int]($IntervalHours * 3600)

New-Item -ItemType Directory -Path (Split-Path $LogFile) -Force | Out-Null

function Write-Log([string]$msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

$cycle = 0
Write-Log "Automatica poll loop started — project: $Project, provider: $Provider, interval: ${IntervalHours}h"

while ($true) {
    $cycle++
    Write-Log "=== Cycle #$cycle ==="

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

    Write-Log "=== Cycle #$cycle complete (exit $($proc.ExitCode)) ==="

    $wake = (Get-Date).AddSeconds($SleepSeconds)
    Write-Log "Sleeping until $($wake.ToString('yyyy-MM-dd HH:mm:ss')) (${IntervalHours}h)..."
    Start-Sleep -Seconds $SleepSeconds
}
