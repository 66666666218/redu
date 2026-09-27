@echo off
rem Register the two scheduled tasks for the Redian monitor:
rem   RedianMonitor_AutoStart  - start the app 1 min after logon
rem   RedianMonitor_Watchdog   - hourly check, revive the app if it died
rem Run once as the normal user; admin rights not required.
rem Keep this file ASCII-only. See doc/operations.md section 12.
schtasks /create /tn RedianMonitor_AutoStart /tr "wscript.exe D:\code\redian\scripts\win\start_hidden.vbs" /sc onlogon /delay 0001:00 /f
schtasks /create /tn RedianMonitor_Watchdog /tr "wscript.exe D:\code\redian\scripts\win\start_hidden.vbs" /sc hourly /mo 1 /f
echo --- verify ---
schtasks /query /tn RedianMonitor_AutoStart
schtasks /query /tn RedianMonitor_Watchdog
