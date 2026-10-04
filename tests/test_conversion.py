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


def test_others_prefer_play_then_view_then_share() -> None:
    """其余平台按"播放量"优先;没有播放量才降级 —— **阶梯顺序即优先级**。"""
    r = cv.estimate("bilibili", {"play_count": 1000, "view_count": 500, "share_count": 10})
    assert r["basis"] == "play_count" and r["estimate"] == 400

    r = cv.estimate("zhihu", {"view_count": 500, "share_count": 10})       # 无播放量
    assert r["basis"] == "view_count" and r["estimate"] == 200

    r = cv.estimate("xiaohongshu", {"share_count": 419})                    # 只剩分享
    assert r["basis"] == "share_count" and r["estimate"] == int(419 * 0.40)


def test_engage_is_the_last_resort_ladder() -> None:
    """★ 实测事实:小红书/贴吧的搜索接口**根本没有播放量**。
    小红书实测字段 = liked/collected/comment/share;贴吧只有 total_replay_num。

    阶梯顺序 `播放 → 浏览 → 分享 → 互动总量`:分享**排在互动之前**(它更接近"曝光/传播"),
    所以小红书有 `share_count` 时会走分享档。⚠️ 这个选择是**有代价的**:
    同一条笔记(赞 4013/藏 6498/评 28/转 419)走分享档只算出 167,走互动档是 4383 —— **差 26 倍**。
    本测试把两种口径都钉住,口径若要改(把 `share_count` 从阶梯里拿掉)一眼能看出影响面。
    """
    xhs = cv.estimate("xiaohongshu", {"liked_count": "4013", "collected_count": "6498",
                                      "comment_count": "28", "share_count": "419"})
    assert xhs["basis"] == "share_count", "分享档优先于互动档(阶梯顺序即优先级)"
    assert xhs["estimate"] == int(419 * 0.40)

    # 没有分享档时才落到互动总量
    xhs2 = cv.estimate("xiaohongshu", {"liked_count": "4013", "collected_count": "6498",
                                       "comment_count": "28"})
    assert xhs2["basis"] == "engage"
    assert xhs2["value"] == 4013 + 6498 + 28          # 字符串也要能转
    assert xhs2["estimate"] == int(xhs2["value"] * 0.40)

    tieba = cv.estimate("tieba", {"total_replay_num": 1, "total_replay_page": 1})
    # 贴吧用的是**回复数**当曝光 —— 量纲与"播放量"差着数量级,所以必须带出来
    assert tieba["value"] == 1 and tieba["basis"] in ("view_count", "engage")


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
