"""Repository 数据访问层单测:验证各聚合查询返回正确结构。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db import repository as repo
from app.db.models import DouhotWatch, DouhotWatchSnap, User, WeiboHotItem, XianyuDaily


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_weibo_heat_series() -> None:
    db = _session()
    db.add_all([
        WeiboHotItem(user_id=1, title="A", heat=100, rank=1, captured_at=datetime(2026, 9, 1, 8)),
        WeiboHotItem(user_id=1, title="A", heat=200, rank=1, captured_at=datetime(2026, 9, 2, 8)),
        WeiboHotItem(user_id=1, title="B", heat=50, rank=2, captured_at=datetime(2026, 9, 1, 8)),
    ])
    db.commit()
    s = repo.weibo_heat_series(db, 1)
    assert set(s) == {"A", "B"}
    assert [v for _, v in s["A"]] == [100, 200]  # 升序
    db.close()


def test_watch_snap_series_and_get_watch() -> None:
    db = _session()
    db.add(DouhotWatch(user_id=1, list_type="word", keyword="词"))
    db.add_all([
        DouhotWatchSnap(user_id=1, list_type="word", keyword="词", score=10, rank_now=1),
        DouhotWatchSnap(user_id=1, list_type="word", keyword="词", score=20, rank_now=1),
    ])
    db.commit()
    assert len(repo.watch_snap_series(db, 1, "词")) == 2
    assert repo.get_watch(db, 1, "word", "词") is not None
    assert repo.get_watch(db, 1, "word", "不存在") is None
    db.close()


def test_xianyu_want_series_and_by_date() -> None:
    db = _session()
    db.add_all([
        XianyuDaily(user_id=1, item_id="a", title="教程", snap_date="2026-09-01", want_count=5),
        XianyuDaily(user_id=1, item_id="a", title="教程", snap_date="2026-09-02", want_count=9),
    ])
    db.commit()
    s = repo.xianyu_want_series(db, 1)
    assert [v for _, v in s["教程"]] == [5, 9]
    assert len(repo.xianyu_daily_by_date(db, 1, "2026-09-02")) == 1
    db.close()


def test_list_enabled_users_filters() -> None:
    db = _session()
    db.add_all([
        User(username="on", password_hash="x", enabled=True),
        User(username="off", password_hash="x", enabled=False),
    ])
    db.commit()
    assert {u.username for u in repo.list_enabled_users(db)} == {"on"}
    db.close()
