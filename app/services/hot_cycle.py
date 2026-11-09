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


def hot_video_seeds(settings: Settings, seeds: list[str], *,
                    limit: int = 3) -> list[str]:
    """给「扫评论区」用的**新热点搜索词**(按首现新→旧取前 `limit` 个)。

    **优先无种子通路**(`board_words`:全榜独立事件);它取不到时才退回种子族扩展
    (`emerging`:已知事件的变体)。读 Cookie、吞掉一切失败 —— 调用方(定时作业)
    不该因为热点宝挂了就整轮不跑。
    """
    cookie = read_cookie(settings)
    if not cookie:
        return []
    rows: list[HotWord] = []
    try:
        rows = board_words(cookie, settings)
    except Exception:  # noqa: BLE001 - 热点是"加分项",拿不到就退回原有素材
        logger.warning("无种子热点取词失败", exc_info=True)
    if not rows and seeds:
        try:
            rows = emerging(cookie, seeds, settings)
        except Exception:  # noqa: BLE001
            logger.warning("新热点取词失败", exc_info=True)
            return []
    out = [h.word for h in rows][:limit]
    if out:
        logger.info("新热点搜索词:%s", "、".join(f"{h.word}({h.source})" for h in rows[:limit]))
    return out
