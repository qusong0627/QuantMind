@echo off
rem QuantMind portable: sync code from local git clone into package
setlocal
cd /d "%~dp0"
set "PACK=%CD%"
set "BRANCH=main"
set "REPO=C:\QuantMind-src"
if defined QM_REPO_ROOT set "REPO=%QM_REPO_ROOT%"
echo [sync] begin - pack: %PACK%

if exist "%REPO%\.git" goto :repo_ok
echo [!] git repo not found at %REPO%
echo     Edit REPO line in this file, or run:  set QM_REPO_ROOT=C:\path\to\quantmind
pause
exit /b 1
:repo_ok
echo [sync] repo: %REPO%  branch: %BRANCH%

echo [sync] step 1/5 git fetch...
git -C "%REPO%" fetch origin
if errorlevel 1 goto :git_fail

echo [sync] step 2/5 git checkout...
git -C "%REPO%" checkout %BRANCH%
if errorlevel 1 goto :git_fail

echo [sync] step 3/5 git pull...
git -C "%REPO%" pull origin %BRANCH%
if errorlevel 1 goto :git_fail

echo [sync] step 4/5 stop services...
taskkill /FI "WINDOWTITLE eq QuantMind-Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryWorker*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryBeat*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Huntly*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-QwenPaw*" /T /F >nul 2>&1
taskkill /FI "IMAGENAME eq python.exe" /F >nul 2>&1

echo [sync] step 5/5 copy code into package...
robocopy "%REPO%\backend" "%PACK%\backend" /E /NFL /NDL /NJH /NJS
robocopy "%REPO%\config" "%PACK%\config" /E /NFL /NDL /NJH /NJS
robocopy "%REPO%\strategy_templates" "%PACK%\strategy_templates" /E /NFL /NDL /NJH /NJS
for /d /r "%PACK%\backend" %%d in (__pycache__) do rd /s /q "%%d" 2>nul

echo.
echo [sync] Done. Restart with start.bat
pause
exit /b 0

:git_fail
echo [!] git step failed - check network, credentials, branch name.
pause
exit /b 1
