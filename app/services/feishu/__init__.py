"""飞书域包(2026-09-30 自 feishu.py 拆分:卡片排版 _cards / 推送作业 _jobs)。

本 __init__ 即兼容层:历史调用方(`from app.services.feishu import X`)与测试引用的名字全部保持可用。
"""
# ruff: noqa: F401
# ↑ 与 wechat_monitor.py 同理:本文件全部 import 都是有意为之的 re-export。
# ---- 原 feishu.py 的完整 import 区(兼容层:外部名与 monkeypatch 目标保持可见) ----
from __future__ import annotations
import time
from datetime import datetime, timedelta
from typing import Any
import unicodedata
from sqlalchemy import select
from sqlalchemy.orm import Session
from config.settings import Settings, get_settings
from app.db import repository
from app.db.models import BaiduHotItem, DouhotWatchSnap, DouhotWord, FeishuAlert, WeiboHotItem, XianyuItem
from app.services import douhot
from app.services.feishu_client import FeishuClient, platform_webhook, webhook_for, webhooks_for
from app.utils import get_logger

from app.services.feishu._cards import (  # noqa: F401
    SECTIONS,
    SECTION_LABELS,
    _RANK_FIELD,
    _SERIES,
    _SHORT,
    _TABLES,
    _TS_COL,
    _agent_confidence_rank,
    _aligned_row,
    _batches,
    _col_set_row,
    _cross_section_lines,
    _daily_cross_lines,
    _delta,
    _display_width,
    _dw,
    _entry_line,
    _keyword_entries_lines,
    _keyword_entries_rows,
    _keyword_watch_lines,
    _md_safe,
    _overview,
    _pad,
    _pad_cell,
    _riser_tally,
    _rjust,
    _section_lines,
    _split_messages,
    _trend_summary,
    _w,
    _wechat_ops_lines,
    build_daily,
    build_keyword_card,
    mask_own,
)
from app.services.feishu._jobs import (  # noqa: F401
    _in_cooldown,
    _mark_alerted,
    _section_weekly_tally,
    run_feishu_daily,
    run_feishu_insight_digest,
    run_feishu_keyword_alerts,
    run_feishu_keyword_realtime,
    run_feishu_realtime,
    run_feishu_wechat_analysis,
)
