"""数据源健康中心:每个采集源 HEALTHY / DEGRADED / CIRCUIT_OPEN 三态判定。

聚合四类信号(全部来自已存在的机制,不新增采集行为):
  ① 新鲜度:最近一次成功采集距今(对照该源的用户采集间隔,超 1.5 倍即降级)
  ② 失败率:近 24h 失败次数(≥3 降级)
  ③ 熔断状态:闲鱼滑块/WAF 冷却(verify_cooldown_active)、搜狗验证码熔断
  ④ Cookie 在位:未配置/解密失败 = OPEN
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import get_settings
from app.db.models import (BaiduHotItem, DouhotWord, RunRecord, UserCookie, UserSchedule,
                           WeiboHotItem, XianyuItem)
from app.services.tenant_base import verify_cooldown_active
from app.utils import get_logger

logger = get_logger(__name__)

_LABELS = {"weibo": "微博", "xianyu": "闲鱼", "douhot": "抖音", "baidu": "百度", "wechat": "公众号"}
_SECTIONS = ("weibo", "xianyu", "douhot", "baidu", "wechat")
# wechat 板块的运行记录按细粒度 kind 落库(wechat_listen/wechat_sync),
# 而 section 名是 "wechat"——直接用 section 查恒不命中,健康度里公众号恒显示"从未运行"。
_KINDS = {"wechat": ("wechat_listen", "wechat_sync", "wechat")}


def _kinds(section: str) -> tuple[str, ...]:
    return _KINDS.get(section, (section,))


def _last_data_age(db: Session, user_id: int, section: str) -> float | None:
    """最近一次数据写入距 now 的小时数;从未写入返回 None。"""
    model_ts = {
        "weibo": (WeiboHotItem, "captured_at"),
        "baidu": (BaiduHotItem, "captured_at"),
        "douhot": (DouhotWord, "created_at"),
        "xianyu": (XianyuItem, "created_at"),
    }
    if section == "wechat":
        from app.db.models import WechatArticle
        ts = db.scalar(select(func.max(WechatArticle.created_at)).where(
            WechatArticle.user_id == user_id))
    else:
        model, col = model_ts[section]
        ts = db.scalar(select(func.max(getattr(model, col))).where(model.user_id == user_id))
    if ts is None:
        return None
    return (datetime.now() - ts).total_seconds() / 3600


def source_health(db: Session, user_id: int, settings=None) -> list[dict]:
    settings = settings or get_settings()
    now = datetime.now()
    day_ago = now - timedelta(hours=24)
    out = []
    for section in _SECTIONS:
        # 用户采集间隔(判"新鲜度超标"的基准)
        interval_h = (db.scalar(select(UserSchedule.interval_minutes).where(
            UserSchedule.user_id == user_id, UserSchedule.section == section)) or 60) / 60
        if section == "wechat":
            # 监听是定点作业,用户间隔不参与调度(claim_schedule force=True);
            # 拿 interval 当基准会把夜间空档(20:00→4:00)误判成数据停滞。
            from app.services.schedule_service import wechat_listen_gap_hours
            interval_h = wechat_listen_gap_hours()

        last_ok = db.scalar(select(func.max(RunRecord.started_at)).where(
            RunRecord.user_id == user_id, RunRecord.kind.in_(_kinds(section)),
            RunRecord.status.in_(("success", "partial"))))
        fails_24h = db.scalar(select(func.count()).select_from(RunRecord).where(
            RunRecord.user_id == user_id, RunRecord.kind.in_(_kinds(section)),
            RunRecord.status == "failed", RunRecord.started_at >= day_ago)) or 0
        last_fail = db.scalar(select(RunRecord.detail).where(
            RunRecord.user_id == user_id, RunRecord.kind.in_(_kinds(section)),
            RunRecord.status == "failed").order_by(RunRecord.id.desc()).limit(1)) or ""

        from app.services.cookie_store import get_cookie
        cookie_ready = bool((get_cookie(db, user_id, "weibo" if section != "wechat" else "weread")
                             or "").strip())
        if section == "wechat":  # 公众号:书架号+dajiala 也算数据源在位
            cookie_ready = cookie_ready or bool(db.scalar(
                select(func.count()).select_from(UserCookie).where(
                    UserCookie.user_id == user_id, UserCookie.platform == "weread")))

        # 熔断状态(现有机制的直接读取)
        circuit = ""
        if section == "xianyu" and verify_cooldown_active(db, user_id, settings):
            circuit = "滑块/WAF 冷却中"
        if section == "wechat":
            recent = db.scalar(select(RunRecord.detail).where(
                RunRecord.user_id == user_id, RunRecord.kind == "wechat_listen",
                RunRecord.started_at >= day_ago, RunRecord.detail.contains("-2012")
            ).limit(1))
            if recent:
                circuit = "微信读书 Cookie 失效(续期失败)"

        data_age_h = _last_data_age(db, user_id, section)
        ok_age_h = last_ok and (now - last_ok).total_seconds() / 3600

        # 三态判定
        problems: list[str] = []
        if not cookie_ready:
            problems.append("Cookie 未配置")
        if circuit:
            problems.append(circuit)
        if fails_24h >= 3:
            problems.append(f"24h 失败 {fails_24h} 次")
        if data_age_h is None:
            problems.append("从未写入数据")
        elif data_age_h > max(interval_h * 1.5, 2):
            problems.append(f"数据停滞 {data_age_h:.1f}h")

        if not cookie_ready or (circuit and "失效" in circuit):
            health, emoji = "CIRCUIT_OPEN", "🔴"
        elif problems:
            health, emoji = "DEGRADED", "🟡"
        else:
            health, emoji = "HEALTHY", "🟢"

        out.append({
            "section": section, "label": _LABELS[section],
            "health": health, "emoji": emoji,
            "problems": problems,
            "last_success_at": last_ok.isoformat(sep=" ", timespec="seconds") if last_ok else None,
            "last_success_age_h": round(ok_age_h, 1) if ok_age_h is not None else None,
            "fails_24h": int(fails_24h),
            "last_fail_detail": last_fail[:120],
            "data_age_h": round(data_age_h, 1) if data_age_h is not None else None,
            "interval_h": round(interval_h, 1),
            "cookie_ready": cookie_ready,
        })
    return out
