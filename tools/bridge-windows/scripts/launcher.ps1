# ============================================================
#  QuantMind 交易台一键启动器
#
#  双击 启动交易台.cmd，或：
#      .\scripts\launcher.ps1              # 缺谁起谁，已在跑不动
#      .\scripts\launcher.ps1 -Restart     # 强制重启桥 + 采集器
#      .\scripts\launcher.ps1 -RestartCollector   # 盘中只重启采集器（不动桥）
#      .\scripts\launcher.ps1 -HotSet "600519.SH,000001.SZ"
#      .\scripts\launcher.ps1 -NoCollector -NoPause
#
#  顺序：TQ 17709（需 TdxW 已登录）→ 桥 8550 → 看门狗 → 采集器 8560
#  说明：TDX 客户端与 TQ 策略必须在通达信里手动登录/运行，本脚本只探测 17709。
# ============================================================
param(
    [switch]$Restart,
    [switch]$RestartCollector,
    [switch]$NoCollector,
    [switch]$NoWatchdog,
    [switch]$NoThs,
    [string]$HotSet = "",
    [switch]$NoPause,
    [switch]$NoUi
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venv = Join-Path $root ".venv\Scripts\python.exe"
$py = if (Test-Path $venv) { $venv } elseif (Test-Path "C:\Program Files\Python312\python.exe") { "C:\Program Files\Python312\python.exe" } else { (Get-Command python).Source }
if (-not $py -or $py -like "*WindowsApps*") {
    Write-Host "[launcher] 找不到可用的 Python（拒绝 WindowsApps 别名）" -ForegroundColor Red

# ---------- 交易台 UI（带 token 打开） ----------
# token 放在 URL fragment（#token=...）：fragment 不会发给服务器、不进访问日志，
# 页面加载后立即存入 localStorage 并从地址栏抹掉。启动器日志不打印 token。
function Get-BridgeToken {
    if ($env:BRIDGE_AUTH_TOKEN) { return $env:BRIDGE_AUTH_TOKEN.Trim() }
    $cfgPath = Join-Path $root "config.yaml"
    if (Test-Path $cfgPath) {
        $cfgText = Get-Content $cfgPath -Raw -ErrorAction SilentlyContinue
        if ($cfgText -match '(?m)^\s*token:\s*["'']?([0-9a-fA-F]{64})') {
            return $Matches[1]
        }
    }
    return ""
}

if (-not $NoUi) {
    $uiToken = Get-BridgeToken
    $uiUrl = if ($uiToken) {
        "http://127.0.0.1:8550/ui#token=" + $uiToken
    } else {
        "http://127.0.0.1:8550/ui"
    }
    try {
        Start-Process $uiUrl
        if ($uiToken) { Write-Host "  交易台已打开（token 已注入，无需手填）" -ForegroundColor Green }
        else { Write-Host "  交易台已打开（未找到 token，需在页面右上角填写）" -ForegroundColor Yellow }
    } catch {
        Write-Host ("  交易台打开失败: " + $_.Exception.Message) -ForegroundColor Yellow
    }
}

if (-not $NoPause) { Read-Host "按回车关闭" | Out-Null }
    exit 1
}

function Test-Port([int]$Port) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$c
}
function Get-Json([string]$Url) {
    try { return Invoke-RestMethod -Uri $Url -TimeoutSec 4 } catch { return $null }
}
function Stop-Port([int]$Port) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($x in $c) {
        Stop-Process -Id $x.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "  QuantMind · 通达信交易台启动器" -ForegroundColor Cyan
Write-Host "  $root" -ForegroundColor DarkGray
Write-Host ""

# ---------- 1. TQ 17709 ----------
$tqOk = Test-Port 17709
if ($tqOk) { Write-Host "[1/5] TQ 17709       OK" -ForegroundColor Green }
else { Write-Host "[1/5] TQ 17709       未监听：请先启动 TdxW 并登录，再运行 TQ 常驻策略" -ForegroundColor Yellow }

# ---------- 2. 桥 8550 ----------
$bridge = if (-not $Restart) { Get-Json "http://127.0.0.1:8550/api/v1/health" } else { $null }
if ($Restart -or -not $bridge) {
    if ($Restart -or (Test-Port 8550)) {
        Write-Host "[2/5] 桥 8550        停止旧进程..." -ForegroundColor DarkGray
        # 先停看门狗：否则它会把刚杀掉的桥/采集器抢着拉起来，形成双实例
        Stop-ScheduledTask -TaskName "TDXBridgeWatchdog" -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        Stop-Port 8550
        Start-Sleep -Seconds 2
    }
    Write-Host "[2/5] 桥 8550        启动中..." -ForegroundColor DarkGray
    # 防 token 静默轮换：config 依赖 ${BRIDGE_AUTH_TOKEN} 而本会话又没注入时，
    # main.py 会自动生成新 token 并写回 config，平台/采集器立刻全员 401
    # （2026-10-23 实盘事故）。宁可拒绝启动，也不静默换 token。
    if (-not $env:BRIDGE_AUTH_TOKEN) {
        $cfgText = Get-Content (Join-Path $root "config.yaml") -Raw -ErrorAction SilentlyContinue
        if ($cfgText -match '(?m)^\s*token:\s*["'']?\$\{BRIDGE_AUTH_TOKEN\}') {
            Write-Host "[2/5] 桥 8550        拒绝启动：config.yaml 的 auth.token 依赖 BRIDGE_AUTH_TOKEN，本会话未设置" -ForegroundColor Red
            Write-Host "      先执行:  `$env:BRIDGE_AUTH_TOKEN = '<与 Linux .env 相同的 64 位 hex>'" -ForegroundColor Yellow
            Write-Host "      否则桥会生成新 token 并写回 config，平台与采集器会全部 401。" -ForegroundColor Yellow
            if (-not $NoPause) { Read-Host "按回车关闭" | Out-Null }
            exit 1
        }
    }
    Start-Process -FilePath $py `
        -ArgumentList "-m main --mode auto --config config.yaml" `
        -WorkingDirectory $root -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 8
    $bridge = Get-Json "http://127.0.0.1:8550/api/v1/health"
}
if ($bridge -and $bridge.status -eq "ok") {
    Write-Host ("[2/5] 桥 8550        OK  tdx_connected={0}" -f $bridge.tdx_connected) -ForegroundColor Green
} else {
    Write-Host "[2/5] 桥 8550        启动失败（看 data\logs\bridge.log）" -ForegroundColor Red
}

# ---------- 3. 看门狗 ----------
if (-not $NoWatchdog) {
    $wd = Get-ScheduledTask -TaskName "TDXBridgeWatchdog" -ErrorAction SilentlyContinue
    if ($wd) {
        if ($wd.State -ne "Running") { Start-ScheduledTask -TaskName "TDXBridgeWatchdog" }
        Write-Host "[3/5] 看门狗         OK（计划任务）" -ForegroundColor Green
    } else {
        Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\watchdog.ps1`"" -WorkingDirectory $root -WindowStyle Hidden | Out-Null
        Write-Host "[3/5] 看门狗         已临时启动；建议跑 scripts\fix_watchdog.ps1 注册" -ForegroundColor Yellow
    }
} else {
    Write-Host "[3/5] 看门狗         跳过（-NoWatchdog）" -ForegroundColor DarkGray
}

# ---------- 4. 采集器 8560 ----------
if (-not $NoCollector) {
    # -HotSet 是显式意图：即使采集器在跑也要按新热集重启（只重启采集器，不动桥）
    $colRestart = $Restart -or $RestartCollector -or [bool]$HotSet
    $col = if (-not $colRestart) { Get-Json "http://127.0.0.1:8560/health" } else { $null }
    $colTask = Get-ScheduledTask -TaskName "TdxCollector" -ErrorAction SilentlyContinue
    if ($colRestart -or -not $col) {
        # 关键：必须同时停计划任务。只杀端口进程时任务仍算 Running，
        # 之后 Start-ScheduledTask 会被 MultipleInstances=IgnoreNew 静默忽略，
        # 采集器就再也起不来（2026-10-23 实报）。
        if ($colTask) { Stop-ScheduledTask -TaskName "TdxCollector" -ErrorAction SilentlyContinue }
        if ($colRestart -or (Test-Port 8560)) {
            Write-Host "[4/5] 采集器 8560    停止旧进程..." -ForegroundColor DarkGray
            Stop-Port 8560
            Start-Sleep -Seconds 2
        }
        if ($colTask -and -not $HotSet) {
            Start-ScheduledTask -TaskName "TdxCollector"
        } else {
            $colArgLine = "scripts\tdx_collector.py --config config.yaml"
            if ($HotSet) { $colArgLine += " --hot-set `"$HotSet`"" }
            Start-Process -FilePath $py -ArgumentList $colArgLine -WorkingDirectory $root -WindowStyle Hidden | Out-Null
        }
        Start-Sleep -Seconds 5
        $col = Get-Json "http://127.0.0.1:8560/health"
    }
    if ($col -and $col.success -and $col.data.status -eq "ok") {
        Write-Host ("[4/5] 采集器 8560    OK  universe={0}" -f $col.data.universe_size) -ForegroundColor Green
    } else {
        Write-Host "[4/5] 采集器 8560    启动失败（看 data\logs\collector.log）" -ForegroundColor Red
    }
} else {
    Write-Host "[4/5] 采集器         跳过（-NoCollector）" -ForegroundColor DarkGray
}

# ---------- 5. 同花顺 worker 8765（可选） ----------
if (-not $NoThs) {
    $thsTask = Get-ScheduledTask -TaskName "TdxThsWorker" -ErrorAction SilentlyContinue
    $thsRestart = $Restart
    $ths = if (-not $thsRestart) { Get-Json "http://127.0.0.1:8765/api/status" } else { $null }
    if ($thsRestart -or -not $ths) {
        if ($thsRestart -or (Test-Port 8765)) {
            Write-Host "[5/5] 同花顺       停止旧进程..." -ForegroundColor DarkGray
            if ($thsTask) { Stop-ScheduledTask -TaskName "TdxThsWorker" -ErrorAction SilentlyContinue }
            Stop-Port 8765
            Start-Sleep -Seconds 2
        }
        Write-Host "[5/5] 同花顺       启动中..." -ForegroundColor DarkGray
        if ($thsTask) {
            Start-ScheduledTask -TaskName "TdxThsWorker"
        } else {
            Start-Process -FilePath $py `
                -ArgumentList "-u `"$root\scripts\ths_worker.py`" --host 127.0.0.1 --port 8765" `
                -WorkingDirectory $root -WindowStyle Hidden | Out-Null
        }
        Start-Sleep -Seconds 8
        $ths = Get-Json "http://127.0.0.1:8765/api/status"
    }
    if ($ths) {
        Write-Host ("[5/5] 同花顺       OK  connected={0} credentials={1}" -f `
            $ths.connected, $ths.credentials) -ForegroundColor Green
    } else {
        $thsVendor = Join-Path $root "vendor\ths"
        if (-not (Test-Path $thsVendor)) {
            Write-Host "[5/5] 同花顺       未安装（可选）：先运行 scripts\install.ps1 -WithThs" -ForegroundColor Yellow
        } else {
            Write-Host "[5/5] 同花顺       未启动（查 data\ths.env 与 data\logs）" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "[5/5] 同花顺       跳过（-NoThs）" -ForegroundColor DarkGray
}

# ---------- 汇总 ----------
Write-Host ""
Write-Host "  ------------------------- 状态 -------------------------" -ForegroundColor DarkGray
$h = Get-Json "http://127.0.0.1:8550/api/v1/health"
$c = Get-Json "http://127.0.0.1:8560/health"
$cdata = if ($c) { $c.data } else { $null }
$lim = if ($h) { $h.trade_limits.orders } else { $null }
$tqText = if ($tqOk) { "监听" } else { "未监听" }
$bridgeText = if ($h) { "ok / tdx=" + $h.tdx_connected } else { "不可达" }
$colText = if ($cdata) { $cdata.status + " / 宇宙 " + $cdata.universe_size } else { "不可达" }
$limText = if ($lim) { "{0}/{1}（本秒 {2}/{3}）" -f $lim.day_count, $lim.day_limit, $lim.last_sec_count, $lim.sec_limit } else { "--" }
Write-Host ("  TQ 17709    {0}" -f $tqText)
Write-Host ("  桥 8550     {0}" -f $bridgeText)
Write-Host ("  采集器 8560 {0}" -f $colText)
$thsSum = Get-Json "http://127.0.0.1:8765/api/status"
$thsText = if ($thsSum) { "ok / connected=" + $thsSum.connected } else { "未启动" }
Write-Host ("  同花顺 8765 {0}" -f $thsText)
Write-Host ("  硬上限      {0}" -f $limText)
Write-Host "  --------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""

if (-not $NoPause) { Read-Host "按回车关闭" | Out-Null }
