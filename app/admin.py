"""管理后台服务(工作台/用户/日志/系统设置/导出)。"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.db import repository
from app.utils.net import redact_proxy_creds
from app.db.models import (
    AdminLog,
    AlertRecord,
    AlertRule,
    BaiduHotItem,
    DouhotWatch,
    DouhotWatchSnap,
    DouhotWord,
    FeishuAlert,
    LoginLog,
    RunRecord,
    SystemConfig,
    User,
    UserCookie,
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


def toggle_user(db: Session, user_id: int, operator: "User | None" = None) -> dict | None:
    u = db.get(User, user_id)
    if not u:
        return None
    # 目标保护:operator(非 admin)不得禁用 admin;任何人都不能禁用自己(防误锁)
    if operator is not None:
        if u.id == operator.id:
            raise PermissionError("不能禁用自己的账号")
        if u.role == "admin" and operator.role != "admin":
            raise PermissionError("operator 无权禁用管理员账号")
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


def _csv_cell(value: object) -> str:
    """CSV 公式注入防护:以 = + - @ 或制表/回车开头的单元格前置单引号。

    用户名/邮箱/监控关键词都是用户可控,导出后管理员用 Excel/WPS 打开时,
    `=cmd|'/c calc'!A0`、`@SUM(...)`、`+...` 会被当公式/DDE 执行。
    """
    s = str(value)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def export_users(db: Session) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "username", "email", "role", "enabled", "created"])
    for u in db.scalars(select(User).order_by(User.id)).all():
        w.writerow([u.id, _csv_cell(u.username), _csv_cell(u.email or ""),
                    _csv_cell(u.role), u.enabled, u.created_at.isoformat()])
    return buf.getvalue()


def export_alerts(db: Session) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "keyword", "reason", "time"])
    for r in db.scalars(select(AlertRecord).order_by(AlertRecord.id.desc())).all():
        w.writerow([r.user_id, _csv_cell(r.keyword), _csv_cell(r.reason),
                    r.triggered_at.isoformat()])
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
    # limit 钳制:超大值一次载入全部租户原始行进 Python 内存可打挂单进程 worker,
    # 负数在 MySQL 8 报 Incorrect arguments to LIMIT(500)、SQLite 等于不限。
    limit = min(max(int(limit or 50), 1), 500)
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
    # days 钳制:days=700000 会建同规模 days_list 与 dict、响应数十 MB 撑爆内存;
    # days=10**9 则 date.today()-timedelta 直接 OverflowError → 500。
    days = min(max(int(days or 30), 1), 365)
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


def collection_health(db: Session) -> dict:
    """采集健康度:各平台最近一次采集状态 + 近24h运行/失败 + 数据写入 + 飞书推送 + Cookie 配置。

    供运维一键看"哪些平台在采、是否失败、有没有推",无需手查 MySQL。
    """
    since = datetime.now() - timedelta(hours=24)

    def last_of(kind: str) -> dict:
        row = db.execute(
            select(RunRecord).where(RunRecord.kind == kind).order_by(RunRecord.started_at.desc()).limit(1)
        ).scalars().first()
        return {
            "last_run": row.started_at.isoformat() if row else None,
            "last_status": row.status if row else None,
            "last_detail": (row.detail or "")[:200] if row else None,
            "runs_24h": db.scalar(select(func.count(RunRecord.id)).where(
                RunRecord.kind == kind, RunRecord.started_at >= since)) or 0,
            "failed_24h": db.scalar(select(func.count(RunRecord.id)).where(
                RunRecord.kind == kind, RunRecord.started_at >= since, RunRecord.status == "failed")) or 0,
        }

    # 各平台最近采集运行(含闲鱼深采)
    platforms = {k: last_of(k) for k in ("weibo", "baidu", "douhot", "xianyu", "xianyu_deep")}

    # 各平台最近一次写入数据的时间(是否有新数据进账)
    data_cols = {
        "weibo": (WeiboHotItem, "captured_at"),
        "baidu": (BaiduHotItem, "captured_at"),
        "xianyu": (XianyuItem, "created_at"),
        "douhot": (DouhotWord, "created_at"),
    }
    data_health = {}
    for p, (model, col) in data_cols.items():
        row = db.execute(select(model).order_by(getattr(model, col).desc()).limit(1)).scalars().first()
        data_health[p] = getattr(row, col).isoformat() if row else None

    # 飞书推送统计
    pushes = dict(db.execute(
        select(FeishuAlert.section, func.count(FeishuAlert.id)).group_by(FeishuAlert.section)
    ).all())
    last_alert = db.execute(select(FeishuAlert).order_by(FeishuAlert.alerted_at.desc()).limit(1)).scalars().first()

    # Cookie 配置(各平台有用户配置 Cookie 的数量)
    cookies = dict(db.execute(
        select(UserCookie.platform, func.count(func.distinct(UserCookie.user_id))).group_by(UserCookie.platform)
    ).all())

    return {
        "generated_at": datetime.now().isoformat(),
        "platforms": platforms,
        "data": data_health,
        "feishu": {
            "pushes_by_section": [{"section": s or "?", "count": c} for s, c in pushes.items()],
            "last_push": last_alert.alerted_at.isoformat() if last_alert else None,
        },
        "cookies": {k: v for k, v in cookies.items()},
    }


def failed_runs(db: Session, limit: int = 50) -> list[dict]:
    rows = repository.failed_runs(db, limit)
    return [
        {"run_id": r.run_id, "kind": r.kind, "user_id": r.user_id,
         "detail": (r.detail or "")[:200], "time": r.started_at.isoformat(), "retry": r.retry_count}
        for r in rows
    ]


def _retry_runners() -> dict:
    """kind → 重跑函数。手动 retry_run 与自动 retry_failed_runs 共用同一张表,
    避免两套映射不一致(历史上手动只有 weibo/xianyu/douhot,重试 baidu/wechat_* 直接
    报"未知板块",而自动路径反而支持)。"""
    from app.services import tenant
    from app.services.wechat_monitor import run_wechat_listen, sample_traffic as _run_wechat_traffic
    from app.services.xianyu_analytics import run_xianyu_deep

    return {
        "weibo": tenant.run_weibo,
        "baidu": tenant.run_baidu,
        "xianyu": tenant.run_xianyu,
        "xianyu_deep": run_xianyu_deep,
        "douhot": tenant.run_douhot,
        "wechat_listen": run_wechat_listen,
        "wechat_sync": run_wechat_listen,
        "wechat_traffic": _run_wechat_traffic,
    }


def retry_run(db: Session, run_id: str, settings=None) -> dict:
    from config.settings import get_settings

    settings = settings or get_settings()
    # run_id(秒级时间戳)非唯一,优先用自增 id 精确定位,避免同秒撞到其它记录
    run = None
    if str(run_id).isdigit():
        run = db.get(RunRecord, int(run_id))
    run = run or db.scalar(select(RunRecord).where(RunRecord.run_id == str(run_id)))
    if not run or run.status != "failed":
        return {"ok": False, "msg": "运行不存在或非失败"}
    runner = _retry_runners().get(run.kind)
    if not runner:
        return {"ok": False, "msg": f"未知板块 {run.kind}"}
    try:
        runner(db, run.user_id, settings)
        # 成功后必须关闭该失败记录,否则它仍是 status='failed',会被
        # retry_failed_runs 在 24h 窗口内再次自动重跑一遍(重复采集)。
        run.status = "recovered"
        run.detail = f"{run.detail} → manual_retry_ok"
        db.commit()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        run.retry_count = (run.retry_count or 0) + 1
        db.commit()
        # 代理链(xianyu/weibo)异常文本内嵌 http://user:pass@host,msg 会回给
        # 管理端并经 log_admin 落 AdminLog——源头遮蔽(同 collect HTTP 面)
        return {"ok": False, "msg": redact_proxy_creds(str(exc))[:200]}


def retry_failed_runs(max_retry: int = 3) -> dict:
    """调度器自动重试:重试近 24h 失败且重试次数 < max_retry 的运行。"""
    from datetime import datetime, timedelta

    from config.settings import get_settings
    from app.db import get_session_local

    settings = get_settings()
    db = get_session_local()()
    runners = _retry_runners()
    n = 0
    try:
        # 保底不永久放弃:retry_count 越大要求等待越久(指数退避:2^count 小时),
        # 但只要"距上次失败已等够"就再次重试——网络/Cookie 恢复后自动续上。
        # 注意:重试成功后必须关闭旧 failed 记录(recovered),否则 24h 窗口内
        # 每 30 分钟都会对同一次失败重复采集(2026-09-14 审计:单次抖动放大 ~48 次)。
        #
        # 每个 (user_id, kind) 只取**最新一条** failed 作重试候选:run_* 内部在重试
        # 失败时会 _record_run 落一条 retry_count=0 的新 failed 子记录,若按全局
        # `id desc limit(20)` 取,不断重生的最新子记录会长期霸占 20 行窗口,把其它
        # 租户更早的失败挤出重试队列(单用户抖动 → 饿死全部租户的失败恢复)。
        # 用相关子查询锁定"同 user+kind 里 id 最大"的那条,天然去重、且不再被行数截断。
        inner = aliased(RunRecord)
        latest_id = (
            select(func.max(inner.id))
            .where(
                inner.user_id == RunRecord.user_id,
                inner.kind == RunRecord.kind,
                inner.status == "failed",
            )
            .correlate(RunRecord)
            .scalar_subquery()
        )
        recent = db.scalars(
            select(RunRecord).where(
                RunRecord.status == "failed",
                RunRecord.started_at >= datetime.now() - timedelta(hours=24),
                RunRecord.id == latest_id,
                # 排除被管理员禁用用户的失败记录:封禁即停推。run_* → alert_service.evaluate
                # 按用户启用的 AlertRule 发实时提醒,并可能 notify_incident 推飞书;若不剔除,
                # 用户被禁用前遗留的 failed 记录会在 24h 窗口内被自动重试而复活其推送链路
                # (常规定时采集已靠 due_schedules 的 notin_ 过滤停推,重试路径需保持一致)。
                # 用 notin_(禁用 id) 而非内联 JOIN:无对应 User 行的孤儿记录维持旧行为。
                RunRecord.user_id.notin_(select(User.id).where(User.enabled.is_(False))),
            ).order_by(RunRecord.id.desc()).limit(50)
        ).all()
        # 过滤:一律要求"距该次失败已过 2^retry_count 小时"的指数退避,
        # retry_count 达上限的不再重试(防同次失败被无限重放)
        eligible = []
        for run in recent:
            if (run.retry_count or 0) >= max_retry:
                continue
            wait_h = min(2 ** min(run.retry_count or 0, 6), 24)  # 1h/2h/4h...上限24h
            if datetime.now() - run.started_at >= timedelta(hours=wait_h):
                eligible.append(run)
        recent = eligible
        for run in recent:
            runner = runners.get(run.kind)
            if not runner:
                continue
            try:
                runner(db, run.user_id, settings)
                # 采集成功:关闭旧失败记录,阻止同一失败被反复重试
                run.status = "recovered"
                run.detail = f"{run.detail} → retry_ok"
                db.commit()
                n += 1
            except Exception:  # noqa: BLE001
                db.rollback()
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
        snaps = repository.watch_snap_series(db, user_id, keyword, list_type=list_type)
        values = [s.score for s in snaps]
        if len(values) < 2:
            continue
        agent = keyword_agent.analyze(keyword, values)
        hist = keyword_agent.history(values, [s.captured_at for s in snaps])
        row = {
            "keyword": keyword, "list_type": list_type, "user_id": user_id,
            "trend_label": agent["trend_label"], "growth": agent["growth"],
            "forecast_next": agent["forecast_next"], "confidence": agent["confidence"],
            "points": agent["points"], "burst": agent["burst"],
            # 爆点历史回溯
            "first_rise": hist.get("first_rise"), "peak_value": hist.get("peak_value"),
            "peak_at": hist.get("peak_at"), "current": hist.get("current"),
            "duration_hours": hist.get("duration_hours"),
        }
        if agent["burst"]:
            burst.append(row)
        elif agent["trend_label"] == "上升期":
            rising.append(row)

    burst.sort(key=lambda r: (r["forecast_next"] or 0), reverse=True)
    rising.sort(key=lambda r: (r["growth"] or 0), reverse=True)

    # 跨用户聚合 微博/闲鱼 的智能体预测(与抖音同一套逻辑)
    from app.services import tenant as tenant_svc

    weibo_merged: dict[str, dict] = {}
    xy_merged: dict[str, dict] = {}
    for uid in db.scalars(select(User.id).where(User.enabled.is_(True))).all():
        pa = tenant_svc.platform_agent(db, uid, top_n=5)
        for key, bucket in (("weibo", weibo_merged), ("xianyu", xy_merged)):
            for item in pa[key]:
                prev = bucket.get(item["title"])
                if prev is None or (item.get("forecast_next") or 0) > (prev.get("forecast_next") or 0):
                    bucket[item["title"]] = item
    weibo_pred = sorted(weibo_merged.values(), key=lambda x: (x["burst"], x["forecast_next"] or 0), reverse=True)[:10]
    xy_pred = sorted(xy_merged.values(), key=lambda x: (x["burst"], x["forecast_next"] or 0), reverse=True)[:10]

    # 全站抖音内容词(按去重标题取热度最高的):数据库层 GROUP BY,
    # 不再全表实例化(30 天旧库 ~86 万行,原写法打开洞察页即拖垮)
    for title, score, delta in db.execute(
        select(DouhotWord.title, func.max(DouhotWord.score), func.max(DouhotWord.trend_delta))
        .group_by(DouhotWord.title)
        .order_by(func.max(DouhotWord.score).desc())
        .limit(20)
    ).all():
        hot_words.append({"title": title, "score": float(score or 0), "trend_delta": float(delta or 0)})

    return {
        "stats": {
            "users": users, "watchers": watchers, "watch_keywords": watch_keywords,
            "burst": len(burst), "rising": len(rising), "today_alerts": today_alerts,
        },
        "burst": burst[:15],
        "rising": rising[:10],
        "hot_words": hot_words,
        "weibo": weibo_pred,
        "xianyu": xy_pred,
    }
