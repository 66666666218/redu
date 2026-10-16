@echo off
rem 小红书 CDP 窗口的**登录自启**(2026-10-16,用户问「关机再开机能否自启」)。
rem
rem ⚠️ 逻辑在 app/services/xhs_cdp_source.ensure_running(**单一出处**):
rem    起的是**普通 Edge**(只带 --remote-debugging-port,**没有** --enable-automation)
rem    ⇒ navigator.webdriver = False ✓ 与"人开的窗口"**等价**;
rem    已经在跑就**不重复起**(幂等,重复开会抢同一个档案、互相踢登录态)。
rem
rem ⚠️ **这条其实可选**:presence 那 6 轮(1:10/5:10/9:10/13:10/17:10/21:10)在发现
rem    CDP 不可达时会**自己把窗口拉起来** ⇒ 开机后不用管,第一轮自己就有了。
rem    注册它只是为了"开机立刻有窗口",以及提前把那一次启动的等待挪出作业时间。
rem
rem 注册(登录时触发,管理员 CMD 跑一次即可;删掉:schtasks /delete /tn RedianXhsCdp /f):
rem   schtasks /create /tn "RedianXhsCdp" /tr "D:\code\redian\scripts\win\xhs_cdp_autostart.bat" /sc onlogon /rl highest
cd /d D:\code\redian
"C:\Python314\pythonw.exe" tools\xhs_cdp.py launch >> data\xhs_cdp_autostart.log 2>&1
