' Run app_watchdog.bat with no visible window (used by scheduled tasks).
' Keep this file ASCII-only; path must match the repo location.
CreateObject("WScript.Shell").Run """D:\code\redian\scripts\win\app_watchdog.bat""", 0, False
