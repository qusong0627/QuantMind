@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_pipe_bridge.ps1" %*
exit /b %ERRORLEVEL%
