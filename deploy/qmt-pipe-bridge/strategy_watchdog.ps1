# Run qmt_strategy_watchdog.py once and append stdout/stderr to logs\strategy_watchdog.log.
# Scheduled task QMTTools-StrategyWatchdog runs this every minute.
# ASCII-only (Windows PowerShell 5.1 reads non-BOM UTF-8 as GBK).
$base = 'C:\new_tdx_mock\PYPlugins\bridge-windows\qmt-tools'
$logDir = Join-Path $base 'logs'
$log = Join-Path $logDir 'strategy_watchdog.log'
$py = 'C:\xmt\venv\Scripts\python.exe'
$script = Join-Path $base 'qmt-external-bridge\qmt_strategy_watchdog.py'
$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$env:QMT_VENV_SITE_PACKAGES = 'C:\xmt\venv\Lib\site-packages'

$out = & $py $script 2>&1
$code = $LASTEXITCODE
Add-Content -Path $log -Value ("$ts rc=$code " + ($out -join ' | ')) -Encoding UTF8

# Keep the log bounded (roughly a week of per-minute lines).
$lines = Get-Content $log -ErrorAction SilentlyContinue
if ($lines -and $lines.Count -gt 5000) {
    $lines | Select-Object -Last 3000 | Set-Content $log -Encoding UTF8
}
exit $code
