# Restart the TDX bridge on port 8550 (ASCII only: Windows PowerShell 5.1 reads
# UTF-8 scripts without BOM as ANSI/GBK, Chinese text can break parsing).
$base = 'C:\new_tdx_mock\PYPlugins\bridge-windows'
$conns = Get-NetTCPConnection -LocalPort 8550 -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns) {
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    if ($p) {
        Write-Output ("stop old bridge pid=" + $p.Id)
        Stop-Process -Id $p.Id -Force
    }
}
Start-Sleep -Seconds 2
$cmd = 'cmd.exe /c "' + $base + '\start_bridge_hidden.cmd"'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Output ("start rc=" + $r.ReturnValue + " pid=" + $r.ProcessId)
