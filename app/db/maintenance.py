"""数据保留治理:清理超过保留期的快照/运行/告警/日志(控制库体积)。

背景:每次采集都会往快照表追加(内容词 top100 每轮 ×100 条、微博 ~50 条、闲鱼 ~100 条、
关键词快照每词每轮 1 条),另外运行/告警/日志表只增不删。长期不清理会持续膨胀。

**保留期语义**:删除 `created_at/captured_at/...` 早于 `now - DATA_RETENTION_DAYS` 的记录。
保留期内数据足够支撑 判涨(取最近 200/500 条)与智能体预测(近 30 天序列)。
"""
from __future__ import annotations

import logging
import os
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
        # SQLite 快照备份(每日一次,保留最近 7 份;MySQL 部署由 backup.sh 负责)
        try:
            import shutil
            from config.settings import get_settings as _gs
            url = _gs().database_url
            if url.startswith("sqlite"):
                db_path = url.split("sqlite:///")[-1]
                if os.path.exists(db_path):
                    import glob as _glob
                    bak_dir = os.path.join(os.path.dirname(db_path) or ".", "backups")
                    os.makedirs(bak_dir, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d")
                    snap = os.path.join(bak_dir, f"platform_{stamp}.db")
                    src = _gs().database_url.split("sqlite:///")[-1]
                    src_engine = __import__("sqlalchemy").create_engine(url)
                    src_conn = src_engine.raw_connection()
                    dst = __import__("sqlalchemy").create_engine(f"sqlite:///{snap}")
                    src_conn.backup(dst.raw_connection())
                    dst.dispose()
                    src_conn.close()
                    old_baks = sorted(_glob.glob(os.path.join(bak_dir, "platform_*.db")))[:-7]
                    for ob in old_baks:
                        os.remove(ob)
                    logger.info("SQLite 快照备份完成:%s", snap)
        except Exception:  # noqa: BLE001 - 备份失败不阻塞清理
            logger.exception("SQLite 快照备份失败")
        result["retention_days"] = days
        return result
    finally:
        if own_session:
            db.close()
