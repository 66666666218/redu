"""**转存前必须先查资源库**的源码级守卫(2026-10-19,用户口径「资源库有的就不再新转存」)。

## 为什么用 AST 而不是 grep

同一条纪律写两处、一处先修另一处照旧,是本仓的母题(抖音那条护栏只加在
`pan_discovery` 的教训)。而**按子串查会自噬**:注释里提到 `transfer_and_share`
也会被算成调用点(这个坑本仓栽过多次)。⇒ 只认**真实的 Call 节点**,并定位到
它所在的函数。

## 判据

`app/services/` 下每一次 `transfer_and_share(...)` / `transfer_pan_url(...)`,
它所在的那个函数里**必须**出现 `reuse_if_have(...)` 或 `already_have(...)`;
四类"分发器/客户端定义"文件例外(它们自己就是转存实现或被统一入口调用):
`pan_discovery.py` / `quark_transfer.py` / `baidupan_transfer.py` / `xunlei_transfer.py`。
"""
import ast
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"

#: 转存调用(方法名或函数名)
TARGETS = {"transfer_and_share", "transfer_pan_url"}
#: 资源库查询入口
REUSE = {"reuse_if_have", "already_have"}
#: 这些文件是"转存实现/统一分发器",不要求在本文件内自查
ALLOW = {"pan_discovery.py", "quark_transfer.py", "baidupan_transfer.py", "xunlei_transfer.py"}


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _enclosing_func(tree: ast.AST, lineno: int):
    """最内层包含该行的函数(嵌套函数取更靠内的那个)。"""
    best = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(n, "end_lineno", n.lineno)
            if n.lineno <= lineno <= end:
                if best is None or n.lineno >= best.lineno:
                    best = n
    return best


def _functions_with_calls(tree: ast.AST) -> list[tuple[ast.AST, str, list[ast.Call]]]:
    """(所在函数, 调用名, 所有调用节点) —— 同一函数内同名调用合并。"""
    out: dict[tuple[int, str], tuple[ast.AST, str, list[ast.Call]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in TARGETS:
            continue
        fn = _enclosing_func(tree, node.lineno)
        key = (getattr(fn, "lineno", 0), name)
        if key not in out:
            out[key] = (fn, name, [])
        out[key][2].append(node)
    return list(out.values())


def test_every_transfer_call_site_checks_library_first() -> None:
    problems: list[str] = []
    for path in sorted(SERVICES.rglob("*.py")):
        if "__pycache__" in path.parts or path.name in ALLOW:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for fn, name, calls in _functions_with_calls(tree):
            if fn is None:
                problems.append(f"{path}:{calls[0].lineno} 模块级 {name}() 没有资源库前置检查")
                continue
            called = {_call_name(c) for c in ast.walk(fn) if isinstance(c, ast.Call)}
            if not (called & REUSE):
                problems.append(
                    f"{path}:{calls[0].lineno} {fn.name}() 调了 {name}() 却没先查资源库")
    assert not problems, (
        "转存前必须先查资源库(三盘互通):\n  " + "\n  ".join(problems))


def test_scheduler_registers_self_check() -> None:
    """自检作业必须真的排进调度器(光有模块 = 没人调用)。"""
    src = (SERVICES / "scheduler.py").read_text(encoding="utf-8")
    assert "pan_self_check_tick" in src and '"pan_self_check"' in src, \
        "scheduler.build_jobs 里没有注册网盘自检"
