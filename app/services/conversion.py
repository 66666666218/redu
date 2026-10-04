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
    贴吧   → total_replay_num(回复数)(**无播放量,连点赞都没有**)
    抖音   → liked_count / collected_count / comment_count / share_count
**GitHub 上查证了根因**:小红书/抖音的播放量是「**创作者私有数据**」——
只对自己的号可见,别人的内容拿不到(见 `cwjcw/xhs_douyin_content` 的 README)。

所以取曝光的顺序是:
    ① **真·播放量**(B站有;抖音原始响应里有,待补抓)
    ② **点赞量推算**(用户口径:看不到播放量的按点赞量算 → `LIKE_AS_PLAY_RATIO`)
    ③ **互动总量**(只剩连"赞"都没有的平台,如贴吧)

⚠️ **不许静默替换**:0.4 乘在"播放量"和乘在"点赞推算的曝光"上,可信度差很多,
调用方**必须**把 `basis` 带出去。
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
#   play_count  = 真·曝光(**首选**)
#   like_derived= **点赞量 ÷ 点赞率**(用户口径:看不到播放量的按点赞量算)
#   engage      = 互动总量(赞+藏+评+转)—— 只剩连"赞"都没有的平台时兜底(如贴吧)
EXPOSURE_LADDER: tuple[str, ...] = ("play_count", "like_derived", "engage")

# ⚠️ **点赞率假设**:用户口径"看不到播放量的就按点赞量的 10%"。
# 按字面 ×0.1 会让"曝光"小于"点赞"(点赞是播放的子集,量纲反了),所以按
# **点赞约占播放量的 10%** 解读 ⇒ **曝光 ≈ 点赞量 ÷ 10% = 点赞量 × 10**。
# ⚠️ 这是个**经验值,不是实测值**;若口径理解错了,改这一个数即可(测试已钉住两种方向)。
LIKE_AS_PLAY_RATIO = 0.10

# 兜底合成"互动总量"时,加哪几个字段
ENGAGE_KEYS: tuple[str, ...] = ("liked_count", "collected_count", "comment_count", "share_count")

# 各平台字段名差异(媒体爬虫/自研路径给的键不一样,这里归一)
ALIASES: dict[str, tuple[str, ...]] = {
    "play_count": ("play_count", "play_num", "view", "play", "vv"),
    "view_count": ("view_count", "view_num", "read_num", "visit_count"),
    "share_count": ("share_count", "share_num", "repost_count", "forward_count"),
    "liked_count": ("liked_count", "like_count", "like_num", "zan_num", "voteup_count",
                    "digg_count", "up_count"),
    "collected_count": ("collected_count", "collect_count", "fav_count", "favorite_count"),
    "comment_count": ("comment_count", "comments", "total_replay_num", "reply_count"),
}


# ⚠️ **这些指标取到 0 时,当成"平台没给",不是"真的是 0"**(2026-10-04 实测定案):
# 抖音的 `play_count` 对**别人的视频恒为 0**(203 条实测,非 0 的 0 条)——
# 播放量是创作者私有数据。若把 0 当真实值,一个"点赞 4013"的笔记会被算成
# 「曝光 0 × 0.4 = 预估 0」,而它其实是热门 —— 数字会精确地骗人。
# 判据:曝光量若为 0 而下方互动非 0,在物理上不可能;所以 0 只能是"缺失"。
ZERO_MEANS_MISSING: tuple[str, ...] = ("play_count", "view_count")


def _pick(metrics: dict, key: str) -> int | None:
    """按别名表取一个非负整数指标;取不到返回 None(**不是 0** —— "没有"与"是0"不同)。"""
    zero_ok = key not in ZERO_MEANS_MISSING      # 曝光类:0 视为缺失
    for alias in ALIASES.get(key, (key,)):
        if alias not in metrics:
            continue
        try:
            v = int(float(str(metrics[alias]).strip() or 0))
        except (TypeError, ValueError):
            continue
        if v > 0 or (v == 0 and zero_ok):
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

    取曝光的顺序:① 真·播放量 → ② **点赞量推算**(用户口径:看不到播放量的按点赞量算)
    → ③ 互动总量(连"赞"都没有的平台,如贴吧)。

    返回 `{"platform","factor","basis","metric","raw","value","estimate","degraded"}`:
      - `basis`  : `play_count` / `like_derived` / `engage` —— **用了哪一档**
      - `metric` : 原始指标名(便于回查);`raw` = 原始值
      - `value`  : **实际参与计算的曝光量**(`like_derived` 时 = 点赞量 ÷ 点赞率,≠ raw)
      - `degraded`: True = **没用上该平台点名的指标**(抖音点名分享、公众号点名阅读数)
                    ⇒ 下游**必须**标出来,不能当等效数字用

    ⚠️ **拿不到就返回 None,不返回 0** —— 0 会被读成"这个资源没人看",而事实是"我们不知道"。
    """
    p = (platform or "").lower()
    fac = factor_for(p)
    primary = PLATFORM_PRIMARY.get(p)

    def _make(basis: str, metric: str, raw: int, exposure: int) -> dict[str, Any]:
        return {"platform": p, "factor": fac, "basis": basis, "metric": metric,
                "raw": raw, "value": exposure, "estimate": int(exposure * fac),
                "degraded": primary is not None}

    # ① 平台点名了指标,先按点名取(公众号=阅读数 / 抖音=分享数)
    if primary:
        val = _pick(metrics, primary)
        if val is not None:
            out = _make(primary, primary, val, val)
            out["degraded"] = False
            return out

    # ② 真·播放量
    play = _pick(metrics, "play_count")
    if play is not None:
        return _make("play_count", "play_count", play, play)

    # ③ 点赞量推算(用户口径)
    likes = _pick(metrics, "liked_count")
    if likes is not None:
        return _make("like_derived", "liked_count", likes,
                     int(likes / LIKE_AS_PLAY_RATIO) if LIKE_AS_PLAY_RATIO else likes)

    # ④ 互动总量兜底(贴吧这类连"赞"都没有的)
    eng = _engage(metrics)
    if eng is not None:
        return _make("engage", ",".join(ENGAGE_KEYS), eng, eng)

    return {"platform": p, "factor": fac, "basis": "", "metric": "", "raw": 0, "value": 0,
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

    形如:`预估 1204(抖音:分享数 1505 × 0.8)`;点赞推算时**把推算过程写出来**
    (`点赞 4013 推算曝光 40130`),降级时**明写降级**。
    """
    if result.get("estimate") is None:
        return "预估 —(没有可用的曝光指标)"
    basis_cn = {"play_count": "播放量", "like_derived": "点赞推算", "engage": "互动总量",
                "read_num": "阅读数", "share_count": "分享数"}.get(result["basis"], result["basis"])
    src = f"{basis_cn} {result.get('raw', result['value'])}"
    if result["basis"] == "like_derived" and result["value"] != result.get("raw"):
        # 把"点赞 → 曝光"这一步摊开写,免得读者以为曝光就是点赞数
        src += f"(按点赞率 {LIKE_AS_PLAY_RATIO:.0%} 推算曝光 {result['value']})"
    suffix = " ⚠️降级指标" if result.get("degraded") else ""
    return (f"预估 {result['estimate']}({result['platform']}:{src}"
            f" × {result['factor']}){suffix}")
