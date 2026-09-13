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


# ---------------------------------------------------------------- SQLite 快照备份
def _make_db(tmp_path, rows: int = 200) -> str:
    """造一个有数据的 SQLite 库,返回路径。"""
    import sqlite3

    path = tmp_path / "platform.db"
    con = sqlite3.connect(str(path))
    con.execute("create table t(x)")
    con.executemany("insert into t values(?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()
    return str(path)


def test_snapshot_sqlite_produces_valid_nonempty_copy(tmp_path) -> None:
    """快照必须是可用的非空副本。

    回归:旧实现用 `engine.raw_connection()`(返回 _ConnectionFairy 代理)当
    `Connection.backup()` 的 target,C 层类型检查直接 TypeError → 备份从未成功,
    只留下 0 字节的假文件,还被 except 静默吞掉。
    """
    import sqlite3

    from app.db.maintenance import snapshot_sqlite

    src = _make_db(tmp_path, rows=200)
    info = snapshot_sqlite(f"sqlite:///{src}")

    assert info["bytes"] > 0, "快照不能是 0 字节"
    assert os.path.getsize(info["path"]) == info["bytes"]
    con = sqlite3.connect(f"file:{info['path']}?mode=ro", uri=True)
    try:
        assert con.execute("select count(*) from t").fetchone()[0] == 200
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_snapshot_sqlite_rotates_keeping_latest(tmp_path) -> None:
    """轮转:超过 keep 份时只留最近 N 份(按日期文件名排序)。"""
    import glob

    from app.db.maintenance import snapshot_sqlite

    src = _make_db(tmp_path)
    base = datetime(2026, 9, 13, 4, 0)
    for i in range(6):
        snapshot_sqlite(f"sqlite:///{src}", keep=3, now=base - timedelta(days=5 - i))

    kept = sorted(glob.glob(str(tmp_path / "backups" / "platform_*.db")))
    assert [os.path.basename(p) for p in kept] == [
        "platform_20260911.db", "platform_20260912.db", "platform_20260913.db",
    ]
    assert all(os.path.getsize(p) > 0 for p in kept)


def test_snapshot_sqlite_discards_and_removes_invalid_file(tmp_path) -> None:
    """0 字节/损坏的快照必须被丢弃并删除——留着假备份比没有更危险。"""
    from app.db.maintenance import _verify_sqlite_snapshot

    empty = tmp_path / "platform_20260913.db"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        _verify_sqlite_snapshot(str(empty))
    assert not empty.exists(), "不合格的快照应被删除,不能留下残骸"

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite file")
    with pytest.raises(Exception):
        _verify_sqlite_snapshot(str(garbage))
    assert not garbage.exists()


def test_cleanup_skips_backup_for_non_sqlite(tmp_path, session) -> None:
    """MySQL 部署(backup.sh 负责)不应触发 SQLite 快照——也保证注入 settings 的
    测试不会去动真实库。"""
    from app.db.maintenance import cleanup_old_data

    res = cleanup_old_data(_settings(), db=session)  # 默认 database_url 是 mysql
    assert "sqlite_backup" not in res
    assert not (tmp_path / "backups").exists()


def test_cleanup_records_backup_result(tmp_path, session) -> None:
    """SQLite 部署下 cleanup_old_data 要真的产出备份,并把结果写回返回值。"""
    from app.db.maintenance import cleanup_old_data

    src = _make_db(tmp_path)
    res = cleanup_old_data(_settings(database_url=f"sqlite:///{src}"), db=session)
    assert res.get("sqlite_backup", "").endswith(".db")
    assert os.path.getsize(res["sqlite_backup"]) > 0


def test_cleanup_records_backup_failure_instead_of_silent(tmp_path, session) -> None:
    """备份失败必须留痕:此前是 except 静默吞掉,导致 0 字节假备份骗了很久。"""
    from app.db.maintenance import cleanup_old_data

    res = cleanup_old_data(_settings(database_url=f"sqlite:///{tmp_path}/nope.db"), db=session)
    assert "sqlite_backup" not in res
    assert "FileNotFoundError" in res.get("sqlite_backup_error", "")


def test_cleanup_records_snapshot_error_for_corrupt_source(tmp_path, session) -> None:
    """源不是合法 SQLite 库时,错误也要落到返回值里,而不是只写日志。"""
    from app.db.maintenance import cleanup_old_data

    bogus = tmp_path / "bogus.db"
    bogus.write_bytes(b"not a database at all")
    res = cleanup_old_data(_settings(database_url=f"sqlite:///{bogus}"), db=session)
    assert res.get("sqlite_backup_error"), "损坏库应记下备份失败原因"
    bak = tmp_path / "backups"
    leftovers = list(bak.glob("*.db")) if bak.exists() else []
    assert leftovers == [], f"失败不应留下半成品:{leftovers}"
