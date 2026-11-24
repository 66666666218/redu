# -*- coding: utf-8 -*-
"""守卫:`scripts/quark_global_dedupe.py` 的判据(2026-11-18)。

## 为什么这个测试值得存在

那个脚本一次删掉 **2703 份 / 7.32 TiB** 真资源。它的判据是「**同名 + 同字节数
⇒ 同一份文件被存到了不同目录**」,只要判据错一次,删掉的就是**唯一的一份**。
所以把判据抽成纯函数(`build_plan`),在这里用**人工构造的数据**把每个分支都钉死。

`build_plan` 是纯函数(不碰网络、不碰 DB),`scripts/` 下的脚本能被直接 import。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    """按路径加载脚本模块(`scripts/` 不是包)。"""
    path = ROOT / "scripts" / "quark_global_dedupe.py"
    spec = importlib.util.spec_from_file_location("_qgd", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_qgd"] = mod
    spec.loader.exec_module(mod)
    return mod


qgd = _load()


def _f(fid: str, name: str, size: int, *, pdir: str = "p", created: int = 0) -> dict:
    return {"fid": fid, "name": name, "size": size, "pdir": pdir, "created": created}


def test_同名的两份只删一份且留创建更早的():
    plan, stats = qgd.build_plan(
        [_f("a", "课.mp4", 100, pdir="x", created=200),
         _f("b", "课.mp4", 100, pdir="y", created=100)],
        set(), set(), set())
    assert [p["fid"] for p in plan] == ["a"], "应留下 created=100 的那份(b)"
    assert plan[0]["keep_fid"] == "b"


def test_同名字但字节数不同不算重复():
    plan, _ = qgd.build_plan(
        [_f("a", "课.mp4", 100), _f("b", "课.mp4", 101)], set(), set(), set())
    assert plan == [], "字节数不同 ⇒ 很可能是不同转码版本,不能当重复"


def test_名字不同但字节数相同不算重复():
    plan, _ = qgd.build_plan(
        [_f("a", "01.mp4", 100), _f("b", "02.mp4", 100)], set(), set(), set())
    assert plan == [], "同名同大小才算;只按大小判会把不同集数混为一谈"


def test_受保护的一级fid_整组跳过():
    """`pan_owned_shares.first_fid` 命中的文件是我方分享的落点,一份都不能动。"""
    plan, stats = qgd.build_plan(
        [_f("a", "课.mp4", 100), _f("b", "课.mp4", 100)],
        {"b"}, set(), set())
    assert plan == [], "组里有受保护的 fid ⇒ 整组跳过(宁可留,不可错删)"
    assert stats["protected_groups"] == 1


def test_整名等于分享标题也整组跳过():
    plan, _ = qgd.build_plan(
        [_f("a", "课程合集", 100), _f("b", "课程合集", 100)], set(), {"课程合集"}, set())
    assert plan == []


def test_整名在推送台账里也整组跳过():
    plan, _ = qgd.build_plan(
        [_f("a", "推过的资源", 100), _f("b", "推过的资源", 100)], set(), set(), {"推过的资源"})
    assert plan == []


def test_引流文件不动():
    """`is_promo_name` 命中的归引流那条链管,这里不碰。"""
    from app.services import quark_dup as qd

    promo = next((n for n in ("代发", "引流", "推广", "公众号") if qd.is_promo_name(n)), None)
    if promo is None:
        pytest.skip("当前 is_promo_name 没命中这几个裸词(见 junk-files 那条记忆)")
    plan, _ = qgd.build_plan([_f("a", promo, 100), _f("b", promo, 100)], set(), set(), set())
    assert plan == []


def test_三份重复只删两份():
    plan, _ = qgd.build_plan(
        [_f("a", "课.mp4", 100), _f("b", "课.mp4", 100), _f("c", "课.mp4", 100)],
        set(), set(), set())
    assert len(plan) == 2
    assert len({p["keep_fid"] for p in plan}) == 1, "三份只能留一份"


def test_只差副本后缀也算同一份():
    """★ 2026-11-18 新增判据:重复下载会让第二份变成 `课(1).mp4`。

    名字不同但字节数**完全相等**(同一份文件),老判据「名字必须完全相同」
    整批漏掉了 —— 实测 822 组 / 883 GiB。
    """
    plan, _ = qgd.build_plan(
        [_f("a", "课.mp4", 100, created=100), _f("b", "课(1).mp4", 100, created=200)],
        set(), set(), set())
    assert [p["fid"] for p in plan] == ["b"], "应删带后缀那份(b),留原件(a)"
    assert plan[0]["keep_fid"] == "a"


def test_时间戳并列时也要留不带后缀的原件():
    """★ 2026-11-18 实战踩到:`(1)`/`(2)` 和原件的 `created` **完全相同**
    (`超凡蜘蛛侠2…2160p.mp4` 三份都是 `1786689946501`),`sorted` 是稳定排序
    ⇒ 谁当 `keep` 取决于输入顺序(快照里 `(1)` 恰好排在原件前面)。

    旧计划 8205 份里 **6666 份的 `keep_name` 带着副本后缀**。
    删仍然是安全的(剥完同 base),但「留最老的原件」这个目标失效了 ——
    而回收站捞回时按 `keep_name` 认哪一份。
    """
    plan, _ = qgd.build_plan(
        [_f("b", "课(1).mp4", 100, created=0), _f("c", "课(2).mp4", 100, created=0),
         _f("a", "课.mp4", 100, created=0)],
        set(), set(), set())
    assert {p["keep_fid"] for p in plan} == {"a"}, "时间戳并列时也必须留原件"
    assert plan[0]["keep_name"] == "课.mp4"
    assert sorted(p["fid"] for p in plan) == ["b", "c"]

    plan, _ = qgd.build_plan(
        [_f("b", "课(1).mp4", 100, created=777), _f("a", "课.mp4", 100, created=777)],
        set(), set(), set())
    assert [p["keep_fid"] for p in plan] == ["a"], "并列时原文在前也要留原件"


def test_原件的创建时间更晚也优先保留():
    """副本后缀本身就说明「它是后来另存的」,比时间戳更可信。"""
    plan, _ = qgd.build_plan(
        [_f("a", "课.mp4", 100, created=900), _f("b", "课(1).mp4", 100, created=100)],
        set(), set(), set())
    assert [p["fid"] for p in plan] == ["b"]
    assert plan[0]["keep_name"] == "课.mp4"


def test_中文括号和副本后缀也能归组():
    for suffix in ("（2）", "- 副本"):
        plan, _ = qgd.build_plan(
            [_f("a", "资料.zip", 100, created=100),
             _f("b", f"资料{suffix}.zip", 100, created=200)],
            set(), set(), set())
        assert [p["fid"] for p in plan] == ["b"], f"{suffix} 没被认成副本后缀"


def test_全是副本后缀的组不删():
    """没有不带后缀的原件 ⇒ 认不出哪份是原件,也排除不掉「名字本来就带括号」。

    `广告(1).mp4` / `广告(2).mp4` 若是两份**不同**资源,名字剥完只是巧合。
    """
    plan, _ = qgd.build_plan(
        [_f("a", "课(1).mp4", 100), _f("b", "课(2).mp4", 100)], set(), set(), set())
    assert plan == []


def test_组内名字完全相同时不受无原件闸约束():
    """★ 2026-11-18 回归:上一轮加「必须有不带后缀的原件」那道闸时一刀切成
    「`base` 不在 `names` 就跳过」,把**名字里自带编号的真资源**整组放过了。

    `42期预测3（2）.mp4` 两份同名同字节数 —— 它到底是「副本」还是「第 2 讲」
    **不影响判断**:同一个名字 + 同样的字节数,就是同一份文件被存了两遍。
    在加副本后缀判据之前,这些组本来能正常去重。实测 21 组 / 28 份 / 25.06 GiB。
    """
    plan, _ = qgd.build_plan(
        [_f("a", "42期预测3（2）.mp4", 100, created=100),
         _f("b", "42期预测3（2）.mp4", 100, created=200)],
        set(), set(), set())
    assert [p["fid"] for p in plan] == ["b"], "同名同字节数必须能去重"
    assert plan[0]["keep_fid"] == "a"
    assert plan[0]["keep_name"] == "42期预测3（2）.mp4"

    plan, _ = qgd.build_plan(
        [_f("a", "14.长期股权投资（2）.mp4", 100),
         _f("b", "14.长期股权投资（2）.mp4", 100),
         _f("c", "14.长期股权投资（2）.mp4", 100)],
        set(), set(), set())
    assert len(plan) == 2, "三份同名同大小留一份"


def test_名字只差后缀且无原件时仍然不删():
    """这条闸的原意没有放宽:名字**不一样**且认不出原件时,依然跳过。"""
    plan, _ = qgd.build_plan(
        [_f("a", "42期预测3（2）.mp4", 100), _f("b", "42期预测3（2）(1).mp4", 100)],
        set(), set(), set())
    assert plan == []


def test_副本后缀组也受保护清单约束():
    plan, stats = qgd.build_plan(
        [_f("a", "课.mp4", 100), _f("b", "课(1).mp4", 100)],
        {"b"}, set(), set())
    assert plan == [], "带后缀那份是我方分享落点 ⇒ 整组跳过"
    assert stats["protected_groups"] == 1


def test_剥后缀不会误伤带括号的真名字():
    """`变形金刚(2007).mp4` 的括号是**年份**,不是副本序号。"""
    assert qgd.strip_copy_suffix("变形金刚(2007).mp4") == "变形金刚(2007).mp4", \
        "四位数字不是副本序号,不能剥"
    assert qgd.strip_copy_suffix("课(1).mp4") == "课.mp4"
    assert qgd.strip_copy_suffix("课(12).mp4") == "课.mp4"
    assert qgd.strip_copy_suffix("课（3）.zip") == "课.zip"
    assert qgd.strip_copy_suffix("课 - 副本.rar") == "课.rar"
    assert qgd.strip_copy_suffix("课.mp4") == "课.mp4", "没有后缀要原样返回"
    assert qgd.strip_copy_suffix("第1集.mp4") == "第1集.mp4"


def test_刮号在名字中间的不能剥():
    """`课(1) 第二讲.mp4` 的括号不在尾部,不是副本后缀。"""
    assert qgd.strip_copy_suffix("课(1) 第二讲.mp4") == "课(1) 第二讲.mp4"


def test_单份不成组():
    plan, _ = qgd.build_plan([_f("a", "唯一.mp4", 100)], set(), set(), set())
    assert plan == []


def test_扫描入口必须同时有两个视图():
    """★ 2026-11-17 之前八轮清理全部做错的那个点,以及补上的另一半。

    | 视图 | `pdir_fid` | `size:desc` 窗口下限 |
    |---|---|---|
    | 全盘 | `""` | 1.137 GiB |
    | 根目录 | `"0"` | 0.583 GiB |

    * 只扫根目录 ⇒ 漏掉深层的大文件(19 万条看不到);
    * 只扫全盘 ⇒ **漏掉 0.33~1.14 GiB 这段**,而旧判据的 3.36 TiB 全在里面。

    两个窗口互相独立,所以**必须取并集**。砍掉任何一个都会静默漏一大块。
    """
    pd = {p for _, p, _ in qgd.VIEWS}
    assert pd == {"", "0"}, f"两个视图必须都在(空串=全盘,0=根目录),现在是 {pd}"
    for tag, pdir, sorts in qgd.VIEWS:
        assert len(sorts) >= 2, f"{tag}视图只跑 {sorts} —— 单键只看得见 10000 条"
        assert len(set(sorts)) == len(sorts), f"{tag}视图里有重复的排序键"
    assert "size:desc" in qgd.VIEWS[0][2], "全盘视图的主力键是 size:desc"


def test_删除永远进回收站():
    src = (ROOT / "scripts" / "quark_global_dedupe.py").read_text(encoding="utf-8")
    assert "to_recycle=True" in src
    assert "to_recycle=False" not in src, "彻底删除不可逆,这个脚本不许用"


def test_扫描要跑多个排序键():
    """★ 单个排序键的翻页上限是 **offset 10000**,与 `_size` 无关。

    实测 `_size=50` 时 page 202(offset 10100)能取、page 205(10250)就 `61004`。
    而**不同键看到的 10000 条是不同的**,多键取并集才看得见全盘:
    实测 7 个键把唯一文件数从 10000 抬到 **52156**。

    谁把 `SORTS` 收成一个键,清理就会退回「只清皮毛」。
    """
    assert len(qgd.SORTS) >= 5, f"排序键太少({qgd.SORTS}),覆盖会大幅缩水"
    assert "size:desc" in qgd.SORTS, "按体积倒序是主力键"
    assert "file_name:desc" in qgd.SORTS, "实测这个键带来的新增最多"
    assert len(set(qgd.SORTS)) == len(qgd.SORTS), "重复的键只会白跑一遍"


# ---------------------------------------------------------------------------
# 分享树保护(2026-11-24)
#
# ⚠️ 这一组钉的是**一次差点发生的交付事故**:`first_fid` 只是分享的**一级** fid,
# 而盘上资源是「家 / 资源目录 / 子目录 / … / 文件」的深层结构 ⇒ 子文件**一个都不在**
# `share_fids` 里。实测 2 号盘 66 份待删项 **66/66 全在存活分享的目录树内**
# (其中 15 份跨分享),按老判据 `protected_groups=0` —— 删了就是**交付缺文件的分享**。
# ---------------------------------------------------------------------------

def test_子文件落在分享树内也受保护():
    """★ 本组最重要的一条:`first_fid` 指向目录,里面的文件没一个 fid 等于它。"""
    # 树:shareDir ← sub ← file ;`shareDir` 是我方分享一级 fid
    parent_of = {"sub": "shareDir", "file": "sub", "other": "elsewhere"}
    plan, stats = qgd.build_plan(
        [_f("file", "课.mp4", 100), _f("other", "课.mp4", 100)],
        {"shareDir"}, set(), set(), parent_of=parent_of)
    assert plan == [], "组里有一份在分享树内 ⇒ 整组跳过"
    assert stats["protected_groups"] == 1


def test_两棵树都在分享外才允许删():
    #: 父链必须**走到 "0"** 才算判清(根/家);停在半路 = 断链 = 受保护
    parent_of = {"a1": "d1", "d1": "0", "b1": "d2", "d2": "0"}
    plan, stats = qgd.build_plan(
        [_f("a1", "课.mp4", 100, created=200),
         _f("b1", "课.mp4", 100, created=100)],
        {"shareDir"}, set(), set(), parent_of=parent_of)
    assert [p["fid"] for p in plan] == ["a1"], "两棵都不在分享树内 ⇒ 正常去重"
    assert plan[0]["keep_fid"] == "b1"
    assert stats["protected_groups"] == 0


def test_溯源断链时按受保护处理():
    """查不到父 fid ⇒ **判不清 ⇒ 不删**。这条比「多删」重要得多。"""
    parent_of = {"a1": "d1", "b1": "0"}   # a1 的父 d1 无父(断链);b1 能走到根
    plan, _ = qgd.build_plan(
        [_f("a1", "课.mp4", 100, created=200), _f("b1", "课.mp4", 100, created=100)],
        {"shareDir"}, set(), set(), parent_of=parent_of)
    assert plan == [], "a1 断链 ⇒ 受保护;组里有一份受保护就整组跳过"


def test_in_share_tree_走到根就不算():
    parent_of = {"a": "d", "d": "0"}          # 走到 "0" = 家/根
    assert qgd.in_share_tree("a", {"shareDir"}, parent_of) is False


def test_in_share_tree_一级fid直接命中():
    assert qgd.in_share_tree("shareDir", {"shareDir"}, {}) is True


def test_in_share_tree_父链上有一级fid就算():
    parent_of = {"f": "sub", "sub": "shareDir", "shareDir": "0"}
    assert qgd.in_share_tree("f", {"shareDir"}, parent_of) is True


def test_in_share_tree_防环():
    parent_of = {"a": "b", "b": "a"}          # 人为造环,不许死循环
    assert qgd.in_share_tree("a", {"zzz"}, parent_of) is True   # 环 ⇒ 判不清 ⇒ 受保护


def test_不传parent_of时退化成只看一级fid():
    """没有树信息时保持老行为(便于纯单测);一旦传了就必须覆盖深层。"""
    plan, _ = qgd.build_plan(
        [_f("a", "课.mp4", 100, created=200), _f("b", "课.mp4", 100, created=100)],
        {"zzz"}, set(), set())
    assert [p["fid"] for p in plan] == ["a"]


def test_实跑必须传parent_of_源码级守卫():
    """★ 判据对了但 `main()` 忘了传 = 等于没修。

    与判据本身分开钉,是因为「函数是好的、调用点漏了」在本仓栽过多次。
    """
    src = (ROOT / "scripts" / "quark_global_dedupe.py").read_text(encoding="utf-8")
    assert "parent_of=parent_of" in src, "main() 必须把 parent_of 传给 build_plan"
    assert "/1/clouddrive/file/info" in src, "深层溯源要调 file/info 补父子边"
    assert "in_share_tree(p[\"fid\"]" in src, "落盘前必须再独立复核一次"
