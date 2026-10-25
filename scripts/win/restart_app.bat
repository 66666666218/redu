@echo off
rem One-click PLANNED restart of the redian platform (2026-10-14).
rem Usage: double-click this file, read the outcome, press a key. Nothing else needed.
rem
rem WHY this file exists, instead of double-clicking stop_app.bat then start_hidden.vbs:
rem   1. the ORDER matters (stop before start) and the two clicks must land inside the
rem      180 s graceful-shutdown-marker window -- the kind of manual sequence that goes
rem      wrong at 23:00;
rem   2. start_hidden.vbs runs app_watchdog.bat, and that script STARTS NOTHING while
rem      /healthz still answers -- so clicking start first silently does nothing, and the
rem      stop that follows leaves the platform down until the next hourly watchdog run
rem      (:41), i.e. up to ~1 hour of blindness;
rem   3. it WAITS for port 8080 to be released, so the new uvicorn cannot die with
rem      "address already in use".
rem
rem FAIL-SAFE BY DESIGN (do not simplify this away): if 8080 is still LISTENING after the
rem grace period, it REFUSES to start a second uvicorn and exits 1. Rationale: starting
rem while the old one still holds the port produces a doomed process, and starting while
rem the old one is alive would also fail on the `>> data\app.log` redirection -- app.log
rem is HELD by the running service (that is why notify_restart.py logs to its own file).
rem So the worst case here is "still running the OLD code", never "nothing running".
rem
rem The launch line is copied VERBATIM from app_watchdog.bat on purpose: same pythonw,
rem same entrypoint, same port, same log. Do not "improve" it -- if the watchdog and this
rem file drift apart, a restart stops being equivalent to a watchdog restart.
rem
rem KEEP THIS FILE ASCII-ONLY (same rule as app_watchdog.bat / stop_app.bat).
cd /d D:\code\redian
call scripts\win\stop_app.bat
set /a tries=0
:wait
rem ping is the portable batch sleep (timeout.exe needs a console and can be absent)
ping -n 2 127.0.0.1 >NUL 2>&1
netstat -ano | findstr ":8080" | findstr "LISTENING" >NUL 2>&1
if errorlevel 1 goto start_it
set /a tries+=1
if %tries% GEQ 20 goto stuck
goto wait
:start_it
rem ---- rotate app.log (2026-10-26) ----
rem Same rule as app_watchdog.bat: keep the two launch paths identical.
if exist data\app.log for %%A in (data\app.log) do if %%~zA GTR 52428800 move /y data\app.log data\app.log.1 >NUL 2>&1
start "" /b "C:\Python314\pythonw.exe" -m uvicorn app.platform:app --host 127.0.0.1 --port 8080 >> data\app.log 2>&1
echo.
echo  Restart issued. Give it ~5 s, then verify it really came back:
echo    curl -s -o NUL -w "%%{http_code}" -m 8 --noproxy "*" http://127.0.0.1:8080/healthz
echo  Expected: 200   (--noproxy and -f matter -- see app_watchdog.bat)
echo.
pause
exit /b 0
:stuck
echo.
echo  STOP FAILED: port 8080 was still LISTENING after ~20 s.
echo  Refusing to start a second uvicorn. The OLD process is still serving,
echo  so nothing is down -- it just still runs the old code. Re-run this file,
echo  or just wait for the hourly watchdog (:41) after killing it manually.
echo.
echo [%date% %time%] restart_app: port 8080 still LISTENING after ~20s, refused to start a second uvicorn >> data\restart_app.log
pause
exit /b 1
