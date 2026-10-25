"""热搜 → 吃瓜候选 单测(2026-10-25)。**全部离线**,不碰三个平台、不调模型。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest

from app.services import gossip_topics as gt


def test_强信号直接命中():
    sc, hits = gt.score("廖碧儿承认恋情")
    assert sc >= 3 and "恋情" in hits


def test_硬排除一票否决():
    """时政/灾害/比分这类**不许进候选** —— 它们一旦做成资料就是事故。"""
    for t in ("县委书记说被扔下水", "西藏那曲地震救援一线", "TES战胜AL 赛果", "金价飙涨"):
        sc, hits = gt.score(t)
        assert sc == 0 and hits and hits[0].startswith("排除:"), (t, sc, hits)


def test_跨平台同一件事要合并():
    items = [{"platform": "微博", "title": "某明星承认恋情", "heat": 10, "rank": 2},
             {"platform": "抖音", "title": "某明星 承认恋情", "heat": 99, "rank": 5}]
    got = gt.pick_gossip(items, min_score=3)
    assert len(got) == 1, got
    assert set(got[0]["platforms"]) == {"微博", "抖音"}, "多平台同时上榜要记下来"
    assert got[0]["heat"] == 99


def test_按关键词挑候选_低于阈值不要():
    items = [{"platform": "微博", "title": "某明星承认恋情", "heat": 1, "rank": 1},
             {"platform": "微博", "title": "今天天气不错", "heat": 9, "rank": 2}]
    assert [g["title"] for g in gt.pick_gossip(items, min_score=3)] == ["某明星承认恋情"]


class _S:
    deepseek_api_key = "k"
    deepseek_base_url = "http://x"
    deepseek_model = "m"


def test_硬排除不许送进模型(monkeypatch):
    """★ 2026-10-25 实测:模型把「张雪把县委书记丢河里」判成"争议事件、可吃瓜" ——
    那是**时政**。所以黑名单必须在**模型之前**生效,不能因为"交给模型了"就跳过。"""
    seen: list[list[str]] = []

    def _judge(base, key, model, titles, **k):
        seen.append(list(titles))
        return [{"title": t, "is_gossip": True, "reason": "x"} for t in titles]

    monkeypatch.setattr("app.services.llm_client.judge_gossip_titles", _judge)
    monkeypatch.setattr(gt, "judge_all", gt.judge_all)      # 保持真实实现
    items = [{"platform": "微博", "title": "县委书记说被扔下水是临时起意", "heat": 1, "rank": 1},
             {"platform": "微博", "title": "某明星承认恋情", "heat": 1, "rank": 2}]
    picked, _ = gt.judge_all(items, settings=_S())
    flat = [t for batch in seen for t in batch]
    assert not any("书记" in t for t in flat), f"时政词进了模型:{flat}"
    assert [g["title"] for g in picked] == ["某明星承认恋情"]


def test_模型判否的条目要剔掉(monkeypatch):
    def _judge(base, key, model, titles, **k):
        return [{"title": t, "is_gossip": "恋情" in t, "reason": "理由"} for t in titles]

    monkeypatch.setattr("app.services.llm_client.judge_gossip_titles", _judge)
    items = [{"platform": "微博", "title": "某明星承认恋情", "heat": 1, "rank": 1},
             {"platform": "百度", "title": "老板回应学生时薪高10元", "heat": 1, "rank": 2}]
    picked, note = gt.judge_all(items, settings=_S())
    assert [g["title"] for g in picked] == ["某明星承认恋情"]
    assert "AI 判定" in note


def test_模型挂了要退回关键词并说出来(monkeypatch):
    """⚠️ 空结果与「今天没瓜」同形 ⇒ 必须**有话说**,不能静默清空。"""
    monkeypatch.setattr("app.services.llm_client.judge_gossip_titles",
                        lambda *a, **k: None)
    items = [{"platform": "微博", "title": "某明星承认恋情", "heat": 1, "rank": 1}]
    picked, note = gt.judge_all(items, settings=_S())
    assert picked and "退回关键词" in note, (picked, note)


def test_抓取失败要逐个记下来(monkeypatch):
    """单平台挂了不许拖垮整轮,但**必须说出来**(否则"今天没热搜"与"抓取挂了"同形)。"""
    monkeypatch.setattr(gt, "fetch_weibo", lambda *a, **k: [{"platform": "微博",
                                                           "title": "某明星承认恋情",
                                                           "heat": 1, "rank": 1}])
    monkeypatch.setattr(gt, "fetch_baidu", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("百度挂了")))
    monkeypatch.setattr(gt, "fetch_douyin", lambda *a, **k: [])
    items, fails = gt.fetch_all()
    assert len(items) == 1 and "百度" in fails and "百度挂了" in fails["百度"]
