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
    """夸克:去重(只删**同一份文件出现在 ≥2 个家里**的那种)。返回本轮统计。

    ★★ **按池遍历所有账号**(2026-11-13):此前这里是 `cookie_for()` —— **只取一个号**。
    而 `cookie_for` 返回的是**最空**的那个(转存要挑最空的写,清理不是):
    实测 `夸克2` 只有 **1 个家**、`夸克1` 有 **19 个家**,于是清理**永远只扫 1 个家**、
    `计划0 删0` 连挂 8 天(11-05 ~ 11-12),而真正该清的 1 号已用 **123%** 却从没被扫过。
    ⇒ 与 `clean_xunlei` 同一个形状:**逐号扫各自的盘**,单号失败不挡其余。
    """
    from app.services import quark_dup
    from app.services.quark_dup_cache import ScanCache
    from app.services.quark_transfer import QuarkAuthError, QuarkTransfer

    from app.services import pan_accounts as _A

    #: ⚠️ **不能用 `cookie_for`**(只给最空的一个)。`refs_for` 给整个池(最空在前),
    #: 且**连 `AccountRef` 一起给** —— 下面按账号分家文件要用 `ref.cookie_key`,
    #: 拿 `label`(中文显示名)去反推是猜。
    try:
        pool = _A.refs_for(db, "quark", user_id=user_id)
    except Exception:  # noqa: BLE001 - 池读不到就退回 `.env` 那一枚(单账号老行为)
        logger.debug("读夸克账号池失败,回落 .env", exc_info=True)
        pool = []
    if not pool:
        ck = str(getattr(settings, "quark_cookie", "") or "").strip()
        pool = [(_A.default_ref("quark", user_id=user_id), ck)] if ck else []
    if not pool:
        return {"pan": "夸克", "state": "no_cookie"}

    scope = str(getattr(settings, "pan_auto_clean_quark_month", "") or "")
    #: ★★ **TTL 必须由作业传进来**(2026-10-15):`build_plan` 自己的默认是 8h,
    #: 而本作业**每天**才跑一轮 ⇒ 8h 的快照届时**全已过期** ⇒ 缓存形同虚设、
    #: 每轮从零重扫、在同一处撞预算 ⇒ `complete` 恒 False ⇒ **夸克连着三天没删过东西**。
    #: 见 `settings.pan_auto_clean_quark_ttl_hours` 与 `quark_dup.build_plan` 的说明。
    ttl = float(getattr(settings, "pan_auto_clean_quark_ttl_hours", 48.0) or 48.0)
    fid_base = str(getattr(settings, "quark_fid_store", "") or "")
    cache_base = str(getattr(settings, "pan_auto_clean_quark_cache", "") or "") \
        or "data/quark_dup_cache.json"

    per: list[dict] = []          #: 每个号一段,汇总时**分号列出**(否则看不出是哪个号在动)
    totals = {"planned": 0, "deleted": 0, "blocked_by_cap": 0, "scanned": 0,
              "intro_protected": 0, "pkg_groups": 0, "pkg_planned": 0, "pkg_bytes": 0,
              "pkg_deleted": 0, "pkg_blocked_by_cap": 0}
    freed = 0
    errors: list[str] = []
    budget = left
    for ref, ck in pool:
        if not ck:
            continue
        label, key = (ref.label or ref.cookie_key), str(ref.cookie_key or "quark")
        #: ★ **fid / 快照都按账号分家**(2026-11-13):两个号都有 `redian监听` 这个家,
        #: 但 **fid 不同** ⇒ 共用一份快照时 `check_home` 每轮都判 `FID_CHANGED`,
        #: 两边轮流作废对方的缓存,谁都不收敛(和 fid_store 那个坑是同一个)。
        store = _A.scoped_path(fid_base, key, platform="quark") if fid_base else None
        cache = ScanCache(_A.scoped_path(cache_base, key, platform="quark"))
        try:
            qt = QuarkTransfer(ck, fid_store=store)
            plan = quark_dup.build_plan(qt, mmdd_prefix=scope, depth=2, cache=cache,
                                        ttl_hours=ttl)
        except QuarkAuthError as exc:
            errors.append(f"{label}:登录态失效({str(exc)[:40]})")
            continue
        except Exception as exc:  # noqa: BLE001 - 扫不动就如实报,别当"没有重复"
            logger.warning("夸克自动去重(%s):扫描失败 %s: %s", label, type(exc).__name__,
                           str(exc)[:120])
            errors.append(f"{label}:扫描失败({type(exc).__name__})")
            continue

        # ★ 护栏 4:没扫完 ⇒ 计划不可信 ⇒ **这个号**拒绝删(其余号照常)
        if not plan.get("complete", True):
            per.append({"label": label, "state": "incomplete",
                        "planned": int(plan.get("n_delete") or 0),
                        "remaining_homes": len(plan.get("remaining_homes") or []),
                        "scanned": int(plan.get("scanned_homes") or 0),
                        "calls": int(plan.get("calls") or 0),
                        "cache_miss": dict(plan.get("cache_miss") or {}), "ttl_hours": ttl})
            continue

        all_del = list(plan.get("delete") or [])
        todo, blocked = _cap(all_del, budget)
        intro = list(plan.get("intro_protected") or [])
        one: dict[str, Any] = {
            "label": label, "state": "ok", "planned": int(plan.get("n_delete") or 0),
            "scanned": int(plan.get("scanned_homes") or 0), "blocked_by_cap": blocked,
            "scope": scope or "(所有家)", "protected": int(plan.get("protected") or 0),
            # ★ 宣传简介(会被复制进每个资源)到底护住了几个 —— 护不住要能一眼看见,
            #   所以这也写进 `runs.detail`(详见 `quark_dup.collect_intro_protect`)。
            "intro_protected": len(intro),
        }
        if not intro:
            logger.warning("夸克自动去重(%s):简介保护名单为**空**(简介目录取不到或本来就是空的)"
                           "—— 本轮简介文件不在保护范围内", label)
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
            logger.warning("夸克整包去重(%s):扫描失败 %s: %s", label, type(exc).__name__,
                           str(exc)[:110])
            pkg = {}
        pkg_groups = list(pkg.get("groups") or [])
        one["pkg_groups"] = int(pkg.get("n_groups") or 0)
        one["pkg_planned"] = int(pkg.get("n_drop") or 0)
        one["pkg_bytes"] = int(pkg.get("bytes") or 0)
        #: 两个去重**共用**同一个硬上限(`left`):整包那一份也算一次删除。
        #: 文件级先删 ⇒ 整包那一遍的余量要**减去**这次将要删掉的条数。
        #: ⚠️⚠️ **这里原来写的是 `out["deleted"]`,而那个键要到下面才被赋值** ⇒
        #:   `KeyError: 'deleted'` **把整个夜间清理打挂了**(2026-10-16 04:32 那一轮记成
        #:   「异常,见日志」,三盘**一件没做**;10-14 之前一直好,10-16 我加的这段之后才坏)。
        #:   教训:**新加的那段要用"手上已经有的量"算**,别去读"稍后才会写的键"。
        room = max(0, budget - len(todo))
        take, blocked_pkg = [], 0
        used = 0
        for g in pkg_groups:
            if used + len(g["drop"]) > room:
                blocked_pkg += 1
                continue
            take.append(g)
            used += len(g["drop"])
        if blocked_pkg:
            one["pkg_blocked_by_cap"] = blocked_pkg
        if dry_run:
            one["deleted"] = 0
            one["dry_run"] = True
            per.append(one)
            continue
        plan["delete"] = todo                     # 只喂截断后的清单给执行器
        res = quark_dup.apply_plan(qt, plan, cache=cache)
        one["deleted"] = int(res.get("deleted") or 0)
        one["freed"] = int(res.get("bytes") or 0)
        if res.get("failed"):
            one["errors"] = res["failed"][:3]
        if take:
            res2 = quark_dup.apply_package_plan(qt, {"groups": take, "bytes": one["pkg_bytes"]},
                                                to_recycle=True)
            one["pkg_deleted"] = int(res2.get("deleted") or 0)
            one["freed"] = int(one["freed"]) + int(res2.get("bytes") or 0)
            if res2.get("failed"):
                one["errors"] = (one.get("errors") or []) + list(res2["failed"])[:3]
        per.append(one)
        budget -= int(one.get("deleted") or 0) + int(one.get("pkg_deleted") or 0)

    for one in per:
        for k in totals:
            totals[k] += int(one.get(k) or 0)
        freed += int(one.get("freed") or 0)
    #: ⚠️ **单号的结果不能被汇总吃掉**:以前只回一个 dict,于是"扫了哪个号"根本无法表达。
    #: ⚠️ 顶层 `dry_run` **必须留着**:`_fmt` 靠它区分「演练没删」与「真删了 0 件」——
    #: 少了它,演练轮会被打成 `删0`,和"扫完了但确实没重复"长得一模一样。
    out: dict[str, Any] = {"pan": "夸克", "accounts": len(per), "per_account": per,
                           "state": "ok" if per else "no_account", "freed": freed,
                           "dry_run": bool(dry_run), **totals}
    if errors:
        out["errors"] = errors[:3]
    #: 全都没扫成 ⇒ 沿用旧口径的 `state`,让 `_fmt` 打出"跳过(为什么)"
    if not per and errors:
        out["state"] = "scan_failed"
        out["reason"] = ";".join(errors[:2])
    return out


def clean_xunlei(db: Session, user_id: int, settings, *, dry_run: bool, left: int) -> dict:
    """迅雷:**过期转存**清理(判据 = N 天内没人再提到它)。返回本轮统计。

    ★ 2026-10-20:按**账号池逐个扫各自的落地目录** —— 新加的 `xunlei#3/#4`
    的 `parent_id` 与默认号不同,不能拿一个 id 去列别人的盘。
    """
    from app.services import pan_accounts as _A
    from app.services import xunlei_cleanup

    days = int(getattr(settings, "pan_auto_clean_xunlei_days", 3) or 3)
    try:
        refs = _A.accounts(db, "xunlei", user_id=user_id)
    except Exception:  # noqa: BLE001 - 池读不到就退回默认账号,不挡清理
        refs = []
    keys = [r.cookie_key for r in refs if getattr(r, "cookie_key", "")] or [""]
    planned = deleted = unmatched = blocked = kept_shared = 0
    errors: list[str] = []
    for key in keys:
        try:
            # `run_cleanup` 自带 `xunlei_cleanup_max_per_run` 限流与 `dry_run`,不重写
            res = xunlei_cleanup.run_cleanup(
                db, user_id, days=days, settings=settings, dry_run=dry_run,
                cookie_key=key)
        except Exception as exc:  # noqa: BLE001 - 单账号失败不影响其余
            errors.append(f"{key or 'xunlei'}:{type(exc).__name__}")
            continue
        if res.get("error"):
            errors.append(f"{key or 'xunlei'}:{str(res['error'])[:60]}")
            continue
        planned += int(res.get("to_delete") or 0)
        deleted += int(res.get("deleted") or 0)
        unmatched += int(res.get("unmatched") or 0)
        blocked += int(res.get("skipped_by_limit") or 0)
        kept_shared += int(res.get("kept_shared") or 0)
        errors.extend(f"{key or 'xunlei'}:{e}" for e in (res.get("errors") or [])[:2])
    return {"pan": "迅雷", "state": "ok", "days": days, "accounts": len(keys),
            "planned": planned, "deleted": deleted, "blocked_by_limit": blocked,
            "unmatched": unmatched, "kept_shared": kept_shared, "dry_run": bool(dry_run),
            "errors": errors[:3]}


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
    if st == "no_account":
        return f"{p['pan']}跳过(池里没有可用账号)"
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
    if p.get("accounts", 1) > 1:
        bits.append(f"账号{p['accounts']}")
    #: ★★ **每个号单独列一段**(2026-11-13)。只报汇总的话,"两个号都没删"与
    #: 「1 号扫了 19 个家但是 0 重复 / 2 号压根没扫」长得一模一样 ——
    #: 而后者正是这次要修的那个 bug(清理只扫最空的 2 号、1 个家)。
    for one in (p.get("per_account") or []):
        seg = [f"{one.get('label') or '?'} 扫{one.get('scanned', '-')}"
               f" 计划{one.get('planned', 0)}"]
        if one.get("state") != "ok":
            seg.append(f"**{one.get('state')}**(还差{one.get('remaining_homes', '?')}个家)")
        elif one.get("dry_run"):
            seg.append("dry-run")
        else:
            seg.append(f"删{one.get('deleted', 0)}")
            if one.get("pkg_deleted"):
                seg.append(f"整包删{one['pkg_deleted']}")
        bits.append("(" + " ".join(seg) + ")")
    if p.get("unmatched"):
        bits.append(f"未匹配监控源{p['unmatched']}")
    #: ★ **护住了几个已发分享的文件夹**(2026-11-14):不写出来的话,
    #: "没删"与"根本没扫到 / 保护没生效"在运行记录里长得一模一样。
    if p.get("kept_shared"):
        bits.append(f"分享保护{p['kept_shared']}")
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
