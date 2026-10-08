"""夸克清理计划(`classify`)的守卫(2026-10-08)。全部离线。

`classify` 是**纯函数**(不碰网络、不碰盘),所以这里能把每条判据钉死。
它决定的是**要删什么** —— 本仓唯一一个"判错了就真删了"的地方,所以每条都要有守卫。

## 三层,一层比一层宽,但都有明确的反面用例
| 判据 | 删? | 反面(不删)由谁守 |
|---|---|---|
| **强模式**(名字就是网盘/加群指令) | 删,单家也删 | — |
| **弱模式** + 横跨 ≥2 个资源包 | 删 | `test_弱模式只出现在一个包里只报告不删` |
| **跨目录重复**(名字+大小一样) | 删旧的、留新的 | `test_小体积重复不参与` |
| **保护名单**(分享链 first_fid / 简介.doc) | **永不删** | 两条专属用例 |

⚠️ 最要紧的一条是**没扫完**:`build_plan` 会把 `complete=False` 带出来,而
`quark_dup_apply.py` 默认**拒绝执行** —— 因为"只出现在一个包里"这个判断
在没扫完时**是假的**(没扫到的家里可能也有一份)。
"""
from __future__ import annotations

from app.services.quark_dup import (
    DUP_MIN_BYTES,
    classify,
)

#: ⚠️ 三个月**都在 10 月** —— 用 `_0929` 当"旧的那个"会踩到范围闸(9 月不在范围内),
#: 于是"跨目录重复"这条**看着在测、其实什么都没测**。测试当场抓到了这个。
ORDER = ["redian监听_1007", "redian监听_1004", "redian监听_1001"]
SEP_HOME = "redian监听_0929"


def _f(name: str, fid: str, size: int, home: str, path: str = "x") -> dict:
    return {"home": home, "path": path, "name": name, "size": size, "fid": fid}


def _run(files, prot_fids=frozenset(), prot_names=frozenset(), prefix="10"):
    return classify(files, set(prot_fids), set(prot_names),
                    mmdd_prefix=prefix, home_order=ORDER)


def _names(plan, key="delete") -> set[str]:
    return {d["name"] for d in plan[key]}


# ---------------------------------------------------------------------------
# 引流:强模式 / 弱模式
# ---------------------------------------------------------------------------


def test_强模式单家也删() -> None:
    """★ 真样本:`切勿卸载网盘,后续资源会同步更新到本网盘.txt`(60 字节)——
    这种名字本身就是一条**网盘使用指令**,资源本体不可能叫这个。"""
    plan = _run([_f("切勿卸载网盘，后续资源会同步更新到本网盘.txt", "A", 60, ORDER[0])])
    assert _names(plan) == {"切勿卸载网盘，后续资源会同步更新到本网盘.txt"}


def test_另一个真样本_先保存在观看更高清() -> None:
    """真样本(目录名,但归类逻辑与文件一致):`先保存在观看更高清`。"""
    plan = _run([_f("先保存在观看更高清", "A", 0, ORDER[0])])
    assert _names(plan) == {"先保存在观看更高清"}


def test_弱模式横跨两个包才删() -> None:
    """★ 弱模式**必须**靠"每个包都带一份"这个特征 —— 单看名字是**不敢判**的
    (实测 `字体安装方法说明.docx` 是真资源)。"""
    plan = _run([_f("下载说明.txt", "A", 100, ORDER[0]), _f("下载说明.txt", "B", 100, ORDER[1])])
    assert len(plan["delete"]) == 2, "横跨两个包 ⇒ 两份都删"
    assert plan["review"] == []


def test_弱模式只出现在一个包里只报告不删() -> None:
    """⚠️ 这条**必须**不删:一个包里孤零零的 `使用说明.pdf` 极可能就是资源本体的一部分。
    宁可漏删(用户看得见清单),不可误删(不可逆)。"""
    plan = _run([_f("字体安装方法说明.docx", "A", 14905, ORDER[0])])
    assert plan["delete"] == []
    assert _names(plan, "review") == {"字体安装方法说明.docx"}


def test_名字像广告但不在任何模式里的不动() -> None:
    """反面对照 —— 不然规则会越滚越宽,最后把资源吃掉。"""
    plan = _run([_f("高性价比人生指南-HowToLiveBetter-现代-338页.pdf", "A", 6_285_115, ORDER[0])])
    assert plan["delete"] == [] and plan["review"] == []


# ---------------------------------------------------------------------------
# 跨目录重复
# ---------------------------------------------------------------------------


def test_跨目录重复删旧的留新的() -> None:
    """★ 用户报的那件事:`高性价比人生指南-...pdf` 在 `_0929` 与 `_1004` 各一份。

    留**最近在用的那个家**(`ORDER` 里越靠前越新 ⇒ 留 `_1004`)。
    ⚠️ "留哪份"**必须显式传 `home_order`** —— 写死成按名字排序会看着对但没依据。
    """
    big = DUP_MIN_BYTES + 1
    plan = _run([_f("资源包.pdf", "OLD", big, ORDER[2]), _f("资源包.pdf", "NEW", big, ORDER[1])])
    assert [d["fid"] for d in plan["delete"]] == ["OLD"], f"该删旧家的那份,实际 {plan['delete']}"
    assert "redian监听_1004" in plan["delete"][0]["why"]


def test_小体积重复不参与() -> None:
    """删小的重复省不到空间,却一样承担误删风险 ⇒ 不碰(引流另有一条规则)。"""
    plan = _run([_f("某文件.dat", "A", 1024, ORDER[0]), _f("某文件.dat", "B", 1024, ORDER[1])])
    assert plan["delete"] == []


def test_同一个家里同名同大小不算重复() -> None:
    """父目录不同才算跨目录重复;同一个家里出现两次是另一类问题,不在这里处理。"""
    big = DUP_MIN_BYTES + 1
    plan = _run([_f("资源包.pdf", "A", big, ORDER[0]), _f("资源包.pdf", "B", big, ORDER[0])])
    assert plan["delete"] == []


def test_大小不同不算同一份() -> None:
    big = DUP_MIN_BYTES + 1
    plan = _run([_f("资源包.pdf", "A", big, ORDER[0]), _f("资源包.pdf", "B", big + 7, ORDER[1])])
    assert plan["delete"] == []


# ---------------------------------------------------------------------------
# 保护(一票否决)
# ---------------------------------------------------------------------------


def test_被分享链指着的副本永不删() -> None:
    """★★ 最要紧的保护:删掉它 = **已经发给别人的链接变「已失效」**,发出去就收不回。
    这里强模式也得让路 —— 宁可留一个引流文件,不可弄死一条已发出的链。"""
    plan = _run([_f("切勿卸载网盘，后续资源会同步更新.txt", "SHARED", 60, ORDER[0])],
                prot_fids={"SHARED"})
    assert plan["delete"] == []
    assert plan["protected_left"] and "分享链" in plan["protected_left"][0]["why"]


def test_部分副本被保护时只删没被保护的那些() -> None:
    plan = _run([_f("下载前必看.docx", "SHARED", 10671, ORDER[0]),
                 _f("下载前必看.docx", "FREE", 10671, ORDER[1])],
                prot_fids={"SHARED"})
    assert [d["fid"] for d in plan["delete"]] == ["FREE"]


def test_分享标题级保护_连名字一起护住() -> None:
    """`file_num>1` 的链只给得出 `first_fid`,其余项只能**按名字**保护(名字能连坐,比漏保护好)。"""
    plan = _run([_f("音乐合集.zip", "A", 2_000_000, ORDER[0]),
                 _f("音乐合集.zip", "B", 2_000_000, ORDER[1])],
                prot_names={"音乐合集.zip"})
    assert plan["delete"] == []


def test_我们自己的简介永不删() -> None:
    """刚放进每个资源里的宣传简介 —— 强模式也护不住它,得靠**专属**名单。"""
    plan = _run([_f("简介.doc", "A", 2_976_256, ORDER[0])])
    assert plan["delete"] == [] and plan["review"] == []


# ---------------------------------------------------------------------------
# 范围:只动 10 月的账
# ---------------------------------------------------------------------------


def test_只动指定月份的副本() -> None:
    """用户口径「先删 **10 月份之内**保存进来的」⇒ 9 月的家完全不动。"""
    plan = _run([_f("切勿卸载网盘，后续资源会同步更新.txt", "SEP", 60, SEP_HOME)],
                prefix="10")
    assert plan["delete"] == [], "9 月的家不该被这次清理碰到"
    plan10 = _run([_f("切勿卸载网盘，后续资源会同步更新.txt", "OCT", 60, "redian监听_1004")],
                  prefix="10")
    assert len(plan10["delete"]) == 1


# ---------------------------------------------------------------------------
# 执行器:只走回收站 + 一批失败不中断
# ---------------------------------------------------------------------------


def test_删除只走回收站_永不彻底删() -> None:
    """★ 「彻底删」和「回收站」的差别是**能不能捞回来**。
    判据错了还能从回收站捡回来,彻底删就真没了 —— 盘商回收站是现成的兜底,没有理由不用。"""
    from app.services.quark_dup import apply_plan

    seen: list = []

    class _Q:
        def delete_files(self, fids, *, to_recycle=True):
            seen.append((list(fids), to_recycle))
            return {}

    plan = {"delete": [{"fid": "A", "size": 10}, {"fid": "B", "size": 20}]}
    out = apply_plan(_Q(), plan)
    assert seen == [(["A", "B"], True)], f"必须带 to_recycle=True,实际 {seen}"
    assert out["deleted"] == 2 and out["bytes"] == 30 and out["failed"] == []


def test_一批删除失败要记下来并继续下一批() -> None:
    """⚠️ 静默吞掉失败 = "看起来删干净了"。失败的批必须进 `failed` 被报出来。"""
    from app.services.quark_dup import apply_plan

    class _Q:
        def __init__(self):
            self.n = 0

        def delete_files(self, fids, *, to_recycle=True):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("夸克接口 500")
            return {}

    plan = {"delete": [{"fid": f"F{i}", "size": 1} for i in range(3)]}
    out = apply_plan(_Q(), plan, batch=1)
    assert out["deleted"] == 2, "后两批该继续执行"
    assert len(out["failed"]) == 1, "失败的那批必须被记下来"
