"""公众号监听/同步门面(2026-09-29 架构拆分:实现在 app/services/wechat/ 包)。

本文件只做**兼容 re-export**:历史调用方(`from app.services.wechat_monitor import X`)
与测试引用的私有名、以及 monkeypatch 目标(requests/WereadError 等外部名)全部保持可用;
新代码请经本门面引用(直接 import 子模块会触发循环导入:子模块头部依赖门面)。
"""
# ---- 原 wechat_monitor.py 的完整 import 区(门面兼容:monkeypatch 目标与测试可见名) ----
from __future__ import annotations
"""公众号监听公共工具:盘链识别/正文抓取/元信息解析/质量评估/响应解析。

从 wechat_monitor.py(1300+ 行)按域拆分而来;域间共享的工具函数集中于此。
"""

import html as html_mod
import json
import re
import time
import uuid
import zlib
from datetime import datetime, timedelta

import requests
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import (FeishuAlert, User, WechatArticle, WechatBenchmark, WechatCandidate,
                           WechatPanLink, WechatRewrite, WechatTrafficSample)
from app.db.tx import HeldSavepoint, savepoint
from app.services.quark_transfer import QuarkAuthError, QuarkError, QuarkTransfer, extract_quark_urls
from app.services.reader_platform_client import PlatformError, ReaderPlatformClient
from app.services.werss_client import WerssClient
from app.services.sogou_weixin import search_articles as sogou_search_articles
from app.services.tenant_base import _base, _record_run
from app.services.content_extract import extract_account_refs as _ear
from app.services.early_agent import _md_safe_light
from app.services.weread_client import WereadAuthError, WereadClient, WereadError, build_mp_url
from app.services.feishu_client import is_quiet_hours
from app.utils import get_logger

from app.services.wechat._text import (  # noqa: F401
    PAN_PATTERNS,
    TITLE_HINTS,
    _BAIT_PATTERNS,
    _MY_LINK_RE,
    _QUALITY_MULTI,
    _QUALITY_PAN,
    _QUALITY_RECENT,
    _UA,
    _article_text_with_links,
    _extract_pan_urls,
    _parse_time,
    _public_get,
    assess_quality,
    detect_pan_types,
    extract_article_meta,
    fetch_article_content,
    title_hits,
)
from app.services.wechat._source import (  # noqa: F401
    _FEED_BIZ_PREFIX,
    _RENEWAL_COOLDOWN_KEY,
    _RENEWAL_COOLDOWN_MIN,
    _RENEWAL_FAIL_TEXT,
    _cookie_fingerprint,
    _is_privileged,
    _norm_mp_name,
    _platform_client,
    _quark_cookie,
    _renewal_cooldown_until,
    _weread_cookie,
    _weread_cookie_for_shelf,
    add_benchmark,
    feed_biz,
    find_feed_biz_by_name,
    import_benchmarks_from_shelf,
    list_benchmarks,
    match_biz_from_werss,
    nudge_werss,
    refresh_weread_cookie,
    remove_benchmark,
    set_benchmark_active,
    weread_refresh_tick,
    weread_shelf,
    werss_feed_index,
)
from app.services.wechat._candidates import (  # noqa: F401
    _ENTITY_RE,
    _HAS_CJK,
    _PUNCT_RE,
    _TERM_STOPWORDS,
    _overlap,
    _push_candidates,
    candidate_discover_tick,
    discover_candidates,
    list_candidates,
    mine_title_entities,
    mine_title_terms,
    set_candidate_status,
)
from app.services.wechat._enrich import (  # noqa: F401
    _backfill_pan_links,
    _backfill_pan_urls,
    _enrich_new_articles,
    _insert_new_articles,
)
from app.services.wechat._listen import (  # noqa: F401
    _BAN_MARKERS,
    _COVER_QUOTA_TRIP,
    _LISTEN_CURSOR_KEY,
    _SHELF_REVIEW_KEYS,
    _SHELF_TS_KEYS,
    _WEREAD_QUOTA_MARKS,
    _acquire_listen_slot,
    _advance_listen_cursor,
    _bump_shelf_round,
    _detect_ban_reason,
    _is_weread_quota_error,
    _listen_lock_key,
    _listen_round,
    _load_shelf_marks,
    _my_pan_link_from_history,
    _push_listen,
    _release_listen_slot,
    _save_shelf_marks,
    _shelf_gate_plan,
    _shelf_slot,
    _shelf_ts_to_dt,
    _weread_collect,
    repush_unpushed,
    run_wechat_listen,
)
from app.services.wechat._sync import (  # noqa: F401
    _dedupe_sync_push_rows,
    _sync_push_after_transfer,
    sync_wechat_account,
)
from app.services.wechat._ticks import (  # noqa: F401
    keyword_article_all_users,
    keyword_article_tick,
    pan_cookie_keepalive_tick,
    run_full_sync_if_pending,
)
