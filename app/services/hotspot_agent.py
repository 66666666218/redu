"""热点→网盘拉新 Agent:把「监控词热度」与「供应商新发资源」对上,产出可执行的发货建议。

三层(2026-09-28 v3):
- 精确匹配(零成本):热点词与资源文标题归一化子串命中 → 「热点已有人供货」,附我方/源链;
- 语义匹配 + 拉新选题(一次 LLM 调用,配了 deepseek 才跑):
  ① 标题不含字面热点词但语义相关的资源文也能匹配(如热点「世界杯」↔ 资源「足球赛程表」);
  ② 无现成资源的热点给拉新方案(资源清单/发布标题/人群/转存钩子);
- 落库:每条建议写 hotspot_suggestions 表(回看 + 未来效果回填闭环)。

节流:同一热点词 24h 内只推一次(记忆落 system_config);输出按 2026-09-27 口径走
站内告警(push_feishu=False,飞书群只推文章与 Cookie 提醒)。
计划任务:hotspot_agent_cron(默认 9:10/15:10/21:10,跟在三个白天定点监听后面,数据最鲜)。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import (DouhotWatchSnap, HotspotSuggestion, SystemConfig,
                           WechatArticle)
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
    """标题命中(归一化子串)即视为「热点已有现成资源」——零成本,字面命中绝不漏。"""
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


def _llm_plan(settings: Settings, hotspots: list[dict],
              supply: list[WechatArticle], proven: list[str]) -> dict:
    """一次 LLM 调用同时完成:①热点↔资源语义匹配 ②无资源热点的拉新选题。

    返回 {"matches": {热点词: {"article_id": id, "why": 一句话}},
          "plans": {热点词: "资源:… | 标题:… | 人群:… | 拉新点:…"}};失败/未配 key 返回 {}。
    """
    if not settings.deepseek_api_key or not hotspots:
        return {}
    hs = [f"{i}. 《{h['keyword']}》热度增长 +{h['growth']:.0f}%"
          for i, h in enumerate(hotspots, 1)]
    sup = [f"{a.id}. {a.title}" for a in supply[:60]]
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
                       + "\n\n我们近 72h 已采集到的资源文(id. 标题;语义相关即可匹配,"
                         "标题不必字面含热点词):\n" + ("\n".join(sup) if sup else "(无)")
                       + "\n\n我们自己验证过能带来转存的资源文标题(参考选题套路):\n"
                         + proven_block
                       + "\n\n严格只输出一个 JSON 对象(无多余文字/无代码围栏):\n"
                         '{"matches": [{"hotspot": "热点词", "article_id": 资源文id,'
                         ' "why": "一句话说明相关性"}],\n'
                         ' "plans": [{"hotspot": "热点词", "resource": "具体到文件内容的资料清单",'
                         ' "title": "1条发布标题(带时效词/人群词)", "audience": "谁非要不可",'
                         ' "hook": "为什么必须转存(拉新点)"}]}\n'
                         "规则:matches 只收语义真正相关的资源(没有就空数组);"
                         "matches 里没有对应资源的热点必须给 plan;禁止编造不存在的 article_id。"}],
                  "temperature": 0.5, "max_tokens": 1200},
            timeout=60)
        if resp.status_code >= 400:
            logger.warning("热点 LLM 规划失败 HTTP %s", resp.status_code)
            return {}
        text = (resp.json().get("choices", [{}])[0].get("message", {}) or {}).get("content") or ""
    except requests.RequestException as exc:
        logger.warning("热点 LLM 规划请求异常:%s", exc)
        return {}
    text = text.strip()
    if text.startswith("```"):   # 剥掉可能的代码围栏
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text.strip())
    except ValueError:
        logger.warning("热点 LLM 返回非 JSON,丢弃(%s...)", text[:80])
        return {}
    matches = {str(m.get("hotspot") or "").strip(): {"article_id": m.get("article_id"),
                                                     "why": str(m.get("why") or "")}
               for m in data.get("matches", []) if m.get("hotspot")}
    plans: dict[str, str] = {}
    for p in data.get("plans", []):
        kw = str(p.get("hotspot") or "").strip()
        if not kw:
            continue
        parts = [f"资源:{p.get('resource') or '?'}", f"标题:{p.get('title') or '?'}",
                 f"人群:{p.get('audience') or '?'}", f"拉新点:{p.get('hook') or '?'}"]
        plans[kw] = " | ".join(parts)
    return {"matches": matches, "plans": plans}


def _copy_block(article: WechatArticle) -> str:
    """从资源文行里拼「复制即用」发货块:标题 + 我方/源链 + 提取码。"""
    my = next((x.strip() for x in (article.my_pan_urls or "").splitlines() if x.strip()), "")
    src = next((x.strip() for x in (article.pan_urls or "").splitlines() if x.strip()), "")
    line = my or src
    if not line:
        return article.title
    m = re.search(r"提取码\s*([0-9A-Za-z]{4})", line)
    code = m.group(1) if m else ""
    link = re.match(r"https?://\S+?(?=\s|\(|$)", line)
    return f"「{article.title}」\n{link.group(0) if link else line}" + (f" 提取码:{code}" if code else "")


_RISK_HIGH = ("全集", "影视", "电影", "电视剧", "网剧", "4k", "蓝光", "付费课程", "网课", "破解")
_RISK_LOW = ("真题", "课件", "模板", "壁纸", "笔记", "汇总", "攻略", "素材", "赛程", "题库")


def resource_risk(title: str) -> tuple[str, str]:
    """资源侵权风险粗筛:版权方清扫(霸王茶姬事件实测)主要打影视/课程类。

    返回 (等级, 说明):high=高危短命(发布投入产出比低,慎投时效),
    low=低危长尾(自制整理类安全),mid=介于其间。纯词表粗筛,供时效决策参考。
    """
    n = _norm(title)
    if any(m in n for m in _RISK_HIGH):
        return "high", "影视/课程类,版权清扫高危"
    if any(m in n for m in _RISK_LOW):
        return "low", "整理/自制类,长尾安全"
    return "mid", ""


def supplier_scores(db: Session, user_id: int, days: int = 30) -> list[dict]:
    """供应商评分:近 N 天 产出资源数 × 盘链被全网转载次数(越多=需求被反复验证)。

    用于「优质号加密监控/劣质号降权」的运营决策,也可在 Agent 建议里标注货源质量。
    """
    from app.db.models import WechatPanLink

    cutoff = datetime.now() - timedelta(days=days)
    rows = db.execute(
        select(WechatArticle.author,
               func.count(func.distinct(WechatArticle.id)),
               func.count(WechatPanLink.id))
        .outerjoin(WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
        .where(WechatArticle.user_id == user_id,
               WechatArticle.created_at >= cutoff,
               WechatArticle.pan_urls != "")
        .group_by(WechatArticle.author)
        .order_by(func.count(WechatPanLink.id).desc())
        .limit(10)).all()
    return [{"author": a or "未知", "articles": int(ar or 0), "reposts": int(rp or 0),
             "score": int(ar or 0) * 2 + int(rp or 0) * 3} for a, ar, rp in rows]


def burst_plan(db: Session, user_id: int, topics: list[str],
               settings: Settings | None = None) -> str | None:
    """爆发话题的即时拉新方案(douhot_window 检出 burst 时调用);每话题 24h 一次。

    与定时 Agent 的区别:不定点、不求全——爆发话题本身就是最高优先级热点,
    立刻给「发什么货 + 标题 + 人群 + 转存钩子」,顺带匹配近 72h 是否已有现成资源。
    """
    settings = settings or get_settings()
    if not getattr(settings, "hotspot_agent_enabled", True) or not topics:
        return None
    mem_key = f"hotspot_agent_burst_{user_id}"
    mem_row = db.get(SystemConfig, mem_key)
    now = datetime.now()
    memory: dict[str, str] = {}
    try:
        raw = json.loads(mem_row.value) if mem_row and mem_row.value else {}
    except ValueError:
        raw = {}
    memory = {_norm(str(k)): str(v) for k, v in raw.items()}
    topics = [t for t in topics
              if not (memory.get(_norm(t))
                      and (now - datetime.fromisoformat(memory[_norm(t)])).total_seconds() < 86400)]
    if not topics:
        return None
    hotspots = [{"keyword": t, "growth": 0} for t in topics[: max(1, int(getattr(settings, "hotspot_agent_llm_top", 3) or 3))]]
    proven = _proven_titles(db, user_id)
    llm = _llm_plan(settings, hotspots, _supply_articles(db, user_id), proven)
    lines: list[str] = []
    for t in topics:
        m = (llm.get("matches") or {}).get(t) or {}
        aid = m.get("article_id")
        if isinstance(aid, int):
            art = db.get(WechatArticle, aid)
            if art:
                my = next((x.strip() for x in (art.my_pan_urls or "").splitlines() if x.strip()), "")
                src = next((x.strip() for x in (art.pan_urls or "").splitlines() if x.strip()), "")
                link = my or src
                lines.append(f"⚡《{t}》爆发 → 已有现成资源:「{art.title[:40]}」"
                             + (f" → 点这:{link}" if link else ""))
                db.add(HotspotSuggestion(user_id=user_id, keyword=t, growth=0, kind="match",
                                         resource_title=art.title[:255],
                                         link=link, plan=m.get("why") or "爆发语义匹配"))
                continue
        p = (llm.get("plans") or {}).get(t)
        if p:
            lines.append(f"⚡《{t}》爆发 → {p}")
            db.add(HotspotSuggestion(user_id=user_id, keyword=t, growth=0, kind="llm",
                                     plan=p[:500]))
    if not lines:
        return None
    for t in topics:
        memory[_norm(t)] = now.isoformat(timespec="seconds")
    val = json.dumps(memory, ensure_ascii=False)
    if mem_row is None:
        db.add(SystemConfig(key=mem_key, value=val, updated_at=now))
    else:
        mem_row.value = val
        mem_row.updated_at = now
    db.commit()
    return "\n".join(lines)[:1800]


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

    top_n = max(1, int(getattr(settings, "hotspot_agent_top_n", 8) or 8))
    fresh = fresh[:top_n]
    supply = _supply_articles(db, user_id)

    # 第一层:标题子串精确匹配(零成本,字面命中绝不漏)
    matched: list[tuple[dict, WechatArticle, str]] = []
    rest: list[dict] = []
    for h in fresh:
        art = _match_supply(h["keyword"], supply)
        if art:
            matched.append((h, art, "标题字面命中"))
        else:
            rest.append(h)

    # 第二层:LLM 语义匹配 + 拉新选题(一次调用;没配 key 自动跳过)
    llm_plans: dict[str, str] = {}
    llm_matches: dict[str, tuple[WechatArticle, str]] = {}
    llm_top = max(0, int(getattr(settings, "hotspot_agent_llm_top", 3) or 3))
    llm_hotspots = rest[:llm_top]
    if llm_hotspots:
        proven = _proven_titles(db, user_id)
        llm_out = _llm_plan(settings, llm_hotspots, supply, proven)
        by_id = {a.id: a for a in supply}
        for h in llm_hotspots:
            m = (llm_out.get("matches") or {}).get(h["keyword"]) or {}
            aid = m.get("article_id")
            if isinstance(aid, int) and aid in by_id:
                art = by_id[aid]
                matched.append((h, art, m.get("why") or "语义匹配"))
                llm_matches[h["keyword"]] = (art, m.get("why") or "")
            else:
                plan = (llm_out.get("plans") or {}).get(h["keyword"])
                if plan:
                    llm_plans[h["keyword"]] = plan

    if not matched and not llm_plans:
        return {"status": "no_suggestions", "hotspots": len(fresh)}

    # 落库:建议进 hotspot_suggestions(回看 + 未来效果回填)
    by_kw_article = {h["keyword"]: (a, why) for h, a, why in matched}
    plan_by_kw = dict(llm_plans)
    for h, art, why in matched:
        db.add(HotspotSuggestion(
            user_id=user_id, keyword=h["keyword"], growth=h["growth"], kind="match",
            resource_title=art.title[:255], link=art.my_pan_urls or art.pan_urls or "",
            plan=f"{why}·匹配自 {art.author}"))
    for kw, plan in plan_by_kw.items():
        h = next((x for x in fresh if x["keyword"] == kw), {"growth": 0})
        db.add(HotspotSuggestion(
            user_id=user_id, keyword=kw, growth=h["growth"], kind="llm",
            resource_title="", link="", plan=plan[:500]))

    # 输出行
    lines: list[str] = []
    copy_blocks: list[str] = []
    for h, art, why in matched:
        my = next((x.strip() for x in (art.my_pan_urls or "").splitlines() if x.strip()), "")
        src = next((x.strip() for x in (art.pan_urls or "").splitlines() if x.strip()), "")
        link = my or src
        level, why_risk = resource_risk(art.title)
        risk_tag = f" ⚠️{why_risk},慎投时效" if level == "high" else ""
        lines.append(f"🔥《{h['keyword']}》热度 +{h['growth']:.0f}% → 已有现成资源:"
                     f"「{art.title[:40]}」({art.author})"
                     + (f" [{why}]" if why and why != "标题字面命中" else "")
                     + risk_tag
                     + (f" → 点这:{link}" if link else ""))
        copy_blocks.append(f"【{art.title}】\n{link}")
    for kw, plan in plan_by_kw.items():
        lines.append(f"💡 {kw} → {plan}")
    if copy_blocks:
        lines.append("──── 复制即用 ────")
        lines.extend(copy_blocks)

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
        title=f"🤖 热点选题建议:{len(matched)} 条现成资源 / {len(plan_by_kw)} 条拉新选题",
        detail="近 24h 监控词热度达标,可执行动作:\n" + "\n".join(lines)[:2000],
        settings=settings, push_feishu=False)
    return {"status": "ok", "hotspots": len(fresh), "matched": len(matched),
            "llm": len(plan_by_kw), "notified": len(matched) + len(plan_by_kw)}


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
