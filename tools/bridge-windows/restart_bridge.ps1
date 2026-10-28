# Restart the TDX bridge (port 8550) with the current source tree.
#
# ASCII only: Windows PowerShell 5.1 reads UTF-8 files without BOM as ANSI/GBK;
# the old Chinese text in this file made the script fail to parse (2026-10-28).
#
# Usage (right-click "Run with PowerShell", or):
#   powershell -NoProfile -ExecutionPolicy Bypass -File restart_bridge.ps1
$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "[restart] stopping process listening on 8550..."
$conns = Get-NetTCPConnection -LocalPort 8550 -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns) {
    $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host ("[restart] stop pid=" + $proc.Id + " (" + $proc.ProcessName + ")")
        Stop-Process -Id $proc.Id -Force
    }
}
if (-not $conns) { Write-Host "[restart] nothing listening on 8550" }

Start-Sleep -Seconds 2
$cmd = 'cmd.exe /c "' + $ScriptDir + '\start_bridge_hidden.cmd"'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Host ("[restart] start rc=" + $r.ReturnValue + " pid=" + $r.ProcessId)

Start-Sleep -Seconds 6
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8550/api/v1/health" -TimeoutSec 8
    Write-Host ("[restart] health=" + $health.status + " tdx_connected=" + $health.tdx_connected +
                " qmt_online=" + $health.qmt.online)
} catch {
    Write-Warning ("[restart] health check failed: " + $_.Exception.Message)
}
