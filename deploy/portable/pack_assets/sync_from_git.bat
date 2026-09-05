@echo off
rem QuantMind portable: sync code from local git clone into package
setlocal
cd /d "%~dp0"
set "PACK=%CD%"
set "BRANCH=master"
set "REPO="
rem auto-detect: env > user home > common path > next to package
for %%R in ("%QM_REPO_ROOT%" "%USERPROFILE%\quantmind-src" "%USERPROFILE%\QuantMind" "C:\QuantMind-src" "%PACK%\..\quantmind-src" "%PACK%\..\QuantMind" "%PACK%\..\src\quantmind-src") do (
    if exist "%%~fR\.git" set "REPO=%%~fR"
)
echo [sync] begin - pack: %PACK%
where git >nul 2>&1
if errorlevel 1 goto :git_missing
if defined REPO goto :repo_ok
echo [!] git is installed, but no repo auto-detected.
echo     Clones tried: QM_REPO_ROOT / user home / C:\QuantMind-src /
echo     a quantmind-src folder NEXT TO this package.
echo.
echo     HOW TO FIX - one-time setup:
echo     1. In Explorer go to:  %PACK%\..
echo     2. Open a terminal there and run:
echo        git clone -b master --single-branch https://gitee.com/qusong0627/QuantMind.git quantmind-src
echo        (private repo? use the URL the maintainer gave you)
echo     3. Run this script again - it will auto-find the clone.
echo.
echo     Or skip git entirely: ask the maintainer for a patch zip.
pause
exit /b 1
:git_missing
echo [!] git is NOT installed on this machine.
echo     Install it first from https://git-scm.com/download/win
echo     (next-next-next install, then close and reopen this window),
echo     then run this script again.
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
