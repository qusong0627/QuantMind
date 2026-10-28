# QMT pipe bridge watchdog (pipe_redis_bridge only; does NOT touch TQRelay/file bridge)
# 1) process missing -> start it
# 2) process alive but heartbeat stale (>60s) -> restart (10 min cooldown)
# Called every minute by scheduled task QMTTools-PipeWatchdog.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads scripts without BOM
#       as ANSI/GBK; UTF-8 Chinese comments can swallow the next line.
$base = 'C:\new_tdx_mock\PYPlugins\bridge-windows\qmt-tools'
$log  = Join-Path $base 'logs\pipe_watchdog.log'
$cool = Join-Path $base 'logs\.pipe_watchdog_last_restart'
$ts   = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$py   = 'C:\xmt\venv\Scripts\python.exe'

function Write-Log($msg) {
    Add-Content -Path $log -Value ("$ts $msg") -Encoding UTF8
}

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
         Select-Object -ExpandProperty CommandLine
$running = $false
foreach ($c in $procs) { if ($c -and $c -match 'pipe_redis_bridge') { $running = $true } }

$health = & $py (Join-Path $base 'qmt-external-bridge\bridge_health.py') 2>&1
$healthCode = $LASTEXITCODE
# 3 = 心跳正常但账户快照停摆（QMT 主线程不 tick / 管道卡死）：
# 只告警不重启——交易时段重启伴生桥会打断在途 RPC（含下单回报）。
if ($healthCode -eq 3) {
    Write-Log "DATA_STALE $health (no restart)"
    exit 0
}
$healthy = ($healthCode -eq 0)

if ($running -and $healthy) { exit 0 }

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
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
        if ($_.CommandLine -match 'pipe_redis_bridge') { Stop-Process -Id $_.ProcessId -Force }
    }
    Start-Sleep -Seconds 2
    Set-Content -Path $cool -Value $now
    Write-Log "HEALTH $health restarting"
}

$cmd = "cmd.exe /c `"$base\start_pipe_bridge.cmd`""
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Log "STARTED rc=$($r.ReturnValue) pid=$($r.ProcessId)"
