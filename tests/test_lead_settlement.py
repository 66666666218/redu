"""线索结算对账单测(2026-10-03)。

**它守的是什么**:结算回流此前缺的**不是代码,是数据** —— 链接级真实转存数在夸克侧不可得
(2026-09-29 定案),而唯一的真值入口(周录)一次都没录过。这里把**系统侧量级**与
**人工周录**并排摆出来,并**严格区分"你还没录"与"这周真没有"**。
"""
import json
import os
from datetime import date, datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import DouyinLead, PanRecruitWeekly, User
from app.services import lead_settlement as ls


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _monday(weeks_ago: int = 0) -> date:
    today = date.today()
    return today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_ago)


def _lead(session, aweme_id: str, share: int, day: date) -> None:
    session.add(DouyinLead(user_id=1, aweme_id=aweme_id, share_count=share,
                           found_date=day.isoformat(), found_at=datetime.now()))


def test_weekly_report_aggregates_leads_and_flags_recorded_weeks(session) -> None:
    """线索按周聚合;**没录的周必须能被一眼认出**(`has_record=False`)。

    ⚠️ 这条是关键:`recorded_total=0` 与"还没录"是**两件不同的事** ——
    混在一起就会把"忘了录"读成"这周没拉新"。
    """
    this_mon = _monday(0)
    _lead(session, "a", 176, this_mon)
    _lead(session, "b", 24, this_mon + timedelta(days=2))
    _lead(session, "c", 100, _monday(1))
    session.commit()

    out = ls.weekly_report(session, 1, weeks=3)
    by_week = {w["week_start"]: w for w in out["weeks"]}

    this = by_week[this_mon.isoformat()]
    assert this["leads"] == 2 and this["share_total"] == 200    # 176 + 24
    assert this["estimated"] == 140.0                            # × 先验 0.7
    assert this["has_record"] is False                           # 还没录 → 明确标出来

    last = by_week[_monday(1).isoformat()]
    assert last["leads"] == 1 and last["share_total"] == 100


def test_weekly_report_reads_channel_breakdown(session) -> None:
    """分渠道周录要能读回来(用户口径:"我只能给你我的",且要分渠道)。"""
    wk = _monday(1)
    session.add(PanRecruitWeekly(user_id=1, week_start=datetime.combine(wk, datetime.min.time()),
                                 recruits=60,
                                 channels=json.dumps({"douyin": 42, "wechat": 18})))
    session.commit()
    out = ls.weekly_report(session, 1, weeks=2)
    row = next(w for w in out["weeks"] if w["week_start"] == wk.isoformat())
    assert row["has_record"] is True and row["recorded_total"] == 60
    assert row["recorded_channels"] == {"douyin": 42, "wechat": 18}
    assert out["recorded_weeks"] == 1


def test_weekly_report_distinguishes_missing_record_from_zero(session) -> None:
    """录了 **0** 与**没录**必须分开:前者 `has_record=True`。"""
    wk = _monday(1)
    session.add(PanRecruitWeekly(user_id=1, week_start=datetime.combine(wk, datetime.min.time()),
                                 recruits=0, channels=""))
    session.commit()
    row = next(w for w in ls.weekly_report(session, 1, weeks=2)["weeks"]
               if w["week_start"] == wk.isoformat())
    assert row["recorded_total"] == 0 and row["has_record"] is True


def test_coefficient_override_is_read(session) -> None:
    """先验系数可被 `system_config(lead_coefficients)` 覆盖(将来校准用)。"""
    from app.db.models import SystemConfig

    session.add(SystemConfig(key="lead_coefficients", value=json.dumps({"douyin": 0.5})))
    session.commit()
    assert ls.load_coefficients(session)["douyin"] == 0.5
    _lead(session, "a", 100, _monday(0))
    session.commit()
    assert ls.weekly_report(session, 1, weeks=1)["weeks"][0]["estimated"] == 50.0


# ---------------------------------------------------------------- 周录提醒(2026-10-03)

def _stub_tick(monkeypatch, session):
    import app.db as db_mod
    monkeypatch.setattr(db_mod, "get_session_local", lambda: (lambda: session))


def _S(**kw):
    from types import SimpleNamespace
    base = dict(recruit_reminder_enabled=True, feishu_webhook_admin="ADMIN",
                feishu_webhook="CUSTOMER", feishu_secret="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_missing_weeks_excludes_current_week(session) -> None:
    """**本周还没结束,不算"缺录"** —— 只数已经过完的周。"""
    out = ls._missing_weeks(session, 1, weeks=3)
    from datetime import date, timedelta

    this_monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    assert this_monday not in out
    assert len(out) == 3


def test_reminder_pushes_to_admin_when_week_missing(session, monkeypatch) -> None:
    """⚠️ 真值入口空着就提醒 —— 推**管理员群**(内部待办,不回落客户群)。"""
    from app.services import feishu_client as fc

    _stub_tick(monkeypatch, session)
    sent: list = []

    class _F:
        def __init__(self, webhook, secret="") -> None:
            sent.append(webhook)

        def send(self, text):
            sent.append(text)
            return True

    monkeypatch.setattr(fc, "FeishuClient", _F)
    assert ls.record_reminder_tick(_S()) == 1
    assert sent[0] == "ADMIN"
    assert "该录上周拉新" in sent[1] and "分渠道" in sent[1]


def test_reminder_is_silent_when_last_week_recorded(session, monkeypatch) -> None:
    """⚠️ **录了就不再提醒** —— 否则每周一条通知,很快变成噪音被无视

    (与看门狗告警"一天响 4 次"同一个教训)。
    """
    from datetime import date, timedelta

    _stub_tick(monkeypatch, session)
    this_monday = date.today() - timedelta(days=date.today().weekday())
    for i in range(1, 5):                       # 把最近 4 周都录上
        wk = this_monday - timedelta(weeks=i)
        session.add(PanRecruitWeekly(user_id=1, week_start=datetime.combine(wk, datetime.min.time()),
                                     recruits=10, channels=""))
    session.commit()
    called = {"n": 0}

    class _F:
        def __init__(self, *a, **k) -> None:
            called["n"] += 1

    from app.services import feishu_client as fc
    monkeypatch.setattr(fc, "FeishuClient", _F)
    assert ls.record_reminder_tick(_S()) == 0 and called["n"] == 0


def test_reminder_does_not_fall_back_to_customer_group(session, monkeypatch) -> None:
    """没配管理员群就**安静跳过** —— 这是内部待办,推进客户群是事故。"""
    _stub_tick(monkeypatch, session)
    assert ls.record_reminder_tick(_S(feishu_webhook_admin="")) == 0
