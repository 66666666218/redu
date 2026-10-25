"""浏览器档案清理的**安全性质**测试(2026-10-26)。不联网、不碰真档案。

这里守的不是"能不能删",而是**"绝不会删错"**:
1. 白名单里**不许出现**任何登录态路径;
2. 只有"真档案"(有 `Local State` 或 `Default/Network/Cookies`)才会被认出来 ——
   实测 `data/_rootfs`(逆向环境的 rootfs)曾因为有个 `Default/` 被误认;
3. 不加 `--yes` 时**一个文件都不删**。
"""
import importlib.util
from pathlib import Path

import pytest


def _mod():
    p = Path(__file__).resolve().parents[1] / "scripts" / "clean_browser_caches.py"
    spec = importlib.util.spec_from_file_location("clean_browser_caches", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_白名单里不许有任何登录态路径():
    cb = _mod()
    for keep in cb._KEEP:
        assert keep not in cb._CACHE_PATHS, f"{keep} 是登录态,绝不能被清"
    #: 再加一道:任何缓存路径都不许落在 Network / Local Storage 这些目录下
    for p in cb._CACHE_PATHS:
        assert not p.startswith(("Default/Network", "Default/Local Storage")), p


def test_只有真档案才被认出来(tmp_path):
    """`_rootfs` 那种"只是有个 Default 目录"的**不许**被当成档案。"""
    cb = _mod()
    fake = tmp_path / "_rootfs"
    (fake / "Default").mkdir(parents=True)
    assert cb.looks_like_profile(fake) is False

    real = tmp_path / "_douyin_profile"
    (real / "Default" / "Network").mkdir(parents=True)
    (real / "Default" / "Network" / "Cookies").write_bytes(b"x")
    assert cb.looks_like_profile(real) is True

    real2 = tmp_path / "xhs_cdp_profile"
    real2.mkdir()
    (real2 / "Local State").write_text("{}", encoding="utf-8")
    assert cb.looks_like_profile(real2) is True


def test_常驻CDP档案默认不清(tmp_path):
    """★ 用户 2026-10-26 明确要求:`xhs_cdp_profile` / `_douyin_profile` 是
    "一直登着、模拟真人"的资产 —— **默认一个字节都不清**,要清必须显式加参数。

    这不只是"少清一个":它把"绝不会被顺手清掉"变成**代码里的默认**,
    而不是靠每次记得加 `--only`。
    """
    cb = _mod()
    ps = [tmp_path / "xhs_cdp_profile", tmp_path / "_douyin_profile",
          tmp_path / "_quark_profile", tmp_path / "xhs_2"]
    targets, kept = cb.select_profiles(ps)
    assert [p.name for p in kept] == ["xhs_cdp_profile", "_douyin_profile"]
    assert [p.name for p in targets] == ["_quark_profile", "xhs_2"]


def test_显式要求才连常驻档案一起清(tmp_path):
    cb = _mod()
    ps = [tmp_path / "xhs_cdp_profile", tmp_path / "_quark_profile"]
    targets, kept = cb.select_profiles(ps, include_kept=True)
    assert kept == [] and len(targets) == 2


def test_only_按名字过滤(tmp_path):
    cb = _mod()
    ps = [tmp_path / "xhs_1", tmp_path / "xhs_2", tmp_path / "_quark_profile"]
    targets, _ = cb.select_profiles(ps, only="xhs_")
    assert [p.name for p in targets] == ["xhs_1", "xhs_2"]


def test_默认只统计不删(tmp_path, monkeypatch):
    cb = _mod()
    prof = tmp_path / "p"
    (prof / "Default" / "Network").mkdir(parents=True)
    (prof / "Default" / "Network" / "Cookies").write_bytes(b"cookie")
    cache = prof / "Default" / "Cache"
    cache.mkdir(parents=True)
    (cache / "a.bin").write_bytes(b"0" * 100)

    monkeypatch.setattr(cb, "in_use", lambda p: "")          # 不真的去查进程
    total = cb.clean(prof, yes=False)
    assert total > 0
    assert cache.exists(), "dry-run 不许删任何东西"

    cb.clean(prof, yes=True)
    assert not cache.exists(), "给了 --yes 才删"
    assert (prof / "Default" / "Network" / "Cookies").exists(), "登录态必须还在"


def test_浏览器在用时跳过(tmp_path, monkeypatch):
    """边跑边删会把档案搞坏 —— 宁可少清一个。"""
    cb = _mod()
    prof = tmp_path / "p"
    cache = prof / "Default" / "Cache"
    cache.mkdir(parents=True)
    (cache / "a.bin").write_bytes(b"0" * 10)
    monkeypatch.setattr(cb, "in_use", lambda p: "msedge.exe --user-data-dir=...")
    assert cb.clean(prof, yes=True) == 0.0
    assert cache.exists(), "被占用时一个字节都不许动"
