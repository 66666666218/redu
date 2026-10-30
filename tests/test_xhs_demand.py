"""小红书「求1型」扫描的**纯逻辑**测试(2026-10-30)。不开浏览器、不联网。

浏览器那半(`search_notes`/`note_comments`)只能在**人开着并登录过的**那个 CDP 浏览器上实测,
所以这里守的是:
1. 判据与抖音**同一套**(密度/重复率来自 `demand_watch` 的归一化);
2. CDP 不可达时**明确 skip**,不自己拉起浏览器(脚本拉起的浏览器 `webdriver=True`,那条路就废了)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest

from app.services import xhs_demand as X


def test_评论统计用同一套判据():
    texts = ["1", "1", "求完整版", "还有吗", "求", "好看", "1", "求链接", "哈哈", "1"]
    st = X.stats_from_texts(texts)

    assert st["total"] == 10
    assert st["ones"] == 4
    assert st["demand"] == 4          # 求完整版 / 求 / 求链接 / 还有吗(「有吗」也算求)
    assert st["dup_ratio"] > 0


def test_重复短句也按归一算():
    texts = ["刚看完", "刚看完[笑哭]", "刚看完。", "冰露", "好看", "1"]
    st = X.stats_from_texts(texts)
    assert st["dup_ratio"] >= 0.3


class _S:
    demand_xhs_enabled = True
    demand_seed_words = "小小绘"


def test_CDP不可达时明确跳过_不自己开浏览器(monkeypatch):
    from app.services import demand_watch as dw

    monkeypatch.setattr(X, "available", lambda settings=None: (False, "CDP 不可达"))
    out = dw.scan_xhs(None, 1, _S())

    assert out["skipped"] == "CDP 不可达"


def test_关掉开关就不碰浏览器(monkeypatch):
    from app.services import demand_watch as dw

    class S(_S):
        demand_xhs_enabled = False

    monkeypatch.setattr(X, "available", lambda settings=None: pytest.fail("关了开关不该探活"))
    assert dw.scan_xhs(None, 1, S())["skipped"] == "disabled"
