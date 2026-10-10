# -*- coding: utf-8 -*-
"""三盘的**自动**去重 / 清理(2026-10-10,用户口径「做第二档,严格规定好边界」)。

## 边界 —— 每一条都对着本仓踩过的坑,不是想当然

1. **删除一律进回收站,永不彻底删。**
   夸克 `apply_plan` 里 `to_recycle=True` 是**硬编码**的;迅雷走 `trash_files()`(移入回收站)。
   ⚠️ **百度不在本模块的删除范围内** —— 它的删除**不可逆**(`baidupan_transfer.delete_paths`
   的注释原话:「百度这边没有回收站保证 —— 调用方必须自己确认路径」)。
   所以百度**只报告、绝不自动删**(那个副作用不可撤销,而"看错一行"的成本是不对称的)。
2. **报告与执行共用同一份判据、同一个范围参数。**
   2026-10-10 那次「报告说 102 个 / 3.66 GiB,执行删了 **7735 个 / 264 GiB**」的根因就是
   两边默认范围不一致(一个只清 10 月、一个清了所有月份)。⇒ 本模块**不自己造范围**,
   范围一律来自 `settings`、并且**每一轮都大声写进运行记录**。判据也全部从
   `quark_dup` / `pan_promo_sweep` 引,**一行都不重写**(那种"两处各写一遍、迟早飘"的事本仓吃过亏)。
3. **单轮删除有硬上限**(`pan_auto_clean_max_deletes`,默认 30)。
   撞上限就**停,不硬冲**;剩下的留给下一轮 —— 每轮都在跑,会自己收敛。
4. **没扫完就拒绝删**(夸克)。`build_plan` 的 `complete=False` 意味着
   「只出现在一个包里」这句判断**是假的**(没扫到的家里可能也有一份)⇒ 计划不可信,
   **直接跳过本轮**,等快照攒齐。这是脚本那一版就有的护栏(`--allow-partial` 才可绕过),
   自动作业**不提供绕过开关**。
5. **弱判据永不自动删。** 引流话术那类(`WEAK_PATTERNS`/`review` 堆)只报告、不动手 ——
   实测它们在**重复盘上恒成立**,是误判的主要来源。
6. **我方的东西一票否决。** 我方分享链的 `first_fid`、我们自己的 `简介.doc`,
   `classify` / `build_plan` 里已经永不入选,本模块**再核一遍**(删掉 = 已发出的链接立刻失效)。
7. **`dry_run` 默认开。** 头几轮只看数字;确认无误再把 `pan_auto_clean_dry_run` 关掉。
   每轮的数字(扫了几个家/计划删几条/实际删几条/多少体积/有没有撞上限/为什么跳过)
   **全部进 `runs.detail`** —— 别让「这轮什么都没删」与「这条路没接上」长得一样。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 百度**只报告**用的搜索词(**故意取少**:每个词都是一次平台请求)
_BAIDU_WORDS = ("监控简介", "宣传简介", "引流")


def _cap(plan_delete: list[dict], left: int) -> tuple[list[dict], int]:
    """按剩余额度截断删除清单。返回 `(要删的, 被上限挡下的条数)`。

    ⚠️ **一定返回挡住多少** —— 否则"剩下 200 条没删"看起来就像"总共只有 30 条",
    下一轮再看到同样的数字,人会以为脚本卡住了(本仓"静默"的老毛病)。
    """
    if left <= 0:
        return [], len(plan_delete)
    if len(plan_delete) <= left:
        return plan_delete, 0
    return plan_delete[:left], len(plan_delete) - left


def clean_quark(db: Session, user_id: int, settings, *, dry_run: bool, left: int) -> dict:
    """夸克:去重(只删**同一份文件出现在 ≥2 个家里**的那种)。返回本轮统计。"""
    from app.services import quark_dup
    from app.services.cookie_store import get_cookie
    from app.services.quark_dup_cache import ScanCache
    from app.services.quark_transfer import QuarkTransfer

    ck = (get_cookie(db, user_id, "quark") or getattr(settings, "quark_cookie", "") or "").strip()
    if not ck:
        return {"pan": "夸克", "state": "no_cookie"}
    scope = str(getattr(settings, "pan_auto_clean_quark_month", "") or "")
    try:
        qt = QuarkTransfer(ck, fid_store=getattr(settings, "quark_fid_store", "") or None)
        cache = ScanCache()
        plan = quark_dup.build_plan(qt, mmdd_prefix=scope, depth=2, cache=cache)
    except Exception as exc:  # noqa: BLE001 - 扫不动就如实报,别当"没有重复"
        logger.warning("夸克自动去重:扫描失败 %s: %s", type(exc).__name__, str(exc)[:120])
        return {"pan": "夸克", "state": "scan_failed", "reason": f"{type(exc).__name__}: {str(exc)[:100]}"}

    # ★ 护栏 4:没扫完 ⇒ 计划不可信 ⇒ 拒绝删
    if not plan.get("complete", True):
        rem = plan.get("remaining_homes") or []
        return {"pan": "夸克", "state": "incomplete", "planned": int(plan.get("n_delete") or 0),
                "remaining_homes": len(rem), "scanned": int(plan.get("scanned_homes") or 0)}

    all_del = list(plan.get("delete") or [])
    todo, blocked = _cap(all_del, left)
    out: dict[str, Any] = {
        "pan": "夸克", "state": "ok", "planned": int(plan.get("n_delete") or 0),
        "scanned": int(plan.get("scanned_homes") or 0), "blocked_by_cap": blocked,
        "scope": scope or "(所有家)", "protected": int(plan.get("protected") or 0),
    }
    if dry_run:
        out["deleted"] = 0
        out["dry_run"] = True
        return out
    plan["delete"] = todo                     # 只喂截断后的清单给执行器
    res = quark_dup.apply_plan(qt, plan, cache=cache)
    out["deleted"] = int(res.get("deleted") or 0)
    out["freed"] = int(res.get("bytes") or 0)
    if res.get("failed"):
        out["errors"] = res["failed"][:3]
    return out


def clean_xunlei(db: Session, user_id: int, settings, *, dry_run: bool, left: int) -> dict:
    """迅雷:**过期转存**清理(判据 = 7 天内没人再提到它)。返回本轮统计。"""
    from app.services import xunlei_cleanup

    days = int(getattr(settings, "pan_auto_clean_xunlei_days", 7) or 7)
    try:
        # `run_cleanup` 自带 `xunlei_cleanup_max_per_run` 限流与 `dry_run`,不重写
        res = xunlei_cleanup.run_cleanup(db, user_id, days=days, settings=settings, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("迅雷自动清理失败 %s: %s", type(exc).__name__, str(exc)[:120])
        return {"pan": "迅雷", "state": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:100]}"}
    if res.get("error"):
        return {"pan": "迅雷", "state": "error", "reason": str(res["error"])[:100]}
    return {"pan": "迅雷", "state": "ok", "days": days,
            "planned": int(res.get("to_delete") or 0), "deleted": int(res.get("deleted") or 0),
            "blocked_by_limit": int(res.get("skipped_by_limit") or 0),
            "dry_run": bool(res.get("dry_run")),
            "errors": (res.get("errors") or [])[:3]}


def report_baidu(db: Session, user_id: int, settings) -> dict:
    """百度:**只报告,永不自动删**(它没有回收站,删了不可逆)。返回本轮统计。"""
    from app.services import pan_promo_sweep
    from app.services.cookie_store import get_cookie

    # ⚠️ 平台键是 **`baidupan`**(不是 `baidu`)—— 与 `pan_discovery` / `_enrich` 同口径
    ck = (get_cookie(db, user_id, "baidupan") or "").strip()
    if not ck:
        return {"pan": "百度", "state": "no_cookie"}
    try:
        # ⚠️ `BaiduAdapter(cookie)` 只收 cookie(它自己造客户端)—— 与夸克那边的 `QuarkTransfer` 同形
        ad = pan_promo_sweep.BaiduAdapter(ck)
        plan = pan_promo_sweep.build_plan(ad, words=_BAIDU_WORDS)
    except Exception as exc:  # noqa: BLE001 - 百度凭据失效/被挡都不该拖垮整轮
        logger.info("百度清引流(只报告)跳过:%s: %s", type(exc).__name__, str(exc)[:100])
        return {"pan": "百度", "state": "skipped", "reason": f"{type(exc).__name__}: {str(exc)[:80]}"}
    return {"pan": "百度", "state": "report_only",
            "strong": len(plan.get("delete") or []), "weak": len(plan.get("review") or []),
            "note": "不可逆,只报告;要删请人工跑 scripts/pan_dedupe_apply.py"}


def auto_clean_tick(settings=None) -> int:
    """计划任务入口:三盘自动去重/清理。返回本轮**实际删除**的条数(0 = 没删/只报告)。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User
    from app.services.tenant_base import _record_run

    settings = settings or get_settings()
    if not getattr(settings, "pan_auto_clean_enabled", False):
        return 0
    dry = bool(getattr(settings, "pan_auto_clean_dry_run", True))
    budget = int(getattr(settings, "pan_auto_clean_max_deletes", 30) or 0)
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            left = budget
            parts: list[dict] = []
            try:
                q = clean_quark(db, uid, settings, dry_run=dry, left=left)
                parts.append(q)
                left -= int(q.get("deleted") or 0)          # ★ 两个盘**共用**这个硬上限
                parts.append(clean_xunlei(db, uid, settings, dry_run=dry, left=left))
                if getattr(settings, "pan_auto_clean_baidu_report", True):
                    parts.append(report_baidu(db, uid, settings))
            except Exception:  # noqa: BLE001 - 单盘失败不该拖垮其余
                logger.exception("自动清理失败 user=%s", uid)
                db.rollback()
                _record_run(db, uid, "pan_auto_clean", "failed", "异常,见日志")
                db.commit()
                continue

            total += sum(int(p.get("deleted") or 0) for p in parts)
            detail = ("[dry-run] " if dry else "") + f"上限{budget} | " + " | ".join(
                _fmt(p) for p in parts)
            _record_run(db, uid, "pan_auto_clean", "success", detail[:900])
            db.commit()
            logger.info("三盘自动清理:%s", detail[:400])
    finally:
        db.close()
    return total


def _fmt(p: dict) -> str:
    """单盘统计 → 一行人读文本。**没删/跳过也要写清楚为什么**(护栏 7)。"""
    st = p.get("state")
    if st in ("no_cookie", "scan_failed", "failed", "error", "skipped"):
        return f"{p['pan']}跳过({st}:{str(p.get('reason') or '')[:50]})"
    if st == "incomplete":
        return (f"{p['pan']}跳过(没扫完:已扫{p.get('scanned')}个家,"
                f"还差{p.get('remaining_homes')}个;计划{p.get('planned')}条不可信)")
    if st == "report_only":
        return f"{p['pan']}**只报告** 强模式{p.get('strong')}条 / 待确认{p.get('weak')}条"
    bits = [f"扫{p.get('scanned', '-')}", f"计划{p.get('planned', 0)}"]
    if p.get("dry_run"):
        bits.append("**dry-run 未删**")
    else:
        bits.append(f"删{p.get('deleted', 0)}")
        if p.get("freed"):
            bits.append(f"省{int(p['freed']) / 2 ** 30:.2f}GiB")
        if p.get("blocked_by_cap"):
            bits.append(f"撞上限留下{p['blocked_by_cap']}条")
        if p.get("blocked_by_limit"):
            bits.append(f"撞限流留下{p['blocked_by_limit']}条")
    if p.get("errors"):
        bits.append(f"失败{len(p['errors'])}")
    return f"{p['pan']} " + " ".join(bits)
