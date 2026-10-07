"""`ADDITIONS` 里**不许有重复的表名键**(2026-10-07 实测踩到)。

## 为什么值得单独守一条
Python 的字典字面量**重复键取后者、且不报错** —— 我当天往 `database.py` 别处又写了一个
`"cross_platform_accounts": [...]`,于是**新加的两列被静默吃掉**,`_migrate()` 根本没建它们。
表现是"迁移跑了、也没报错,但列不在",而依赖那两列的代码会以各种奇怪方式失效
(我这次是 `next_scan_after` 读不到 → 自适应降频形同虚设)。

⚠️ 这与本仓那条铁律同源:**静默失败 = 假成功**。字典重复键是最隐蔽的一种 ——
连 lint 都不一定报。所以用 AST 静态查。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ADDITIONS_没有重复键() -> None:
    src = (ROOT / "app" / "db" / "database.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found: list[tuple[str, list[str]]] = []
    for node in ast.walk(tree):
        # ⚠️⚠️ **`AnnAssign` 也要管** —— `ADDITIONS: dict[str, list[str]] = {…}`
        # 是**带类型注解**的赋值,只 walk `ast.Assign` 会**一个都看不到**,
        # 于是这条守卫是**假绿**(我第一版正是如此,变异验证才发现)。
        target_names: list[str] = []
        value = None
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value = node.value
        if "ADDITIONS" not in target_names or not isinstance(value, ast.Dict):
            continue
        node = type("_N", (), {"value": value})()
        seen: dict[str, int] = {}
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                seen[k.value] = seen.get(k.value, 0) + 1
        found = [(k, []) for k, n in seen.items() if n > 1]
        break
    assert not found, (
        f"`ADDITIONS` 里有重复的表名键:{[k for k, _ in found]} —— "
        f"**后者会静默覆盖前者**,那些列不会建上(实测踩过)。请合并成一项。")
