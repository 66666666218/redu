"""**热点从哪来、能活多久、值不值得跟**(2026-11-08,2026-11-09 订正)。

> 用户口径:「**每次都会有新的热点出来你需要总结这些规律来完成监控和预测**」
>
> 用户纠正:「**热点事件名称是没有关联性的,每一个都是独立的事件**」

⇒ 这条纠正**推翻了「按种子族扩展」的设计前提**。用已知名字当关键词,只能找到
**已知事件的变体**(下面第 4 条有实证:一族一天刷出 13 种说法),**永远发现不了
全新事件**。要发现新事件只能看**全榜**,见下面第 2 条的两条无种子通路。

## 一、先说清楚:哪些规律**测出来了**,哪些**测不出来**

样本 = 热点宝搜索榜(`hot_search`)按 12 个种子词定向查回的 **359 个不重复词 / 486 条日序列**
(2026-11-08 实测,`date_window=168` 近 7 天,每条最多 14 个日点)。

### ✅ 测出来的四条

**1. 门槛差 275 倍:同一个词进不了高窗口榜,但进得了 1 小时榜**

| | 全局榜 `dw=168` | 截图那三个词(定向查) | 1 小时榜 `dw=1` |
|---|---|---|---|
| 门槛 | 第 49 名 = **2.1 亿** | 76.5 万 ~ 724.7 万 | **6 万 ~ 4.7 亿**(p25=28 万) |
| 无种子能拿到吗 | 只能拿到亿级 | 拿不到(要有关键词) | **能,50 条一次** |

⚠️ **2026-11-09 订正**:上一版写的是「长尾热点**永远**只能定向查」——**过强了**。
`dw=168` 那一档确实只有亿级,**但 `hot_search(date_window=1)` 不带任何 keyword
就能一次回 50 条、量级横跨 6 万~4.7 亿**,白雪公主那一档(几十万~几百万)也在里面。
真正的区别不是「能不能拿到」,而是**「拿到的时机」**:高窗口给的是**累计量**,
1 小时榜给的是**当下在冲的词**,后者才是「新事件」。

**2. ★ 两条**不带种子**就能发现独立事件的通路(2026-11-09 实测)

| 通路 | 实测 | 新鲜度 | 结论 |
|---|---|---|---|
| `hot_search(limit=50, dw=1, sub_type=3001/3002)` | 50 + 50 条,去重后 14 条「当天新起」 | **小时级序列**,新事件日点 1~5 | ✅ **主通路** |
| `hot_words(limit=200, dw=168)` 内容词 | 200 条 | **日级且滞后**:2026-11-09 测得末点仍是 **11-07**;`dw=72` 回 **0 条** | ⚠️ 只作热度参考,不能判「今天首现」 |

小时榜里那些**点数 ≤5** 的正是当天新事件:`现场观战BLG vs T1`(日点2)、
`特朗普回应"黄仁勋承认出轨"`(日点2)、`22年小孩游泳馆溺水后续`(日点2)、
`黄仁勋发声明承认出轨`(日点3,616 万)、`blg对决t1比分`(日点3);
常青词(`kpl`/`仙逆`)是 24 点满序列 —— **「点数少」比「量大」更能说明是新事件**。

**3. 「首现」能把新事件从同族老词里挑出来 —— 但**只能在已知那一族里挑**

拿 `墨尔本七个小矮人` 定向查回 50 个词,其中 **18 个是最近 2 天才首现**的 ——
而且首现的那批**恰好就是对的事件**(`墨尔本华人外卖员遇刺身亡` 3286 万、
`墨尔本遇害中国骑手妻子发声` 2502 万),而 `墨尔本`/`墨尔本外卖员` 这类老词一直在榜上晃。
`覆面系` 同理:6 个首现词全是 `黑白覆面塌` 的子事件。

⚠️ 但这**只能发现"已知事件的新说法"**。同一次实测里还混进了 `接亲七个枣核含义`
(只共用「七」这个字)—— 说明种子扩展既**发现不了新事件**,又**会带进路人**。
这正是用户那句「每一个都是独立的事件」的落点:**新事件只能从全榜来**。

⚠️ 这里判的是「**这个搜索词第一次被看到有量**」,不是「热度涨了多少」——后者见第 5 条(没用)。

**4. 爆点次日 38% 维持、31% 崩掉**(这是**上限**,不是预测)

按峰值 ≥5×基线、≥20 万算一个爆点(94 个),次日相对峰值:

| 次日/峰值 | 占比 |
|---|---|
| ≥0.8(还在烧) | **38%** |
| 0.4~0.8(半衰) | 31% |
| <0.4(凉了) | **31%** |

⇒ **当天的热点必须当天跟**。等下一轮作业跑第二遍,1/3 的可能已经凉了。

### ❌ 没能测出来的三条(如实写下来,别当成"做到了")

**5. 突增倍数预测不了明天**:按「末日/前段中位数」分桶,次日再翻倍率 —— 2~5 倍档 **5.8%**、
10~30 倍档 **13.5%**、100 倍以上档 **7.5%**,基线(全样本) **5.2%**。区分度约 2 倍且**非单调**。

**6. 形态预测不了未来**:平稳/波动/爬升/阶跃四类,未来 2 天翻倍率 13.1%~15.6%、
3 天 16.7%~21.2%,**四类几乎一样**。

**7. 跨种子共振 = 0**:359 个词里被 ≥2 个不同种子同时返回的有 **0 个**。
"多垂类同时抬头 = 大热点"这个直觉,在这批样本上**不成立**。

⇒ 本模块**不输出"预测值"**。它输出**已经发生、可核查的事实**(哪天首现、现在多大量、
出自哪条通路),排序靠事实、不靠预测。

## 二、还有一个更硬的约束:**这些热点在抖音上没人推网盘**

对 7 个爆发词(`小小甄选`/`水老鼠`/`覆面系`/`玲玲夫妇`/`爆炸桃`/`黑白覆面`/`小小猪`)
各取 13~14 条搜索结果:**带网盘链 0 条**;标题带资源语义(网盘/链接/全集/口令/迅雷/夸克…)
**0~1 条**(那 1 条还是"摘桃子")。

而库里真出货的词(`白泽的梦` 100%、`天晴宝箱` 100%、`咐置` 32%)全是**资源名**;
`runs` 里 11 轮大瓜轮**全部 `外部命中0/产出0`**,搜回来的是漫剧/游戏/问卷。

⇒ **热点词喂 `demand_watch`(找名字),不喂 `douyin_leads`(找盘链)。**
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from config.settings import Settings
from app.utils import get_logger

logger = get_logger(__name__)

#: 定向查「相关搜索词」用的时段:**近 7 天**。
#: ⚠️ 不是随便选的 —— 用户截图那三个数(724.7万/206.6万/76.5万)只有 `date_window=168`
#: 才逐位对得上;换 1/24/72 都取不到同样的量。
SEED_WINDOW = 168

#: 判"首现"用几天:最近 N 个日点里有量、之前没有。
EMERGE_DAYS = 2

#: 多大的量才值得看一眼。**50 万**是实测出来的:全局榜第 49 名 2.1 亿,而用户给的样本
#: `白雪公主1995` 只有 76.5 万 —— 长尾就在几十万到几百万。低于 50 万的实测里绝大多数
#: 是 `小`/`吃`/`桃桃` 这类单字泛词。
MIN_LAST = 500_000

#: 首现日之前允许的"底噪"。热点宝对没量的词返回 1000~3000 的保底值,不能要求严格为 0。
NOISE_FLOOR = 10_000

#: 一个种子族最多交几个词。**3** 是实测定的:墨尔本那族同一天首现 **13 个变体**
#: (`墨尔本华人外卖员遇刺身亡`/`中国骑手墨尔本遇害`/`熊猫外卖骑手于墨尔本遇害`…),
#: 全是**同一个事件的不同说法**,不设上限就是同一件事刷满一张卡。
PER_SEED_CAP = 3

#: 无种子通路取哪些窗口:搜索榜**近 1 小时**(小时级序列)。
#: ⚠️ 内容词榜(`hot_words`)实测是**日级且滞后** —— `date_window=168` 的末点仍是
#: 前一天(2026-11-09 测得末点=11-07),`date_window=72` 直接回 **0 条**;
#: 按它判「今天首现」会把一天前的事件当成新事件,甚至什么都判不出来。
BOARD_WINDOWS = (3001, 3002)

#: 小时级序列上「点数 ≤ 5」= 最近 5 小时才出现在榜上 ⇒ **当天新起的事件**。
#: 实测(2026-11-09 `dw=1`):`黄仁勋发声明承认出轨` 日点 2、`大连英博夺冠` 日点 3、
#: `现场观战BLG vs T1` 日点 1;而 `kpl`/`仙逆` 这类常青词是 24 点满序列。
FRESH_POINTS = 5

#: 小时榜的量门槛。**20 万**取自实测分位(p25=28 万):再往下多是长尾词条。
SEARCH_MIN_LAST = 200_000

#: 无种子通路的泛词闸门。⚠️ 它**比种子族的 `_belongs` 弱得多**(没有种子可对),
#: 所以只挡「去掉修饰后没有任何实体的流程词」;真事件里的泛词素(比分/直播)不挡 ——
#: `blg对决t1比分` 里有 `blg`/`t1`,仍然要收。
_BOARD_STOP = frozenset({
    "晋级", "晋级半决赛", "晋级决赛", "半决赛", "决赛", "赛程", "直播",
    "最新消息", "现场观战", "三周年", "完整版", "在线观看", "免费观看",
})


# ---------------------------------------------------------------- 形态词探针(2026-11-10)

#: ★ **不靠固定词条找新热点** —— 探的是「事件名被填进哪几种语法外壳」。
#:
#: ## 为什么必须是这条路(实测,2026-11-10)
#:
#: 搜索榜是**策展式**的,不是全量排序:实测「只忍五次」系列近 1 小时 16 万,
#: 而 3001 榜第 50 名是 **217 万**;即使翻到第 8 页(分数线已降到 6.8 万,**比它还低**)
#: 仍然找不到 ⇒ **它从头到尾没进过榜**。翻页、调阈值都救不了这条路。
#:
#: 但 `hot_search(keyword=…)` 查的是**全量词库**:`keyword='直播'` 回的 50 条全带「直播」,
#: 其中就有 `只忍老板五次直播视频`(16 万,低于榜底线 13 倍)。
#:
#: ## 为什么是"形态词"而不是"内容词"
#:
#: 用户的口径是「每个词条都不一样,没办法给固定词条」。事件名确实是任意的,
#: **但它必须被填进固定的几种壳子**才能被搜索和传播 —— 「后续 / 回应 / 通报 / 离职 /
#: 处罚…」描述的是**事件被命名的方式**,不是事件本身。所以这张表比内容词稳定得多。
#:
#: ## 实测产出(2026-11-10,12 个词 × 1 次请求;2026-11-12 起收敛为 6 个)
#:
#: | 指标 | 结果 |
#: |---|---|
#: | 候选(非满序列) | **275** 个 |
#: | 常数词被滤掉(24 点满序列) | 177 个 |
#: | 峰值 ≥15 万 | **117** 个 |
#: | 峰值 <1 万(长尾噪音) | 仅 10 个 |
#:
#: 且**确实捞到了目标**:`彻底疯狂泼粪离职事件回放`(270 万,`彻底疯狂` 正是原作者)、
#: `老板处理泼粪事件方法`、`泼粪离职事件忍5次视频`、`f8fe直播泼粪完整版`。
#:
#: ## 2026-11-12 收敛:12 → 6(砍 通报/离职/处罚/被罚/警方/完整版)
#:
#: **依据是对照实测,不是拍的**:同一天逐词跑探针,被砍的 6 个词里
#: `处罚/被罚/警方` 各只有 0~2 条原始返回、**有效命中 0 条**;`通报/离职/完整版`
#: 的有效命中全在长尾(43 万/87 万/59 万以下)。把它们**全部砍掉后重跑**:
#: 归并事件组数、top10 代表词列表**逐字不变** —— 那些词的命中要么与保留词
#: 归并进同组,要么排序后根本进不了 top。⇒ 请求从 12 次降到 **6 次**,
#: 产出零损失。砍掉的词如果哪天又起量,`hot_carrier_enabled` 的配置项
#: 仍然兜底(表本身可改)。
CARRIER_WORDS = (
    "后续", "回应", "事件", "最新", "来龙去脉", "原因",
)

#: 序列点数**小于**它 = 这个小时内才起量 ⇒ 今天新起的事件。
#: 小时窗口最多 24 个点;**满 24 点的是常青词**(实测 `财经直播`/`东方甄选直播间`/`台球直播`
#: 全是 24 点),而目标 `只忍老板五次直播视频` 是 **13** 点、`医院老奶奶被打后续` 是 **3** 点。
#: ⚠️ 这就是为什么不能用榜单那条 `FRESH_POINTS = 5`:只忍 13 点会被直接漏掉。
CARRIER_FULL_POINTS = 23

#: 形态词探针的量门槛。实测峰值分布:≥15 万有 117 个、≥5 万有 195 个、<1 万只有 10 个。
#: 取 **15 万** —— 再往下进的是「小事件的不同说法」,而本模块只取前几个名额。
CARRIER_MIN_PEAK = 150_000

#: 同一事件的不同说法要归并(Jaccard 二元组相似度)。
#: 实测校准(只忍一族 10 种说法两两比):
#: `泼粪离职事件处理`~`泼粪离职事件处理方法` = **0.78**、~`忍5次视频` = 0.42、
#: ~`彻底疯狂…回放` = 0.38、~`老板处理泼粪事件方法` = 0.29。
#: 取 **0.3**:能串起整族(靠传递闭包),而 `只忍老板五次直播视频`~`只忍五次满了直播账号` 只有 0.20
#: 仍能靠中间词连上,不会把不相关的并进来。
CARRIER_MERGE_JACCARD = 0.3

#: 形态词探针每轮最多交几个**归并后的事件**。一个事件通常有 3~7 种说法,取多了是同义反复。
CARRIER_TOP_N = 5



def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度(只对短串用,`O(len(a)*len(b))` 完全够)。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _belongs(word: str, seed: str) -> bool:
    """`word` 是不是**真的属于** `seed` 这一族,而不是热点宝"相关"出来的路人。

    ⚠️ 必须做这道闸,实测证据:种子 `墨尔本七个小矮人` 会返回 `接亲七个枣核含义`
    —— 只是**共用了「七」这个字**,跟墨尔本事件毫无关系。热点宝的 keyword 过滤是
    **模糊相关**,不是包含关系。

    ## 判据 = 最长公共子串长度

    | seed | word | LCS | 判定 |
    |---|---|---|---|
    | 墨尔本七个小矮人 | 墨尔本华人外卖员遇刺身亡 | `墨尔本`=3 | ✅ 收 |
    | 墨尔本七个小矮人 | 接亲七个枣核含义 | `七个`=2 | ❌ 挡 |
    | 覆面系(3 字) | 黑白覆面塌 | `覆面`=2 | ✅ 收 |
    | 玲玲(2 字) | 玲玲夫妇是什么瓜 | `玲玲`=2 | ✅ 收 |

    ⚠️ **为什么不用二元组覆盖率**:`墨尔本七个小矮人` 有 7 个二元组,而真事件的标题
    只共用「墨尔本」两个 ⇒ 覆盖率 2/7=0.29,门槛 0.5 会把**真事件全挡掉**。
    长串种子的正确判法是"**有没有一段够长的共同片段**",不是"覆盖了多少比例"。
    """
    s = str(seed or "").strip()
    w = str(word or "").strip()
    if len(s) < 2 or not w:
        return False
    if s in w or w in s:              # 包含关系直接算同族(`覆面系` ⊂ `黑白覆面系cos`)
        return True
    #: **去掉虚字再试一次**:`狗爹和小桃` → `狗爹小桃`,于是 `狗爹小桃vlog合集` 能认出。
    #: 观众/推广号写名字时经常把「和/与/的」省掉,这不是另一个名字。
    bare_s = re.sub(r"[和与的、,，\s]", "", s)
    bare_w = re.sub(r"[和与的、,，\s]", "", w)
    if len(bare_s) >= 2 and (bare_s in bare_w or bare_w in bare_s):
        return True
    need = 2 if len(s) <= 3 else 3    #: 2 字种子(玲玲/吃瓜)只能要求 2 字;长种子要求 3 字
    return _lcs_len(s, w) >= need


class HotWord(NamedTuple):
    """一个**已经被观察到**的热点词;字段全部可核查(没有任何预测值)。"""

    word: str
    last: int           #: 最新一天的热度
    peak: int           #: 序列里最高那一天
    days: int           #: 这个搜索词有多少个日点
    emerge: int         #: **首现距今几天**(0=今天才有量;-1=判断不了)
    source: str         #: 从哪个种子词查回来的(可回溯)


def _values(trends) -> list[int]:
    """`trends` → `[value, ...]`(保持日期顺序)。畸形点跳过,不抛。"""
    out: list[int] = []
    for p in trends or []:
        if isinstance(p, dict) and p.get("value") is not None:
            try:
                out.append(int(p["value"]))
            except (TypeError, ValueError):
                continue
    return out


def _emerge_days(vals: list[int]) -> int:
    """这个搜索词**有量**是从几天前开始的:0=今天,1=昨天…;判断不了给 -1。

    判据 = 从末尾往前数,连续多少天 `>= NOISE_FLOOR`。
    """
    if not vals:
        return -1
    n = 0
    for v in reversed(vals):
        if v < NOISE_FLOOR:
            break
        n += 1
    return n - 1 if n else -1


def _shape(vals: list[int]) -> str:
    """形态,**只用于展示**,不参与排序(见模块文档第 5/6 条:它预测不了未来)。"""
    if len(vals) <= 2:
        return "新出现"
    base = statistics.median(vals[:-1]) or 0
    if base <= 0:
        return "新出现"
    mult = vals[-1] / base
    if mult >= 50:
        return "阶跃"
    if mult >= 5:
        return "爬升"
    if mult < 1.5:
        return "平稳"
    return "波动"


def _raw_trends(cookie: str, seed: str, settings: Settings | None) -> dict[str, list[int]]:
    """`{搜索词: [每日热度]}` —— 直连 client 取 `trends`。

    ⚠️ 为什么不用 `douhot.fetch_keyword_items`:它把 `trends` **算掉**了(只回
    `trend_growth`),而本模块要的是「哪天首现」⇒ 必须有原始日序列。同一份数据、
    同一次请求,不重复出网。
    """
    from app.services.douhot_client import DouhotClient, DouhotError

    try:
        raw = DouhotClient(cookie, settings).hot_search(
            limit=50, keyword=seed, date_window=SEED_WINDOW)
    except DouhotError as exc:
        logger.warning("热点定向查询失败 seed=%s: %s", seed, exc)
        return {}
    out: dict[str, list[int]] = {}
    for it in raw or []:
        w = str(it.get("key_word") or "").strip()
        if w:
            out[w] = _values(it.get("trends"))
    return out


def emerging(cookie: str, seeds: list[str], settings: Settings | None = None,
             *, min_last: int = MIN_LAST, max_seeds: int = 3,
             exclude: set[str] | None = None) -> list[HotWord]:
    """从 `seeds` 这几族里挑出**最近才首现**的热点词(首现越新越前,同一天量大的在前)。

    ## 判据(全部来自实测,见模块文档)

    1. `last >= min_last` —— 太小的多半是泛词;
    2. `0 <= emerge <= EMERGE_DAYS-1` —— 最近两天才首现(**唯一有区分度的信号**);
    3. 不在 `exclude` 里(种子本身当然"一直在榜上")。

    ## 为什么不用"突增倍数"

    实测次日翻倍率非单调(2~5 倍 5.8%、10~30 倍 13.5%、100 倍以上 7.5%,基线 5.2%),
    会把它排到前面 —— 典型误判:`爆炸桃` 124 倍,实际是**水果**。

    ⚠️ **这条路只是补位**:实测它只能产出**已知事件的变体**,发现不了新事件
    (用户口径:每一个都是独立的事件)。主通路是 `board_words`(全榜,无种子)。
    """
    exclude = {str(x).strip() for x in (exclude or set()) if str(x).strip()}
    seen: dict[str, HotWord] = {}
    for seed in [str(s).strip() for s in seeds if str(s).strip()][:max_seeds]:
        if seed in exclude:
            continue
        family: list[HotWord] = []
        for word, vals in _raw_trends(cookie, seed, settings).items():
            if not word or word in exclude or word in seen or not vals:
                continue
            if not _belongs(word, seed):     #: 热点宝的 keyword 是"模糊相关",要自己收紧
                continue
            last = vals[-1]
            emerge = _emerge_days(vals)
            if last < min_last or emerge < 0 or emerge > EMERGE_DAYS - 1:
                continue
            family.append(HotWord(word=word, last=last, peak=max(vals), days=len(vals),
                                  emerge=emerge, source=seed))
        #: ⚠️ **同族只留量最大的几个**(实测:墨尔本那族一天首现 13 个变体,全是同一事件)。
        family.sort(key=lambda h: -h.last)
        for h in family[:PER_SEED_CAP]:
            seen[h.word] = h
    out = list(seen.values())
    out.sort(key=lambda h: (h.emerge, -h.last))   #: 今天 > 昨天;同一天量大的在前
    return out


def read_cookie(settings: Settings) -> str:
    """热点宝 Cookie;读不到返回 `""`(调用方据此跳过,不报错)。"""
    f = Path(getattr(settings, "douhot_cookie_file", "data/douhot_cookie.txt"))
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def board_words(cookie: str, settings: Settings | None = None,
                *, min_last: int = SEARCH_MIN_LAST, fresh: int = FRESH_POINTS,
                exclude: set[str] | None = None) -> list[HotWord]:
    """**无种子通路**:直接从搜索榜捞出「今天才起」的独立事件。

    ## 为什么要有这条路(用户 2026-11-08 口径)

    > 「热点事件名称**是没有关联性的**,**每一个都是独立的事件**」

    ⇒ 拿已知名字当种子去扩展,只能找到**已知事件的变体**(`墨尔本七个小矮人` 一族
    一天刷出 13 种说法),**永远发现不了全新事件**。要发现新事件,只能看**全榜**。

    ## 判据(全部实测,见模块文档)

    1. 序列点数 `<= fresh` —— 小时级榜单上「刚上榜几小时」= 当天新起;
    2. `trends[-1] >= min_last` —— 太小的是长尾词条,不是事件;
    3. 不在 `_BOARD_STOP` / `exclude` 里。

    ⚠️ 这条路**不判断"热度会不会涨"** —— 实测突增倍数与次日翻倍率不单调相关
    (见模块文档第 4 条),排序只用**已发生的事实**:新 → 旧、量大的在前。
    """
    from app.services.douhot_client import DouhotClient, DouhotError

    exclude = {str(x).strip() for x in (exclude or set()) if str(x).strip()}
    client = DouhotClient(cookie, settings)
    seen: dict[str, HotWord] = {}
    for sub_type in BOARD_WINDOWS:
        try:
            raw = client.hot_search(limit=50, date_window=1, sub_type=sub_type)
        except DouhotError as exc:
            logger.warning("搜索榜拉取失败 sub_type=%s: %s", sub_type, exc)
            continue
        for it in raw or []:
            word = str(it.get("key_word") or "").strip()
            if not word or word in exclude or word in seen or word in _BOARD_STOP:
                continue
            vals = _values(it.get("trends"))
            if not vals or len(vals) > fresh or vals[-1] < min_last:
                continue
            seen[word] = HotWord(word=word, last=vals[-1], peak=max(vals),
                                 days=len(vals), emerge=0, source="搜索榜")
    out = list(seen.values())
    out.sort(key=lambda h: (h.days, -h.last))   #: 刚上榜的先;同样新则量大的先
    return out


# ---------------------------------------------------------------- 形态词探针

def _bigrams(text: str) -> set[str]:
    """中文串切二元组(去掉标点空白),用于判「两个词是不是同一件事的不同说法」。"""
    chars = [c for c in text if c.isalnum()]
    if len(chars) < 2:
        return set(chars)
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def _same_event(a: str, b: str, threshold: float = CARRIER_MERGE_JACCARD) -> bool:
    """两个搜索词是不是**同一个事件的不同说法**(Jaccard 二元组相似度)。

    ⚠️ 刻意**不做"一个包含另一个"就判同** —— `泼粪离职事件处理` 与
    `老板处理泼粪事件方法` 是同一件事但不互相包含,靠相似度才连得上。
    """
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return False
    return len(ba & bb) / len(ba | bb) >= threshold


def _group_events(words: list[HotWord],
                  threshold: float = CARRIER_MERGE_JACCARD) -> list[list[HotWord]]:
    """把同一事件的不同说法**分组**(每组内按峰值降序)。差分的正确性靠它。

    ⚠️ **为什么要交分组而不是只交代表**:只忍一族实测有 10 种说法。若差分只记
    "今天那条代表词",明天另一种说法涨过它就又成了"新事件"⇒ 同一件事会被反复推。
    调用方必须把**整组词**都记进 `hot_word_seen`,判"新"时只要组里有**任一个**见过
    就整组算旧。
    """
    n = len(words)
    if n <= 1:
        return [[w] for w in words]
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if _same_event(words[i].word, words[j].word, threshold):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    groups: dict[int, list[HotWord]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(words[i])
    out = [sorted(g, key=lambda h: -h.peak) for g in groups.values()]
    out.sort(key=lambda g: -g[0].peak)
    return out


def _merge_events(words: list[HotWord]) -> list[HotWord]:
    """把**同一事件的不同说法**归并,每个事件留一条代表(量最大的那条当名字)。

    实测必要性(2026-11-10,只忍一族):同一次探针里 10 种说法全被捞到
    (`泼粪离职事件处理`/`…处理方法`/`…忍5次视频`/`直播泼粪离职满五次直播`/
    `彻底疯狂泼粪离职事件回放`/`老板处理泼粪事件方法`…),不归并就是同一件事刷满一张卡。

    做法:**并查集**(传递闭包)—— A~B、B~C 都相似时三条并成一组。
    ⚠️ 传递闭包会把"链式相近"的也并进来,所以阈值不能定太低(实测 0.3 刚好)。

    ⚠️ 实测确实会并掉**同一事件的不同地点**(`常州地震官方通报`~`泰州地震官方通报`
    =0.75、`乐平/弋阳/江西巨响官方通报`)。这在本模块是**对的** —— 我们要的是一个
    能去抖音搜的搜索词,江苏多地地震/江西多地巨响本就是同一轮新闻。
    """
    return [g[0] for g in _group_events(words)]


def carrier_words(cookie: str, settings: Settings | None = None,
                  *, carriers: tuple[str, ...] | None = None,
                  min_peak: int | None = None,
                  full_points: int | None = None,
                  top: int | None = None,
                  merge_jaccard: float | None = None,
                  return_groups: bool = False):
    """**不靠固定词条**找新事件:用「事件语法壳子」去查全量词库。

    见上方 `CARRIER_WORDS` 的注释(为什么是形态词、为什么榜单那条路救不了)。

    判据三件:
      1. **序列点数 < `full_points`** —— 满 24 点的是常青词(`财经直播`),
         半截序列说明它是这一小时内才起量的;
      2. **峰值 ≥ `min_peak`** —— 太小的多半是小事件的各种说法;
      3. 不在 `_BOARD_STOP` 里,且**能匹配到形态词本身**(接口有时会放宽匹配)。

    ⚠️ 这条路**不读也不写数据库** —— 它只负责"捞"。差分的状态由调用方
    (`seen_before` / `remember`)管,这样纯函数可测、也不会在慢活的循环里写库
    (本仓在 `new-write-point-holds-lock-over-slowwork` 上栽过两次)。

    :param carriers: 覆盖默认形态词表(单测用)。
    :param top: 归并后最多交几个事件。
    :param return_groups: 为真时返回 `(representatives, groups)`。`groups` 是
        **同一事件的全部说法**(每组代表排第一),差分要用它 —— 只记代表词的话,
        明天另一种说法涨过代表词就会被当成"新事件"重复推(只忍一族实测有 10 种说法)。
    """
    from app.services.douhot_client import DouhotClient, DouhotError

    #: 参数优先级:显式传参(单测) > settings > 模块常量。全用 `None` 兜底是为了
    #: **不把默认值在定义时就固化成常量** —— 那样配置项改了也不生效。
    if min_peak is None:
        min_peak = int(getattr(settings, "hot_carrier_min_peak", CARRIER_MIN_PEAK))
    if full_points is None:
        full_points = int(getattr(settings, "hot_carrier_full_points",
                                  CARRIER_FULL_POINTS))
    if top is None:
        top = int(getattr(settings, "hot_carrier_top_n", CARRIER_TOP_N))
    if merge_jaccard is None:
        merge_jaccard = float(getattr(settings, "hot_carrier_merge_jaccard",
                                      CARRIER_MERGE_JACCARD))

    client = DouhotClient(cookie, settings)
    found: dict[str, HotWord] = {}
    for carrier in (carriers or CARRIER_WORDS):
        try:
            raw = client.hot_search(limit=50, date_window=1, sub_type=3001,
                                    keyword=carrier)
        except DouhotError as exc:
            logger.warning("形态词探针失败 %s: %s", carrier, exc)
            continue
        for it in raw or []:
            word = str(it.get("key_word") or "").strip()
            if not word or word in found or word in _BOARD_STOP:
                continue
            #: 接口声称按 keyword 过滤,但**不假定它一定照做** —— 不沾形态词的直接丢,
            #: 否则一次探针就会把整张榜塞进来(实测返回 50 条全带该词,但那是当前行为,
            #: 不是契约)。
            if carrier not in word:
                continue
            vals = _values(it.get("trends"))
            if not vals:
                continue
            peak = max(vals)
            if len(vals) >= full_points or peak < min_peak:
                continue
            found[word] = HotWord(word=word, last=vals[-1], peak=peak,
                                  days=len(vals), emerge=0, source=f"形态词:{carrier}")
    #: `_group_events` 已按每组峰值降序排好,组内代表在 `g[0]`。
    groups = _group_events(list(found.values()), threshold=merge_jaccard)
    top_groups = groups[:top]
    reps = [g[0] for g in top_groups]
    if return_groups:
        return reps, top_groups
    return reps


def seen_before(session, user_id: int, words: list[str]) -> set[str]:
    """这批词里**以前见过**的(差分用)。

    ⚠️ 没有这一步,每轮探到的 200+ 个词会原样重推 —— 4 小时一轮就是一天 6 次。
    """
    from sqlalchemy import select

    from app.db.models import HotWordSeen

    ids = [str(w).strip() for w in (words or []) if str(w).strip()]
    if not ids or session is None:
        return set()
    return set(session.scalars(
        select(HotWordSeen.word).where(
            HotWordSeen.user_id == user_id,
            HotWordSeen.word.in_(ids))).all())


def unfed_search_words(session, user_id: int, *, top: int = 3,
                       max_len: int = 12) -> list[str]:
    """★ **取「还没喂过搜索」的探针词**(2026-11-12)喂 `douyin_leads`。

    ## 为什么需要这一层

    探针词此前只有一条去处:`demand_watch` 评论扫描 —— 实测**空转**(名字恒 0):
    探针捞的是新闻/剧情解说类热点,评论区是「围观/追剧」不是「求资源」。
    而库里真正搬成功的名字全是短剧/小说名,来自 `douyin_leads` 的**搜索词**。
    ⇒ 探针词该同时进搜索词表:抖音推广号标题写《口令》,**只有搜索能命中**。

    ## 为什么从 `hot_word_seen` 读而不是再打一次探针

    `new_hot_words` 每轮已经差分记账(6 个 HTTP);这里只是**换一个消费者**,
    再打一次探针就是 6 个重复请求。读库不读网,零额外成本。

    ## 判据

    - `fed_search_at IS NULL` —— 还没喂过(**喂过没产出**的不再喂,那会烧搜索额度;
      词本身还热的话探针下轮会刷新 `last_at`,但 `fed_search_at` 不重置);
    - `last_at` 近 24h —— 还在发酵的才值得搜,凉词不占名额;
    - 词长 ≤ `max_len` —— 与 `douyin_leads._HOT_MAX_LEN` 同一取舍(事件句太长,
      推广号标题写不下,搜了也命不中),但放宽到 12:探针词天然带形态词后缀
      (`…后续`/`…事件`),按 8 会把好词挡掉一半;
    - 按峰值降序 —— 大事件优先。
    """
    from sqlalchemy import select

    from app.db.models import HotWordSeen

    if session is None or top <= 0:
        return []
    since = datetime.now() - timedelta(hours=24)
    rows = session.execute(
        select(HotWordSeen.word, HotWordSeen.peak).where(
            HotWordSeen.user_id == user_id,
            HotWordSeen.fed_search_at.is_(None),
            HotWordSeen.last_at >= since).order_by(
            HotWordSeen.peak.desc()).limit(top * 4)).all()
    out = [str(w).strip() for w, _ in rows
           if str(w).strip() and len(str(w).strip()) <= max_len]
    return out[:top]


def mark_fed_search(session, user_id: int, words: list[str]) -> int:
    """给这批词打上「已喂过搜索」的戳(`fed_search_at`)。返回打戳条数。

    ⚠️ **由调用方在搜索完成后打**(不是取词时):搜索挂了词没消耗,下轮还能再喂。
    """
    from sqlalchemy import select

    from app.db.models import HotWordSeen

    if session is None or not words:
        return 0
    now = datetime.now()
    ids = [str(w).strip() for w in words if str(w).strip()]
    rows = session.scalars(select(HotWordSeen).where(
        HotWordSeen.user_id == user_id,
        HotWordSeen.word.in_(ids))).all()
    for r in rows:
        r.fed_search_at = now
    session.flush()
    return len(rows)


def remember(session, user_id: int, rows: list[HotWord]) -> int:
    """把这些词记进 `hot_word_seen`(已存在的只更新 `last_at`)。返回新增条数。

    ⚠️ 调用方必须**在慢活之后**调它(本仓栽过"循环里写一次 + 调外部接口 156 秒")。
    """
    from sqlalchemy import select

    from app.db.models import HotWordSeen

    if session is None or not rows:
        return 0
    now = datetime.now()
    words = [h.word for h in rows]
    known = {r.word: r for r in session.scalars(
        select(HotWordSeen).where(
            HotWordSeen.user_id == user_id,
            HotWordSeen.word.in_(words))).all()}
    added = 0
    for h in rows:
        row = known.get(h.word)
        if row is not None:
            row.last_at = now
            row.peak = max(float(row.peak or 0), float(h.peak or 0))
            continue
        session.add(HotWordSeen(
            user_id=user_id, word=h.word, seen_at=now, last_at=now,
            peak=float(h.peak or 0), carrier=str(h.source or "")[:32]))
        added += 1
    session.flush()
    return added


def prune_seen(session, user_id: int, *, keep_days: int = 14) -> int:
    """删掉 `keep_days` 天没再出现的词,返回删除条数(这张表会一直变大)。"""
    from sqlalchemy import delete

    from app.db.models import HotWordSeen

    if session is None or keep_days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=keep_days)
    res = session.execute(delete(HotWordSeen).where(
        HotWordSeen.user_id == user_id, HotWordSeen.last_at < cutoff))
    session.flush()
    return int(res.rowcount or 0)



def hot_video_seeds(settings: Settings, seeds: list[str], *,
                    limit: int = 3) -> list[str]:
    """给「扫评论区」用的**新热点搜索词**(按首现新→旧取前 `limit` 个)。

    降级链见 `_candidates`(形态词探针 → 1 小时榜 → 种子族扩展)。读 Cookie、
    吞掉一切失败 —— 调用方(定时作业)不该因为热点宝挂了就整轮不跑。

    ⚠️ 这里**不做差分**(每次都交同一批还在发酵的事件)。要"只交新的"用
    `new_hot_words`。
    """
    cookie = read_cookie(settings)
    if not cookie:
        return []
    #: ⚠️ **取多再截**:下游还要差分/再取前 N ⇒ 这里多要一点,别让它先被截断。
    rows = _candidates(settings, seeds, cookie, limit=max(limit, CARRIER_TOP_N))
    out = [h.word for h in rows][:limit]
    if out:
        logger.info("新热点搜索词:%s", "、".join(f"{h.word}({h.source})" for h in rows[:limit]))
    return out


def new_hot_words(settings: Settings, seeds: list[str], *, session=None,
                  user_id: int = 0, limit: int = 3,
                  keep_days: int | None = None) -> list[HotWord]:
    """**带差分**的热点取词:只交**以前没见过**的事件,并把见过的记下来。

    这是真正决定"产出"的那一层 —— `carrier_words` 每轮都会捞到同一批还在发酵的事件,
    不差分的话 4 小时一轮就是**一天推 6 次同一件事**。

    ## 差分是按**事件组**做的,不是按词

    只忍一族实测一句话能有 10 种写法(`泼粪离职事件处理`/`…忍5次视频`/`彻底疯狂…回放`…)。
    若只把"代表词"记进库,明天另一种写法涨过它就会被判成"新事件"再推一次。
    所以:组里**任何一个写法**见过 ⇒ **整组**都算旧;记账时**整组所有写法**都写进去。

    ## 写锁纪律(2026-11-10)

    ⚠️ 本函数**不能**借调用方的长事务记账(本仓栽过"循环里写一次 + 调外部接口 156 秒")。
    `session=None` 时**自己开一个短会话**,写完立刻 `commit()` + `close()`,锁只拿几毫秒;
    取数(6 个 HTTP 请求,2026-11-12 起词表收敛到 6)在**开会话之前**就做完了。
    调用方要复盘时传自己的 session(单测就是这样注入的)。

    ⚠️ `session=None` 且 `user_id=0` 时**不做差分**,原样返回 —— 手工脚本要能单独跑探针。
    """
    cookie = read_cookie(settings)
    if not cookie:
        return []
    #: ⚠️ 这里**故意多取一些**:差分之后可能一条不剩,得多留余量。
    reps, groups = _candidates(settings, seeds, cookie,
                               limit=max(limit * 4, 12), return_groups=True)
    if not reps:
        return []
    if not user_id:
        return reps[:limit]
    if keep_days is None:
        keep_days = int(getattr(settings, "hot_word_keep_days", 14))
    every = [h for g in groups for h in g]

    #: 取数已完成,从这里开始才碰库。
    own = session is None
    if own:
        from app.db import get_session_local
        session = get_session_local()()
    try:
        seen = seen_before(session, user_id, [h.word for h in every])
        fresh = [g[0] for g in groups if not any(h.word in seen for h in g)]
        #: **整组**都记账(见过的写法也要更新 `last_at`,否则清理会把还热的事件删掉)。
        remember(session, user_id, every)
        prune_seen(session, user_id, keep_days=keep_days)
        if own:
            session.commit()
    finally:
        if own:
            session.close()
    logger.info("热点差分:候选%d事件/说法%d 其中新%d → %s", len(groups), len(every),
                len(fresh),
                "、".join(h.word for h in fresh[:limit]) or "(无新事件)")
    return fresh[:limit]


def _candidates(settings: Settings, seeds: list[str], cookie: str,
                *, limit: int, return_groups: bool = False):
    """降级链:形态词探针 → 1 小时榜 → 种子族扩展。

    返回代表词列表(`HotWord`);`return_groups=True` 时返回
    `(代表词, 事件组列表)`,组里是同一事件的全部说法 —— 差分要靠它。
    """
    reps: list[HotWord] = []
    groups: list[list[HotWord]] = []
    if bool(getattr(settings, "hot_carrier_enabled", True)):
        try:
            #: ⚠️ **必须把 limit 传下去**:`carrier_words` 默认只交配置的 `top_n` 个,
            #: 不传的话调用方要的"多取一点给差分留余量"就白写了(实测差分会退化成 0)。
            reps, groups = carrier_words(
                cookie, settings, top=max(limit, CARRIER_TOP_N), return_groups=True)
        except Exception:  # noqa: BLE001 - 探针挂了不影响榜单那条
            logger.warning("形态词探针取词失败", exc_info=True)
    if not reps:
        try:
            reps = board_words(cookie, settings)
            groups = [[h] for h in reps]
        except Exception:  # noqa: BLE001
            logger.warning("无种子热点取词失败", exc_info=True)
    if not reps and seeds:
        try:
            reps = emerging(cookie, seeds, settings)
            groups = [[h] for h in reps]
        except Exception:  # noqa: BLE001
            logger.warning("新热点取词失败", exc_info=True)
            reps, groups = [], []
    reps, groups = reps[:limit], groups[:limit]
    return (reps, groups) if return_groups else reps

