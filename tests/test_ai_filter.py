# -*- coding: utf-8 -*-
"""AI 价值筛选单测(2026-10-29)。

用户口径:「群信息有点杂乱…让 AI 筛选出有价值的热点出来,**每个板块都加上 AI 筛选**」。

三条最要紧的语义:
  ① 模型失败/没配 key ⇒ **原样全推**(绝不静默丢);
  ② 按标题**缓存** —— 热榜 20 分钟一轮、同一标题不该重复花钱;
  ③ 全被挡光时至少留 `min_keep` 条(别发空卡)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import ai_filter  # noqa: E402


class _S:
    ai_filter_enabled = True
    ai_filter_sections = ""
    ai_filter_min_score = 6
    ai_filter_min_keep = 1
    ai_filter_max_items = 40
    ai_filter_timeout = 5
    deepseek_api_key = "sk-test"
    deepseek_base_url = "https://api.example.com"
    deepseek_model = "test-model"


def _items(*titles: str) -> list[dict]:
    return [{"title": t} for t in titles]


@pytest.fixture(autouse=True)
def _clear_cache():
    ai_filter._CACHE.clear()
    yield
    ai_filter._CACHE.clear()


def test_keeps_only_high_value_and_sorts_by_score(monkeypatch) -> None:
    from app.services import llm_client

    def _fake(base, key, model, section, items, timeout=45, extra=""):
        #: 标题里带"真题"的高分;"广告"低分
        out = []
        for it in items:
            hi = ("真题" in it["text"]) or ("考研" in it["text"])
            out.append({"i": it["i"], "score": 9 if hi else 3, "keep": hi, "why": ""})
        return out

    monkeypatch.setattr(llm_client, "judge_value", _fake)
    items = _items("四级真题", "加V领红包", "考研资料")
    res = ai_filter.filter_items("douyin_peer", items, _S())
    assert [x["title"] for x in res.items] == ["四级真题", "考研资料"]
    assert res.source == "llm" and res.summary().startswith("AI筛2/3")


def test_model_failure_keeps_everything(monkeypatch) -> None:
    """★ 模型挂了**不许丢内容** —— 宁可吵,不可"看起来筛过了其实全丢了"。"""
    from app.services import llm_client

    monkeypatch.setattr(llm_client, "judge_value", lambda *a, **k: None)
    items = _items("甲", "乙", "丙")
    res = ai_filter.filter_items("weibo", items, _S())
    assert res.items == items and res.source == "fallback"
    assert res.summary() == ""          # 不给自己贴金


def test_no_key_or_disabled_or_not_listed_is_off(monkeypatch) -> None:
    from app.services import llm_client

    calls: list = []
    monkeypatch.setattr(llm_client, "judge_value", lambda *a, **k: calls.append(1) or [])

    class _NoKey(_S):
        deepseek_api_key = ""

    assert ai_filter.filter_items("weibo", _items("甲"), _NoKey()).source == "off"

    class _Off(_S):
        ai_filter_enabled = False

    assert ai_filter.filter_items("weibo", _items("甲"), _Off()).source == "off"

    class _Only(_S):
        ai_filter_sections = "douyin_peer"

    assert ai_filter.filter_items("weibo", _items("甲"), _Only()).source == "off"
    assert calls == [], "不该调用模型"


def test_result_is_cached_by_title(monkeypatch) -> None:
    """热榜 20 分钟一轮、同一标题反复出现 —— 缓存命中时**不再调模型**。"""
    from app.services import llm_client

    calls: list[list] = []

    def _fake(base, key, model, section, items, timeout=45, extra=""):
        calls.append([it["text"] for it in items])
        return [{"i": it["i"], "score": 8, "keep": True, "why": ""} for it in items]

    monkeypatch.setattr(llm_client, "judge_value", _fake)
    items = _items("同一个标题")
    ai_filter.filter_items("douhot", items, _S())
    ai_filter.filter_items("douhot", items, _S())
    assert len(calls) == 1, f"第二次应命中缓存:{calls}"


def test_at_least_one_item_survives(monkeypatch) -> None:
    """全被挡光时按分数补 `min_keep` 条 —— 客户不该收到一张空卡。"""
    from app.services import llm_client

    monkeypatch.setattr(llm_client, "judge_value", lambda *a, **k: [
        {"i": 0, "score": 1, "keep": False, "why": ""},
        {"i": 1, "score": 2, "keep": False, "why": ""}])
    res = ai_filter.filter_items("zhihu", _items("甲", "乙"), _S())
    assert len(res.items) == 1
    assert res.items[0]["title"] == "乙"      # 分数更高的那个


def test_max_items_passes_the_rest_through(monkeypatch) -> None:
    """只判前 N 条,**其余原样放行**(并日志写明)—— 别让人以为全筛过了。"""
    from app.services import llm_client

    class _S2(_S):
        ai_filter_max_items = 2

    monkeypatch.setattr(llm_client, "judge_value", lambda b, k, m, s, items, timeout=45, extra="": [
        {"i": it["i"], "score": 0, "keep": False, "why": ""} for it in items])
    items = _items("甲", "乙", "丙", "丁")
    res = ai_filter.filter_items("wechat", items, _S2())
    #: 前两条判了且被挡;后两条**没判 ⇒ 放行**
    assert {x["title"] for x in res.items} == {"丙", "丁"}


def test_extra_context_is_passed_to_the_model(monkeypatch) -> None:
    """数据先验(各赛道历史搬成率)要**真的进提示词** —— 否则"按历史数据决策"只是口号。"""
    from app.services import llm_client

    seen: dict = {}

    def _fake(base, key, model, section, items, timeout=45, extra=""):
        seen["extra"] = extra
        return [{"i": it["i"], "score": 9, "keep": True, "why": ""} for it in items]

    monkeypatch.setattr(llm_client, "judge_value", _fake)
    ai_filter.filter_items("douyin_peer", _items("真题"), _S(),
                           extra_context="历史经验:软件93% 影视57%")
    assert seen["extra"] == "历史经验:软件93% 影视57%"
