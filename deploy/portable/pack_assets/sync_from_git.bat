@echo off
rem ============================================================
rem QuantMind portable: one-click git update for end users (win)
rem ASCII + CRLF + flat style - do not add parens-heavy constructs
rem ============================================================
setlocal
cd /d "%~dp0"
set "PACK=%CD%"
set "BRANCH=next"
set "URL=https://gitee.com/qusong0627/QuantMind.git"
set "REPO="
set "GITOK=0"
set "BEFORE="
set "N_COMMITS=0"
set "N_FILES=0"

echo [sync] begin - pack: %PACK%

rem ---- 0. write permission pre-check (read-only dir / AV lock) ----
echo test> "%PACK%\.qm_write_test"
if errorlevel 1 goto :perm_fail
del "%PACK%\.qm_write_test" >nul 2>&1
if not exist "%PACK%\backend" mkdir "%PACK%\backend" 2>nul
echo test> "%PACK%\backend\.qm_write_test"
if errorlevel 1 goto :perm_fail
del "%PACK%\backend\.qm_write_test" >nul 2>&1

rem ---- 1. ensure git (system, or portable downloaded into pack) ----
set "PATH=%PACK%\tools\git\cmd;%PATH%"
where git >nul 2>&1
if not errorlevel 1 set "GITOK=1"
if "%GITOK%"=="1" goto :git_ready
if exist "%PACK%\tools\git\cmd\git.exe" goto :git_ready
echo [sync] git not found - downloading portable git (one-time ~45MB)...
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

rem ---- 2. locate repo (env / home / next to package) ----
for %%R in ("%QM_REPO_ROOT%" "%USERPROFILE%\quantmind-src" "%USERPROFILE%\QuantMind" "C:\QuantMind-src" "%PACK%\..\quantmind-src" "%PACK%\..\QuantMind" "%PACK%\..\src\quantmind-src") do (
    if exist "%%~fR\.git" set "REPO=%%~fR"
)
if defined REPO goto :repo_ok

rem ---- 3. not found - offer auto-clone ----
echo [sync] no local clone found next to the package.
echo     I can clone it for you now (one-time, ~hundreds MB):
echo       target: %PACK%\..\quantmind-src
echo       source: %URL%   branch: %BRANCH%
set /p DO_CLONE=Auto-clone now? [y/n]:
if /I "%DO_CLONE%"=="y" goto :auto_clone
if /I "%DO_CLONE%"=="yes" goto :auto_clone
echo.
echo     Skipped. Manual clone later:
echo       cd /d %PACK%\..
echo       git clone -b %BRANCH% %URL% quantmind-src
echo     then rerun this script. Private repo? ask the maintainer for URL/access.
echo     No git at all? ask the maintainer for a patch zip.
pause
exit /b 1
:auto_clone
echo [sync] cloning...
git clone -b %BRANCH% %URL% "%PACK%\..\quantmind-src"
if errorlevel 1 goto :clone_fail
set "REPO=%PACK%\..\quantmind-src"
echo [sync] clone OK.
goto :repo_ok

:repo_ok
echo [sync] repo: %REPO%  branch: %BRANCH%
for /f "delims=" %%H in ('git -C "%REPO%" rev-parse HEAD 2^>nul') do set "BEFORE=%%H"

echo [sync] step 1/5 fetch...
git -C "%REPO%" fetch origin
if errorlevel 1 goto :git_fail
git -C "%REPO%" fetch origin %BRANCH%:refs/remotes/origin/%BRANCH%
if errorlevel 1 echo [sync] warning: prefetch of %BRANCH% failed, checkout will tell if it matters
echo [sync] step 2/5 checkout...
git -C "%REPO%" checkout %BRANCH%
if errorlevel 1 goto :git_fail
echo [sync] step 3/5 pull...
git -C "%REPO%" pull origin %BRANCH%
if errorlevel 1 goto :git_fail

rem counts (commits & changed files since before)
for /f "delims=" %%C in ('git -C "%REPO%" rev-list --count %BEFORE%..HEAD 2^>nul') do set "N_COMMITS=%%C"
for /f "delims=" %%F in ('git -C "%REPO%" diff --name-only %BEFORE%..HEAD 2^>nul') do set /a N_FILES+=1

echo [sync] step 4/5 stop services and backup current code...
taskkill /FI "WINDOWTITLE eq QuantMind-Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryWorker*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryBeat*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Huntly*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-QwenPaw*" /T /F >nul 2>&1
taskkill /FI "IMAGENAME eq python.exe" /F >nul 2>&1

rem ---- 4b. backup the currently working code before overwriting ----
rem      roll two copies: backups\code-backup (newest) and .old
rem      restore after a failed start with restore_backup.bat
set "BACKUP=%PACK%\backups\code-backup"
set "BACKUP_OLD=%PACK%\backups\code-backup.old"
if not exist "%PACK%\backend\main_oss.py" goto :skip_backup
mkdir "%PACK%\backups" 2>nul
if not exist "%BACKUP%\backend\main_oss.py" goto :roll_done
rmdir /s /q "%BACKUP_OLD%" >nul 2>&1
ren "%BACKUP%" code-backup.old
:roll_done
echo [sync] backing up current runnable code (a minute for 150MB)...
robocopy "%PACK%\backend" "%BACKUP%\backend" /E /XD __pycache__ /NFL /NDL /NJH /NJS
set "BRC1=%errorlevel%"
robocopy "%PACK%\config" "%BACKUP%\config" /E /XD __pycache__ /NFL /NDL /NJH /NJS
set "BRC2=%errorlevel%"
robocopy "%PACK%\strategy_templates" "%BACKUP%\strategy_templates" /E /XD __pycache__ /NFL /NDL /NJH /NJS
set "BRC3=%errorlevel%"
set "BRC4=0"
if exist "%PACK%\web\index.html" robocopy "%PACK%\web" "%BACKUP%\web" /E /XD __pycache__ /NFL /NDL /NJH /NJS
if errorlevel 1 set "BRC4=%errorlevel%"
if %BRC1% GEQ 8 goto :backup_fail
if %BRC2% GEQ 8 goto :backup_fail
if %BRC3% GEQ 8 goto :backup_fail
if %BRC4% GEQ 8 goto :backup_fail
echo [sync] backup OK - if the new code fails to start you can restore it.
:skip_backup

echo [sync] step 5/5 copy code...
robocopy "%REPO%\backend" "%PACK%\backend" /E /NFL /NDL /NJH /NJS
set "RC1=%errorlevel%"
robocopy "%REPO%\config" "%PACK%\config" /E /NFL /NDL /NJH /NJS
set "RC2=%errorlevel%"
robocopy "%REPO%\strategy_templates" "%PACK%\strategy_templates" /E /NFL /NDL /NJH /NJS
set "RC3=%errorlevel%"
rem training scripts: built from repo docker\training into package root - MUST refresh
set "RC5=0"
if exist "%REPO%\docker\training\train.py" (
    copy /Y "%REPO%\docker\training\train.py" "%PACK%\train.py" >nul
    copy /Y "%REPO%\docker\training\preprocessing.py" "%PACK%\preprocessing.py" >nul
    copy /Y "%REPO%\docker\training\parallel_utils.py" "%PACK%\parallel_utils.py" >nul
) else (
    echo [sync] note: repo docker\training missing - package train.py NOT refreshed
    set "RC5=1"
)
rem web = prebuilt frontend tracked in repo (since 2026-09): mirror into package
set "RC4=0"
if exist "%REPO%\web\index.html" robocopy "%REPO%\web" "%PACK%\web" /MIR /NFL /NDL /NJH /NJS
if errorlevel 1 set "RC4=%errorlevel%"
if not exist "%REPO%\web\index.html" echo [sync] note: repo web/index.html missing - frontend sync skipped
for /d /r "%PACK%\backend" %%d in (__pycache__) do rd /s /q "%%d" 2>nul
if %RC1% GEQ 8 goto :copy_fail
if %RC2% GEQ 8 goto :copy_fail
if %RC3% GEQ 8 goto :copy_fail
if %RC4% GEQ 8 goto :copy_fail
if exist "%REPO%\web\index.html" echo [sync] web assets updated (UI changes are in this sync)

rem ---- summary ----
mkdir "%PACK%\logs" 2>nul
echo %date% %time% ^| commits=%N_COMMITS% files=%N_FILES% >> "%PACK%\logs\sync_history.log"
echo.
echo ============================================
echo [sync] UPDATE SUMMARY
if "%N_COMMITS%"=="0" echo   No new commits - already up to date.
if not "%N_COMMITS%"=="0" echo   New commits : %N_COMMITS%
echo   Files changed : %N_FILES%
echo   Backup       : backups\code-backup
echo   History      : logs\sync_history.log
echo ============================================
echo [sync] Done. Restart with start.bat
rem customer update zips (repo deploy\portable\updates): mirror into package updates\
mkdir "%PACK%\updates" 2>nul
if exist "%REPO%\deploy\portable\updates\*.zip" copy /Y "%REPO%\deploy\portable\updates\*.zip" "%PACK%\updates\" >nul
echo [sync] customer update zips mirrored to updates\
echo [sync] Start failed? Restore the previous working code:
echo        double-click restore_backup.bat, then start.bat again
pause
exit /b 0

:perm_fail
echo [!] CANNOT WRITE to the package folder.
echo     Causes: folder read-only, on a CD/network share, or antivirus
echo     is locking it. Move/copy the package to a normal local folder
echo     e.g. C:\QuantMind, add an antivirus exception if needed, retry.
pause
exit /b 1

:backup_fail
echo [!] BACKUP FAILED - update cancelled, your working code is intact.
echo     Free disk space, close antivirus popups, then rerun.
pause
exit /b 1

:copy_fail
echo [!] some files could not be copied - likely still in use.
echo     Run stop.bat, then:  taskkill /F /IM python.exe
echo     then rerun this script.
pause
exit /b 1

:dl_fail
echo [!] git download/setup failed - check internet and rerun.
echo     Or install git from https://git-scm.com/download/win and rerun.
pause
exit /b 1

:clone_fail
echo [!] clone failed - check internet, or repo needs credentials.
echo     Private repo? ask the maintainer for access or another URL.
pause
exit /b 1

:git_fail
echo [!] git step failed - check network / credentials / branch name.
pause
exit /b 1
