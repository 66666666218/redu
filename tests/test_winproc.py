"""守卫:生产代码里**每一条**子进程都必须不弹黑框(2026-11-17)。

## 背景(实测,不是推断)

用户口径:「**能否在运行的时候终端不要在显示出来在后台**」。

服务本身早就是无窗口的(`pythonw.exe` + `start_hidden.vbs`),但**子进程**会弹窗:
`pythonw.exe` 不分配控制台 ⇒ 它 `subprocess` 起任何**控制台子系统**程序
(`powershell` / `cmd` / `docker` / `adb` / `netstat` / `taskkill` / `python.exe`)时,
Windows 会给子进程**新建一个可见控制台**。

本机实测对照(只改 `creationflags` 一个变量):

| 调用 | 新出现的可见窗口 |
|---|---|
| `Popen([...])` | `CASCADIA_HOSTING_WINDOW_CLASS [C:\\WINDOWS\\SYSTEM32\\cmd.exe]` |
| `Popen([...], creationflags=CREATE_NO_WINDOW)` | **无** |

⚠️ 这类调用在**采集作业**里很密(`chain_health` 每小时、每轮 `kill_stale_browsers`、
每次扒 8080 端口),所以用户看到的是"**终端老是闪**"。

## 为什么用 AST 而不是查源码文本

本仓那条老教训([[source-guard-by-ast-not-substring]]):**子串守卫会自噬** ——
只要模块 docstring / 注释里出现同一串字符,文本匹配就假红;而用脚本 `replace`
修又可能把已有内容吞掉。所以这里 `ast.parse` 真实调用,只看**关键字参数里有没有**
`creationflags=<winproc.flags()>`。
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
#: 计划任务直接拉起的脚本也归这条规则管(`pythonw.exe` 起的 ⇒ 同一机理)。
WIN = ROOT / "scripts" / "win"

#: 真正会"起进程"的那几个入口。`subprocess.DEVNULL` 之流不是 Call,自然不会被命中。
_SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}


def _is_winproc_flags(node: ast.AST) -> bool:
    """关键字值是 `winproc.flags()`(允许带参数)。"""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "flags"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "winproc")


def _subprocess_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """该文件里 `subprocess` 的**模块别名**与**直接导入的函数名**。

    ⚠️ 必须按别名走 AST,不能只看 `.run` 这个名字 —— `asyncio.run(coro)` 也叫 `run`
    (`app/utils/asyncrun.py` 就是),按名字匹配会把它当子进程(第一版就这么假红了)。
    """
    mods: set[str] = set()
    funcs: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "subprocess":
                    mods.add(a.asname or "subprocess")
        elif isinstance(n, ast.ImportFrom) and (n.module or "") == "subprocess":
            for a in n.names:
                if a.name in _SPAWNERS:
                    funcs.add(a.asname or a.name)
    return mods, funcs


def _spawn_calls(tree: ast.AST) -> list[ast.Call]:
    mods, funcs = _subprocess_aliases(tree)
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr in _SPAWNERS \
                and isinstance(f.value, ast.Name) and f.value.id in mods:
            out.append(n)
        elif isinstance(f, ast.Name) and f.id in funcs:
            out.append(n)
    return out


def _py_files() -> list[Path]:
    files = [p for p in APP.rglob("*.py") if "__pycache__" not in p.parts]
    files += [p for p in WIN.glob("*.py") if "__pycache__" not in p.parts]
    return sorted(files)


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_每一条子进程都传了_creationflags(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = []
    for call in _spawn_calls(tree):
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        if not _is_winproc_flags(kw.get("creationflags")):
            bad.append(getattr(call, "lineno", "?"))
    assert not bad, (
        f"{path.relative_to(ROOT)} 有子进程没传 `creationflags=winproc.flags()`"
        f"(行 {bad})—— 它会在用户桌面弹一个终端窗口。见 app/utils/winproc.py"
    )


def test_flags_在_windows_上带_CREATE_NO_WINDOW() -> None:
    from app.utils import winproc

    if os.name != "nt":
        pytest.skip("只在 Windows 上有意义")
    assert winproc.flags() & winproc.CREATE_NO_WINDOW
    assert winproc.CREATE_NO_WINDOW == 0x08000000, "winbase.h 里的值是 0x08000000"


def test_flags_能合并调用方自己的标志() -> None:
    """`extra` 存在是为了避免同一个调用里传两次 `creationflags=`(Python 会直接报错)。"""
    from app.utils import winproc

    merged = winproc.flags(0x00000008)          # DETACHED_PROCESS
    assert merged & 0x00000008
    if os.name == "nt":
        assert merged & winproc.CREATE_NO_WINDOW