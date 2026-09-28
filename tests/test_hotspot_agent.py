"""热点→网盘选题 Agent 测试:规则匹配 / LLM 层 / 24h 去重 / 开关。"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, DouhotWatchSnap, SystemConfig, User, WechatArticle
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
    """标准环境:用户 + 启用状态。返回捕获告警的列表。"""
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


def _supply(session, title: str, hours_ago: float = 1.0, pan: str = "https://pan.quark.cn/s/x1") -> None:
    session.add(WechatArticle(user_id=1, title=title, author="供应商号", source="listen",
                              url=f"https://mp.weixin.qq.com/s/{abs(hash(title)) % 99999}",
                              pan_urls=pan, created_at=dt.datetime.now() - dt.timedelta(hours=hours_ago)))
    session.commit()


def _run(session, monkeypatch, llm_text: str | None = None, **kw):
    settings = _agent_settings(**kw)
    if llm_text is not None:
        monkeypatch.setattr(settings, "deepseek_api_key", "sk-x")
        import app.services.hotspot_agent as ha
        monkeypatch.setattr(ha.requests, "post", lambda *a, **k: _LlmResp(llm_text))
    return hotspot_agent.run_hotspot_agent(session, 1, settings)


def _agent_settings(**kw):
    from config.settings import Settings
    base = {"hotspot_agent_enabled": True, "hotspot_min_growth": 50.0,
            "hotspot_agent_llm_top": 3, "deepseek_api_key": ""}
    base.update(kw)
    return Settings(_env_file=None, is_dev=True, **base)


class _LlmResp:
    status_code = 200

    def __init__(self, text: str) -> None:
        self._text = text

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


def test_hotspot_matches_supply_and_notifies(session, monkeypatch, agent_env) -> None:
    """热点词涨幅达标且已有现成资源 → 建议「立即跟发」并附我方/源链;24h 内不重复推。"""
    _snap(session, "Switch模拟器", growth=180)
    _supply(session, "Switch模拟器最新版整合包(附安装教程)")
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 1 and out["status"] == "ok"
    title, detail, feishu = alerts[0]
    assert "热点选题建议" in title and "Switch模拟器" in detail
    assert "pan.quark.cn/s/x1" in detail            # 现成资源链直接给出
    assert feishu is False                           # 业务建议走站内,不刷飞书群

    again = _run(session, monkeypatch, dajiala_key="")
    assert again["status"] == "all_duplicated"       # 24h 去重:同一热点不重复推
    assert len(alerts) == 1


def test_hotspot_low_growth_ignored(session, monkeypatch, agent_env) -> None:
    """涨幅不达标的热点不生成建议(避免噪音)。"""
    _snap(session, "PS5游戏资源", growth=10)
    out = _run(session, monkeypatch, dajiala_key="")
    assert out["status"] == "no_hotspots"


def test_hotspot_llm_suggests_for_unmatched(session, monkeypatch, agent_env) -> None:
    """热点没现成资源 → LLM 给网盘选题;LLM 漏掉的热点也有占位,列表完整。"""
    _snap(session, "剪映模板", growth=120)
    _snap(session, "考公资料", growth=90)
    llm = ("《剪映模板》→ 模板包 | 2026最新剪映爆款模板合集 | 剪映,模板,视频剪辑\n"
           "《考公资料》→ 真题合集 | 国省考历年真题+冲刺笔记 | 考公,真题,笔记")
    alerts = agent_env

    out = _run(session, monkeypatch, llm_text=llm, deepseek_api_key="sk-x")
    assert out["llm"] == 2 and out["matched"] == 0
    title, detail, _ = alerts[0]
    assert "剪映爆款模板合集" in detail and "考公" in detail


def test_hotspot_agent_disabled(session, monkeypatch, agent_env) -> None:
    _snap(session, "Switch模拟器", growth=180)
    out = _run(session, monkeypatch, hotspot_agent_enabled=False)
    assert out["status"] == "disabled"
