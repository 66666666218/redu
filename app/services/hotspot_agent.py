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
推送带建议 [#id],运营一键标记「已发」(acted)后,该建议 + **结算信号**才构成
"预测→下注→结算"学习样本。⚠️ **结算信号 2026-09-29 已改道**(2026-10-03 更正此处旧注释):
原写"夸克 save_pv",但夸克官方「分享管理」不提供链接级转存数,采集链已休眠;
现在用 **`repost_gain`**(发文后全网新增的该文盘链记录数 = 资源被疯转 = 需求被反复验证),
总账对账走 `pan_recruit_weekly`(方案B 人工周录)。见 `settle_suggestions` 的 docstring。
每天推送额度默认 top3,其余落库备选。

节流:同一热点词 24h 内只推一次(记忆落 system_config);输出按 2026-09-27 口径走
站内告警(push_feishu=False,飞书群只推文章与 Cookie 提醒)。
计划任务:**没有独立 cron** —— 本函数由 `push_timeline` 的 `agent` 类驱动
(时段见 `push_timeline.PUSH_KINDS["agent"]`,默认 **09:10/14:10/20:40**,跟在白天定点监听后面,数据最鲜)。
(旧注释写的 `hotspot_agent_cron` 是**死配置**:`scheduler` 从不读它,已删除。)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import (WechatPanLink, BaiduHotItem, DouhotWatchSnap, HotspotSuggestion,
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


# 同平台的多个榜单算**一个平台**:36氪有 quick/renqi/主榜三条、财联社有 hot/depth/telegraph……
# 它们的标题常常一模一样,若不归一,"同一个 36氪内容挂三个 id"会被当成"3 平台共振"而霸榜,
# 把真正跨平台的热点(如"豆瓣+爱奇艺同现的剧")挤到后面去(2026-10-01 扩容后实测踩到)。
_PLAT_FAMILY = {
    "36kr-quick": "36kr", "36kr-renqi": "36kr",
    "cls-hot": "cls", "cls-depth": "cls", "cls-telegraph": "cls",
    "wallstreetcn-hot": "wallstreetcn", "wallstreetcn-news": "wallstreetcn",
    "wallstreetcn-quick": "wallstreetcn",
    "fastbull-express": "fastbull", "fastbull-news": "fastbull",
    "chongbuluo-hot": "chongbuluo", "chongbuluo-latest": "chongbuluo",
}


def _family(src: str) -> str:
    """榜单 id → 平台族(用于"真·跨平台数"统计)。"""
    return _PLAT_FAMILY.get(str(src or ""), str(src or ""))


def _platform_hot_candidates(db: Session, user_id: int, top_rank: int = 10,
                             hours: int = 24, cap: int = 60) -> list[dict]:
    """多平台热榜候选(v2.2.0):hot_source_items 近 N 小时 top 条目进入选题池。

    跨平台同现(标题归一化相同 ≥2 平台)是全网级真实信号;单平台 top 交给 LLM 判可做性。
    growth 为 0(热榜条目无 douhot 式历史涨幅)——排序按 **跨平台数 → 平台拉新权重 → 名次**。

    `cap` 默认 60(2026-10-01 由 15 上调):平台已扩到 43 个,而原来无论多少平台都只留
    15 条 —— 实测 430 条候选里 415 条被砍在 LLM 之前,**热点利用率不足 4%**。
    按权重排序而非单纯截断,是为了让"豆瓣热剧/酷安软件"这类真能出素材的排在
    "雪球热股/财联社快讯"前面(权重表见 `niche_fit.SOURCE_FIT`)。
    """
    from app.db.models import HotSourceItem
    from app.services.niche_fit import SOURCE_FIT

    cutoff = datetime.now() - timedelta(hours=hours)
    rows = db.execute(select(HotSourceItem.source, HotSourceItem.title, HotSourceItem.rank)
                      .where(HotSourceItem.user_id == user_id,
                             HotSourceItem.captured_at >= cutoff,
                             HotSourceItem.rank <= top_rank)).all()
    agg: dict[str, dict] = {}
    for src, title, rank in rows:
        k = _norm(str(title or ""))
        if not k:
            continue
        e = agg.setdefault(k, {"title": str(title), "plats": set(), "best_rank": 99})
        e["plats"].add(str(src))
        e["best_rank"] = min(e["best_rank"], int(rank or 99))
    out = [{"keyword": e["title"], "growth": 0.0,
            "platforms": "+".join(sorted(e["plats"])), "rank": e["best_rank"],
            "auto": True}  # 自动发现型:需过适配度;用户自选监控词(growth 型)不拦
           for e in agg.values()]
    out.sort(key=lambda h: (
        -len({_family(p) for p in h["platforms"].split("+")}),               # 真·跨平台数(同平台多榜算一个)
        -max(SOURCE_FIT.get(p, 0.0) for p in h["platforms"].split("+")),     # 再看平台拉新权重
        h["rank"],                                                           # 最后看名次
    ))
    return out[:cap]


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


# **资源库证据**的加权(2026-10-04)。与榜单共振同一套分级思路,但依据不同:
#   榜单说"**大家在讨论**";资源库说"**已经有人在发这个资源的盘链**"。
LIBRARY_BOOST_WITH_LINK = 1.4   # 库里有 + **我方已有链** ⇒ 点一下就能发,最强
LIBRARY_BOOST_NO_LINK = 1.2     # 库里有 + 还没搬 ⇒ 需求在,但还得动手


def _library_evidence(db: Session, user_id: int, hotspots: list[dict],
                      days: int = 30, cap: int = 25) -> None:
    """给热点补「**资源库证据**」:库里有没有这个资源、我方有没有现成的链。

    ⚠️ **为什么值得单独一档**(对拉新业务而言它可能比榜单更硬):
    榜单说明"**大家在讨论**",而"**已经有人在发这个资源的盘链**"说明"**这事有人已经在做了**";
    ⭐ 若我方**已经有链**,那就是"**点一下就能发**"——这是最接近可执行的信号。

    原地写 `h["library"]`(命中详情或 None)、`h["library_boost"]`,并乘进 `effective_growth`
    (与 `_resonance` 同一套加权口径,可叠加)。

    ⚠️ **拿不到不算负面** —— 匹配不上就是 `None`(倍数 1.0),**不惩罚**:
    "库里没有"可能是"我们还没搬",不是"这事不行"。
    """
    from app.services.resource_library import search_resources

    for h in hotspots[:cap]:
        kw = str(h.get("keyword") or "").strip()
        hit = None
        if len(kw) >= 2:                     # 两字以下不检索(与 search_resources 同口径)
            try:
                hits = search_resources(db, user_id, kw, days=days, limit=1)
            except Exception:  # noqa: BLE001 - 检索失败不该拖垮选题
                logger.exception("资源库证据检索失败 kw=%s", kw)
                hits = []
            hit = hits[0] if hits else None
        h["library"] = hit
        boost = 1.0
        if hit:
            boost = LIBRARY_BOOST_WITH_LINK if hit.get("my_link") else LIBRARY_BOOST_NO_LINK
        h["library_boost"] = boost
        if boost != 1.0:
            h["effective_growth"] = float(h.get("effective_growth") or h.get("growth") or 0) * boost
        # ⚠️ 一屏内别刷太多检索:热点通常十来条,但 burst 路径可能更多
    for h in hotspots[cap:]:
        h.setdefault("library", None)
        h.setdefault("library_boost", 1.0)


def _library_tag(h: dict) -> str:
    """人读资源库标记:`📦库内已有链`(点一下就能发)/ `📦库内有(待搬)` / 空串。"""
    hit = h.get("library")
    if not hit:
        return ""
    titles = hit.get("titles") or []
    sample = str(titles[0])[:20] if titles else ""
    if hit.get("my_link"):
        return f" 📦库内已有链「{sample}」"
    return f" 📦库内有「{sample}」(还没搬)"


# **跨平台资源同现**的加权(2026-10-05)。比榜单共振**更硬的一档**:
#   榜单共振 = "大家在**讨论**";这一档 = "**同一个资源被多个平台的人贴过**"
#   —— 有人已经在靠它拉新,而且不止一处。
CROSS_PLATFORM_BOOST: dict[int, float] = {3: 1.6, 2: 1.3}   # 平台数 → 倍数(<2 不加)


def _cross_platform_evidence(db: Session, user_id: int, hotspots: list[dict],
                             days: int = 14) -> None:
    """给热点补「**跨平台资源同现**」证据:这个资源在**几个平台**被人贴过。

    ⚠️ **为什么值得单独一档**(它是目前能拿到的**最硬**的需求证据):
    榜单只是"大家在讨论";而**同一个资源在微博/知乎/贴吧/公众号都被贴出来**
    说明"**有人已经在靠它拉新,而且不止一处**"。这与 `resource_library.resonance_resources`
    的"多号同发"是同一逻辑,但把"号"扩到了"**平台**" —— 跨平台同现比同平台多号更难伪装。

    ⚠️ **`>= 2` 个平台才加权**:单个平台命中是常态(某个词本来就在某平台流行),
    加它等于给所有词加一样的分,没信息量。

    原地写 `h["platforms_found"]`(平台名列表)、`h["cross_boost"]`,并乘进 `effective_growth`。
    """
    from app.db.models import DiscoveredPanLink

    since = datetime.now() - timedelta(days=days)
    # (平台, 标题) 对:发现链自带 platform;公众号那条从文章标题取,平台记作 wechat
    pairs: list[tuple[str, str]] = [
        (str(p or ""), str(t or ""))
        for p, t in db.execute(select(DiscoveredPanLink.platform, DiscoveredPanLink.title)
                               .where(DiscoveredPanLink.user_id == user_id,
                                      DiscoveredPanLink.found_at >= since)).all()]
    pairs += [("wechat", str(t or ""))
              for (t,) in db.execute(
                  select(WechatArticle.title)
                  .join(WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
                  .where(WechatPanLink.user_id == user_id,
                         WechatArticle.created_at >= since)).all()]
    pairs = [(pl, ti) for pl, ti in pairs if pl and ti]
    for h in hotspots:
        kw = str(h.get("keyword") or "").strip()
        if len(kw) < 2:
            h.setdefault("platforms_found", [])
            h.setdefault("cross_boost", 1.0)
            continue
        plats = sorted({pl for pl, ti in pairs if kw in ti})
        h["platforms_found"] = plats
        boost = CROSS_PLATFORM_BOOST.get(len(plats), 1.0)
        h["cross_boost"] = boost
        if boost != 1.0:
            h["effective_growth"] = float(h.get("effective_growth") or h.get("growth") or 0) * boost


def _evidence_tag(h: dict) -> str:
    """把这条热点**靠哪几档证据顶上来**摊开(`[证据 榜单×1.5 跨平台×1.6 …]`)。

    ⚠️ **只列触发过的档** —— 这正是它的用处:一眼看出哪一档在起作用、哪一档**从没触发过**。
    没触发的档要么是数据源没接上、要么是阈值定得太高,两种都值得看一眼
    (本仓反复出现的"废弃链只摘了一半"就是这类:功能在、但永远不生效)。

    返回空串 = **一档都没触发** —— 这条信息本身也重要(说明这条完全靠原生热度上来的)。
    """
    tiers: list[tuple[str, float]] = []
    # 榜单共振:微博/百度各算一档(与 `_resonance` 的加权一一对应)
    if h.get("baidu"):
        tiers.append(("百度榜", 1.5))
    if h.get("weibo"):
        tiers.append(("微博榜", 1.2))
    lib = h.get("library")
    if lib:
        tiers.append(("资源库已有链" if lib.get("my_link") else "资源库待搬",
                      float(h.get("library_boost") or 1.0)))
    plats = h.get("platforms_found") or []
    if len(plats) >= 2:
        tiers.append((f"跨平台({len(plats)}个)", float(h.get("cross_boost") or 1.0)))
    cw = float(h.get("category_weight") or 1.0)
    if abs(cw - 1.0) > 1e-9:
        tiers.append(("品类权重", cw))
    if not tiers:
        return ""
    body = " ".join(f"{n}×{v:g}" for n, v in tiers)
    return f" [证据 {body}]"


def _evidence_tally(hotspots: list[dict]) -> str:
    """本轮**各档证据各触发了几次**(给运维看的体检行)。

    形如:`📊 证据触发:百度榜2 微博榜5 资源库3 跨平台1 品类权重0`。
    ⚠️ **恒为 0 的那几档要显眼** —— 那是"接了但没生效"的信号,不是"今天恰好没有"。
    """
    cnt = {"百度榜": 0, "微博榜": 0, "资源库": 0, "跨平台同现": 0, "品类权重": 0}
    for h in hotspots:
        if h.get("baidu"):
            cnt["百度榜"] += 1
        if h.get("weibo"):
            cnt["微博榜"] += 1
        if h.get("library"):
            cnt["资源库"] += 1
        if len(h.get("platforms_found") or []) >= 2:
            cnt["跨平台同现"] += 1
        if abs(float(h.get("category_weight") or 1.0) - 1.0) > 1e-9:
            cnt["品类权重"] += 1
    parts = [f"{k}{v}" for k, v in cnt.items()]
    dead = [k for k, v in cnt.items() if v == 0]
    tail = (f" ⚠️未触发:{'、'.join(dead)}(没数据或阈值太高,值得看一眼)"
            if len(dead) == len(cnt) else "")
    return f"📊 证据触发({len(hotspots)} 条热点):" + " ".join(parts) + tail


def _cross_platform_tag(h: dict) -> str:
    """人读标记:`🌍跨平台资源同现(微博+知乎+贴吧)` / 空串(不足 2 个平台不加)。"""
    plats = h.get("platforms_found") or []
    if len(plats) < 2:
        return ""
    _cn = {"wechat": "公众号", "weibo": "微博", "zhihu": "知乎", "tieba": "贴吧",
           "xiaohongshu": "小红书", "kuaishou": "快手", "douyin": "抖音", "bilibili": "B站"}
    return f" 🌍跨平台资源同现({'+'.join(_cn.get(p, p) for p in plats)})"


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
    # **品类权重**(2026-10-04 用户口径「自动调选题权重」):从**结算回来的实测数据**
    # 学一个倍数(reads_gain / repost_gain 各一条依据)。⚠️ 带护栏:样本 <3 的品类不参与、
    # 倍数夹在 [0.5,2.0]、**无数据时是空 dict ⇒ 公式与以前完全一致**(向后兼容)。
    from app.services.category_weight import multiplier_for, multipliers

    _cw = multipliers(db, user_id)
    for h in hotspots:
        h["competition"] = _competition_factor(supply, h["keyword"])
        _fit = h.get("fit")
        _fit_score = _fit.score if _fit is not None else 1.0  # burst 路径无 fit,不惩罚
        h["category_weight"] = multiplier_for(_cw, str(h.get("category") or ""))
        h["opportunity"] = (h["effective_growth"] * h["competition"] * h["window_factor"]
                            * max(_fit_score, 0.3)       # 适配度是乘数但设下限:弱适配不清零需求
                            * h["category_weight"])      # 品类权重(无数据时恒为 1.0)
    hotspots.sort(key=lambda x: x["opportunity"], reverse=True)


def _window_tag(h: dict) -> str:
    """人读窗口标记:⏳窗口充足(12h) / 空串(无窗口数据)。"""
    if not h.get("window_tag"):
        return ""
    return f" ⏳{h['window_tag']}(~{h.get('window_hours', '?')}h)"


def _safe_author(name: str, settings: Settings | None = None) -> str:
    """自营号名在建议输出文本中显示为「内部资源」(防推送外泄自营身份)。

    落库 plan 仍存真名(运营者自己的数据分析需要);此函数只用于组装对外的
    展示行——push_feishu 目前为 False,开飞书后也不漏(2026-09-29 防御性脱敏)。
    """
    settings = settings or get_settings()
    names = [n.strip() for n in (getattr(settings, "own_account_names", "") or "").split(",") if n.strip()]
    return "内部资源" if str(name or "").strip() in names else str(name or "")


def _supply_articles(db: Session, user_id: int, hours: int = 72) -> list[WechatArticle]:
    """资源候选:近 72h 对标资源文 + **资源库全历史高共振资源**(v2.4.0)。

    资源库实证:高共振资源(同链被多号同发)是验证过的金矿,却常沉在 72h 窗口外
    (如"霸王茶姬教程 ×5 号"横跨数周)。把它们的文章排到候选前面,
    LLM 输入(取前 60)自然优先看到验证过的资源;不改标题(字面匹配逻辑依赖)。
    """
    cutoff = datetime.now() - timedelta(hours=hours)
    near = list(db.scalars(select(WechatArticle).where(
        WechatArticle.user_id == user_id,
        WechatArticle.created_at >= cutoff,
        WechatArticle.pan_urls.isnot(None),
        WechatArticle.pan_urls != "",
    ).order_by(WechatArticle.created_at.desc()).limit(300)).all())
    try:
        from app.services.resource_library import resonance_resources

        hot = resonance_resources(db, user_id, days=90, min_accounts=2, limit=20)
        if hot:
            have = {a.id for a in near}
            ids = db.scalars(select(WechatPanLink.article_id).where(
                WechatPanLink.user_id == user_id,
                WechatPanLink.pan_url.in_([r["pan_url"] for r in hot]))).all()
            extra = list(db.scalars(select(WechatArticle).where(
                WechatArticle.user_id == user_id,
                WechatArticle.id.in_(ids),
                WechatArticle.pan_urls.isnot(None), WechatArticle.pan_urls != "",
            ).order_by(WechatArticle.created_at.desc()).limit(60)).all())
            # 验证过的排最前(去重)
            near = [a for a in extra if a.id not in have] + near
    except Exception:  # noqa: BLE001 - 资源库增强失败不挡选题
        logger.debug("资源库候选增强失败", exc_info=True)
    return near[:360]


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


def _fit_line(h: dict) -> str:
    """适配度一行(给运营的"为什么值得做";v2.3.0 用户命题:利用路径要可见)。"""
    fit = h.get("fit")
    if fit is None:
        return ""
    tag = {"strong": "🎯强适配", "mid": "🟡可做", "weak": "⚪弱"}.get(fit.level, "")
    why = fit.reasons[0] if fit.reasons else ""
    win = {"longtail": " · 长尾常青", "instant": " · 抢时效"}.get(fit.window, "")
    return f"   {tag} {why}{win}\n"


def _plan_text(p: dict) -> str:
    """LLM 建议字段 → 教学式文本(≤500,入库 plan 列与飞书卡通用)。

    2026-09-30 方法论化(用户要求):从"罗列四件套"升级为
    「为什么能做→做什么→给谁→时机→三步走」——每条建议自带可照做的利用路径,
    而不是只报"哪个热点高就去发"。
    """
    seg: list[str] = []
    if p.get("why_doable"):
        seg.append("【为什么能做】" + str(p["why_doable"]))
    res = "【做什么】" + (str(p.get("resource")) or "?")
    if p.get("title"):
        res += " | 标题:" + str(p["title"])
    seg.append(res)
    seg.append("【给谁】" + (str(p.get("audience")) or "?")
               + (f" | 钩子:{p.get('hook')}" if p.get("hook") else ""))
    if p.get("timing"):
        seg.append("【时机】" + str(p["timing"]))
    if p.get("steps"):
        seg.append("【三步走】" + str(p["steps"]))
    return "\n".join(seg)[:500]


def _json_cut_points(text: str) -> list[int]:
    """**不在字符串里**的 `}` 位置(从后往前)—— 截断 JSON 的候选修复点。

    ⚠️ 必须跟踪字符串状态:标题里带 `}`(如「{模板}」)会把朴素的 `rfind("}")` 骗过去。
    """
    out: list[int] = []
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "}":
            out.append(i)
    return out[::-1]


def _balance(text: str) -> str:
    """补上未闭合的 `[`/`{`(字符串内不计)。"""
    stack: list[str] = []
    in_str = esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            stack.append(ch)
        elif ch in "]}":
            if stack:
                stack.pop()
    return text + "".join("]" if c == "[" else "}" for c in reversed(stack))


def loads_llm_json(text: str) -> dict | None:
    """解析 LLM 返回的 JSON;**被 `max_tokens` 截断时救出已写完的部分**(绝不补造)。

    ⚠️ **为什么必须救**(2026-10-05 实测):一次真实调用 `finish_reason=length` ——
    3109 字的输出被截在 `...{"hotspot": "新能源车补贴", "why`,而**前面十几条 plan
    是完整的**。整批丢掉 = "一天跑三次的选题 Agent **一条建议都不出**"。

    ⚠️ **更要紧的是原实现从不看 `finish_reason`**:把"上下文超了"记成
    「LLM 返回非 JSON」—— **看着像模型乱输出,方向完全错**,于是没人去调 `max_tokens`。
    (本仓那条母题:一个错误的诊断会把排障带往反方向。)

    救法:从后往前找**不在字符串里**的 `}`,把尾巴截到那里再补齐括号重试。
    **只保留原文里确实存在的字段** —— 截断处那个半截对象会被丢掉,不猜。
    """
    t = (text or "").strip()
    if not t:
        return None
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    for cut in _json_cut_points(t)[:120]:
        try:
            obj = json.loads(_balance(t[:cut + 1]))
        except ValueError:
            continue
        if isinstance(obj, dict) and (obj.get("matches") or obj.get("plans")):
            return obj
    return None


def _llm_plan(settings: Settings, hotspots: list[dict],
              supply: list[WechatArticle], proven: list[str]) -> dict:
    """一次 LLM 调用同时完成:①热点↔资源语义匹配 ②无资源热点的拉新选题。

    2026-09-30 方法论化:输出从"四件套"升级为教学式(为什么能做/做什么/给谁/时机/三步走),
    plans[kw] 为字段 dict,展示用 _plan_text() 组装。
    返回 {"matches": {热点词: {"article_id": id, "why": 教怎么盘活旧资源}},
          "plans": {热点词: 字段dict}};失败/未配 key 返回 {}。
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
                       "你是网盘拉新运营教练,专注「项目拆解/网盘资源」赛道。商业模式:"
                       "借社会热点制作/整理「配套资料」(课件/真题/模板/安装包/壁纸/攻略),"
                       "用夸克网盘分享链发布,用户为拿资料必须转存 → 完成拉新。"
                       "你的任务不是报告哪个热点火,而是**教会运营怎么利用这个热点**:"
                       "每条建议都要讲清「为什么这个热点的人群会非要一份文件不可(需求缺口)"
                       "→ 做什么资料 → 发什么标题 → 给谁 → 什么时机发 → 新手三步怎么落地」。"
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
                         ' "why": "一句话教运营怎么借这个热点盘活这份旧资源(切入角度/标题怎么改/怎么组合)"}],\n'
                         ' "plans": [{"hotspot": "热点词",'
                         ' "why_doable": "一句话讲透需求缺口:这个热点的人群此刻会搜什么/缺什么文件",'
                         ' "resource": "具体到文件内容的资料清单",'
                         ' "title": "1条发布标题(带时效词/人群词)", "audience": "谁非要不可",'
                         ' "hook": "为什么必须转存(拉新点)",'
                         ' "timing": "发布时机:热点发酵窗口(几小时内动手/热度还能持续几天)",'
                         ' "steps": "三步执行清单:①… ②… ③…(每步一个具体动作,教新手落地)",'
                         ' "keywords": ["用户会搜索的资源词1", "词2", "词3"]}]}\n'
                         "规则:matches 只收语义真正相关的资源(没有就空数组);"
                         "matches 里没有对应资源的热点必须给 plan;禁止编造不存在的 article_id。"
                         "你的输出是教一个新手「怎么利用这条热点」,不是报告热度——"
                         "每条建议都要给到能照着做的程度。"}],
                  "temperature": 0.5,
                  # ⚠️ 1600 不够(2026-10-05 实测 `finish_reason=length`,3109 字被截断 ⇒
                  # **整批建议被丢**)。每轮要出十几条 plan、每条 8 个字段,留足余量;
                  # 就算还超,下面 `loads_llm_json` 也能把**已写完的那部分**救回来。
                  "max_tokens": 4000},
            timeout=90)
        if resp.status_code >= 400:
            logger.warning("热点 LLM 规划失败 HTTP %s", resp.status_code)
            return {}
        _choice = (resp.json().get("choices") or [{}])[0]
        text = (_choice.get("message") or {}).get("content") or ""
        finish = str(_choice.get("finish_reason") or "")
    except requests.RequestException as exc:
        logger.warning("热点 LLM 规划请求异常:%s", exc)
        return {}
    text = text.strip()
    if text.startswith("```"):   # 剥掉可能的代码围栏
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    data = loads_llm_json(text)
    if data is None:
        # ⚠️ **把"截断"与"乱输出"分开报** —— 原来一律记成「返回非 JSON」,
        # 看着像模型胡说八道,于是没人去调 max_tokens。截断是**我们这边参数给少了**。
        if finish == "length":
            logger.warning("热点 LLM **被 max_tokens 截断**且抢救不出完整条目"
                           "(max_tokens=%s,输出 %d 字)—— 调大 max_tokens 或减少每轮热点数",
                           4000, len(text))
        else:
            logger.warning("热点 LLM 返回非 JSON(finish_reason=%s),丢弃(%s...)",
                           finish or "?", text[:80])
        return {}
    if finish == "length":
        logger.info("热点 LLM 输出被截断(finish_reason=length),已**抢救出完整的那部分**")
    matches = {str(m.get("hotspot") or "").strip(): {"article_id": m.get("article_id"),
                                                     "why": str(m.get("why") or "")}
               for m in data.get("matches", []) if m.get("hotspot")}
    plans: dict[str, dict] = {}
    for p in data.get("plans", []):
        kw = str(p.get("hotspot") or "").strip()
        if not kw:
            continue
        kws = [str(x).strip() for x in (p.get("keywords") or []) if str(x).strip()]
        plans[kw] = {"why_doable": str(p.get("why_doable") or ""),
                     "resource": str(p.get("resource") or ""), "title": str(p.get("title") or ""),
                     "audience": str(p.get("audience") or ""), "hook": str(p.get("hook") or ""),
                     "timing": str(p.get("timing") or ""), "steps": str(p.get("steps") or ""),
                     "keywords": kws}
    return {"matches": matches, "plans": plans}


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
                lines.append(f"⚡《{t}》爆发{_resonance_tag(h)}{_library_tag(h)}{_cross_platform_tag(h)} → 已有现成资源:「{art.title[:40]}」"
                             + (f" → 点这:{link}" if link else ""))
                db.add(HotspotSuggestion(user_id=user_id, keyword=t, growth=0, kind="match",
                                         resource_title=art.title[:255],
                                         link=link, plan=m.get("why") or "爆发语义匹配",
                                         platforms=str(h.get("platforms") or "douyin")))
                continue
        p = (llm.get("plans") or {}).get(t)
        if p:
            plan_text = _plan_text(p)
            lines.append(f"⚡《{t}》爆发{_resonance_tag(h)}{_library_tag(h)}{_cross_platform_tag(h)} → {plan_text}")
            db.add(HotspotSuggestion(user_id=user_id, keyword=t, growth=0, kind="llm",
                                     plan=plan_text[:500],
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
    # v2.2.0:多平台热榜候选并入(全部进入 Agent 选题;与 douhot 同词去重,保留涨幅版)
    _seen = {_norm(h["keyword"]) for h in hotspots}
    for _c in _platform_hot_candidates(db, user_id):
        _k = _norm(_c["keyword"])
        if _k and _k not in _seen:
            hotspots.append(_c)
            _seen.add(_k)
    if not hotspots:
        # 晨间/冷却期兜底(2026-09-30):涨幅>=50% 的口径在热点冷却时段会空手——
        # 一级:微博/百度"新上榜"(上升信号最干净的代理);二级:douhot 降阈值取正增长词。
        # 弱信号也交给 LLM 判可做性,好过定点空手(可做性判断本就是 LLM 的活)。
        from app.db.models import BaiduHotItem, WeiboHotItem

        newcom: dict[str, dict] = {}
        for model, plat in ((WeiboHotItem, "weibo"), (BaiduHotItem, "baidu")):
            for norm, it in _platform_newcomers(db, user_id, model).items():
                newcom.setdefault(norm, {"title": it["title"], "plats": set()})
                newcom[norm]["plats"].add(plat)
        hotspots = [{"keyword": v["title"], "growth": 0.0,
                     "platforms": "+".join(sorted(v["plats"]))}
                    for v in newcom.values()][:5]
        if not hotspots:
            rows2 = db.execute(
                select(DouhotWatchSnap.keyword, func.max(DouhotWatchSnap.trend_growth))
                .where(DouhotWatchSnap.user_id == user_id,
                       DouhotWatchSnap.section == "douhot",
                       DouhotWatchSnap.captured_at >= datetime.now() - timedelta(hours=24))
                .group_by(DouhotWatchSnap.keyword)
                .having(func.max(DouhotWatchSnap.trend_growth) >= 15)
                .order_by(func.max(DouhotWatchSnap.trend_growth).desc())
                .limit(5)).all()
            hotspots = [{"keyword": str(kw), "growth": float(g or 0)} for kw, g in rows2]
            if not hotspots:
                return {"status": "no_hotspots"}
    # v2.3.0 网盘拉新适配度(用户命题):监控热点的唯一目的是拉新——
    # 先把"不能资料化"的热点(纯新闻围观/无文件交付物)挡在 LLM 之前,
    # 省 token、降噪,且每条进 LLM 的候选都自带"为什么值得做"的可解释理由。
    from app.services.niche_fit import assess_many

    # 只拦自动发现型的弱适配热点;用户自选监控词(douhot,已表达跟随意向)直通,
    # fit 仍会计算——仅用于机会分权重与卡片展示("为什么值得做")
    # 用户口径(2026-10-01):"能拉新的都不要放过"——weak 不再直接挡,改为
    # **降权进备选**(机会分乘 0.3 下限已体现),仍交给 LLM 判;挡掉只针对"完全无从下手"
    # (适配度 < 0.1 且无平台分——纯围观类,如股票行情,进 LLM 也是浪费 token)
    _scored = assess_many(hotspots)
    hotspots = [h for h in _scored
                if not h.get("auto") or h["fit"].score >= 0.1]
    if not hotspots:
        return {"status": "no_doable_hotspots"}  # 有热点但都不适配网盘拉新

    # 多平台共振:微博/百度新上榜交叉验证,共振热点加权上浮(输入去单一化)
    _resonance(db, user_id, hotspots)
    # **资源库证据**(2026-10-04):库里有没有这个资源、我方有没有现成的链。
    # 与"榜单共振"同一套加权,但依据不同 —— 榜单是"大家在讨论",这里是"**已经有人在发**"。
    _library_evidence(db, user_id, hotspots)
    # **跨平台资源同现**(2026-10-05):同一个资源在几个平台被贴过 —— 目前能拿到的**最硬**的需求证据。
    # 榜单是"大家在讨论",这一档是"**有人已经在不止一处靠它拉新**"。
    _cross_platform_evidence(db, user_id, hotspots)

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
            category=str(h.get("category") or "")[:16],
            opportunity=float(h.get("opportunity") or 0))
        db.add(row)
        db.flush()
        row_ids[h["keyword"]] = row.id
    for kw, plan in plan_by_kw.items():
        h = by_kw.get(kw, {"growth": 0, "platforms": "douyin"})
        row = HotspotSuggestion(
            user_id=user_id, keyword=kw, growth=h.get("growth", 0), kind="llm",
            resource_title="", link="", plan=_plan_text(plan),
            platforms=str(h.get("platforms") or "douyin"),
            category=str(h.get("category") or "")[:16],
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
                f"{_resonance_tag(h)}{_window_tag(h)}{_evidence_tag(h)}{comp_tag} → 已有现成资源:"
                f"「{art.title[:40]}」({_safe_author(art.author, settings)})"
                + (f" [{why}]" if why and why != "标题字面命中" else "")
                + risk_tag
                + (f" → 点这:{link}" if link else ""))
        entries.append((float(h.get("opportunity") or 0), line,
                        f"【{art.title}】\n{link}" if link else ""))
    for kw, plan in plan_by_kw.items():
        h = by_kw.get(kw, {})
        entries.append((float(h.get("opportunity") or 0),
            f"💡[# {row_ids[kw]}] {kw}{_resonance_tag(h)}{_window_tag(h)}{_evidence_tag(h)}"
            f"\n{_fit_line(h)}{_plan_text(plan)}",
                        ""))
    entries.sort(key=lambda x: -x[0])
    # **证据体检行**(2026-10-05):把"这一轮各档证据各触发几次"摊开 ——
    # ⚠️ 恒为 0 的那几档是"接了但没生效"的信号(数据源没接上 / 阈值太高),不是"今天恰好没有"。
    tally = _evidence_tally(fresh)
    lines = ["🎯 优先发货(机会分 top):", tally]
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
        detail="近 24h 监控词热度达标,可执行动作:\n" + "\n".join(lines)[:2000]
        + "\n💡 做完之后什么都不用做——系统会自动识别发文并结算这条建议带了多少拉新",
        settings=settings, push_feishu=False)
    return {"status": "ok", "hotspots": len(fresh), "matched": len(matched),
            "llm": len(plan_by_kw), "notified": len(matched) + len(plan_by_kw),
            # 把"各档证据各触发几次"带出去 —— 调用方写进运行记录,链路体检就能读到
            # (不用等推送;而且**恒为 0 的档**一眼可见,那是"接了但没生效"的信号)
            "evidence_tally": tally}


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
                    # ⚠️ **记运行记录**(2026-10-05 补):这个作业此前**一行记录都不留**,
                    # 于是链路体检看不见它、也看不见"哪档证据从没触发过"。
                    # 把证据触发统计写进 detail ⇒ 随手跑一次体检就能看出哪档死了。
                    from app.services.tenant_base import _record_run
                    _record_run(db, uid, "hotspot_agent", "success",
                                f"热点{out.get('hotspots', 0)} 现成资源{out.get('matched', 0)} "
                                f"选题{out.get('llm', 0)} | {out.get('evidence_tally', '')}")
                db.commit()
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("热点选题 Agent 失败 user=%s", uid)
    finally:
        db.close()
    return total


# ---------------------------------------------------------------- 建议结算(v5, 2026-09-29)

def _article_covers_link(art: WechatArticle, link: str) -> bool:
    """发文是否包含该盘链(my_pan_urls/pan_urls 按行拆分精确匹配)。"""
    if not link:
        return False
    links = {x.strip() for x in
             ((art.my_pan_urls or "") + "\n" + (art.pan_urls or "")).splitlines() if x.strip()}
    return link in links


def _auto_mark_acted(db: Session, user_id: int) -> int:
    """从监听数据自动推断"建议已被执行",替代人工标记(2026-09-30)。

    运营者把建议下发后,实际发文的是员工——运营者不知道员工发没发,人工 acted
    标记必然断链(2026-09-30 用户明确)。发文会留下客观数据,据此反推:
    - match 类(带盘链):新文章覆盖同一条盘链 = 有人用它发了文 → acted;
    - llm 类(拉新选题,无链接):建议之后入库的文章标题命中热点词 → acted(标题级宽松)。
    acted_at 取命中文章的入库时间,即结算的扩散基线。只回看近 14 天,返回自动标记数。
    """
    cutoff = datetime.now() - timedelta(days=14)
    pending = db.scalars(select(HotspotSuggestion).where(
        HotspotSuggestion.user_id == user_id,
        HotspotSuggestion.acted.is_(False),
        HotspotSuggestion.created_at >= cutoff)).all()
    if not pending:
        return 0
    articles = db.scalars(select(WechatArticle).where(
        WechatArticle.user_id == user_id,
        WechatArticle.created_at >= cutoff)).all()
    marked = 0
    for sug in pending:
        made_at = sug.created_at
        hit = None
        if sug.link:
            hit = next((a for a in articles if a.created_at and made_at
                        and a.created_at >= made_at
                        and _article_covers_link(a, sug.link)), None)
        elif sug.keyword and sug.keyword.strip():
            kw = sug.keyword.strip()
            hit = next((a for a in articles if a.created_at and made_at
                        and a.created_at >= made_at
                        and kw in (a.title or "")), None)
        if hit:
            sug.acted, sug.acted_at = True, hit.created_at
            marked += 1
    return marked


def settle_suggestions(db: Session, user_id: int) -> dict:
    """结算已发建议的效果(v5 结算端,2026-09-29 去 dajiala 版)。

    信号体系(全部免费/自有):
    - 归因: acted 建议 → 按盘链精确匹配发文(不做标题模糊,宁少样本不脏样本);
    - repost_gain = 发文后(acted_at 起)全网新增的该文盘链记录数(wechat_pan_links
      随时间增长 = 资源被疯转 = 需求被反复验证)——**结算主信号**;
    - **reads_gain 已复活**(2026-10-04):当初停用是因为"dajiala 放弃后无免费阅读数源",
      而现在**有了** —— 微信读书列表接口带精确 `readNum`,列表额度轮转也修好了。
      口径:走 `conversion` 单一事实源(**公众号第一原则 = 阅读数 × 30%**)。
      ⚠️ 它是**需求侧预估**(乘的是这条文的阅读数),**不是我方实收**;
      ⚠️ `read_num <= 0` 时**不写**(0 表示"没采到",不是"没人读")——
         阅读数受列表额度限制(每轮 25 个号、~2 天轮一圈),没轮到的号就是没有;
    - 总账对账: pan_recruit_weekly(拉新周录,人工)。
    可重复结算: repost_gain 随盘链扩散刷新。
    """
    from app.db.models import WechatPanLink
    from app.services import conversion

    auto = _auto_mark_acted(db, user_id)
    if auto:
        logger.info("建议执行自动归因 user=%s:新增 acted=%s(发文数据反推,无需人工标记)", user_id, auto)

    rows = db.scalars(select(HotspotSuggestion).where(
        HotspotSuggestion.user_id == user_id,
        HotspotSuggestion.acted.is_(True),
        HotspotSuggestion.link != "")).all()
    articles = db.scalars(select(WechatArticle).where(
        WechatArticle.user_id == user_id,
        WechatArticle.created_at >= datetime.now() - timedelta(days=30))).all()
    settled, attributed = 0, 0
    now = datetime.now()
    for sug in rows:
        art = next((a for a in articles if _article_covers_link(a, sug.link)), None)
        if art is None:
            continue          # 还没归因到发文,等下一轮(发文后录入即自动接上)
        sug.article_id = art.id
        baseline = sug.acted_at or sug.created_at
        repost = db.scalar(select(func.count(WechatPanLink.id)).where(
            WechatPanLink.user_id == user_id,
            WechatPanLink.article_id == art.id,
            WechatPanLink.created_at >= baseline)) or 0
        sug.repost_gain = int(repost)
        # **reads_gain 复活**(2026-10-04):阅读数 × 30%(公众号第一原则,走 conversion)。
        # ⚠️ `read_num <= 0` 保持原值不动 —— 0 是"这一轮没采到"(列表额度轮转),
        #    不是"没人读";写成 0 会让"没采样"和"热度为零"混成一样(本仓的老毛病)。
        reads = int(getattr(art, "read_num", 0) or 0)
        if reads > 0:
            est = conversion.estimate("wechat", {"read_num": reads})
            if est["estimate"] is not None:
                sug.reads_gain = int(est["estimate"])
        sug.settled_at = now
        settled += 1
        attributed += 1
    db.commit()
    # 按品类聚合"哪类真赚"(P1 转化回流,2026-10-01):repost_gain 是盘链扩散的代理指标,
    # 聚合后给运营看"该往哪个品类加码",也是未来自动调 PROVEN_CATEGORIES 权重的数据源。
    # 2026-10-04 起再加一列 **reads_gain**(阅读数×30% 的预估拉新量)—— 与 repost_gain
    # **并列**而不是替代:一个是"资源配置被疯转",一个是"预估触达",口径不同、互相印证。
    by_cat: dict[str, dict] = {}
    for sug in rows:
        if sug.article_id is None:
            continue
        cat = (sug.category or "未分类")[:16]
        e = by_cat.setdefault(cat, {"n": 0, "repost_gain": 0, "reads_gain": 0})
        e["n"] += 1
        e["repost_gain"] += int(sug.repost_gain or 0)
        e["reads_gain"] += int(sug.reads_gain or 0)
    return {"status": "ok", "acted_with_link": len(rows), "settled": settled,
            "attributed": attributed, "auto_acted": auto, "by_category": by_cat}


def settle_suggestions_all_users(settings: Settings | None = None) -> int:
    """计划任务入口:对全部启用用户各结算一轮。"""
    from app.db.database import get_session_local
    from app.db.models import User

    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                out = settle_suggestions(db, uid)
                if out.get("settled"):
                    total += out["settled"]
                db.commit()
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("建议结算失败 user=%s", uid)
    finally:
        db.close()
    return total


def _auto_transfer(session, user_id: int, pan_url: str, settings) -> str:
    """把别人的原始链转存成**我方分享链**;任何失败都返回空串(文案照常生成,只是没我方链)。

    仅夸克(百度转存是另一套实现,按需再接)。触发频率低——`generate_draft` 按需调用,
    不会像监听轮的批量转存那样打风控。
    """
    from app.services.cookie_store import get_cookie
    from app.services.quark_transfer import QuarkTransfer

    ck = get_cookie(session, user_id, "quark")
    if not ck:
        logger.info("自动转存跳过:未配夸克 Cookie")
        return ""
    try:
        quark = QuarkTransfer(ck, fid_store=getattr(settings, "quark_fid_store", "") or None)
        res = quark.transfer_and_share(pan_url,
                                       save_dir=getattr(settings, "quark_save_dir", "") or "/来自选题",
                                       password=getattr(settings, "quark_share_password", "") or "")
        out = str(res.get("share_url") or "")
        logger.info("现成资源已自动转存:%s → %s", pan_url[:44], out[:44])
        return out
    except Exception as exc:  # noqa: BLE001 - 转存失败不挡文案生成
        logger.warning("现成资源自动转存失败 %s:%s", pan_url[:44], exc)
        return ""


def generate_draft(session, user_id: int, suggestion_id: int, settings=None) -> dict:
    """按建议生成可直接发布的公众号文案(v2.5.0 发布最后一公里,按需调用省成本)。

    输入取建议的教学字段(keyword/plan/link);若建议资源在资源库已有我方转存链,
    自动带上(文案里可直接挂链)。结果落 HotspotSuggestion.draft 供复用。
    返回 {"status", "titles", "content"};LLM 未配/失败返回 {"status": "failed"}。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.services import llm_client

    sug = session.get(HotspotSuggestion, suggestion_id)
    if sug is None or sug.user_id != user_id:
        return {"status": "not_found"}
    if not getattr(settings, "deepseek_api_key", ""):
        return {"status": "no_llm_key"}

    # 我方链:建议自带 link 优先;否则按关键词去资源库找 —— 有我方转存链直接用;
    # 只有别人的原始链时,**当场转存成我方链**再进文案(2026-10-01 用户要求:
    # "有现成的网盘资源先保存到我自己的网盘里面然后再转存出推送")。
    my_link, raw_link = "", ""
    try:
        from app.services.resource_library import search_resources

        for r in search_resources(session, user_id, sug.keyword, limit=3):
            if not my_link and r.get("my_link"):
                my_link = r["my_link"]
            if not raw_link and r.get("pan_url"):
                raw_link = r["pan_url"]
            if my_link and raw_link:
                break
    except Exception:  # noqa: BLE001 - 资源库查询失败不挡文案生成
        logger.debug("文案生成查资源库失败", exc_info=True)
    if not my_link and raw_link:
        my_link = _auto_transfer(session, user_id, raw_link, settings)

    out = llm_client.draft_article(
        settings.deepseek_base_url, settings.deepseek_api_key, settings.deepseek_model,
        hotspot=sug.keyword or "", resource=(sug.plan or "")[:400], my_link=my_link)
    if not out:
        return {"status": "failed"}
    titles, content, keyword = out["titles"], out["content"], out.get("keyword", "")
    # 合规前置(P2,2026-10-01):盗版影视/付费课程是唯一会"账号说没就没"的红线——
    # resource_risk 词表原本只在选题侧用,这里在**文案落地后**再过一遍:
    # 高危内容当场标警示,让运营决定要不要发/怎么改,而不是发出去被平台清了才知道。
    risk_level, risk_why = resource_risk(" | ".join(titles) + " " + content[:400])
    if risk_level == "high":
        content = (f"⚠️ 合规提醒:命中盗版高危信号({risk_why}),发布前请确认授权状态,"
                   "或改以「教程/清单」形式规避。\n\n") + content
    # 公众号 SEO 模式(2026-10-01 依调研改,见 doc/pan-promotion-channels.md):
    # **正文不挂链**——公众号带外链会影响微信收录与排名;链接与自动回复关键词另起一段
    # 交给运营,由他在后台配「关键词回复」。老行为(文末直接附链接)已废。
    block = [" | ".join(titles), "", content]
    if keyword or my_link:
        block += ["", "——— 公众号配置(正文里不要放链接)———"]
        if keyword:
            block.append(f"① 自动回复关键词:{keyword}")
        if my_link:
            block.append("② 该关键词的回复内容:" + chr(10) + "📦 资源链接:" + chr(10) + my_link)
    sug.draft = chr(10).join(block)[:8000]
    session.commit()
    return {"status": "ok", "titles": titles, "content": content, "my_link": my_link}
