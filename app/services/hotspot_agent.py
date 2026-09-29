"""热点→网盘拉新 Agent:把「监控词热度」与「供应商新发资源」对上,产出可执行的发货建议。

三层(2026-09-28 v3):
- 精确匹配(零成本):热点词与资源文标题归一化子串命中 → 「热点已有人供货」,附我方/源链;
- 语义匹配 + 拉新选题(一次 LLM 调用,配了 deepseek 才跑):
  ① 标题不含字面热点词但语义相关的资源文也能匹配(如热点「世界杯」↔ 资源「足球赛程表」);
  ② 无现成资源的热点给拉新方案(资源清单/发布标题/人群/转存钩子);
- 落库:每条建议写 hotspot_suggestions 表(回看 + 未来效果回填闭环)。

多平台共振(v4, 2026-09-29):抖音监控词仍是热点入口(唯一带量化涨幅的源),
微博/百度热搜的「新上榜」条目做交叉验证——共振热点加权排序,并在 LLM 输入与
建议输出中标注平台证据,破"单平台+个人样本"的输入单一性(用户 2026-09-29 指出)。

机会分决策(v5, 2026-09-29):排序与推送取舍用确定性公式
  机会分 = 需求(共振分级加权涨幅:百度×1.5/微博×1.2) × 竞争稀疏度(1/(1+同话题供给))
         × 窗口因子(score 动量分档 12h/6h/2h);
LLM 只负责选题发散,不参与排序——可解释、可回测、可调权重。
推送带建议 [#id],运营一键标记「已发」(acted)后,该建议 + 夸克 save_pv
才构成"预测→下注→结算"学习样本;每天推送额度默认 top3,其余落库备选。

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
from app.db.models import (BaiduHotItem, DouhotWatchSnap, HotspotSuggestion,
                           SystemConfig, WechatArticle, WeiboHotItem)
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


def _platform_newcomers(db: Session, user_id: int, model,
                        hours: int = 24, fresh_hours: int = 6,
                        limit: int = 60) -> dict[str, dict]:
    """某热搜平台近 N 小时的「新上榜」条目(首次出现在近 fresh_hours 内)。

    新上榜 = 上升信号最干净的代理指标:整张榜全塞给 Agent 会淹没真正的增量。
    返回 {归一化标题: {"title", "heat", "rank"}}。
    """
    cutoff = datetime.now() - timedelta(hours=hours)
    fresh = datetime.now() - timedelta(hours=fresh_hours)
    rows = db.execute(
        select(model.title, func.min(model.captured_at),
               func.max(model.heat), func.min(model.rank))
        .where(model.user_id == user_id, model.captured_at >= cutoff)
        .group_by(model.title)).all()
    out: dict[str, dict] = {}
    for title, first, heat, rank in rows:
        if first and first >= fresh:
            n = _norm(title)
            if n:
                out[n] = {"title": str(title or ""), "heat": int(heat or 0),
                          "rank": int(rank or 0)}
    # 榜单可能极长(微博一天上百条新上榜),取 rank 最靠前的 limit 条
    ranked = sorted(out.items(), key=lambda kv: kv[1]["rank"])
    return dict(ranked[:limit])


def _match_newcomer(kw_norm: str, newcomers: dict[str, dict]) -> dict | None:
    """抖音词 ↔ 热搜标题共振匹配(归一化双向包含;短词也能命中长标题同话题)。"""
    if len(kw_norm) < 2:
        return None
    for tnorm, sig in newcomers.items():
        if kw_norm in tnorm or tnorm in kw_norm:
            return sig
    return None


def _resonance(db: Session, user_id: int, hotspots: list[dict],
               hours: int = 24, fresh_hours: int = 6) -> None:
    """原地给抖音热点附加微博/百度交叉证据。

    每个热点新增:weibo/baidu(命中详情或 None)、platforms("douyin+weibo" 等)、
    effective_growth(证据分级加权,排序用;growth 仍是主导)。

    证据分级(v5, 2026-09-29):对网盘拉新(搜索→转存行为)而言,百度榜≈真实搜索
    意图 > 微博榜≈社会话题(可被运营) > 抖音热度≈内容消费。共振加权按证据
    可信度分级:百度 ×1.5 / 微博 ×1.2(此前统一 ×1.3 过粗)。
    """
    weibo = _platform_newcomers(db, user_id, WeiboHotItem, hours, fresh_hours)
    baidu = _platform_newcomers(db, user_id, BaiduHotItem, hours, fresh_hours)
    for h in hotspots:
        kw = _norm(h["keyword"])
        w = _match_newcomer(kw, weibo)
        b = _match_newcomer(kw, baidu)
        h["weibo"] = w
        h["baidu"] = b
        platforms = ["douyin"] + (["weibo"] if w else []) + (["baidu"] if b else [])
        h["platforms"] = "+".join(platforms)
        boost = 1.0
        if b:
            boost *= 1.5
        if w:
            boost *= 1.2
        h["effective_growth"] = h["growth"] * boost


def _resonance_tag(h: dict) -> str:
    """人读共振标记:🌐抖音+微博+百度三榜共振 / 空串(仅抖音)。"""
    parts = []
    if h.get("weibo"):
        parts.append(f"微博榜第{h['weibo']['rank']}名")
    if h.get("baidu"):
        parts.append(f"百度榜第{h['baidu']['rank']}名")
    if not parts:
        return ""
    return f" 🌐共振({' + '.join(['抖音'] + parts)})"


def _window_factor(db: Session, user_id: int, hotspots: list[dict],
                   hours: int = 24) -> None:
    """热度动量 → 剩余窗口估计(v5)。

    用该词最近两拍 score 环比(30 分钟一拍)分级:动量 >0.2 上升期(12h,充足)/
    >0 平台期(6h,收紧)/ 否则衰退(2h,将关闭)。首拍无对比按充足处理
    (新词刚进监控,与其猜不如给观察机会)。
    """
    if not hotspots:
        return
    cutoff = datetime.now() - timedelta(hours=hours)
    rows = db.execute(
        select(DouhotWatchSnap.keyword, DouhotWatchSnap.score)
        .where(DouhotWatchSnap.user_id == user_id,
               DouhotWatchSnap.section == "douhot",
               DouhotWatchSnap.captured_at >= cutoff,
               DouhotWatchSnap.keyword.in_([h["keyword"] for h in hotspots]))
        .order_by(DouhotWatchSnap.keyword, DouhotWatchSnap.captured_at)).all()
    seqs: dict[str, list[float]] = {}
    for kw, score in rows:
        seqs.setdefault(str(kw), []).append(float(score or 0))
    for h in hotspots:
        seq = seqs.get(h["keyword"]) or []
        momentum = (seq[-1] / seq[-2] - 1) if len(seq) >= 2 and seq[-2] > 0 else 1.0
        if momentum > 0.2:
            h["window_hours"], h["window_factor"], h["window_tag"] = 12, 1.0, "窗口充足"
        elif momentum > 0:
            h["window_hours"], h["window_factor"], h["window_tag"] = 6, 0.6, "窗口收紧"
        else:
            h["window_hours"], h["window_factor"], h["window_tag"] = 2, 0.3, "窗口将关闭"


def _competition_factor(supply: list[WechatArticle], keyword: str) -> float:
    """竞争稀疏度 = 1/(1+72h 内同话题供给数):已有 N 家供货,机会按稀疏度衰减。

    判定同 _match_supply 的归一化子串口径(红海/蓝海的一等公民量化,
    不再只靠 LLM prompt 里的一句提醒)。
    """
    n = _norm(keyword)
    if len(n) < 2:
        return 1.0
    hits = sum(1 for a in supply if n in _norm(a.title))
    return 1.0 / (1 + hits)


def _opportunity(db: Session, user_id: int, hotspots: list[dict],
                 supply: list[WechatArticle]) -> None:
    """机会分 = 需求(共振加权涨幅) × 竞争稀疏度 × 窗口因子;原地写 h["opportunity"] 并按其排序。

    排序与「每天推哪几条」的取舍用确定性公式——可解释、可回测、可调权重;
    LLM 只负责选题发散(资源/人群/钩子),不参与排序(v5, 2026-09-29)。
    """
    _window_factor(db, user_id, hotspots)
    for h in hotspots:
        h["competition"] = _competition_factor(supply, h["keyword"])
        h["opportunity"] = h["effective_growth"] * h["competition"] * h["window_factor"]
    hotspots.sort(key=lambda x: x["opportunity"], reverse=True)


def _window_tag(h: dict) -> str:
    """人读窗口标记:⏳窗口充足(12h) / 空串(无窗口数据)。"""
    if not h.get("window_tag"):
        return ""
    return f" ⏳{h['window_tag']}(~{h.get('window_hours', '?')}h)"


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

    def _hline(i: int, h: dict) -> str:
        """热点输入行:带多平台证据(共振 = 全网真实需求,单平台 = 待观察)。"""
        line = f"{i}. 《{h['keyword']}》抖音热度增长 +{h['growth']:.0f}%"
        if h.get("weibo"):
            line += f" [微博热搜第{h['weibo']['rank']}名·热度{h['weibo']['heat']}]"
        if h.get("baidu"):
            line += f" [百度热搜第{h['baidu']['rank']}名]"
        if h.get("platforms") and h["platforms"] != "douyin":
            line += f" → {len(h['platforms'].split('+'))}平台共振(全网级需求)"
        return line

    hs = [_hline(i, h) for i, h in enumerate(hotspots, 1)]
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
                       "你是网盘拉新运营专家,专注「项目拆解/网盘资源」赛道。商业模式:"
                       "借社会热点制作/整理「配套资料」(课件/真题/模板/安装包/壁纸/攻略),"
                       "用夸克网盘分享链发布,用户为拿资料必须转存 → 完成拉新。"
                       "选题铁律:①热点事件决定人群,人群决定他们非要不可的那份资料;"
                       "宁要一个急用人群,不要十个围观者;"
                       "②热点可以来自任何领域(体育/影视/节日/社会事件),但变现方案"
                       "必须能落到网盘资源上——问自己「这群人此刻会搜什么、要什么文件」;"
                       "③该资源若已被同行大量跟发(竞争密度高),给出差异化角度而不是放弃。"
                       "④多平台共振(抖音+微博/百度同现)的热点是全网级真实需求,优先出方案;"
                       "单平台热点仅供参考,方案要更保守。"},
                      {"role": "user", "content":
                       "rising 热点如下(平台证据已标注):\n" + "\n".join(hs)
                       + "\n\n我们近 72h 已采集到的资源文(id. 标题;语义相关即可匹配,"
                         "标题不必字面含热点词):\n" + ("\n".join(sup) if sup else "(无)")
                       + "\n\n历史高转载资源文标题(个人+对标号混合样本,仅参考选题套路,"
                         "不代表当前需求,勿直接照抄):\n"
                         + proven_block
                       + "\n\n严格只输出一个 JSON 对象(无多余文字/无代码围栏):\n"
                         '{"matches": [{"hotspot": "热点词", "article_id": 资源文id,'
                         ' "why": "一句话说明相关性"}],\n'
                         ' "plans": [{"hotspot": "热点词", "resource": "具体到文件内容的资料清单",'
                         ' "title": "1条发布标题(带时效词/人群词)", "audience": "谁非要不可",'
                         ' "hook": "为什么必须转存(拉新点)",' ' "keywords": ["用户会搜索的资源词1", "词2", "词3"]}]}\n'
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
    plans: dict[str, dict] = {}
    for p in data.get("plans", []):
        kw = str(p.get("hotspot") or "").strip()
        if not kw:
            continue
        kws = [str(x).strip() for x in (p.get("keywords") or []) if str(x).strip()]
        parts = [f"资源:{p.get('resource') or '?'}", f"标题:{p.get('title') or '?'}",
                 f"人群:{p.get('audience') or '?'}", f"拉新点:{p.get('hook') or '?'}"]
        plans[kw] = {"text": " | ".join(parts), "keywords": kws}
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
    # 爆发话题同样做多平台共振验证(爆发=最需要确认是全网级需求的时刻)
    _resonance(db, user_id, hotspots)
    by_topic = {h["keyword"]: h for h in hotspots}
    proven = _proven_titles(db, user_id)
    llm = _llm_plan(settings, hotspots, _supply_articles(db, user_id), proven)
    lines: list[str] = []
    for t in topics:
        h = by_topic.get(t, {})
        m = (llm.get("matches") or {}).get(t) or {}
        aid = m.get("article_id")
        if isinstance(aid, int):
            art = db.get(WechatArticle, aid)
            if art:
                my = next((x.strip() for x in (art.my_pan_urls or "").splitlines() if x.strip()), "")
                src = next((x.strip() for x in (art.pan_urls or "").splitlines() if x.strip()), "")
                link = my or src
                lines.append(f"⚡《{t}》爆发{_resonance_tag(h)} → 已有现成资源:「{art.title[:40]}」"
                             + (f" → 点这:{link}" if link else ""))
                db.add(HotspotSuggestion(user_id=user_id, keyword=t, growth=0, kind="match",
                                         resource_title=art.title[:255],
                                         link=link, plan=m.get("why") or "爆发语义匹配",
                                         platforms=str(h.get("platforms") or "douyin")))
                continue
        p = (llm.get("plans") or {}).get(t)
        if p:
            lines.append(f"⚡《{t}》爆发{_resonance_tag(h)} → {p}")
            db.add(HotspotSuggestion(user_id=user_id, keyword=t, growth=0, kind="llm",
                                     plan=p[:500],
                                     platforms=str(h.get("platforms") or "douyin")))
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


def _auto_watch_keywords(db: Session, user_id: int, plan_by_kw: dict[str, dict],
                         cap: int = 3) -> list[str]:
    """把 LLM 选题的资源搜索词自动加入抖音热度监控(外部需求传感器)。

    为什么是它:个人转化数据有「小样本+幸存者偏差」的局限,能预测拉新的主力是
    **外部需求曲线**——资源词进监控后,第二天就有该词的真实热度,下一轮建议的
    排序即有外部数据支撑(个人夸克后台只做类型校准,非阻塞)。
    每日最多加 cap 个防污染;已在监控列表的词跳过。
    """
    from app.db.models import DouhotWatch

    existing = {w.keyword for w in db.scalars(select(DouhotWatch).where(
        DouhotWatch.user_id == user_id, DouhotWatch.section == "douhot")).all()}
    day_key = f"hotspot_agent_watchday_{user_id}"
    row = db.get(SystemConfig, day_key)
    today = datetime.now().date().isoformat()
    used = 0
    if row and row.value:
        try:
            d, n = str(row.value).split("|", 1)
            used = int(n) if d == today else 0
        except ValueError:
            used = 0
    added: list[str] = []
    for kw, info in plan_by_kw.items():
        if len(added) + used >= cap:
            break
        for kw2 in info.get("keywords") or []:
            k = str(kw2).strip()
            if not k or k in existing or k in added:
                continue
            db.add(DouhotWatch(user_id=user_id, section="douhot", list_type="word",
                               keyword=k[:128]))
            added.append(k)
            break   # 每个选题只加第一个词
    if added:
        val = f"{today}|{len(added) + used}"
        if row is None:
            db.add(SystemConfig(key=day_key, value=val, updated_at=datetime.now()))
        else:
            row.value = val
            row.updated_at = datetime.now()
    return added


def run_hotspot_agent(db: Session, user_id: int, settings: Settings | None = None) -> dict:
    """跑一轮热点选题建议;返回统计。站内通知,24h 内同热点不重复推。"""
    settings = settings or get_settings()
    if not getattr(settings, "hotspot_agent_enabled", True):
        return {"status": "disabled"}
    min_growth = float(getattr(settings, "hotspot_min_growth", 50) or 50)
    hotspots = _hotspots(db, user_id, min_growth)
    if not hotspots:
        return {"status": "no_hotspots"}
    # 多平台共振:微博/百度新上榜交叉验证,共振热点加权上浮(输入去单一化)
    _resonance(db, user_id, hotspots)

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
    # 机会分排序(v5):需求(共振分级)×竞争稀疏度×窗口,确定性公式取代纯涨幅排序
    _opportunity(db, user_id, fresh, supply)

    top_n = max(1, int(getattr(settings, "hotspot_agent_top_n", 8) or 8))
    fresh = fresh[:top_n]

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
    llm_plans: dict[str, dict] = {}
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

    # 落库:建议进 hotspot_suggestions(回看 + 未来效果回填)。
    # flush 取 id:推送行带 [#id],运营一键标记「已发」构成预测→结算闭环。
    by_kw = {h["keyword"]: h for h in fresh}
    plan_by_kw = dict(llm_plans)
    row_ids: dict[str, int] = {}
    for h, art, why in matched:
        row = HotspotSuggestion(
            user_id=user_id, keyword=h["keyword"], growth=h["growth"], kind="match",
            resource_title=art.title[:255], link=art.my_pan_urls or art.pan_urls or "",
            plan=f"{why}·匹配自 {art.author}", platforms=str(h.get("platforms") or "douyin"),
            opportunity=float(h.get("opportunity") or 0))
        db.add(row)
        db.flush()
        row_ids[h["keyword"]] = row.id
    for kw, plan in plan_by_kw.items():
        h = by_kw.get(kw, {"growth": 0, "platforms": "douyin"})
        row = HotspotSuggestion(
            user_id=user_id, keyword=kw, growth=h.get("growth", 0), kind="llm",
            resource_title="", link="", plan=plan["text"][:500],
            platforms=str(h.get("platforms") or "douyin"),
            opportunity=float(h.get("opportunity") or 0))
        db.add(row)
        db.flush()
        row_ids[kw] = row.id

    # 输出行,按机会分分组:优先发货(top push_top)在前,备选落库可回看。
    # 注意力是稀缺资源:全推等于没推,推送额度用确定性公式取舍。
    push_top = max(1, int(getattr(settings, "hotspot_agent_push_top", 3) or 3))
    entries: list[tuple[float, str, str]] = []   # (opportunity, 展示行, 复制块或空)
    for h, art, why in matched:
        my = next((x.strip() for x in (art.my_pan_urls or "").splitlines() if x.strip()), "")
        src = next((x.strip() for x in (art.pan_urls or "").splitlines() if x.strip()), "")
        link = my or src
        level, why_risk = resource_risk(art.title)
        risk_tag = f" ⚠️{why_risk},慎投时效" if level == "high" else ""
        comp = h.get("competition")
        comp_tag = f" 竞争{round(1 / comp - 1)}家" if comp is not None and comp < 1 else " 竞争空白"
        line = (f"🔥[# {row_ids.get(h['keyword'], '?')}]《{h['keyword']}》热度 +{h['growth']:.0f}%"
                f"{_resonance_tag(h)}{_window_tag(h)}{comp_tag} → 已有现成资源:"
                f"「{art.title[:40]}」({art.author})"
                + (f" [{why}]" if why and why != "标题字面命中" else "")
                + risk_tag
                + (f" → 点这:{link}" if link else ""))
        entries.append((float(h.get("opportunity") or 0), line,
                        f"【{art.title}】\n{link}" if link else ""))
    for kw, plan in plan_by_kw.items():
        h = by_kw.get(kw, {})
        entries.append((float(h.get("opportunity") or 0),
                        f"💡[# {row_ids.get(kw, '?')}] {kw}{_resonance_tag(h)}{_window_tag(h)} → {plan['text']}",
                        ""))
    entries.sort(key=lambda x: -x[0])
    lines = ["🎯 优先发货(机会分 top):"]
    lines.extend(e[1] for e in entries[:push_top])
    if len(entries) > push_top:
        lines.append("📋 备选(已落库,机会分靠后):")
        lines.extend(e[1] for e in entries[push_top:])
    copy_blocks = [e[2] for e in entries[:push_top] if e[2]]
    if copy_blocks:
        lines.append("──── 复制即用 ────")
        lines.extend(copy_blocks)
    watched = _auto_watch_keywords(db, user_id, plan_by_kw)
    if watched:
        lines.append("📡 已自动把资源词加入热度监控:" + "、".join(watched)
                     + "(明天这条建议会附上真实需求曲线)")

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
