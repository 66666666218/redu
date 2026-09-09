"""早期苗头 Agent v2 单测:评分/加速度/生命周期跃迁/回落/跨板块共振/闲鱼字符串时间戳。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import AgentStage, BaiduHotItem, User, WeiboHotItem, XianyuDaily
from app.services import early_agent, feishu_client


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(username="t", password_hash="x"))
    db.commit()
    yield db
    db.close()


@pytest.fixture
def st():
    from config.settings import Settings
    return Settings(_env_file=None, is_dev=True,
                    feishu_webhook="https://open.feishu.cn/hook/main",
                    feishu_webhook_weibo="https://open.feishu.cn/hook/weibo",
                    feishu_webhook_xianyu="https://open.feishu.cn/hook/xianyu",
                    agent_score_threshold=55, agent_cooldown_hours=12,
                    focus_min_len=4, wechat_resample_growth_pct=100,
                    wechat_burst_min_reads=200)


class _FakeFeishu:
    sent: list[str] = []
    last_webhook: str = ""

    def __init__(self, webhook, secret="") -> None:
        _FakeFeishu.last_webhook = webhook

    def send(self, msg: str) -> bool:
        _FakeFeishu.sent.append(msg)
        return True

    def send_card(self, card: dict) -> bool:
        import json
        _FakeFeishu.sent.append(json.dumps(card, ensure_ascii=False))
        return True


@pytest.fixture(autouse=True)
def _fake_feishu(monkeypatch):
    _FakeFeishu.sent = []
    _FakeFeishu.last_webhook = ""
    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)


def _weibo(session, title: str, heat: float, hours_ago: float) -> None:
    session.add(WeiboHotItem(user_id=1, title=title, heat=int(heat), rank=1,
                             captured_at=datetime.now() - timedelta(hours=hours_ago)))


def _xianyu_daily(session, title: str, want: int, days_ago: int) -> None:
    session.add(XianyuDaily(user_id=1, item_id=f"i{title[:6]}{days_ago}", title=title,
                            snap_date=(datetime.now() - timedelta(days=days_ago)).date().isoformat(),
                            want_count=want))


def test_velocity_and_acceleration_scoring(session, st) -> None:
    """3 轮 100→150→400:增速166%(+40) + 加速(+20) + 连续3轮(+15) + 量级(+15) = 90 → 爆发。"""
    for i, h in enumerate([100, 150, 400]):
        _weibo(session, "某游戏新版本", h, hours_ago=(3 - i) * 2)
    session.commit()
    signals = early_agent.detect_signals(session, 1, st)
    s = next(x for x in signals if x["kw"] == "某游戏新版本")
    assert s["score"] == 90 and "加速上涨" in s["parts"] and "增速+167%" in s["parts"]
    pushed = early_agent.agent_tick(session, 1, settings=st)
    assert pushed == 1
    stage = session.scalar(select(AgentStage))
    assert stage.stage == "爆发" and "加速上涨" in stage.parts


def test_lifecycle_escalation_and_stop_repeat(session, st) -> None:
    """生命周期:首推(上升)→ 同阶段不重推 → 升级(爆发)才再推 → 跌入回落提醒止损。"""
    for i, h in enumerate([120, 400, 900]):  # tick1: 增速125%(+40)+连续3轮(+15)+量级(+15)=70 → 上升
        _weibo(session, "某热门游戏新词", h, hours_ago=(4 - i) * 2)
    session.commit()
    assert early_agent.agent_tick(session, 1, settings=st) == 1  # 首推
    assert session.scalar(select(AgentStage)).stage == "上升"

    _weibo(session, "某热门游戏新词", 950, hours_ago=1)  # tick2: 增速仅5.6%,分降至30 → 无跃迁
    session.commit()
    n_sent = len(_FakeFeishu.sent)
    assert early_agent.agent_tick(session, 1, settings=st) == 0
    assert len(_FakeFeishu.sent) == n_sent  # 同阶段不重复推送
    assert session.scalar(select(AgentStage)).stage == "上升"  # 阶段记忆保留

    _weibo(session, "某热门游戏新词", 2500, hours_ago=0.5)  # tick3: 增速163%+加速 → 爆发 → 升级推送
    session.commit()
    assert early_agent.agent_tick(session, 1, settings=st) == 1
    assert session.scalar(select(AgentStage)).stage == "爆发"

    _weibo(session, "某热门游戏新词", 300, hours_ago=0.2)  # tick4: 2500→300 回落 → 止损提醒
    session.commit()
    assert early_agent.agent_tick(session, 1, settings=st) == 1
    assert session.scalar(select(AgentStage)).stage == "回落"
    assert "停止跟进" in _FakeFeishu.sent[-1]


def test_xianyu_string_timestamp_velocity(session, st) -> None:
    """闲鱼 snap_date 是字符串日期:增速/量级正常评分(回归:旧版新上榜信号因类型失效)。"""
    for i, w in enumerate([200, 200, 500]):
        _xianyu_daily(session, "某网盘资源", w, days_ago=2 - i)
    session.commit()
    signals = early_agent.detect_signals(session, 1, st)
    s = next(x for x in signals if x["kw"] == "某网盘资源")
    assert s["score"] >= 70  # 增速150%(+40) + 连续3轮(+15) + 量级(+15)
    assert early_agent.agent_tick(session, 1, settings=st) == 1
    assert _FakeFeishu.last_webhook == "https://open.feishu.cn/hook/xianyu"


def test_cross_resonance_boost_pushes_to_main(session, st) -> None:
    """同关键词微博+百度同时增速 → 共振+30,分数≥85 → 跨板块卡推总群。"""
    for i, h in enumerate([100, 260]):  # 增速160% → +40;量级+15
        _weibo(session, "某游戏新版本", h, hours_ago=(2 - i) * 2)
    for i, h in enumerate([100, 260]):
        session.add(BaiduHotItem(user_id=1, title="某游戏新版本下载", heat=int(h), rank=1,
                                 captured_at=datetime.now() - timedelta(hours=(2 - i) * 2)))
    session.commit()
    signals = early_agent.detect_signals(session, 1, st)
    assert all(s["score"] >= 85 for s in signals) and all("共振" in s["parts"] for s in signals)
    assert early_agent.agent_tick(session, 1, settings=st) == 2
    assert _FakeFeishu.last_webhook == "https://open.feishu.cn/hook/main"  # 共振推总群


def test_agent_disabled(session, st) -> None:
    _weibo(session, "某游戏新版本", 400, hours_ago=0)
    session.commit()
    from config.settings import Settings
    st_off = Settings(_env_file=None, is_dev=True, agent_enabled=False,
                      feishu_webhook="https://open.feishu.cn/hook/main")
    assert early_agent.agent_tick(session, 1, settings=st_off) == 0


def test_rank_jump_signal_weibo(session, st) -> None:
    """微博排名跳升 ≥3 名(+15):即使热度平稳也能靠排名速度捕捉起势。"""
    # 两轮热度持平(无增速信号),但排名 10 → 2(跳升 8)
    session.add(WeiboHotItem(user_id=1, title="某个突然爆火的词条", heat=5000, rank=10,
                             captured_at=datetime.now() - timedelta(hours=2)))
    session.add(WeiboHotItem(user_id=1, title="某个突然爆火的词条", heat=5100, rank=2,
                             captured_at=datetime.now()))
    session.commit()
    signals = early_agent.detect_signals(session, 1, st)
    s = next(x for x in signals if x["kw"] == "某个突然爆火的词条")
    assert "排名↑8" in s["parts"]
    # 分数:新上榜25 + 量级15 + 排名↑8(15) = 55 → 恰好到苗头线
    assert s["score"] >= 55
    assert early_agent.agent_tick(session, 1, settings=st) >= 1


def test_agent_learning_roundtrip_and_backtest(session, st) -> None:
    """自学习:权重持久化 + 空数据回测安全。"""
    from app.services.agent_learning import load_weights, save_weights, backtest_and_learn

    w = load_weights(session)
    assert set(DEFAULT_W_KEYS := w.keys()) == {"velocity", "new_entry", "repeat", "volume",
                                                "resonance", "rank_jump", "accel"}
    w["resonance"] = 35
    save_weights(session, w)
    assert load_weights(session)["resonance"] == 35

    out = backtest_and_learn(session, 1, settings=st)
    assert out["backtested"] == 0 and out["weights"]["resonance"] == 35
