# -*- coding: utf-8 -*-
"""出口 IP 变化自检(2026-10-20,用户出差/换网络)。

## 它解决什么

换网络后,平台侧最先坏的不是代码,而是 **IP 绑定**:
小红书 `300011/300012`、闲鱼滑块/`FAIL_SYS_USER_VALIDATE`、抖音 `verify_check`、
公众号 `200003`、迅雷/夸克/百度 token 或 cookie 间歇失效。
这些失败**散在**各链的 `runs` 里,人往往要等数据断了才回头翻。

⇒ 每 15 分钟记录一次公网出口 IP;变化时推管理员群:

    IP 已变化:旧 → 新
    近 N 小时失败:小红书 2 次、抖音 1 次…

变化本身也写 `runs`(**不许静默**);同一小时内的连续变化只推第一条
(酒店 Wi-Fi ↔ 手机热点来回切时,避免刷屏)。

## 纪律

- **只读**:取 IP 只是一个 GET;失败只记日志,不推人(网络本身可能正断着)。
- **不猜**:取不到 IP / 拿到的不是合法 IP ⇒ 不更新"上次 IP",也不报警。
- **不刷屏**:告警有独立冷却(`network_guard_alert_cooldown_minutes`)。
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

_LAST_IP_KEY = "network_guard_last_ip"
_LAST_OK_KEY = "network_guard_last_ok_at"
_LAST_ALERT_KEY = "network_guard_last_alert_at"


def _get_cfg(db: Session, key: str) -> str:
    from app.db.models import SystemConfig

    row = db.scalar(select(SystemConfig).where(SystemConfig.key == key))
    return str(row.value or "") if row else ""


def _set_cfg(db: Session, key: str, value: str) -> None:
    from app.db.models import SystemConfig

    row = db.scalar(select(SystemConfig).where(SystemConfig.key == key))
    if row:
        row.value = str(value)
    else:
        db.add(SystemConfig(key=key, value=str(value)))
    db.commit()


def current_ip(settings) -> str:
    """取公网出口 IP;取不到返回空串。多个 URL 依次回退。"""
    urls = [u.strip() for u in
            str(getattr(settings, "network_guard_ip_urls", "") or "").split(",")
            if u.strip()]
    for url in urls:
        try:
            r = requests.get(url, timeout=8)
            if r.status_code != 200:
                continue
            text = (r.text or "").strip()
            if not text:
                continue
            value = text.split()[0]
            ipaddress.ip_address(value)      # 不是合法 IP 就当这个源失败
            return value
        except Exception as exc:  # noqa: BLE001 - 换下一个源
            logger.debug("取出口 IP 失败 %s: %s", url, str(exc)[:80])
    return ""


def _failure_summary(db: Session, hours: int, limit: int = 8) -> list[str]:
    """近 N 小时的失败摘要(按 kind 聚合),给人一眼看出"哪条链可能被 IP 影响"。"""
    from app.db.models import RunRecord

    since = datetime.now() - timedelta(hours=max(1, int(hours or 2)))
    rows = db.execute(
        select(RunRecord.kind, RunRecord.status, func.count(),
               func.max(RunRecord.started_at))
        .where(RunRecord.started_at >= since,
               RunRecord.status.in_(("failed", "partial")))
        .group_by(RunRecord.kind, RunRecord.status)
        .order_by(func.max(RunRecord.started_at).desc())
        .limit(limit)
    ).all()
    return [f"{kind} {n} 次({status},最近 {last:%H:%M})"
            for kind, status, n, last in rows]


def check(db: Session, user_id: int = 1, settings=None) -> dict:
    """跑一次自检。返回 `{status: disabled|no_ip|init|same|changed, ...}`。"""
    from config.settings import get_settings

    settings = settings or get_settings()
    if not getattr(settings, "network_guard_enabled", True):
        return {"status": "disabled"}
    ip = current_ip(settings)
    if not ip:
        logger.warning("出口 IP 自检:取不到公网 IP(网络可能正在切换)")
        return {"status": "no_ip"}
    now = datetime.now()
    _set_cfg(db, _LAST_OK_KEY, now.isoformat(timespec="seconds"))
    old = _get_cfg(db, _LAST_IP_KEY)
    if not old:
        #: 首次运行只记录基准,不报警(否则新部署第一条就是"IP 变了")。
        _set_cfg(db, _LAST_IP_KEY, ip)
        logger.info("出口 IP 自检:初始记录 %s", ip)
        return {"status": "init", "ip": ip}
    if ip == old:
        return {"status": "same", "ip": ip}

    _set_cfg(db, _LAST_IP_KEY, ip)
    fails = _failure_summary(
        db, int(getattr(settings, "network_guard_failure_window_hours", 2) or 2))
    #: ★ 2026-10-22:把"IP 漂移"与"跨平台会话批量失效"绑在一起报。
    #: 当天查到的真因:Windows **IPv6 临时地址**(RFC 4941)会定期换随机后缀
    #: (`netsh interface ipv6 show privacy` → Use Temporary Addresses: enabled),
    #: 加随身 WiFi/热点切换 ⇒ 一天内出口变 10 次。而微博/小红书/闲鱼/抖音/微信读书
    #: 的会话**都绑出口 IP** ⇒ 同一账号在服务端看来"不停换地方登录"。
    #: ⚠️ 之前这条告警只说"IP 变了",没说要去找谁 —— 于是我们一直在单条链上
    #:    调 cookie/签名/代理,而根因在它们共同的前提上。
    changes_24h = 0
    try:
        from app.db.models import RunRecord

        changes_24h = int(db.scalar(
            select(func.count()).select_from(RunRecord).where(
                RunRecord.kind == "network_guard",
                RunRecord.started_at >= now - timedelta(hours=24),
                RunRecord.detail.like("%IP 变化%"))
        ) or 0) + 1          # 本次也算一次(它还没写进 runs)
    except Exception:  # noqa: BLE001 - 数不出来不影响告警本身
        logger.debug("统计近 24h 出口变化次数失败", exc_info=True)

    detail = (f"出口 IP 已变化:{old} → {ip}\n"
              f"时间:{now:%Y-%m-%d %H:%M}\n")
    detail += ("近时段失败:" + " | ".join(fails)) if fails else "近时段各链无失败记录。"
    detail += (f"\n近 24 小时出口变化 **{changes_24h}** 次"
               + ("(≥3 次 ⇒ **出口不稳定**)" if changes_24h >= 3 else "") + "\n")
    detail += ("⚠️ 绑出口 IP 的会话(微博/小红书/闲鱼/抖音/微信读书)可能随后批量失效。"
               "**先确认网络,别在单条链上调 cookie/签名**。常见来源:"
               "① Windows IPv6 临时地址定期轮换(`netsh interface ipv6 show privacy` "
               "看 Use Temporary Addresses);② 随身 WiFi/热点切换;③ 双栈下 IPv4/IPv6 交替。"
               "采集机应固定单一出口后再重登。")

    #: 变化本身必须留痕 —— 即使飞书冷却/发送失败,也不能只剩一行 debug。
    from app.services.tenant_base import _record_run

    _record_run(db, user_id, "network_guard", "success",
                f"IP 变化 {old} → {ip} | " + (" | ".join(fails) if fails else "无失败"))
    db.commit()

    cooldown = int(getattr(settings, "network_guard_alert_cooldown_minutes", 60) or 0)
    last_alert = _get_cfg(db, _LAST_ALERT_KEY)
    if last_alert and cooldown > 0:
        try:
            if now - datetime.fromisoformat(last_alert) < timedelta(minutes=cooldown):
                logger.info("出口 IP 变化:告警冷却中,只记 runs 不推飞书")
                return {"status": "changed", "ip": ip, "old": old, "alerted": False}
        except (TypeError, ValueError):
            pass      # 值坏了就当没冷却(宁可多推一次,也别永远不推)

    try:
        from app.services.alert_service import notify_incident

        notify_incident(db, user_id, "network", "网络出口 IP 已变化", detail,
                        settings=settings)
        _set_cfg(db, _LAST_ALERT_KEY, now.isoformat(timespec="seconds"))
    except Exception:  # noqa: BLE001 - 告警失败不影响自检本身
        logger.exception("出口 IP 变化告警推送失败")
    return {"status": "changed", "ip": ip, "old": old, "alerted": True}


def network_guard_tick(settings=None) -> int:
    """计划任务入口:跑一次自检。返回 1 = 本轮检测到 IP 变化。"""
    from config.settings import get_settings
    from app.db import get_session_local

    settings = settings or get_settings()
    db = get_session_local()()
    try:
        out = check(db, 1, settings)
        return 1 if out.get("status") == "changed" else 0
    finally:
        db.close()
