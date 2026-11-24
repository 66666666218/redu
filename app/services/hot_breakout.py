# -*- coding: utf-8 -*-
"""**破圈前预警**(2026-11-24) —— 在**同行还没入局**时就把热点抓出来。

## 用户口径与它要解决的那次差距

> 「热点**刚破圈**、或者**马上就要破圈的前夕**、**还没有同行入局**的时候能实现吗 ——
> 就像昨天那个《斗破苍穹》，几个小时到顶；还有《麦穗与🐏》。怎样能在火之前抓到？
> **等同行们都一起发的时候再跟风往往都晚了**」

实测出来的两次差距(都以真实库回放,不是估的):

**斗破苍穹(11-22)**
```
11:05  ithome #3  「九年长篇今日完结：国漫《斗破苍穹》剧场版《双帝之战》定档午间 12 点开播」
12:05  kuaishou #5  ← 第 2 个平台出现 = 破圈已经发生
       ↑ 这一刻,库里推过「斗破」的同行 **0 人**(台账 + 公众号两个源都是 0)
20:15  我们才推第一条 douyin_peer(还没链);11-23 04:13 才第一次推出带链卡
```
⇒ **我们从 11:05 就握着这条数据(本机 `hot_source_items`),却 17 小时后才动。**

**麦穗与羊(11-17)**:00:38 贴吧首见 → 04:03 库里 3 个号发文 → 04:16 我们推了;
热搜侧直到 11-18 18:05 才首现 —— **比我们晚 38 小时**。它靠**公众号监听**赢的,
所以本模块**不替代**任何东西,只补「热榜侧的先手」。

## 为什么现有链路抓不到(是判据错位,不是没数据)

| 链路 | 判据 | 问题 |
|---|---|---|
| `promo_forecast` | 头 24h 攒够 **4 个推手** + 没链 | **4 个推手 = 同行已经入局**,那是跟风信号;且要等 24h 窗口走完 |
| `hotspot_agent` | 热榜 → LLM 选题 | **只跑远程**(role=hotspot),本机 `hotspot_suggestions` 停在 10-01 |
| `early_agent` | 微博/百度榜单增速 | 只跑远程;本机 `baidu_hot_items` **0 行** |
| `events.event_assign` | HotspotEvent 聚类 | 只跑远程 |

## 两条判据(拿真实库回放出来的,不是拍的)

### A 预告档 —— 精确命中斗破那一条
源 ∈ 内容型源 ∧ 标题含 `《作品名》` ∧ 含预告词(`定档/开播/上线/首播/官宣/即将/…`)。
**不设名次闸**:预告稿常常不在榜首(斗破 #3;实测一堆在 #8~#18)。
15 天回放去重 **31 个作品 = 2.1 条/天**,逐条核对**全是真作品**
(仙剑四 / 三体短篇集 / 异人之下 / 复仇者联盟5 / GTA6 / 灵笼 / 泥面人 …)。
剔除:书名号内命中政策类词(`白皮书/规划/报告/条例/…`)⇒ 那多半是文件名不是作品名。

### B 起势档
源 ∈ 内容型源 ∧ 标题含 `《作品名》` ∧ 首见在 `first_hours`(默认 6h)内 ∧ `rank ≤ 15`。
15 天回放 **99 条 = 6.6 条/天**(rank≤10 时 5.5/天)。

> ⚠️ **反例记下来**:纯按「标题跨平台 ≥2 平台」的规则会**同时漏掉斗破和麦穗** ——
> 各平台标题措辞完全不同(`斗破苍穹双帝之战开播` vs `九年长篇今日完结：国漫《…》定档…`),
> **只有书名号里的作品名是一致的**。所以身份取 `《…》`,不取标题。

## 「同行入局」怎么数

语料 = 最近 `peer_hours`(默认 24h)的 `push_ledger` **+** `wechat_articles`,
按作品名归一 key 精确匹配,数**不同作者**。

**为什么两个源都要**:`push_ledger` 只覆盖"推出去的"(抖音/微博/热榜卡片),
而公众号监听的文章不进台账 —— 麦穗那次先手正是先在 `wechat_articles` 看出来的。

**为什么是 24h 窗口不是 7 天**:斗破 @11-22 11:05 用 7 天窗口数出来 **4 人**
(其中 1 个是 11-18 发的完结感慨,与剧场版开播无关),24h 窗口是 **0 人** ——
24h 才是「此刻还有人在推这个」的正确口径。

⚠️ 卡片文案必须写「**库内**同行」,不能写「全网没人发」—— 我们只看得到自己库里的。

## 与 `promo_forecast` 的分工(别把两条合成一条)

`promo_forecast` 问「**已经有很多人在推的东西值不值得搬**」(24h 攒 4 人,精确率 41%)。
本模块问「**还没人推的东西是不是马上要炸**」。前者是**跟风的最优解**,后者是**先手**。
两档冷却**各一把锁、故意不共用**:斗破那种「11:05 预告 → 12:05 起势」是**两个不同事实**,
第二张卡带的是「它真的开始起量了」—— 共用一把锁会把最有价值的那一步吞掉。

## 去向与冷却

**管理群**(`webhook_for(settings, "admin")`)—— 运营情报,不进客户群(与 `promo_forecast` 同)。
每条**发送成功后**才落冷却行(理由见 `push_surge._recently_alerted`:pysqlite 上先占位再发,
一次抖动就吃掉整条提醒)。冷却默认 48h。

被各道闸挡下的**不静默丢**:每个数字都进运行记录(`已有链/套话/同行已入局/冷却`),
否则「今天没预警」与「作业看不见数据」在库里长得一样。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: **内容型源** —— 能出"作品"的榜。排除财经/时政线(jin10 / fastbull / cls /
#: wallstreetcn / gelonghui / cankaoxiaoxi / ifeng / thepaper / xueqiu / 36kr):
#: 它们天天命中「《…白皮书》《…规划》」,是噪音源而不是漏报源。
CONTENT_SOURCES: frozenset[str] = frozenset({
    "tieba", "kuaishou", "bilibili", "bilibili-hotsearch", "zhihu", "douban", "hupu",
    "qqvideo-tv-hotsearch", "iqiyi", "tencent-hot", "toutiao", "coolapk", "steam",
    "aihot", "sspai", "juejin", "nowcoder", "github-trending-today", "producthunt",
    "ithome", "solidot", "dongqiudi",
})

#: **预告词** —— 出现它说明"还没上线/刚上线",正是先手的时刻。
#: 15 天回放 31 个作品逐个核对过,没有误报(与 `promo_forecast` 的 CTA 词不同,
#: 这里不需要"剥词后判空"那一套:作品名本身来自 `《》`)。
PRE_WORDS = re.compile(
    r"(定档|开播|上线|首播|官宣|即将|首发|预售|抢先|片花|预告|完结|大结局)")

#: 书名号里出现这些 ⇒ 是**文件名/政策文件**,不是作品名。
POLICY_WORDS = re.compile(
    r"(展望|白皮书|蓝皮书|公报|条例|办法|规定|意见|通知|规划|纲要|宣言|法案|"
    r"决议|协议|声明|条约|谅解|目录|标准|规范|合同|报告)")

_BRACKET_RE = re.compile(r"《([^》]{2,30})》")

#: 档位名(进冷却键;**两档各一把锁**,理由见模块头注)
TIER_PRE = "预告"
TIER_RISE = "起势"


def _plat(src: str) -> str:
    """源 id → 中文名。**复用 `hot_sources._PLAT_LABEL`**(那里已经有一份全的),
    认不出就原样返回 —— 别让一个没登记的源导致卡片发不出去。"""
    from app.services.hot_sources import _PLAT_LABEL

    return _PLAT_LABEL.get(str(src or ""), str(src or ""))


def _names_in(title: str) -> list[tuple[str, str]]:
    """标题里的 `《…》` → `[(原文名, 归一key)]`(去重保序)。

    ⚠️ **key 一律走 `resource_key`** —— 与下面 `_library_rows` 同一把尺子。
    两处用不同的归一(比如一边 `match_key` 一边 `resource_key`)会让同行计数**恒为 0**,
    而且它不报错:卡片照发,只是永远写着「还没人入局」。本仓母题,别踩。
    """
    from app.services.resource_library import resource_key

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in _BRACKET_RE.findall(str(title or "")):
        raw = raw.strip()
        if not raw or POLICY_WORDS.search(raw):
            continue
        k = resource_key(raw)
        if k and k not in seen:
            seen.add(k)
            out.append((raw, k))
    return out


def _primary_name(title: str) -> tuple[str, str] | None:
    """一条热榜标题 → **它真正在说的那个作品** `(原文名, key)`,取**最后一个**书名号。

    ## 为什么不是取第一个,也不是全都报

    中文标题里主作品**在后面**,前面那些书名号常是"原班人马/前作/品牌":
      · `国漫《斗破苍穹》剧场版《双帝之战》定档…`        ⇒ 主 = 双帝之战
      · `集结《龙之谷：破晓奇兵》《精灵王座》原班人马，…《西游记之真假美猴王》定档` ⇒ 主 = 西游记
      · `《英雄联盟》S16 中国 LPL 纪录片《风起今朝》12 月播出`                 ⇒ 主 = 风起今朝

    而"全都报"会让**同一条标题发两张卡**(斗破苍穹 + 双帝之战)。实测第一次回放就是这样。

    ⚠️ 同行的语料**仍用全部书名号**(`_names_in`)—— 有人推前作也算在推这个 IP;
    但**候选身份**只认主名,这样「还有谁在推**这个作品**」才不会被前作的人数污染。
    """
    names = _names_in(title)
    return names[-1] if names else None


def _library_rows(session: Session, user_id: int, since: datetime, until: datetime
                  ) -> list[tuple[str, str, bool]]:
    """`[since, until]` 内的 (作者, 标题, 是否已有我方链) —— 台账 **+** 公众号文章。

    ⚠️ **必须有上界 `until`**:这是"回放到某一时刻"的唯一保证。只有下界的话,
    `now` 之后的行会被算进来 —— 回放 11-22 时会看到 11-24 的"首见",
    B 档判据(首见 ≤6h)整条失真。第一次回放就踩到了(那条卡片写着「首见 11-24」)。

    合成一次是为了省一遍全表:两张表都是万级(实测台账 8.9k / 文章 14.1k)。
    归一化在 Python 侧做(`resource_key` 是纯函数,SQL 里调不了 ——
    与 `search_resources` 同一做法)。
    """
    from app.db.models import PushLedger, WechatArticle

    rows: list[tuple[str, str, bool]] = []
    for author, title, pan_url in session.execute(
            select(PushLedger.author, PushLedger.title, PushLedger.pan_url)
            .where(PushLedger.user_id == user_id,
                   PushLedger.pushed_at >= since,
                   PushLedger.pushed_at <= until)).all():
        rows.append((str(author or "").strip(), str(title or ""),
                     bool(str(pan_url or "").strip())))
    for author, title, my_pan in session.execute(
            select(WechatArticle.author, WechatArticle.title, WechatArticle.my_pan_urls)
            .where(WechatArticle.user_id == user_id,
                   WechatArticle.created_at >= since,
                   WechatArticle.created_at <= until)).all():
        rows.append((str(author or "").strip(), str(title or ""),
                     bool(str(my_pan or "").strip())))
    return rows


#: 同行语料的一行:`(作者, 归一后的标题, 是否已有我方链)`
_CorpusRow = tuple[str, str, bool]


def _corpus(rows: list[tuple[str, str, bool]]) -> list[_CorpusRow]:
    """台账/文章行 → **归一后**的语料行(见 `_peer_count`)。"""
    from app.services.resource_library import match_key

    out: list[_CorpusRow] = []
    for author, title, has_own in rows:
        k = match_key(title)
        if k:
            out.append((author, k, has_own))
    return out


def _peer_count(corpus: list[_CorpusRow], name_key: str) -> tuple[int, bool]:
    """`(推过它的不同作者数, 我们是否已有我方链)`。

    ## ⚠️ 必须按**归一标题的包含**判,不能只认 `《…》`

    实测(2026-11-24):公众号里推斗破的标题是
    `斗破苍穹剧场版 双帝之战 高清4K（速存）` —— **一个书名号都没有**。
    只按书名号取名字的话,同行计数**恒为 0**,卡片永远写着「还没人入局」——
    而它不报错,是本仓最贵的那类静默失败。

    ⚠️ 判据是 `name_key in 标题key`(裸包含)。这里**可以**放宽:
    我们要答的是「库里**有没有人在说这个**」,不是「这条链是不是它」——
    后者才需要 `title_claims` 那道更严的护栏(见 `resource_library.title_claims`)。
    """
    authors: set[str] = set()
    owned = False
    for author, title_key, has_own in corpus:
        if name_key not in title_key:
            continue
        if author:
            authors.add(author)
        if has_own:
            owned = True
    return len(authors), owned


def _hot_rows(session: Session, user_id: int, sources: set[str],
              since: datetime, until: datetime) -> list[tuple[str, int, str, datetime]]:
    """`[since, until]` 内内容型源的**全部**条目(不是只取最新一轮)。

    ⚠️ 要**整个 lookback 窗口**而不是最新快照:判「首见」必须有历史;
    只取最新一轮的话每条都算"刚刚首见",B 档会变成「凡书名号都报」。
    ⚠️ **上界 `until` 同样必须**(理由见 `_library_rows`)。
    """
    from app.db.models import HotSourceItem

    return list(session.execute(
        select(HotSourceItem.source, HotSourceItem.rank, HotSourceItem.title,
               HotSourceItem.captured_at)
        .where(HotSourceItem.user_id == user_id,
               HotSourceItem.source.in_(sources),
               HotSourceItem.captured_at >= since,
               HotSourceItem.captured_at <= until)
        .order_by(HotSourceItem.captured_at.asc())).all())


def detect_breakouts(session: Session, user_id: int, *, rank_top: int = 15,
                     first_hours: float = 6, lookback_hours: float = 12,
                     history_hours: float = 168, peer_hours: float = 24,
                     max_peer_authors: int = 1,
                     top: int = 5, now: datetime | None = None,
                     sources: set[str] | None = None,
                     stats: dict | None = None) -> list[dict]:
    """找出**还没同行入局**的新热点(纯读)。按 (同行人数升序, 名次升序, 年龄升序) 排。

    每条:`{tier, name, raw, source, rank, first, age_h, platforms, peer_authors}`。
    """
    from app.services.promo_forecast import is_cta

    now = now or datetime.now()
    srcs = set(sources or CONTENT_SOURCES)
    hist = _hot_rows(session, user_id, srcs,
                     now - timedelta(hours=float(lookback_hours)), now)
    #: 每个源**各自**的最新一轮:只有它上面的行才参与"当前名次"判定。
    #: ⚠️ **不能取全局 max** —— 各源抓取时刻并不完全对齐(慢一拍的源会整源消失,
    #: 实测 zhihu 停在 :05、ithome 到了 :10 时,zhihu 那批候选**一条都进不来**)。
    #: 也不能要"整窗口"的行:那样每条都算"刚刚首见",B 档会变成「凡书名号都报」。
    latest_by_src: dict[str, datetime] = {}
    for _src, _rank, _title, _cap in hist:
        cur = latest_by_src.get(_src)
        if cur is None or _cap > cur:
            latest_by_src[_src] = _cap
    latest = max(latest_by_src.values(), default=None)
    if latest is None:
        if stats is not None:
            stats.update({"rows": 0, "works": 0, "announce": 0, "rising": 0,
                          "owned": 0, "cta": 0, "peer": 0, "cand": 0})
        return []

    corpus = _corpus(
        _library_rows(session, user_id, now - timedelta(hours=float(peer_hours)), now))

    #: 作品 key 的**首见时刻**:在 `history_hours`(**远长于** `lookback_hours`)里最早一次。

    #: ⚠️⚠️ **必须用长历史窗,不能只用 lookback 那个 12h**(2026-11-24 回放实测):
    #: 资源是**反复进出榜**的 —— `影之刃零` 11-18 进榜→掉出→11-20 又进,用 12h 窗口看,
    #: 第二次就是"全新的"⇒ 两天内为同一个资源发两张「刚起势」卡。
    #: 这与 `promo_forecast` 头注里记的是同一个坑(短窗口会把老资源当成"新出现的"),
    #: 那边给的修法就是「首见必须在一个**远长于判据**的窗口里算」—— 这里照抄。
    long_hist = (hist if float(history_hours) <= float(lookback_hours) else
                 _hot_rows(session, user_id, srcs,
                           now - timedelta(hours=float(history_hours)), now))
    first_seen: dict[str, datetime] = {}
    for src, rank, title, cap in long_hist:
        for _raw, key in _names_in(title):
            if key not in first_seen or cap < first_seen[key]:
                first_seen[key] = cap

    groups: dict[str, dict] = {}
    n_announce = n_rising_rows = 0
    for src, rank, title, cap in hist:
        if cap != latest_by_src.get(src):
            continue                    #: 名次只认**该源**最新一轮;历史行只用来算首见
        #: ⚠️ 候选身份只认**主名**(最后一个书名号)。"全报"会让同一条标题发两张卡
        #: (斗破苍穹 + 双帝之战),第一次回放实测就是这样。
        primary = _primary_name(title)
        if primary is None:
            continue
        raw, key = primary
        is_pre = bool(PRE_WORDS.search(str(title or "")))
        if is_pre:
            n_announce += 1
        g = groups.get(key)
        if g is None:
            groups[key] = g = {"name": key, "raw": raw, "source": src,
                               "rank": int(rank or 0), "first": first_seen[key],
                               "platforms": set(), "tier": ""}
        g["platforms"].add(src)
        if is_pre:
            #: 同一作品只要**有一行**带预告词就算 A 档(预告稿可能只在一个源发)
            g["tier"] = TIER_PRE
        if int(rank or 0) and 0 < int(rank) < (g["rank"] or 10 ** 6):
            g.update({"source": src, "rank": int(rank), "raw": raw})

    n_work = 0
    cands: list[dict] = []
    for key, g in groups.items():
        age_h = max(0.0, (now - g["first"]).total_seconds() / 3600.0)
        tier = g["tier"]
        if not tier:
            #: B 起势档:必须**还很新** ∧ **已经爬到前面** —— 两个条件缺一不可
            #: (只新不前面 = 刚冒头没起量;只前面不新 = 老话题,那是 `promo_forecast` 的活)
            if age_h <= float(first_hours) and 0 < g["rank"] <= int(rank_top):
                tier = TIER_RISE
                n_rising_rows += 1
        if not tier:
            continue
        n_work += 1
        g["tier"], g["age_h"] = tier, age_h
        g["peer_authors"], g["owned"] = _peer_count(corpus, key)
        cands.append(g)

    n_owned = n_cta = n_peer = 0
    out: list[dict] = []
    for g in cands:
        if g["owned"]:
            n_owned += 1
            continue
        if is_cta(g["name"]):
            n_cta += 1
            continue
        if g["peer_authors"] > int(max_peer_authors):
            n_peer += 1
            continue
        g["platforms"] = sorted(g["platforms"])
        out.append(g)
    out.sort(key=lambda x: (x["peer_authors"], x["rank"] or 10 ** 6, x["age_h"]))
    if stats is not None:
        stats.update({"rows": len(hist), "works": n_work, "announce": n_announce,
                      "rising": n_rising_rows, "owned": n_owned, "cta": n_cta,
                      "peer": n_peer, "cand": len(out),
                      "snapshot": str(latest)[:16]})
    return out[:int(top)] if top else out


def _line_of(a: dict) -> str:
    """一条 → 卡片里的一行。

    ⚠️ 平台只在**≥2 个**时才单列 —— 单平台时它和上一行的来源重复
    (第一版每条都多一行 `🗂 IT之家`,一眼看去像两条信息其实是一条)。
    """
    icon = "⚡" if a["tier"] == TIER_PRE else "📈"
    age = a["age_h"]
    age_txt = "刚刚" if age < 1 else f"{age:.1f}h 前"
    n = a["peer_authors"]
    peer_txt = "库内同行 **0 人** · 还没人入局" if n == 0 else f"库内同行 {n} 人"
    plats = sorted(_plat(s) for s in a["platforms"])
    where = f"{plats[0]}#{a['rank']}" if len(plats) <= 1 else \
        f"{len(plats)} 平台({'/'.join(plats[:4])})·最佳#{a['rank']}"
    return (f"{icon} **{a['raw'][:40]}** — {where} · 首见 {age_txt} · {peer_txt}")


def _render(alerts: list[dict], nl: str) -> str:
    """**一轮一张汇总卡** → 飞书纯文本。

    ## 为什么是一张而不是一条一张

    实测产量 A 档 2.1 条/天 + B 档 6.6 条/天 ≈ **9 条/天**。逐条发就是往管理群
    每小时塞一条、一天塞九条的刷屏 —— 而 `promo_forecast` 那种逐条卡之所以还行,
    是因为它一天只有几条。**先手信息要的是「一眼扫完今天该动手的」**,不是九次打扰。

    代价:整卡发失败时,**所有条目都不落冷却** ⇒ 下一小时原样重试 —— 那正是想要的
    (fail-open;宁可重报,不要静默丢)。
    """
    pre = [a for a in alerts if a["tier"] == TIER_PRE]
    rise = [a for a in alerts if a["tier"] == TIER_RISE]
    lines = [f"🔥 **破圈前预警** · {len(alerts)} 个还没同行入局的热点"]
    for group, head in ((pre, "⚡ 预告档(还没上线/刚上线)"),
                        (rise, "📈 起势档(刚进榜且还很新)")):
        if not group:
            continue
        lines.append("")
        lines.append(f"**{head}**")
        lines.extend(_line_of(a) for a in group)
    lines.append("")
    lines.append("— 全部**还没有自有链**;同行数是**我们库内**的读数(台账+公众号),"
                 "不代表全网")
    return nl.join(lines)


def _recently_alerted(session, user_id: int, key: str, cooldown_hours: float) -> bool:
    """**纯读**预检:这条(档位+作品)在冷却期内发过没有(语义同 `promo_forecast`)。"""
    from app.db.models import FeishuAlert

    row = session.scalar(select(FeishuAlert).where(
        FeishuAlert.section == "hot_breakout",
        FeishuAlert.user_id == user_id,
        FeishuAlert.title == key[:200]))
    if row is None:
        return False
    return (datetime.now() - row.alerted_at) < timedelta(hours=float(cooldown_hours))


def _mark_alerted(session, user_id: int, key: str, cooldown_hours: float,
                  reason: str) -> None:
    """**发送成功之后**才落冷却行。写失败只影响去重,不抛。"""
    from app.services.alert_service import feishu_alert_gate

    try:
        feishu_alert_gate(session, user_id, "hot_breakout", key, cooldown_hours, reason)
        session.commit()
    except Exception:  # noqa: BLE001 - 冷却没落盘只影响去重
        logger.warning("破圈预警冷却门写入失败 %s", key, exc_info=True)
        session.rollback()


def _record(session, user_id: int, status: str, detail: str) -> None:
    """运行记录失败不挡业务(与 `promo_forecast` 同一处理)。"""
    try:
        from app.services.tenant_base import _record_run

        _record_run(session, user_id, "hot_breakout", status, detail)
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.warning("破圈预警运行记录失败 user=%s", user_id, exc_info=True)


def _int(settings, name: str, default: int) -> int:
    v = getattr(settings, name, default)
    return int(default if v is None else v)


def _float(settings, name: str, default: float) -> float:
    v = getattr(settings, name, default)
    return float(default if v is None else v)


def hot_breakout_tick(settings=None) -> int:
    """定时入口(每小时 :42,本机 wechat 角色):还没同行入局的新热点 → **管理群**。"""
    from config.settings import get_settings

    settings = settings or get_settings()
    if not getattr(settings, "hot_breakout_enabled", True):
        return 0
    from app.db import get_session_local
    from app.db.models import User
    from app.services.feishu_client import FeishuClient, webhook_for

    hook = webhook_for(settings, "admin")
    if not hook:
        return 0
    srcs = {s.strip() for s in str(
        getattr(settings, "hot_breakout_content_sources", "") or "").split(",") if s.strip()}
    kw: dict = {
        "rank_top": _int(settings, "hot_breakout_rank_top", 15),
        "first_hours": _float(settings, "hot_breakout_first_hours", 6),
        "lookback_hours": _float(settings, "hot_breakout_lookback_hours", 12),
        "history_hours": _float(settings, "hot_breakout_history_hours", 168),
        "peer_hours": _float(settings, "hot_breakout_peer_hours", 24),
        "max_peer_authors": _int(settings, "hot_breakout_max_peer_authors", 1),
        "top": _int(settings, "hot_breakout_top", 5),
    }
    if srcs:
        kw["sources"] = srcs
    cooldown = _float(settings, "hot_breakout_cooldown_hours", 48)
    client = FeishuClient(hook, getattr(settings, "feishu_secret", ""))
    session = get_session_local()()
    nl = chr(10)
    sent = 0
    try:
        for (uid,) in session.execute(select(User.id).where(User.enabled.is_(True))).all():
            stats: dict = {}
            try:
                alerts = detect_breakouts(session, uid, stats=stats, **kw)
            except Exception:  # noqa: BLE001 - 单用户失败不挡其余
                session.rollback()
                logger.exception("破圈预警检测失败 user=%s", uid)
                _record(session, uid, "failed", "检测异常")
                continue
            #: ① 先在内存里剔掉冷却期内的(冷却键含**档位**:两档各一把锁,
            #:    斗破那次「11:05 预告 → 12:05 起势」就是两件事,见模块头注)
            fresh: list[dict] = []
            keys: dict[str, tuple[str, str]] = {}
            n_cool = 0
            for a in alerts:
                key = f"breakout:{a['tier']}:{a['name'][:80]}"
                try:
                    if _recently_alerted(session, uid, key, cooldown):
                        n_cool += 1
                        continue
                except Exception:  # noqa: BLE001 - 读不到冷却就照发(宁可多报)
                    logger.warning("破圈预警冷却预检失败 %s(按未发过处理)", key,
                                   exc_info=True)
                    session.rollback()
                fresh.append(a)
                keys[a["name"]] = (key, f"{a['tier']}档/{a['peer_authors']}人/"
                                        f"{_plat(a['source'])}#{a['rank']}")
            #: ② **一轮一张汇总卡**;★ 先发、成功后才**逐条**落冷却行(理由同 `push_surge`)
            n_sent = 0
            if fresh and client.send(_render(fresh, nl)):
                sent += 1
                n_sent = len(fresh)
                for a in fresh:
                    k, why = keys[a["name"]]
                    _mark_alerted(session, uid, k, cooldown, why)
            #: 运行记录:漏斗每一级都记,否则「没预警」与「看不见数据」分不开
            _record(session, uid, "success",
                    f"快照{stats.get('snapshot', '-')} "
                    f"条目{stats.get('rows', 0)} "
                    f"预告行{stats.get('announce', 0)} "
                    f"起势行{stats.get('rising', 0)} "
                    f"作品{stats.get('works', 0)} ⇒ "
                    f"已有链{stats.get('owned', 0)} "
                    f"套话{stats.get('cta', 0)} "
                    f"同行已入局{stats.get('peer', 0)} "
                    f"冷却{n_cool} 卡内{n_sent}")
    finally:
        session.close()
    return sent