"""用户预警规则(见 doc/dev.md §5.10)。

每个板块(weibo/xianyu/douhot)都支持:
- `threshold`:某指标超过阈值即预警(growth/pct/delta/score/count);
- `new`:出现"新增"的项(关键词/商品/词)即告知;
- `fixed_time`:定时发送该板块总结(由调度器读取,见 alert_digest)。
关键词过滤 + 冷却防重复 + 邮件/AlertRecord 触达。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db import repository
from app.db.models import AlertRecord, AlertRule, RunRecord, User
from app.services.notifier import get_notifier, get_user_notifier
from app.utils import get_logger

logger = get_logger(__name__)

SECTIONS = ("weibo", "xianyu", "douhot", "baidu")
RULE_TYPES = ("threshold", "new", "fixed_time")


def _key(item: dict) -> str:
    return str(item.get("key") or item.get("item_id") or item.get("keyword") or item.get("title") or "")


def add_rule(
    session: Session,
    user_id: int,
    section: str,
    rule_type: str,
    metric: str | None = None,
    threshold: float | None = None,
    keyword: str | None = None,
    alert_time: str | None = None,
) -> AlertRule:
    if section not in SECTIONS:
        raise ValueError("未知板块")
    if rule_type not in RULE_TYPES:
        raise ValueError("未知规则类型")
    rule = AlertRule(
        user_id=user_id, section=section, rule_type=rule_type,
        metric=metric, threshold=threshold, keyword=(keyword or None),
        alert_time=(alert_time or None),
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def list_rules(session: Session, user_id: int) -> list[dict]:
    rules = session.scalars(select(AlertRule).where(AlertRule.user_id == user_id).order_by(AlertRule.id)).all()
    return [_rule_dict(r) for r in rules]


def _rule_dict(r: AlertRule) -> dict:
    return {
        "id": r.id,
        "section": r.section,
        "rule_type": r.rule_type,
        "metric": r.metric,
        "threshold": r.threshold,
        "keyword": r.keyword,
        "alert_time": r.alert_time,
        "enabled": r.enabled,
        "last_alert_at": r.last_alert_at.isoformat() if r.last_alert_at else None,
    }


def delete_rule(session: Session, user_id: int, rule_id: int) -> bool:
    rule = session.scalar(select(AlertRule).where(AlertRule.id == rule_id, AlertRule.user_id == user_id))
    if rule is None:
        return False
    session.delete(rule)
    session.commit()
    return True


def _alert_reason(section: str, item: dict) -> str:
    metric = item.get("metric")
    val = item.get("value")
    if metric and val is not None:
        return f"{section}项 {item.get('name')} {metric}={val:.2f} 超过阈值"
    return f"{section}项新增: {item.get('name')}"


def evaluate(
    session: Session,
    user_id: int,
    section: str,
    latest: list[dict],
    prev_keys: set[str],
    settings: Settings | None = None,
) -> int:
    """根据用户该板块的规则,对最新快照做"阈值/新增"判定并触达;返回触发条数。"""
    settings = settings or get_settings()
    user = session.get(User, user_id)
    notifier = get_user_notifier(user, settings) if user else get_notifier(settings)
    cooldown_hours = getattr(settings, "alert_cooldown_hours", 6)
    rules = session.scalars(
        select(AlertRule).where(
            AlertRule.user_id == user_id, AlertRule.section == section, AlertRule.enabled.is_(True)
        )
    ).all()

    triggered: list[tuple[AlertRule, dict]] = []
    now = datetime.now()
    for rule in rules:
        # 冷却:已有预警且仍在冷却期则跳过
        if rule.last_alert_at and (now - rule.last_alert_at).total_seconds() < cooldown_hours * 3600:
            continue
        for item in latest:
            key = _key(item)
            if rule.keyword and rule.keyword != key:
                continue
            if rule.rule_type == "new":
                if key and key not in prev_keys:
                    triggered.append((rule, item))
                    break
            elif rule.rule_type == "threshold":
                m = (rule.metric or "growth").lower()
                val = item.get(m)
                if val is None:
                    continue
                try:
                    if float(val) > float(rule.threshold or 0):
                        triggered.append((rule, item))
                        break
                except (TypeError, ValueError):
                    continue

    if not triggered:
        return 0

    # 同一规则下的多条合并为一条,避免刷屏
    per_rule: dict[int, list[str]] = {}
    for rule, item in triggered:
        key = _key(item)
        m = (rule.metric or "growth")
        val = item.get(m)
        reason = f"{key} {m}={float(val):.2f} 跨阈值 {rule.threshold}" if val is not None else f"新增 {key}"
        per_rule.setdefault(rule.id, []).append(reason)
        session.add(
            AlertRecord(user_id=user_id, section=section, keyword=key, reason=reason, triggered_at=now)
        )

    for rule, reasons in per_rule.items():
        subject = f"[预警] {section} · 触发 {len(reasons)} 条"
        notifier.send(subject, "\n".join(reasons))
        r = session.get(AlertRule, rule)
        if r:
            r.last_alert_at = now
    session.commit()
    logger.info("预警触发 section=%s user=%s 条数=%s", section, user_id, sum(len(v) for v in per_rule.values()))
    return sum(len(v) for v in per_rule.values())


def _build_digest(session: Session, user_id: int, section: str, settings: Settings) -> str:
    """生成某板块的定时总结文本。"""
    from app.db.models import DouhotWord, WeiboTrend, XianyuItem

    lines = [f"【{section} 定时总结】"]
    if section == "weibo":
        rows = session.scalars(
            select(WeiboTrend).where(WeiboTrend.user_id == user_id, WeiboTrend.rising.is_(True))
            .order_by(WeiboTrend.growth.desc()).limit(10)
        ).all()
        lines += [f"- {r.keyword} 增长 {(r.growth or 0)*100:.1f}%" for r in rows] or ["- 暂无上涨趋势"]
    elif section == "xianyu":
        rows = session.scalars(
            select(XianyuItem).where(XianyuItem.user_id == user_id)
            .order_by(XianyuItem.hit_keywords.desc(), XianyuItem.best_rank.asc()).limit(10)
        ).all()
        lines += [f"- {r.title[:28]} {r.price} (x{r.hit_keywords})" for r in rows] or ["- 暂无热榜"]
    else:
        rows = session.scalars(
            select(DouhotWord).where(DouhotWord.user_id == user_id)
            .order_by(DouhotWord.score.desc()).limit(10)
        ).all()
        lines += [f"- {r.title} 飙升 {(r.score or 0)/1e4:.1f}万" for r in rows] or ["- 暂无内容词"]
    return "\n".join(lines)


def run_fixed_time_digests() -> int:
    """定时派发:遍历所有 enabled 的 fixed_time 规则,当前 HH:MM 命中则发该板块总结。

    供调度器每个分钟调用;当天同一规则只发一次。返回发送条数。
    """
    from app.db import get_session_local

    settings = get_settings()
    db = get_session_local()()
    try:
        now = datetime.now()
        hhmm = now.strftime("%H:%M")
        rules = db.scalars(
            select(AlertRule).where(
                AlertRule.enabled.is_(True), AlertRule.rule_type == "fixed_time", AlertRule.alert_time == hhmm
            )
        ).all()
        sent = 0
        for rule in rules:
            if rule.last_alert_at and rule.last_alert_at.date() == now.date():
                continue  # 当天已发
            user = db.get(User, rule.user_id)
            notifier = get_user_notifier(user, settings) if user else get_notifier(settings)
            digest = _build_digest(db, rule.user_id, rule.section, settings)
            notifier.send(f"[{rule.section}] 定时总结 {hhmm}", digest)
            rule.last_alert_at = now
            sent += 1
        db.commit()
        return sent
    finally:
        db.close()


def build_weekly_summary(db: Session, user_id: int, settings: Settings) -> str:
    """生成某用户的周度洞察摘要(近 7 天):统计 + 爆发/上升词 + 预测。"""
    from datetime import datetime, timedelta

    from app.services import keyword_agent
    from app.db.models import DouhotWatchSnap

    since = datetime.now() - timedelta(days=7)
    lines = [f"📊 本周热点洞察(近 7 天)", ""]

    # 关注词智能体(近 7 天快照)
    burst, rising = [], []
    from app.db.models import DouhotWatch

    watches = db.scalars(select(DouhotWatch).where(DouhotWatch.user_id == user_id)).all()
    for w in watches:
        snaps = repository.watch_snap_series(db, user_id, w.keyword, since)
        values = [s.score for s in snaps]
        if len(values) >= 2:
            a = keyword_agent.analyze(w.keyword, values)
            (burst if a["burst"] else rising).append(a)
    if burst:
        lines.append("🔥 可能爆发:")
        for a in sorted(burst, key=lambda r: r["forecast_next"] or 0, reverse=True):
            lines.append(f"  · {a['keyword']} 环比+{(a['growth'] or 0)*100:.0f}% 预测{int(a['forecast_next'] or 0)}")
    if rising:
        lines.append("📈 上升:")
        lines.append("  " + ", ".join(a["keyword"] for a in rising[:8]))
    if not burst and not rising:
        lines.append("本周暂无爆发/上升词(需关注词并积累多轮采集)")

    # 微博/闲鱼预测
    from app.services import tenant

    pa = tenant.platform_agent(db, user_id, top_n=6)
    for label, items in (("微博", pa["weibo"]), ("闲鱼", pa["xianyu"])):
        if items:
            lines.append(f"📈 {label}热点预测:")
            for it in items[:5]:
                lines.append(f"  · {it['title'][:20]} {it['trend_label']} 环比+{(it['growth'] or 0)*100:.0f}%")
    return "\n".join(lines)


def run_weekly_summary() -> int:
    """每周邮件总结:给每个启用用户发本周洞察摘要(走其 SMTP 或全局 SMTP)。"""
    settings = get_settings()
    from app.db import get_session_local
    from app.services.notifier import NullNotifier

    db = get_session_local()()
    sent = 0
    try:
        for user in repository.list_enabled_users(db):
            if not (user.email or ""):
                continue
            notify = get_user_notifier(user, settings)
            if isinstance(notify, NullNotifier):  # 未配置 SMTP 时不外发
                continue
            body = build_weekly_summary(db, user.id, settings)
            if notify.send("本周热点洞察", body):
                sent += 1
        logger.info("周度邮件总结推送完成,发送=%s", sent)
        return sent
    finally:
        db.close()


def _last_success_days(db: Session, uid: int, kind: str) -> int | None:
    """该 (用户,平台) 距最近一次**成功**采集的天数;从未成功返回 None。用于判断"长期坏"升级。"""
    last_ok = db.scalar(select(func.max(RunRecord.started_at)).where(
        RunRecord.user_id == uid, RunRecord.kind == kind, RunRecord.status == "success"))
    if last_ok is None:
        return None
    return int((datetime.now() - last_ok).total_seconds() // 86400)


def check_collect_failures(settings: Settings | None = None, db: Session | None = None) -> int:
    """采集持续失败告警:某用户某板块近 24h 失败 >= 阈值,推送飞书并去重。

    解决"Cookie 过期/接口异常,只有日志没有主动提醒"的运维盲区。
    返回本次告警条数;未配置飞书 webhook 时跳过。`db` 供测试注入。
    """
    settings = settings or get_settings()
    if not settings.feishu_webhook:
        return 0
    from sqlalchemy import func

    from app.db import get_session_local
    from app.db.models import FeishuAlert, RunRecord
    from app.services.feishu_client import FeishuClient

    threshold = settings.fail_alert_threshold
    since = datetime.now() - timedelta(hours=24)
    own_session = db is None
    db = db or get_session_local()()
    sent = 0
    try:
        # 仅当某 (用户,板块) **最近一次运行仍失败**(当前仍断)才告警;
        # 否则中途重试成功过,不算"持续失败"(否则会误报)。
        latest = select(RunRecord.user_id, RunRecord.kind, func.max(RunRecord.id).label("mid")).group_by(
            RunRecord.user_id, RunRecord.kind
        ).subquery()
        latest_rows = db.execute(
            select(latest.c.user_id, latest.c.kind, RunRecord.status).join(RunRecord, RunRecord.id == latest.c.mid)
        ).all()
        currently_broken = {(uid, kind) for uid, kind, status in latest_rows if status == "failed"}

        rows = db.execute(
            select(RunRecord.user_id, RunRecord.kind, func.count(RunRecord.id))
            .where(RunRecord.status == "failed", RunRecord.started_at >= since)
            .group_by(RunRecord.user_id, RunRecord.kind)
        ).all()
        hits = []
        for uid, kind, cnt in rows:
            if (uid, kind) not in currently_broken or cnt < threshold:
                continue
            existing = db.scalar(
                select(FeishuAlert).where(
                    FeishuAlert.section == "collect_fail", FeishuAlert.user_id == uid, FeishuAlert.title == kind
                )
            )
            if existing and (datetime.now() - existing.alerted_at).total_seconds() < settings.feishu_alert_cooldown_hours * 3600:
                continue
            hits.append((uid, kind, cnt))
            if existing:
                existing.reason, existing.alerted_at = f"近24h失败{cnt}次", datetime.now()
            else:
                db.add(FeishuAlert(section="collect_fail", user_id=uid, title=kind, reason=f"近24h失败{cnt}次"))
        if hits:
            escalate_days = getattr(settings, "health_escalate_days", 3) or 3
            any_long = False
            msg = "⚠️ 采集持续失败(近24h)"
            for uid, kind, cnt in hits:
                # 带上最近一次失败的具体原因(如"XianyuVerify: 闲鱼人机验证(滑块),需人工处理"),
                # 让运维一眼知道该做什么(人工过滑块/换出口 IP),而非只看到"失败 N 次"。
                detail = db.scalar(
                    select(RunRecord.detail)
                    .where(RunRecord.user_id == uid, RunRecord.kind == kind, RunRecord.status == "failed")
                    .order_by(RunRecord.id.desc())
                    .limit(1)
                ) or ""
                extra = f"  {detail}" if detail else ""
                # 长期坏升级:距最近一次成功超过 N 天 → 标注,区分偶发与长期
                days = _last_success_days(db, uid, kind)
                long_txt = ""
                if days is not None and days >= escalate_days:
                    any_long = True
                    long_txt = f"  【长期】已 {days} 天未成功,建议人工处理"
                msg += f"\n  · 用户#{uid} 板块[{kind}] 失败 {cnt} 次{extra}{long_txt}"
            if any_long:
                msg = "🔴 " + msg[len("⚠️ "):]  # 有长期坏的 → 前缀升级为 🔴
            FeishuClient(settings.feishu_webhook, settings.feishu_secret).send(msg)
            db.commit()
            sent = len(hits)
            logger.info("采集失败告警推送,条数=%s", sent)
        return sent
    finally:
        if own_session:
            db.close()


def check_health_stalls(settings: Settings | None = None, db: Session | None = None) -> int:
    """采集停摆告警:已配 Cookie(在用)的平台超过 `health_stall_hours` 无新数据写入 → 推飞书。

    补 `check_collect_failures` 盲区——它只看"失败运行",漏掉**静默停摆**(后端宕机/调度停/
    被风控全挡却未记为失败,如磁盘满导致整站 502)。返回告警条数;未配 webhook 返回 0。`db` 供测试注入。
    """
    settings = settings or get_settings()
    if not settings.feishu_webhook:
        return 0
    from sqlalchemy import func

    from app.db import get_session_local
    from app.db.models import BaiduHotItem, DouhotWord, FeishuAlert, UserCookie, WeiboHotItem, XianyuItem
    from app.services.feishu_client import FeishuClient

    own_session = db is None
    db = db or get_session_local()()
    stall_hours = getattr(settings, "health_stall_hours", 24) or 24
    since = datetime.now() - timedelta(hours=stall_hours)
    labels = {"weibo": "微博", "xianyu": "闲鱼", "douhot": "抖音", "baidu": "百度"}
    data_tables = {
        "weibo": (WeiboHotItem, "captured_at"),
        "baidu": (BaiduHotItem, "captured_at"),
        "xianyu": (XianyuItem, "created_at"),
        "douhot": (DouhotWord, "created_at"),
    }
    # 在用平台 = 有启用用户配了该平台 Cookie(否则该平台本就不采集,不告警)
    # Cookie 平台名与数据平台名不同(goofish→xianyu,douyin→douhot),需映射。
    cookie_to_data = {"weibo": "weibo", "baidu": "baidu", "douyin": "douhot", "goofish": "xianyu"}
    data_to_cookie = {v: k for k, v in cookie_to_data.items()}
    raw = set(db.execute(
        select(UserCookie.platform).join(User, User.id == UserCookie.user_id).where(User.enabled.is_(True)).distinct()
    ).scalars().all())
    in_use = {cookie_to_data.get(c, c) for c in raw}
    stalled = []
    for p, (model, col) in data_tables.items():
        if p not in in_use:
            continue
        latest = db.execute(select(func.max(getattr(model, col)))).scalar()
        if latest is None or latest < since:
            # 记一个"在用该平台的首个启用用户"作为告警归属,便于去重
            uid = db.scalar(select(User.id).join(UserCookie, UserCookie.user_id == User.id).where(
                User.enabled.is_(True), UserCookie.platform == data_to_cookie[p]).limit(1))
            stalled.append((p, latest, uid))
    stalled = [s for s in stalled if s[2] is not None]
    if not stalled:
        if own_session:
            db.close()
        return 0

    cooldown = settings.feishu_alert_cooldown_hours * 3600
    now = datetime.now()
    items = []
    for p, latest, uid in stalled:
        existing = db.scalar(select(FeishuAlert).where(
            FeishuAlert.section == "health_stall", FeishuAlert.title == p, FeishuAlert.user_id == uid))
        if existing and (now - existing.alerted_at).total_seconds() < cooldown:
            continue
        items.append((p, latest))
        if existing:
            existing.reason, existing.alerted_at = f"无数据流入({stall_hours}h)", now
        else:
            db.add(FeishuAlert(user_id=uid, section="health_stall", title=p, reason=f"无数据流入({stall_hours}h)"))
    sent = 0
    if items:
        escalate_days = getattr(settings, "health_escalate_days", 3) or 3
        any_long = False
        lines = [f"⚠️ 采集停摆(> {stall_hours}h 无新数据)"]
        for p, latest in items:
            when = latest.strftime("%m-%d %H:%M") if latest else "从未进数据"
            long_txt = ""
            if latest:
                days = int((now - latest).total_seconds() // 86400)
                if days >= escalate_days:
                    any_long = True
                    long_txt = f"  【长期】已 {days} 天,建议人工排查"
            lines.append(f"  · {labels.get(p, p)}:最近数据 {when}{long_txt}")
        lines.append("可能:后端宕机/调度停止/Cookie失效/被风控全挡(闲鱼常见滑块/限流)")
        head = f"🔴 采集停摆(> {stall_hours}h 无新数据,有的已长期)" if any_long else lines[0]
        lines[0] = head
        if FeishuClient(settings.feishu_webhook, settings.feishu_secret).send("\n".join(lines)):
            db.commit()
            sent = len(items)
            logger.info("采集停摆告警推送,条数=%s", sent)
        else:
            db.rollback()  # 发送失败不记账,下次再试
    if own_session:
        db.close()
    return sent
