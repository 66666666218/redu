"""管理后台服务(工作台/用户/日志/系统设置/导出)。"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AdminLog,
    AlertRecord,
    AlertRule,
    DouhotWatch,
    DouhotWatchSnap,
    DouhotWord,
    LoginLog,
    RunRecord,
    SystemConfig,
    User,
    WeiboHotItem,
    XianyuDaily,
    XianyuItem,
)


def dashboard(db: Session) -> dict:
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    counts = {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "enabled_users": db.scalar(select(func.count(User.id)).where(User.enabled.is_(True))) or 0,
        "admins": db.scalar(select(func.count(User.id)).where(User.role.in_(["admin", "operator"]))) or 0,
        "runs": db.scalar(select(func.count(RunRecord.id))) or 0,
        "alerts": db.scalar(select(func.count(AlertRecord.id))) or 0,
        "weibo_items": db.scalar(select(func.count(WeiboHotItem.id))) or 0,
        "xianyu_items": db.scalar(select(func.count(XianyuItem.id))) or 0,
        "douhot_words": db.scalar(select(func.count(DouhotWord.id))) or 0,
    }
    # 每日运行/告警(近 7 天)
    days = [(date.today() - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    runs_by_day = dict(
        db.execute(
            select(func.date(RunRecord.started_at), func.count(RunRecord.id))
            .where(RunRecord.started_at >= date.today() - timedelta(days=7))
            .group_by(func.date(RunRecord.started_at))
        ).all()
    )
    alerts_by_day = dict(
        db.execute(
            select(func.date(AlertRecord.triggered_at), func.count(AlertRecord.id))
            .where(AlertRecord.triggered_at >= date.today() - timedelta(days=7))
            .group_by(func.date(AlertRecord.triggered_at))
        ).all()
    )
    today_runs = db.scalar(select(func.count(RunRecord.id)).where(func.date(RunRecord.started_at) == today)) or 0
    # 近 30 天趋势
    days30 = [(date.today() - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    runs30 = dict(
        db.execute(
            select(func.date(RunRecord.started_at), func.count(RunRecord.id))
            .where(RunRecord.started_at >= date.today() - timedelta(days=30))
            .group_by(func.date(RunRecord.started_at))
        ).all()
    )
    alerts30 = dict(
        db.execute(
            select(func.date(AlertRecord.triggered_at), func.count(AlertRecord.id))
            .where(AlertRecord.triggered_at >= date.today() - timedelta(days=30))
            .group_by(func.date(AlertRecord.triggered_at))
        ).all()
    )
    # 待办:未配置闲鱼Cookie的用户数、近 48h 失败的运行数
    from app.db.models import UserCookie

    users_with_cookie = db.scalar(select(func.count(func.distinct(UserCookie.user_id))).where(UserCookie.platform == "goofish")) or 0
    pending_users = max(counts["users"] - users_with_cookie, 0)
    failed_runs = db.scalar(
        select(func.count(RunRecord.id)).where(RunRecord.status == "failed", RunRecord.started_at >= date.today() - timedelta(days=2))
    ) or 0
    return {
        "counts": counts,
        "today_runs": today_runs,
        "pending_users": pending_users,
        "failed_runs": failed_runs,
        "breakdown": _kind_breakdown(db),
        "trend": [
            {"date": d, "runs": int(runs_by_day.get(d, 0)), "alerts": int(alerts_by_day.get(d, 0))}
            for d in days
        ],
        "trend30": [
            {"date": d, "runs": int(runs30.get(d, 0)), "alerts": int(alerts30.get(d, 0))}
            for d in days30
        ],
    }


def list_users(db: Session, q: str = "") -> list[dict]:
    stmt = select(User).order_by(User.id)
    if q:
        stmt = stmt.where(User.username.contains(q) | User.email.contains(q))
    return [
        {
            "id": u.id, "username": u.username, "email": u.email or "", "role": u.role,
            "enabled": u.enabled, "smtp": bool(u.smtp_user), "created": u.created_at.isoformat(),
        }
        for u in db.scalars(stmt).all()
    ]


def toggle_user(db: Session, user_id: int) -> dict | None:
    u = db.get(User, user_id)
    if not u:
        return None
    u.enabled = not u.enabled
    db.commit()
    return {"id": u.id, "enabled": u.enabled}


def delete_user(db: Session, user_id: int) -> bool:
    u = db.get(User, user_id)
    if not u:
        return False
    db.delete(u)
    db.commit()
    return True


def log_admin(db: Session, admin: User, action: str, target: str, detail: str = "") -> None:
    db.add(AdminLog(admin_id=admin.id, admin_name=admin.username, action=action, target=target[:255], detail=detail))
    db.commit()


def list_logins(db: Session, limit: int = 50) -> list[dict]:
    rows = db.scalars(select(LoginLog).order_by(LoginLog.id.desc()).limit(limit)).all()
    return [{"username": r.username, "ip": r.ip, "ua": r.ua[:60], "ok": r.ok, "time": r.created_at.isoformat()} for r in rows]


def list_admin_logs(db: Session, limit: int = 50) -> list[dict]:
    rows = db.scalars(select(AdminLog).order_by(AdminLog.id.desc()).limit(limit)).all()
    return [{"admin": r.admin_name, "action": r.action, "target": r.target, "time": r.created_at.isoformat()} for r in rows]


def config_get(db: Session) -> list[dict]:
    rows = db.scalars(select(SystemConfig).order_by(SystemConfig.key)).all()
    return [{"key": r.key, "value": r.value} for r in rows]


def config_set(db: Session, key: str, value: str) -> None:
    cfg = db.get(SystemConfig, key)
    if cfg is None:
        cfg = SystemConfig(key=key, value=value)
        db.add(cfg)
    else:
        cfg.value = value
    db.commit()


def export_users(db: Session) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "username", "email", "role", "enabled", "created"])
    for u in db.scalars(select(User).order_by(User.id)).all():
        w.writerow([u.id, u.username, u.email or "", u.role, u.enabled, u.created_at.isoformat()])
    return buf.getvalue()


def export_alerts(db: Session) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "keyword", "reason", "time"])
    for r in db.scalars(select(AlertRecord).order_by(AlertRecord.id.desc())).all():
        w.writerow([r.user_id, r.keyword, r.reason, r.triggered_at.isoformat()])
    return buf.getvalue()


def user_detail(db: Session, user_id: int) -> dict | None:
    """用户详情:基本信息 + Cookie(掩码) + 预警规则 + 最近告警。"""
    from app.services.cookie_store import list_cookies

    u = db.get(User, user_id)
    if not u:
        return None
    rules = [
        {
            "section": r.section, "rule_type": r.rule_type, "metric": r.metric,
            "threshold": r.threshold, "keyword": r.keyword, "alert_time": r.alert_time, "enabled": r.enabled,
        }
        for r in db.scalars(select(AlertRule).where(AlertRule.user_id == user_id)).all()
    ]
    alerts = [
        {"keyword": a.keyword, "reason": a.reason, "time": a.triggered_at.isoformat()}
        for a in db.scalars(select(AlertRecord).where(AlertRecord.user_id == user_id).order_by(AlertRecord.id.desc()).limit(20)).all()
    ]
    return {
        "user": {
            "id": u.id, "username": u.username, "email": u.email or "", "role": u.role,
            "enabled": u.enabled, "smtp": bool(u.smtp_user), "created": u.created_at.isoformat(),
        },
        "cookies": list_cookies(db, user_id),
        "rules": rules,
        "alerts": alerts,
    }


def import_users(db: Session, text: str) -> dict:
    """批量导入用户,格式:email,password[,role](每行一个)。"""
    from app.auth import register_user

    created = skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("email,", "id,")):
            continue
        parts = line.replace(";", ",").split(",")
        email = parts[0].strip()
        pwd = parts[1].strip() if len(parts) > 1 else "123456"
        role = parts[2].strip() if len(parts) > 2 else None
        if "@" not in email:
            skipped += 1
            continue
        try:
            u = register_user(db, email, pwd, role)
            u.role = role or u.role
            db.commit()
            created += 1
        except Exception:  # noqa: BLE001
            skipped += 1
    return {"created": created, "skipped": skipped}


_DATA_FIELDS = {
    "weibo": ["user_id", "title", "heat", "rank", "captured_at"],
    "xianyu": ["user_id", "item_id", "title", "price", "hit_keywords", "best_rank", "created_at"],
    "douhot": ["user_id", "title", "score", "trend_delta", "query_day", "created_at"],
}


def data_browse(db: Session, section: str, user_id: int | None = None, limit: int = 50) -> list[dict]:
    """浏览某板块原始采集数据(可按用户过滤)。"""
    base = {"weibo": WeiboHotItem, "xianyu": XianyuItem, "douhot": DouhotWord}.get(section)
    if base is None:
        return []
    stmt = select(base).order_by(base.id.desc()).limit(limit)
    if user_id:
        stmt = stmt.where(base.user_id == user_id)
    fields = _DATA_FIELDS[section]
    out = []
    for r in db.scalars(stmt).all():
        d = {f: getattr(r, f).isoformat() if hasattr(getattr(r, f), "isoformat") else getattr(r, f) for f in fields}
        d["user_id"] = r.user_id
        out.append(d)
    return out


def category_dist(db: Session) -> list[dict]:
    """闲鱼类目分布(数量 + 想要数合计)。"""
    rows = db.execute(
        select(XianyuDaily.category, func.count(XianyuDaily.id), func.sum(XianyuDaily.want_count)).group_by(XianyuDaily.category)
    ).all()
    return [{"name": c or "未分类", "count": n, "want": int(w or 0)} for c, n, w in rows]


def alert_trend(db: Session, days: int = 30) -> list[dict]:
    """告警趋势:近 N 天每天各板块告警数(来自 AlertRecord,无需外部令牌)。"""
    start = date.today() - timedelta(days=days)
    rows = db.execute(
        select(func.date(AlertRecord.triggered_at), AlertRecord.section, func.count(AlertRecord.id))
        .where(AlertRecord.triggered_at >= start)
        .group_by(func.date(AlertRecord.triggered_at), AlertRecord.section)
    ).all()
    days_list = [(date.today() - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    out = {d: {"weibo": 0, "xianyu": 0, "douhot": 0} for d in days_list}
    for d, sec, n in rows:
        if d in out and sec in out[d]:
            out[d][sec] += n
    return [{"date": d, **out[d], "total": sum(out[d].values())} for d in days_list]


def category_pie(db: Session) -> dict:
    """分类饼图数据(全部来自本地库,无需外部令牌)。"""
    alerts_section = dict(db.execute(select(AlertRecord.section, func.count(AlertRecord.id)).group_by(AlertRecord.section)).all())
    watch_types = dict(db.execute(select(DouhotWatch.list_type, func.count(DouhotWatch.id)).group_by(DouhotWatch.list_type)).all())
    return {
        "alerts_section": [{"name": k or "?", "value": v} for k, v in alerts_section.items()],
        "watch_types": [{"name": k, "value": v} for k, v in watch_types.items()],
    }


# ---------------- RBAC 按钮/菜单级权限 ----------------
# 角色 → 权限点集合;"*" 通配全部。operator 可查/启停/导出,不可删/导入/改配置。
PERMS: dict[str, set[str]] = {
    "admin": {"*"},
    "operator": {
        "dashboard.view", "users.view", "users.toggle", "data.view", "data.export",
        "logs.view", "config.view",
    },
}


def perms_for(role: str) -> set[str]:
    p = PERMS.get(role, set())
    return {"*"} if "*" in p else p


def has_perm(role: str, perm: str) -> bool:
    p = perms_for(role)
    return "*" in p or perm in p


def perms_list(role: str) -> list[str]:
    p = perms_for(role)
    if "*" in p:
        return sorted(PERMS.get("operator", set()) | {"users.delete", "users.import", "config.set", "dashboard.view"})
    return sorted(p)


def _kind_breakdown(db: Session) -> dict:
    runs_by_kind = dict(db.execute(select(RunRecord.kind, func.count(RunRecord.id)).group_by(RunRecord.kind)).all())
    alerts_by_section = dict(db.execute(select(AlertRecord.section, func.count(AlertRecord.id)).group_by(AlertRecord.section)).all())
    return {
        "runs_by_kind": [{"kind": k or "?", "count": v} for k, v in runs_by_kind.items()],
        "alerts_by_section": [{"section": k or "?", "count": v} for k, v in alerts_by_section.items()],
    }


def failed_runs(db: Session, limit: int = 50) -> list[dict]:
    rows = db.scalars(select(RunRecord).where(RunRecord.status == "failed").order_by(RunRecord.id.desc()).limit(limit)).all()
    return [
        {"run_id": r.run_id, "kind": r.kind, "user_id": r.user_id,
         "detail": (r.detail or "")[:200], "time": r.started_at.isoformat(), "retry": r.retry_count}
        for r in rows
    ]


def retry_run(db: Session, run_id: str, settings=None) -> dict:
    from config.settings import get_settings
    from app.services import tenant

    settings = settings or get_settings()
    run = db.scalar(select(RunRecord).where(RunRecord.run_id == run_id))
    if not run or run.status != "failed":
        return {"ok": False, "msg": "运行不存在或非失败"}
    runner = {"weibo": tenant.run_weibo, "xianyu": tenant.run_xianyu, "douhot": tenant.run_douhot}.get(run.kind)
    if not runner:
        return {"ok": False, "msg": f"未知板块 {run.kind}"}
    try:
        runner(db, run.user_id, settings)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        run.retry_count = (run.retry_count or 0) + 1
        db.commit()
        return {"ok": False, "msg": str(exc)[:200]}


def retry_failed_runs(max_retry: int = 3) -> dict:
    """调度器自动重试:重试近 24h 失败且重试次数 < max_retry 的运行。"""
    from datetime import datetime, timedelta

    from config.settings import get_settings
    from app.db import get_session_local
    from app.services import tenant

    settings = get_settings()
    db = get_session_local()()
    runners = {"weibo": tenant.run_weibo, "xianyu": tenant.run_xianyu, "douhot": tenant.run_douhot}
    n = 0
    try:
        recent = db.scalars(
            select(RunRecord).where(
                RunRecord.status == "failed",
                RunRecord.retry_count < max_retry,
                RunRecord.started_at >= datetime.now() - timedelta(hours=24),
            ).order_by(RunRecord.id.desc()).limit(5)
        ).all()
        for run in recent:
            runner = runners.get(run.kind)
            if not runner:
                continue
            try:
                runner(db, run.user_id, settings)
                n += 1
            except Exception:  # noqa: BLE001
                run.retry_count = (run.retry_count or 0) + 1
                db.commit()
        return {"retried": n}
    finally:
        db.close()


def insights(db: Session) -> dict:
    """智能体洞察:跨用户聚合趋势预测,便于运维全局扫一眼。

    - `stats`:用户数 / 关注词数 / 爆发数 / 今日告警
    - `burst`:跨用户的"预测可能爆发"关键词(带趋势/预测/置信度)
    - `rising`:跨用户的上升期关键词(非爆发,但有预测价值)
    - `hot_words`:全站抖音内容词按热度去重 Top N
    """
    from app.services import keyword_agent
    from app.services.trend_analyzer import compute_growth

    today = date.today().isoformat()
    # 统计
    users = db.scalar(select(func.count(User.id))) or 0
    watchers = db.scalar(select(func.count(func.distinct(DouhotWatch.user_id)))) or 0
    watch_keywords = db.scalar(select(func.count(DouhotWatch.id))) or 0
    today_alerts = db.scalar(
        select(func.count(AlertRecord.id)).where(func.date(AlertRecord.triggered_at) == today)
    ) or 0

    # 威胁词:对每个用户的每个关注词跑智能体
    burst, rising, hot_words = [], [], []

    # 跨用户的所有关注词
    watch_rows = db.execute(
        select(DouhotWatch.user_id, DouhotWatch.list_type, DouhotWatch.keyword).order_by(DouhotWatch.id)
    ).all()
    for user_id, list_type, keyword in watch_rows:
        snaps = db.scalars(
            select(DouhotWatchSnap)
            .where(DouhotWatchSnap.user_id == user_id, DouhotWatchSnap.keyword == keyword)
            .order_by(DouhotWatchSnap.id.asc())
        ).all()
        values = [s.score for s in snaps]
        if len(values) < 2:
            continue
        agent = keyword_agent.analyze(keyword, values)
        row = {
            "keyword": keyword, "list_type": list_type, "user_id": user_id,
            "trend_label": agent["trend_label"], "growth": agent["growth"],
            "forecast_next": agent["forecast_next"], "confidence": agent["confidence"],
            "points": agent["points"], "burst": agent["burst"],
        }
        if agent["burst"]:
            burst.append(row)
        elif agent["trend_label"] == "上升期":
            rising.append(row)

    burst.sort(key=lambda r: (r["forecast_next"] or 0), reverse=True)
    rising.sort(key=lambda r: (r["growth"] or 0), reverse=True)

    # 全站抖音内容词(按去重标题取热度最高的)
    seen: dict[str, float] = {}
    for w in db.scalars(select(DouhotWord).order_by(DouhotWord.score.desc())).all():
        title = w.title
        if title in seen:
            continue
        seen[title] = w.score
        hot_words.append({"title": title, "score": w.score, "trend_delta": w.trend_delta})
        if len(seen) >= 20:
            break

    return {
        "stats": {
            "users": users, "watchers": watchers, "watch_keywords": watch_keywords,
            "burst": len(burst), "rising": len(rising), "today_alerts": today_alerts,
        },
        "burst": burst[:15],
        "rising": rising[:10],
        "hot_words": hot_words,
    }
