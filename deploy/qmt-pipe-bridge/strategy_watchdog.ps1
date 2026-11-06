# Run qmt_strategy_watchdog.py once and append stdout/stderr to logs\strategy_watchdog.log.
# Hard cap 60s: a wedged pipe read must never leave the task Running forever
# (2026-11-06: one run hung 8 minutes; every later trigger was swallowed by
#  MultipleInstances=IgnoreNew and auto-recovery was dead when it was needed).
# ASCII-only (Windows PowerShell 5.1 reads non-BOM UTF-8 as GBK).
$base = 'C:\new_tdx_mock\PYPlugins\bridge-windows\qmt-tools'
$logDir = Join-Path $base 'logs'
$log = Join-Path $logDir 'strategy_watchdog.log'
$py = 'C:\xmt\venv\Scripts\python.exe'
$script = Join-Path $base 'qmt-external-bridge\qmt_strategy_watchdog.py'
$outFile = Join-Path $logDir '.wd_out.txt'
$errFile = Join-Path $logDir '.wd_err.txt'
$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$env:QMT_VENV_SITE_PACKAGES = 'C:\xmt\venv\Lib\site-packages'
Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue

$p = Start-Process -FilePath $py -ArgumentList @($script) -WorkingDirectory $base `
     -WindowStyle Hidden -PassThru `
     -RedirectStandardOutput $outFile -RedirectStandardError $errFile
$finished = $p.WaitForExit(60000)

$out = ''
if (Test-Path $outFile) { $out += (Get-Content $outFile -Raw -ErrorAction SilentlyContinue) }
if (Test-Path $errFile) { $out += (Get-Content $errFile -Raw -ErrorAction SilentlyContinue) }
if ($finished) {
    try { $p.Refresh(); $code = [int]$p.ExitCode } catch { $code = 0 }
} else {
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    $code = 124
    $out = 'TIMEOUT 60s (pipe wedged?) ' + $out
}
Add-Content -Path $log -Value ("$ts rc=$code " + (($out -replace "`r?`n", ' | ').Trim().TrimEnd(' ', '|'))) -Encoding UTF8

$lines = Get-Content $log -ErrorAction SilentlyContinue
if ($lines -and $lines.Count -gt 5000) {
    $lines | Select-Object -Last 3000 | Set-Content $log -Encoding UTF8
}
exit $code
