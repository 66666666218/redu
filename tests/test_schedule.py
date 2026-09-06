"""每用户采集频率:设置校验、到期判定、调度 tick 单测(不联网、不起真调度器)。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User, UserCookie, UserSchedule
from app.services import schedule_service as svc
from app.services import scheduler


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def _add_user(session, user_id: int = 7) -> None:
    session.add(User(id=user_id, username=f"u{user_id}", password_hash="x"))
    session.commit()


# ---- 间隔校验 ----
def test_normalize_interval_ok() -> None:
    assert svc.normalize_interval(10) == 10
    assert svc.normalize_interval("30") == 30
    assert svc.normalize_interval(1440) == 1440


def test_normalize_interval_rejects_too_frequent() -> None:
    """低于下限会打爆三方接口并触发风控,必须挡在后端(前端限制不可信)。"""
    with pytest.raises(svc.ScheduleError):
        svc.normalize_interval(1)
    with pytest.raises(svc.ScheduleError):
        svc.normalize_interval(9)


def test_normalize_interval_rejects_invalid() -> None:
    with pytest.raises(svc.ScheduleError):
        svc.normalize_interval(99999)
    with pytest.raises(svc.ScheduleError):
        svc.normalize_interval("abc")


# ---- 读写 ----
def test_get_or_create_defaults(session) -> None:
    row = svc.get_or_create(session, 1, "douhot")
    assert row.interval_minutes == svc.DEFAULT_INTERVAL and row.enabled
    assert svc.get_or_create(session, 1, "douhot").id == row.id  # 幂等,不重复建


def test_unknown_section_rejected(session) -> None:
    with pytest.raises(svc.ScheduleError):
        svc.get_or_create(session, 1, "tiktok")


def test_list_schedules_fills_all_sections(session) -> None:
    out = svc.list_schedules(session, 1)
    assert [i["section"] for i in out["items"]] == list(svc.SECTIONS)
    assert out["min_interval"] == svc.MIN_INTERVAL and 10 in out["choices"]


def test_set_schedule_updates_interval_and_switch(session) -> None:
    assert svc.set_schedule(session, 1, "weibo", 10)["interval_minutes"] == 10
    out = svc.set_schedule(session, 1, "weibo", enabled=False)
    assert out["enabled"] is False and out["interval_minutes"] == 10  # 只改开关不动间隔
    assert out["next_run_at"] is None  # 关掉后不再排期


# ---- 到期判定 ----
def test_due_when_never_run(session) -> None:
    svc.get_or_create(session, 1, "weibo")
    assert [r.section for r in svc.due_schedules(session)] == ["weibo"]


def test_not_due_within_interval(session) -> None:
    row = svc.get_or_create(session, 1, "weibo")
    svc.set_schedule(session, 1, "weibo", 30)
    svc.mark_ran(session, row, datetime.now() - timedelta(minutes=29))
    assert svc.due_schedules(session) == []


def test_due_after_interval(session) -> None:
    row = svc.get_or_create(session, 1, "weibo")
    svc.set_schedule(session, 1, "weibo", 30)
    svc.mark_ran(session, row, datetime.now() - timedelta(minutes=31))
    assert len(svc.due_schedules(session)) == 1


def test_disabled_never_due(session) -> None:
    svc.get_or_create(session, 1, "weibo")
    svc.set_schedule(session, 1, "weibo", enabled=False)
    assert svc.due_schedules(session) == []


def test_ten_minute_interval_runs_six_times_an_hour(session) -> None:
    """10 分钟档:模拟一小时内逐分钟 tick,应恰好到期 6 次。"""
    row = svc.get_or_create(session, 1, "douhot")
    svc.set_schedule(session, 1, "douhot", 10)
    start = datetime(2026, 9, 1, 0, 0)
    svc.mark_ran(session, row, start)
    fired = 0
    for minute in range(1, 61):
        now = start + timedelta(minutes=minute)
        if svc.due_schedules(session, now):
            fired += 1
            svc.mark_ran(session, row, now)
    assert fired == 6


# ---- 调度 tick ----
def _patch_session(monkeypatch: pytest.MonkeyPatch, session) -> None:
    import app.db

    monkeypatch.setattr(app.db, "get_session_local", lambda: (lambda: session))
    monkeypatch.setattr(session, "close", lambda: None)


def test_tick_runs_due_sections(monkeypatch: pytest.MonkeyPatch, session) -> None:
    _patch_session(monkeypatch, session)
    _add_user(session, 7)
    session.add(UserCookie(user_id=7, platform="weibo", cookie="x"))
    session.commit()
    called: list[tuple[int, str]] = []
    monkeypatch.setattr(
        scheduler, "_runners", lambda: {s: (lambda db, uid, st, s=s: called.append((uid, s))) for s in svc.SECTIONS}
    )
    out = scheduler.collect_tick()
    # 只配了微博 Cookie:微博/百度跑(百度是公开接口无需 Cookie),闲鱼/抖音因缺 Cookie 被跳过;
    # 公众号监听不需要 Cookie(用 dajiala key),同样会跑
    assert out == {"due": 3, "ok": 3, "failed": 0, "skipped": 2}
    assert called == [(7, "weibo"), (7, "baidu"), (7, "wechat")]


def test_tick_enrolls_new_users(monkeypatch: pytest.MonkeyPatch, session) -> None:
    """新注册用户不必先打开设置页,调度器会自动补齐频率记录并纳入采集。"""
    _patch_session(monkeypatch, session)
    _add_user(session, 7)
    monkeypatch.setattr(scheduler, "_runners", lambda: {s: (lambda db, uid, st: None) for s in svc.SECTIONS})
    scheduler.collect_tick()
    assert {r.section for r in session.scalars(select(UserSchedule)).all()} == set(svc.SECTIONS)


def test_tick_skips_section_without_cookie(monkeypatch: pytest.MonkeyPatch, session) -> None:
    """缺 Cookie 的板块跳过且不标记,配好 Cookie 后下一轮立刻能跑(而不是每轮刷失败记录)。"""
    _patch_session(monkeypatch, session)
    _add_user(session, 7)
    monkeypatch.setattr(scheduler, "_runners", lambda: {s: (lambda db, uid, st: None) for s in svc.SECTIONS})
    assert scheduler.collect_tick() == {"due": 2, "ok": 2, "failed": 0, "skipped": 3}
    assert len(svc.due_schedules(session)) == 3  # 未被标记(微博/闲鱼/抖音缺 Cookie;百度/公众号已跑已标记)

    session.add(UserCookie(user_id=7, platform="douyin", cookie="x"))
    session.commit()
    assert scheduler.collect_tick()["ok"] == 1


def test_tick_marks_ran_even_on_failure(monkeypatch: pytest.MonkeyPatch, session) -> None:
    """采集失败也要标记,否则失败任务每分钟重试会把三方接口打爆。"""
    _patch_session(monkeypatch, session)
    _add_user(session, 7)
    session.add(UserCookie(user_id=7, platform="douyin", cookie="x"))
    session.commit()

    def boom(db, uid, st):  # type: ignore[no-untyped-def]
        raise RuntimeError("Cookie 失效")

    monkeypatch.setattr(scheduler, "_runners", lambda: {"douhot": boom})
    out = scheduler.collect_tick()
    assert out["failed"] == 1
    assert svc.get_or_create(session, 7, "douhot").last_run_at is not None
    # 抖音已标记不再重跑;微博/闲鱼/百度/公众号因无 runner 被跳过、未标记,仍在待跑队列
    assert [r.section for r in svc.due_schedules(session)] == ["weibo", "xianyu", "baidu", "wechat"]


def _fire_weekday(expr: str, fallback: dict) -> int:
    """计算 cron 的下次触发是星期几(0=Mon)。用实际行为验证,而非内部字段。"""
    import datetime as dt

    from apscheduler.util import localize

    from app.services.scheduler import _cron_trigger

    tr = _cron_trigger(expr, fallback)
    anchor = localize(dt.datetime(2026, 9, 7, 0, 0), tr.timezone)  # 周一 00:00
    return tr.get_next_fire_time(None, anchor).weekday()


def test_cron_posix_weekday_conversion() -> None:
    """星期几 POSIX(0=Sun)→APScheduler(0=Mon):'0 9 * * 1' 应为周一而非周二。"""
    fallback = {"day_of_week": "mon", "hour": 9, "minute": 0}
    assert _fire_weekday("0 9 * * 1", fallback) == 0   # 周一
    assert _fire_weekday("0 20 * * 0", {"day_of_week": "sun", "hour": 20, "minute": 0}) == 6  # 周日


def test_cron_field_count_violation_falls_back() -> None:
    """6 段 cron(带秒)应落到安全默认(周日 20:00),而不是静默丢掉第 6 段。"""
    default = {"day_of_week": "sun", "hour": 20, "minute": 0}
    assert _fire_weekday("30 * * * * *", default) == 6  # 6 段 → 回退默认(周日)
    assert _fire_weekday("0 9 * * 1", default) == 0      # 合法 5 段 → 不被破坏(周一)
