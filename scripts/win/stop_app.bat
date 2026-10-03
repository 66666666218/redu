@echo off
rem Planned stop for the Redian monitor app (2026-10-03).
rem Writes the graceful-shutdown marker FIRST, then stops ONLY the process bound
rem to 127.0.0.1:8080. The watchdog alert (notify_restart.py) skips alerting when
rem that marker is fresh -- so a planned restart stays silent while a real crash
rem still pages the admin group.
rem WHY a separate script: taskkill /F is a hard kill and never runs the app's
rem lifespan handler, so the marker would not be written by the app itself.
rem WHY not "taskkill /IM pythonw.exe": that kills EVERY python on the box
rem (MediaCrawler's venv, ad-hoc scripts). Only our listener should die.
rem KEEP THIS FILE ASCII-ONLY (same rule as app_watchdog.bat).
cd /d D:\code\redian
set PID=
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080" ^| findstr "LISTENING"') do set PID=%%a
if not exist data mkdir data
powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-ddTHH:mm:ss' | Set-Content -Encoding ASCII 'data\last_shutdown.txt'"
echo [%date% %time%] stop_app: marker written, stopping pid %PID% >> data\app.log
if defined PID taskkill /PID %PID% /F >NUL 2>&1
if not defined PID echo [%date% %time%] stop_app: nothing listening on 8080 >> data\app.log
exit /b 0
