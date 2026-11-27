# Robust QMT pipe bridge launcher (PowerShell 5.1 compatible, ASCII only).
# - Serializes concurrent launchers with a global mutex.
# - Kills ONLY exact bridge processes (PID file / 17720/17719 listener / cmdline).
# - Refuses to kill an unrelated process that happens to hold the port.
# - Starts detached via Win32_Process.Create (survives SSH/session close).
# - Waits for 17720 to listen; exits non-zero if it does not come up.
# - Default is SAFE START: if 17720 is already listening and health is 0/3/4,
#   do nothing. Pass -Force to force a restart.
param([switch]$Force)
$ErrorActionPreference = 'Stop'
$base = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $base 'logs\start_pipe_bridge.log'
$pidFile = Join-Path $base 'data\pipe_bridge.pid'
$rawCmd = Join-Path $base 'start_pipe_bridge_raw.cmd'
$script = Join-Path $base 'qmt-external-bridge\pipe_redis_bridge.py'
$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

function Write-Log($msg) {
    Add-Content -Path $log -Value ("$ts $msg") -Encoding UTF8
}

function Get-CmdLine([int]$procId) {
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        if ($proc) { return [string]$proc.CommandLine }
    } catch { }
    return ''
}

function Get-PidFileValue {
    if (-not (Test-Path $pidFile)) { return 0 }
    try {
        $doc = Get-Content -Path $pidFile -Raw | ConvertFrom-Json
        return [int]$doc.pid
    } catch { return 0 }
}

$mutex = New-Object System.Threading.Mutex($false, 'Global\QMTToolsPipeBridgeLauncher')
$locked = $false
try {
    try {
        $locked = $mutex.WaitOne(30000)
    } catch [System.Threading.AbandonedMutexException] {
        $locked = $true
    }
    if (-not $locked) {
        Write-Log 'ABORT another launcher is running'
        exit 1
    }

    Write-Log 'BEGIN'
    $targets = @{}
    $pidFilePid = Get-PidFileValue

    # 1) PID file: trust only if the process command line is the bridge.
    if ($pidFilePid -gt 0) {
        $cmd = Get-CmdLine $pidFilePid
        if ($cmd -match 'pipe_redis_bridge') {
            $targets[$pidFilePid] = 'pidfile'
        }
    }

    # 2) Port owners: 17720 (HTTP) and 17719 (single-instance lock).
    $conflict = ''
    foreach ($port in @(17719, 17720)) {
        try {
            $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        } catch { $listeners = $null }
        if (-not $listeners) { continue }
        foreach ($l in $listeners) {
            $owner = [int]$l.OwningProcess
            if ($owner -le 0) { continue }
            $cmd = Get-CmdLine $owner
            $trusted = ($cmd -match 'pipe_redis_bridge') -or ($owner -eq $pidFilePid)
            if ($trusted) {
                $targets[$owner] = "port$port"
            } else {
                $conflict = "port $port held by pid $owner (not verifiably pipe_redis_bridge): $cmd"
            }
        }
    }

    # 3) Command-line scan (works when running as SYSTEM; may be empty for non-elevated).
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
        foreach ($p in $procs) {
            if ([string]$p.CommandLine -match 'pipe_redis_bridge') {
                $targets[[int]$p.ProcessId] = 'cmdline'
            }
        }
    } catch { }

    if ($conflict -ne '') {
        Write-Log ("CONFLICT " + $conflict)
        exit 2
    }

    # Safe start: a healthy running bridge is left alone unless -Force.
    # Health codes 3 (data stale) and 4 (inflight order intent) must not bypass.
    if (-not $Force) {
        $listener = Get-NetTCPConnection -LocalPort 17720 -State Listen -ErrorAction SilentlyContinue |
                    Select-Object -First 1
        if ($listener) {
            $healthPy = 'C:\xmt\venv\Scripts\python.exe'
            $healthScript = Join-Path $base 'qmt-external-bridge\bridge_health.py'
            $healthCode = 1
            $healthOut = ''
            if ((Test-Path $healthPy) -and (Test-Path $healthScript)) {
                try {
                    $healthOut = & $healthPy $healthScript 2>&1
                    $healthCode = $LASTEXITCODE
                } catch {
                    $healthCode = 1
                    $healthOut = $_.Exception.Message
                }
            }
            if (($healthCode -eq 0) -or ($healthCode -eq 3) -or ($healthCode -eq 4)) {
                Write-Log ("NOOP already running pid=" + $listener.OwningProcess + " health=" + $healthCode + " " + $healthOut)
                exit 0
            }
            Write-Log ("HEALTH " + $healthCode + " " + $healthOut + " -> restart")
        }
    }

    # 4) Stop exact targets and wait for ports to free.
    foreach ($targetPid in @($targets.Keys)) {
        $proc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Log ("KILL pid=$targetPid reason=" + $targets[$targetPid] + " path=" + $proc.Path)
            Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
        }
    }
    for ($i = 0; $i -lt 10; $i++) {
        $busy = Get-NetTCPConnection -LocalPort 17720,17719 -State Listen -ErrorAction SilentlyContinue
        if (-not $busy) { break }
        Start-Sleep -Seconds 1
    }
    $busy = Get-NetTCPConnection -LocalPort 17720,17719 -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        $owners = ($busy | ForEach-Object { [string]$_.OwningProcess }) -join ','
        Write-Log ("ABORT ports still listening after kill owners=$owners")
        exit 3
    }

    # 5) Remove stale PID file (only when its PID is gone).
    if ($pidFilePid -gt 0 -and -not (Get-Process -Id $pidFilePid -ErrorAction SilentlyContinue)) {
        Remove-Item -Path $pidFile -Force -ErrorAction SilentlyContinue
    }

    # 6) Start detached via CIM (not tied to this PowerShell/SSH session).
    if (-not (Test-Path $rawCmd)) {
        Write-Log ("ABORT missing " + $rawCmd)
        exit 4
    }
    $cmdLine = 'cmd.exe /c "' + $rawCmd + '"'
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine }
    Write-Log ("START rc=" + $r.ReturnValue + " pid=" + $r.ProcessId)

    # 7) Verify 17720 comes up.
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        $listeners = Get-NetTCPConnection -LocalPort 17720 -State Listen -ErrorAction SilentlyContinue
        if ($listeners) { $ok = $true; break }
    }
    if (-not $ok) {
        Write-Log 'ABORT bridge did not listen on 17720 within 30s'
        exit 5
    }
    $owner = (Get-NetTCPConnection -LocalPort 17720 -State Listen | Select-Object -First 1).OwningProcess
    Write-Log ("OK pid=" + $owner)
    exit 0
} catch {
    Write-Log ("ERROR " + $_.Exception.Message)
    exit 1
} finally {
    if ($locked) {
        try { $mutex.ReleaseMutex() } catch { }
    }
    try { $mutex.Dispose() } catch { }
}
