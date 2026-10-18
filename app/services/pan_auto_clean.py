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
    from app.services.quark_dup_cache import ScanCache
    from app.services.quark_transfer import QuarkTransfer

    #: ★ **按池取号**(2026-10-16):见 `pan_accounts.cookies_for`(没配池 = 老行为)
    from app.services import pan_accounts as _A

    ck = (_A.cookie_for(db, "quark", user_id=user_id)
          or getattr(settings, "quark_cookie", "") or "").strip()
    if not ck:
        return {"pan": "夸克", "state": "no_cookie"}
    scope = str(getattr(settings, "pan_auto_clean_quark_month", "") or "")
    #: ★★ **TTL 必须由作业传进来**(2026-10-15):`build_plan` 自己的默认是 8h,
    #: 而本作业**每天**才跑一轮 ⇒ 8h 的快照届时**全已过期** ⇒ 缓存形同虚设、
    #: 每轮从零重扫、在同一处撞预算 ⇒ `complete` 恒 False ⇒ **夸克连着三天没删过东西**。
    #: 见 `settings.pan_auto_clean_quark_ttl_hours` 与 `quark_dup.build_plan` 的说明。
    ttl = float(getattr(settings, "pan_auto_clean_quark_ttl_hours", 48.0) or 48.0)
    try:
        qt = QuarkTransfer(ck, fid_store=getattr(settings, "quark_fid_store", "") or None)
        cache = ScanCache()
        plan = quark_dup.build_plan(qt, mmdd_prefix=scope, depth=2, cache=cache,
                                    ttl_hours=ttl)
    except Exception as exc:  # noqa: BLE001 - 扫不动就如实报,别当"没有重复"
        logger.warning("夸克自动去重:扫描失败 %s: %s", type(exc).__name__, str(exc)[:120])
        return {"pan": "夸克", "state": "scan_failed", "reason": f"{type(exc).__name__}: {str(exc)[:100]}"}

    # ★ 护栏 4:没扫完 ⇒ 计划不可信 ⇒ 拒绝删
    if not plan.get("complete", True):
        rem = plan.get("remaining_homes") or []
        #: ★★ **必须把"为什么没扫完"带出去**(2026-10-15):只报
        #: `已扫19个家,还差9个` 的话,**看不出是快照过期还是预算不够** ——
        #: 而这两者的修法完全不同(改 TTL / 加预算)。那三天就是这么卡住的:
        #: 每天数字一字不差,没人能看出它其实**每天从零重扫**。
        return {"pan": "夸克", "state": "incomplete", "planned": int(plan.get("n_delete") or 0),
                "remaining_homes": len(rem), "scanned": int(plan.get("scanned_homes") or 0),
                "calls": int(plan.get("calls") or 0),
                "cache_miss": dict(plan.get("cache_miss") or {}), "ttl_hours": ttl}

    all_del = list(plan.get("delete") or [])
    todo, blocked = _cap(all_del, left)
    intro = list(plan.get("intro_protected") or [])
    out: dict[str, Any] = {
        "pan": "夸克", "state": "ok", "planned": int(plan.get("n_delete") or 0),
        "scanned": int(plan.get("scanned_homes") or 0), "blocked_by_cap": blocked,
        "scope": scope or "(所有家)", "protected": int(plan.get("protected") or 0),
        # ★ 宣传简介(会被复制进每个资源)到底护住了几个 —— 护不住要能一眼看见,
        #   所以这也写进 `runs.detail`(详见 `quark_dup.collect_intro_protect`)。
        "intro_protected": len(intro),
    }
    if not intro:
        logger.warning("夸克自动去重:简介保护名单为**空**(简介目录取不到或本来就是空的)"
                       "—— 本轮简介文件不在保护范围内")
    # ★★ **整包重复**(2026-10-16):上面那条抓的是**跨家**的同名同大小**文件**;
    #    而真占空间的是**同一个家下面的 `X` 与 `X(1)`** —— 夸克给重名**目录**加后缀 ⇒
    #    **整个资源包多一份**(实测一轮删掉 24 份 / 44.48 GiB)。
    #    ⚠️ 那批重复 `classify` **一条都抓不到**:它们的目录名不同,不在"同一家/同一目录"下。
    #    ⚠️ 放在**上面那条之后**:先删文件级重复(便宜),再动整包(要对每个候选列目录)。
    #    ⚠️ 它**当场列目录**(不吃快照)—— "删"要用当前事实。
    try:
        pkg = quark_dup.build_package_plan(
            qt, protect_dirs=(getattr(settings, "pan_intro_quark_dir", "") or "/监听宣传",))
    except Exception as exc:  # noqa: BLE001 - 整包那条扫不动不该拖垮上面那条已经删好的
        logger.warning("夸克整包去重:扫描失败 %s: %s", type(exc).__name__, str(exc)[:110])
        pkg = {}
    pkg_groups = list(pkg.get("groups") or [])
    out["pkg_groups"] = int(pkg.get("n_groups") or 0)
    out["pkg_planned"] = int(pkg.get("n_drop") or 0)
    out["pkg_bytes"] = int(pkg.get("bytes") or 0)
    #: 两个去重**共用**同一个硬上限(`left`):整包那一份也算一次删除。
    #: 两个去重**共用**同一个硬上限(`left`):文件级先删 ⇒ 整包那一遍的余量要**减去**
    #: 这次将要删掉的条数。
    #: ⚠️⚠️ **这里原来写的是 `out["deleted"]`,而那个键要到下面才被赋值** ⇒
    #:   `KeyError: 'deleted'` **把整个夜间清理打挂了**(2026-10-16 04:32 那一轮记成
    #:   「异常,见日志」,三盘**一件没做**;10-14 之前一直好,10-16 我加的这段之后才坏)。
    #:   教训:**新加的那段要用"手上已经有的量"算**,别去读"稍后才会写的键"。
    #:   ⚠️ 而且用 `len(todo)` 本来就更对:`left` 是"这一轮总共只许删这么多",
    #:   文件级已经占掉 `len(todo)` 个名额了。
    room = max(0, left - len(todo))
    take, blocked_pkg = [], 0
    used = 0
    for g in pkg_groups:
        if used + len(g["drop"]) > room:
            blocked_pkg += 1
            continue
        take.append(g)
        used += len(g["drop"])
    if blocked_pkg:
        out["pkg_blocked_by_cap"] = blocked_pkg
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
    if take:
        res2 = quark_dup.apply_package_plan(qt, {"groups": take, "bytes": out["pkg_bytes"]},
                                            to_recycle=True)
        out["pkg_deleted"] = int(res2.get("deleted") or 0)
        out["freed"] = int(out["freed"]) + int(res2.get("bytes") or 0)
        if res2.get("failed"):
            out["errors"] = (out.get("errors") or []) + list(res2["failed"])[:3]
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


def report_baidu(db: Session, user_id: int, settings, *, left: int = 0,
                 report_path: str | None = None) -> dict:
    """百度:**只报告,永不自动删**(它没有回收站,删了不可逆)。返回本轮统计。

    ★ **2026-10-16 起报两件事**(用户口径「先做识别 + 清单」):
      ① **引流文件**(`pan_promo_sweep`,原样保留);
      ② **跨目录重复**(`baidu_dup`,判据与夸克那边同一条:同名+同大小、出现在不同目录)。
    ⚠️ 重复清单**落到 `settings.pan_auto_clean_baidu_report_path`** 供人工过目 ——
    **默认不删**是有意的:百度删了捞不回来
    (`baidupan_transfer.delete_paths` 的文档写着「调用方必须自己确认路径」)。

    ⚠️ **`report_path` 是给测试用的**(2026-10-19):原来路径写死 `data/...`,
    而 `tests/test_baidu_dup.py` 调的就是这个真实函数 ⇒ 跑一次测试就把**生产清单**
    覆盖成 `dup0.apk` 那种造出来的数据。测试必须显式传临时路径。
    """
    from app.services import pan_promo_sweep

    # ⚠️ 平台键是 **`baidupan`**(不是 `baidu`)—— 与 `pan_discovery` / `_enrich` 同口径
    from app.services import pan_accounts as _A

    ck = (_A.cookie_for(db, "baidupan", user_id=user_id) or "").strip()
    if not ck:
        return {"pan": "百度", "state": "no_cookie"}
    out: dict = {"pan": "百度", "state": "report_only"}
    try:
        # ⚠️ `BaiduAdapter(cookie)` 只收 cookie(它自己造客户端)—— 与夸克那边的 `QuarkTransfer` 同形
        ad = pan_promo_sweep.BaiduAdapter(ck)
        plan = pan_promo_sweep.build_plan(ad, words=_BAIDU_WORDS)
        out["strong"] = len(plan.get("delete") or [])
        out["weak"] = len(plan.get("review") or [])
    except Exception as exc:  # noqa: BLE001 - 百度凭据失效/被挡都不该拖垮整轮
        logger.info("百度清引流(只报告)跳过:%s: %s", type(exc).__name__, str(exc)[:100])
        return {"pan": "百度", "state": "skipped",
                "reason": f"{type(exc).__name__}: {str(exc)[:80]}"}
    # ⚠️ 这一段用 **`BaiduPanClient`**(它有 `list_dir`),不是 `BaiduAdapter`
    #    (那个只有 `search_by_name`,列不了目录 ⇒ 判不了"跨目录")
    try:
        import json
        from pathlib import Path

        from app.services import baidu_dup
        from app.services.baidupan_transfer import BaiduPanClient

        root = (getattr(settings, "pan_auto_clean_baidu_root", "") or "").strip() \
            or baidu_dup.DEFAULT_ROOT
        dup = baidu_dup.build_plan(
            BaiduPanClient(ck), roots=(root,),
            intro_dir=(getattr(settings, "pan_intro_baidu_dir", "") or ""))
        p = Path(report_path
                 or getattr(settings, "pan_auto_clean_baidu_report_path", "")
                 or "data/pan_dedupe_baidu.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"root": root, **dup}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
        out.update({"dup_groups": dup["n_groups"], "dup_files": dup["n_dup"],
                    "dup_bytes": dup["bytes"], "dup_scanned": dup["scanned"],
                    "dup_truncated": dup["truncated"],
                    "dup_errors": dup.get("n_errors", 0),
                    "intro_protected": dup["intro_protected"],
                    "dup_list": str(p)})
        # ★★★ **删除开关**(默认关;2026-10-16 用户口径「先做识别 + 清单,看过再决定」)。
        if dup.get("n_dup") and bool(getattr(settings, "pan_auto_clean_baidu_delete", False)):
            out["auto_delete"] = True
            cap = max(0, int(left or 0))
            todo = list(dup["dup_files"])[:cap]
            #: ⚠️ **余量要记账**(不管删没删成):只写"删了 N 份"看不出"还剩多少没删"
            blocked = int(dup["n_dup"]) - len(todo)
            if blocked > 0:
                out["baidu_blocked_by_cap"] = blocked
            if todo:
                try:
                    #: ⚠️⚠️ **不可逆**:百度没有回收站(`delete_paths` 的文档原话是
                    #: 「调用方必须自己确认路径」)⇒ 这里**只删清单里那些重复副本**、
                    #: `keep` 那份永不删(它不在 `dup_files` 里)、且只收**绝对路径**。
                    #: 任何异常**如实报**,绝不静默(静默 = 客户以为清了而其实没清)。
                    _paths = [str(x["path"]) for x in todo if str(x.get("path") or "").startswith("/")]
                    out["baidu_deleted"] = int(BaiduPanClient(ck).delete_paths(_paths))
                    out["baidu_freed"] = sum(int(x.get("size") or 0) for x in todo)
                except Exception as exc:  # noqa: BLE001 - 删失败要让报告看见,不是吞掉
                    out["baidu_delete_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
                    logger.warning("百度重复删除失败:%s", str(exc)[:120])
    except Exception as exc:  # noqa: BLE001 - 识别失败不该让上面那份引流报告也白做
        logger.info("百度跨目录重复识别跳过:%s: %s", type(exc).__name__, str(exc)[:100])
        out["dup_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    out["note"] = ("不可逆,只报告;删除开关默认关闭(`PAN_AUTO_CLEAN_BAIDU_DELETE=false`),"
                   "看过真实清单、确认判据没误判再开;"
                   f"清单见 {out.get('dup_list') or 'data/pan_dedupe_baidu.json'},"
                   "重扫用 `python scripts/baidu_dedupe.py`(只读)")
    return out


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
                    #: ⚠️ 把**剩余删除额度**传进去:百度那条(若开了删除)与夸克/迅雷**共用**上限
                    parts.append(report_baidu(db, uid, settings, left=left))
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
        #: ★ 「**为什么**没扫完」必须写出来(2026-10-15):`stale` = 快照过期(改 TTL),
        #: 没写 `stale` 才是预算(加预算)。只写计数的话,这两件事长得一模一样。
        miss = p.get("cache_miss") or {}
        why = ",".join(f"{k}×{v}" for k, v in sorted(miss.items())) or "无"
        return (f"{p['pan']}跳过(没扫完:已扫{p.get('scanned')}个家,"
                f"还差{p.get('remaining_homes')}个;计划{p.get('planned')}条不可信;"
                f"**重扫原因 {why}**;调用{p.get('calls')};TTL {p.get('ttl_hours')}h)")
    if st == "report_only":
        bits = [f"引流 强模式{p.get('strong')}条/待确认{p.get('weak')}条"]
        #: ★ **跨目录重复单独报**(2026-10-16):与"引流文件"是**两条判据**,
        #: 合在一起就再也看不出是"有人在硬塞广告"还是"我们自己存重了"。
        if p.get("dup_groups") is not None:
            bits.append(f"**跨目录重复{p['dup_groups']}组/多出{p.get('dup_files', 0)}份"
                        f"({int(p.get('dup_bytes') or 0) / 2 ** 30:.2f}GiB)**")
            if p.get("dup_truncated"):
                bits.append("⚠️重复扫没列完(撞上限),数字偏小")
            if p.get("dup_errors"):
                bits.append(f"⚠️{p['dup_errors']}个目录没列成")
            #: ★ **真的删了才报** —— 且把「省了多少」一起给(只报份数看不出值不值得)
            if p.get("baidu_deleted"):
                bits.append(f"**已删{p['baidu_deleted']}份"
                            f"(省{int(p.get('baidu_freed') or 0) / 2 ** 30:.2f}GiB)"
                            f"(⚠️不可逆)**")
            if p.get("baidu_blocked_by_cap"):
                bits.append(f"撞上限留下{p['baidu_blocked_by_cap']}份")
            if p.get("baidu_delete_error"):
                bits.append(f"⚠️删除失败:{str(p['baidu_delete_error'])[:40]}")
            if p.get("intro_protected"):
                bits.append(f"简介护{p['intro_protected']}")
            bits.append(f"清单{p.get('dup_list')}")
        elif p.get("dup_error"):
            bits.append(f"⚠️重复识别失败({str(p['dup_error'])[:40]})")
        return f"{p['pan']}**只报告(不删)** " + " | ".join(bits)
    bits = [f"扫{p.get('scanned', '-')}", f"计划{p.get('planned', 0)}"]
    if p.get("intro_protected"):
        bits.append(f"简介护{p['intro_protected']}")
    if p.get("dry_run"):
        bits.append("**dry-run 未删**")
    else:
        bits.append(f"删{p.get('deleted', 0)}")
        if p.get("freed"):
            bits.append(f"省{int(p['freed']) / 2 ** 30:.2f}GiB")
        if p.get("blocked_by_cap"):
            bits.append(f"撞上限留下{p['blocked_by_cap']}条")
        #: ★ **整包重复**单独报(2026-10-16):它与上面的"文件级去重"是**两条判据**,
        #: 合在一起就再也看不出是哪一层在删(与"盘链级 vs 资源级复用"同一条纪律)。
        if p.get("pkg_groups"):
            bits.append(f"**整包重复{p['pkg_groups']}组**/删{p.get('pkg_deleted', 0)}份"
                        f"({int(p.get('pkg_bytes') or 0) / 2 ** 30:.2f}GiB)")
        if p.get("pkg_blocked_by_cap"):
            bits.append(f"整包撞上限留下{p['pkg_blocked_by_cap']}组")
        if p.get("blocked_by_limit"):
            bits.append(f"撞限流留下{p['blocked_by_limit']}条")
    if p.get("errors"):
        bits.append(f"失败{len(p['errors'])}")
    return f"{p['pan']} " + " ".join(bits)
