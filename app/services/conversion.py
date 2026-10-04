"""平台曝光 → **预估拉新量**(2026-10-04)。

用户给定口径:
    公众号 阅读数 × **30%**  /  抖音 分享数 × **80%**  /  其余平台 播放量 × **40%**

⚠️ **这是"需求侧估算",不是我方实收**。三个系数乘的都是**别人的号 / 别人的视频**的曝光量
(对标号的文章阅读数、线索视频的转发量),算出来的是
**「这个资源在平台上有多热 ⇒ 预估能带来多少拉新」**,不等于我们自己的拉新成果。
用途(用户口径):① 选题权重 ② 结算/复盘 ③ **管理群**汇报。
**不给客户群看具体数字** —— 客户群只看到"火爆程度"分级(见 `heat_level`)。

⚠️ **"播放量"在很多平台根本取不到**(2026-10-04 实测 MediaCrawler 的搜索接口):
    小红书 → liked_count / collected_count / comment_count / share_count(**无播放量**)
    贴吧   → total_replay_num(回复数)(**无播放量,连浏览量都没有**)
    抖音   → liked_count / collected_count / comment_count / share_count
所以按下面的**降级阶梯**取"曝光量",并**必须把用的是哪一档带出去**(`basis`)。
⚠️ **不许静默替换**:0.4 乘在"播放量"和乘在"回复数"上,量纲与含义都不同,
用户看到数字时必须知道它是怎么来的。
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- 单一事实源
# 平台 → 转化率。**改口径只改这里**,别在调用方散落魔数。
PLATFORM_FACTORS: dict[str, float] = {
    "wechat": 0.30,      # 公众号:阅读数 × 30%
    "douyin": 0.80,      # 抖音:分享数 × 80%
}
DEFAULT_FACTOR = 0.40    # 其余平台:播放量 × 40%

# 各平台的**指定指标**(用户点名要用的那个);取不到才走阶梯
PLATFORM_PRIMARY: dict[str, str] = {
    "wechat": "read_num",
    "douyin": "share_count",
}

# 降级阶梯:越靠前越接近"曝光量"的本义。**顺序即优先级**。
#   play/view  = 真·曝光
#   share      = 传播(抖音的口径就是这个)
#   engage     = 互动总量(赞+藏+评+转)—— 最后的兜底,量纲最小
EXPOSURE_LADDER: tuple[str, ...] = ("play_count", "view_count", "share_count")

# 兜底合成"互动总量"时,加哪几个字段
ENGAGE_KEYS: tuple[str, ...] = ("liked_count", "collected_count", "comment_count", "share_count")

# 各平台字段名差异(媒体爬虫/自研路径给的键不一样,这里归一)
ALIASES: dict[str, tuple[str, ...]] = {
    "play_count": ("play_count", "play_num", "view", "play", "vv"),
    "view_count": ("view_count", "view_num", "read_num", "visit_count", "total_replay_page"),
    "share_count": ("share_count", "share_num", "repost_count", "forward_count"),
    "liked_count": ("liked_count", "like_count", "like_num", "zan_num", "voteup_count",
                    "digg_count", "up_count"),
    "collected_count": ("collected_count", "collect_count", "fav_count", "favorite_count"),
    "comment_count": ("comment_count", "comments", "total_replay_num", "reply_count"),
}


def _pick(metrics: dict, key: str) -> int | None:
    """按别名表取一个非负整数指标;取不到返回 None(**不是 0** —— "没有"与"是0"不同)。"""
    for alias in ALIASES.get(key, (key,)):
        if alias not in metrics:
            continue
        try:
            v = int(float(str(metrics[alias]).strip() or 0))
        except (TypeError, ValueError):
            continue
        if v >= 0:
            return v
    return None


def _engage(metrics: dict) -> int | None:
    """互动总量(赞+藏+评+转)。全都没取到 → None(**别把"不知道"报成 0**)。"""
    parts = [_pick(metrics, k) for k in ENGAGE_KEYS]
    got = [p for p in parts if p is not None]
    return sum(got) if got else None


def factor_for(platform: str) -> float:
    """该平台的转化率(`wechat` 0.30 / `douyin` 0.80 / 其余 0.40)。"""
    return PLATFORM_FACTORS.get((platform or "").lower(), DEFAULT_FACTOR)


def estimate(platform: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """**曝光 → 预估拉新量**。返回结构化结果,拿不到曝光时 `estimate` 为 `None`。

    返回 `{"platform","factor","basis","metric","value","estimate","degraded"}`:
      - `basis`   : 用了哪一档(`play_count` / `view_count` / `share_count` / `engage`)
      - `metric`  : 实际取到的那个字段名(便于回查)
      - `degraded`: True = **没用上用户点名的那个指标**,而是降级取来的
                    ⇒ 下游**必须**把它标出来,不能当等效数字用

    ⚠️ **拿不到就返回 None,不返回 0** —— 0 会被读成"这个资源没人看",而事实是"我们不知道"。
    """
    p = (platform or "").lower()
    fac = factor_for(p)
    primary = PLATFORM_PRIMARY.get(p)

    if primary:
        val = _pick(metrics, primary)
        if val is not None:
            return {"platform": p, "factor": fac, "basis": primary, "metric": primary,
                    "value": val, "estimate": int(val * fac), "degraded": False}

    for key in EXPOSURE_LADDER:
        val = _pick(metrics, key)
        if val is not None:
            return {"platform": p, "factor": fac, "basis": key, "metric": key,
                    "value": val, "estimate": int(val * fac),
                    "degraded": primary is not None}      # 点名了却没取到 = 降级

    eng = _engage(metrics)
    if eng is not None:
        return {"platform": p, "factor": fac, "basis": "engage", "metric":",".join(ENGAGE_KEYS),
                "value": eng, "estimate": int(eng * fac), "degraded": primary is not None}

    return {"platform": p, "factor": fac, "basis": "", "metric": "", "value": 0,
            "estimate": None, "degraded": primary is not None}


# ---------------------------------------------------------------- 给客户群看的"火爆程度"
# ⚠️ 客户群**不给具体数字**(用户口径:具体多少推管理群)。这里只出档位。
HEAT_LEVELS: tuple[tuple[int, str], ...] = (
    (10_000, "🔥🔥🔥 极热"),
    (1_000, "🔥🔥 很热"),
    (100, "🔥 偏热"),
)


def heat_level(estimate_value: int | None) -> str:
    """预估拉新量 → **火爆程度档位**(给客户群/卡片用;不出具体数字)。"""
    if estimate_value is None:
        return "—"              # 不知道就说不知道,别报"冷"
    for threshold, label in HEAT_LEVELS:
        if estimate_value >= threshold:
            return label
    return "一般"


def describe(result: dict[str, Any]) -> str:
    """一句话说清"这个预估是怎么来的"(管理群汇报用)。

    形如:`预估 1204(抖音:分享数 1505 × 0.8)`;降级时**明写降级**。
    """
    if result.get("estimate") is None:
        return "预估 —(没有可用的曝光指标)"
    suffix = " ⚠️降级指标" if result.get("degraded") else ""
    basis_cn = {"play_count": "播放量", "view_count": "浏览量", "share_count": "分享数",
                "engage": "互动总量", "read_num": "阅读数"}.get(result["basis"], result["basis"])
    return (f"预估 {result['estimate']}({result['platform']}:{basis_cn} {result['value']}"
            f" × {result['factor']}){suffix}")
