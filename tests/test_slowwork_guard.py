"""**慢活不许在写事务里做**的源码级守卫(2026-10-06)。

## 为什么要有这个文件
这是本项目**复发过三次**的同一个坑:
  ① `quark_kouling` 整轮跑模拟器(8 条 × 15–20 秒 ≈ 2.7 分钟)把库占死 ——
     14:00 那一分钟里 `bili_account_scan` / `xunlei_sync` / `xunlei_group`
     **三个作业同时**报 `database is locked`(它们 `busy_timeout` 只有 30 秒);
  ② `pan_discovery` 转存 13 条(每条 1–3 秒,可超 30 秒)——
     `11:34:30`(正好是本作业时段)`alert_fixed_time` / `collect_tick`
     的「作业心跳写入失败」成批出现;
  ③ `resource_presence` 转存 3 条 —— **同一个坑的第二条链**,当场审计逮到。

SQLite 是**单写者**:一个写事务里做网络/浏览器/模拟器慢活,别人就写不进去。
三次都是"我自己刚在另一条链上修过、转头又在新的地方犯" ⇒ 光靠注释拦不住,
**得让它在测试里红**。

## 守卫怎么工作
`SLOW_SITES` 显式列出"慢活调用点"。对每个点:
  · 断言**往上一段**能找到 `commit()`(只在本函数内找,不跨 `def`);
  · 断言 `app/services/` 里**没有清单外**的转存调用点 —— 新加一条链必须回到这里登记,
    登记的动作用来强迫"想一想:它该不该在事务外"。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "app" / "services"

# 往回找 commit 的窗口(行)。够宽以容纳实现里那段长注释,又窄到不会误取远处的东西。
# ⚠️ 为什么是 30 而不是 10:`quark_kouling` 的 `resolve` 调用点前面垫着 **20 行注释**
# (记录模拟器那次踩坑),commit 在 21 行之外 —— 窗口开小了会把**已经修对的地方**判红,
# 那种"假红"最后一定是靠把守卫删掉来收场。跨函数的误报由下面的 `def ` 边界挡。
_BACK = 30

#: 已登记的慢活调用点:文件 → 该文件里必须"先 commit 再调用"的标记串。
SLOW_SITES: dict[str, str] = {
    "app/services/pan_discovery.py": "res = transfer_pan_url(",
    "app/services/resource_presence.py": "res = transfer_pan_url(",
    # 夸克口令:循环里跑**模拟器**(15–20 秒/条)。调用处写的是裸 `resolve(`,
    # 本模块内它是 `quark_kouling.resolve`。
    "app/services/quark_kouling.py": "res = resolve(",
    # 公众号入库的**迅雷**补链段(2026-10-06 新增):走统一入口 `transfer_pan_url`,
    # 而迅雷转存要过 captcha 续期,**可能几十秒**。
    "app/services/wechat/_enrich.py": "res = transfer_pan_url(",
}


def _py_files() -> list[Path]:
    return [p for p in SERVICES.rglob("*.py") if "__pycache__" not in p.parts]


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _lines(rel: str) -> list[str]:
    return (ROOT / rel).read_text(encoding="utf-8").splitlines()


def _call_lines(text_lines: list[str], marker: str) -> list[int]:
    """标记串出现、且**不是函数定义**的行号(0 基)。"""
    out = []
    for i, ln in enumerate(text_lines):
        if marker not in ln:
            continue
        if ln.lstrip().startswith(("def ", "async def ")):   # 定义本身不算"调用点"
            continue
        out.append(i)
    return out


class TestEverySlowCallSiteIsRegistered:
    """新加一条"转存/模拟器"链,必须回到 `SLOW_SITES` 登记 —— 强迫过一遍"它在事务外吗"。"""

    def test_没有清单外的转存调用点(self) -> None:
        found: set[str] = set()
        for p in _py_files():
            if any("transfer_pan_url(" in ln and not ln.lstrip().startswith("def ")
                   for ln in p.read_text(encoding="utf-8").splitlines()):
                found.add(_rel(p))
        declared = {f for f, m in SLOW_SITES.items() if "transfer_pan_url(" in m}
        assert found == declared, (
            "`transfer_pan_url` 的调用点与 `SLOW_SITES` 对不上。\n"
            f"  代码里实际有:{sorted(found)}\n"
            f"  清单里登记了:{sorted(declared)}\n"
            "新增/删除调用点都要在这里登记 —— 并确认**调用前释放了写锁**"
            "(慢活不该在事务里,否则别的作业会 `database is locked`)。")


class TestSlowWorkIsOutsideTransaction:
    """★ 核心断言:**每个慢活调用点之前,同一个函数内必须先 `commit()`**。"""

    def _commit_before(self, rel: str, marker: str) -> list[str]:
        """返回"没在调用前 commit"的行描述(空列表 = 全部合格)。"""
        bad: list[str] = []
        lines = _lines(rel)
        for i in _call_lines(lines, marker):
            hit = False
            for j in range(i - 1, max(-1, i - 1 - _BACK), -1):
                s = lines[j].strip()
                # 撞到函数边界就停 —— 上一个函数里的 commit 不能算数
                if s.startswith(("def ", "async def ")):
                    break
                if re.search(r"\bcommit\(\)", s):
                    hit = True
                    break
            if not hit:
                bad.append(f"{rel}:{i + 1}  `{marker.strip()}` 之前 {_BACK} 行内没有 commit()")
        return bad

    def test_每个慢活调用点之前都先提交了(self) -> None:
        bad: list[str] = []
        for rel, marker in SLOW_SITES.items():
            bad += self._commit_before(rel, marker)
        assert not bad, (
            "**慢活(网络/浏览器/模拟器)不许在写事务里做** —— SQLite 是单写者,"
            "整轮占着库会把别的作业饿死(它们的 `busy_timeout` 只有 30 秒),"
            "表现是成批的 `database is locked` / `作业心跳写入失败`。\n"
            "修法:在这个调用点**之前**插一行 `session.commit()`(顺带把已算出的状态落盘)。\n"
            "不合格的调用点:\n  " + "\n  ".join(bad))

    def test_守卫本身能发现反例(self) -> None:
        """⚠️ 反向验证:一个**故意不 commit** 的样本必须被判不合格 —— 否则这守卫是假绿的。"""
        import tempfile
        sample = (
            "def f(session):\n"
            "    for x in y:\n"
            "        res = transfer_pan_url(session, 1, x, None, '')\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sample.py"
            p.write_text(sample, encoding="utf-8")
            lines = sample.splitlines()
            assert _call_lines(lines, "res = transfer_pan_url("), "样本里应当能认出调用点"
            # 直接复用判定逻辑:样本里没有 commit ⇒ 必须被判不合格
            hit = any(re.search(r"\bcommit\(\)", l) for l in lines)
            assert not hit, "样本本来就没有 commit,应当判不合格"
