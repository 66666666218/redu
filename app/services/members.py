"""付费群会员管理:按入群时间+周期生成"该收续费/该踢"名单,推飞书提醒运营者。

自动化边界(设计预留):当前只做"提醒运营者人工处理";自动私信/踢人需接
微信个人号机器人(封号风险),后续接入时把 notify 的名单转给执行器即可。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import GroupMember
from app.utils import get_logger

logger = get_logger(__name__)

GRACE_HOURS = 24  # 到期后宽限:24h 内=该私信收续费,超 24h=该踢出群聊


def _cycle_anchor(m: GroupMember) -> datetime:
    """周期锚点:续费过从最近一次续费起算,否则从入群起算。"""
    return m.last_renewed_at or m.joined_at


def member_state(m: GroupMember, now: datetime | None = None) -> dict:
    """计算单成员的到期信息:due_date / 剩余天数 / 当前应处状态(due|overdue|ok)。"""
    now = now or datetime.now()
    due = _cycle_anchor(m) + timedelta(days=m.cycle_days or 30)
    remaining = (due - now).total_seconds() / 86400
    if m.status in ("kicked", "exempt"):
        state = m.status
    elif remaining <= 0:
        state = "overdue" if remaining <= -GRACE_HOURS / 24 else "due"
    else:
        state = "ok"
    return {"due_date": due, "remaining_days": round(remaining, 1), "state": state}


def list_members(db: Session, user_id: int) -> list[dict]:
    rows = db.scalars(select(GroupMember).where(
        GroupMember.user_id == user_id).order_by(GroupMember.id)).all()
    now = datetime.now()
    out = []
    for m in rows:
        info = member_state(m, now)
        out.append({
            "id": m.id, "group_name": m.group_name, "nickname": m.nickname,
            "wechat_id": m.wechat_id,
            "joined_at": m.joined_at.isoformat(sep=" ", timespec="seconds"),
            "cycle_days": m.cycle_days,
            "last_renewed_at": m.last_renewed_at.isoformat(sep=" ", timespec="seconds")
            if m.last_renewed_at else None,
            "status": m.status, "note": m.note,
            "due_date": info["due_date"].isoformat(sep=" ", timespec="seconds"),
            "remaining_days": info["remaining_days"], "state": info["state"],
        })
    return out


def add_member(db: Session, user_id: int, nickname: str, joined_at: datetime,
               group_name: str = "", wechat_id: str = "", cycle_days: int = 30,
               note: str = "") -> GroupMember:
    m = GroupMember(user_id=user_id, nickname=(nickname or "").strip()[:128],
                    joined_at=joined_at, group_name=(group_name or "").strip()[:128],
                    wechat_id=(wechat_id or "").strip()[:128],
                    cycle_days=max(1, int(cycle_days or 30)), note=(note or "")[:255])
    db.add(m)
    db.commit()
    return m


def renew(db: Session, user_id: int, member_id: int) -> bool:
    """续费:刷新 last_renewed_at(回到新周期起点)。"""
    m = db.scalar(select(GroupMember).where(
        GroupMember.id == member_id, GroupMember.user_id == user_id))
    if m is None:
        return False
    m.last_renewed_at = datetime.now()
    m.status = "active"
    db.commit()
    return True


def set_status(db: Session, user_id: int, member_id: int, status: str) -> bool:
    if status not in ("active", "kicked", "exempt"):
        return False
    m = db.scalar(select(GroupMember).where(
        GroupMember.id == member_id, GroupMember.user_id == user_id))
    if m is None:
        return False
    m.status = status
    db.commit()
    return True


def delete_member(db: Session, user_id: int, member_id: int) -> bool:
    m = db.scalar(select(GroupMember).where(
        GroupMember.id == member_id, GroupMember.user_id == user_id))
    if m is None:
        return False
    db.delete(m)
    db.commit()
    return True


def renewal_tick(settings: Settings | None = None, db: Session | None = None) -> dict:
    """每日检查:到期成员 → 飞书推运营者"该私信收续费";超 24h → "该踢出群聊"。

    每成员每天最多提醒一次(feishu_alert_gate 冷却);返回 {due: n, overdue: n}。
    """
    settings = settings or get_settings()
    from app.services.alert_service import feishu_alert_gate
    from app.services.feishu import webhook_for
    from app.services.feishu_client import FeishuClient

    wh = webhook_for(settings, "wechat") or settings.feishu_webhook
    own_session = db is None
    if own_session:
        from app.db import get_session_local
        db = get_session_local()()
    due_list: list[GroupMember] = []
    overdue_list: list[GroupMember] = []
    try:
        now = datetime.now()
        rows = db.scalars(select(GroupMember).where(GroupMember.status == "active")).all()
        for m in rows:
            st = member_state(m, now)
            if st["state"] == "due":
                due_list.append(m)
            elif st["state"] == "overdue":
                overdue_list.append(m)
        if not (due_list or overdue_list) or not wh:
            return {"due": len(due_list), "overdue": len(overdue_list), "notified": False}

        lines: list[str] = []
        if due_list:
            lines.append("📞 今日该收续费(到期 24h 内):")
            for m in due_list:
                days_in = (now - m.joined_at).days
                lines.append(f"  · {m.nickname}"
                             + (f"({m.wechat_id})" if m.wechat_id else "")
                             + f"  群[{m.group_name or '默认'}] 入群第 {days_in} 天,已到期")
        if overdue_list:
            lines.append("❌ 超 24h 未续费,该踢出群聊:")
            for m in overdue_list:
                days_in = (now - m.joined_at).days
                lines.append(f"  · {m.nickname}"
                             + (f"({m.wechat_id})" if m.wechat_id else "")
                             + f"  群[{m.group_name or '默认'}] 入群第 {days_in} 天,超期未续")
        lines.append("(私信/踢群请手动处理;续费后在平台「会员」页点续费刷新周期)")

        # 每日一条汇总(以"有到期成员"为门,24h 冷却防重复)
        if feishu_alert_gate(db, due_list[0].user_id if due_list else overdue_list[0].user_id,
                             "member_renewal", "daily_renewal_digest", 24,
                             f"due={len(due_list)} overdue={len(overdue_list)}"):
            sent = FeishuClient(wh, settings.feishu_secret).send("\n".join(lines))
            if sent:
                db.commit()
                logger.info("会员续费提醒已推送:due=%s overdue=%s", len(due_list), len(overdue_list))
            else:
                db.rollback()
        return {"due": len(due_list), "overdue": len(overdue_list), "notified": True}
    finally:
        if own_session:
            db.close()
