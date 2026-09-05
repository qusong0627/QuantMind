@echo off
rem ============================================================
rem QuantMind portable: sync code from local git clone into package
rem Prereq: this file sits in the package root, and a git clone of
rem the QuantMind repo exists on this machine.
rem Edit REPO below to that clone path (or set env QM_REPO_ROOT).
rem
rem What it does:
rem   git pull (origin <branch>)
rem   robocopy backend/ config/ strategy_templates/ into the package
rem   clear __pycache__ (stale bytecode protection)
rem   ask you to restart with start.bat
rem
rem NOTE: runtime/models/data are NOT in git - code updates only.
rem Frontend changes are NOT in git either (dist-react artifacts);
rem sync web/ separately if the update includes UI changes.
rem ============================================================
setlocal
cd /d "%~dp0"
set "PACK=%CD%"
if defined QM_REPO_ROOT (set "REPO=%QM_REPO_ROOT%") else (set "REPO=C:\QuantMind-src")
set "BRANCH=main"

if not exist "%REPO%\.git" (
    echo [!] git repo not found at %REPO%
    echo     Edit this file (REPO line) or set QM_REPO_ROOT, e.g.
    echo     set QM_REPO_ROOT=C:\path\to\quantmind
    pause
    exit /b 1
)

echo [sync] repo: %REPO%  branch: %BRANCH%
echo [sync] pulling latest code...
git -C "%REPO%" fetch origin
git -C "%REPO%" checkout %BRANCH%
git -C "%REPO%" pull origin %BRANCH%
if errorlevel 1 goto :pull_fail

echo [sync] stopping services first...
taskkill /FI "WINDOWTITLE eq QuantMind-Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryWorker*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryBeat*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Huntly*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-QwenPaw*" /T /F >nul 2>&1
taskkill /FI "IMAGENAME eq python.exe" /F >nul 2>&1

echo [sync] copying backend...
robocopy "%REPO%\backend" "%PACK%\backend" /E /NFL /NDL /NJH /NJS >nul
echo [sync] copying config...
robocopy "%REPO%\config" "%PACK%\config" /E /NFL /NDL /NJH /NJS >nul
echo [sync] copying strategy_templates...
robocopy "%REPO%\strategy_templates" "%PACK%\strategy_templates" /E /NFL /NDL /NJH /NJS >nul

echo [sync] clearing __pycache__...
for /d /r "%PACK%\backend" %%d in (__pycache__) do rd /s /q "%%d" 2>nul

echo.
echo [sync] Done. Restart with start.bat.
echo [sync] UI changes? rebuild dist or sync web/ from the maintainer.
pause
exit /b 0

:pull_fail
echo [!] git pull failed - check network/credentials/branch name.
pause
exit /b 1
