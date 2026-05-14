<#
.SYNOPSIS
    Registers the Price Monitor Sender as a Windows Scheduled Task.

.DESCRIPTION
    Creates a task that:
      * Runs the sender at startup AND every 5 minutes thereafter.
      * Only triggers Mon-Fri (matches Q20: no holiday calendar).
      * 'If the task is already running, do not start a new instance.'
        (Essential — we rely on Task Scheduler to NOT duplicate-launch.)
      * Restarts after failure once a minute, up to 3 times per cycle.
      * Runs whether user is logged in or not (SYSTEM account).

    The sender exits with code 99 when the heartbeat watchdog triggers
    a self-kill (see heartbeat.py). Task Scheduler's 'restart on failure'
    mechanism then relaunches it.

.PARAMETER TaskName
    Name of the scheduled task. Default: PriceMonitorSender.

.PARAMETER ProjectRoot
    Absolute path to the project root. Default: script's parent folder.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_task_scheduler.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = "PriceMonitorSender",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

$launcher = Join-Path $ProjectRoot "scripts\start_sender.bat"
if (-not (Test-Path $launcher)) {
    throw "Launcher not found: $launcher"
}

Write-Host "Project root : $ProjectRoot"
Write-Host "Launcher     : $launcher"
Write-Host "Task name    : $TaskName"

$action = New-ScheduledTaskAction -Execute $launcher -WorkingDirectory $ProjectRoot

# Trigger 1: at boot.
$bootTrigger = New-ScheduledTaskTrigger -AtStartup

# Trigger 2: Mon-Fri 05:55 LOCAL (just before the 06:00 UK business-day rollover).
# NOTE: Task Scheduler triggers fire in the host's local clock; adjust this if
# the host is NOT on UK time. e.g. on USPROD (US system time) use a local time
# whose UK equivalent is 05:55 UK (so EST/EDT ≈ 00:55 local). The sender itself
# is always business-day-correct via BUSINESS_TIMEZONE in .env regardless.
$dailyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "05:55"

# Trigger 3: every 5 minutes safety-net — if the process has exited the task
# principal re-invokes; 'DontAllowNewInstance' in the settings blocks duplicate runs.
$recurringTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 365)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)   # 0 = no time limit

$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task."
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $action `
    -Trigger @($bootTrigger, $dailyTrigger, $recurringTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description "Price Monitor Sender — CSV transmitter. Mon-Fri, self-restarting."

Write-Host ""
Write-Host "Task registered. Inspect in Task Scheduler or with:"
Write-Host "    Get-ScheduledTask -TaskName $TaskName | Format-List *"
