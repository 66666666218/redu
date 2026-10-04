"""作业心跳单测(2026-10-04)。

背景:`resource_presence` 注册着、trigger 正确、`enabled=True`,却**六天一次没跑** ——
因为作业的"执行事实"没有统一落点(有的写进 `runs` 但换个名字,有的压根不写),
"配置说每天跑"与"实际跑没跑"之间无从对照。心跳就是补这个洞:
**注册了就一定有心跳**,查 `job_heartbeats` 即可。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: F401,E402
from app.db.models import JobHeartbeat  # noqa: E402
from app.services import scheduler as sch  # noqa: E402


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def patch_db(monkeypatch, engine):
    """把心跳用的会话工厂指向内存库。"""
    import app.db.database as appdb
    monkeypatch.setattr(appdb, "get_session_local", lambda: sessionmaker(bind=engine))
    return engine


def _beat_of(engine, job_id: str) -> JobHeartbeat | None:
    with sessionmaker(bind=engine)() as s:
        return s.scalar(select(JobHeartbeat).where(JobHeartbeat.job_id == job_id))


def test_successful_run_writes_heartbeat(patch_db, engine) -> None:
    sch._safe(lambda: None, "demo_job")()
    row = _beat_of(engine, "demo_job")
    assert row is not None, "作业跑过了却没心跳 —— 又回到'查不出跑没跑'的老问题"
    assert row.run_count == 1 and row.error_count == 0
    assert row.last_run_at is not None and row.last_ok_at is not None


def test_failing_run_is_recorded_and_does_not_propagate(patch_db, engine) -> None:
    """失败也要有心跳(**而且带原因**)—— 否则"跑了但一直错"和"根本没跑"分不开。"""
    def _boom():
        raise RuntimeError("模拟炸了")

    sch._safe(_boom, "bad_job")()          # 不应抛出去(抛出去会杀死调度器)
    row = _beat_of(engine, "bad_job")
    assert row is not None
    assert row.run_count == 1 and row.error_count == 1
    assert row.last_ok_at is None
    assert "模拟炸了" in row.last_error


def test_repeated_runs_accumulate(patch_db, engine) -> None:
    f = sch._safe(lambda: None, "tick_job")
    for _ in range(3):
        f()
    row = _beat_of(engine, "tick_job")
    assert row.run_count == 3 and row.error_count == 0


def test_no_job_id_means_no_heartbeat(patch_db, engine) -> None:
    """没给 job_id 就不写(避免把无关调用也记成作业)。"""
    sch._safe(lambda: None)()
    with sessionmaker(bind=engine)() as s:
        assert s.scalars(select(JobHeartbeat)).all() == []


def test_heartbeat_failure_never_breaks_the_job(monkeypatch) -> None:
    """⚠️ 心跳只是"顺手记一笔":DB 锁了/表没了,**作业本身必须照常完成**。"""
    import app.db.database as appdb

    def _boom():
        raise RuntimeError("数据库锁了")

    monkeypatch.setattr(appdb, "get_session_local", _boom)
    ran = []
    sch._safe(lambda: ran.append(1), "j")()      # 不得抛
    assert ran == [1], "心跳写失败把作业也带崩了"


def test_add_job_wires_the_heartbeat(patch_db, engine) -> None:
    """接线测试:**经 `_add_job` 注册的作业**,执行后必须有心跳(而不是只有裸调 `_safe` 才有)。"""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    s = BackgroundScheduler(timezone="Asia/Shanghai")
    ok = sch._add_job(s, lambda: None, CronTrigger(minute="*/5"), "wired_job", "both")
    assert ok is True
    s.get_job("wired_job").func()                  # 手工触发一次
    assert _beat_of(engine, "wired_job") is not None, "_add_job 没把 job_id 接上"


def test_add_job_sets_a_misfire_grace_by_default() -> None:
    """⚠️ **不能吃 APScheduler 默认的 1 秒宽限**(2026-10-04 定案)。

    本机是常驻机器、看门狗还会重启(实测 6 天重启 125 次):触发那一刻只要在休眠唤醒、
    重启、或调度线程被占住,1 秒就过去了 —— 作业**被直接丢弃且不报错**。
    实测证据:`wechat_collect_tick`(08:00,**显式设了 3600**)天天正常,
    而隔壁 `resource_presence`(09:00,**没设,用默认 1 秒**)**六天一次没跑**。
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    s = BackgroundScheduler(timezone="Asia/Shanghai")
    sch._add_job(s, lambda: None, CronTrigger(minute="0", hour="9"), "fixed_job", "both")
    assert s.get_job("fixed_job").misfire_grace_time == sch._DEFAULT_MISFIRE_GRACE

    # 调用方显式传的仍然优先,不被兜底覆盖
    sch._add_job(s, lambda: None, CronTrigger(minute="*/5"), "explicit_job", "both",
                 misfire_grace_time=42)
    assert s.get_job("explicit_job").misfire_grace_time == 42


def test_interval_estimate_is_conservative() -> None:
    """期望间隔只做**确定性**估计:拿不准的返回 None(宁可漏报也不误报,否则"超期"变噪音)。"""
    import sys
    from pathlib import Path

    from apscheduler.triggers.cron import CronTrigger

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import job_liveness

    assert job_liveness._interval_seconds(CronTrigger(minute="*/20")) == 1200
    assert job_liveness._interval_seconds(CronTrigger(minute="*")) == 60
    assert job_liveness._interval_seconds(CronTrigger(minute="0", hour="9")) is None  # 定点 → 估不出


# ---------------------------------------------- "没有心跳"要分两类(2026-10-04)

class _J:
    """够用即可:对账脚本只读 `.id` 与 `.trigger`。"""

    def __init__(self, jid, trigger):
        self.id, self.trigger = jid, trigger


def _job_liveness():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import job_liveness

    return job_liveness


def test_liveness_does_not_flag_jobs_that_are_not_due_yet() -> None:
    """⚠️ 2026-10-04 实测踩到:心跳表是**当天 03:10 才上线**的,于是"每天/每周跑一次"的作业
    全被报成"从没执行过" —— 一次列出 4 个**全是假的**:`douyin_leads`(每天 11:00)、
    `pan_discovery`(每天 11:30)当天稍晚就会自己补上;`cross_account_discover`(周一/周四)、
    `recruit_reminder`(周一)本来就是今天(周日)还不到。**误报的代价是重复劳动。**
    """
    from datetime import datetime

    from apscheduler.triggers.cron import CronTrigger

    jl = _job_liveness()
    baseline, now = datetime(2026, 10, 4, 3, 10), datetime(2026, 10, 4, 10, 50)
    jobs = [
        _J("daily_11", CronTrigger(minute="0", hour="11")),                       # 今天 11:00 → 未到
        _J("weekly_mon", CronTrigger(minute="40", hour="9", day_of_week="mon")),  # 周一 → 未到
        _J("daily_08", CronTrigger(minute="0", hour="8")),                        # 今天 08:00 → 早过了
        _J("has_beat", CronTrigger(minute="0", hour="9")),                        # 有心跳
    ]
    missing, not_yet = jl.classify_missing(jobs, {"has_beat": object()}, now, baseline)

    assert missing == ["daily_08"], "过了触发点还没心跳的,才是真问题"
    assert sorted(not_yet) == ["daily_11", "weekly_mon"], "还没到点的别报出来"


def test_liveness_flags_everything_when_baseline_is_unknown() -> None:
    """心跳表还空着(基线查不到)时**不许假装没事** —— 维持旧行为,一律按"漏跑"报。"""
    from datetime import datetime

    from apscheduler.triggers.cron import CronTrigger

    jl = _job_liveness()
    jobs = [_J("a", CronTrigger(minute="0", hour="11")), _J("b", CronTrigger(minute="*"))]
    missing, not_yet = jl.classify_missing(jobs, {}, datetime(2026, 10, 4, 10, 50), None)

    assert not_yet == [], "基线未知时不能替作业开脱"
    assert missing == ["a", "b"]


def test_should_have_fired_handles_aware_vs_naive() -> None:
    """库里的 `baseline` 是 **naive**,trigger 给的下一跳是 **aware** —— 直接比会 `TypeError`
    把整个对账脚本打崩。必须走进保守分支,而不是抛。"""
    from datetime import datetime

    from apscheduler.triggers.cron import CronTrigger

    jl = _job_liveness()
    tr = CronTrigger(minute="0", hour="11")          # 用本机时区,免依赖测试机所在时区
    assert jl._should_have_fired(tr, datetime(2026, 10, 4, 3, 10),
                                 datetime(2026, 10, 4, 10, 50)) is False   # 11:00 还没到
    assert jl._should_have_fired(tr, datetime(2026, 10, 4, 3, 10),
                                 datetime(2026, 10, 4, 11, 30)) is True    # 早过了
