"""夸克「我的分享」统计采集:share/update_list 接口(免签名直连,2026-09-29 实测)。

接口要点(主包 quark_app_index.js 位置 233998 的参数构造):
- POST https://drive-pc.quark.cn/1/clouddrive/share/update_list?pr=ucpro&fr=pc
- body(实测必需): page_size/page/fetch_total/share_read_statues=[0] 必带,
  空数组报 400、fr=android 报 401(fr 参与鉴权)、未知参数被静默忽略。
- 返回 data.list + data.metadata(_total 总数);统计字段 save_pv/click_pv/
  download_pv/visit_user_count。**2026-09-29 定案**:夸克官方「分享管理」
  只能看链接是否失效,不提供转存统计(运营者确认)——这些字段就是数据全集,
  -1/0 = 平台不对外,并非"有开关未打开";链接级转存数当前不可得,
  本采集的价值在库存清单/失效/违规监控与将来若开放的统计回填。
- 纪律: 只读采集、Cookie 复用 user_cookies 配置、失败抛语义化异常。
"""
from __future__ import annotations

from datetime import datetime

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import HotspotSuggestion, QuarkShareStat
from app.utils import get_logger

logger = get_logger(__name__)

UPDATE_LIST_URL = "https://drive-pc.quark.cn/1/clouddrive/share/update_list"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
PAGE_SIZE = 100
MAX_PAGES = 20  # 保险丝:单次采集最多 20 页(2000 条),防 metadata 异常导致死循环


class QuarkShareStatsError(Exception):
    """分享统计采集失败(message 面向用户,不含 Cookie)。"""


class QuarkShareStatsAuthError(QuarkShareStatsError):
    """Cookie 失效(401),需重新在「Cookie 管理」粘贴。"""


def _ms_to_dt(ms: int | None) -> datetime | None:
    """夸克毫秒时间戳 → datetime(无效值返回 None)。"""
    try:
        return datetime.fromtimestamp(int(ms) / 1000) if ms else None
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def fetch_share_page(cookie: str, page: int = 1, page_size: int = PAGE_SIZE,
                     timeout: float = 20.0) -> tuple[list[dict], dict]:
    """拉一页分享列表,返回 (list, metadata);非 200/业务非 0 抛异常。"""
    try:
        r = requests.post(
            UPDATE_LIST_URL, params={"pr": "ucpro", "fr": "pc"},
            headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                     "Cookie": cookie, "Origin": "https://pan.quark.cn",
                     "Referer": "https://pan.quark.cn/",
                     "Content-Type": "application/json"},
            json={"page_size": page_size, "page": page, "fetch_max_file_update_pos": 0,
                  "fetch_total": 1, "fetch_update_files": 0, "needTotalNum": 0,
                  "share_read_statues": [0]},
            timeout=timeout)
    except requests.RequestException as exc:
        raise QuarkShareStatsError(f"夸克接口网络异常:{type(exc).__name__}") from exc
    if r.status_code == 401:
        raise QuarkShareStatsAuthError("夸克 Cookie 已失效,请到 Cookie 管理重新粘贴")
    if r.status_code != 200:
        raise QuarkShareStatsError(f"夸克接口 HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as exc:
        raise QuarkShareStatsError("夸克接口返回非 JSON") from exc
    if data.get("code") != 0:
        raise QuarkShareStatsError(f"夸克接口业务错误:{data.get('message') or data.get('code')}")
    payload = data.get("data") or {}
    return payload.get("list") or [], payload.get("metadata") or {}


def collect_all(cookie: str) -> list[dict]:
    """翻页采集全部分享(按 metadata._total 判停,带 MAX_PAGES 保险丝)。"""
    items: list[dict] = []
    total: int | None = None
    for page in range(1, MAX_PAGES + 1):
        batch, meta = fetch_share_page(cookie, page)
        items.extend(batch)
        if total is None:
            total = int(meta.get("_total") or 0)
        if len(items) >= total or not batch:
            break
    return items


def _row_from_item(user_id: int, it: dict) -> dict:
    """接口条目 → 模型字段 dict(裁剪 title/path_info 防超列宽)。"""
    return {
        "user_id": user_id,
        "share_id": str(it.get("share_id") or ""),
        "pwd_id": str(it.get("pwd_id") or ""),
        "title": str(it.get("title") or "")[:255],
        "share_url": str(it.get("share_url") or "")[:255],
        "save_pv": int(it.get("save_pv") or 0),
        "click_pv": int(it.get("click_pv") or 0),
        "download_pv": int(it.get("download_pv") or 0),
        "visit_user_count": int(it.get("visit_user_count") or 0),
        "file_num": int(it.get("file_num") or 0),
        "status": int(it.get("status") or 0),
        "audit_status": int(it.get("audit_status") or 0),
        "path_info": str(it.get("path_info") or "")[:255],
        "share_created_at": _ms_to_dt(it.get("created_at")),
        "share_updated_at": _ms_to_dt(it.get("updated_at")),
    }


def upsert_stats(db: Session, user_id: int, items: list[dict]) -> int:
    """按 (user_id, share_id) 覆盖更新;新链接插入。返回落库条数。"""
    existing = {
        row.share_id: row
        for row in db.scalars(select(QuarkShareStat).where(QuarkShareStat.user_id == user_id)).all()
    }
    now = datetime.now()
    count = 0
    for it in items:
        fields = _row_from_item(user_id, it)
        if not fields["share_id"]:
            continue
        row = existing.get(fields["share_id"])
        if row is None:
            db.add(QuarkShareStat(captured_at=now, **fields))
        else:
            for k, v in fields.items():
                setattr(row, k, v)
            row.captured_at = now
        count += 1
    db.commit()
    return count


def backfill_suggestions(db: Session, user_id: int) -> int:
    """把采集到的保存数精确回填 hotspot_suggestions(按 share_url 匹配 link)。

    只回填人工未干预的行(saves_at 为空),不覆盖 record_saves.py 录入的值;
    夸克侧 save_pv<0(平台未给出)不回填。
    """
    stats = db.scalars(select(QuarkShareStat).where(QuarkShareStat.user_id == user_id)).all()
    by_url = {s.share_url: s.save_pv for s in stats if s.share_url and s.save_pv >= 0}
    if not by_url:
        return 0
    rows = db.scalars(select(HotspotSuggestion).where(
        HotspotSuggestion.user_id == user_id,
        HotspotSuggestion.saves_at.is_(None),
        HotspotSuggestion.link.in_(by_url.keys()))).all()
    now = datetime.now()
    for row in rows:
        row.saves = by_url[row.link]
        row.saves_at = now
    db.commit()
    return len(rows)


def collect_for_user(db: Session, user_id: int, cookie: str) -> dict:
    """采集 + 落库 + 回填建议,返回摘要(API 与调度共用)。"""
    items = collect_all(cookie)
    saved = upsert_stats(db, user_id, items)
    backfilled = backfill_suggestions(db, user_id)
    top = sorted((i for i in items if i.get("save_pv", 0) > 0),
                 key=lambda x: -x["save_pv"])[:5]
    return {"total": len(items), "saved": saved, "suggestions_backfilled": backfilled,
            "top_saves": [{"title": i.get("title"), "save_pv": i.get("save_pv"),
                           "share_url": i.get("share_url")} for i in top]}
