# -*- coding: utf-8 -*-
"""守卫:`scripts/quark_prune_old.py` 的「快照没扫过 ≠ 里面没有已分享文件」(2026-11-24)。

## 为什么这个测试值得存在

那个脚本按「**重复 + 很老旧 + 没人推**」列夸克盘的删除计划。判「没人推」的
最后一层是**递归比对目录内文件的 fid 有没有被某条我方分享链指着**,离线模式
(`--offline`)下这一步零 API、直接从 `quark_dup_cache` 快照里取。

⚠️ **快照是增量的**(`quark_dup_cache` 的 `remaining_homes`,一轮只扫一部分家)。
老实现 `_fids_from_snapshot` 对「这个家根本没被扫过」和「扫过了但里面没有
已分享文件」**返回同一个空集** ⇒ 整个家的目录全被判成「没人推」⇒
**还在用的资源被列进删除计划**。

所以这里钉死两件事:
1. 没扫过的家返回 `None`(不是 `set()`);
2. 调用方见到 `None` 时**整家保留**,并把它单独计数/打印。

脚本不是包(`scripts/`),但纯函数可以被 import —— 与 `test_quark_global_dedupe`
同一套加载方式。
"""
from __future__ import annotations

import importlib.util
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "quark_prune_old.py"
    spec = importlib.util.spec_from_file_location("_qpo", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qpo = _load()


def _snap(homes: dict) -> dict:
    return {"version": 1, "homes": homes}


def _home(files: list[dict], *, scanned: bool = True) -> dict:
    h = {"fid": "homefid", "depth": 2, "scanned_at": "2026-11-23 04:42"}
    if scanned:
        h["files"] = files
    return h


def _f(path: str, fid: str) -> dict:
    return {"home": "redian监听", "path": path, "name": path.rsplit("/", 1)[-1],
            "size": 1, "fid": fid}


def test_家不在快照里返回None而不是空集():
    """★ 这条是整个文件的重点 —— `None` 与 `set()` 的差别就是「整家保留」与「整家删」。"""
    got = qpo._fids_from_snapshot("没扫过的家", "某目录", _snap({}))
    assert got is None


def test_家在快照里但没有files键也返回None():
    """增量扫描会先占位再慢慢填;只有 `fid/depth/scanned_at` 没有 `files` 的家同样是「没扫过」。"""
    got = qpo._fids_from_snapshot("占位家", "某目录", _snap({"占位家": _home([], scanned=False)}))
    assert got is None


def test_扫过但目录为空返回空集():
    """扫过了、目录里确实一个文件都没有 ⇒ 空集,这是**可以删**的正经读数。"""
    snap = _snap({"redian监听": _home([])})
    assert qpo._fids_from_snapshot("redian监听", "空目录", snap) == set()


def test_只取该家该顶层目录下的文件():
    snap = _snap({"redian监听": _home([
        _f("redian监听/甲/a.mp4", "f1"),
        _f("redian监听/甲/sub/b.mp4", "f2"),
        _f("redian监听/乙/c.mp4", "f3"),
    ])})
    got = qpo._fids_from_snapshot("redian监听", "甲", snap)
    assert got == {"f1", "f2"}
    assert qpo._fids_from_snapshot("redian监听", "乙", snap) == {"f3"}


def test_路径只有家名一层时不误配():
    """`parts[1]` 越界的老写法会把这些条目算进任意目录;这里要求它被跳过。"""
    snap = _snap({"redian监听": _home([_f("redian监听", "orphan")])})
    assert qpo._fids_from_snapshot("redian监听", "任意", snap) == set()


def test_调用方遇到None必须整家保留_源码级守卫():
    """决策落在 `main()` 里(要真连网盘,不适合在这里跑)⇒ 只能查源码结构。

    AST 查的是**真实的 `None` 比较 + `continue`**,不是子串 —— 注释里写同样的话
    不应让守卫变绿/变红(本仓那条「子串自噬」)。
    """
    src = (ROOT / "scripts" / "quark_prune_old.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    guards = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        # 形如 `if inner is None:`
        if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                and t.left.id == "inner"
                and any(isinstance(op, ast.Is) for op in t.ops)):
            guards.append(node)
    assert guards, "找不到 `if inner is None:` 这道闸 —— 没扫过的家会被当成「没有已分享文件」"
    body_src = ast.dump(guards[0])
    assert "Continue" in body_src, "`inner is None` 的分支里必须 `continue`(整家保留),不能往下走"