@echo off
rem Register Redian monitor persistence, NO admin required:
rem   Watchdog  - hourly scheduled task (user-level, schtasks works without admin)
rem   AutoStart - logon autostart via the user's Startup folder
rem               (schtasks /sc onlogon needs admin: "Access Denied" without it)
rem Keep this file ASCII-only. See doc/operations.md section 12.
schtasks /create /tn RedianMonitor_Watchdog /tr "wscript.exe D:\code\redian\scripts\win\start_hidden.vbs" /sc hourly /mo 1 /f
copy /y "D:\code\redian\scripts\win\start_hidden.vbs" "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\RedianMonitor_AutoStart.vbs"
echo --- verify ---
schtasks /query /tn RedianMonitor_Watchdog
dir "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\RedianMonitor_AutoStart.vbs"
