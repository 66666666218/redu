"""CHANGELOG 完整性守卫(2026-10-05)。

⚠️ **为什么值得为一份文档写测试**:本仓的 `CHANGELOG` 被"预置新条目"的写法**反复静默截断**。
根因是一行看着没问题的代码:

```python
open(P, "w").write(entry + open(P).read())   # ← open(...,"w") 在求值期就把文件清空了
```

`open(P, "w")` **在右边的表达式求值之前**就截断了文件,于是 `open(P).read()` 拿到**空串**,
结果"追加"变成了"只剩这一条"。它**不报错、不抛异常、测试全绿** —— 我从 `4123298` 起连着
这么写了好几轮,条目数一路 14→1→2→3→4,几百条历史**悄无声息地没了**,直到偶然
`git checkout CHANGELOG` 打出 4 条才被发现(最后靠 `bb2c1b4` 才拼回来)。

静态检查抓不到(语法没错)、跑代码也看不出来(写得"成功"了)—— 只有**对结果做断言**能拦住。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

_RE_ENTRY = re.compile(r"^- \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]")


def _entries() -> list[str]:
    path = Path(__file__).resolve().parents[1] / "CHANGELOG"
    text = io.open(path, encoding="utf-8").read()
    return [ln[:16] for ln in text.split("\n") if _RE_ENTRY.match(ln)]


class TestChangelogNotTruncated:
    def test_条目数不能塌陷(self) -> None:
        """★ **这条就是用来抓"预置截断"的**。

        截断的特征极其鲜明:**条目数从几百骤降到个位数**(实测 14 → 1)。
        取 400 作阈值(当前 488)——正常增删都在几十条量级内波动,**不可能**跌破它;
        而一旦有人再用 `open(P,"w").write(x + open(P).read())` 那种写法,**当场变红**。
        """
        n = len(_entries())
        assert n >= 400, (
            f"CHANGELOG 只有 {n} 条,正常应在 480 上下 —— "
            "**极可能又被「预置新条目」的写法截断了**。"
            "正确姿势:先把整个文件读进变量(`old = io.open(P).read()`),再开写"
            "(`io.open(P,'w').write(entry + old)`);"
            "**绝不能**写成 `open(P,'w').write(entry + open(P).read())` —— "
            "`open(...,'w')` 在求值期就清空了文件,右边的 `.read()` 只会拿到空串。"
        )

    def test_顶部是最近写的_且最近若干条时间不倒退(self) -> None:
        """顶部区域是**我自己刚写的**那几条,这里必须严格不倒退(历史深处有 17 处
        早期写乱的老逆序,属既有事实、不在此断言范围内)。"""
        top = _entries()[:30]
        assert top, "一条条目都没有?文件被清空了"
        bad = [(top[i], top[i + 1]) for i in range(len(top) - 1) if top[i] < top[i + 1]]
        assert not bad, f"顶部条目时间倒退了:{bad}"

    def test_文件以条目开头_没有被塞进前言(self) -> None:
        path = Path(__file__).resolve().parents[1] / "CHANGELOG"
        first = io.open(path, encoding="utf-8").read().split("\n")[0]
        assert _RE_ENTRY.match(first), (
            f"首行不是条目,像被插了别的东西(开头空行/标题都会让后续 preprend 出错):{first[:80]!r}")
