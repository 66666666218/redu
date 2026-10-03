"""迅雷盘里"过期转存"的清理(2026-10-03,用户口径)。

**用户策略**:"可以定期清理久远资源,如果**一个星期内没有人再发了**就可以删除了。"

**"又有人发了"怎么判**:一个资源值不值得留,看**外面还有没有人在推它**。所以对盘里的每个
转存文件夹,取它的 `last_seen` = 下面几个时刻的**最大值**:

    · 我们自己转存它的时间(文件夹的 `created_time`)
    · 最近一次有**群分享卡**提到它(`xunlei_group_shares.msg_time`,按资源名归一匹配)
    · 最近一次**抖音线索**提到它(`DouyinLead.found_at`)
    · 最近一次**发现链**提到它(`DiscoveredPanLink.found_at`)

`now - last_seen > xunlei_cleanup_days`(默认 7)→ 判定过期。

⚠️ **两个刻意的保守设计**:
  1. **默认只扫 `最全文件`**(`xunlei_transfer_parent_id`)—— 那是本流水线的落点。
     用户自己的 `我的转存/右右玩软件`(车机资源那批)是**人工收藏**,不在自动清理范围内。
  2. **默认预览、不执行**(`xunlei_cleanup_enabled=False`)。删盘是难逆操作,先让人看过清单。
     执行时走 `trash_files()`(**移入回收站**,不是永久删),留一条后悔路。

⚠️ **它解决不了"盘快满"**:实测盘里本流水线的内容只有 ~0.9TB(配额 24TB 里的大头
在别处,接口查不到)。所以这是**长期卫生**,不是腾空间的主力手段。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DiscoveredPanLink, DouyinLead, XunleiGroupShare
from app.utils import get_logger

logger = get_logger(__name__)


def _norm(text: str) -> str:
    """资源名的归并键(去发货话术/符号/空白/大小写)。

    复用 `xianyu.resource_key` —— 它本来就是"同一资源换皮重发"的归并判据。跨域借用在这个
    项目里是既有做法(`douyin_leads` 也借 `xunlei_group.is_bulk_resource`),不为一个函数再抄一份。
    """
    from app.services.xianyu import resource_key

    return resource_key(str(text or ""))


def _matches(name_key: str, other_key: str) -> bool:
    """两条归一化资源名是否指同一个东西。

    用**双向包含**(短的被长的包含即算)—— 盘里的文件夹常是「警笛模拟器」,而群里的标题是
    「手机警报器（警笛模拟器）2.0版」,严格相等匹配不到。与 `early_agent` 判共振同一套口径。
    ⚠️ 但要求短的那个**至少 4 字**,否则「软件」「资源」这种会把不相干的东西全匹配上。
    """
    if not name_key or not other_key:
        return False
    short, long_ = (name_key, other_key) if len(name_key) <= len(other_key) else (other_key, name_key)
    return len(short) >= 4 and short in long_


def _source_latest(db: Session, user_id: int) -> list[tuple[str, datetime | None]]:
    """各来源的 (归一化资源名, 时间) 列表 —— 用来算 last_seen。

    只取近 `_LOOKBACK_DAYS` 天:更早的记录对"最近一周有没有人发"没有意义,还拖慢匹配。
    """
    since = datetime.now() - timedelta(days=_LOOKBACK_DAYS)
    out: list[tuple[str, datetime | None]] = []
    for title, ts in db.execute(
            select(XunleiGroupShare.title, XunleiGroupShare.msg_time)
            .where(XunleiGroupShare.user_id == user_id, XunleiGroupShare.title != "",
                   XunleiGroupShare.msg_time >= since)).all():
        out.append((_norm(title), ts))
    for title, ts in db.execute(
            select(DouyinLead.title, DouyinLead.found_at)
            .where(DouyinLead.user_id == user_id, DouyinLead.found_at >= since)).all():
        out.append((_norm(title), ts))
    for title, ts in db.execute(
            select(DiscoveredPanLink.title, DiscoveredPanLink.found_at)
            .where(DiscoveredPanLink.user_id == user_id,
                   DiscoveredPanLink.found_at >= since)).all():
        out.append((_norm(title), ts))
    return [(k, t) for k, t in out if k]


_LOOKBACK_DAYS = 30          # 只回看 30 天:更早的来源记录对"近一周有没有人发"无意义


def _parse_time(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v)[:19])
    except (ValueError, TypeError):
        return None


def plan(db: Session, user_id: int, days: int = 7, settings=None,
         max_list: int = 2000) -> dict:
    """**只看不删**:算出哪些转存文件夹已过期,连"为什么"一起给出来。

    返回 `{"days", "folders": [{name, id, created, last_seen, days_idle, matched_by}],
    "stale": N, "keep": N, "scanned": N, "error": ""}`。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.services import xunlei_transfer as xt

    out = {"days": days, "folders": [], "stale": 0, "keep": 0, "scanned": 0, "error": ""}
    parent = str(getattr(settings, "xunlei_transfer_parent_id", "") or "")
    if not parent:
        out["error"] = "未配 xunlei_transfer_parent_id(不知道该扫哪个目录)"
        return out
    try:
        items = xt.list_files(parent, limit=max_list)
    except Exception as exc:  # noqa: BLE001 - 列目录失败要给明确原因,别当"没有过期资源"
        out["error"] = f"列目录失败:{type(exc).__name__}: {str(exc)[:120]}"
        return out
    if not items:
        # ⚠️ 空列表可能是"目录真空"也可能是"凭据失效/网络失败"(迅雷接口失败即返回 [])
        # —— 不能当成"没有过期资源"就记成功。见 doc/体检与优化方案-2026-10-03.md A 类问题。
        out["error"] = "目录返回空:可能是真为空,也可能是凭据/网络失败(接口不区分)"
        return out

    sources = _source_latest(db, user_id)
    now = datetime.now()
    for f in items:
        if f.get("kind") != "drive#folder":
            continue
        out["scanned"] += 1
        key = _norm(f.get("name"))
        created = _parse_time(f.get("created_time"))
        last_seen, matched_by = created, "转存时间"
        for skey, ts in sources:
            if ts and _matches(key, skey) and (last_seen is None or ts > last_seen):
                last_seen, matched_by = ts, "外部又有人发"
        idle = (now - last_seen).days if last_seen else None
        row = {"name": str(f.get("name"))[:80], "id": str(f.get("id")),
               "created": created.isoformat()[:16] if created else "",
               "last_seen": last_seen.isoformat()[:16] if last_seen else "",
               "days_idle": idle, "matched_by": matched_by}
        out["folders"].append(row)
        if idle is None or idle > days:
            out["stale"] += 1
        else:
            out["keep"] += 1
    out["folders"].sort(key=lambda x: -(x["days_idle"] if x["days_idle"] is not None else 99999))
    return out


def run_cleanup(db: Session, user_id: int, days: int = 7, settings=None,
                dry_run: bool = True) -> dict:
    """执行清理:**移入回收站**(不是永久删),留一条后悔路。

    `dry_run=True` 时只回清单不落手 —— 删盘是难逆操作,默认就该是预览。
    ⚠️ `trash_files` **没有批量接口**,得逐个删;所以这里按 `max_delete` 限流,
    免得一次几百个把接口打爆。
    """
    out = plan(db, user_id, days=days, settings=settings)
    if out.get("error"):
        return {**out, "deleted": 0, "dry_run": dry_run}
    stale = [f for f in out["folders"]
             if f["days_idle"] is None or f["days_idle"] > days]
    limit = int(getattr(settings, "xunlei_cleanup_max_per_run", 20) or 20)
    todo = stale[:limit]
    result = {**out, "to_delete": len(stale), "deleted": 0, "errors": [],
              "dry_run": dry_run, "skipped_by_limit": max(0, len(stale) - len(todo))}
    if dry_run or not todo:
        return result
    from app.services import xunlei_transfer as xt

    for f in todo:
        try:
            r = xt.trash_files([f["id"]])
        except Exception as exc:  # noqa: BLE001 - 单个失败不该中断整批
            result["errors"].append(f"{f['name']}: {type(exc).__name__}")
            continue
        if (r or {}).get("status") == "ok" or (r or {}).get("deleted"):
            result["deleted"] += 1
        else:
            result["errors"].append(f"{f['name']}: {str((r or {}).get('message'))[:60]}")
    logger.info("迅雷盘清理:候选 %d,本轮移入回收站 %d(失败 %d)",
                len(stale), result["deleted"], len(result["errors"]))
    return result


def cleanup_tick(settings=None) -> int:
    """定时:清理过期的转存文件夹(**默认关**,`xunlei_cleanup_enabled` 开了才跑)。

    ⚠️ 没开就**直接返回 0、什么都不做** —— 不记 `_record_run`,免得运行记录里出现一批
    "success 但什么也没干"的空轮(那种记录会让真正的问题更难查)。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    if not getattr(settings, "xunlei_cleanup_enabled", False):
        return 0
    from app.db import get_session_local
    from app.db.models import User

    days = int(getattr(settings, "xunlei_cleanup_days", 7) or 7)
    db = get_session_local()()
    total = 0
    try:
        from app.services.tenant_base import _record_run

        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                out = run_cleanup(db, uid, days=days, settings=settings, dry_run=False)
                total += int(out.get("deleted") or 0)
                note = (f"候选{out.get('to_delete', 0)} 移入回收站{out.get('deleted', 0)}"
                        + (f" 超限{out['skipped_by_limit']}" if out.get("skipped_by_limit") else "")
                        + (f" 错误{len(out['errors'])}" if out.get("errors") else ""))
                if out.get("error"):
                    note = f"跳过:{out['error'][:120]}"
                _record_run(db, uid, "xunlei_cleanup",
                            "failed" if out.get("error") else "success", note)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("迅雷盘清理失败 user=%s", uid)
                _record_run(db, uid, "xunlei_cleanup", "failed", str(exc)[:200])
                db.commit()
    finally:
        db.close()
    return total

