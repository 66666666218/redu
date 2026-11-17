"""Windows 子进程**不弹黑框**:给 `subprocess` 加 `creationflags`(2026-11-17)。

## 为什么要单列一个模块

用户口径:「**能否在运行的时候终端不要在显示出来在后台**」。

原来以为服务已经是无窗口的 —— 看门狗用 `pythonw.exe` 起 uvicorn、计划任务走 `wscript` +
`start_hidden.vbs`,确实**没有一个常驻的黑窗口**。但实测(本机 PowerShell 枚举
`EnumWindows`,只在**可见**窗口里做前后对照)发现:**终端会一闪而过**。

```
pythonw.exe(无控制台) → subprocess.Popen(["powershell", ...])
  ⇒ 可见窗口 CASCADIA_HOSTING_WINDOW_CLASS ["C:\\WINDOWS\\SYSTEM32\\cmd.exe"]
  ⇒ 关掉子进程,窗口消失
```

机理:`pythonw.exe` 自己**不分配控制台**(`PEP 397`),于是任何 `subprocess` 起 **控制台子系统**
程序(`powershell` / `cmd` / `docker` / `adb` / `netstat` / `taskkill` / 另一个 `python.exe`)
时,Windows 会**为子进程新建一个控制台** —— 默认就是**可见**的。所以"服务看不见窗口"与
"子进程弹窗"是**两件事**,前者做到了不代表后者。

对照(同一测试,只改一个变量):

| 子进程 | 新出现的可见窗口 |
|---|---|
| `Popen([...])`(现状) | `CASCADIA_HOSTING_WINDOW_CLASS [cmd.exe]` |
| `Popen([...], creationflags=CREATE_NO_WINDOW)` | **无** |

## 为什么不是别的办法

- ❌ `pythonw.exe` 换掉 `python.exe`:父进程本来就已是 `pythonw`,解决不了子进程;
- ❌ `powershell -WindowStyle Hidden`:那是**进程起来之后再隐藏**,窗口仍会**出现一瞬**
  (闪烁),而且拦不住 `cmd`/`adb`/`docker` 这些非 PowerShell 的子进程;
- ✅ `CREATE_NO_WINDOW`(0x08000000)是**创建时**就要求"别分配控制台" ⇒ **根本不会出现**。

## 边界

- 只在 Windows 生效;其它平台 `flags()` 返回 `0`(非 0 的 `creationflags` 在 POSIX 上
  会直接 `ValueError`)。`set_window_title` 在非 Windows 上是空操作。
- **GUI 程序**(Edge/Chrome)加不加都一样(它本来就不建控制台),但为了让守卫能写成
  一条**没有例外的规则**(见 `tests/test_winproc.py`),这里一律照加。
- ⚠️ 它**不改变**子进程的行为与输出:管道(`capture_output`)照旧,退出码照旧。
"""
from __future__ import annotations

import sys

#: `winbase.h`:不为子进程分配控制台窗口。
CREATE_NO_WINDOW = 0x08000000


def flags(extra: int = 0) -> int:
    """要传给 `subprocess.run/Popen(creationflags=...)` 的值。

    `extra` 用来把调用方自己的标志位合并进来(例如 `DETACHED_PROCESS`),
    避免同时传两个 `creationflags` 关键字。
    """
    base = int(extra or 0)
    if sys.platform == "win32":
        return base | CREATE_NO_WINDOW
    return base