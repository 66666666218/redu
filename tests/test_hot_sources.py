"""热榜源契约单测(v2.2.0 命门自持架构):解析/契约/错误路径。"""
import pytest


@pytest.fixture
def session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.database import Base
    from app.db import models  # noqa: F401 - 注册全部表

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def test_bilibili_douban_direct_parse(monkeypatch) -> None:
    from app.services import hot_sources as hs

    class _R:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    # B站:官方排行 JSON 结构
    monkeypatch.setattr(hs.creq, "get", lambda *a, **kw: _R({"data": {"list": [
        {"title": "《原神》主题曲", "bvid": "BV1x", "tname": "手机游戏",
         "stat": {"view": 12345}},
        {"title": "", "bvid": "BV2"},  # 无标题丢弃
    ]}}))
    items = hs.BilibiliSource().fetch(limit=10)
    assert len(items) == 1 and items[0]["title"] == "《原神》主题曲"
    assert "bilibili.com/video/BV1x" in items[0]["url"]

    # 豆瓣:公开 JSON 结构
    monkeypatch.setattr(hs.creq, "get", lambda *a, **kw: _R({"subjects": [
        {"title": "年会不能停！2", "url": "https://movie.douban.com/subject/1/", "rate": "6.4"}]}))
    items = hs.DoubanSource().fetch()
    assert items[0]["title"] == "年会不能停！2" and "6.4" in items[0]["extra"]


def test_newsnow_source_contract(monkeypatch) -> None:
    from app.services import hot_sources as hs

    class _R:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    monkeypatch.setattr(hs.creq, "get", lambda *a, **kw: _R({
        "status": "success", "id": "zhihu",
        "items": [{"id": "1", "title": "知乎热榜条目", "url": "https://zhihu.com/q/1",
                   "extra": {"info": "1310 万热度"}}]}))
    items = hs.NewsnowSource("zhihu").fetch()
    assert items[0]["title"] == "知乎热榜条目" and "1310 万热度" in items[0]["extra"]

    # 容器返回异常 → HotSourceError(调用方可降级)
    monkeypatch.setattr(hs.creq, "get", lambda *a, **kw: _R({"error": True, "statusCode": 500}))
    with pytest.raises(hs.HotSourceError):
        hs.NewsnowSource("zhihu").fetch()


def test_fetch_hot_registry_and_unknown() -> None:
    from app.services import hot_sources as hs

    # 自研源注册在位(命门自持的核心两个)
    assert "bilibili" in hs.SOURCES and "douban" in hs.SOURCES
    assert isinstance(hs.SOURCES["bilibili"], hs.BilibiliSource)
    with pytest.raises(hs.HotSourceError):
        hs.fetch_hot("not_exist")


def test_platform_hot_candidates_aggregates_by_platform(session) -> None:
    """多平台候选(全进 Agent 选题):跨平台同现聚合、平台数与名次排序。"""
    from datetime import datetime, timedelta

    from app.db.models import HotSourceItem
    from app.services.hotspot_agent import _platform_hot_candidates

    now = datetime.now()
    rows = [
        # 跨 2 平台同现(归一化后相同)
        HotSourceItem(user_id=1, source="bilibili", rank=1, title="迪拜航空客机事故", captured_at=now),
        HotSourceItem(user_id=1, source="zhihu", rank=2, title="迪拜航空客机事故", captured_at=now),
        # 单平台
        HotSourceItem(user_id=1, source="douban", rank=1, title="年会不能停2", captured_at=now),
        # 名次超界(>10)不进候选
        HotSourceItem(user_id=1, source="hupu", rank=11, title="低名次条目", captured_at=now),
        # 过期(24h 外)
        HotSourceItem(user_id=1, source="weibo", rank=1, title="过期条目",
                      captured_at=now - timedelta(hours=30)),
    ]
    session.add_all(rows)
    session.commit()
    cands = _platform_hot_candidates(session, 1)
    titles = [c["keyword"] for c in cands]
    assert "迪拜航空客机事故" in titles and "年会不能停2" in titles
    assert "低名次条目" not in titles and "过期条目" not in titles
    # 跨平台同现排在单平台之前(平台数降序)
    assert titles[0] == "迪拜航空客机事故"
    assert cands[0]["platforms"] in ("bilibili+zhihu", "zhihu+bilibili")
