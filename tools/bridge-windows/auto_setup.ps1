# ============================================================
#  通达信量化交易桥 - 全自动一键部署
#  (新手友好: 双击运行, 自动完成所有配置)
#
#  自动完成:
#    0. 自动创建/确认 PYPlugins 共享 (供 Ubuntu 挂载同步)
#    1. 检测/安装 Python
#    2. 安装桥依赖 (aiohttp/pyyaml)
#    3. 给通达信 TQ Python 装 numpy/pandas
#    4. 部署 tdx_keepalive 常驻策略 (自动运行)
#    5. 设置环境变量 (BRIDGE_AUTH_TOKEN)
#    6. 放行防火墙 8550 + SMB 445
#    7. 启动桥
#    8. 引导启动通达信 + 保持 keepalive 运行
# ============================================================
param(
    [string]$Mode = "auto",
    # Ubuntu 侧挂载 SMB 共享用的账号/口令。**没有默认口令**——它曾经是写死的
    # 字面量，于是每台桥机的这个账号都是同一个口令，且它连同内网地址一起留在了
    # 公开仓的 git 历史里。留空则现场生成一个随机口令并打印一次，请当场记下：
    # Ubuntu 侧 mount 时用 `-o username=<SmbUser>,password=<该口令>`。
    [string]$SmbUser = "esxi",
    [string]$SmbPassword = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  通达信量化交易桥 - 全自动一键部署" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# ============ [0] 自动创建/确认 PYPlugins 共享 ============
Write-Host "[0/9] 检查 PYPlugins 共享 (供 Ubuntu 同步)..." -ForegroundColor Yellow
$PYPluginsDir = Join-Path $ScriptDir "PYPlugins"
if (-not (Test-Path $PYPluginsDir)) {
    $PYPluginsDir = $ScriptDir
}
# 脚本放在 PYPlugins\bridge-windows, 所以共享根 = 脚本的上级
$shareRoot = Split-Path $ScriptDir -Parent
Write-Host "      共享目录候选: $shareRoot"

# 检查共享是否已存在
$shareExists = $false
$existingShare = net share | Select-String "PYPlugins"
if ($existingShare) {
    $shareExists = $true
    Write-Host "      共享 PYPlugins 已存在" -ForegroundColor Green
}

# 创建共享 (需要管理员权限; 若失败提示)
if (-not $shareExists) {
    try {
        Write-Host "      创建共享 PYPlugins -> $shareRoot ..." -ForegroundColor Yellow
        net share PYPlugins="$shareRoot" /grant:everyone,READ /grant:esxi,FULL 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "      共享创建成功" -ForegroundColor Green
        } else {
            Write-Host "      [警告] net share 失败, 可能需要管理员权限" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "      [警告] 创建共享失败: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# 确保 SMB 挂载账号存在 (Ubuntu 挂载用的账号)
$esxiUser = net user $SmbUser 2>$null
if (-not $esxiUser) {
    if (-not $SmbPassword) {
        # 现场生成: 22 位, 去掉 look-alike 字符 (0/O、1/l/I), 便于人工抄录
        $alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        $SmbPassword = -join (1..22 | ForEach-Object { $alphabet[(Get-Random -Maximum $alphabet.Length)] })
        $generated = $true
    }
    Write-Host "      创建账号 $SmbUser ..." -ForegroundColor Yellow
    net user $SmbUser $SmbPassword /add 2>$null | Out-Null
    net localgroup Administrators $SmbUser /add 2>$null | Out-Null
    if ($generated) {
        Write-Host ""
        Write-Host "      ============================================================" -ForegroundColor Cyan
        Write-Host "      SMB 账号 $SmbUser 已创建, 口令 (只显示这一次, 请立即记下):" -ForegroundColor Cyan
        Write-Host "        $SmbPassword" -ForegroundColor White
        Write-Host "      Ubuntu 侧挂载: -o username=$SmbUser,password=<上面这串>" -ForegroundColor Cyan
        Write-Host "      ============================================================" -ForegroundColor Cyan
        Write-Host ""
    } else {
        Write-Host "      账号 $SmbUser 已创建 (口令来自 -SmbPassword 参数)" -ForegroundColor Green
    }
}
# 确保 SMB 1.0 兼容 + 防火墙放行 SMB
try {
    netsh advfirewall firewall add rule name="TDX-SMB445" dir=in action=allow protocol=TCP localport=445 2>$null | Out-Null
} catch {}

# ============ [1] 检测 Python ============
Write-Host "[1/9] 检测 Python..." -ForegroundColor Yellow
$PY = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $PY = "python" }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $PY = "py" }
if (-not $PY) {
    Write-Host "[错误] 未找到 Python, 请安装后重试" -ForegroundColor Red
    Write-Host "  https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "  安装时勾选 'Add Python to PATH'" -ForegroundColor Yellow
    Read-Host "按回车退出"
    exit 1
}
Write-Host "      Python: $PY"
& $PY --version

# ============ [2] 安装桥依赖 ============
Write-Host "[2/9] 安装桥依赖 (aiohttp/pyyaml)..." -ForegroundColor Yellow
& $PY -m pip install --quiet --upgrade pip
& $PY -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 依赖安装失败" -ForegroundColor Red
    Read-Host "按回车退出"; exit 1
}
Write-Host "      桥依赖 OK"

# ============ [3] 给通达信 TQ Python 装 numpy/pandas ============
Write-Host "[3/9] 配置通达信 TQ Python (numpy/pandas)..." -ForegroundColor Yellow
$tdxRoot = $null
foreach ($r in @("C:\new_tdx_mock", "C:\new_tdx", "C:\通达信金融终端64")) {
    if (Test-Path $r) { $tdxRoot = $r; break }
}
if (-not $tdxRoot) {
    $cand = Split-Path $ScriptDir -Parent
    if (Test-Path $cand) { $tdxRoot = $cand }
}
$tdxPythons = @()
if ($tdxRoot) {
    Write-Host "      通达信目录: $tdxRoot"
    $tdxPythons += Get-ChildItem -Path $tdxRoot -Filter "python.exe" -Recurse -Depth 3 -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
}
$tdxPythons = $tdxPythons | Select-Object -Unique
if ($tdxPythons.Count -eq 0) {
    Write-Host "      未找到捆绑 Python, 用系统 Python 装 numpy/pandas" -ForegroundColor Yellow
    & $PY -m pip install --quiet numpy pandas
} else {
    foreach ($tp in $tdxPythons) {
        Write-Host "      给 $tp 装 numpy/pandas" -ForegroundColor DarkCyan
        & $tp -m pip install --quiet numpy pandas 2>$null
        if ($LASTEXITCODE -eq 0) { Write-Host "        OK" -ForegroundColor Green }
        else { Write-Host "        FAIL, 手动: `"$tp`" -m pip install numpy pandas" -ForegroundColor Yellow }
    }
}

# ============ [4] 部署 keepalive 到通达信 user 目录 ============
Write-Host "[4/9] 部署 tdx_keepalive 常驻策略..." -ForegroundColor Yellow
$keepaliveSrc = Join-Path $ScriptDir "tdx_keepalive.py"
$tdxUserDir = $null
if ($tdxRoot) {
    $candUser = Join-Path $tdxRoot "PYPlugins\user"
    if (Test-Path $candUser) { $tdxUserDir = $candUser }
}
# 共享目录即 PYPlugins, user 在其下
if (-not $tdxUserDir) {
    $candUser2 = Join-Path $ScriptDir "user"
    if (Test-Path $candUser2) { $tdxUserDir = $candUser2 }
}
if ($tdxUserDir -and (Test-Path $keepaliveSrc)) {
    Copy-Item $keepaliveSrc (Join-Path $tdxUserDir "tdx_keepalive.py") -Force
    Write-Host "      已复制到: $tdxUserDir\tdx_keepalive.py" -ForegroundColor Green
    # 配置 py_strategy.cfg 自动运行
    $cfgPath = Join-Path $tdxUserDir "..\py_strategy.cfg"
    if (Test-Path $cfgPath) {
        $cfg = @"
[Strategy]
Name0=tdx_keepalive
Path0=tdx_keepalive.py
Note0=TQ常驻服务,保持17709监听
Last0=2026-08-12
New0=2026-08-12
Modify0=2026-08-12
Kind0=4
Lang0=0
Sys0=0
AutoRun0=1
Runs0=1
Num=1
[Writer]
path=
"@
        Copy-Item $cfgPath "$cfgPath.bak" -Force -ErrorAction SilentlyContinue
        Set-Content -Path $cfgPath -Value $cfg -Encoding UTF8
        Write-Host "      py_strategy.cfg 已配置 AutoRun=1" -ForegroundColor Green
    }
} else {
    Write-Host "      [警告] 未找到通达信 user 目录, 请手动把 tdx_keepalive.py 放到 PYPlugins\user\" -ForegroundColor Yellow
}

# ============ [5] 环境变量 ============
Write-Host "[5/9] 设置环境变量..." -ForegroundColor Yellow
if (-not $env:BRIDGE_AUTH_TOKEN) {
    $env:BRIDGE_AUTH_TOKEN = Read-Host "请输入 BRIDGE_AUTH_TOKEN (64位hex, 与Linux侧一致)"
}
if (-not $env:SHARED_DIR) { $env:SHARED_DIR = $ScriptDir }
Write-Host "      token: $($env:BRIDGE_AUTH_TOKEN.Substring(0, [Math]::Min(8, $env:BRIDGE_AUTH_TOKEN.Length)))..."

# ============ [6] 防火墙 ============
Write-Host "[6/9] 防火墙放行 8550..." -ForegroundColor Yellow
try {
    netsh advfirewall firewall delete rule name="TDXBridge-8550" 2>$null | Out-Null
    netsh advfirewall firewall add rule name="TDXBridge-8550" dir=in action=allow protocol=TCP localport=8550 2>$null | Out-Null
    Write-Host "      OK" -ForegroundColor Green
} catch {
    Write-Host "      [警告] 防火墙失败, 请手动放行 8550" -ForegroundColor Yellow
}

# ============ [7] 检查通达信 ============
Write-Host "[7/9] 检查通达信客户端..." -ForegroundColor Yellow
$tdxRunning = Get-Process TdxW -ErrorAction SilentlyContinue
if (-not $tdxRunning) {
    Write-Host "      [重要] 通达信未运行!" -ForegroundColor Red
    Write-Host "      请先启动通达信客户端并登录交易账号" -ForegroundColor Yellow
    Write-Host "      然后在 TQ 策略界面运行 tdx_keepalive 策略" -ForegroundColor Yellow
    Read-Host "按回车继续(桥会尝试连接)"
} else {
    Write-Host "      通达信运行中" -ForegroundColor Green
    Write-Host "      请确认: TQ 策略界面已运行 tdx_keepalive 策略 (保持运行状态)" -ForegroundColor Yellow
}

# ============ [8] 启动桥 ============
Write-Host ""
Write-Host "[9/9] 启动通达信交易桥..." -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  保持本窗口开启 = 桥运行中" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

& $PY -m main --mode $Mode --config config.yaml
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 桥异常退出" -ForegroundColor Red
    Read-Host "按回车退出"
}
