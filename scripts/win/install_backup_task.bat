@echo off
rem Register the hourly offsite-backup task. The python script self-limits
rem to one push per day (marker file data/.offsite_backup_done).
rem Prerequisite: this machine's ~/.ssh/id_ed25519 pubkey is authorized on the
rem VPS (see doc/监控体系最优方案.md P1). Keep this file ASCII-only.
schtasks /create /tn RedianMonitor_Backup /tr "C:\Python314\pythonw.exe D:\code\redian\scripts\win\offsite_backup.py" /sc hourly /mo 1 /f
schtasks /query /tn RedianMonitor_Backup
