"""热搜 → **吃瓜候选**筛选(2026-10-25,测试版)。

## 为什么要一个单独模块

`weibo_hot_items` / `baidu_hot_items` 那两张表是**采集落库**用的,而且热榜作业跑在
**远程 hotspot 侧**(本机库里停在 09-29)。而"今天有什么能吃瓜"这件事需要**实时**,
所以这里直接调三家的抓取口,**不写库、不排程**。

## 三个来源(2026-10-25 当场实测)

| 平台 | 入口 | 实测 |
|---|---|---|
| 微博 | `collector.fetch_hot_search`(要 Cookie) | 52 条 |
| 百度 | `baidu.fetch_hot`(免登录) | 30 条 |
| 抖音 | 公开接口 `iesdouyin.com/web/api/v2/hotsearch/billboard/word/` | 50 条 |

⚠️ 抖音的**热点宝** Cookie 已失效(`DouhotAuthError`),而 `/hot` 页面会给**验证码中间页**
⇒ 走公开榜单接口;那条路**免登录**,只要 UA/Referer。

## 两道筛选

1. **关键词打分**(`score`):强信号★3 / 弱信号★1 / **硬排除社会与政务词**。
   它便宜、可解释,但**分不清"公众人物"和"普通人"**。
2. **模型判定**(`judge_all`):把**全部**热搜交给模型判「是不是公众人物的娱乐瓜」。
   关键词只留着**排序**和"模型挂了"时的兜底。

> 2026-10-25 实测:只用关键词时,「老板回应南开大学学生时薪高10元」
> 「妈妈给儿子准备40万结婚钱 女儿报警」都被收进候选(命中「回应/结婚」);
> 加上模型判定后只剩「廖碧儿承认恋情」「王楚淇终于结婚了」两条真瓜。

## 已知边界

- 抖音榜单的 `hot_value` 与微博/百度量纲不同 ⇒ **只用于同平台排序**。
- 模型漏答的条目**一律不收**(宁可少,不可滥);某一批判定失败 ⇒ 整轮退回关键词并说明。
"""
from __future__ import annotations

import re

from app.utils import get_logger

logger = get_logger(__name__)

#: 强信号:出现一个就够可疑
_STRONG = (
    "出轨", "恋情", "绯闻", "分手", "离婚", "复合", "官宣", "结婚", "怀孕", "生子",
    "道歉", "辟谣", "回应", "塌房", "爆料", "实锤", "互撕", "前妻", "前夫",
    "前女友", "前男友", "亲吻", "牵手", "夜宿", "婚变", "小三", "原配",
    "恋情曝光", "恋情疑似", "约炮", "偷拍", "私生", "解约", "撕破脸",
)
#: 弱信号:自己不够,但配合强信号/人物词时加分
_WEAK = ("明星", "演员", "歌手", "网红", "偶像", "爱豆", "粉丝", "出道", "综艺",
         "剧组", "片场", "直播", "热搜")

#: 硬排除:不是瓜,而且混进来会让整张候选表失去可信度
_BLOCK = (
    "书记", "县委", "市委", "省委", "国务院", "人大", "政协", "主席", "总书记",
    "地震", "救援", "台风", "洪水", "疫情", "疫苗", "高考", "考研", "公务员",
    "比分", "赛果", "决赛", "夺冠", "联赛", "季后赛", "男单", "女单",
    "股价", "涨幅", "跌", "金价", "楼市", "房价", "降息", "加息", "GDP",
    "光复", "周年", "演习", "导弹", "军舰",
)


def _norm(title: str) -> str:
    """跨平台去重用的归一化:去空白/标点,保留中文数字字母。"""
    return re.sub(r"[^\w一-鿿]+", "", title or "").lower()


def score(title: str) -> tuple[int, list[str]]:
    """给一条标题打分 → `(分数, 命中的词)`。命中硬排除直接 0 分。"""
    t = title or ""
    hit_block = [w for w in _BLOCK if w in t]
    if hit_block:
        return 0, [f"排除:{hit_block[0]}"]
    hits: list[str] = []
    total = 0
    for w in _STRONG:
        if w in t:
            hits.append(w)
            total += 3
    for w in _WEAK:
        if w in t and w not in hits:
            hits.append(w)
            total += 1
    return total, hits


def fetch_weibo(settings=None) -> list[dict]:
    """微博热搜。要 Cookie;失败**抛**,由 `fetch_all` 记下来。"""
    from config.settings import get_settings
    from app.services import collector

    items = collector.fetch_hot_search(settings or get_settings())
    return [{"platform": "微博", "title": getattr(x, "title", ""),
             "heat": int(getattr(x, "heat", 0) or 0),
             "rank": int(getattr(x, "rank", 0) or 0)} for x in items]


def fetch_baidu(settings=None) -> list[dict]:
    """百度热搜(免登录)。"""
    from config.settings import get_settings
    from app.services import baidu

    items = baidu.fetch_hot(settings or get_settings())
    return [{"platform": "百度", "title": getattr(x, "title", ""),
             "heat": int(getattr(x, "heat", 0) or 0),
             "rank": int(getattr(x, "rank", 0) or 0)} for x in items]


#: 抖音公开热榜接口(**免登录**;热点宝那条 Cookie 已失效,`/hot` 页面会给验证码中间页)
_DOUYIN_URL = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"


def fetch_douyin(timeout: float = 15.0) -> list[dict]:
    """抖音热榜(公开接口)。"""
    import requests

    r = requests.get(_DOUYIN_URL, timeout=timeout, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Referer": "https://www.douyin.com/"})
    if r.status_code >= 400:
        raise RuntimeError(f"抖音热榜 HTTP {r.status_code}")
    words = (r.json() or {}).get("word_list") or []
    out: list[dict] = []
    for i, it in enumerate(words, 1):
        title = str(it.get("word") or "").strip()
        if not title:
            continue
        out.append({"platform": "抖音", "title": title,
                    "heat": int(it.get("hot_value") or 0), "rank": i})
    return out


def fetch_all(settings=None) -> tuple[list[dict], dict[str, str]]:
    """三个平台一起拉 → `(条目, {平台: 失败原因})`。

    ⚠️ **单个平台失败不许拖垮整轮**,但必须**说出来**(返回的第二个值)——
    "微博今天没热搜"与"微博抓取挂了"长得一模一样,这正是本仓最忌讳的假结论。
    """
    items: list[dict] = []
    fails: dict[str, str] = {}
    for name, fn in (("微博", fetch_weibo), ("百度", fetch_baidu), ("抖音", fetch_douyin)):
        try:
            got = fn(settings) if name != "抖音" else fn()
            logger.info("热搜:%s %d 条", name, len(got))
            items.extend(got)
        except Exception as exc:  # noqa: BLE001 - 逐个失败逐个记
            fails[name] = f"{type(exc).__name__}: {str(exc)[:120]}"
            logger.warning("热搜:%s 抓取失败 —— %s", name, fails[name])
    return items, fails


def _merge(items: list[dict]) -> list[dict]:
    """按标题跨平台合并(同一件事在多个平台都上榜 ⇒ 说明它真在发酵)。"""
    merged: dict[str, dict] = {}
    for it in items or []:
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        key = _norm(title)
        sc, hits = score(title)
        cur = merged.get(key)
        if cur is None:
            merged[key] = {"title": title, "score": sc, "matched": hits,
                           "platforms": [it.get("platform", "")],
                           "heat": int(it.get("heat") or 0),
                           "rank": int(it.get("rank") or 0)}
            continue
        cur["score"] = max(cur["score"], sc)
        for h in hits:
            if h not in cur["matched"]:
                cur["matched"].append(h)
        pl = it.get("platform", "")
        if pl and pl not in cur["platforms"]:
            cur["platforms"].append(pl)
        cur["heat"] = max(cur["heat"], int(it.get("heat") or 0))
        cur["rank"] = min(int(cur["rank"] or 999), cur["rank"] or 999)
    return list(merged.values())


def _order(groups: list[dict]) -> list[dict]:
    """排序:多平台同时上榜的优先 → 关键词分高的 → 热度高的。"""
    return sorted(groups, key=lambda g: (-len(g["platforms"]), -g["score"], -g["heat"]))


def pick_gossip(items: list[dict], *, min_score: int = 3,
                limit: int = 20) -> list[dict]:
    """**只按关键词**挑候选(`--no-llm` 或模型不可用时用)。"""
    groups = [g for g in _merge(items) if g["score"] >= min_score]
    return _order(groups)[:limit]


#: 单批发给模型的条数(太多它会漏行;40 实测稳定)
_JUDGE_BATCH = 40


def judge_all(items: list[dict], settings=None, *, limit: int = 30) -> tuple[list[dict], str]:
    """**让模型判全部热搜**,关键词只用于排序与兜底 → `(候选, 说明)`。

    ★ 为什么要判全部而不是只判关键词筛过的(2026-10-25):关键词既能**误收**
    (「老板回应南开学生时薪」命中"回应"),也会**漏收**(只用当事人名字的瓜,
    一个瓜词都没有)。判全部才有召回,关键词留着当排序分和"模型挂了"时的兜底。

    ⚠️ 模型不可用/中途失败 ⇒ **退回关键词结果**并说明 —— 空结果与"今天没瓜"同形。
    """
    from config.settings import get_settings

    s = settings or get_settings()
    #: ★ **硬排除必须在模型之前生效**(2026-10-25 实测):模型会把
    #: 「张雪把县委书记丢河里」判成"争议事件、可吃瓜" —— 那是**时政**,一旦做成资料就是事故。
    #: 关键词的黑名单就是用来兜这条底线的,不能因为"交给模型了"就跳过。
    groups = [g for g in _merge(items)
              if not (g["matched"] and str(g["matched"][0]).startswith("排除:"))]
    if not groups:
        return [], ""
    if not getattr(s, "deepseek_api_key", ""):
        return pick_gossip(items, limit=limit), "⚠️ 未配模型 ⇒ 只给关键词结果(可能混入社会新闻)"

    from app.services import llm_client

    verdicts: dict[str, dict] = {}
    for i in range(0, len(groups), _JUDGE_BATCH):
        batch = groups[i:i + _JUDGE_BATCH]
        got = llm_client.judge_gossip_titles(
            s.deepseek_base_url, s.deepseek_api_key, s.deepseek_model,
            [g["title"] for g in batch])
        if got is None:
            #: 任何一批失败 ⇒ **整轮退回关键词**(半套 AI + 半套关键词最难解释)
            return pick_gossip(items, limit=limit), \
                "⚠️ AI 判定中途失败 ⇒ 退回关键词结果(可能混入社会新闻)"
        for j in got:
            verdicts[j["title"]] = j

    kept: list[dict] = []
    for g in groups:
        v = verdicts.get(g["title"])
        if not v or not v.get("is_gossip"):
            continue                      #: 模型没答/判否的**一律不收**(宁可少,不可滥)
        g = dict(g)
        g["reason"] = v.get("reason", "")
        kept.append(g)
    note = (f"AI 判定:全部 {len(groups)} 条热搜 → 保留 {len(kept)} 条"
            f"(关键词读不出的瓜也能收进来,社会新闻被剔掉)")
    return _order(kept)[:limit], note
