"""数据保留治理:清理超过保留期的快照/运行/告警/日志(控制库体积)。

背景:每次采集都会往快照表追加(内容词 top100 每轮 ×100 条、微博 ~50 条、闲鱼 ~100 条、
关键词快照每词每轮 1 条),另外运行/告警/日志表只增不删。长期不清理会持续膨胀。

**保留期语义**:删除 `created_at/captured_at/...` 早于 `now - DATA_RETENTION_DAYS` 的记录。
保留期内数据足够支撑 判涨(取最近 200/500 条)与智能体预测(近 30 天序列)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select, func
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db import get_session_local
from app.db.models import (
    AdminLog,
    AlertRecord,
    DouhotWatchSnap,
    DouhotWord,
    LoginLog,
    RunRecord,
    WeiboHotItem,
    WeiboTrend,
    XianyuDaily,
    XianyuItem,
)

logger = logging.getLogger(__name__)

# (模型, 时间列, 是否字符串日期) —— 字段名需在模型里存在
_TABLES = [
    (DouhotWord, "created_at", False),
    (WeiboHotItem, "captured_at", False),
    (DouhotWatchSnap, "captured_at", False),
    (WeiboTrend, "decided_at", False),
    (XianyuItem, "created_at", False),
    (XianyuDaily, "snap_date", True),      # YYYY-MM-DD 字符串
    (RunRecord, "started_at", False),
    (AlertRecord, "triggered_at", False),
    (LoginLog, "created_at", False),
    (AdminLog, "created_at", False),
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
        db.commit()
        total = sum(result.values())
        if total:
            logger.info("数据清理:保留 %s 天,删除 %s 条(%s)", days, total, result)
        result["retention_days"] = days
        return result
    finally:
        if own_session:
            db.close()
