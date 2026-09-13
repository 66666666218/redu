"""数据保留治理:清理超过保留期的快照/运行/告警/日志(控制库体积)。

背景:每次采集都会往快照表追加(内容词 top100 每轮 ×100 条、微博 ~50 条、闲鱼 ~100 条、
关键词快照每词每轮 1 条),另外运行/告警/日志表只增不删。长期不清理会持续膨胀。

**保留期语义**:删除 `created_at/captured_at/...` 早于 `now - DATA_RETENTION_DAYS` 的记录。
保留期内数据足够支撑 判涨(取最近 200/500 条)与智能体预测(近 30 天序列)。
"""
from __future__ import annotations

import glob
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db import get_session_local
from app.db.models import (
    AdminLog,
    AlertRecord,
    BaiduHotItem,
    DouhotWatchSnap,
    DouhotWord,
    LoginLog,
    RunRecord,
    WeiboHotItem,
    WeiboTrend,
    FeishuAlert,
    WechatArticle,
    WechatCandidate,
    WechatTrafficSample,
    AgentStage,
    XianyuDaily,
    XianyuItem,
)

logger = logging.getLogger(__name__)

# (模型, 时间列, 是否字符串日期) —— 字段名需在模型里存在
_TABLES = [
    (DouhotWord, "created_at", False),
    (WeiboHotItem, "captured_at", False),
    (BaiduHotItem, "captured_at", False),
    (DouhotWatchSnap, "captured_at", False),
    (WeiboTrend, "decided_at", False),
    (XianyuItem, "created_at", False),
    (XianyuDaily, "snap_date", True),      # YYYY-MM-DD 字符串
    (RunRecord, "started_at", False),
    (AlertRecord, "triggered_at", False),
    (LoginLog, "created_at", False),
    (AdminLog, "created_at", False),
    (WechatTrafficSample, "sampled_at", False),
    (FeishuAlert, "alerted_at", False),      # 冷却记录本体 6~24h 有效,清理只是防无限增长
    (AgentStage, "updated_at", False),       # Agent 阶段记忆(陈旧状态自然失效)
    (WechatCandidate, "discovered_at", False),  # 候选号(dismissed 的也清理,防无限增长)
]


def _verify_sqlite_snapshot(path: str) -> None:
    """校验快照可用:非空 + `quick_check` 通过。不合格则删掉残留文件再抛错。

    留着 0 字节/损坏的"备份"比没有备份更危险——出事后才发现恢复不了。
    """
    try:
        if os.path.getsize(path) == 0:
            raise ValueError("快照为 0 字节")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            verdict = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
        if not verdict or verdict[0] != "ok":
            raise ValueError(f"快照完整性校验未通过:{verdict}")
    except Exception:
        _remove_quietly(path)
        raise


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def snapshot_sqlite(source_url: str, keep: int = 7, now: datetime | None = None) -> dict:
    """对 SQLite 库做**在线一致性快照**,保留最近 `keep` 份。返回 `{"path","bytes"}`。

    两个必须守住的点(此前的实现两条都踩了,导致备份从未成功过):

    1. **必须传真正的 `sqlite3.Connection`**。`engine.raw_connection()` 返回的是
       SQLAlchemy 的 `_ConnectionFairy` **代理**,而 `Connection.backup()` 是 C 实现,
       会对 target 做类型检查,传代理直接抛
       `TypeError: backup() argument 'target' must be sqlite3.Connection, not _ConnectionFairy`。
    2. **必须走备份 API,不能直接拷文件**。直拷在库正被写入时会拷到撕裂的中间状态;
       备份 API 由 SQLite 自己保证一致性,且不需要停写。

    任何一步失败都不留残骸——半成品文件比没有备份更误导人。
    """
    db_path = source_url.split("sqlite:///")[-1]
    if not db_path or not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite 库不存在:{db_path}")

    bak_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(bak_dir, exist_ok=True)
    snap = os.path.join(bak_dir, f"platform_{(now or datetime.now()):%Y%m%d}.db")

    try:
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(snap)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
        _verify_sqlite_snapshot(snap)
    except Exception:
        # 连接/备份/校验任一环节失败,都可能已经在磁盘上建出了 0 字节的半成品
        _remove_quietly(snap)
        logger.exception("SQLite 快照失败,已丢弃半成品:%s", snap)
        raise

    # 轮转:文件名按日期排序,保留最近 keep 份
    for old in sorted(glob.glob(os.path.join(bak_dir, "platform_*.db")))[:-keep]:
        os.remove(old)
    return {"path": snap, "bytes": os.path.getsize(snap)}


def cleanup_old_data(settings: Settings | None = None, db: Session | None = None) -> dict:
    """删除超过 `DATA_RETENTION_DAYS` 的旧数据,返回各表删除条数。`db` 供测试注入。"""
    settings = settings or get_settings()
    days = max(getattr(settings, "data_retention_days", 30), 1)
    cutoff = datetime.now() - timedelta(days=days)
    own_session = db is None
    db = db or get_session_local()()
    result: dict[str, int] = {}
    try:
        for model, col, is_date in _TABLES:
            col_attr = getattr(model, col)
            threshold: object = cutoff.date().isoformat() if is_date else cutoff
            result[model.__tablename__] = db.execute(delete(model).where(col_attr < threshold)).rowcount
        # 公众号文章分级清理:无盘链且超 60 天的删(选题时效已过),带盘链的保留 180 天(改写素材库)
        try:
            cutoff60 = datetime.now() - timedelta(days=60)
            cutoff180 = datetime.now() - timedelta(days=180)
            n60 = db.execute(delete(WechatArticle).where(
                WechatArticle.pan_urls == "", WechatArticle.created_at < cutoff60)).rowcount
            n180 = db.execute(delete(WechatArticle).where(
                WechatArticle.pan_urls != "", WechatArticle.created_at < cutoff180)).rowcount
            if n60 or n180:
                result["wechat_articles_tiered"] = n60 + n180
        except Exception:  # noqa: BLE001 - 分级清理失败不阻塞
            logger.exception("公众号文章分级清理失败")

        db.commit()
        total = sum(result.values())
        if total:
            logger.info("数据清理:保留 %s 天,删除 %s 条(%s)", days, total, result)
        # SQLite 快照备份(每日一次,保留最近 7 份;MySQL 部署由 backup.sh 负责)。
        # 用已解析的 settings(而非再调 get_settings):注入 settings 的测试不该去动生产库。
        # 备份结果写回 result:备份此前是**静默失败**的(except 吞掉 + 留下 0 字节假文件),
        # 调用方/日志至少能从这里看出"今天到底有没有备份成功"。
        try:
            if settings.database_url.startswith("sqlite"):
                info = snapshot_sqlite(settings.database_url)
                logger.info("SQLite 快照备份完成:%s(%.1f KB)", info["path"], info["bytes"] / 1024)
                result["sqlite_backup"] = info["path"]
        except Exception as exc:  # noqa: BLE001 - 备份失败不阻塞清理
            logger.exception("SQLite 快照备份失败")
            result["sqlite_backup_error"] = f"{type(exc).__name__}: {exc}"
        result["retention_days"] = days
        return result
    finally:
        if own_session:
            db.close()
