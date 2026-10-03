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

_LABELS = {"weibo": "微博", "xianyu": "闲鱼", "douhot": "抖音", "baidu": "百度",
           "wechat": "公众号", "pan": "网盘资源"}
# "pan"(2026-10-02 新增):把**迅雷/公开平台那几条资源链**也纳入体检 ——
# 它们此前**一条运行记录都不写**、健康页完全看不到,静默失败没人知道。
_SECTIONS = ("weibo", "xianyu", "douhot", "baidu", "wechat", "pan")
# wechat 板块的运行记录按细粒度 kind 落库(wechat_listen/wechat_sync),
# 而 section 名是 "wechat"——直接用 section 查恒不命中,健康度里公众号恒显示"从未运行"。
# 板块归哪一侧跑(见 settings.scheduler_role):分体部署时**本实例不跑的不该报"停滞"**
_SECTION_ROLE = {"weibo": "hotspot", "douhot": "hotspot", "baidu": "hotspot",
                 "xianyu": "wechat", "wechat": "wechat", "pan": "wechat"}
_KINDS = {"wechat": ("wechat_listen", "wechat_sync", "wechat"),
          "pan": ("xunlei_group", "xunlei_sync", "douyin_leads",
                  "pan_discovery", "resource_presence")}


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
    elif section == "pan":
        # 三条资源链**任一**有新数据就算新鲜(群转存 / 扫盘 / 公开平台发现)。
        # ⚠️ 必须**先滤掉 None 再 max** —— 空表返回 None,直接 max 会 None>None 报 TypeError
        from app.db.models import DiscoveredPanLink, XunleiGroupShare, XunleiResource

        stamps = [
            db.scalar(select(func.max(XunleiGroupShare.synced_at)).where(
                XunleiGroupShare.user_id == user_id)),
            db.scalar(select(func.max(XunleiResource.synced_at)).where(
                XunleiResource.user_id == user_id)),
            db.scalar(select(func.max(DiscoveredPanLink.found_at)).where(
                DiscoveredPanLink.user_id == user_id)),
        ]
        ts = max([t for t in stamps if t]) if any(stamps) else None
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
        if section == "pan":
            # 资源本来就不是每小时都有:按 24h 当基准(停滞阈值 max(24*1.5,2)=36h)
            interval_h = 24.0

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
        _ck_plat = {"wechat": "weread", "pan": "xunlei"}.get(section, "weibo")
        cookie_ready = bool((get_cookie(db, user_id, _ck_plat) or "").strip())
        if section == "wechat":  # 公众号:书架号也算数据源在位
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

        list_sources = ""
        if section == "wechat":
            # 列表源链状态(2026-10-01 抗停维升级):运维一眼看到源链在位情况与降级位
            from app.services.wemp_cred import exists as _wemp_cred_exists

            g = getattr
            srcs = []
            if g(settings, "wechat_werss_url", "") and g(settings, "wechat_werss_ak", ""):
                srcs.append("WeRSS")
            if _wemp_cred_exists(db, user_id):
                srcs.append("自研Wemp")
            if g(settings, "wechat_reader_platform_url", "") and g(settings, "wechat_reader_token", ""):
                srcs.append("读书平台")
            list_sources = " → ".join(srcs) if srcs else "无(仅微信读书cover兜底)"

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

        # ⚠️ **分体部署的现实**(2026-10-02 修):作业本来就按角色过滤(本机 wechat 角色
        # **不跑**微博/抖音/百度),可健康页却在报它们"数据停滞 80 小时" —— 天天假警报。
        # 本实例不跑的板块一律标 N/A 且**不计问题**。
        # 用**传进来的 settings**(而不是全局),测试里换替身就能验;语义与 scheduler._role_allows 一致
        _role = (getattr(settings, "scheduler_role", "all") or "all").strip().lower()
        _want = _SECTION_ROLE.get(section, "both")
        if _role not in ("all", "both") and _role != _want:
            health, emoji = "N/A", "⚪"
            problems = [f"本实例不跑该板块(角色={getattr(settings, 'scheduler_role', 'all')})"]

        out.append({
            "section": section, "label": _LABELS[section],
            "health": health, "emoji": emoji,
            "problems": problems,
            "last_success_at": last_ok.isoformat(sep=" ", timespec="seconds") if last_ok else None,
            "last_success_age_h": round(ok_age_h, 1) if ok_age_h is not None else None,
            "fails_24h": int(fails_24h),
            "last_fail_detail": last_fail[:120],
            **({"list_sources": list_sources} if list_sources else {}),
            "data_age_h": round(data_age_h, 1) if data_age_h is not None else None,
            "interval_h": round(interval_h, 1),
            "cookie_ready": cookie_ready,
        })
    return out


def peer_status(settings=None, timeout: float = 6.0) -> dict:
    """探**对端实例**是否在线 —— 只为回答"远端那几条链路还活着吗"。

    ⚠️ **为什么需要它**(2026-10-03):本机(wechat 侧)与远程(hotspot 侧)**数据库各自独立**
    (见 `doc/operations.md`),本机看不到远程的 runs —— 微博/抖音/百度热榜归远程跑,
    所以本机只知道它们"数据停在某天",**分不清是远端整机挂了、还是那些源本身没更新**。
    这里用对端**已有的公开 `/healthz`** 做一次轻探,把"对端失联"与"源没数据"分开。

    **不新增任何暴露面**:`/healthz` 本来就是对端给探活用的公开端点,我们只是从另一侧去读它。
    未配 `peer_health_url` 时返回 `{"configured": False}`(不探、不报错)。
    """
    settings = settings or get_settings()
    base = str(getattr(settings, "peer_health_url", "") or "").strip().rstrip("/")
    if not base:
        return {"configured": False, "online": False, "url": ""}
    import time

    import requests

    t0 = time.monotonic()
    try:
        resp = requests.get(f"{base}/healthz", timeout=timeout)
        ms = round((time.monotonic() - t0) * 1000)
        data = resp.json() if resp.status_code == 200 else {}
        return {"configured": True, "online": resp.status_code == 200,
                "url": base, "latency_ms": ms,
                "version": str((data or {}).get("version") or ""),
                "time": str((data or {}).get("time") or ""),
                "error": "" if resp.status_code == 200 else f"HTTP {resp.status_code}"}
    except Exception as exc:  # noqa: BLE001 - 探测失败就是"失联",不该抛给调用方
        logger.warning("对端健康探测失败(%s):%s", base, exc)
        return {"configured": True, "online": False, "url": base,
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "version": "", "time": "", "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


def health_card(settings=None, db: Session | None = None, user_id: int = 1) -> dict:
    """**板块健康卡**(飞书 JSON):一眼看完所有采集源,给远端(hotspot 侧)每日自报用。

    设计取舍:远端每天往**管理员群**推一张,而不是让本机去轮询远端数据库 ——
    两边库是独立的,推卡片是零配置、零新增暴露面的做法;远端整机挂了这张卡就断,
    "该来没来"本身就是信号(本机侧的 `peer_status` 探活可作为第二重确认)。
    """
    settings = settings or get_settings()
    rows: list[dict] = []
    if db is not None:
        try:
            rows = source_health(db, user_id, settings)
        except Exception:  # noqa: BLE001 - 健康卡本身不该因为一个源炸掉
            logger.exception("健康卡取 source_health 失败")
    lines = [f"{r.get('emoji', '')} **{r.get('label')}** {r.get('health')}"
             # 数据龄可能是 None(该板块在本实例没有数据)—— 直接插值会打出 "Noneh"
             f" · 数据 {'—' if r.get('data_age_h') is None else str(r.get('data_age_h')) + 'h'}"
             + (f" · 24h 失败 {r['fails_24h']}" if r.get("fails_24h") else "")
             + (f"\n　　{(r.get('problems') or [''])[0]}" if r.get("problems") else "")
             for r in rows]
    bad = [r for r in rows if r.get("health") == "CIRCUIT_OPEN" or (r.get("fails_24h") or 0) >= 3]
    role = str(getattr(settings, "scheduler_role", "") or "")
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red" if bad else "green",
                   "title": {"tag": "plain_text",
                             "content": f"🩺 采集源健康 · {role or '本机'}"
                                        + (f" · {len(bad)} 个需处理" if bad else " · 全部正常")}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md",
                                             "content": "\n".join(lines) or "（无板块）"}}],
    }


def health_push_tick(settings=None) -> int:
    """定时(远端 hotspot 侧):把**板块健康卡**推到**管理员群**。返回 1=推成功,0=跳过。

    ⚠️ **只推管理员群,不回落主群**:`webhook_for(settings, "admin")` 在未配管理员群时会
    回落客户主群(旧行为)—— 采集源健康属**运维噪音**,推进客户群是事故。所以这里直接读
    `feishu_webhook_admin`,没配就安静跳过(本机侧的 `peer_status` 探活仍能看到这台机活着)。
    """
    settings = settings or get_settings()
    if not getattr(settings, "health_push_enabled", True):
        return 0
    webhook = str(getattr(settings, "feishu_webhook_admin", "") or "").strip()
    if not webhook:
        logger.info("健康卡跳过:未配管理员群 webhook(不回落主群)")
        return 0
    from app.db import get_session_local
    from app.db.models import User

    db = get_session_local()()
    try:
        uid = db.scalar(select(User.id).where(User.enabled.is_(True)).order_by(User.id))
        card = health_card(settings, db, int(uid or 1))
    finally:
        db.close()
    from app.services.feishu_client import FeishuClient

    try:
        FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(card)
        return 1
    except Exception:  # noqa: BLE001 - 推失败只记日志,别把调度打挂
        logger.exception("健康卡推送失败")
        return 0


def check_optional_containers(settings=None) -> list[str]:
    """可选容器探活(v2.8.0):WeRSS / newsnow 挂了要有人知道(此前静默)。

    两者都是列表源/热榜源的可替换实现——挂了业务降级不断(链路会退到自研/cover),
    但"静默降级"会让运营以为源还在,影响扩容决策(如热榜卡没数据)。返回异常容器名。
    """
    from config.settings import get_settings
    from curl_cffi import requests as creq

    st = settings or get_settings()
    targets = []
    if getattr(st, "wechat_werss_url", ""):
        targets.append(("WeRSS 列表源", st.wechat_werss_url.rstrip("/")))
    targets.append(("newsnow 热榜源", "http://127.0.0.1:4444"))
    down = []
    for name, base in targets:
        try:
            r = creq.get(base, impersonate="chrome", timeout=5)
            if r.status_code >= 500:
                down.append(name)
        except Exception:  # noqa: BLE001 - 连不上即视为挂
            down.append(name)
    return down
