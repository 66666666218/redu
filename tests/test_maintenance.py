"""数据保留治理单测:清理超期快照、保留近期数据。"""
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
from app.db.maintenance import cleanup_old_data
from app.db.models import DouhotWatchSnap, DouhotWord, WeiboHotItem


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def _settings(**kw):
    from config.settings import Settings

    return Settings(_env_file=None, data_retention_days=30, **kw)


def test_cleanup_deletes_old_keeps_recent(session) -> None:
    old = datetime.now() - timedelta(days=40)
    recent = datetime.now()
    session.add_all([
        DouhotWord(user_id=1, title="旧词", score=1, created_at=old),
        DouhotWord(user_id=1, title="新词", score=9, created_at=recent),
        WeiboHotItem(user_id=1, title="旧微博", heat=1, rank=1, captured_at=old),
        WeiboHotItem(user_id=1, title="新微博", heat=9, rank=1, captured_at=recent),
        DouhotWatchSnap(user_id=1, list_type="word", keyword="旧", score=1, captured_at=old),
    ])
    session.commit()

    res = cleanup_old_data(_settings(), db=session)
    assert res["douhot_words"] == 1 and res["weibo_hot_items"] == 1 and res["douhot_watch_snap"] == 1
    assert res["retention_days"] == 30
    # 旧删除、新保留
    assert session.scalar(select(DouhotWord).where(DouhotWord.title == "新词")) is not None
    assert session.scalar(select(DouhotWord).where(DouhotWord.title == "旧词")) is None
    assert session.scalar(select(WeiboHotItem).where(WeiboHotItem.title == "新微博")) is not None


def test_cleanup_bounds_snap_date_string(session) -> None:
    """闲鱼 snap_date 是 YYYY-MM-DD 字符串,需按字符串日期比较。"""
    from app.db.models import XianyuDaily

    old = (datetime.now() - timedelta(days=40)).date().isoformat()
    recent = datetime.now().date().isoformat()
    session.add_all([
        XianyuDaily(user_id=1, item_id="a", title="旧", snap_date=old, want_count=1),
        XianyuDaily(user_id=1, item_id="b", title="新", snap_date=recent, want_count=9),
    ])
    session.commit()
    cleanup_old_data(_settings(), db=session)
    assert session.scalar(select(XianyuDaily).where(XianyuDaily.title == "新")) is not None
    assert session.scalar(select(XianyuDaily).where(XianyuDaily.title == "旧")) is None


def test_scheduler_registers_data_cleanup_job() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.services.scheduler import build_jobs

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)
    ids = [j.id for j in sched.get_jobs()]
    assert "data_cleanup" in ids
    sched.shutdown(wait=False) if sched.running else None
