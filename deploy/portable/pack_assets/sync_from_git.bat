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
set "PATH=%PACK%\tools\git\cmd;%PATH%"
where git >nul 2>&1
if not errorlevel 1 goto :git_ready
if exist "%PACK%\tools\git\cmd\git.exe" goto :git_ready
echo [sync] git not found - downloading portable git (one-time, ~45MB)...
curl -L --retry 2 -o "%TEMP%\QuantMind-mingit.zip" "https://github.com/git-for-windows/git/releases/download/v2.49.0.windows.1/MinGit-2.49.0-64-bit.zip"
if errorlevel 1 goto :dl_fail
mkdir "%PACK%\tools" 2>nul
tar -xf "%TEMP%\QuantMind-mingit.zip" -C "%PACK%\tools"
if exist "%PACK%\tools\git\cmd\git.exe" goto :git_ready
powershell -NoProfile -Command "Expand-Archive -Force '%TEMP%\QuantMind-mingit.zip' '%PACK%\tools'"
if errorlevel 1 goto :dl_fail
:git_ready
where git >nul 2>&1
if errorlevel 1 goto :dl_fail
if defined REPO goto :repo_ok
echo [sync] git is ready, but no local clone found next to the package.
echo     I can clone it for you now (recommended, one-time ~hundreds MB):
echo       target: %PACK%\..\quantmind-src
echo       source: https://gitee.com/qusong0627/QuantMind.git  branch: master
set /p DO_CLONE=Auto-clone now? [y/n]:
if /I "%DO_CLONE%"=="y" goto :auto_clone
if /I "%DO_CLONE%"=="yes" goto :auto_clone
echo.
echo     Skipped. To do it manually later:
echo       cd /d %PACK%\..
echo       git clone -b master --single-branch https://gitee.com/qusong0627/QuantMind.git quantmind-src
echo     then rerun this script. Private repo? use the maintainer URL.
echo     No git at all? ask the maintainer for a patch zip.
pause
exit /b 1
:auto_clone
echo [sync] cloning into %PACK%\..\quantmind-src ...
git clone -b master --single-branch https://gitee.com/qusong0627/QuantMind.git "%PACK%\..\quantmind-src"
if errorlevel 1 goto :clone_fail
set "REPO=%PACK%\..\quantmind-src"
echo [sync] clone OK.
goto :repo_ok
:clone_fail
echo [!] clone failed - check internet or repo access.
echo     If it asked for credentials, the repo is private: ask the
echo     maintainer for access or a different URL, then rerun.
pause
exit /b 1
:dl_fail
echo [!] automatic git download failed.
echo     Check internet, then rerun. Or install git manually from
echo     https://git-scm.com/download/win and rerun this script.
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
