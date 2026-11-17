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


def test_扫描入口用的是全盘视图而不是根目录():
    """★ 这是 2026-11-17 之前八轮清理全部做错的那个点。

    `pdir_fid` 为 `"0"` 只是**根目录**(窗口 10200 条),空字符串才是**全盘**
    (1094602 个文件)。谁把这里改成 `"0"`,清理就会重新变回只清皮毛。
    """
    src = (ROOT / "scripts" / "quark_global_dedupe.py").read_text(encoding="utf-8")
    assert '"pdir_fid": ""' in src, "全盘扫描必须传 pdir_fid 为空字符串"
    assert '"pdir_fid": "0"' not in src, "根目录窗口只有 10200 条,会漏掉 19 万条"


def test_删除永远进回收站():
    src = (ROOT / "scripts" / "quark_global_dedupe.py").read_text(encoding="utf-8")
    assert "to_recycle=True" in src
    assert "to_recycle=False" not in src, "彻底删除不可逆,这个脚本不许用"