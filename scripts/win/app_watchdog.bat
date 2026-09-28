@echo off
rem Redian monitor watchdog: start the platform only if /healthz does not answer.
rem Used by scheduled tasks "RedianMonitor_AutoStart" (at logon) and
rem "RedianMonitor_Watchdog" (hourly). Same entrypoint/port as Dockerfile
rem (app.platform:app), but bound to 127.0.0.1 (local access only).
rem pythonw = no console window at all: the user cannot kill it by closing a
rem minimized console (that is exactly how it died on 2026-09-28 11:16).
rem Keep this file ASCII-only. See doc/operations.md (Windows hosting).
cd /d D:\code\redian
curl -s -o NUL -m 5 http://127.0.0.1:8080/healthz >NUL 2>&1
if %errorlevel%==0 exit /b 0
echo [%date% %time%] watchdog: app not answering, starting uvicorn >> data\app.log
start "" /b "C:\Python314\pythonw.exe" -m uvicorn app.platform:app --host 127.0.0.1 --port 8080 >> data\app.log 2>&1
