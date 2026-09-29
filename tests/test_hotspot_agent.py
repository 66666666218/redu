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
    """共振分级加权(v5):百度证据 ×1.5 → 涨幅较低的百度共振热点排序上浮。

    对照:仅微博共振 ×1.2 翻不了盘(微博证据弱,80×1.2=96 < 100)——
    这正是证据分级的语义,百度(真实搜索)>微博(可运营话题)。
    """
    _snap(session, "单平台热点", growth=100)
    _snap(session, "共振热点", growth=80)
    _baidu(session, "共振热点全网刷屏", rank=3)
    _supply(session, 41, "单平台热点资源包")
    _supply(session, 42, "共振热点资源包")
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 2
    _, detail, _ = alerts[0]
    assert detail.index("共振热点") < detail.index("单平台热点")   # 百度共振加权后上浮
    rows = {r.keyword: r.platforms for r in session.scalars(select(HotspotSuggestion)).all()}
    assert rows["共振热点"] == "douyin+baidu" and rows["单平台热点"] == "douyin"


def test_opportunity_prefers_blue_ocean(session, monkeypatch, agent_env) -> None:
    """机会分排序(v5):高涨幅红海(多家供货)让位低涨幅蓝海(竞争空白)。"""
    _snap(session, "红海热点", growth=150)
    _snap(session, "蓝海热点", growth=90)
    for i in range(41, 46):     # 红海:5 家已供货 → 稀疏度 1/6
        _supply(session, i, f"红海热点资源包{i}")
    _supply(session, 50, "蓝海热点资源包")   # 蓝海:1 家 → 稀疏度 1/2
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 2
    _, detail, _ = alerts[0]
    # 机会分:蓝海 90×(1/2)=45 > 红海 150×(1/6)=25 → 蓝海排前(红海虽热但挤满供货)
    assert detail.index("蓝海热点") < detail.index("红海热点")
    assert "竞争" in detail
    rows = {r.keyword: r.opportunity for r in session.scalars(select(HotspotSuggestion)).all()}
    assert set(rows) == {"红海热点", "蓝海热点"}
    assert all(v > 0 for v in rows.values())


def test_window_factor_by_momentum(session, monkeypatch, agent_env) -> None:
    """窗口因子(v5):score 动量转跌 → 输出带「窗口将关闭」,机会分被压低。"""
    now = dt.datetime.now()
    for i, (s, hrs) in enumerate([(1000.0, 1.0), (500.0, 0.3)]):   # 两拍,环比 -50%
        session.add(DouhotWatchSnap(user_id=1, section="douhot", list_type="word",
                                    keyword="退烧热点", trend_growth=120, rank_now=2,
                                    score=s, captured_at=now - dt.timedelta(hours=hrs)))
    session.commit()
    _supply(session, 61, "退烧热点资源包")
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 1
    _, detail, _ = alerts[0]
    assert "窗口将关闭" in detail and "2h" in detail
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.opportunity > 0    # 窗口因子 0.3 已折进机会分


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


def test_mark_acted_api(session) -> None:
    """一键标记已发(v5 下注环节):只有 acted 建议 + save_pv 才构成预测→结算样本。"""
    from app.api.hotspot import list_suggestions, mark_acted

    session.add(User(id=2, email="u@test.com", username="u2", password_hash="x"))
    session.add(HotspotSuggestion(user_id=2, keyword="测试热点", kind="llm", plan="x"))
    session.commit()
    user = session.get(User, 2)

    out = mark_acted(1, None, user=user, db=session)
    assert out["acted"] is True and out["keyword"] == "测试热点"
    row = session.get(HotspotSuggestion, 1)
    assert row.acted is True and row.acted_at is not None

    listing = list_suggestions(limit=50, acted=True, user=user, db=session)
    assert listing["total"] == 1 and listing["list"][0]["saves"] == 0

    # 取消标记 / 越权 404
    mark_acted(1, type("P", (), {"acted": False})(), user=user, db=session)
    assert session.get(HotspotSuggestion, 1).acted is False
    import pytest as _pytest
    from fastapi import HTTPException
    with _pytest.raises(HTTPException):
        mark_acted(999, None, user=user, db=session)
