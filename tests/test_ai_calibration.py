# -*- coding: utf-8 -*-
"""AI 分数线自动校准单测(2026-10-29)。

用户口径:「可以根据之前的数据来进行策略决策」「开始进行下一步」。
闭环 = **AI 分数 ↔ 真实搬成**;规则写死、可回测:保留比例 ≥20%,
在可行阈值里取**保留部分搬成率最高**的,一次最多动 1 分,样本不足不调。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from datetime import datetime  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: F401,E402
from app.db.database import Base  # noqa: E402
from app.db.models import (AiFilterDecision, DouyinLead, SystemConfig,  # noqa: E402
                           User)
from app.services import ai_calibration as ac  # noqa: E402


class _S:
    ai_filter_min_score = 6
    ai_calibration_days = 21
    ai_calibration_min_samples = 40
    deepseek_api_key = "sk-test"
    deepseek_base_url = "https://api.example.com"
    deepseek_model = "m"
    ai_filter_timeout = 5


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _seed(session, pairs: list[tuple[int, int]]) -> None:
    """pairs = [(score, hot)] → 一条判定 + 一条线索(`share_count` 表示热度)。"""
    for i, (score, hot) in enumerate(pairs):
        key = f"aweme-{i}"
        session.add(AiFilterDecision(user_id=1, section="douyin_peer", item_key=key,
                                     title=f"t{i}", score=score, keep=bool(hot),
                                     decided_at=datetime.now()))
        session.add(DouyinLead(user_id=1, aweme_id=key, mark=f"m{i}", title=f"t{i}",
                               share_count=500 if hot else 10,
                               found_at=datetime.now()))
    session.commit()


def test_choose_threshold_prefers_highest_yield_within_volume() -> None:
    #: 9 分档 90% 搬成;7 分档只 40%;3 分档 10% ⇒ 8 分线与 9 分线效果相同,按规则取**更低**的 8。
    pairs = ((9, 1),) * 18 + ((9, 0),) * 2 + ((7, 1),) * 4 + ((7, 0),) * 16 \
        + ((3, 1),) * 1 + ((3, 0),) * 9
    got = ac.choose_threshold(list(pairs), min_samples=40, min_keep_ratio=0.2)
    assert got["action"] == "ok"
    assert got["threshold"] == 8                 # 9 分那档搬成 90%;同率取更低阈值
    assert got["rate"] == pytest.approx(0.9)


def test_choose_threshold_skips_small_sample() -> None:
    got = ac.choose_threshold([(9, 1)] * 5, min_samples=40)
    assert got["action"] == "skip" and "样本不足" in got["reason"]


def test_calibrate_moves_by_one_and_writes_config(session) -> None:
    pairs = ((9, 1),) * 18 + ((9, 0),) * 2 + ((7, 1),) * 4 + ((7, 0),) * 16 \
        + ((3, 1),) * 1 + ((3, 0),) * 9
    _seed(session, list(pairs))
    res = ac.calibrate(session, 1, "douyin_peer", _S(), min_samples=40)
    assert res["old"] == 6 and res["new"] == 7, "一次最多动 1 分"
    row = session.get(SystemConfig, "ai_min_score:douyin_peer")
    assert row is not None and row.value == "7"


def test_backfill_skips_without_key(session) -> None:
    class _NoKey(_S):
        deepseek_api_key = ""

    assert ac.backfill(session, 1, _NoKey())["action"] == "skip"


def test_ai_filter_uses_db_threshold_and_records_decisions(session, monkeypatch) -> None:
    """库里的校准值优先于配置默认;判定要落库(带 item_key)。"""
    from app.services import ai_filter, llm_client

    session.add(SystemConfig(key="ai_min_score:douyin_peer", value="9"))
    session.commit()
    #: ⚠️ `i` 是**0 基下标**(与 `filter_items` 传给模型的一致)
    monkeypatch.setattr(llm_client, "judge_value", lambda *a, **k: [
        {"i": 0, "score": 7, "keep": True, "why": ""},
        {"i": 1, "score": 10, "keep": True, "why": ""}])

    class _F(_S):
        ai_filter_min_keep = 1
        ai_filter_max_items = 40
        ai_filter_sections = ""

    items = [{"title": "甲", "aweme_id": "a1"}, {"title": "乙", "aweme_id": "a2"}]
    res = ai_filter.filter_items("douyin_peer", items, _F(), session=session, user_id=1,
                                 key_of=lambda it: it["aweme_id"])
    assert [x["aweme_id"] for x in res.items] == ["a2"], "9 分线:7 分那条不推"
    rows = {r.item_key: r for r in session.query(AiFilterDecision).all()}
    assert rows["a1"].score == 7 and rows["a2"].score == 10


def test_heat_pairs_uses_share_count_not_moved(session) -> None:
    """★ 2026-10-29 实测:搬成率在各分数档都一样(64~65%),**不能当价值标签**;
    而 AI 分数与转发数强相关 ⇒ 校准标签用「转发数 ≥ 100」。"""
    session.add(AiFilterDecision(user_id=1, section="douyin_peer", item_key="a",
                                 title="甲", score=9, decided_at=datetime.now()))
    session.add(DouyinLead(user_id=1, aweme_id="a", mark="m", title="甲",
                           share_count=1200, our_url="", found_at=datetime.now()))
    session.add(AiFilterDecision(user_id=1, section="douyin_peer", item_key="b",
                                 title="乙", score=2, decided_at=datetime.now()))
    session.add(DouyinLead(user_id=1, aweme_id="b", mark="m2", title="乙",
                           share_count=3, our_url="https://p/s/x", found_at=datetime.now()))
    session.commit()
    assert ac.heat_pairs(session, 1, "douyin_peer", hot_min=100) == [(9, 1), (2, 0)]
