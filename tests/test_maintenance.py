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


def test_cleanup_wechat_articles_covers_null_pan_urls(session) -> None:
    """公众号文章分级清理的漏网:`pan_urls` 为 NULL 的历史行(建表早期/别处写入)
    在 SQL 里既不 `= ""` 也不 `!= ""`,两张网都漏掉 → wechat_articles 无限增长。"""
    from app.db.models import WechatArticle

    old = datetime.now() - timedelta(days=70)
    session.add_all([
        WechatArticle(user_id=1, title="NULL 链列旧文", url="https://mp.weixin.qq.com/s/n",
                      source="listen", pan_urls=None, created_at=old),
        WechatArticle(user_id=1, title="空串旧文", url="https://mp.weixin.qq.com/s/e",
                      source="listen", pan_urls="", created_at=old),
        WechatArticle(user_id=1, title="带链但未满 180 天", url="https://mp.weixin.qq.com/s/k",
                      source="listen", pan_urls="https://pan.quark.cn/s/abc", created_at=old),
    ])
    session.commit()

    cleanup_old_data(_settings(), db=session)
    assert session.scalar(select(WechatArticle).where(WechatArticle.title == "NULL 链列旧文")) is None
    assert session.scalar(select(WechatArticle).where(WechatArticle.title == "空串旧文")) is None
    assert session.scalar(select(WechatArticle).where(WechatArticle.title == "带链但未满 180 天")) is not None


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


def test_cleanup_deletes_old_window_snap(session) -> None:
    """多窗口快照 douhot_window_snap 纳入清理(此前漏加会无限膨胀)。"""
    from app.db.models import DouhotWindowSnap

    old = datetime.now() - timedelta(days=40)
    recent = datetime.now()
    session.add_all([
        DouhotWindowSnap(user_id=1, keyword="旧窗", window=24, captured_at=old),
        DouhotWindowSnap(user_id=1, keyword="新窗", window=24, captured_at=recent),
    ])
    session.commit()
    res = cleanup_old_data(_settings(), db=session)
    assert res["douhot_window_snap"] == 1
    assert session.scalar(select(DouhotWindowSnap).where(DouhotWindowSnap.keyword == "新窗")) is not None


def test_cleanup_reclaims_orphan_pan_links(session) -> None:
    """删掉旧文章后,指向已不存在文章的盘链孤儿行被回收;存活文章的链原样保留。"""
    from app.db.models import WechatArticle, WechatPanLink

    old_no_pan = WechatArticle(user_id=1, title="超60天无盘链旧文", pan_urls="",
                               created_at=datetime.now() - timedelta(days=90))
    live = WechatArticle(user_id=1, title="在架文章", pan_urls="https://pan/x",
                         created_at=datetime.now())
    session.add_all([old_no_pan, live])
    session.commit()
    session.add_all([
        WechatPanLink(user_id=1, article_id=old_no_pan.id, pan_url="https://pan/dead"),
        WechatPanLink(user_id=1, article_id=live.id, pan_url="https://pan/live"),
    ])
    session.commit()

    res = cleanup_old_data(_settings(), db=session)
    assert res.get("wechat_articles_tiered", 0) >= 1  # 旧文已删
    assert session.scalar(select(WechatArticle).where(WechatArticle.title == "在架文章")) is not None
    # 孤儿链(指向已删文章)被回收,存活链保留
    assert session.scalar(select(WechatPanLink).where(WechatPanLink.pan_url == "https://pan/live")) is not None
    assert session.scalar(select(WechatPanLink).where(WechatPanLink.pan_url == "https://pan/dead")) is None
    assert res.get("wechat_pan_links_orphan", 0) == 1


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


# ---------------------------------------------------------------- 2026-10 新增表的保留策略

def _aged(days: int) -> datetime:
    return datetime.now() - timedelta(days=days)


def test_cleanup_prunes_new_tables_at_180_days_not_30(session) -> None:
    """新表按**性质分档**,不是一律 30 天:

    历史类(建议/线索/发现链/分享快照)保留 180 天 —— 它们是**结算与复盘**的底料,
    对账要跨月看,用默认 30 天会把刚攒起来的对照数据清掉。
    """
    from app.db.models import DiscoveredPanLink, DouyinLead, HotspotSuggestion

    session.add_all([
        HotspotSuggestion(user_id=1, keyword="旧", created_at=_aged(200)),
        HotspotSuggestion(user_id=1, keyword="半年内", created_at=_aged(100)),
        DouyinLead(user_id=1, aweme_id="a", found_at=_aged(200)),
        DouyinLead(user_id=1, aweme_id="b", found_at=_aged(10)),
        DiscoveredPanLink(user_id=1, origin_url="u", found_at=_aged(200)),
    ])
    session.commit()
    cleanup_old_data(_settings(), db=session)
    assert session.query(HotspotSuggestion).count() == 1        # 30 天的不该被删,180 天的删
    assert [r.keyword for r in session.query(HotspotSuggestion).all()] == ["半年内"]
    assert session.query(DouyinLead).count() == 1
    assert session.query(DiscoveredPanLink).count() == 0


def test_cleanup_keeps_assets(session) -> None:
    """⚠️ **资产不按时间删** —— `xunlei_resources` 是我方资源清单、`cross_platform_accounts`
    是收录的对标号;按时间删 = 系统"忘了自己有什么",那是自毁不是清理。"""
    from app.db.models import CrossPlatformAccount, XunleiResource

    session.add_all([
        XunleiResource(user_id=1, fid="f1", name="老资源", synced_at=_aged(400)),
        CrossPlatformAccount(user_id=1, platform="bilibili", uid="u1", name="网盘号",
                             status="active", discovered_at=_aged(400)),
    ])
    session.commit()
    cleanup_old_data(_settings(), db=session)
    assert session.query(XunleiResource).count() == 1
    assert session.query(CrossPlatformAccount).count() == 1


def test_cleanup_drops_dismissed_accounts(session) -> None:
    """明确被否掉的对标号(`status='dismissed'`)才清。"""
    from app.db.models import CrossPlatformAccount

    session.add_all([
        CrossPlatformAccount(user_id=1, platform="bilibili", uid="u1", name="留",
                             status="active", discovered_at=_aged(1)),
        CrossPlatformAccount(user_id=1, platform="bilibili", uid="u2", name="否",
                             status="dismissed", discovered_at=_aged(1)),
    ])
    session.commit()
    cleanup_old_data(_settings(), db=session)
    assert [r.name for r in session.query(CrossPlatformAccount).all()] == ["留"]


def test_cleanup_group_shares_only_drops_untransferred_stale(session) -> None:
    """群分享:**只清"从没转存成功过"且过期很久的**。

    转存成功的行要留 —— 它们是我方资源的来源凭证(`our_url`/`fid` 在别处被引用)。
    """
    from app.db.models import XunleiGroupShare

    session.add_all([
        XunleiGroupShare(user_id=1, group_id="g", share_id="s1", title="旧且没转", msg_time=_aged(90)),
        XunleiGroupShare(user_id=1, group_id="g", share_id="s2", title="旧但转过了",
                         our_url="https://pan.xunlei.com/s/X", msg_time=_aged(90)),
        XunleiGroupShare(user_id=1, group_id="g", share_id="s3", title="新的没转", msg_time=_aged(3)),
    ])
    session.commit()
    cleanup_old_data(_settings(), db=session)
    assert sorted(r.share_id for r in session.query(XunleiGroupShare).all()) == ["s2", "s3"]


def test_data_cleanup_is_not_at_0400_anymore() -> None:
    """⚠️ **04:00 是全天最挤的一处** —— 那里 `wechat_collect_tick`(网络长任务)也在跑,
    而清理要跨 ~22 张表 DELETE + 23MB 快照拷贝,两者都在压 SQLite 写锁。已错开到 03:10。"""
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.services.scheduler import build_jobs

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)
    job = next(j for j in sched.get_jobs() if j.id == "data_cleanup")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "3" and fields["minute"] == "10"
    sched.shutdown(wait=False) if sched.running else None
