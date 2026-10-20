# -*- coding: utf-8 -*-
"""按「**N 天无人重发**」做资源保留清理(2026-10-20,用户口径)。

## 规则(用户原话)

> 3 天内如果没人再发这个资源就可以删除了;夸克也适用,百度也可以;
> **但要注意是监控到的文件才适合这个规则**。

## 落点

- **只删「监控到的资源」**:盘里的条目名必须匹配到某个监控来源
  (公众号文章 / 抖音线索 / 迅雷群分享 / 公开平台发现链)。**匹配不到的一律保留** ——
  它们可能是人工收藏、或不属于本流水线。
- **上次被提到距今 > N 天** ⇒ 过期。`last_seen` 取各监控来源里**最新**的一次;
  已匹配但来源没有时间戳时,用盘上的 `updated_at` 兜底。
- **夸克/迅雷:移入回收站**;百度**默认只报告**(没有回收站、删了不可逆),
  只有显式打开 `pan_retention_baidu_delete` 才删。
- **`keep_shared`(默认 True)**:我方分享链 `first_fid` 指向的条目**不删** ——
  删了 = 发给客户的链接立刻失效。确认旧链可以失效时再关掉。
- 简介目录/简介文件不参与清理(它们是功能件,不是资源)。

## 为什么单独一个模块

`xunlei_cleanup` 是迅雷专用的(落地目录 + `trash_files`),`quark_dup` 是**重复**判据;
本条是**保留期**判据,三者不要互相复刻。匹配逻辑复用 `xunlei_cleanup` 的
`_norm` / `_matches` / `_source_latest`(**单一出处**,避免两处判据各修一半)。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)


def _parse_ts(value: Any) -> datetime | None:
    """盘商时间戳:秒/毫秒都认。"""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n > 10_000_000_000:      # 毫秒
        n //= 1000
    try:
        return datetime.fromtimestamp(n)
    except (OSError, ValueError):
        return None


_WEAK_NAME_PATTERNS = ("测试", "限流实验", "screenshot", "截图", "未命名", "副本", "复制")


def _is_weak(name: str) -> bool:
    """名字像**测试/截图/副本**的条目 ⇒ 只报告、不自动删。

    ⚠️ 2026-10-20 干跑抓到:百度批次里混着 `测试组2_2026...`、夸克里有一条
    `Screenshot_20261012_...jpg` —— 它们不是资源本体。按"监控到"匹配也许能命中,
    但把测试残留/截图当资源删掉没有收益,风险却不小。
    """
    low = str(name or "").lower()
    return any(p in low for p in _WEAK_NAME_PATTERNS)


def _match(name: str, sources: list[tuple[str, datetime]]) -> tuple[bool, datetime | None]:
    """条目名 → (是否监控到, 最近一次被提到的时间)。"""
    from app.services import xunlei_cleanup as xc

    key = xc._norm(name)
    last: datetime | None = None
    for skey, ts in sources:
        if ts and xc._matches(key, skey) and (last is None or ts > last):
            last = ts
    return (last is not None), last


def _row(pan: str, name: str, fid_or_path: str, *, sources, now: datetime,
         days: int, updated: datetime | None, protected: bool) -> dict:
    monitored, last = _match(name, sources)
    weak = _is_weak(name)
    if monitored and last is None:
        last = updated
    idle = (now - last).days if last else None
    stale = bool(monitored and not protected and not weak
                 and idle is not None and idle > days)
    return {"pan": pan, "name": name[:100], "fid": str(fid_or_path),
            "last_seen": last.isoformat()[:16] if last else "",
            "days_idle": idle, "monitored": monitored,
            "protected": bool(protected or weak), "weak": weak,
            "stale": stale}


def _sources(db: Session, user_id: int) -> list[tuple[str, datetime]]:
    from app.services import xunlei_cleanup as xc

    return xc._source_latest(db, user_id)


# ---------------------------------------------------------------- 夸克
def plan_quark(db: Session, user_id: int, settings=None, *, days: int = 3,
               keep_shared: bool = True) -> dict:
    """列出夸克里**监控到的**资源,标出「超过 N 天没人重发」的。**只读。**"""
    from app.services import pan_accounts as _A
    from app.services import quark_dup
    from app.services.quark_transfer import QuarkTransfer

    ck = (_A.cookie_for(db, "quark", user_id=user_id) or "") if db is not None else ""
    if not ck:
        return {"pan": "夸克", "error": "no_cookie", "rows": [], "stale": 0}
    try:
        qt = QuarkTransfer(ck, fid_store=getattr(settings, "quark_fid_store", "") or None)
        roots: list[tuple[str, str]] = []
        for h in quark_dup.list_homes(qt, "redian监听"):
            fid = str(h.get("fid") or "")
            if fid:
                roots.append((str(h.get("file_name") or ""), fid))
        #: 公开发现/选题自动转存的落点(若存在)。缺失不是错误。
        for path in ("/来自发现", "/来自选题"):
            try:
                fid = qt.resolve_named_dir(path)
            except Exception:  # noqa: BLE001
                fid = ""
            if fid:
                roots.append((path, fid))
        seen_root: set[str] = set()
        roots = [(n, f) for n, f in roots if not (f in seen_root or seen_root.add(f))]
        sources = _sources(db, user_id)
        now = datetime.now()
        prot_fids: set[str] = set()
        prot_names: set[str] = set()
        if keep_shared:
            try:
                prot_fids, prot_names, _n, _d = quark_dup.refresh_protect(
                    qt, settings=settings)
            except Exception as exc:  # noqa: BLE001 - 保护名单取不到就保守地全保留?
                #: ⚠️ 取不到保护名单时**不能猜**:宁可本轮一个都不删。
                return {"pan": "夸克", "error": f"保护名单取不到:{type(exc).__name__}",
                        "rows": [], "stale": 0}
        rows: list[dict] = []
        errors: list[str] = []
        for root_name, fid in roots:
            try:
                items = qt.list_dir(fid)
            except Exception as exc:  # noqa: BLE001 - 单个家列不动不影响其余
                errors.append(f"{root_name}:{type(exc).__name__}")
                continue
            for it in items:
                name = str(it.get("file_name") or "")
                if not name or not it.get("fid"):
                    continue
                fid_i = str(it["fid"])
                protected = bool(keep_shared and (fid_i in prot_fids or name in prot_names))
                rows.append(_row("夸克", name, fid_i, sources=sources, now=now, days=days,
                                 updated=_parse_ts(it.get("updated_at")),
                                 protected=protected))
        return {"pan": "夸克", "rows": rows, "errors": errors,
                "stale": sum(1 for r in rows if r["stale"]),
                "unmatched": sum(1 for r in rows if not r["monitored"]),
                "protected": sum(1 for r in rows if r["protected"]),
                "roots": len(roots)}
    except Exception as exc:  # noqa: BLE001
        return {"pan": "夸克", "error": f"{type(exc).__name__}: {str(exc)[:120]}",
                "rows": [], "stale": 0}


def run_quark(db: Session, user_id: int, settings=None, *, days: int = 3,
              dry_run: bool = True, keep_shared: bool = True,
              cap: int = 50) -> dict:
    from app.services import pan_accounts as _A
    from app.services.quark_transfer import QuarkTransfer

    plan = plan_quark(db, user_id, settings, days=days, keep_shared=keep_shared)
    if plan.get("error"):
        return {**plan, "planned": 0, "deleted": 0, "dry_run": dry_run}
    todo = [r for r in plan["rows"] if r["stale"]][:max(0, cap)]
    out = {"pan": "夸克", "planned": plan["stale"], "deleted": 0,
           "unmatched": plan["unmatched"], "protected": plan["protected"],
           "dry_run": dry_run, "errors": list(plan.get("errors") or []),
           "blocked_by_cap": max(0, plan["stale"] - len(todo)),
           "items": [r["name"] for r in todo][:10]}
    if dry_run or not todo:
        return out
    ck = _A.cookie_for(db, "quark", user_id=user_id) or ""
    if not ck:
        out["errors"].append("no_cookie")
        return out
    qt = QuarkTransfer(ck, fid_store=getattr(settings, "quark_fid_store", "") or None)
    for r in todo:
        try:
            res = qt.delete_files([r["fid"]], to_recycle=True)
            if (res or {}).get("deleted") or (res or {}).get("status") == "ok":
                out["deleted"] += 1
            else:
                out["errors"].append(f"{r['name']}:{str((res or {}).get('message'))[:50]}")
        except Exception as exc:  # noqa: BLE001 - 单个失败不中断整批
            out["errors"].append(f"{r['name']}:{type(exc).__name__}")
    return out


# ---------------------------------------------------------------- 百度
def plan_baidu(db: Session, user_id: int, settings=None, *, days: int = 3,
               keep_shared: bool = True) -> dict:
    """百度:**只读**列出超期资源。删除默认关闭(见 `run_baidu`)。

    ⚠️ **2026-10-20 干跑发现**:百度落点的顶层是 `来自资源批量转存_YYYYMMDD_HHMMSS`
    **批次目录**,不是资源名。直接拿批次名去匹配监控源会**误把整批当成一个资源**
    (干跑里真的匹配到了 2 个批次目录)。⇒ 批次目录**只下钻、不删**;
    真正的资源是它里面的条目。
    """
    from app.services import baidu_dup
    from app.services import pan_accounts as _A
    from app.services.baidupan_transfer import BaiduPanClient

    ck = (_A.cookie_for(db, "baidupan", user_id=user_id) or "") if db is not None else ""
    if not ck:
        return {"pan": "百度", "error": "no_cookie", "rows": [], "stale": 0}
    root = (getattr(settings, "pan_auto_clean_baidu_root", "") or "").strip() \
        or baidu_dup.DEFAULT_ROOT
    client = BaiduPanClient(ck)
    try:
        root_items = client.list_dir(root)
    except Exception as exc:  # noqa: BLE001
        return {"pan": "百度", "error": f"列目录失败:{type(exc).__name__}: {str(exc)[:100]}",
                "rows": [], "stale": 0}
    sources = _sources(db, user_id)
    now = datetime.now()
    intro = str(getattr(settings, "pan_intro_baidu_dir", "") or "")
    rows: list[dict] = []
    errors: list[str] = []

    def _process(it: dict) -> None:
        name = str(it.get("server_filename") or "")
        path = str(it.get("path") or "")
        if not name or not path:
            return
        if intro and (path == intro or path.startswith(intro.rstrip("/") + "/")):
            return                      # 简介目录不参与
        rows.append(_row("百度", name, path, sources=sources, now=now, days=days,
                         updated=_parse_ts(it.get("server_mtime") or it.get("local_mtime")),
                         protected=False))

    for it in root_items:
        name = str(it.get("server_filename") or "")
        path = str(it.get("path") or "")
        #: 批次目录只下钻,绝不把整批当资源删。
        if it.get("isdir") and name.startswith("来自资源批量转存"):
            try:
                for child in client.list_dir(path):
                    _process(child)
            except Exception as exc:  # noqa: BLE001 - 单批列不动不影响其余
                errors.append(f"{name}:{type(exc).__name__}")
            continue
        _process(it)
    return {"pan": "百度", "rows": rows, "root": root, "errors": errors,
            "stale": sum(1 for r in rows if r["stale"]),
            "unmatched": sum(1 for r in rows if not r["monitored"]),
            "protected": 0}


def run_baidu(db: Session, user_id: int, settings=None, *, days: int = 3,
              dry_run: bool = True, keep_shared: bool = True,
              cap: int = 50, allow_delete: bool = False) -> dict:
    """百度:**默认只报告**;`allow_delete=True` 才真的删(不可逆)。"""
    plan = plan_baidu(db, user_id, settings, days=days, keep_shared=keep_shared)
    if plan.get("error"):
        return {**plan, "planned": 0, "deleted": 0, "dry_run": dry_run}
    todo = [r for r in plan["rows"] if r["stale"]][:max(0, cap)]
    out = {"pan": "百度", "planned": plan["stale"], "deleted": 0,
           "unmatched": plan["unmatched"], "protected": 0,
           "dry_run": dry_run or not allow_delete,
           "delete_allowed": bool(allow_delete),
           "blocked_by_cap": max(0, plan["stale"] - len(todo)),
           "items": [r["name"] for r in todo][:10]}
    if dry_run or not allow_delete or not todo:
        return out
    from app.services import pan_accounts as _A
    from app.services.baidupan_transfer import BaiduPanClient

    ck = _A.cookie_for(db, "baidupan", user_id=user_id) or ""
    if not ck:
        out["errors"] = ["no_cookie"]
        return out
    try:
        out["deleted"] = int(BaiduPanClient(ck).delete_paths([r["fid"] for r in todo]))
    except Exception as exc:  # noqa: BLE001
        out["errors"] = [f"{type(exc).__name__}: {str(exc)[:100]}"]
    return out


# ---------------------------------------------------------------- 作业入口
def retention_tick(settings=None) -> int:
    """计划任务入口:夸克/百度按「N 天无人重发」清理。返回实际删除条数。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User
    from app.services.tenant_base import _record_run

    settings = settings or get_settings()
    if not getattr(settings, "pan_retention_enabled", False):
        return 0
    days = int(getattr(settings, "pan_retention_days", 3) or 3)
    dry = bool(getattr(settings, "pan_retention_dry_run", True))
    cap = int(getattr(settings, "pan_retention_max_per_run", 50) or 50)
    keep_shared = bool(getattr(settings, "pan_retention_keep_shared", True))
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            parts: list[dict] = []
            if getattr(settings, "pan_retention_quark_enabled", True):
                parts.append(run_quark(db, uid, settings, days=days, dry_run=dry,
                                       keep_shared=keep_shared, cap=cap))
            if getattr(settings, "pan_retention_baidu_enabled", True):
                parts.append(run_baidu(db, uid, settings, days=days, dry_run=dry,
                                       keep_shared=keep_shared, cap=cap,
                                       allow_delete=bool(getattr(
                                           settings, "pan_retention_baidu_delete", False))))
            total += sum(int(p.get("deleted") or 0) for p in parts)
            detail = ("[dry-run] " if dry else "") + " | ".join(
                f"{p.get('pan')} 计划{p.get('planned', 0)} 删{p.get('deleted', 0)}"
                f" 未匹配{p.get('unmatched', 0)}"
                + (f" 护{p.get('protected', 0)}" if p.get("protected") else "")
                + (f" 错误{len(p.get('errors') or [])}" if p.get("errors") else "")
                + (f" 原因{p.get('error')}" if p.get("error") else "")
                + (f" 候选:{','.join((p.get('items') or [])[:5])}"
                   if p.get("items") else "")
                for p in parts)
            _record_run(db, uid, "pan_retention", "success", detail[:900])
            db.commit()
            logger.info("保留期清理(user=%s):%s", uid, detail[:400])
    finally:
        db.close()
    return total
