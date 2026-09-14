"""数据源健康中心单测:三态判定(HEALTHY/DEGRADED/CIRCUIT_OPEN)。"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import GroupMember, RunRecord, User, UserCookie, UserSchedule, WeiboHotItem
from app.services import health


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=1, username="a", email="a@b.c", password_hash="x"))
    db.commit()
    yield db
    db.close()


def _settings():
    import types
    return types.SimpleNamespace(xianyu_cooldown_minutes=30, xianyu_proxy_url="")


def test_healthy_when_recent_success(session):
    now = datetime.now()
    session.add(UserSchedule(user_id=1, section="weibo", interval_minutes=60, enabled=True))
    session.add(UserCookie(user_id=1, platform="weibo",
                           cookie=__import__("app.security", fromlist=["encrypt_cookie"]).encrypt_cookie("ck=1")))
    session.add(WeiboHotItem(user_id=1, title="热点词样本", heat=100, rank=1, captured_at=now))
    session.add(RunRecord(run_id="1", user_id=1, kind="weibo", status="success", started_at=now))
    session.commit()
    rows = health.source_health(session, 1, _settings())
    weibo = next(r for r in rows if r["section"] == "weibo")
    assert weibo["health"] == "HEALTHY" and not weibo["problems"]


def test_open_when_cookie_missing(session):
    now = datetime.now()
    session.add(UserSchedule(user_id=1, section="weibo", interval_minutes=60, enabled=True))
    session.add(RunRecord(run_id="1", user_id=1, kind="weibo", status="success", started_at=now))
    session.add(WeiboHotItem(user_id=1, title="热点词样本", heat=100, rank=1, captured_at=now))
    session.commit()
    weibo = next(r for r in health.source_health(session, 1, _settings()) if r["section"] == "weibo")
    assert weibo["health"] == "CIRCUIT_OPEN" and any("Cookie" in p for p in weibo["problems"])


def test_degraded_on_freshness_and_fails(session):
    now = datetime.now()
    session.add(UserSchedule(user_id=1, section="weibo", interval_minutes=60, enabled=True))
    session.add(UserCookie(user_id=1, platform="weibo",
                           cookie=__import__("app.security", fromlist=["encrypt_cookie"]).encrypt_cookie("ck=1")))
    session.add(WeiboHotItem(user_id=1, title="热点词样本", heat=100, rank=1,
                             captured_at=now - timedelta(hours=10)))  # 间隔 1h,停滞 10h
    for i in range(4):  # 24h 失败 4 次(≥3)
        session.add(RunRecord(run_id=str(i), user_id=1, kind="weibo", status="failed",
                              started_at=now - timedelta(hours=i)))
    session.commit()
    weibo = next(r for r in health.source_health(session, 1, _settings()) if r["section"] == "weibo")
    assert weibo["health"] == "DEGRADED"
    assert any("停滞" in p for p in weibo["problems"]) and any("失败" in p for p in weibo["problems"])


def test_circuit_open_on_xianyu_cooldown(session):
    now = datetime.now()
    session.add(UserSchedule(user_id=1, section="xianyu", interval_minutes=60, enabled=True))
    session.add(UserCookie(user_id=1, platform="goofish",
                           cookie=__import__("app.security", fromlist=["encrypt_cookie"]).encrypt_cookie("ck=1")))
    session.add(RunRecord(run_id="1", user_id=1, kind="xianyu", status="failed",
                          detail="XianyuVerify: 滑块", started_at=now))
    session.commit()
    xy = next(r for r in health.source_health(session, 1, _settings()) if r["section"] == "xianyu")
    assert xy["health"] == "CIRCUIT_OPEN" and any("冷却" in p for p in xy["problems"])
