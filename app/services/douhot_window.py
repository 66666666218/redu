"""抖音关键词**多窗口**热度对比(douhot_window_snap 独立表,与单窗口 watch 互补)。

同一监控词每轮**同时**拉近1h/近1天热度 → `window_contrast` 对比标签(新起势/爆发/回落/
高位延续/冷启动),用于"同一个关键词不同时间段热度对比找趋势"。独立 tick 采集 +
手动刷新接口 + 飞书推送;不侵入现有 watch/快照链路(被 focus_alert/early_agent 复用)。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DouhotWatch, DouhotWindowSnap
from config.settings import Settings, get_settings
from app.services import douhot
from app.services.cookie_store import get_cookies
from app.services.feishu import _col_set_row, _md_safe, webhook_for
from app.services.feishu_client import FeishuClient
from app.services.tenant_base import _record_run
from app.utils import get_logger

logger = get_logger(__name__)

_COOLDOWN_HOURS = 6  # 同词对比告警冷却(防刷屏,与平台默认告警冷却一致)


def _windows(settings: Settings | None) -> list[int]:
    """对比窗口集(小时),来自 douhot._windows_of(默认近1h+近1天)。"""
    return list(douhot._windows_of(settings))


def collect_windows(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    """为用户的全部抖音监控词采集一轮多窗口热度,各窗口写入快照。

    每词按 `DOUHOT_WINDOW_WINDOWS`(默认近1h/近1天)各查一次;单词失败降级为 0 不中断
    (Cookie 失效等整体性问题仍会反映在多数词为 0 + 失败记录上)。返回 {words, snaps}。
    """
    settings = settings or get_settings()
    cookie = (get_cookies(session, user_id) or {}).get("douyin", "")
    watches = session.scalars(select(DouhotWatch).where(
        DouhotWatch.user_id == user_id, DouhotWatch.section == "douhot")).all()
    if not watches:
        return {"platform": "douhot_window", "status": "skipped", "reason": "no_watch", "words": 0, "snaps": 0}
    if not cookie:
        return {"platform": "douhot_window", "status": "skipped", "reason": "no_cookie", "words": 0, "snaps": 0}

    windows = _windows(settings)
    collect_ts = datetime.now()
    snaps = 0
    ok = 0
    for w in watches:
        try:
            heat = douhot.fetch_keyword_windows(cookie, w.keyword, w.list_type, settings, windows=windows)
        except Exception:  # noqa: BLE001 - 单词失败不中断整轮
            logger.warning("抖音多窗口采集失败 keyword=%s list_type=%s", w.keyword, w.list_type)
            continue
        if not heat:
            continue
        ok += 1
        for win, h in heat.items():
            session.add(DouhotWindowSnap(
                user_id=user_id, list_type=w.list_type, keyword=w.keyword,
                entry_title=(h.get("title") or "")[:255], window=int(win),
                score=h.get("score") or 0, rank_now=h.get("rank_now") or 0,
                trend_growth=h.get("trend_growth") or 0, captured_at=collect_ts))
            snaps += 1
    session.commit()
    _record_run(session, user_id, "douhot_window", "success",
                f"words={len(watches)} ok={ok} snaps={snaps}")
    session.commit()
    return {"platform": "douhot_window", "status": "success", "words": len(watches),
            "ok": ok, "snaps": snaps}


def query_windows(session: Session, user_id: int, list_type: str, keyword: str,
                  settings: Settings | None = None) -> dict:
    """实时查**任意**关键词的多窗口对比(不依赖监控词,一次性查询,不落库)。

    list_type 取 video/topic(有真近1h口径)效果最佳;search/word 的词近1h常为 0 →
    返回 unknown 标记。无 Cookie 或查不到数据返回 skipped/error。
    """
    settings = settings or get_settings()
    kw = (keyword or "").strip()
    if not kw:
        return {"status": "skipped", "reason": "no_keyword"}
    cookie = (get_cookies(session, user_id) or {}).get("douyin", "")
    if not cookie:
        return {"status": "skipped", "reason": "no_cookie"}
    if list_type not in ("search", "video", "topic", "word"):
        list_type = "video"
    heat = douhot.fetch_keyword_windows(cookie, kw, list_type, settings)
    if not heat:
        return {"status": "error", "keyword": kw, "list_type": list_type, "reason": "no_data"}
    windows = _windows(settings)
    small, big = windows[0], windows[-1]
    h1, h24 = heat.get(small, {}), heat.get(big, {})
    if not (h1.get("score") or h24.get("score")):
        return {"status": "success", "keyword": kw, "list_type": list_type,
                "h1": 0, "h24": 0, "ratio": 0, "label": "冷启动", "signal": "flat"}
    c = douhot.window_contrast(h1, h24, win_h1=small, win_h24=big)
    return {"status": "success", "keyword": kw, "list_type": list_type,
            "h1": c["h1"], "h24": c["h24"], "ratio": c["ratio"],
            "label": c["label"], "signal": c["signal"]}


def _latest_batch(session: Session, user_id: int) -> dict[tuple[str, str], dict[int, DouhotWindowSnap]]:
    """读取每个监控词**最近一轮**各窗口快照:{(list_type, keyword): {window: snap}}。"""
    snaps = session.scalars(select(DouhotWindowSnap).where(
        DouhotWindowSnap.user_id == user_id)).all()
    # 每词的最大 captured_at(一轮一批)
    batch: dict[tuple[str, str], dict[int, DouhotWindowSnap]] = {}
    for s in snaps:
        key = (s.list_type, s.keyword)
        cur = batch.get(key)
        if cur is None:
            batch[key] = {s.window: s}
            continue
        ex = next(iter(cur.values()))
        if s.captured_at > ex.captured_at:
            batch[key] = {s.window: s}
        else:
            cur.setdefault(s.window, s)
    return batch


def _contrast_of(snap: DouhotWindowSnap, settings: Settings | None = None) -> tuple[dict, dict]:
    """取该词对比窗口的高/近一天两档快照(缺失的档用近1天兜底),算对比标签。"""
    windows = _windows(settings)
    want = {w: None for w in windows}
    for w, s in snap.items():
        if w in want:
            want[w] = s
    # 保证有主窗口(近1天)作分母;缺省用已有任一窗口
    cur = [s for s in want.values() if s]
    h24s = want.get(max(windows), None)
    h24 = h24s or (cur[-1] if cur else None)
    h1s = want.get(min(windows), None)
    h1 = h1s or (h24 if h24 else None)
    return h1, h24


def analytics(session: Session, user_id: int, settings: Settings | None = None) -> list[dict]:
    """各监控词的多窗口对比分析:近1h/近1天 值 + 对比标签(读最近一轮快照,不实时打接口)。"""
    settings = settings or get_settings()
    batch = _latest_batch(session, user_id)
    out = []
    for (list_type, keyword), snap in batch.items():
        h1, h24 = _contrast_of(snap, settings)
        v1 = h1.score if h1 else 0
        v24 = h24.score if h24 else 0
        c = douhot.window_contrast({"score": v1}, {"score": v24})
        entry = (h24.entry_title or h1.entry_title) if (h24 or h1) else ""
        out.append({
            "list_type": list_type, "keyword": keyword, "entry_title": entry,
            "h1_score": v1, "h24_score": v24, "ratio": c["ratio"],
            "label": c["label"], "signal": c["signal"],
            "captured_at": h24.captured_at.isoformat(sep=" ", timespec="seconds")
            if h24 else None,
        })
    out.sort(key=lambda d: (d["signal"] != "burst", d["signal"] != "fall", -d["ratio"]))
    return out


def run_feishu(session: Session, user_id: int, settings: Settings | None = None) -> int:
    """把命中 📉持续/🆕新起/🔥爆发 的监控词推公众号飞书群(冷却去重),返回推送条数。"""
    from app.services.alert_service import feishu_alert_gate

    settings = settings or get_settings()
    wh = webhook_for(settings, "douhot")
    if not wh:
        return 0
    rows = analytics(session, user_id, settings)
    notable = [r for r in rows if r["signal"] in ("burst", "fall")]
    if not notable:
        return 0
    pushed = 0
    elements: list[dict] = [
        {"tag": "note", "elements": [{"tag": "plain_text",
            "content": "关键词多窗口对比 · 近1h vs 近1天 · 🔥爆发/🆕新起 → 及时跟进 · 📉回落 → 别追过时"}]},
        _col_set_row([("**关键词**", 5), ("**近1h**", 2), ("**近1天**", 2), ("**趋势**", 3)], grey=True),
    ]
    lines: list[str] = []
    for r in notable[:15]:
        title = f"douhot|{r['list_type']}|{r['keyword']}"
        if not feishu_alert_gate(session, user_id, "douhot", title, _COOLDOWN_HOURS, r["label"]):
            continue
        emoji = {"burst": "🔥" if r["label"] != "新起势" else "🆕", "fall": "📉"}.get(r["signal"], "")
        label = f"{emoji}{r['label']} ({r['ratio']}x)" if r["ratio"] else emoji + r["label"]
        kw = _md_safe(r["keyword"])[:16]
        lines.append(f"{kw}\t{r['h1_score']}\t{r['h24_score']}\t{label}")
        elements.append(_col_set_row([
            (_md_safe(r["keyword"])[:18] or "—", 5), (str(r["h1_score"]), 2),
            (str(r["h24_score"]), 2), (label, 3),
        ]))
        pushed += 1
    if not pushed:
        return 0
    try:
        FeishuClient(wh, settings.feishu_secret).send_card({
            "config": {"wide_screen_mode": True},
            "header": {"template": "orange", "title": {"tag": "plain_text",
                "content": f"📊 抖音关键词趋势对比 · {pushed} 个异动"}},
            "elements": elements,
        })
    except Exception:  # noqa: BLE001 - 推送失败不影响采集
        logger.exception("抖音多窗口对比飞书推送失败 user=%s", user_id)
    return pushed
