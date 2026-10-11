#Requires -Version 5.1
<#
.SYNOPSIS
    Single-shot AIxYad sprint runner — invoked by the Windows Scheduled Task
    "AIxYad_Nova3D_Poll". After the cycle completes, self-reschedules its
    next run with a delay drawn from a Gaussian (normal) distribution
    parameterised by mean and standard deviation, then clamped to [Min, Max] hours.

.DESCRIPTION
    Distribution semantics:
        MeanHours = 2.0, StdDevHours = 0.0 → fixed 2 h cadence (no jitter)
        MeanHours = 2.0, StdDevHours = 1.0 → ~68% of cycles fall in [1, 3] h, ~95% in [0, 4] h
        Hard clamp to [MinHours, MaxHours] avoids pathological draws.

.PARAMETER Project
.PARAMETER Provider
.PARAMETER MaxMinutesPerCycle  Per-cycle wall-clock budget for the sprint runner.
.PARAMETER MeanHours           Distribution mean. Default 2.0.
.PARAMETER StdDevHours         Distribution standard deviation. Default 1.0.
.PARAMETER MinHours            Lower clamp. Default 0.5.
.PARAMETER MaxHours            Upper clamp. Default 12.0.
.PARAMETER ConfigFile          Optional JSON overriding any of the above.
                               Auto-loads from H:\Github\Automatica\nova_poll_config.json
                               if no -ConfigFile is given and the file exists.

.EXAMPLE
    .\nova_poll_once.ps1 -MeanHours 2 -StdDevHours 1   # ~2h ± 1h Gaussian
    .\nova_poll_once.ps1 -MeanHours 4 -StdDevHours 0   # fixed 4h cadence
#>
[CmdletBinding()]
param(
    [string]$Project            = "H:\Github\Nova3D",
    [string[]]$Projects         = @(),                # multi-project mode (overrides $Project if non-empty)
    [string]$Provider           = "Claude-Opus",
    [int]   $MaxMinutesPerCycle = 110,
    [int]   $StartupWaitSeconds = 60,                 # grace period before any work starts
    [double]$MeanHours          = 2.0,
    [double]$StdDevHours        = 1.0,
    [double]$MinHours           = 0.5,
    [double]$MaxHours           = 12.0,
    [string]$ConfigFile         = ""
)

$AixyadDir = "H:\Github\Automatica"
$LogFile       = Join-Path $AixyadDir "logs\nova_poll.log"
$TaskName      = "AIxYad_Nova3D_Poll"
$DefaultConfig = Join-Path $AixyadDir "nova_poll_config.json"

New-Item -ItemType Directory -Path (Split-Path $LogFile) -Force | Out-Null

function Write-Log([string]$msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    # AppendAllText uses FileShare.ReadWrite so 'tail -F' readers don't block us.
    # Retry up to 3 times on transient I/O contention.
    for ($i = 0; $i -lt 3; $i++) {
        try {
            [System.IO.File]::AppendAllText($LogFile, $line + "`r`n",
                                            [System.Text.UTF8Encoding]::new($false))
            return
        } catch {
            Start-Sleep -Milliseconds (50 * ($i + 1))
        }
    }
}

# ── Config-file override ─────────────────────────────────────────────────────
if (-not $ConfigFile -and (Test-Path $DefaultConfig)) { $ConfigFile = $DefaultConfig }
if ($ConfigFile -and (Test-Path $ConfigFile)) {
    try {
        $cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
        $names = $cfg.PSObject.Properties.Name
        if ($names -contains 'Project')            { $Project            = $cfg.Project }
        if ($names -contains 'Projects')           { $Projects           = @($cfg.Projects) }
        if ($names -contains 'Provider')           { $Provider           = $cfg.Provider }
        if ($names -contains 'MaxMinutesPerCycle') { $MaxMinutesPerCycle = [int]$cfg.MaxMinutesPerCycle }
        if ($names -contains 'StartupWaitSeconds') { $StartupWaitSeconds = [int]$cfg.StartupWaitSeconds }
        if ($names -contains 'MeanHours')          { $MeanHours          = [double]$cfg.MeanHours }
        if ($names -contains 'StdDevHours')        { $StdDevHours        = [double]$cfg.StdDevHours }
        if ($names -contains 'MinHours')           { $MinHours           = [double]$cfg.MinHours }
        if ($names -contains 'MaxHours')           { $MaxHours           = [double]$cfg.MaxHours }
        Write-Log "Loaded config overrides from $ConfigFile"
    } catch {
        Write-Log "WARN: Failed to parse $ConfigFile : $_"
    }
}

# Validate
if ($MeanHours -lt 0)   { throw "MeanHours must be >= 0" }
if ($StdDevHours -lt 0) { throw "StdDevHours must be >= 0" }
if ($MinHours -lt 0)    { throw "MinHours must be >= 0" }
if ($MaxHours -le $MinHours) { throw "MaxHours must be > MinHours" }

# Box-Muller → Gaussian sample
function Get-NextIntervalHours {
    if ($StdDevHours -eq 0) {
        return [Math]::Max($MinHours, [Math]::Min($MaxHours, $MeanHours))
    }
    # Box-Muller transform: two uniforms → one standard normal
    $u1 = Get-Random -Minimum 1e-9 -Maximum 1.0
    $u2 = Get-Random -Minimum 0.0  -Maximum 1.0
    $z  = [Math]::Sqrt(-2.0 * [Math]::Log($u1)) * [Math]::Cos(2.0 * [Math]::PI * $u2)
    $hr = $MeanHours + $StdDevHours * $z
    if ($hr -lt $MinHours) { $hr = $MinHours }
    if ($hr -gt $MaxHours) { $hr = $MaxHours }
    return $hr
}

function Reschedule-NextRun([double]$hoursFromNow) {
    $nextRun = (Get-Date).AddSeconds([int]($hoursFromNow * 3600))
    $trigger = New-ScheduledTaskTrigger -Once -At $nextRun
    Set-ScheduledTask -TaskName $TaskName -Trigger $trigger | Out-Null
    Write-Log ("Next run scheduled: {0:yyyy-MM-dd HH:mm:ss} (+{1:N2}h, Gaussian μ={2}h σ={3}h, clamped [{4}, {5}]h)" -f `
        $nextRun, $hoursFromNow, $MeanHours, $StdDevHours, $MinHours, $MaxHours)
}

# ── Run the cycle ────────────────────────────────────────────────────────────
$projectList = if ($Projects.Count -gt 0) { $Projects } else { @($Project) }
$perProject  = [Math]::Max(10, [int]($MaxMinutesPerCycle / $projectList.Count))

Write-Log ("=== Cycle start | projects=[{0}] provider={1} max={2}min ({3}min/project) | dist μ={4}h σ={5}h | startup-wait={6}s ===" -f `
    ($projectList -join ', '), $Provider, $MaxMinutesPerCycle, $perProject, $MeanHours, $StdDevHours, $StartupWaitSeconds)

if ($StartupWaitSeconds -gt 0) {
    Write-Log ("Settling for {0}s before first project (cancel via Ctrl-C or Disable-ScheduledTask -TaskName {1})" -f $StartupWaitSeconds, $TaskName)
    Start-Sleep -Seconds $StartupWaitSeconds
}

$lastExit = 0
foreach ($proj in $projectList) {
    Write-Log "--- project: $proj ---"
    $proc = Start-Process `
        -FilePath "python" `
        -ArgumentList @(
            (Join-Path $AixyadDir "run_autonomous.py"),
            "--Project", $proj,
            "--provider", $Provider,
            "--max-minutes", $perProject,
            "--max-sprints", 9999
        ) `
        -WorkingDirectory $AixyadDir `
        -NoNewWindow -Wait -PassThru
    Write-Log "--- project $proj done | exit=$($proc.ExitCode) ---"
    $lastExit = $proc.ExitCode
}

Write-Log "=== Cycle end ==="

# ── Schedule next firing ─────────────────────────────────────────────────────
try {
    $nextHours = Get-NextIntervalHours
    Reschedule-NextRun -hoursFromNow $nextHours
} catch {
    Write-Log "ERROR rescheduling next run: $_"
}

exit $lastExit
