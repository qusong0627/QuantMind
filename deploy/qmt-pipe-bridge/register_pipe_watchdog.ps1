# Register QMTTools-PipeWatchdog: run pipe_watchdog.ps1 every minute as SYSTEM.
# ASCII-only (Windows PowerShell 5.1 GBK parsing).
$taskName = 'QMTTools-PipeWatchdog'
$script = 'C:\new_tdx_mock\PYPlugins\bridge-windows\qmt-tools\pipe_watchdog.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $script + '"')
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Force | Out-Null
Write-Output ("registered " + $taskName)
