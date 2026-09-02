"""飞书日报/实时提醒单测:签名算法、批次涨跌判定、日报文本、实时去重。

全部不联网:只测数据推演与签名计算,webhook 发送用 Fake 客户端替身。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import base64
import hashlib
import hmac
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import DouhotWord, FeishuAlert, User, WeiboHotItem, XianyuItem
from app.services import feishu
from app.services.feishu import _delta, _sign, build_daily, run_feishu_keyword_alerts, run_feishu_realtime

SECRET = "test-sign-secret-placeholder"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(username="t", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    yield db
    db.close()


def _settings(**kw):
    from config.settings import Settings

    return Settings(
        _env_file=None,
        feishu_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        feishu_secret=SECRET,
        feishu_hot_rank_jump=3,
        feishu_hot_ratio=0.30,
        feishu_alert_cooldown_hours=6,
        **kw,
    )


# ---- 签名 ----
def test_sign_matches_feishu_algorithm() -> None:
    """用飞书官方算法独立算一遍,确认实现正确。

    官方:sign = base64(HmacSHA256("{timestamp}\\n{secret}", ""))——以 string_to_sign 为 key,空消息。
    """
    ts = 1700000000
    key = f"{ts}\n{SECRET}".encode()
    expect = base64.b64encode(hmac.new(key, msg=b"", digestmod=hashlib.sha256).digest()).decode()
    assert _sign(SECRET, ts) == expect
    # 用错方向(secret 当 key)会产生不同结果,证明实现用的是官方方向
    wrong = base64.b64encode(hmac.new(SECRET.encode(), f"{ts}\n{SECRET}".encode(), hashlib.sha256).digest()).decode()
    assert _sign(SECRET, ts) != wrong


def test_sign_deterministic() -> None:
    assert _sign(SECRET, 123) == _sign(SECRET, 123) and _sign(SECRET, 123) != _sign(SECRET, 124)


# ---- 涨跌判定(_delta)----
def _w(title, rank, ts=None):
    return WeiboHotItem(user_id=1, title=title, rank=rank, heat=rank * 1000, captured_at=ts or datetime.now())


def test_delta_up_down_new_stay(session) -> None:
    cur, prev = {"a": _w("a", 2)}, {"a": _w("a", 5), "b": _w("b", 1)}
    assert _delta("weibo", cur["a"], prev) == ("up", "+3名")   # 5 → 2:升 3 名
    assert _delta("weibo", _w("c", 4, datetime.now() + timedelta(days=1)), prev) == ("new", "")  # 新增
    assert _delta("weibo", _w("b", 1, datetime.now() + timedelta(days=1)), prev) == ("stay", "")  # 同排名持平
    assert _delta("weibo", _w("a", 8, datetime.now() + timedelta(days=1)), prev) == ("down", "-3名")


def test_delta_douhot_uses_score_ratio(session) -> None:
    prev = DouhotWord(user_id=1, title="词", score=100, created_at=datetime.now())
    cur_hot = DouhotWord(user_id=1, title="词", score=150, created_at=datetime.now() + timedelta(days=1))
    cur_cold = DouhotWord(user_id=1, title="词", score=90, created_at=datetime.now() + timedelta(days=1))
    assert _delta("douhot", cur_hot, {"词": prev}) == ("up", "+50%")        # 100 → 150
    assert _delta("douhot", cur_cold, {"词": prev}) == ("down", "-10%")     # 100 → 90


# ---- 批次切分与日报 ----
def seed_weibo(session, user_id=1):
    t1, t2 = datetime(2026, 9, 1, 8), datetime(2026, 9, 2, 8)
    # 上一批(9-1):A 第 5 名,B 第 1 名,C 第 3 名
    for title, rank in [("A", 5), ("B", 1), ("C", 3)]:
        session.add(WeiboHotItem(user_id=user_id, title=title, rank=rank, heat=rank * 1000, captured_at=t1))
    # 当前批(9-2):A 升到第 2 名,B 掉到第 4 名,D 新增
    for title, rank in [("A", 2), ("B", 4), ("D", 7)]:
        session.add(WeiboHotItem(user_id=user_id, title=title, rank=rank, heat=100000, captured_at=t2))
    session.commit()


def test_batches_split_into_two_snapshots(session) -> None:
    seed_weibo(session)
    cur, prev = feishu._batches(session, 1, "weibo")
    assert set(cur) == {"A", "B", "D"} and set(prev) == {"A", "B", "C"}
    # 当前批名次:A=2 B=4 D=7;上一批名次:A=5 B=1 C=3
    assert getattr(cur["A"], "rank") == 2 and getattr(prev["A"], "rank") == 5


def test_daily_lines_include_rank_tags(session) -> None:
    seed_weibo(session)
    text = build_daily(session, 1, _settings())
    assert "微博热搜" in text
    assert "A" in text and "🔥+3名" in text          # 5→2 升 3 名
    assert "D" in text and "✅新增" in text           # 新出现
    assert "B" in text and "📉-3名" in text          # 1→4 掉 3 名
    assert "分析:" in text


# ---- 实时提醒(替换发送为假客户端)----
def test_realtime_only_pushes_new_and_big_jump(session, monkeypatch) -> None:
    seed_weibo(session)
    sent = []
    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = run_feishu_realtime("weibo", 1, _settings(), db=session)
    # 触发:新增 D(新增即推)、A 升 3 名(≥3 名);B 掉 3 名不推;C 只在上一批不推
    assert n == 2
    joined = sent[0]
    assert "D" in joined and "新增" in joined
    assert "A" in joined and "+3名" in joined
    assert "B" not in joined


def test_realtime_respects_cooldown(session, monkeypatch) -> None:
    seed_weibo(session)
    n_calls = {"n": 0}

    def fake_send(self, t):
        n_calls["n"] += 1
        return True

    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": fake_send})())
    settings = _settings()
    first = run_feishu_realtime("weibo", 1, settings, db=session)
    second = run_feishu_realtime("weibo", 1, settings, db=session)  # 已写去重表,冷却期内应全走冷却 → 不重推
    assert first == 2 and second == 0 and n_calls["n"] == 1
    assert session.scalar(select(FeishuAlert).where(FeishuAlert.title == "D")) is not None


def test_keyword_burst_alert(monkeypatch, session) -> None:
    """智能体预警:关注词呈加速上升时推送"可能爆发",且进冷却去重。"""
    from app.services import tenant
    from app.db.models import DouhotWatch, DouhotWatchSnap

    # 关注一个词,喂它一段加速上升的热度序列
    session.add(DouhotWatch(user_id=1, list_type="word", keyword="爆点"))
    for v in [1000, 1100, 1300, 1800, 2600]:
        session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="爆点", score=v, rank_now=1))
    session.commit()

    sent = []
    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = run_feishu_keyword_alerts(1, _settings(), db=session)
    assert n == 1
    assert "可能爆发" in sent[0] and "爆点" in sent[0]
    # 冷却期内再跑 → 不重推
    assert run_feishu_keyword_alerts(1, _settings(), db=session) == 0


def test_keyword_burst_skips_when_not_rising(monkeypatch, session) -> None:
    from app.db.models import DouhotWatch, DouhotWatchSnap

    session.add(DouhotWatch(user_id=1, list_type="word", keyword="退潮"))
    for v in [2000, 1500, 1000, 500]:
        session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="退潮", score=v, rank_now=1))
    session.commit()
    n_calls = {"n": 0}
    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (n_calls.__setitem__("n", n_calls["n"] + 1), True)[1]})())
    assert run_feishu_keyword_alerts(1, _settings(), db=session) == 0
    assert n_calls["n"] == 0  # 回落期不推


def test_daily_includes_keyword_agent_section(session) -> None:
    """日报应把关注词的智能体分析(趋势/预测/置信度)带进去。"""
    from app.db.models import DouhotWatch, DouhotWatchSnap

    session.add(DouhotWatch(user_id=1, list_type="word", keyword="爆点"))
    for v in [1000, 1100, 1300, 1800, 2600]:
        session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="爆点", score=v, rank_now=1))
    session.commit()
    text = build_daily(session, 1, _settings())
    assert "关键词关注 · 智能体" in text
    assert "爆点" in text
    assert "上升期" in text and "上升期" in text


def test_scheduler_registers_insight_job() -> None:
    """调度器应注册"爆点回顾"周报 job(feishu_insight)。"""
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.services.scheduler import build_jobs

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)
    ids = [j.id for j in sched.get_jobs()]
    assert "feishu_insight" in ids
    sched.shutdown(wait=False) if sched.running else None


def test_burst_confidence_gate(monkeypatch, session) -> None:
    """置信度分级:FEISHU_BURST_MIN_CONFIDENCE=高 时,"中"置信的爆发不实时推(只进日报/洞察)。"""
    from app.db.models import DouhotWatch, DouhotWatchSnap

    from app.services import keyword_agent as ka

    session.add(DouhotWatch(user_id=1, list_type="word", keyword="中置信词"))
    for i in range(5):
        session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="中置信词", score=100 + i * 50, rank_now=1))
    session.commit()
    # 模拟 analyze 返回"中"置信的爆发
    monkeypatch.setattr(ka, "analyze", lambda kw, v: {
        "keyword": kw, "burst": True, "trend_label": "上升期", "growth": 0.5,
        "forecast_next": 100, "confidence": "中", "points": 5,
    })
    # min=高 → 不推
    sent = []
    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = feishu.run_feishu_keyword_alerts(1, _settings(feishu_burst_min_confidence="高"), db=session)
    assert n == 0 and not sent
    # min=中 → 推
    n2 = feishu.run_feishu_keyword_alerts(1, _settings(feishu_burst_min_confidence="中"), db=session)
    assert n2 == 1 and sent and "中置信词" in sent[0]


def test_agent_confidence_rank() -> None:
    assert feishu._agent_confidence_rank("高") > feishu._agent_confidence_rank("中")
    assert feishu._agent_confidence_rank("中") > feishu._agent_confidence_rank("低")
    assert feishu._agent_confidence_rank(None) == 0
