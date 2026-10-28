$base = 'C:\new_tdx_mock\PYPlugins\bridge-windows\qmt-tools'
$cmd  = "cmd.exe /c `"$base\start_pipe_bridge.cmd`""
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Output ("PipeBridge rc={0} pid={1}" -f $r.ReturnValue, $r.ProcessId)
