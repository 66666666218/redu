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
    DouhotWord,
    LoginLog,
    RunRecord,
    SystemConfig,
    User,
    WeiboHotItem,
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
