@echo off
rem ============================================================
rem QuantMind portable: restore last-known-good code backup (win)
rem Put this file in the package root next to start.bat
rem Run it when start.bat fails AFTER a sync_from_git.bat update.
rem ASCII + CRLF + flat style - do not add parens-heavy constructs
rem ============================================================
setlocal
cd /d "%~dp0"
set "PACK=%CD%"
set "BACKUP=%PACK%\backups\code-backup"
set "BACKUP_OLD=%PACK%\backups\code-backup.old"

echo [restore] pack: %PACK%

if exist "%BACKUP%\backend\main_oss.py" goto :pick_new
if exist "%BACKUP_OLD%\backend\main_oss.py" goto :pick_old
echo [!] no backup found under %PACK%\backups
echo     Nothing was changed. Run start.bat and check logs.
pause
exit /b 1

:pick_old
echo [restore] newest backup is missing - using the older one
set "BACKUP=%BACKUP_OLD%"
:pick_new
echo [restore] source: %BACKUP%

echo [restore] stopping services ...
taskkill /FI "WINDOWTITLE eq QuantMind-Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryWorker*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-CeleryBeat*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-Huntly*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq QuantMind-QwenPaw*" /T /F >nul 2>&1
taskkill /FI "IMAGENAME eq python.exe" /F >nul 2>&1
timeout /t 2 /nobreak >nul

echo [restore] replacing backend ...
if exist "%PACK%\backend" rmdir /s /q "%PACK%\backend"
if exist "%PACK%\backend" goto :del_fail
robocopy "%BACKUP%\backend" "%PACK%\backend" /E /XD __pycache__ /NFL /NDL /NJH /NJS
set "RRC1=%errorlevel%"

echo [restore] replacing config ...
if exist "%PACK%\config" rmdir /s /q "%PACK%\config"
if exist "%PACK%\config" goto :del_fail
robocopy "%BACKUP%\config" "%PACK%\config" /E /XD __pycache__ /NFL /NDL /NJH /NJS
set "RRC2=%errorlevel%"

echo [restore] replacing strategy_templates ...
if exist "%PACK%\strategy_templates" rmdir /s /q "%PACK%\strategy_templates"
if exist "%PACK%\strategy_templates" goto :del_fail
robocopy "%BACKUP%\strategy_templates" "%PACK%\strategy_templates" /E /XD __pycache__ /NFL /NDL /NJH /NJS
set "RRC3=%errorlevel%"

set "RRC4=0"
if not exist "%BACKUP%\web\index.html" goto :web_skip
echo [restore] replacing web (frontend) ...
if exist "%PACK%\web" rmdir /s /q "%PACK%\web"
if exist "%PACK%\web" goto :del_fail
robocopy "%BACKUP%\web" "%PACK%\web" /E /NFL /NDL /NJH /NJS
set "RRC4=%errorlevel%"
:web_skip
if exist "%BACKUP%\web\index.html" goto :web_checked
echo [restore] backup has no web - leaving current frontend untouched
:web_checked

if %RRC1% GEQ 8 goto :copy_fail
if %RRC2% GEQ 8 goto :copy_fail
if %RRC3% GEQ 8 goto :copy_fail
if %RRC4% GEQ 8 goto :copy_fail

echo.
echo [restore] DONE - the previous working code is back.
echo     Double-click start.bat now.
echo     Still failing? The problem is then outside the code
echo     (runtime or data) - see logs\backend.log and ask the
echo     maintainer with the log content.
pause
exit /b 0

:del_fail
echo [!] cannot delete a package folder - a file is locked.
echo     Close all QuantMind windows and any Explorer windows
echo     that are inside the package folder, then rerun.
pause
exit /b 1

:copy_fail
echo [!] restore copy failed - see messages above.
echo     Retry, or ask the maintainer with the log content.
pause
exit /b 1
