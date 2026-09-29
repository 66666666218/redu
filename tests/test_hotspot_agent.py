"""热点→网盘拉新 Agent 测试:精确匹配 / LLM 语义匹配+选题 / 24h 去重 / 开关 / 建议落表 / 多平台共振。"""
import datetime as dt
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (BaiduHotItem, Base, DouhotWatchSnap, HotspotSuggestion,
                           SystemConfig, User, WechatArticle, WeiboHotItem)
from app.services import alert_service, hotspot_agent


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


@pytest.fixture
def agent_env(session, monkeypatch):
    session.add(User(id=1, email="op@test.com", username="op", password_hash="x"))
    alerts: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda db, user_id, kind, title, detail, settings=None, **kw:
                        alerts.append((title, detail, kw.get("push_feishu", True))) or False)
    return alerts


def _snap(session, keyword: str, growth: float, hours_ago: float = 0.5) -> None:
    session.add(DouhotWatchSnap(user_id=1, section="douhot", list_type="word",
                                keyword=keyword, trend_growth=growth, rank_now=2,
                                score=100, captured_at=dt.datetime.now() - dt.timedelta(hours=hours_ago)))
    session.commit()


def _supply(session, aid: int, title: str, hours_ago: float = 1.0,
            pan: str = "https://pan.quark.cn/s/x1") -> None:
    session.add(WechatArticle(id=aid, user_id=1, title=title, author="供应商号",
                              source="listen",
                              url=f"https://mp.weixin.qq.com/s/u{aid}",
                              pan_urls=pan,
                              created_at=dt.datetime.now() - dt.timedelta(hours=hours_ago)))
    session.commit()


def _agent_settings(**kw):
    from config.settings import Settings
    base = {"hotspot_agent_enabled": True, "hotspot_min_growth": 50.0,
            "hotspot_agent_llm_top": 3, "deepseek_api_key": ""}
    base.update(kw)
    return Settings(_env_file=None, is_dev=True, **base)


def _run(session, monkeypatch, llm_json: str | None = None, **kw):
    settings = _agent_settings(**kw)
    if llm_json is not None:
        monkeypatch.setattr(settings, "deepseek_api_key", "sk-x")

        class _Resp:
            status_code = 200

            def __init__(self, text: str) -> None:
                self._text = text

            def json(self):
                return {"choices": [{"message": {"content": self._text}}]}

        import app.services.hotspot_agent as ha
        monkeypatch.setattr(ha.requests, "post", lambda *a, **k: _Resp(llm_json))
    return hotspot_agent.run_hotspot_agent(session, 1, settings)


def test_hotspot_matches_supply_and_notifies(session, monkeypatch, agent_env) -> None:
    """热点词涨幅达标且标题字面命中资源文 → 建议跟发并附复制即用块;建议落表;24h 去重。"""
    _snap(session, "Switch模拟器", growth=180)
    _supply(session, 11, "Switch模拟器最新版整合包(附安装教程)")
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 1 and out["status"] == "ok"
    title, detail, feishu = alerts[0]
    assert "1 条现成资源" in title and "Switch模拟器" in detail
    assert "pan.quark.cn/s/x1" in detail and "复制即用" in detail
    assert feishu is False
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.keyword == "Switch模拟器" and row.kind == "match"
    assert "Switch模拟器" in row.resource_title

    again = _run(session, monkeypatch, dajiala_key="")
    assert again["status"] == "all_duplicated"
    assert len(alerts) == 1


def test_hotspot_low_growth_ignored(session, monkeypatch, agent_env) -> None:
    _snap(session, "PS5游戏资源", growth=10)
    out = _run(session, monkeypatch, dajiala_key="")
    assert out["status"] == "no_hotspots"


def test_hotspot_llm_semantic_match(session, monkeypatch, agent_env) -> None:
    """语义匹配:资源标题不含热点字面词,LLM 按 article_id 匹配成功 → kind=match。"""
    _snap(session, "世界杯", growth=200)
    _supply(session, 21, "2026足球赛程表+强弱分析(免费保存)")
    llm = json.dumps({"matches": [{"hotspot": "世界杯", "article_id": 21,
                                   "why": "赛程表正是世界杯期间的即时刚需"}],
                      "plans": []}, ensure_ascii=False)
    out = _run(session, monkeypatch, llm_json=llm, deepseek_api_key="sk-x")
    assert out["matched"] == 1
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.kind == "match" and "足球赛程表" in row.resource_title
    title, detail, _ = agent_env[0]
    assert "赛程表正是世界杯期间的即时刚需" in detail   # 匹配理由透传给运营


def test_hotspot_llm_suggests_for_unmatched(session, monkeypatch, agent_env) -> None:
    """热点没现成资源 → LLM 给拉新选题;建议落表(kind=llm)且告警带方案。"""
    _snap(session, "剪映模板", growth=120)
    _snap(session, "考公资料", growth=90)
    llm = json.dumps({"matches": [], "plans": [
        {"hotspot": "剪映模板", "resource": "2026最新剪映爆款模板合集",
         "title": "剪映模板直接抄作业(免费保存)", "audience": "短视频新手",
         "hook": "模板要导入剪映才能用,必须转存"},
        {"hotspot": "考公资料", "resource": "国省考历年真题+冲刺笔记",
         "title": "考公真题免费自取", "audience": "应届考公人", "hook": "打印刷题"}]},
        ensure_ascii=False)
    alerts = agent_env

    out = _run(session, monkeypatch, llm_json=llm, deepseek_api_key="sk-x")
    assert out["matched"] == 0 and out["llm"] == 2
    rows = session.scalars(select(HotspotSuggestion)).all()
    assert {r.keyword for r in rows} == {"剪映模板", "考公资料"}
    assert all(r.kind == "llm" for r in rows)
    title, detail, _ = alerts[0]
    assert "剪映爆款模板合集" in detail and "2 条拉新选题" in title


def test_hotspot_agent_disabled(session, monkeypatch, agent_env) -> None:
    _snap(session, "Switch模拟器", growth=180)
    out = _run(session, monkeypatch, hotspot_agent_enabled=False)
    assert out["status"] == "disabled"


def _weibo(session, title: str, rank: int = 1, heat: int = 900_000,
           hours_ago: float = 1.0) -> None:
    session.add(WeiboHotItem(user_id=1, title=title, heat=heat, rank=rank,
                             captured_at=dt.datetime.now() - dt.timedelta(hours=hours_ago)))
    session.commit()


def _baidu(session, title: str, rank: int = 2, heat: int = 8,
           hours_ago: float = 1.0) -> None:
    session.add(BaiduHotItem(user_id=1, title=title, heat=heat, rank=rank,
                             captured_at=dt.datetime.now() - dt.timedelta(hours=hours_ago)))
    session.commit()


def test_resonance_tag_and_platforms(session, monkeypatch, agent_env) -> None:
    """抖音词在微博/百度同话题新上榜 → 输出带共振标记,platforms 落表(多平台证据)。"""
    _snap(session, "王楚钦", growth=150)
    _weibo(session, "王楚钦 男单夺冠", rank=1)
    _baidu(session, "王楚钦男单夺冠", rank=2)
    _supply(session, 31, "王楚钦比赛视频合集(持续更新)")
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 1 and out["status"] == "ok"
    _, detail, _ = alerts[0]
    assert "共振" in detail and "微博榜第1名" in detail and "百度榜第2名" in detail
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.platforms == "douyin+weibo+baidu"


def test_resonance_boost_reorders(session, monkeypatch, agent_env) -> None:
    """共振加权:effective_growth = growth×1.3 → 涨幅较低的共振热点排序上浮。"""
    _snap(session, "单平台热点", growth=100)
    _snap(session, "共振热点", growth=80)
    _weibo(session, "共振热点全网刷屏", rank=3)
    _supply(session, 41, "单平台热点资源包")
    _supply(session, 42, "共振热点资源包")
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 2
    _, detail, _ = alerts[0]
    assert detail.index("共振热点") < detail.index("单平台热点")   # 共振加权后上浮
    rows = {r.keyword: r.platforms for r in session.scalars(select(HotspotSuggestion)).all()}
    assert rows["共振热点"] == "douyin+weibo" and rows["单平台热点"] == "douyin"


def test_resonance_requires_newcomer(session, monkeypatch, agent_env) -> None:
    """微博条目 12h 前就上榜(超出 6h 新上榜窗口)→ 不算共振,不虚标多平台。"""
    _snap(session, "老热点", growth=150)
    _weibo(session, "老热点持续霸榜", hours_ago=12)
    _supply(session, 51, "老热点资源包")
    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 1
    _, detail, _ = agent_env[0]
    assert "共振" not in detail
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.platforms == "douyin"
