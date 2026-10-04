"""曝光 → 预估拉新量(2026-10-04)。

口径由用户给定:公众号 阅读数×30% / 抖音 分享数×80% / 其余 平台 播放量×40%。
这里钉住的是**最容易被悄悄改坏的三件事**:系数、用哪个指标、拿不到时给什么。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import conversion as cv  # noqa: E402


def test_factors_match_the_agreed_rates() -> None:
    """三个系数是**业务口径**,不是随手写的常数 —— 改它必须是有意的。"""
    assert cv.factor_for("wechat") == 0.30
    assert cv.factor_for("douyin") == 0.80
    for other in ("zhihu", "tieba", "xiaohongshu", "kuaishou", "bilibili", "weibo", ""):
        assert cv.factor_for(other) == 0.40, other


def test_wechat_uses_read_num_and_douyin_uses_share_count() -> None:
    """★ 用户点名的指标**必须**优先于阶梯 —— 抖音的 0.8 是乘在**分享数**上的,不是播放量。"""
    w = cv.estimate("wechat", {"read_num": 1151, "zan_num": 99999})
    assert w["basis"] == "read_num" and w["value"] == 1151
    assert w["estimate"] == int(1151 * 0.30)          # zan_num 更大也不许抢
    assert w["degraded"] is False

    d = cv.estimate("douyin", {"share_count": 4406, "liked_count": 61})
    assert d["basis"] == "share_count" and d["value"] == 4406
    assert d["estimate"] == int(4406 * 0.80)
    assert d["degraded"] is False


def test_others_prefer_play_count_then_like_derived() -> None:
    """其余平台:① 真·播放量优先 → ② **点赞量推算**(用户口径:"按点赞量的 10%")。"""
    r = cv.estimate("bilibili", {"play_count": 1000, "liked_count": 5000})
    assert r["basis"] == "play_count" and r["value"] == 1000 and r["raw"] == 1000
    assert r["estimate"] == 400                       # 有真播放量就不许用点赞推算

    # 没播放量的平台(小红书/知乎/快手…)→ 走点赞推算
    r = cv.estimate("xiaohongshu", {"liked_count": 4013})
    assert r["basis"] == "like_derived" and r["metric"] == "liked_count"
    assert r["raw"] == 4013                            # 原始点赞数要留着
    assert r["value"] == 40130                         # 曝光 = 点赞 ÷ 10%
    assert r["estimate"] == int(40130 * 0.40)


def test_like_derived_direction_is_pinned() -> None:
    """★ **方向必须钉死**:曝光 = 点赞 **÷** 10%(点赞约占播放 10%)。

    ⚠️ 口径若被理解反(×0.1),曝光会比点赞还小 —— 而点赞是播放的子集,量纲就反了。
    这条测试保证改错方向时立刻变红。
    """
    r = cv.estimate("zhihu", {"voteup_count": 1000})       # 知乎的赞字段名
    assert r["value"] > r["raw"], "曝光必须**大于**点赞(点赞是播放的子集)"
    assert r["value"] == 10000
    assert cv.LIKE_AS_PLAY_RATIO == 0.10


def test_engage_only_when_there_is_not_even_a_like() -> None:
    """★ 实测:贴吧搜索接口**只有回复数**,连点赞都没有 ⇒ 才落到互动总量兜底。"""
    tieba = cv.estimate("tieba", {"total_replay_num": 7})
    assert tieba["basis"] == "engage" and tieba["value"] == 7
    assert tieba["estimate"] == int(7 * 0.40)

    # 有赞就用赞,不用互动总量
    xhs = cv.estimate("xiaohongshu", {"liked_count": "4013", "collected_count": "6498",
                                      "comment_count": "28", "share_count": "419"})
    assert xhs["basis"] == "like_derived", "有赞就不该用互动总量"
    assert xhs["value"] == 40130


def test_unknown_is_none_not_zero() -> None:
    """⚠️ **拿不到就 None,绝不返回 0** —— 0 会被读成"这资源没人看",而事实是"我们不知道"。
    (与 `admit_transfer` 的"不知道≠满"、探针失败的"不知道≠空"是同一条纪律。)"""
    r = cv.estimate("zhihu", {})
    assert r["estimate"] is None and r["value"] == 0
    assert "—" in cv.describe(r)
    assert cv.heat_level(None) == "—"


def test_degraded_is_flagged_when_the_named_metric_is_missing() -> None:
    """★ 抖音没取到 `share_count` 而走了别的档 ⇒ **必须标 degraded**。
    否则 0.8 会乘在量纲完全不同的数上,报表里看不出任何异常。"""
    d = cv.estimate("douyin", {"liked_count": 100})          # 只有点赞,没有分享
    assert d["degraded"] is True and d["estimate"] is not None
    assert "降级" in cv.describe(d)

    # 公众号同理:没有 read_num 而拿 zan_num 顶替 → 标降级
    w = cv.estimate("wechat", {"zan_num": 50})
    assert w["degraded"] is True


def test_describe_spells_out_the_basis() -> None:
    """管理群汇报要能一眼看出"这数是怎么来的",而不是只给一个孤零零的数字。"""
    txt = cv.describe(cv.estimate("douyin", {"share_count": 1505}))
    assert "预估 1204" in txt and "分享数 1505" in txt and "0.8" in txt


def test_heat_level_never_leaks_the_number() -> None:
    """★ **客户群只看火爆程度,不给具体数字**(用户口径:具体多少推管理群)。"""
    for v in (None, 0, 5, 500, 5_000, 50_000):
        label = cv.heat_level(v)
        assert not any(ch.isdigit() for ch in label), f"{v} 的档位泄漏了数字:{label}"
    assert "🔥🔥🔥" in cv.heat_level(50_000)
    assert "—" == cv.heat_level(None)


def test_negative_or_garbage_metrics_are_treated_as_unknown() -> None:
    """平台给 `-1`(未给出)或乱码时,当"不知道"处理 —— 别乘出负数。"""
    assert cv.estimate("bilibili", {"play_count": -1, "view_count": "n/a"})["estimate"] is None
    assert cv._pick({"play_count": -5}, "play_count") is None


def test_zero_play_count_means_missing_not_zero() -> None:
    """★ **实测定案**(2026-10-04):抖音的 `play_count` 对**别人的视频恒为 0**
    (203 条实测,非 0 的 **0** 条)—— 播放量是创作者私有数据。

    ⚠️ 若把 0 当真实值:一条"点赞 4013"的笔记会被算成「曝光 0 × 0.4 = **预估 0**」,
    而它明明是热门 —— 数字会**精确地**骗人(不是大得离谱,而是小得像个结论)。
    判据:曝光为 0 而互动非 0 在物理上不可能 ⇒ 0 只能是"缺失"。
    """
    r = cv.estimate("xiaohongshu", {"play_count": 0, "liked_count": 4013})
    assert r["basis"] == "like_derived", "play_count=0 必须当成「平台没给」"
    assert r["estimate"] > 0

    # 抖音点名了分享数,不受这条影响(用户口径:抖音还是采用转发计数)
    d = cv.estimate("douyin", {"play_count": 0, "share_count": 4406})
    assert d["basis"] == "share_count" and d["estimate"] == int(4406 * 0.80)

    # 真·播放量(非 0)照用
    b = cv.estimate("bilibili", {"play_count": 1000, "liked_count": 50})
    assert b["basis"] == "play_count" and b["estimate"] == 400

    # 只有 0、什么都没有 → 拿不到就是拿不到
    assert cv.estimate("xiaohongshu", {"play_count": 0})["estimate"] is None


def test_view_count_ranks_before_like_derived() -> None:
    """★ 知乎搜索**直接给浏览量**(`visits_count`)。浏览量是与"播放量"同类的**曝光**,
    比"点赞 ÷ 10% 推算"更接近本义 —— 所以阶梯里排在 `like_derived` **之前**。
    (用户口径里它就是"播放量"那一档,只是平台叫法不同。)
    """
    r = cv.estimate("zhihu", {"view_count": 51906, "liked_count": 25})
    assert r["basis"] == "view_count", "有浏览量就不该退到点赞推算"
    assert r["estimate"] == int(51906 * 0.40)

    # ⚠️ 字段名坑:**搜索接口是 `visits_count`(带 s)、详情接口是 `visit_count`** —— 两个都要认
    assert cv.estimate("zhihu", {"visits_count": 1000})["basis"] == "view_count"
    assert cv.estimate("zhihu", {"visit_count": 1000})["basis"] == "view_count"


def test_zhihu_search_metrics_are_extracted() -> None:
    """知乎 `answer` 对象**直接带**这四个指标(实测 2026-10-04),不用再打详情接口。"""
    from app.services.cross_accounts import _zhihu_metrics

    obj = {"type": "answer", "visits_count": 51906, "voteup_count": 25,
           "comment_count": 1, "favorites_count": 78}
    assert _zhihu_metrics(obj) == {"liked_count": 25, "view_count": 51906,
                                   "comment_count": 1, "collected_count": 78}
    # `article` 没有 visits_count(知乎不暴露文章浏览量)→ 只带赞与评论
    art = {"type": "article", "voteup_count": 7, "comment_count": 1, "zfav_count": 55}
    m = _zhihu_metrics(art)
    assert "view_count" not in m and m["liked_count"] == 7 and m["collected_count"] == 55
    # 全 0 / 缺失 → 空 dict(让 conversion 去走降级,别填 0 冒充"没人看")
    assert _zhihu_metrics({}) == {}
    assert _zhihu_metrics({"voteup_count": 0, "visits_count": 0}) == {}


def test_first_principle_is_never_overridden_by_other_metrics() -> None:
    """★★ **用户口径:抖音第一原则=转发量,公众号第一原则=阅读数 —— 不许混。**

    这两条是**业务定性**,不是"哪个数大用哪个":
      · 抖音:分享数才是这单生意里真正对应"有人想去拿资源"的动作;
      · 公众号:阅读数是唯一直接可见的触达。
    所以哪怕别的指标**更大、更"像曝光"**,也**不许顶掉**它们。
    """
    # 抖音:play_count / liked_count 再大也不能顶掉 share_count
    d = cv.estimate("douyin", {"share_count": 100, "play_count": 9_999_999,
                               "liked_count": 500_000})
    assert d["basis"] == "share_count" and d["value"] == 100
    assert d["estimate"] == int(100 * 0.80)
    assert d["degraded"] is False

    # 公众号:其他指标再大也不能顶掉 read_num
    w = cv.estimate("wechat", {"read_num": 50, "share_count": 9_999_999,
                               "liked_count": 9_999_999})
    assert w["basis"] == "read_num" and w["value"] == 50
    assert w["estimate"] == int(50 * 0.30)
    assert w["degraded"] is False

    # ⚠️ **但第一原则取不到时必须标 degraded** —— 不能悄悄换成别的数还说"这就是抖音的数"
    d2 = cv.estimate("douyin", {"liked_count": 1000})
    assert d2["degraded"] is True and d2["basis"] != "share_count"
