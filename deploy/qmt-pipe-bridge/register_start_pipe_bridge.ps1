$ErrorActionPreference = 'Stop'
$base = 'C:\new_tdx_mock\PYPlugins\bridge-windows\qmt-tools'
$script = Join-Path $base 'start_pipe_bridge.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $script + '"')
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'QMTTools-StartPipeBridge' -Action $action -Principal $principal -Settings $settings -Description 'Stable QMT pipe bridge launcher (exact stale cleanup + detached start)' -Force
Write-Output 'REGISTERED'
