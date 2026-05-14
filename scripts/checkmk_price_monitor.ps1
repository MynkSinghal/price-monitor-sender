<#
.SYNOPSIS
    Check_MK local check for the Price Monitor Sender.

.DESCRIPTION
    Produces 5 Check_MK services from the sender's own log file:

        PriceSender_Task          Is the Windows Scheduled Task in Running state?
        PriceSender_LogFreshness  Has the log been written to recently?
        PriceSender_LastCycle     How long ago was the last CYCLE line? tx_ok status?
        PriceSender_TxFailures    How many consecutive tx_ok=False in a row?
        PriceSender_Completion    complete/total price groups from the last cycle.

    HOW IT FIRES
    ────────────
    Place this script in the Check_MK agent's CACHED local-check folder so it
    runs every 2 minutes (not every 60 s like normal local checks):

        C:\ProgramData\checkmk\agent\local\120\checkmk_price_monitor.ps1
                                           ↑
                                    cache age in seconds

    The agent runs the script, caches the output for 120 s, and the Check_MK
    server polls the cached result.  If the script hasn't run for > 120 s the
    server marks all services UNKNOWN automatically.

    WHAT IT READS FROM THE LOG
    ──────────────────────────
    Log files live at:   <ProjectRoot>\logs_<site>\sender-YYYY-MM-DD.log
    Plain-text format (one line per event):

      2026-05-03 15:25:37 | INFO    | MainThread         | price_sender.main     | CYCLE 7a3cc7b4 | site=USPROD | rows=187 | complete=7 | pending=180 | tx_ok=False | bytes=2585 | elapsed_ms=0
      2026-05-03 15:25:27 | INFO    | MainThread         | price_sender.state    | RECORDED CME_SPAN2A | completed=03/05/2026 15:25:22 | path=C:\Prices\...
      2026-05-03 15:25:27 | INFO    | MainThread         | price_sender.state    | IGNORED CME_SPAN2A (already recorded at 03/05/2026 15:25:22)
      2026-05-03 15:25:37 | ERROR   | MainThread         | price_sender.transport | Transmission failed (network) cycle=abc err=HTTPConnectionPool(...)

    The script tails the last 500 lines for efficiency — never reads the whole
    file even when it grows to tens of thousands of lines.

    LOG ROLLOVER HANDLING
    ─────────────────────
    The sender writes a new file each day at BUSINESS_DAY_START in BUSINESS_TIMEZONE
    (default 06:00 UK, DST/BST aware) — the business-day rollover moment.
    Between 00:00 and 06:00 UKT only yesterday's log may exist — the script falls
    back to yesterday's file automatically if today's has no CYCLE lines yet.

.NOTES
    Requires:  Check_MK Windows agent 2.x  +  PowerShell 5.1+
    Tested on: Windows Server 2016 / 2019 / 2022

    DEPLOYMENT CHECKLIST
    ────────────────────
    1. Edit the three variables in the CONFIG section below.
    2. Copy this file to:
           C:\ProgramData\checkmk\agent\local\120\checkmk_price_monitor.ps1
    3. Restart the Check_MK agent service:
           Restart-Service checkmkservice
    4. Force an agent run on the Check_MK server to discover the new services.
#>

# ═══════════════════════════════════════════════════════════════════
#  CONFIG — edit these three lines, leave everything else alone
# ═══════════════════════════════════════════════════════════════════

$ProjectRoot = "C:\price_monitor_sender"  # absolute path to project root
$SenderSite  = "UKPROD"                   # UKPROD or USPROD (drives log folder name)
$TaskName    = "PriceMonitorSender"       # name given to install_task_scheduler.ps1

# Alert thresholds (minutes)
# Use 3/5 for every_minute mode (sends every 60 s).
# Use 12/20 for business_schedule day-window mode (sends every 10 min in UK time).
$WarnGapMinutes = 3
$CritGapMinutes = 5

# Consecutive tx failures before alerting
$WarnConsecFail = 3
$CritConsecFail = 5

# Log file freshness — CRIT if file not touched for this many minutes
$LogStaleCritMinutes = 6

# Lines to read from tail of log (keeps the check fast on large files)
$TailLines = 500

# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════

function Out-CMK {
    param([int]$Status, [string]$Name, [string]$PerfData, [string]$Summary)
    # Status: 0=OK 1=WARN 2=CRIT 3=UNKNOWN
    Write-Output "$Status $Name $PerfData $Summary"
}

function Get-LogFile {
    param([string]$LogDir)
    # Try today first, fall back to yesterday (handles 00:00–06:00 UKT pre-rollover window)
    $today     = (Get-Date).ToString("yyyy-MM-dd")
    $yesterday = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd")
    $todayPath = Join-Path $LogDir "sender-$today.log"
    $yestPath  = Join-Path $LogDir "sender-$yesterday.log"

    if (Test-Path $todayPath) {
        # Today's file exists — but might have no CYCLE lines yet (just started up)
        $todayCycles = (Get-Content $todayPath -Tail 50 |
                        Where-Object { $_ -match '\| CYCLE ' })
        if ($todayCycles) { return $todayPath }
        # No cycles yet today — fall back to yesterday if it exists
        if (Test-Path $yestPath) { return $yestPath }
        return $todayPath   # return today anyway (no cycles is handled downstream)
    }
    if (Test-Path $yestPath) { return $yestPath }
    return $null
}

function Parse-CycleLines {
    param([string[]]$Lines)
    return $Lines | Where-Object { $_ -match '\| CYCLE ' }
}

function Get-LogTimestamp {
    param([string]$Line)
    # Line starts: "2026-05-03 15:25:37 | INFO ..."
    if ($Line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
        return [datetime]::ParseExact($Matches[1], "yyyy-MM-dd HH:mm:ss", $null)
    }
    return $null
}

# ═══════════════════════════════════════════════════════════════════
#  1. SCHEDULED TASK CHECK
# ═══════════════════════════════════════════════════════════════════

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if (-not $task) {
    Out-CMK 2 "PriceSender_Task" "-" "CRIT: Scheduled task '$TaskName' not found on this host"
} elseif ($task.State -eq "Running") {
    Out-CMK 0 "PriceSender_Task" "-" "OK: Task is Running"
} elseif ($task.State -eq "Ready") {
    # Ready = registered but not currently running (may have just finished a cycle)
    # Only alert if it stays Ready for too long — handled by LastCycle check instead
    Out-CMK 1 "PriceSender_Task" "-" "WARN: Task state is Ready (not Running — may have exited)"
} else {
    Out-CMK 2 "PriceSender_Task" "-" "CRIT: Task state is '$($task.State)' (expected Running)"
}

# ═══════════════════════════════════════════════════════════════════
#  LOCATE LOG FILE
# ═══════════════════════════════════════════════════════════════════

$LogDir  = Join-Path $ProjectRoot "logs_$($SenderSite.ToLower())"
# Fallback: some setups use generic "logs" folder
if (-not (Test-Path $LogDir)) { $LogDir = Join-Path $ProjectRoot "logs" }

$logFile = Get-LogFile -LogDir $LogDir

if (-not $logFile) {
    Out-CMK 2 "PriceSender_LogFreshness" "-" "CRIT: Log directory not found at $LogDir"
    Out-CMK 3 "PriceSender_LastCycle"    "-" "UNKNOWN: No log file — cannot determine last cycle"
    Out-CMK 3 "PriceSender_TxFailures"   "-" "UNKNOWN: No log file — cannot count failures"
    Out-CMK 3 "PriceSender_Completion"   "-" "UNKNOWN: No log file — cannot read completion"
    exit 0
}

# ═══════════════════════════════════════════════════════════════════
#  2. LOG FILE FRESHNESS
# ═══════════════════════════════════════════════════════════════════

$logModified = (Get-Item $logFile).LastWriteTime
$logAgeMin   = [math]::Round(((Get-Date) - $logModified).TotalMinutes, 1)
$perfFresh   = "log_age_minutes=$logAgeMin;$WarnGapMinutes;$LogStaleCritMinutes;0"

if ($logAgeMin -gt $LogStaleCritMinutes) {
    Out-CMK 2 "PriceSender_LogFreshness" $perfFresh `
        "CRIT: Log not written for ${logAgeMin}m — process may be hung or dead ($logFile)"
} elseif ($logAgeMin -gt $WarnGapMinutes) {
    Out-CMK 1 "PriceSender_LogFreshness" $perfFresh `
        "WARN: Log not written for ${logAgeMin}m ($logFile)"
} else {
    Out-CMK 0 "PriceSender_LogFreshness" $perfFresh `
        "OK: Log written ${logAgeMin}m ago ($([System.IO.Path]::GetFileName($logFile)))"
}

# ═══════════════════════════════════════════════════════════════════
#  READ TAIL OF LOG
# ═══════════════════════════════════════════════════════════════════

$tail        = Get-Content $logFile -Tail $TailLines -ErrorAction SilentlyContinue
$cycleLines  = Parse-CycleLines -Lines $tail

if (-not $cycleLines) {
    Out-CMK 1 "PriceSender_LastCycle"  "-" "WARN: No CYCLE lines in last $TailLines lines of $([System.IO.Path]::GetFileName($logFile))"
    Out-CMK 3 "PriceSender_TxFailures" "-" "UNKNOWN: No CYCLE lines to analyse"
    Out-CMK 3 "PriceSender_Completion" "-" "UNKNOWN: No CYCLE lines to analyse"
    exit 0
}

$lastCycle = $cycleLines | Select-Object -Last 1

# ═══════════════════════════════════════════════════════════════════
#  3. LAST CYCLE TIME + TX STATUS
# ═══════════════════════════════════════════════════════════════════

$lastCycleTime = Get-LogTimestamp -Line $lastCycle
$gapMin        = if ($lastCycleTime) {
    [math]::Round(((Get-Date) - $lastCycleTime).TotalMinutes, 1)
} else { 999 }

# Parse last cycle fields
$txOk      = if ($lastCycle -match 'tx_ok=True')  { "True" }  else { "False" }
$elapsed   = if ($lastCycle -match 'elapsed_ms=(\d+)') { $Matches[1] } else { "?" }
$cycleId   = if ($lastCycle -match 'CYCLE\s+([a-f0-9]+)') { $Matches[1].Substring(0,8) } else { "?" }

$perfCycle = "gap_minutes=$gapMin;$WarnGapMinutes;$CritGapMinutes;0"
$summary   = "gap=${gapMin}m  cycle=$cycleId  tx_ok=$txOk  elapsed=${elapsed}ms"

if ($gapMin -ge $CritGapMinutes) {
    Out-CMK 2 "PriceSender_LastCycle" $perfCycle "CRIT: No cycle for ${gapMin}m — $summary"
} elseif ($gapMin -ge $WarnGapMinutes) {
    Out-CMK 1 "PriceSender_LastCycle" $perfCycle "WARN: Last cycle ${gapMin}m ago — $summary"
} elseif ($txOk -eq "False") {
    # Cycle is recent but last one failed — WARN (TxFailures check will escalate if persistent)
    Out-CMK 1 "PriceSender_LastCycle" $perfCycle "WARN: Last cycle OK on time but tx_ok=False — $summary"
} else {
    Out-CMK 0 "PriceSender_LastCycle" $perfCycle "OK: $summary"
}

# ═══════════════════════════════════════════════════════════════════
#  4. CONSECUTIVE TRANSMISSION FAILURES
#     Walk backwards through cycle lines until we hit a tx_ok=True
#     or exhaust the window.
# ═══════════════════════════════════════════════════════════════════

$consecFail  = 0
$cycleArr    = @($cycleLines)   # ensure array
$lastSuccess = $null

for ($i = $cycleArr.Count - 1; $i -ge 0; $i--) {
    $line = $cycleArr[$i]
    if ($line -match 'tx_ok=True') {
        $lastSuccess = Get-LogTimestamp -Line $line
        break
    } elseif ($line -match 'tx_ok=False') {
        $consecFail++
    }
}

$sinceStr = if ($lastSuccess) {
    "$([math]::Round(((Get-Date)-$lastSuccess).TotalMinutes))m ago"
} else { "not in last $TailLines lines" }

$perfFail = "consec_failures=$consecFail;$WarnConsecFail;$CritConsecFail;0"

if ($consecFail -ge $CritConsecFail) {
    Out-CMK 2 "PriceSender_TxFailures" $perfFail `
        "CRIT: $consecFail consecutive failures — last success $sinceStr"
} elseif ($consecFail -ge $WarnConsecFail) {
    Out-CMK 1 "PriceSender_TxFailures" $perfFail `
        "WARN: $consecFail consecutive failures — last success $sinceStr"
} elseif ($consecFail -gt 0) {
    Out-CMK 1 "PriceSender_TxFailures" $perfFail `
        "WARN: $consecFail failure(s) since last success ($sinceStr)"
} else {
    Out-CMK 0 "PriceSender_TxFailures" $perfFail `
        "OK: No consecutive failures — last success $sinceStr"
}

# ═══════════════════════════════════════════════════════════════════
#  5. PRICE GROUP COMPLETION RATE (from last cycle)
# ═══════════════════════════════════════════════════════════════════

if ($lastCycle -match 'complete=(\d+).*?pending=(\d+).*?rows=(\d+)') {
    $complete = [int]$Matches[1]
    $pending  = [int]$Matches[2]
    $rows     = [int]$Matches[3]
    $pct      = if ($rows -gt 0) { [math]::Round(($complete / $rows) * 100, 1) } else { 0 }
    $perfComp = "complete=$complete;;;0;$rows pct=$pct;50;10;0;100"
    Out-CMK 0 "PriceSender_Completion" $perfComp `
        "OK: $complete/$rows groups complete ($pct%) — $pending pending"
} else {
    Out-CMK 3 "PriceSender_Completion" "-" `
        "UNKNOWN: Could not parse complete/pending from last cycle line"
}
