"""热点→网盘选题 Agent:把「监控词热度」与「供应商新发资源」对上,给运营者可执行的发货建议。

两层(2026-09-28 v1):
- 规则层(零成本):监控词趋势(DouhotWatchSnap,本机每 20-40 分钟一拍)涨幅 ≥ 阈值,
  且近 72h 公众号采集到标题命中该词的资源文 → 提示「热点已有人供货」,附我方/源链;
- LLM 层(可选,配了 deepseek 才跑,最多 hotspot_agent_llm_top 个热点控成本):
  没现成资源的热点让 LLM 给网盘选题建议(资源类型/标题模板/关键词)。

节流:同一热点词 24h 内只推一次(记忆落 system_config);输出按 2026-09-27 口径走
站内告警(push_feishu=False,飞书群只推文章与 Cookie 提醒)。
计划任务:hotspot_agent_cron(默认 9:10/15:10/21:10,跟在三个白天定点监听后面,数据最鲜)。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import (DouhotWatchSnap, SystemConfig, WechatArticle)
from app.services import alert_service
from app.utils import get_logger

logger = get_logger(__name__)


def _norm(text: str) -> str:
    return "".join(str(text or "").lower().split())


def _hotspots(db: Session, user_id: int, min_growth: float,
              hours: int = 24) -> list[dict]:
    """近 N 小时每个监控词的最新一拍,按涨幅过滤排序。"""
    cutoff = datetime.now() - timedelta(hours=hours)
    rows = db.execute(
        select(DouhotWatchSnap.keyword, func.max(DouhotWatchSnap.trend_growth),
               func.max(DouhotWatchSnap.rank_now), func.max(DouhotWatchSnap.score),
               func.max(DouhotWatchSnap.captured_at))
        .where(DouhotWatchSnap.user_id == user_id,
               DouhotWatchSnap.section == "douhot",
               DouhotWatchSnap.captured_at >= cutoff)
        .group_by(DouhotWatchSnap.keyword)).all()
    out = []
    for kw, growth, rank, score, _ in rows:
        if (growth or 0) >= min_growth:
            out.append({"keyword": str(kw or ""), "growth": float(growth or 0),
                        "rank": int(rank or 0), "score": float(score or 0)})
    out.sort(key=lambda h: h["growth"], reverse=True)
    return out


def _supply_articles(db: Session, user_id: int, hours: int = 72) -> list[WechatArticle]:
    cutoff = datetime.now() - timedelta(hours=hours)
    return list(db.scalars(select(WechatArticle).where(
        WechatArticle.user_id == user_id,
        WechatArticle.created_at >= cutoff,
        WechatArticle.pan_urls.isnot(None),
        WechatArticle.pan_urls != "",
    ).order_by(WechatArticle.created_at.desc()).limit(300)).all())


def _match_supply(keyword: str, articles: list[WechatArticle]) -> WechatArticle | None:
    """标题命中(归一化子串)即视为「热点已有现成资源」。"""
    n = _norm(keyword)
    if len(n) < 2:
        return None
    for a in articles:
        if n and n in _norm(a.title):
            return a
    return None


def _proven_titles(db: Session, user_id: int, limit: int = 12) -> list[str]:
    """自己验证过能带来转存的资源文标题(近期带盘链的,按盘链被转载次数排序)。"""
    from app.db.models import WechatPanLink

    rows = db.execute(
        select(WechatArticle.title, func.count(WechatPanLink.id))
        .join(WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
        .where(WechatArticle.user_id == user_id,
               WechatArticle.created_at >= datetime.now() - timedelta(days=60))
        .group_by(WechatArticle.id, WechatArticle.title)
        .order_by(func.count(WechatPanLink.id).desc())
        .limit(limit * 2)).all()
    seen: list[str] = []
    for title, _ in rows:
        t = str(title or "").strip()
        if t and t not in seen:
            seen.append(t)
        if len(seen) >= limit:
            break
    return seen


def _llm_suggest(settings: Settings, hotspots: list[dict],
                 proven: list[str]) -> dict[str, str]:
    """对没现成资源的热点,以网盘拉新为目标生成选题方案;失败/未配 key 返回 {}。"""
    if not settings.deepseek_api_key or not hotspots:
        return {}
    hs = [f"{i}. 《{h['keyword']}》热度增长 +{h['growth']:.0f}%"
          for i, h in enumerate(hotspots, 1)]
    proven_block = "\n".join(f"- {t}" for t in proven) if proven else "- (暂无历史数据)"
    try:
        resp = requests.post(
            settings.deepseek_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}",
                     "Content-Type": "application/json"},
            json={"model": settings.deepseek_model,
                  "messages": [
                      {"role": "system", "content":
                       "你是网盘拉新运营专家。商业模式:借社会热点制作/整理「配套资料」"
                       "(课件/真题/模板/安装包/壁纸/攻略),用夸克网盘分享链发布,"
                       "用户为拿资料必须转存 → 完成拉新。选题铁律:热点事件决定人群,"
                       "人群决定他们非要不可的那份资料;宁要一个急用人群,不要十个围观者。"},
                      {"role": "user", "content":
                       "rising 热点如下:\n" + "\n".join(hs)
                       + "\n\n我们自己验证过能带来转存的资源文标题(参考其选题套路与话术):\n"
                         + proven_block
                       + "\n\n对每个热点给出一套拉新方案,每套严格两行:\n"
                         "《热点》→ 资源:具体到文件内容的清单(如「近3年真题+答题模板」)\n"
                         "《热点》→ 标题:1条发布标题(带时效词/人群词) + 人群:谁非要不可 + 拉新点:为什么必须转存"}],
                  "temperature": 0.7, "max_tokens": 900},
            timeout=60)
        if resp.status_code >= 400:
            logger.warning("热点 LLM 建议失败 HTTP %s", resp.status_code)
            return {}
        text = (resp.json().get("choices", [{}])[0].get("message", {}) or {}).get("content") or ""
    except requests.RequestException as exc:
        logger.warning("热点 LLM 建议请求异常:%s", exc)
        return {}
    out: dict[str, str] = {}
    cur: str | None = None
    for line in text.splitlines():
        line = line.strip().lstrip("0123456789.、-• ")
        if "《" not in line:
            continue
        kw = line.split("《", 1)[1].split("》", 1)[0]
        if "→" in line:
            cur = kw
            out[kw] = line
        elif cur:
            out[cur] += " | " + line   # 第二行(标题/人群/拉新点)并入同一热点
    for h in hotspots:  # LLM 漏掉的热点不硬造,标个占位让列表完整
        out.setdefault(h["keyword"], f"《{h['keyword']}》→ (LLM 未给出,自行判断)")
    return out


def run_hotspot_agent(db: Session, user_id: int, settings: Settings | None = None) -> dict:
    """跑一轮热点选题建议;返回统计。站内通知,24h 内同热点不重复推。"""
    settings = settings or get_settings()
    if not getattr(settings, "hotspot_agent_enabled", True):
        return {"status": "disabled"}
    min_growth = float(getattr(settings, "hotspot_min_growth", 50) or 50)
    hotspots = _hotspots(db, user_id, min_growth)
    if not hotspots:
        return {"status": "no_hotspots"}

    mem_key = f"hotspot_agent_last_{user_id}"
    mem_row = db.get(SystemConfig, mem_key)
    now = datetime.now()
    memory: dict[str, str] = {}
    try:
        raw = json.loads(mem_row.value) if mem_row and mem_row.value else {}
    except ValueError:
        raw = {}
    for k, v in raw.items():
        try:
            if (now - datetime.fromisoformat(str(v))).total_seconds() < 86400:
                memory[_norm(k)] = str(v)
        except (ValueError, TypeError):
            continue
    fresh = [h for h in hotspots if _norm(h["keyword"]) not in memory]
    if not fresh:
        return {"status": "all_duplicated", "hotspots": len(hotspots)}

    supply = _supply_articles(db, user_id)
    matched: list[tuple[dict, WechatArticle]] = []
    unmatched: list[dict] = []
    for h in fresh[: max(1, int(getattr(settings, "hotspot_agent_top_n", 8) or 8))]:
        art = _match_supply(h["keyword"], supply)
        (matched if art else unmatched).append((h, art) if art else h)
    llm_top = max(0, int(getattr(settings, "hotspot_agent_llm_top", 3) or 3))
    proven = _proven_titles(db, user_id)
    llm_map = _llm_suggest(settings, unmatched[:llm_top], proven) if unmatched else {}

    lines: list[str] = []
    for h, art in matched:
        my = next((x.strip() for x in (art.my_pan_urls or "").splitlines() if x.strip()), "")
        src = next((x.strip() for x in (art.pan_urls or "").splitlines() if x.strip()), "")
        link = my or src
        lines.append(f"🔥《{h['keyword']}》热度 +{h['growth']:.0f}% → 已有现成资源:"
                     f"「{art.title[:40]}」({art.author})"
                     + (f" → 点这:{link}" if link else ""))
    for h in unmatched:
        s = llm_map.get(h["keyword"], "")
        if s:
            lines.append(f"💡 {s}")
    if not lines:
        return {"status": "no_suggestions", "hotspots": len(fresh)}

    for h in fresh:
        memory[_norm(h["keyword"])] = now.isoformat(timespec="seconds")
    row = db.get(SystemConfig, mem_key)
    val = json.dumps(memory, ensure_ascii=False)
    if row is None:
        db.add(SystemConfig(key=mem_key, value=val, updated_at=now))
    else:
        row.value = val
        row.updated_at = now
    db.commit()

    alert_service.notify_incident(
        db=db, user_id=user_id, kind="agent",
        title=f"🤖 热点选题建议:{len(lines)} 条(现成资源 {len(matched)} / LLM 选题 {len(unmatched)})",
        detail="近 24h 监控词热度达标,对应可执行动作如下:\n" + "\n".join(lines)[:1800],
        settings=settings, push_feishu=False)
    return {"status": "ok", "hotspots": len(fresh), "matched": len(matched),
            "llm": len(unmatched), "notified": len(lines)}


def hotspot_agent_tick_all_users(settings: Settings | None = None) -> int:
    """计划任务入口:对全部启用用户各跑一轮。"""
    settings = settings or get_settings()
    from app.db.database import get_session_local
    from app.db.models import User

    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                out = run_hotspot_agent(db, uid, settings)
                if out.get("status") == "ok":
                    total += 1
                db.commit()
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("热点选题 Agent 失败 user=%s", uid)
    finally:
        db.close()
    return total
