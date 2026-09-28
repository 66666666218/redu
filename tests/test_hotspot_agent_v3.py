"""供应商评分 / 资源风险 / 爆发联动测试。"""
import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (Base, DouhotWatchSnap, SystemConfig, User,
                           WechatArticle, WechatPanLink)
from app.services import alert_service, hotspot_agent


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def _article(session, aid: int, author: str, title: str, days_ago: float = 1.0,
             my: str = "", pan: str = "https://pan.quark.cn/s/x1") -> None:
    created = dt.datetime.now() - dt.timedelta(days=days_ago)
    session.add(WechatArticle(id=aid, user_id=1, title=title, author=author,
                              source="listen", url=f"https://mp.weixin.qq.com/s/u{aid}",
                              pan_urls=pan, my_pan_urls=my, created_at=created))
    session.commit()


def test_resource_risk_levels() -> None:
    """风险粗筛:影视/课程高危,整理类低危,普通中危。"""
    assert hotspot_agent.resource_risk("某热播剧全集4K打包")[0] == "high"
    assert hotspot_agent.resource_risk("付费课程合集免费分享")[0] == "high"
    assert hotspot_agent.resource_risk("近5年高考真题汇总(含答案)")[0] == "low"
    assert hotspot_agent.resource_risk("中秋PPT模板直接套用")[0] == "low"
    assert hotspot_agent.resource_risk("随机的一个资源")[0] == "mid"


def test_supplier_scores_rank_by_reposts(session) -> None:
    """评分 = 产出×2 + 转载×3:被全网疯转多的货源号排前(需求被反复验证)。"""
    _article(session, 1, "优质号", "资源A", my="https://pan.quark.cn/s/mine1")
    _article(session, 2, "优质号", "资源B")
    for i in range(3):  # 优质号资源A的盘链被同行转载 3 次
        session.add(WechatPanLink(user_id=1, article_id=1,
                                  pan_url=f"https://pan.quark.cn/s/re{i}",
                                  created_at=dt.datetime.now()))
    _article(session, 3, "劣质号", "资源C")
    session.commit()

    scores = hotspot_agent.supplier_scores(session, 1)
    assert scores[0]["author"] == "优质号" and scores[0]["score"] > scores[1]["score"]
    assert scores[1]["author"] == "劣质号"


def test_burst_plan_matches_and_dedups(session, monkeypatch) -> None:
    """爆发话题 → 语义命中现成资源给链接 + 落表;24h 内同话题不重复出方案。"""
    session.add(User(id=1, email="o@t.com", username="op", password_hash="x"))
    _article(session, 21, "供应商", "足球赛程表与对阵图合集")
    alerts: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda db, user_id, kind, title, detail, settings=None, **kw:
                        alerts.append((title, detail)) or False)

    llm = ('{"matches": [{"hotspot": "世界杯赛程", "article_id": 21,'
           ' "why": "赛程表正是世界杯即时刚需"}], "plans": []}')
    import app.services.hotspot_agent as ha
    monkeypatch.setattr(ha.requests, "post", lambda *a, **k: type(
        "R", (), {"status_code": 200,
                  "json": lambda self: {"choices": [{"message": {"content": llm}}]}})())

    from config.settings import Settings
    s = Settings(_env_file=None, is_dev=True, deepseek_api_key="sk-x")
    out = hotspot_agent.burst_plan(session, 1, ["世界杯赛程"], s)
    assert out and "足球赛程表" in out and "⚡" in out
    row = session.scalars(select(hotspot_agent.HotspotSuggestion)).one()
    assert row.keyword == "世界杯赛程" and row.kind == "match"

    again = hotspot_agent.burst_plan(session, 1, ["世界杯赛程"], s)
    assert again is None                              # 24h 去重:同话题不重复出方案
