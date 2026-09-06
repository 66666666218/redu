"""闲鱼分析域(见 doc/dev.md §5.8):每日快照、深度采集、涨跌分析。

从 `tenant.py` 拆分而来,避免单文件过大。依赖 `tenant_base` 的 `_base`/`_record_run`,
不反向 import tenant,规避循环依赖。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings
from app.db import repository
from app.db.models import XianyuDaily, XianyuSummary, RunRecord
from app.services import xianyu
from app.services.cookie_store import get_cookies
from app.services.tenant_base import _base, _record_run, persist_refreshed_cookie, verify_cooldown_active
from app.utils import get_logger

logger = get_logger(__name__)


def xianyu_daily(session: Session, user_id: int) -> dict:
    today = datetime.now().date().isoformat()
    summary = session.scalar(
        select(XianyuSummary).where(XianyuSummary.user_id == user_id).order_by(XianyuSummary.id.desc())
    )
    return {"summary_date": summary.summary_date if summary else None, "items": []}


def _xy_detail_limit(settings: Settings) -> int:
    return getattr(settings, "xianyu_detail_limit", 20)


def xianyu_deep_due(session: Session, user_id: int, settings: Settings) -> bool:
    """闲鱼深采是否到期:距上次成功深采 >= `xianyu_deep_interval_hours`,且当前不在验证冷却。

    搜索接力深采时用——避免每次搜索(2h)都跑一次深采(10详情)累积风控;默认 6 小时一次。
    """
    if verify_cooldown_active(session, user_id, settings):
        return False
    hours = getattr(settings, "xianyu_deep_interval_hours", None) or 6
    cutoff = datetime.now() - timedelta(hours=hours)
    last = session.scalar(
        select(RunRecord).where(
            RunRecord.user_id == user_id, RunRecord.kind == "xianyu_deep",
            RunRecord.status.in_(["success", "partial"]), RunRecord.started_at >= cutoff,
        ).order_by(RunRecord.id.desc())
    )
    return last is None


def run_xianyu_deep(session: Session, user_id: int, settings: Settings | None = None,
                    hot: list[dict] | None = None) -> dict:
    """采集闲鱼热榜并抓取前 N 商品详情(想要数/类目/浏览量/卖家粉丝),写入当日快照。

    `hot` 可传已采集的热榜(搜索接力深采时复用,避免重复搜索);缺省则自行 collect_hot。
    """
    settings = _base(settings)
    if verify_cooldown_active(session, user_id, settings):  # 验证后冷却:避免反复撞滑块
        _record_run(session, user_id, "xianyu_deep", "skipped", "verify_cooldown")
        session.commit()
        return {"platform": "xianyu_deep", "count": 0, "status": "skipped", "reason": "verify_cooldown"}
    cookies = get_cookies(session, user_id)
    goofish = cookies.get("goofish", "")
    if not goofish:
        raise ValueError("未配置闲鱼 Cookie")
    # 构造客户端不产生网络请求,放在 try 外:失败路径也能回写运行中刷新的令牌
    client = xianyu.XianyuClient(goofish, proxy=settings.xianyu_proxy_url or None)
    try:
        if hot is None:
            hot = xianyu.collect_hot(settings, client)
        today = datetime.now().date().isoformat()
        base_delay = getattr(settings, "xianyu_request_delay", None) or getattr(settings, "request_delay_seconds", 2.5)
        saved = 0
        stop_reason = None
        for idx, it in enumerate(hot[: _xy_detail_limit(settings)]):
            try:
                detail = xianyu.fetch_detail(client, it["item_id"])
            except xianyu.XianyuVerify:
                stop_reason = "verify"
                logger.warning("闲鱼详情触发人机验证,停止抓取;请人工过滑块或更换出口IP")
                break
            except xianyu.XianyuRateLimit:
                stop_reason = "rate_limit"
                logger.warning("闲鱼限流,停止抓取详情")
                break
            row = repository.get_xianyu_daily(session, user_id, it["item_id"], today)
            if row is None:
                row = XianyuDaily(user_id=user_id, snap_date=today, item_id=it["item_id"])
                session.add(row)
            row.title = it["title"][:500]
            row.price = str(it["price"] or "")[:64]
            row.category = detail.get("category", "")[:64]
            row.want_count = detail.get("want_count", 0)
            row.collect_count = detail.get("collect_count", 0)
            row.sold_count = detail.get("sold_count", 0)
            row.seller_fans = detail.get("seller_fans", 0)
            saved += 1
            if idx < _xy_detail_limit(settings) - 1:
                import random
                import time

                time.sleep(base_delay * random.uniform(0.8, 1.4))
        session.commit()
        status = "partial" if stop_reason else "success"
        detail_note = f"items={saved}" + (f",{stop_reason}" if stop_reason else "")
        _record_run(session, user_id, "xianyu_deep", status, detail_note)
        session.commit()
        persist_refreshed_cookie(session, user_id, client)
        return {"platform": "xianyu_deep", "count": saved, "status": status, "reason": stop_reason}
    except (xianyu.XianyuVerify, xianyu.XianyuRateLimit) as exc:
        # collect_hot 整轮被滑块/限流(全部关键词失败)→ 优雅降级,不 500
        session.rollback()
        _record_run(session, user_id, "xianyu_deep", "failed", f"{type(exc).__name__}: {exc}")
        session.commit()
        persist_refreshed_cookie(session, user_id, client)
        return {"platform": "xianyu_deep", "count": 0, "status": "failed", "reason": type(exc).__name__}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        _record_run(session, user_id, "xianyu_deep", "failed", f"{type(exc).__name__}: {exc}")
        session.commit()
        persist_refreshed_cookie(session, user_id, client)
        raise


def xianyu_analytics(session: Session, user_id: int) -> dict:
    """闲鱼深度面板:今日vs昨日 想要数涨跌、类目分布、上升/下降榜。"""
    today = datetime.now().date().isoformat()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    today_rows = {r.item_id: r for r in repository.xianyu_daily_by_date(session, user_id, today)}
    yesterday_rows = {r.item_id: r for r in repository.xianyu_daily_by_date(session, user_id, yesterday)}
    items = []
    for iid, t in today_rows.items():
        y = yesterday_rows.get(iid)
        y_want = y.want_count if y else None
        delta = (t.want_count - y_want) if y_want is not None else 0
        pct = (delta / y_want) if y_want else (100.0 if t.want_count > 0 else 0.0)
        items.append(
            {
                "item_id": iid,
                "title": t.title[:44],
                "category": t.category or "未分类",
                "price": t.price,
                "want_today": t.want_count,
                "want_yesterday": y_want,
                "delta": delta,
                "pct": pct,
                "collect_today": t.collect_count,
                "sold_today": t.sold_count,
                "seller_fans": t.seller_fans,
            }
        )
    items.sort(key=lambda x: x["delta"], reverse=True)
    cats: dict[str, int] = {}
    total_want = 0
    for it in items:
        cats[it["category"]] = cats.get(it["category"], 0) + 1
        total_want += it["want_today"]
    return {
        "date": today,
        "count": len(items),
        "total_want": total_want,
        "top_risers": items[:10],
        "top_fallers": sorted(items, key=lambda x: x["delta"])[:10],
        "categories": [{"name": k, "count": v} for k, v in sorted(cats.items(), key=lambda x: -x[1])],
        "items": items,
    }
