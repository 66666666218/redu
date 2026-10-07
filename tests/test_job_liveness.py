"""作业落实性对账单测(2026-10-07)。

这个模块存在的理由只有一个:**"注册了却没跑"必须自动说出来**。
下面两条是本模块的两个真实事故,不是假想:
  · 它原来只在 `scripts/` 里、**没有定时调用** ⇒ 靠人记得,而人不会记得;
  · 它的 `_interval_seconds` 对 `hour='4,8,14,20'` 这种**多定点返回 None** ⇒
    公众号监听作业**恰好被排除**,30 小时没跑也判不出来。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: E402,F401
from app.db.database import Base  # noqa: E402
from app.db.models import JobHeartbeat, User  # noqa: E402
from app.services import job_liveness as jl  # noqa: E402

NOW = datetime.now()


class _Job:
    def __init__(self, job_id: str, trigger) -> None:
        self.id = job_id
        self.trigger = trigger


class _Sched:
    def __init__(self, jobs) -> None:
        self._jobs = jobs

    def get_jobs(self):
        return self._jobs


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class TestInterval:
    def test_多定点取最大空档而不是平均(self) -> None:
        """★ **本模块最要紧的一条**:`hour='4,8,14,20'` 的平均间隔是 6h,
        但 20:00→次日 04:00 的空档是 **8h** —— 拿 6h 当基准,每天正常的夜间空档
        都会被算成"超期"。判"该跑没跑"要的是**最坏情况**。"""
        assert jl.trigger_interval_seconds(
            CronTrigger(hour="4,8,14,20", minute="2")) == 8 * 3600

    def test_监听作业不再被判成_算不出(self) -> None:
        """⚠️ **回归**:老实现对这个触发器直接 `return None`(注释写"间隔由哪一天决定"),
        于是 `wechat_collect_tick` **永远不会**被算成超期 —— 而它正是停跑了 30 小时的那个。"""
        assert jl.trigger_interval_seconds(
            CronTrigger(hour="4,8,14,20", minute="2")) is not None

    def test_各类触发器的估算(self) -> None:
        f = jl.trigger_interval_seconds
        assert f(CronTrigger(minute="*")) == 60
        assert f(CronTrigger(minute="*/30")) == 30 * 60
        assert f(CronTrigger(minute="7,27,47")) == 3600 / 3
        assert f(CronTrigger(hour="*/4", minute="10")) == 4 * 3600
        assert f(CronTrigger(hour="9,17", minute="10")) == 16 * 3600   # 跨夜 16h
        assert f(CronTrigger(hour=3, minute="10")) == 86400            # 每天一次
        assert f(CronTrigger(day_of_week="0", hour=10, minute=15)) == 7 * 86400

    def test_认不出就返回None不猜(self) -> None:
        """编一个数出来会让"超期"变成噪音,然后被无视 —— 比不报还糟。"""
        class _Weird:
            fields = None
        assert jl.trigger_interval_seconds(_Weird()) is None


class TestAudit:
    def test_停跑会被判超期(self, session) -> None:
        """★ 用真实形态复现那次事故:监听作业 30 小时没跑 ⇒ 必须被判出来。"""
        sched = _Sched([_Job("wechat_collect_tick",
                             CronTrigger(hour="4,8,14,20", minute="2"))])
        session.add(JobHeartbeat(job_id="wechat_collect_tick",
                                 last_run_at=NOW - timedelta(hours=30),
                                 first_seen_at=NOW - timedelta(days=30)))
        session.commit()
        r = jl.audit(session, scheduler=sched, now=NOW)
        assert [s["job_id"] for s in r["stale"]] == ["wechat_collect_tick"]
        # 30h > 8h×3=24h ⇒ 超期;但没到 8h×3×2=48h,所以体检里该报黄不该报红
        assert r["stale"][0]["expect_s"] == 8 * 3600

    def test_晚一轮不算超期(self, session) -> None:
        """10 小时没跑(比 8h 多一个尾巴)不该报 —— 宁可晚一轮,也别把噪音当告警。"""
        sched = _Sched([_Job("wechat_collect_tick",
                             CronTrigger(hour="4,8,14,20", minute="2"))])
        session.add(JobHeartbeat(job_id="wechat_collect_tick",
                                 last_run_at=NOW - timedelta(hours=10),
                                 first_seen_at=NOW - timedelta(days=30)))
        session.commit()
        assert jl.audit(session, scheduler=sched, now=NOW)["stale"] == []

    def test_注册了从没跑过要报出来(self, session) -> None:
        sched = _Sched([_Job("wechat_collect_tick",
                             CronTrigger(hour="4,8,14,20", minute="2"))])
        session.add(JobHeartbeat(job_id="wechat_collect_tick",
                                 first_seen_at=NOW - timedelta(days=3)))
        session.commit()
        r = jl.audit(session, scheduler=sched, now=NOW)
        assert r["never"] == ["wechat_collect_tick"]

    def test_还没到第一次触发点的不算漏跑(self, session) -> None:
        """⚠️ 误报的代价是"重复劳动":每周一 10:15 的作业在周三被报"从没跑过"是假问题。"""
        sched = _Sched([_Job("chain_ordering", CronTrigger(day_of_week="1", hour=10, minute=15))])
        session.add(JobHeartbeat(job_id="chain_ordering",
                                 first_seen_at=NOW - timedelta(minutes=30)))
        session.commit()
        r = jl.audit(session, scheduler=sched, now=NOW)
        assert r["never"] == [] and r["not_yet"] == ["chain_ordering"]


class TestHealthSection:
    def test_全绿时给绿(self, session) -> None:
        from app.services import chain_health as ch

        sched = _Sched([_Job("wechat_collect_tick",
                             CronTrigger(hour="4,8,14,20", minute="2"))])
        session.add(JobHeartbeat(job_id="wechat_collect_tick", last_run_at=NOW,
                                 first_seen_at=NOW - timedelta(days=30)))
        session.commit()
        import app.services.job_liveness as _jl

        orig = _jl.audit
        _jl.audit = lambda db, **k: orig(db, scheduler=sched, now=NOW)
        try:
            items = ch.check_job_liveness(session)
        finally:
            _jl.audit = orig
        assert items[0]["level"] == ch.GREEN

    def test_停跑要报出来且带作业名(self, session) -> None:
        from app.services import chain_health as ch

        sched = _Sched([_Job("wechat_collect_tick",
                             CronTrigger(hour="4,8,14,20", minute="2"))])
        session.add(JobHeartbeat(job_id="wechat_collect_tick",
                                 last_run_at=NOW - timedelta(hours=30),
                                 first_seen_at=NOW - timedelta(days=30)))
        session.commit()
        import app.services.job_liveness as _jl

        orig = _jl.audit
        _jl.audit = lambda db, **k: orig(db, scheduler=sched, now=NOW)
        try:
            items = ch.check_job_liveness(session)
        finally:
            _jl.audit = orig
        assert items[0]["level"] == ch.YELLOW          # 30h:超 3× 但没到 6×
        assert "wechat_collect_tick" in items[0]["detail"]

    def test_对账自己挂了也要说出来(self, session, monkeypatch) -> None:
        """不能因为对账失败就静默返回空 —— 那正是本仓最怕的那种失败。"""
        from app.services import chain_health as ch
        from app.services import job_liveness as _jl

        monkeypatch.setattr(_jl, "audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸")))
        items = ch.check_job_liveness(session)
        assert items[0]["level"] == ch.YELLOW and "对账本身失败" in items[0]["detail"]


class TestSourceGuard:
    def test_体检必须带上作业落实性(self) -> None:
        """★ **源码级守卫**:这条检查必须挂在每天那份体检里。

        ⚠️ 它原来只活在 `scripts/job_liveness.py`(要人手动跑),所以 30 小时的停跑
        一次都没被说出来 —— 而现在这条检查本身**也可能被谁顺手摘掉**,
        摘掉之后一样是静默的。所以钉住它。
        """
        import inspect

        from app.services import chain_health as ch

        src = inspect.getsource(ch.collect_sections)
        assert "check_job_liveness" in src, "作业落实性检查从每日体检里掉了"
        titles = [t for t, _ in ch.collect_sections.__wrapped__()] if hasattr(
            ch.collect_sections, "__wrapped__") else None
        assert titles is None or any("作业" in x for x in titles)
