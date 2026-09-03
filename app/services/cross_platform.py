"""跨平台聚合:找出在 ≥2 个板块同时上升的关键词,附各板块预测(见 doc/dev.md §5.12)。

四个板块(微博/闲鱼/抖音/百度)都用 `keyword_agent` 对各自的热度序列做趋势分析,
"上升期"(环比 ≥8% 且 斜率>0)的词若出现在 ≥2 个板块,即视为**跨平台共同上升**,
推送到飞书——这通常意味着全网级热点,更值得跟进。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db import repository
from app.services import keyword_agent

SECTION_LABELS = {"weibo": "微博", "xianyu": "闲鱼", "douhot": "抖音", "baidu": "百度"}
_PLATFORM_ORDER = {"weibo": 0, "xianyu": 1, "douhot": 2, "baidu": 3}


def _platform_agents(db: Session, user_id: int, top_n: int = 60) -> dict[str, dict[str, dict]]:
    """每板块:keyword → agent(趋势/预测)。未达 2 样本的跳过。"""
    series = {
        "weibo": repository.weibo_heat_series(db, user_id),
        "baidu": repository.baidu_heat_series(db, user_id),
        "douhot": repository.douhot_score_series(db, user_id),
        "xianyu": repository.xianyu_want_series(db, user_id),
    }
    result: dict[str, dict[str, dict]] = {}
    for platform, s in series.items():
        agents: dict[str, dict] = {}
        for title, pts in list(s.items()):
            values = [v for _, v in pts]
            if len(values) < 2:
                continue
            a = keyword_agent.analyze(title, values)
            agents[title] = a
        result[platform] = agents
    return result


def rising_across(db: Session, user_id: int, min_platforms: int = 2) -> list[dict]:
    """找出在 ≥min_platforms 个板块处于"上升期"的关键词。

    返回 [{keyword, platforms:[..], forecasts:{platform: 预测值}, burst:bool}],
    按预测热度排序。
    """
    agents = _platform_agents(db, user_id)
    kw_platforms: dict[str, dict[str, dict]] = {}
    for platform, by_kw in agents.items():
        for kw, a in by_kw.items():
            if a.get("trend_label") == "上升期":
                kw_platforms.setdefault(kw, {})[platform] = a
    items = []
    for kw, plats in kw_platforms.items():
        if len(plats) >= min_platforms:
            forecasts = {p: a.get("forecast_next") for p, a in plats.items()}
            items.append({
                "keyword": kw,
                "platforms": sorted(plats, key=lambda p: _PLATFORM_ORDER.get(p, 99)),
                "forecasts": forecasts,
                "burst": any(a.get("burst") for a in plats.values()),
                "avg_forecast": sum(v for v in forecasts.values() if v is not None) / max(1, len([v for v in forecasts.values() if v is not None])),
            })
    items.sort(key=lambda x: (x["burst"], x["avg_forecast"] or 0), reverse=True)
    return items[:20]


def run_cross_platform_alert(user_id: int, settings: Settings | None = None, db: Session | None = None) -> int:
    """跨平台共同上升关键词 → 推飞书(去重复用 FeishuAlert)。返回推送条数。"""
    settings = settings or get_settings()
    if not settings.feishu_webhook:
        return 0
    from app.db import get_session_local
    from app.db.models import FeishuAlert
    from app.services.feishu_client import FeishuClient

    own_session = db is None
    db = db or get_session_local()()
    pushed = 0
    try:
        items = rising_across(db, user_id, min_platforms=2)
        hits = []
        now = datetime.now()
        for it in items:
            title = f"up:{it['keyword']}"
            existing = db.scalar(select(FeishuAlert).where(
                FeishuAlert.section == "cross_up", FeishuAlert.user_id == user_id, FeishuAlert.title == title))
            if existing and (now - existing.alerted_at).total_seconds() < settings.feishu_alert_cooldown_hours * 3600:
                continue
            hits.append(it)
            if existing:
                existing.reason, existing.alerted_at = "跨平台共同上升", now
            else:
                db.add(FeishuAlert(section="cross_up", user_id=user_id, title=title, reason="跨平台共同上升"))
        if hits:
            lines = ["🌐 跨平台共同上升(≥2板块)"]
            for it in hits[:12]:
                tag = "🔥" if it["burst"] else ""
                fc = ",".join(f"{SECTION_LABELS[p]}{int(f) if f is not None else '?'}" for p, f in it["forecasts"].items() if p in SECTION_LABELS)
                lines.append(f"  · {it['keyword']}{tag}  [{'+'.join(SECTION_LABELS.get(p, p) for p in it['platforms'])}]  预测→{fc}")
            FeishuClient(settings.feishu_webhook, settings.feishu_secret).send("\n".join(lines))
            db.commit()
            pushed = len(hits)
        return pushed
    finally:
        if own_session:
            db.close()
