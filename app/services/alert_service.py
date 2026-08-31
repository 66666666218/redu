"""用户预警规则(见 doc/dev.md §5.10)。

每个板块(weibo/xianyu/douhot)都支持:
- `threshold`:某指标超过阈值即预警(growth/pct/delta/score/count);
- `new`:出现"新增"的项(关键词/商品/词)即告知;
- `fixed_time`:定时发送该板块总结(由调度器读取,见 alert_digest)。
关键词过滤 + 冷却防重复 + 邮件/AlertRecord 触达。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import AlertRecord, AlertRule
from app.services.notifier import get_notifier
from app.utils import get_logger

logger = get_logger(__name__)

SECTIONS = ("weibo", "xianyu", "douhot")
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
    notifier = get_notifier(settings)
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
            AlertRecord(user_id=user_id, keyword=key, reason=reason, triggered_at=now)
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
