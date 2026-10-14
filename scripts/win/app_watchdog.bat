@echo off
rem Redian monitor watchdog: start the platform only if /healthz does not answer.
rem Used by scheduled tasks "RedianMonitor_AutoStart" (at logon) and
rem "RedianMonitor_Watchdog" (hourly). Same entrypoint/port as Dockerfile
rem (app.platform:app), but bound to 127.0.0.1 (local access only).
rem pythonw = no console window at all: the user cannot kill it by closing a
rem minimized console (that is exactly how it died on 2026-09-28 11:16).
rem Keep this file ASCII-only. See doc/operations.md (Windows hosting).
cd /d D:\code\redian
rem PROBE FLAGS ARE LOAD-BEARING -- do not "simplify" them back (2026-10-13, reproduced live):
rem   * --noproxy "*" : if HTTP_PROXY/HTTPS_PROXY is set in this environment, plain curl sends
rem                      the loopback request THROUGH the proxy. A dead 8080 then yields a
rem                      proxy 502 instead of "connection refused".
rem   * -f             : without it curl exits 0 for ANY HTTP reply (a 502 counts as success),
rem                      so `if %errorlevel%==0` would decide "app is alive" while it is DEAD
rem                      and this watchdog would silently never restart it.
rem Measured with the app stopped and HTTP_PROXY set:
rem   curl -s -o NUL -m 5 http://127.0.0.1:8080/healthz   -> exit 0   (WRONG: claims healthy)
rem   curl -s -f -o NUL -m 5 --noproxy "*" ...            -> exit 7   (correct: refused)
rem This was not theoretical: it made THIS script return 0 without starting anything.
curl -s -f -o NUL -m 5 --noproxy "*" http://127.0.0.1:8080/healthz >NUL 2>&1
if %errorlevel%==0 exit /b 0
echo [%date% %time%] watchdog: app not answering, starting uvicorn >> data\app.log
rem App is down => every in-process alert died with it (doc/operations.md admits
rem this). The watchdog is the only thing still alive, so IT must report the
rem outage. Secrets stay out of this git-tracked file: the script reads .env
rem itself. Failure there must never delay the restart (the script swallows it).
"C:\Python314\pythonw.exe" scripts\win\notify_restart.py
start "" /b "C:\Python314\pythonw.exe" -m uvicorn app.platform:app --host 127.0.0.1 --port 8080 >> data\app.log 2>&1
