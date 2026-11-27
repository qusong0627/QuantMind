# QMT pipe bridge watchdog (pipe_redis_bridge only; does NOT touch TQRelay/file bridge)
# 1) process missing -> start it
# 2) process alive but heartbeat stale (>60s) -> restart (10 min cooldown)
# Called every minute by scheduled task QMTTools-PipeWatchdog.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads scripts without BOM
#       as ANSI/GBK; UTF-8 Chinese comments can swallow the next line.
$base = 'C:\new_tdx_mock\PYPlugins\bridge-windows\qmt-tools'
$log  = Join-Path $base 'logs\pipe_watchdog.log'
$cool = Join-Path $base 'logs\.pipe_watchdog_last_restart'
$pidFile = Join-Path $base 'data\pipe_bridge.pid'
$ts   = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$py   = 'C:\xmt\venv\Scripts\python.exe'

function Write-Log($msg) {
    Add-Content -Path $log -Value ("$ts $msg") -Encoding UTF8
}

# Exact process identity: prefer the bridge's own PID file, then the owner of
# the 17720 listener. Never kill every python.exe whose command line contains
# a substring - that is how a watchdog kills unrelated processes.
function Get-BridgeTargetPid {
    if (Test-Path $pidFile) {
        try {
            $doc = Get-Content -Path $pidFile -Raw | ConvertFrom-Json
            $candidate = [int]$doc.pid
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$candidate" -ErrorAction SilentlyContinue
            if ($proc -and ([string]$proc.CommandLine) -match 'pipe_redis_bridge') {
                return $candidate
            }
        } catch { }
    }
    try {
        $listener = Get-NetTCPConnection -LocalPort 17720 -State Listen -ErrorAction SilentlyContinue |
                    Select-Object -First 1
        if ($listener) {
            $candidate = [int]$listener.OwningProcess
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$candidate" -ErrorAction SilentlyContinue
            if ($proc -and ([string]$proc.CommandLine) -match 'pipe_redis_bridge') {
                return $candidate
            }
        }
    } catch { }
    return 0
}

$running = $false
# Prefer the 17720 listener: CIM CommandLine can be a false negative
# (permissions/timing), and a false negative goes down the "STOPPED"
# unconditional-restart branch, restarting a healthy bridge.
try {
    $listener = Get-NetTCPConnection -LocalPort 17720 -State Listen -ErrorAction SilentlyContinue
    if ($listener) { $running = $true }
} catch { }
if (-not $running) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
             Select-Object -ExpandProperty CommandLine
    foreach ($c in $procs) { if ($c -and $c -match 'pipe_redis_bridge') { $running = $true } }
}

$health = & $py (Join-Path $base 'qmt-external-bridge\bridge_health.py') 2>&1
$healthCode = $LASTEXITCODE
# 3 = heartbeat fresh but account snapshot stalled (QMT main thread not ticking / pipe stuck):
# alert only, do NOT restart - restarting during trading cuts in-flight RPC (incl. order reports).
if ($healthCode -eq 3) {
    Write-Log "DATA_STALE $health (no restart)"
    exit 0
}
# 4 = unresolved order intent (inflight/unknown) in Redis or processing list.
# Restarting the bridge would cut in-flight order reports; wait for human.
if ($healthCode -eq 4) {
    Write-Log "INFLIGHT $health (no restart)"
    exit 0
}
$healthy = ($healthCode -eq 0)

if ($running -and $healthy) { exit 0 }

# Heartbeat fresh but process detection missed it (CIM/port race at startup).
# Do NOT restart a live bridge just because Get-CimInstance cannot see it.
if (-not $running -and $healthy) {
    Write-Log "DETECT_MISS $health (no restart)"
    exit 0
}

if (-not $running) {
    Write-Log "STOPPED restarting (health=$health)"
} else {
    $last = 0
    if (Test-Path $cool) { try { $last = [int64](Get-Content $cool -Raw) } catch { $last = 0 } }
    $now = [int64][double]::Parse((Get-Date -UFormat %s))
    if ($now - $last -lt 600) {
        Write-Log "HEALTH $health COOLDOWN skip"
        exit 0
    }
    $target = Get-BridgeTargetPid
    if ($target -le 0) {
        Write-Log "NO_TARGET health=$health (no kill)"
        exit 1
    }
    Stop-Process -Id $target -Force
    Write-Log "KILLED pid=$target health=$health"
    Start-Sleep -Seconds 2
    Set-Content -Path $cool -Value $now
    Write-Log "HEALTH $health restarting"
}

$launcher = Join-Path $base 'start_pipe_bridge.ps1'
$cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Log "STARTED rc=$($r.ReturnValue) pid=$($r.ProcessId)"
