# -*- coding: utf-8 -*-
"""**预判命中回验**(2026-11-25)—— 三条预判链发出的每一条,72h 后回查真假。

## 它补的是哪个盲区(说清才不会再犯)

`promo_forecast` / `hot_breakout` / `hot_forecast` 都在发「**这个明天会炸**」。
但在此之前,**全仓没有任何一处算过"它后来真炸了没有"**:

  · `feishu_alerts` 只记"发过"(冷却用),不记"准不准";
  · `ai_filter_decisions.outcome/outcome_at` 两列 2026-10-29 就建好了,
    **全库 0 行写入**(没有任何代码写过它们) —— 建了列不等于有闭环。

于是"要不要把预判投进客户群"这个问题**没法用数据回答**,只能靠印象。
这个作业就是把那条闭环接上:每条预警到期后,回库里找「**后来真的有人在推这份带盘资源吗**」。

## 判据(三条链各自的"命中"长什么样)

| 链 | 预警里那个东西 | 什么算命中 | 什么算**未命中** |
|---|---|---|---|
| `promo_forecast` | 源盘链 + 作品名 | 预警后出现**预警前没推过的新推手**(带盘) | 有同名/同链内容,但没有新推手 |
| `hot_breakout` | 《作品名》 | 该作品名后来被**别人**带盘推过 | 同名内容出现,但始终没有带盘推送 |
| `hot_forecast` | 作品名(seed) | 同上 | 同上 |

三条最终都落到同一件事:**事后库内出现了同名且带盘的推送**。
差别只在提取「那个名字」的方法不同(见 `_subject_of`)。

## ⚠️ `miss` 为什么必须存在(2026-11-28 修)

第一版只有 `hit` / `unknown` 两档 —— **`miss` 那条代码路径根本不可达**,
于是"命中率"只要没有 unknown 就是 **100%**。那不是预判准,是尺子只会记对的。
现在的三档:

  · `hit`   —— 有**新增**的带盘证据(见上表);
  · `miss`  —— 预警后**同名内容确实出现过**,但没有任何新的带盘推送
              (只被提及/只发过无盘文章/只有预警前那批推手/只有解析失败的口令)。
              这证明"它没有兑现成网盘资源",是真正的负样本;
  · `unknown` —— 到期但库内**连同名内容都没有**。库里没有 ≠ 世上没有,仍然单列。

`promo_forecast` 还多一条**去同义反复**的规则:它的预警前提就是"头 24h 已攒够
N 个推手",所以"之后还有人推"几乎必然成立。命中必须来自**预警前没出现过的作者**
(基线窗口 = `promo_forecast_early_hours`,默认 24h);只有同一批人继续推 ⇒ `miss`。

## 证据分三档(`evidence`),**不许合并报**

· `peer`  —— `push_ledger` / `wechat_articles` 里**别人**推了同名且带盘链。
             这是"变成网盘资源"的**定义本身**,唯一能当命中率分子的档。
· `douyin`—— `douyin_leads` 里同名且 `kind='share'`(有人发口令/转存链)。
· `pan`   —— `discovered_pan_links` 里同名盘链。**最弱**。

⚠️ **为什么不把三档加在一起算命中率**(2026-11-25 实测,不是理论洁癖):
贴吧源是**资源灌水农场** —— 同一批「漫剧合集」贴被反复重发,「青蛇」能匹配到
《青蛇传-前世今生》,「大主宰」「西游记」能匹配到 10 月的旧帖。
用它当命中 = 把**别人的灌水**记成**我们预判得准**。报表里三档**分行写**。

## 窗口与边界

· 回验点 = 预警 + `forecast_review_after_hours`(默认 **72h**)。三条链里提前量最长的是
  `hot_forecast`(~40h),72h 后该发生的都发生了;再晚就分不清"预判准"和"之后自然变热"。
· **只看预警之后**的证据(`found_at/pushed_at > alerted_at`)—— 预警前的旧内容不算命中,
  这是本模块最容易写错的一处:`promo_forecast` 的样本里就有一条(麦穗与羊)
  在预警前 3 天我们已经搬过。
· `unknown`(到期但库内连同名内容都没有)≠ `miss`。库里没有 ≠ 世上没有 ——
  我们只采样了有限的平台矩阵,游戏/影视预告尤其不在其中。**报表必须把 unknown 单列**,
  否则"我们看不见"会被读成"它没火"。
· `miss` = 同名内容**出现过**、但没有新的带盘证据(见上表的定义)。它与 unknown 的区别
  是"看见了但没兑现"。⚠️ 2026-11-28 之前的行没有这一档,报表要把旧行单独标出来,
  不许和新口径混在一个百分比里。
· 到达期但**还没到 `after_hours`** 的**不写行**(下一轮再来);已经写过的**不再重算**
  (结果要可复现,不许因为今天又冒出一条证据就把上周的 miss 改成 hit)。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 参与回验的三条链。**顺序有意义** —— 报表按这个顺序出。
SECTIONS: tuple[str, ...] = ("promo_forecast", "hot_breakout", "hot_forecast")

#: 证据强弱排序(数字越小越强)。`""` 排最后。
_RANK: dict[str, int] = {"peer": 0, "douyin": 1, "pan": 2, "": 9}

#: `miss` 档上线的时刻(2026-11-28)。**报表用它把旧口径行单独标出来**:
#: 之前写下的行只有 hit/unknown,"未命中=0"是尺子缺失,不是预判全对。
MISS_RULE_SINCE = datetime(2026, 11, 28)

_BRACKET_RE = re.compile(r"《([^》]{2,30})》")

#: `promo_forecast` 的冷却键形态:`forecast:<盘链>|<作品名>`
#: (见 `promo_forecast.promo_forecast_tick` 的 `key = f"forecast:{pan_key}|{work_key}"`)
_FORECAST_RE = re.compile(r"^forecast:(?P<pan>\S*?)\|(?P<work>.+)$")


def _subject_of(section: str, key: str) -> tuple[str, str]:
    """冷却键 → `(作品名, 盘链)`。盘链只有 `promo_forecast` 有。

    ⚠️ 三种键的形态**不一样**,别用一个正则套:
      · `promo_forecast` `forecast:<pan_key>|<work_key>`
      · `hot_breakout`   `breakout:<档位>:<作品名>`  ← **中间还有一段档位**
      · `hot_forecast`   `forecast:<seed>`           ← 与 promo **同前缀、没有 `|`**
    后两条如果只按 `forecast:` 剥前缀,`breakout:起势:巫师4` 会变成
    一个含冒号的假名字,回库匹配**永远命中 0**,而且不报错。
    """
    k = str(key or "").strip()
    if section == "promo_forecast":
        m = _FORECAST_RE.match(k)
        if m:
            return m.group("work").strip(), m.group("pan").strip()
        return "", ""
    if section == "hot_breakout":
        parts = k.split(":", 2)          # `breakout` / 档位 / 作品名
        return (parts[2].strip() if len(parts) == 3 else ""), ""
    if section == "hot_forecast":
        return k[len("forecast:"):].strip() if k.startswith("forecast:") else k, ""
    return k, ""


#: `title → keys` 备忘。回验一条预警要扫四张表,台账标题在**每条预警之间高度重复**
#: —— 不缓存的话同一批标题会被归一化几十遍(一天一次的任务可以慢,但不该做无用功)。
_KEYS_CACHE: dict[str, frozenset[str]] = {}


def _keys(title: str) -> frozenset[str]:
    """标题 → 可用于匹配的归一 key 集合(`resource_key` 与 `match_key` 都收)。

    为什么两个都收:`resource_key` 会剥通用前后缀(适合"是不是同一个东西"),
    `match_key` 保留更多字(适合"标题里有没有它")。只用一个,
    会漏掉另一类写法 —— 而漏掉是**静默的**(表现为命中率偏低,没人知道为什么)。
    """
    t = str(title or "")
    hit = _KEYS_CACHE.get(t)
    if hit is not None:
        return hit
    from app.services.resource_library import match_key, resource_key

    out: set[str] = set()
    for fn in (match_key, resource_key):
        try:
            k = fn(t)
        except Exception:  # noqa: BLE001 - 归一失败就当没有,别让一条脏标题打挂整轮
            continue
        #: ⚠️ 门槛放在 `_matches` 里按**方向**施,不在这里一刀切:
        #: 2 字的资源名(`青蛇`/`斗破`)是常态,在这里砍掉它们等于把这些作品
        #: 的回验**永久判成 unknown**。真正危险的是"2 字的 key 反向包含"。
        if k and len(k) >= 2:
            out.add(k)
    got = frozenset(out)
    if len(_KEYS_CACHE) < 20000:         #: 别无限长(标题千变万化)
        _KEYS_CACHE[t] = got
    return got


def _matches(subject: frozenset[str], content: frozenset[str]) -> bool:
    """`content` 里这条内容**说的是不是** `subject`(子串包含,双向)。

    ## ⚠️ 必须按**包含**判,不能按集合相等

    实测(2026-11-25 第一版就栽在这):预警名是 `巫师4`,台账标题是
    `《巫师4》最新预告完整版` —— 归一后是 `巫师4最新预告完整版`,
    两者**没有一个字面相同的 key**,集合求交恒为空 ⇒ 命中率恒为 0,而且不报错。
    `hot_breakout._peer_count` 早就用的是包含判,这里是同一个理由。

    ## 为什么双向

    · 预警名 ⊆ 内容标题:常见(`巫师4` ⊂ `巫师4最新预告完整版`);
    · 内容标题 ⊆ 预警名:`hot_forecast` 的 seed 是整条热榜标题
      (`两京十五日如何从中挽回观众`),而台账里只有 `《两京十五日》`。

    ⚠️ **反向只认 ≥4 字的短边**:2~3 字的 key(「全」「上」)被任意标题包含,
    放进来就是"匹配一切"。正向**也**要 ≥2:1 个字的 key 是纯噪音。
    """
    for s in subject:
        if len(s) < 2:
            continue
        for c in content:
            if s in c:
                return True
            if len(c) >= 4 and c in s:
                return True
    return False


class _Probe:
    """一条预警的**身份**:作品名的归一 key 集合 + (可选)盘链身份。

    为什么两个身份都要:`promo_forecast` 的预警里带源盘链,而**同一份资源换个
    标题写法就配不上名字** —— 实测有 26 行台账用完全不同的标题推同一条链。
    只按名字匹配会把它们全判成 unknown。
    """

    __slots__ = ("names", "pan_id", "bracket")

    def __init__(self, name: str = "", pan: str = ""):
        self.bracket = str(name or "")
        self.names: set[str] = set()
        if name:
            self.names |= _keys(name)
            for m in _BRACKET_RE.findall(str(name)):
                self.names |= _keys(m)
        self.pan_id = ""
        if pan:
            #: ⚠️ `promo_forecast` 的冷却键里存的**已经是 `pan_key()` 的结果**
            #: (`host/path`,不带 scheme)。再喂一次 `pan_key()` 会返回 `""`
            #: —— 它要求 `parse_qsl(urlsplit(u))` 里有 netloc,裸 `host/path`
            #: 解析出来的 netloc 是空 ⇒ 盘链身份**静默失效**。所以这里先试,
            #: 试不出来就当它已经归一过了(特征:`/s/` 在且没有 `://`)。
            from app.services.push_ledger import pan_key

            raw = str(pan).strip()
            self.pan_id = pan_key(raw) or (raw if "://" not in raw and "/s/" in raw else "")

    def __bool__(self) -> bool:
        return bool(self.names or self.pan_id)

    def hit(self, title: str = "", pan: str = "") -> str:
        """命中身份 → `"name"` / `"pan"` / `""`。**两种身份不等价**,见下。

        ## ⚠️ 盘链身份**不许单独进 `peer` 档**(2026-11-25 实测)

        第一版把两种身份当一个 bool 用,回验《麦穗与羊》时 `peer=56` 里混进了
        5 行标题是《我的有罪之人》的台账/公众号 —— 它们和预警**共用同一条迅雷
        分享**(`...houKA1`),因为迅雷分享**可以挂多个作品**。
        盘链绑的是「分享」,不是「资源」;拿分享证明作品,就是拿别人的合集
        冒充自己的预判命中。所以盘链只能进最弱的 `pan` 档,与贴吧灌水同列。
        """
        if self.names and _matches(self.names, _keys(str(title or ""))):
            return "name"
        if self.pan_id and pan:
            from app.services.push_ledger import pan_key

            if pan_key(str(pan)) == self.pan_id:
                return "pan"
        return ""


_NO_LINK_RE = re.compile(r"^(?:⚠️|\[)?\s*(?:源分享已被封|原文失效|未转存|已失效)")


def _is_link(value) -> bool:
    """这一列里装的是**真的盘链**,还是失败标注 / 空壳?

    ## ⚠️ 只看"非空字符串"是不够的(2026-11-25 实测)

    `wechat_articles.my_pan_urls` 里现网有 **92 行**装的是
    `⚠️源分享已被封(41031),未转存` / `⚠️原文失效(...)` ——
    它们是**失败标注**,恰恰证明"这条没变成网盘资源"。
    第一版按 `str(x).strip()` 判非空,把这类行当成了带盘证据,
    《麦穗与羊》的 `peer` 因此多算了 2 行,方向还正好是反的。
    """
    t = str(value or "").strip()
    if not t:
        return False
    if _NO_LINK_RE.match(t):
        return False
    return t.startswith("http") or "网盘" in t or "/s/" in t


def _window(col, since: datetime, until: datetime | None) -> list:
    """`(since, until]` 的时间条件。`until=None` = 只设下界(回验的常规用法)。"""
    cond = [col > since]
    if until is not None:
        cond.append(col <= until)
    return cond


def _scan_peer(session: Session, user_id: int, probe: _Probe,
               since: datetime, until: datetime | None = None
               ) -> list[tuple[datetime, str, str, str]]:
    """**别人**带盘推过同名内容 → `[(时刻, 引用, 档次, 作者)]`。

    ⚠️ 必须**排掉我们自己** —— 但现网台账里 `author` 基本都有值,真正要防的是
    "我们自己搬完再推出去",那会自证预言。做法简单直接:
    **只认有 author 且有盘链的行**,空的一律不计数(宁可少算)。

    ⚠️ **只靠盘链配上的行降档成 `pan`**(见 `_Probe.hit` 头注):
    分享能挂多个作品,拿分享证明作品就是拿别人的合集冒充我们的预判。

    `author` 带出来是为了 `promo_forecast` 的"新推手"判据(见 `review_one`)。
    """
    from app.db.models import PushLedger, WechatArticle

    hits: list[tuple[datetime, str, str, str]] = []
    for author, title, pan, ts, section in session.execute(
            select(PushLedger.author, PushLedger.title, PushLedger.pan_url,
                   PushLedger.pushed_at, PushLedger.section)
            .where(PushLedger.user_id == user_id,
                   *_window(PushLedger.pushed_at, since, until))).all():
        if not str(author or "").strip() or not _is_link(pan):
            continue
        kind = probe.hit(str(title or ""), str(pan or ""))
        if kind:
            tier = "peer" if kind == "name" else "pan"
            hits.append((ts, f"台账 {section} · {str(title or '')[:40]} · {str(author)[:16]}",
                         tier, str(author or "").strip()))
    for author, title, my_pan, ts in session.execute(
            select(WechatArticle.author, WechatArticle.title,
                   WechatArticle.my_pan_urls, WechatArticle.created_at)
            .where(WechatArticle.user_id == user_id,
                   *_window(WechatArticle.created_at, since, until))).all():
        if not str(author or "").strip() or not _is_link(my_pan):
            continue
        #: ⚠️ `my_pan_urls` 是**我方链**:公众号这条只说明"这篇文章里有盘"。
        #: 仍然算 `peer` 档 —— **同行号**推带盘文章就是"这资源变成网盘资源"的
        #: 直接证据;`author` 过滤已经排掉自己。
        kind = probe.hit(str(title or ""), str(my_pan or ""))
        if kind:
            tier = "peer" if kind == "name" else "pan"
            hits.append((ts, f"公众号 · {str(title or '')[:40]} · {str(author)[:16]}",
                         tier, str(author or "").strip()))
    return hits


def _scan_douyin(session: Session, user_id: int, probe: _Probe,
                 since: datetime, until: datetime | None = None
                 ) -> list[tuple[datetime, str, str, str]]:
    """抖音线索里同名且 `kind='share'`(真带口令/转存链)。"""
    from app.db.models import DouyinLead

    hits: list[tuple[datetime, str, str, str]] = []
    for title, mark, author, found in session.execute(
            select(DouyinLead.title, DouyinLead.mark, DouyinLead.author,
                   DouyinLead.found_at)
            .where(DouyinLead.user_id == user_id,
                   *_window(DouyinLead.found_at, since, until),
                   DouyinLead.kind == "share")).all():
        #: 标题与《口令》都参与匹配:抖音的资源名常常只写在《》里
        if probe.hit(str(title or "")) or probe.hit(str(mark or "")):
            hits.append((found, f"抖音线索 · {str(mark or title or '')[:40]}", "douyin",
                         str(author or "").strip()))
    return hits


def _scan_pan(session: Session, user_id: int, probe: _Probe,
              since: datetime, until: datetime | None = None
              ) -> list[tuple[datetime, str, str, str]]:
    """公开平台发现的盘链(最弱证据;贴吧有灌水农场,见头注)。

    `DiscoveredPanLink` **没有作者列** ⇒ 作者记空串。对 `promo_forecast` 来说,
    它证明不了"来了一个新推手"(见 `review_one` 的新推手判据)。
    """
    from app.db.models import DiscoveredPanLink

    hits: list[tuple[datetime, str, str, str]] = []
    for title, platform, origin, found in session.execute(
            select(DiscoveredPanLink.title, DiscoveredPanLink.platform,
                   DiscoveredPanLink.origin_url, DiscoveredPanLink.found_at)
            .where(DiscoveredPanLink.user_id == user_id,
                   *_window(DiscoveredPanLink.found_at, since, until)).limit(20000)).all():
        if probe.hit(str(title or ""), str(origin or "")):
            hits.append((found, f"盘链 {platform} · {str(title or '')[:40]}", "pan", ""))
    return hits


def _scan_mention(session: Session, user_id: int, probe: _Probe,
                  since: datetime, until: datetime | None = None
                  ) -> list[tuple[datetime, str]]:
    """**同名内容出现过、但没有带盘** → `[(时刻, 引用)]`。这是 `miss` 的证据。

    ## 为什么按"提及"就能判 miss

    三条预判问的都是「**后来会不会变成带盘资源**」。如果预警之后库里出现了这个名字,
    却始终没有一条新的带盘推送,那这条预判就是没兑现 —— 不是"我们没看见"。

    ## 三种提及的边界(都按"宁可漏判,不许把我们的故障算成世界的错")

    · 台账/公众号:`author` 非空才算(自己的无盘卡片不能拿来自证失败);
    · 公众号:只有标题匹配、且 `my_pan_urls`/`pan_urls` **都不是可用链**才算提及
      (失败标注 `⚠️源分享已被封` 属于"试过但没成" ⇒ 是 miss,不是 hit);
    · 抖音:只认 `kind='none'`(明确没有口令)。`error` 是我们解析失败、
      `group` 是群口令(本身也是一种资源交付)—— 都不许当成"没兑现"。
    """
    from app.db.models import DouyinLead, PushLedger, WechatArticle

    hits: list[tuple[datetime, str]] = []
    for author, title, pan, ts in session.execute(
            select(PushLedger.author, PushLedger.title, PushLedger.pan_url,
                   PushLedger.pushed_at)
            .where(PushLedger.user_id == user_id,
                   *_window(PushLedger.pushed_at, since, until))).all():
        if not str(author or "").strip() or _is_link(pan):
            continue
        if probe.hit(str(title or "")) == "name":
            hits.append((ts, f"台账(无盘) {str(title or '')[:36]} · {str(author)[:16]}"))
    for author, title, my_pan, src_pan, ts in session.execute(
            select(WechatArticle.author, WechatArticle.title,
                   WechatArticle.my_pan_urls, WechatArticle.pan_urls,
                   WechatArticle.created_at)
            .where(WechatArticle.user_id == user_id,
                   *_window(WechatArticle.created_at, since, until))).all():
        if not str(author or "").strip():
            continue
        if _is_link(my_pan) or _is_link(src_pan):
            continue
        if probe.hit(str(title or "")) == "name":
            hits.append((ts, f"公众号(无盘) {str(title or '')[:36]} · {str(author)[:16]}"))
    for author, title, mark, kind, found in session.execute(
            select(DouyinLead.author, DouyinLead.title, DouyinLead.mark,
                   DouyinLead.kind, DouyinLead.found_at)
            .where(DouyinLead.user_id == user_id,
                   *_window(DouyinLead.found_at, since, until),
                   DouyinLead.kind == "none")).all():
        if not str(author or "").strip():
            continue
        if probe.hit(str(title or "")) or probe.hit(str(mark or "")):
            hits.append((found, f"抖音(无口令) · {str(mark or title or '')[:36]}"))
    return hits


def _baseline_authors(session: Session, user_id: int, probe: _Probe,
                      lo: datetime, hi: datetime) -> set[str]:
    """预警**之前**那批推手(作者集合)—— `promo_forecast` 去同义反复的基线。

    口径与 `promo_forecast.detect_forecasts` 的 "hands" 对齐:只数**台账里带盘链**
    且作者非空的行。命中判据用 `probe.hit` 的**两种身份**(同名 / 同盘链),
    因为那条链的预警本来就是「同一条链 + 同一个作品名」。
    """
    from app.db.models import PushLedger

    out: set[str] = set()
    for author, title, pan, pk in session.execute(
            select(PushLedger.author, PushLedger.title, PushLedger.pan_url,
                   PushLedger.pan_id)
            .where(PushLedger.user_id == user_id,
                   *_window(PushLedger.pushed_at, lo, hi),
                   or_(PushLedger.pan_url != "", PushLedger.pan_id != ""))).all():
        who = str(author or "").strip()
        if not who:
            continue
        if probe.hit(str(title or ""), str(pan or pk or "")):
            out.add(who)
    return out


def review_one(session: Session, user_id: int, section: str, key: str,
               title: str, alerted_at: datetime, reason: str,
               after_hours: float, baseline_hours: float = 24) -> dict | None:
    """回验**一条**预警。未到期返回 `None`(不写行)。

    `baseline_hours` 只对 `promo_forecast` 有意义:预警前这么久的推手算"已入局",
    他们之后再推**不算**新证据(去同义反复,见模块头)。
    """
    end = alerted_at + timedelta(hours=float(after_hours))
    if datetime.now() < end:
        return None

    work, pan = _subject_of(section, key)
    name = work or str(title or "")
    probe = _Probe(name, pan)

    #: `promo_forecast` 的基线推手 —— **预警之前**那批人。别的链不需要(它们的
    #: 前提本来就是"还没人推"或"最多 1 人推"),留空集即可。
    pre_authors: set[str] = set()
    if section == "promo_forecast" and probe:
        try:
            pre_authors = _baseline_authors(
                session, user_id, probe,
                alerted_at - timedelta(hours=float(baseline_hours)), alerted_at)
        except Exception:  # noqa: BLE001
            #: 基线取不到就**不写结论**(下一轮重试):宁可不判,也不要因为"没排除旧推手"
            #: 把同一批人的重复推送记成新命中 —— 那是这个作业最该避免的假阳性。
            session.rollback()
            logger.warning("回验基线推手取数失败,本轮跳过 %s/%s", section, key[:60],
                           exc_info=True)
            return None

    counts: dict[str, int] = {"peer": 0, "douyin": 0, "pan": 0}
    best: tuple[int, datetime, str] | None = None
    new_hands = 0
    for _tag, fn in (("peer", _scan_peer), ("douyin", _scan_douyin), ("pan", _scan_pan)):
        try:
            #: ⚠️ 下界就是 `alerted_at` 本身 —— 预警**之前**的旧内容不算命中,
            #: 这是本模块最容易写错的一处(见头注)。
            rows = fn(session, user_id, probe, alerted_at) if probe else []
        except Exception:  # noqa: BLE001 - 单档失败不影响其余档(宁可少算)
            session.rollback()
            logger.warning("回验取证失败 %s/%s", section, key[:60], exc_info=True)
            continue
        for ts, ref, tier, author in rows:
            #: ⚠️ 用**这一行自己的**档次,不用扫描器的标签 —— `_scan_peer` 会把
            #: "只靠盘链配上的行"降成 `pan`(见 `_Probe.hit`)。
            counts[tier] = counts.get(tier, 0) + 1
            #: ★ `promo_forecast`:**预警前没推过的人**才算新证据。
            #: 作者为空的证据(公开盘链)证明不了"来了一个新推手",一律不算。
            if section == "promo_forecast":
                if not author or author in pre_authors:
                    continue
                new_hands += 1
            cand = (_RANK[tier], ts, ref)
            if best is None or (cand[0], cand[1]) < (best[0], best[1]):
                best = cand

    mentions: list[tuple[datetime, str]] = []
    try:
        mentions = _scan_mention(session, user_id, probe, alerted_at) if probe else []
    except Exception:  # noqa: BLE001 - 提及档失败只会让 miss 少判(宁可少算)
        session.rollback()
        logger.warning("回验提及档失败 %s/%s", section, key[:60], exc_info=True)

    total_pos = counts["peer"] + counts["douyin"] + counts["pan"]
    if best is not None:
        rank, ts, ref = best
        evidence = {v: k for k, v in _RANK.items()}[rank]
        outcome = "hit"
        lag = round((ts - alerted_at).total_seconds() / 3600.0, 2)
    elif mentions or (section == "promo_forecast" and total_pos):
        #: 看见了同名内容、但没有**新增的带盘证据** ⇒ miss(不是 unknown)。
        #: `promo_forecast` 还多一种:只有预警前那批推手/同链在重复出现 ⇒ 没有新推手。
        outcome, evidence, lag = "miss", "", None
        ref = (mentions[0][1] if mentions
               else "预警后只有预警前已入局的推手/同链,无新增带盘")
    else:
        #: 库内**一条同名内容都没有** ⇒ unknown,不是 miss(见头注)
        outcome, evidence, ref, lag = "unknown", "", "", None

    detail = " ".join(f"{k}={counts[k]}" for k in ("peer", "douyin", "pan"))
    if mentions:
        detail += f" mention={len(mentions)}"
    if section == "promo_forecast" and total_pos:
        detail += f" new={new_hands}"
    return {"section": section, "key": key[:255], "title": name[:255],
            "alerted_at": alerted_at, "reason": str(reason or "")[:255],
            "outcome": outcome, "evidence": evidence, "evidence_ref": ref[:255],
            "lag_hours": lag, "detail": detail}


def review_pending(session: Session, user_id: int, settings, *,
                   now: datetime | None = None) -> dict:
    """把**已到期但还没回验**的预警逐条回验并落库(纯读 + 写本表)。

    ⚠️ **分页批处理 + 每批 commit**:回验一条要扫四张表,一批几百条会把 SQLite
    写锁抱很久(本仓在 `resource_presence` 上栽过 181 秒)。批大小可配。
    """
    from app.db.models import FeishuAlert, ForecastReview

    now = now or datetime.now()
    after = float(getattr(settings, "forecast_review_after_hours", 72) or 72)
    batch = max(1, int(getattr(settings, "forecast_review_batch", 50) or 50))
    days = int(getattr(settings, "forecast_review_days", 14) or 14)
    #: `promo_forecast` 的"新推手"基线窗口必须与预警自己的早窗口一致(默认 24h)。
    baseline_h = float(getattr(settings, "promo_forecast_early_hours", 24) or 24)

    done = set(session.execute(
        select(ForecastReview.section, ForecastReview.key)
        .where(ForecastReview.user_id == user_id)).all())
    pending = session.execute(
        select(FeishuAlert.section, FeishuAlert.title, FeishuAlert.reason,
               FeishuAlert.alerted_at)
        .where(FeishuAlert.user_id == user_id,
               FeishuAlert.section.in_(SECTIONS),
               FeishuAlert.alerted_at >= now - timedelta(days=days),
               FeishuAlert.alerted_at <= now - timedelta(hours=after))
        .order_by(FeishuAlert.alerted_at.asc())).all()

    stats = {"due": len(pending), "done": 0, "hit": 0, "miss": 0, "unknown": 0,
             "skipped": 0, "batch": batch}
    n = 0
    for section, key, reason, alerted_at in pending:
        if (section, key) in done:
            stats["skipped"] += 1
            continue
        try:
            row = review_one(session, user_id, section, key, "", alerted_at,
                             reason or "", after, baseline_hours=baseline_h)
        except Exception:  # noqa: BLE001 - 单条失败不挡其余
            session.rollback()
            logger.exception("回验失败 %s/%s", section, str(key)[:60])
            continue
        if row is None:
            stats["skipped"] += 1
            continue
        session.add(ForecastReview(user_id=user_id, **row, reviewed_at=now))
        stats["done"] += 1
        stats[row["outcome"]] = stats.get(row["outcome"], 0) + 1
        n += 1
        if n % batch == 0:
            session.commit()
    session.commit()
    return stats


#: `hot_breakout` 的 reason 形如 `起势档/0人/IT之家#15` —— 源名在**最后一段**。
_HB_SRC_RE = re.compile(r"/([^/#\d]{2,20})(?:#\d+)?$")


def _reason_tag(section: str, reason) -> str:
    """`reason` → 可用于分组的标签。**分链解,取不到就返回 `""`**。

    ## ⚠️ 别用一个 split 套三条链(2026-11-25 实测)

    三条链的 `reason` 形态**毫无共性**:
      · `hot_breakout`  `起势档/0人/IT之家#15`  ← 只有这条带源名
      · `promo_forecast` `11 人/24h`            ← 是个推手计数
      · `hot_forecast`  `42名/4616 万/39x`      ← 是热榜名次
    第一版按 `split("/")[-1]` 一刀切,`11 人/24h` 剥完数字只剩 `h`,
    报表里出现「分源命中:h 2/2」这种没人看得懂的东西。
    """
    r = str(reason or "")
    if section == "hot_breakout":
        m = _HB_SRC_RE.search(r)
        return m.group(1).strip() if m else (r.split("/")[0].strip() or "")
    if section == "promo_forecast":
        return ""                                #: 推手计数不是"源",分组没有意义
    if section == "hot_forecast":
        return r.split("/")[0].strip()[:20]      #: 名次档,能看出是哪一档起的
    return ""


def build_report(session: Session, user_id: int, settings, *,
                 days: int = 14) -> str:
    """给**管理群**的回验报表。三档**分行**写,合计另起一行标注口径。"""
    from app.db.models import ForecastReview

    since = datetime.now() - timedelta(days=int(days))
    rows = session.execute(
        select(ForecastReview.section, ForecastReview.outcome,
               ForecastReview.evidence, ForecastReview.lag_hours,
               ForecastReview.reason, ForecastReview.detail,
               ForecastReview.reviewed_at)
        .where(ForecastReview.user_id == user_id,
               ForecastReview.alerted_at >= since)).all()
    nl = chr(10)
    lines = [f"🎯 预判回验(预警 {days} 天内 · 已到期才计)", ""]
    if not rows:
        lines.append("这一窗口内**没有到期可回验**的预警 —— "
                     "要么没发过,要么都还没到 72 小时。")
        return nl.join(lines)

    for sec in SECTIONS:
        sub = [r for r in rows if r[0] == sec]
        if not sub:
            lines.append(f"【{sec}】**0 条**")
            continue
        n = len(sub)
        out = {k: sum(1 for r in sub if r[1] == k) for k in ("hit", "miss", "unknown")}
        ev = {k: sum(1 for r in sub if r[2] == k) for k in ("peer", "douyin", "pan")}
        lags = sorted(r[3] for r in sub if r[3] is not None)
        med = lags[len(lags) // 2] if lags else None
        #: ⚠️ 两个率**都要出**,因为它们回答的是两个问题:
        #:   · 确认率 = hit / 全部到期 —— "发出去的预判里,有多少真的兑现了";
        #:   · 判对率 = hit / (hit + miss) —— "在有明确结果的那些里,判对了多少"。
        #: `unknown` 只进确认率的分母(它确实占了一次预警),不进判对率。
        judged = out["hit"] + out["miss"]
        rate = f"{out['hit'] * 100.0 / judged:.0f}%" if judged else "—"
        confirm = f"{out['hit'] * 100.0 / n:.0f}%" if n else "—"
        lines.append(f"【{sec}】{n} 条 · 命中 {out['hit']} / 未命中 {out['miss']}"
                     f" / 无数据 {out['unknown']}"
                     f" · **确认率(命中/到期) {confirm}** · **判对率(有结果) {rate}**")
        lines.append(f"   证据档:同行 {ev['peer']} · 抖音 {ev['douyin']} · 公开盘链 {ev['pan']}"
                     + (f" · 命中位中位提前 {med:.0f}h" if med is not None else ""))
        #: 按 `reason` 里的档位/来源分组 —— 这一行才是"该砍哪个源"的依据
        by_src: dict[str, list[str]] = {}
        for r in sub:
            tag = _reason_tag(sec, r[4])
            if tag:
                by_src.setdefault(tag, []).append(r[1])
        parts = []
        for tag, outs in sorted(by_src.items(), key=lambda kv: -len(kv[1]))[:5]:
            h = sum(1 for o in outs if o == "hit")
            parts.append(f"{tag} {h}/{len(outs)}")
        if parts:
            lines.append("   分源命中:" + " · ".join(parts))

    #: 最强证据那几行摆出来 —— 光看比例不会有人行动,得看见"是哪条中了"
    peer_hits = [r for r in rows if r[2] == "peer"][:8]
    if peer_hits:
        lines.append(nl + "【最强证据样本 · 同行带盘】(这些就是「预判对了」的样子)")
        for sec, _o, _e, lag, reason, detail, _rv in peer_hits:
            lines.append(f"  · {sec} {str(reason or '')[:50]}"
                         + (f" · 预警后 {lag:.0f}h" if lag is not None else ""))
            lines.append(f"     {str(detail or '')[:120]}")
    #: 旧口径行必须**单独标出来** —— 它们没有"未命中"这一档,和新口径混进同一个
    #: 百分比,会把"尺子缺一档"读成"预判真准"。14 天后这些行自然滚出窗口。
    old_rows = [r for r in rows if r[6] is not None and r[6] < MISS_RULE_SINCE]
    if old_rows:
        lines.append(nl + f"⚠️ 其中 **{len(old_rows)} 条是 2026-11-28 前的旧口径回验**"
                          "(当时没有「未命中」档)⇒ 它们的未命中数天然为 0,别和新口径相加。")
    lines.append(nl + "(口径:只看**预警之后**出现的证据;"
                      "`未命中` = 预警后同名内容出现过、但**没有新增的带盘推送**;"
                      "`promo_forecast` 还要求**预警前没推过的新推手**;"
                      "`同行` = 台账/公众号里别人带盘推了同名;"
                      "`公开盘链` 含贴吧灌水,单独一档别当命中率;"
                      "`无数据` = 到期但库内查无同名内容,不等于没火。)")
    return nl.join(lines)


def forecast_review_tick(settings=None, *, send: bool = True) -> int:
    """作业入口(每天一次):回验到期的预警 → 写 `forecast_reviews` → 推管理群。"""
    from config.settings import get_settings

    settings = settings or get_settings()
    if not bool(getattr(settings, "forecast_review_enabled", True)):
        return 0
    from app.db import get_session_local
    from app.db.models import User
    from app.services.tenant_base import _record_run

    session = get_session_local()()
    sent = 0
    try:
        for (uid,) in session.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                st = review_pending(session, uid, settings)
            except Exception:  # noqa: BLE001 - 单用户失败不挡其余
                session.rollback()
                logger.exception("预判回验失败 user=%s", uid)
                continue
            detail = (f"到期{st['due']} 回验{st['done']} "
                      f"命中{st['hit']} 未中{st['miss']} 无数据{st['unknown']} "
                      f"跳过{st['skipped']}")
            try:
                _record_run(session, uid, "forecast_review", "success", detail)
                session.commit()
            except Exception:  # noqa: BLE001 - 运行记录失败不挡业务
                session.rollback()
                logger.warning("预判回验运行记录失败 user=%s", uid, exc_info=True)
            if not send:
                continue
            try:
                from app.services.feishu_client import FeishuClient, webhook_for

                hook = webhook_for(settings, "admin")
                if not hook:
                    continue
                text = build_report(session, uid, settings,
                                    days=int(getattr(settings, "forecast_review_days", 14) or 14))
                if FeishuClient(hook, getattr(settings, "feishu_secret", "")).send(text[:3000]):
                    sent += 1
            except Exception:  # noqa: BLE001 - 推送失败不影响已落库的回验结果
                logger.exception("预判回验报表推送失败 user=%s", uid)
    finally:
        session.close()
    return sent
