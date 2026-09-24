# ============================================================
# QuantMind Portable - one-click installer / preflight (Windows x64)
#
#   powershell -ExecutionPolicy Bypass -File install.ps1           check + install + start
#   powershell -ExecutionPolicy Bypass -File install.ps1 -NoStart  check + install only
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Shortcut also create a Desktop shortcut
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Force    continue past blocking checks
#
# ASCII only, on purpose: Windows PowerShell 5.1 reads a BOM-less .ps1 using the
# ANSI code page, so non-ASCII text here would print as garbage on a zh-CN
# system (start.bat is ASCII+CRLF for the same reason). The Chinese docs live in
# README.md next to this file.
#
# What it does
#   1. preflight: 64-bit OS, pack completeness, local (non-SMB) disk, path shape,
#      free space, the ports the pack needs
#   2. first run: write pack.env with a random local DB password (only while
#      pgdata\ does not exist - the password is fixed at initdb time)
#   3. hand over to start.bat, which does the actual first start
#
# Exit codes: 0 ok / 1 a blocking check failed / 2 the pack itself is incomplete
# ============================================================
[CmdletBinding()]
param(
    [switch]$NoStart,
    [switch]$Force,
    [switch]$Shortcut
)

$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }

function Say([string]$m)   { Write-Host "[install] $m" }
function Good([string]$m)  { Write-Host "[ok]      $m" -ForegroundColor Green }
function Warn([string]$m)  { Write-Host "[warn]    $m" -ForegroundColor Yellow }
function Bad([string]$m)   { Write-Host "[fail]    $m" -ForegroundColor Red }

$script:blocking = 0
function FailCheck([string]$m) { $script:blocking++; Bad $m }

Write-Host ''
Write-Host '  QuantMind Portable - installer' -ForegroundColor Cyan
Write-Host '  -------------------------------'
Write-Host "  pack root: $Root"
Write-Host ''

# ---------------------------------------------------------------
# 1. platform
# ---------------------------------------------------------------
if (-not [Environment]::Is64BitOperatingSystem) {
    FailCheck 'This pack is Windows x64 only (32-bit Windows is not supported).'
}
if ([Environment]::OSVersion.Version.Major -lt 10) {
    Warn "Windows $([Environment]::OSVersion.Version) detected; Windows 10/11 x64 is what this pack is built and tested for."
}

# ---------------------------------------------------------------
# 2. pack completeness (a half-extracted zip is the most common cause)
# ---------------------------------------------------------------
$required = @(
    'runtime\python\python.exe',
    'pgsql\bin\initdb.exe',
    'redis\redis-server.exe',
    'backend\main_oss.py',
    'web\index.html',
    'start.bat',
    'stop.bat',
    'pg_setup.py'
)
$missing = @($required | Where-Object { -not (Test-Path (Join-Path $Root $_)) })
if ($missing.Count -gt 0) {
    foreach ($m in $missing) { Bad "missing from the pack: $m" }
    Write-Host ''
    Write-Host '  The package looks incomplete. Re-extract the whole zip' -ForegroundColor Red
    Write-Host '  (extract ALL files; do not run it from inside the zip viewer).' -ForegroundColor Red
    exit 2
}
Good 'pack contents complete'

# ---------------------------------------------------------------
# 3. where the pack lives
# ---------------------------------------------------------------
if ($Root.StartsWith('\\')) {
    FailCheck 'The pack is on a network share (\\server\share). PostgreSQL and Redis cannot run there - copy the folder to a local disk, e.g. C:\QuantMind.'
}
try {
    $driveLetter = (Get-Item -LiteralPath $Root).PSDrive.Name
    $driveType = (New-Object System.IO.DriveInfo($driveLetter)).DriveType
    if ($driveType -eq 'Network') {
        FailCheck "Drive $driveLetter`: is a mapped network drive. PostgreSQL and Redis cannot run there - use a local disk."
    }
} catch {
    Warn "Could not determine the drive type of $Root - continuing."
}

if ($Root -match '[^\x20-\x7E]') {
    Warn 'The path contains non-ASCII characters. PostgreSQL initdb and some bundled tools can fail on such paths.'
    if (-not $Force) {
        FailCheck 'Move the folder to a pure-ASCII path (e.g. C:\QuantMind) and run install.bat again. Use -Force to try anyway.'
    }
}
if ($Root -match '\s') { Warn 'The path contains spaces; that is supported, but a path like C:\QuantMind is less trouble.' }
if ($Root.Length -gt 90) { Warn 'The path is long; deep sub-paths inside the pack may hit the Windows 260-character limit.' }

# ---------------------------------------------------------------
# 4. free space
# ---------------------------------------------------------------
try {
    $freeGB = [math]::Round((New-Object System.IO.DriveInfo($driveLetter)).AvailableFreeSpace / 1GB, 1)
    if ($freeGB -lt 10) {
        FailCheck "$freeGB GB free on $driveLetter`: - at least 10 GB is needed for the runtime, the database and logs."
    } elseif ($freeGB -lt 25) {
        Warn "$freeGB GB free on $driveLetter`: - enough to start, but market data (A-shares full history is ~60 GB) needs room."
    } else {
        Good "$freeGB GB free on $driveLetter`:"
    }
} catch {
    Warn 'Could not read free space - continuing.'
}

# ---------------------------------------------------------------
# 5. ports
# ---------------------------------------------------------------
$portNames = [ordered]@{
    5432 = 'PostgreSQL'
    6379 = 'Redis'
    8000 = 'web / API'
    8001 = 'engine'
    8002 = 'trade'
    8003 = 'stream'
}
$optionalPorts = [ordered]@{
    8090 = 'Huntly (RSS, optional)'
    8088 = 'QwenPaw (optional)'
}

function Get-PortOwner([int]$port) {
    try {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop | Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
    } catch {
        # Get-NetTCPConnection unavailable (old PowerShell) -> netstat fallback
        $line = netstat -ano | Select-String -Pattern ":$port\s" | Select-String -Pattern 'LISTENING' | Select-Object -First 1
        if ($line) {
            $parts = ($line.ToString() -split '\s+') | Where-Object { $_ -ne '' }
            if ($parts.Count -ge 5) { return [int]$parts[-1] }
        }
    }
    return 0
}

$alreadyRunning = $false
try {
    $health = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 3 -UseBasicParsing
    if ($health.StatusCode -eq 200) { $alreadyRunning = $true }
} catch { }

$busy = @()
foreach ($p in $portNames.Keys) {
    $owner = Get-PortOwner $p
    if ($owner -gt 0) {
        $pname = 'unknown'
        try { $pname = (Get-Process -Id $owner -ErrorAction Stop).ProcessName } catch { }
        $busy += [pscustomobject]@{ Port = $p; What = $portNames[$p]; Pid = $owner; Proc = $pname }
    }
}
if ($busy.Count -gt 0) {
    if ($alreadyRunning) {
        Good 'A QuantMind instance is already running on this machine.'
        foreach ($b in $busy) { Say "  port $($b.Port) ($($b.What)) is held by $($b.Proc) (PID $($b.Pid))" }
        Write-Host ''
        Write-Host "  Open http://127.0.0.1:8000/  - or run stop.bat first and re-run install.bat." -ForegroundColor Cyan
        if (-not $NoStart) { Start-Process 'http://127.0.0.1:8000/' }
        exit 0
    }
    foreach ($b in $busy) { Bad "port $($b.Port) ($($b.What)) is already in use by $($b.Proc) (PID $($b.Pid))" }
    Write-Host ''
    Write-Host '  Two ways out:' -ForegroundColor Cyan
    Write-Host '    a) stop whatever holds those ports, or' -ForegroundColor Cyan
    Write-Host '    b) change the ports in pack.env (QM_PG_PORT / QM_REDIS_PORT / QM_API_PORT /' -ForegroundColor Cyan
    Write-Host '       QM_ENGINE_PORT / QM_TRADE_PORT / QM_STREAM_PORT) - start.bat picks them up.' -ForegroundColor Cyan
    FailCheck 'required ports are taken'
} else {
    Good 'ports 5432 / 6379 / 8000-8003 are free'
}
foreach ($p in $optionalPorts.Keys) {
    if ((Get-PortOwner $p) -gt 0) {
        Warn "port $p ($($optionalPorts[$p])) is in use - that component will not start (the rest is unaffected)."
    }
}

# ---------------------------------------------------------------
# 6. pack.env (local settings; only generated on a first install)
# ---------------------------------------------------------------
$envPath = Join-Path $Root 'pack.env'
$pgData  = Join-Path $Root 'pgdata'
if (Test-Path $envPath) {
    Good 'pack.env exists - left as it is'
} elseif (Test-Path $pgData) {
    Warn 'pgdata\ exists but pack.env does not - the database keeps the password it was created with.'
    Warn 'If you never changed it, that password is the built-in default; see README.md.'
} else {
    # Random local DB password. Windows start.bat and Linux start.sh share one
    # shape: DB_PASSWORD drives the connection URL, the client env and initdb.
    # Alphanumeric only - it is stored in a .bat/.env style file and must not
    # need quoting.
    $alphabet = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    $pw = -join (1..28 | ForEach-Object { $alphabet[(Get-Random -Maximum $alphabet.Length)] })
    $lines = @(
        '# QuantMind portable - local settings (generated by install.ps1)',
        '# Both start.bat (Windows) and start.sh (Linux/WSL) read this file.',
        '# Commented-out lines are examples; uncomment to change a value.',
        '',
        '# Service ports (change if something else already uses them)',
        '#QM_PG_PORT=5432',
        '#QM_REDIS_PORT=6379',
        '#QM_API_PORT=8000',
        '#QM_ENGINE_PORT=8001',
        '#QM_TRADE_PORT=8002',
        '#QM_STREAM_PORT=8003',
        '',
        '# Local PostgreSQL password for this pack. It is applied when the database',
        '# directory is created on the FIRST start - changing it afterwards does NOT',
        '# change the password stored inside pgdata\\.',
        "DB_PASSWORD=$pw",
        '',
        '# AI / LLM keys (optional; without them the AI features stay disabled)',
        '#DEEPSEEK_API_KEY=sk-xxxx',
        '#DASHSCOPE_API_KEY=sk-xxxx',
        '',
        '# TDX bridge (only for live quote push; off by default)',
        '#ENABLE_TDX_PUSH=true',
        '#TDX_BRIDGE_URL=http://127.0.0.1:8550',
        '#TDX_BRIDGE_TOKEN=xxxx',
        '',
        '# Real trading - the BACKEND half of the switch. The frontend half is baked',
        '# in when the pack is built, so this key alone does not enable the live',
        '# trading UI. Off by default; only turn it on if you know what you are doing.',
        '#ENABLE_REAL_TRADING=true',
        '',
        '# Do not open a browser on start (server / headless use)',
        '#QM_OPEN_BROWSER=0'
    )
    # UTF-8 without BOM: start.bat reads it via `Get-Content -Encoding UTF8`.
    [System.IO.File]::WriteAllLines($envPath, $lines, (New-Object System.Text.UTF8Encoding($false)))
    Good 'pack.env created (random local database password, ports left at defaults)'
}

# ---------------------------------------------------------------
# 7. Desktop shortcut (opt-in)
# ---------------------------------------------------------------
if ($Shortcut) {
    try {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $lnk = Join-Path $desktop 'QuantMind.lnk'
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($lnk)
        $sc.TargetPath = Join-Path $Root 'start.bat'
        $sc.WorkingDirectory = $Root
        $sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
        $sc.Description = 'Start QuantMind'
        $sc.Save()
        Good "Desktop shortcut created: $lnk (it points at this folder - moving the pack breaks it)"
    } catch {
        Warn "Could not create the Desktop shortcut: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------
# 8. result
# ---------------------------------------------------------------
if ($script:blocking -gt 0) {
    Write-Host ''
    Write-Host "  $($script:blocking) blocking check(s) failed - nothing was started." -ForegroundColor Red
    Write-Host '  Fix them and run install.bat again (README.md has the details).' -ForegroundColor Red
    exit 1
}

Write-Host ''
Write-Host '  Checks passed.' -ForegroundColor Green
Write-Host '  Web login after start:  admin / admin123  (change it in Settings once you are in)'
Write-Host '  The first start takes 1-3 minutes: database init, schema, then 4 services + Celery.'
Write-Host ''

if ($NoStart) {
    Good 'Done (-NoStart). Double-click start.bat when you are ready.'
    exit 0
}

Say 'Starting QuantMind (start.bat) ...'
Write-Host ''
Start-Process -FilePath (Join-Path $Root 'start.bat') -WorkingDirectory $Root
Write-Host '  A new window is running the startup; the browser opens when it is ready.' -ForegroundColor Cyan
Write-Host '  Stop everything later with stop.bat.' -ForegroundColor Cyan
exit 0
