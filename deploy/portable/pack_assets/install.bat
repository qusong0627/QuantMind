@echo off
rem ============================================================
rem QuantMind Portable - one-click install (Windows x64)
rem
rem Thin wrapper: the real checks live in install.ps1 so there is exactly one
rem implementation behind two entry points. This file exists so a user can
rem double-click something, and it still does the two checks that matter even
rem when PowerShell is unavailable or blocked by policy.
rem
rem ASCII + CRLF on purpose: cmd on a zh-CN system parses a UTF-8 batch file as
rem GBK and the whole script turns into garbage.
rem ============================================================
setlocal
cd /d "%~dp0"
title QuantMind Installer

echo.
echo   QuantMind Portable - one-click install
echo   --------------------------------------
echo.

rem ---- 1. 64-bit Windows (pure cmd: works without PowerShell) ----
if "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 goto :bad_arch

rem ---- 2. pack completeness (most common cause: half-extracted zip) ----
if not exist "%~dp0runtime\python\python.exe" goto :bad_pack
if not exist "%~dp0pgsql\bin\initdb.exe" goto :bad_pack
if not exist "%~dp0backend\main_oss.py" goto :bad_pack
if not exist "%~dp0start.bat" goto :bad_pack

rem ---- 3. hand over to install.ps1 ----
where powershell >nul 2>nul
if errorlevel 1 goto :no_ps

set "PS_ARGS=-NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1""

choice /c YN /n /m "Create a Desktop shortcut to start.bat? [Y=yes / N=no] "
if errorlevel 2 goto :run_ps
set "PS_ARGS=%PS_ARGS% -Shortcut"
echo.

:run_ps
powershell %PS_ARGS% %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" goto :ps_failed
echo   Installer finished. This window can be closed.
pause
exit /b 0

:ps_failed
echo   Installer exited with code %RC% - see the [fail] lines above.
echo   Nothing was started. README.md has the details, then run install.bat again.
pause
exit /b %RC%

rem ---------------------------------------------------------------
:bad_arch
echo [!] 32-bit Windows is not supported.
echo     This package is Windows x64 only (Windows 10/11 64-bit).
pause
exit /b 2

:bad_pack
echo [!] The package looks incomplete - these are missing from %~dp0:
echo       runtime\python\python.exe / pgsql\bin\initdb.exe /
echo       backend\main_oss.py / start.bat
echo.
echo     Re-extract the WHOLE zip (extract all files first; do not run it
echo     from inside the zip viewer or a partial copy).
pause
exit /b 2

:no_ps
echo [!] PowerShell was not found on this system.
echo.
echo     The installer checks (ports, disk space, pack.env) were skipped.
echo     You can still run the pack directly:
echo       1. double-click start.bat
echo       2. if a port is already taken, edit pack.env (see pack.env for keys)
echo       3. web login is admin / admin123 - change it in Settings afterwards
echo.
pause
exit /b 3
