"""推送时段表单测(2026-10-01):时刻/星期判定、非法值丢弃、清空即停用、失败隔离。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.services import push_timeline as pt


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def test_defaults_cover_all_kinds_and_match_old_crons(session) -> None:
    """默认值必须与原先写死在 settings 里的那几条 cron 等价,否则迁移当天就悄悄改了点。"""
    cfg = pt.load(session)
    assert set(cfg["kinds"]) == set(pt.PUSH_KINDS)
    assert cfg["kinds"]["daily"]["times"] == ["08:00"]            # 原 "0 8 * * *"
    assert cfg["kinds"]["hotrank"]["times"] == ["09:30", "21:30"]  # 原 "30 9,21 * * *"
    assert cfg["kinds"]["agent"]["times"] == ["09:10", "15:10", "21:10"]  # 原 "10 9,15,21 * * *"
    assert cfg["kinds"]["insight"]["days"] == [1]                 # 原 "0 9 * * 1"(周一)
    assert cfg["kinds"]["weekly"]["days"] == [0]                  # 原 "0 20 * * 0"(周日)


def test_due_kinds_matches_time_and_weekday(session) -> None:
    cfg = pt.load(session)
    assert pt.due_kinds(datetime(2026, 10, 5, 8, 0), cfg) == ["daily"]     # 周一 08:00
    assert pt.due_kinds(datetime(2026, 10, 5, 8, 1), cfg) == []            # 差一分钟不触发
    assert pt.due_kinds(datetime(2026, 10, 4, 9, 0), cfg) == []            # 周日不跑周一的爆点回顾
    assert pt.due_kinds(datetime(2026, 10, 5, 9, 0), cfg) == ["insight"]   # 周一 09:00
    assert pt.due_kinds(datetime(2026, 10, 4, 20, 0), cfg) == ["weekly"]   # 周日 20:00


def test_save_drops_invalid_times_and_days(session) -> None:
    """坏值只丢它自己,不把整份配置报废(界面手滑输个 25:00 不该让所有推送停摆)。"""
    cfg = pt.save(session, {"kinds": {"daily": {"times": ["08:00", "25:00", "8:0"], "days": [1, 9]}}})
    assert cfg["kinds"]["daily"]["times"] == ["08:00"]
    assert cfg["kinds"]["daily"]["days"] == [1]
    assert pt.load(session)["kinds"]["daily"]["times"] == ["08:00"]   # 确实落库
    assert pt.load(session)["kinds"]["hotrank"]["times"] == ["09:30", "21:30"]  # 没被牵连


def test_empty_times_disables_kind(session) -> None:
    """清空时刻 = 关掉它:界面不该显示成"开着"却永不触发。"""
    cfg = pt.save(session, {"kinds": {"daily": {"times": []}}})
    assert cfg["kinds"]["daily"]["enabled"] is False
    assert pt.due_kinds(datetime(2026, 10, 5, 8, 0), cfg) == []


def test_weekend_silence(session) -> None:
    """去掉周六周日 = 周末静默(最实际的诉求:工作日报别在周末响)。"""
    cfg = pt.save(session, {"kinds": {"daily": {"times": ["08:00"], "days": [1, 2, 3, 4, 5]}}})
    assert pt.due_kinds(datetime(2026, 10, 5, 8, 0), cfg) == ["daily"]   # 周一
    assert pt.due_kinds(datetime(2026, 10, 3, 8, 0), cfg) == []          # 周六
    assert pt.due_kinds(datetime(2026, 10, 4, 8, 0), cfg) == []          # 周日


def test_tick_runs_only_due_kinds(session, monkeypatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(pt, "_run", lambda kind, settings: ran.append(kind) or 1)
    out = pt.tick(db=session, when=datetime(2026, 10, 5, 8, 0))
    assert out["ran"] == ["daily"] and ran == ["daily"] and out["failed"] == []


def test_tick_swallows_single_failure(session, monkeypatch) -> None:
    """单类推送炸了不能拖累同一分钟的其他类(它们常常撞在同一个整点)。"""
    def boom(kind, settings):
        if kind == "daily":
            raise RuntimeError("推送炸了")
        return 1

    monkeypatch.setattr(pt, "_run", boom)
    # 让两类撞在同一分钟(monkeypatch 会自动还原,不留副作用)
    monkeypatch.setitem(pt.PUSH_KINDS["hotrank"], "times", ["08:00"])
    out = pt.tick(db=session, when=datetime(2026, 10, 5, 8, 0))
    assert out["failed"] == ["daily"] and out["ran"] == ["hotrank"]


# ---- 补跑:调度器偶尔把一次 tick 拖到下一分钟,被跳过的时刻不能永久漏掉 ----


def test_minutes_since_covers_skipped_minutes() -> None:
    last = datetime(2026, 10, 5, 8, 0)
    now = datetime(2026, 10, 5, 8, 3, 30)
    got = [m.strftime("%H:%M") for m in pt.minutes_since(last, now)]
    assert got == ["08:01", "08:02", "08:03"]


def test_minutes_since_caps_backfill_window() -> None:
    """服务停了一夜后重启,不该把攒了一晚上的推送全倒出来。"""
    last = datetime(2026, 10, 4, 8, 0)
    now = datetime(2026, 10, 5, 8, 0)
    got = pt.minutes_since(last, now, cap=10)
    assert len(got) == 10
    assert got[-1] == now.replace(second=0, microsecond=0)


def test_minutes_since_empty_within_same_minute() -> None:
    """同一分钟内被触发第二次(调度抖动)不该重复检查——否则同一条会推两遍。"""
    assert pt.minutes_since(datetime(2026, 10, 5, 8, 0, 30),
                            datetime(2026, 10, 5, 8, 0, 45)) == []


def test_tick_backfills_skipped_minute(session, monkeypatch) -> None:
    """核心回归:08:00 那一刻被调度器跳过,08:01 的 tick 必须把它补上。"""
    ran: list[str] = []
    monkeypatch.setattr(pt, "_run", lambda kind, settings: ran.append(kind) or 1)
    assert pt.tick(db=session, when=datetime(2026, 10, 5, 7, 59))["ran"] == []  # 先建好水位
    out = pt.tick(db=session, when=datetime(2026, 10, 5, 8, 1))                 # 跳过 08:00
    assert out["ran"] == ["daily"] and ran == ["daily"]


def test_tick_does_not_repeat_within_same_minute(session, monkeypatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(pt, "_run", lambda kind, settings: ran.append(kind) or 1)
    assert pt.tick(db=session, when=datetime(2026, 10, 5, 8, 0, 10))["ran"] == ["daily"]
    assert pt.tick(db=session, when=datetime(2026, 10, 5, 8, 0, 50))["ran"] == []
    assert ran == ["daily"]        # 只推了一次


def test_tick_first_run_still_checks_current_minute(session, monkeypatch) -> None:
    """首次部署(没有水位记录)时,当前这一分钟必须照常检查,不能空转。"""
    ran: list[str] = []
    monkeypatch.setattr(pt, "_run", lambda kind, settings: ran.append(kind) or 1)
    assert pt.tick(db=session, when=datetime(2026, 10, 5, 8, 0))["ran"] == ["daily"]
