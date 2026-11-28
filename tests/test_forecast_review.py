"""**预判命中回验**(`app/services/forecast_review.py`)的口径守卫。

## 为什么值得钉

这个作业的产出**直接决定"要不要把预判投进客户群"**。它错的方式都很安静 ——
数字照出、报表照发,只是结论反了。四类错误各自对着一件真事:

① **交错的冷却键解析错** ⇒ 三条链三种键形(`forecast:a|b` / `breakout:档:名` /
   `forecast:名`),一个正则套三个 ⇒ `breakout:起势:巫师4` 变成含冒号的假名字,
   回库匹配**永远 0**,而且不报错。
② **把预警前的旧内容算成命中** ⇒ 麦穗与羊在预警前 3 天我们已经搬过,
   按"库里有就算"会把它记成一次完美预判。
③ **把三档证据并成一个"命中"** ⇒ 贴吧灌水农场(同一批漫剧合集被反复重发)
   能把命中率刷到很好看,而它证明的不是"预判准",是"贴吧在灌水"。
④ **`unknown` 被当成 `miss`** ⇒ 游戏/影视预告根本不在我们的采样矩阵里,
   "我们看不见"会被读成"它没火",于是结论变成"预判没用"。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: F401  —— 必须先导入,`Base.metadata` 才会登记全部表
from app.db.database import Base  # noqa: E402
from app.db.models import (DiscoveredPanLink, DouyinLead, FeishuAlert,  # noqa: E402
                           ForecastReview, PushLedger, User, WechatArticle)
from app.services import forecast_review as fr  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class _S:
    """最小 settings 替身 —— 只给这个作业读的四个字段。"""

    forecast_review_enabled = True
    forecast_review_after_hours = 72
    forecast_review_days = 14
    forecast_review_batch = 50


def _alert(session, section, key, *, hours_ago=100, reason=""):
    session.add(FeishuAlert(user_id=1, section=section, title=key[:200],
                            reason=reason, alerted_at=datetime.now() - timedelta(hours=hours_ago)))
    session.commit()


def _ledger(session, title, *, pan="https://pan.quark.cn/s/abc123def456",
            hours_ago=1, author="同行A", section="bilibili"):
    session.add(PushLedger(user_id=1, section=section, item_key=f"{author}-{title}",
                           title=title, pan_url=pan, pan_type="夸克", author=author,
                           pushed_at=datetime.now() - timedelta(hours=hours_ago)))
    session.commit()


# ---------------------------------------------------------------- ① 键解析

def test_subject_of_three_key_shapes():
    """三条链的冷却键形态**不一样**,各解各的。"""
    # promo_forecast:`forecast:<盘链>|<作品名>`
    work, pan = fr._subject_of("promo_forecast", "forecast:pan.quark.cn/s/abc|麦穗与羊")
    assert work == "麦穗与羊"
    assert pan == "pan.quark.cn/s/abc"

    # hot_breakout:`breakout:<档位>:<作品名>` —— 中间那段档位必须剥掉
    work, pan = fr._subject_of("hot_breakout", "breakout:起势:巫师4")
    assert work == "巫师4", "档位不许留在名字里,否则回库匹配永远 0"
    assert pan == ""

    # hot_forecast:`forecast:<seed>` —— 与 promo 同前缀但**没有 `|`**
    work, pan = fr._subject_of("hot_forecast", "forecast:两京十五日如何从中挽回观众")
    assert work == "两京十五日如何从中挽回观众"
    assert pan == ""


def test_hot_breakout_key_with_colon_in_name():
    """作品名自己带冒号时,`split(':', 2)` 只切前两段 —— 名字不许被切坏。"""
    work, _ = fr._subject_of("hot_breakout", "breakout:预告:蜘蛛侠:超越宇宙")
    assert work == "蜘蛛侠:超越宇宙"


# ---------------------------------------------------------------- ② 时间边界

def test_never_counts_evidence_before_alert(session):
    """**预警之前**的推送不算命中 —— 这条是本模块最容易写错的一处。"""
    _alert(session, "promo_forecast", "forecast:pan.xunlei.com/s/X|麦穗与羊", hours_ago=100)
    # 预警前 4 天就已经有人在推了
    _ledger(session, "《麦穗与羊》完整版", hours_ago=100 + 96)

    row = fr.review_one(session, 1, "promo_forecast",
                        "forecast:pan.xunlei.com/s/X|麦穗与羊", "",
                        datetime.now() - timedelta(hours=100), "", 72)
    assert row is not None
    assert row["outcome"] == "unknown", "预警前的旧内容不许算命中"
    assert row["detail"] == "peer=0 douyin=0 pan=0"


def test_counts_evidence_after_alert(session):
    _alert(session, "hot_breakout", "breakout:起势:巫师4", hours_ago=100)
    _ledger(session, "《巫师4》最新预告完整版", hours_ago=10)

    row = fr.review_one(session, 1, "hot_breakout", "breakout:起势:巫师4", "",
                        datetime.now() - timedelta(hours=100), "", 72)
    assert row["outcome"] == "hit"
    assert row["evidence"] == "peer"
    assert row["lag_hours"] is not None and 0 < row["lag_hours"] < 100


def test_not_due_returns_none(session):
    """没到 72h 的**不写行**(下一轮再来)。"""
    row = fr.review_one(session, 1, "hot_breakout", "breakout:起势:巫师4", "",
                        datetime.now() - timedelta(hours=10), "", 72)
    assert row is None


# ---------------------------------------------------------------- ③ 证据分档

def test_pan_evidence_is_separate_tier(session):
    """贴吧灌水型证据必须单独成档 —— **不许**和 `peer` 混成一个"命中"。"""
    _alert(session, "hot_breakout", "breakout:起势:青蛇", hours_ago=100)
    session.add(DiscoveredPanLink(
        user_id=1, platform="tieba", origin_url="https://pan.baidu.com/s/xyz",
        title="多部完结版漫剧！超甜超好看 喜欢自取 百度网盘 《青蛇传 - 前世今生》",
        status="ok", found_at=datetime.now() - timedelta(hours=5)))
    session.commit()

    row = fr.review_one(session, 1, "hot_breakout", "breakout:起势:青蛇", "",
                        datetime.now() - timedelta(hours=100), "", 72)
    assert row["outcome"] == "hit"
    assert row["evidence"] == "pan", "最弱的那档必须如实标 pan,不许抬成 peer"
    assert row["detail"] == "peer=0 douyin=0 pan=1"


def test_peer_beats_pan_when_both_present(session):
    """两档都有时 `evidence` 记**最强**那档。"""
    _alert(session, "hot_breakout", "breakout:起势:巫师4", hours_ago=100)
    _ledger(session, "《巫师4》完整版", hours_ago=10)
    session.add(DiscoveredPanLink(
        user_id=1, platform="tieba", origin_url="https://pan.baidu.com/s/y",
        title="《巫师4》完整版 网盘", status="ok",
        found_at=datetime.now() - timedelta(hours=2)))
    session.commit()

    row = fr.review_one(session, 1, "hot_breakout", "breakout:起势:巫师4", "",
                        datetime.now() - timedelta(hours=100), "", 72)
    assert row["evidence"] == "peer"
    assert row["detail"] == "peer=1 douyin=0 pan=1"


def test_wechat_article_counts_as_peer(session):
    """公众号同行号推带盘文章 = "这资源变成网盘资源"的直接证据。"""
    _alert(session, "hot_breakout", "breakout:起势:巫师4", hours_ago=100)
    session.add(WechatArticle(
        user_id=1, title="《巫师4》高清完整版资源", author="同行号",
        url="https://mp.weixin.qq.com/s/x", my_pan_urls="https://pan.quark.cn/s/q1",
        created_at=datetime.now() - timedelta(hours=8)))
    session.commit()

    row = fr.review_one(session, 1, "hot_breakout", "breakout:起势:巫师4", "",
                        datetime.now() - timedelta(hours=100), "", 72)
    assert row["evidence"] == "peer"


def test_douyin_lead_only_share_kind_counts(session):
    """抖音线索只认 `kind='share'`(真带口令/转存链);`none` 只是提到了名字。

    `none` 那条**不是命中**,但也不是 unknown —— 它证明"名字出现了、没兑现"⇒ miss。
    """
    _alert(session, "hot_breakout", "breakout:起势:巫师4", hours_ago=100)
    for kind, mark in (("none", "巫师4"), ("share", "巫师4")):
        session.add(DouyinLead(
            user_id=1, aweme_id=f"vid-{kind}", title=f"《{mark}》",
            mark=mark, kind=kind, keyword="k", author=f"号-{kind}",
            found_at=datetime.now() - timedelta(hours=6)))
    session.commit()

    row = fr.review_one(session, 1, "hot_breakout", "breakout:起势:巫师4", "",
                        datetime.now() - timedelta(hours=100), "", 72)
    assert row["outcome"] == "hit"
    assert row["evidence"] == "douyin"
    assert row["detail"] == "peer=0 douyin=1 pan=0 mention=1", "kind=none 那条只进提及档"


def test_promo_forecast_matches_by_pan_identity_but_only_as_weak_tier(session):
    """盘链身份能配上(**换名字也算同一条分享**),但**只进最弱的 `pan` 档**。

    ⚠️ 2026-11-25 实测:迅雷一条分享可以挂多个作品 —— 《麦穗与羊》的预警盘链
    同时被 5 行《我的有罪之人》的台账/公众号用着。把"分享配上了"当成
    《麦穗与羊》命中,就是拿别人的合集冒充我们的预判。
    """
    _alert(session, "promo_forecast",
           "forecast:pan.quark.cn/s/7acfc4a22f03|后会友期人格测试", hours_ago=100)
    # 标题完全不同,但盘链同一条
    _ledger(session, "这个测试太好笑了", pan="https://pan.quark.cn/s/7acfc4a22f03",
            hours_ago=4)

    row = fr.review_one(session, 1, "promo_forecast",
                        "forecast:pan.quark.cn/s/7acfc4a22f03|后会友期人格测试", "",
                        datetime.now() - timedelta(hours=100), "", 72)
    assert row["outcome"] == "hit"
    assert row["evidence"] == "pan", "只靠盘链配上的行不许抬成 peer"
    assert row["detail"].startswith("peer=0 douyin=0 pan=1")
    assert row["detail"].endswith("new=1"), "预警前没人推过 ⇒ 这个是新推手"


def test_shared_share_link_does_not_prove_another_work(session):
    """同一分享挂多部作品时,**别部作品**的推送不算本预警的 `peer`。

    实测样本《麦穗与羊》(`...houKA1`) —— 该分享同时挂着《我的有罪之人》。
    """
    _alert(session, "promo_forecast",
           "forecast:pan.xunlei.com/s/VP6XqobV5vdtY7KuROqmhouKA1?pwd=8cze|麦穗与羊",
           hours_ago=100)
    # 同名且带盘的才是真证据
    _ledger(session, "《麦穗与羊》原稿未删版", pan="https://pan.xunlei.com/s/VP6XqobV5vdtY7KuROqmhouKA1?pwd=8cze",
            hours_ago=6)
    # 同一条分享、但标题是别的作品 —— 只许进 pan 档
    _ledger(session, "《我的有罪之人》全集", pan="https://pan.xunlei.com/s/VP6XqobV5vdtY7KuROqmhouKA1?pwd=8cze",
            hours_ago=5, author="同行B")

    row = fr.review_one(session, 1, "promo_forecast",
                        "forecast:pan.xunlei.com/s/VP6XqobV5vdtY7KuROqmhouKA1?pwd=8cze|麦穗与羊",
                        "", datetime.now() - timedelta(hours=100), "", 72)
    assert row["detail"].startswith("peer=1 douyin=0 pan=1"), "别部作品只许算 pan 档"


# ---------------------------------------------------------------- ③b 未命中档

def test_promo_same_authors_are_not_new_evidence(session):
    """★ 去同义反复:预警前就在推的那批人,预警后继续推**不算命中**。

    这条链的预警前提就是"头 24h 已攒够 N 个推手" —— 若不做基线,
    "之后还有人推"几乎必然成立,命中率会永远接近 100%。
    """
    _alert(session, "promo_forecast", "forecast:pan.quark.cn/s/aaa|某剧", hours_ago=100)
    # 预警前 10h:同行A 已经在推(基线窗口内)
    _ledger(session, "《某剧》全集", pan="https://pan.quark.cn/s/aaa", hours_ago=110,
            author="同行A")
    # 预警后:同一个同行A 继续推
    _ledger(session, "《某剧》全集 更新", pan="https://pan.quark.cn/s/aaa", hours_ago=5,
            author="同行A")

    row = fr.review_one(session, 1, "promo_forecast", "forecast:pan.quark.cn/s/aaa|某剧",
                        "", datetime.now() - timedelta(hours=100), "", 72)
    assert row["outcome"] == "miss", "同一批人继续推不是新证据"
    assert row["evidence"] == ""
    assert row["detail"].startswith("peer=1 douyin=0 pan=0")
    assert row["detail"].endswith("new=0")


def test_promo_new_author_is_evidence(session):
    """预警后出现**预警前没推过的人** ⇒ 才是真的"更多人在推"。"""
    _alert(session, "promo_forecast", "forecast:pan.quark.cn/s/bbb|另一剧", hours_ago=100)
    _ledger(session, "《另一剧》全集", pan="https://pan.quark.cn/s/bbb", hours_ago=110,
            author="同行A")
    _ledger(session, "《另一剧》原稿", pan="https://pan.quark.cn/s/bbb", hours_ago=4,
            author="同行C")

    row = fr.review_one(session, 1, "promo_forecast", "forecast:pan.quark.cn/s/bbb|另一剧",
                        "", datetime.now() - timedelta(hours=100), "", 72)
    assert row["outcome"] == "hit"
    assert row["evidence"] == "peer"
    assert row["detail"].endswith("new=1")


def test_ledger_row_without_pan_is_a_miss(session):
    """台账里**不带盘**的行不是命中,但**是未命中的证据** —— 名字出现了、没兑现。"""
    _alert(session, "hot_breakout", "breakout:起势:巫师4", hours_ago=100)
    _ledger(session, "《巫师4》最新预告", pan="", hours_ago=10)

    row = fr.review_one(session, 1, "hot_breakout", "breakout:起势:巫师4", "",
                        datetime.now() - timedelta(hours=100), "", 72)
    assert row["outcome"] == "miss"
    assert row["evidence"] == ""
    assert row["detail"] == "peer=0 douyin=0 pan=0 mention=1"


def test_failure_annotation_is_not_a_pan_link(session):
    """`my_pan_urls` 里的 `⚠️源分享已被封,未转存` 是**失败标注**,不是盘链。

    现网有 92 行是这个形状;把它当带盘证据方向正好反了 ——
    它证明的恰恰是"这条没变成网盘资源"⇒ **miss**,不是 unknown。
    """
    _alert(session, "hot_breakout", "breakout:起势:巫师4", hours_ago=100)
    session.add(WechatArticle(
        user_id=1, title="《巫师4》最新预告", author="同行号",
        url="https://mp.weixin.qq.com/s/y",
        my_pan_urls="⚠️源分享已被封(41031),未转存",
        created_at=datetime.now() - timedelta(hours=6)))
    session.commit()

    row = fr.review_one(session, 1, "hot_breakout", "breakout:起势:巫师4", "",
                        datetime.now() - timedelta(hours=100), "", 72)
    assert row["outcome"] == "miss", "失败标注是未兑现的证据,不是带盘证据"
    assert row["detail"] == "peer=0 douyin=0 pan=0 mention=1"


def test_is_link_shapes():
    """`_is_link` 的判据:真链收、标注/空壳拒。"""
    assert fr._is_link("https://pan.quark.cn/s/abc")
    assert fr._is_link("pan.xunlei.com/s/VP6XqobV5vdtY7KuROqmhouKA")
    assert not fr._is_link("")
    assert not fr._is_link("[]")
    assert not fr._is_link("⚠️源分享已被封(41031),未转存")
    assert not fr._is_link("⚠️原文失效(麦穗与羊｜原稿未删版)")


def test_two_char_names_still_match():
    """2 字资源名(`青蛇`/`斗破`)是常态,**不许**被门槛砍掉。"""
    assert fr._matches(fr._keys("青蛇"), fr._keys("《青蛇传 - 前世今生》合集"))


def test_single_char_never_matches():
    """1 个字的 key 匹配一切,必须挡掉。"""
    assert fr._keys("全") == frozenset()
    assert not fr._matches(fr._keys("全"), fr._keys("《全村的希望》合集"))


def test_reverse_substring_requires_four_chars():
    """反向包含只认 ≥4 字 —— 2~3 字的 key 被任意长标题包含,那就是"匹配一切"。"""
    #: 正向(被观察对象本身很短)是允许的
    assert fr._matches(fr._keys("青蛇"), fr._keys("《青蛇》完整版"))
    #: 反向靠 4 字门槛:内容标题太短时不许倒过来吞掉整个预警名
    long_subject = fr._keys("《巫师4》最新预告完整版")
    assert fr._matches(long_subject, fr._keys("巫师4")) is False


# ---------------------------------------------------------------- ④ 批处理/幂等

def test_review_pending_skips_already_done(session):
    """已回验过的**不再重算** —— 结果要可复现。"""
    key = "breakout:起势:巫师4"
    _alert(session, "hot_breakout", key, hours_ago=100)
    st1 = fr.review_pending(session, 1, _S)
    assert st1["done"] == 1

    # 之后又来了一条更强的证据 —— 不许把已有的 unknown 改成 hit
    _ledger(session, "《巫师4》完整版", hours_ago=1)
    st2 = fr.review_pending(session, 1, _S)
    assert st2["done"] == 0
    assert st2["skipped"] == 1
    row = session.query(ForecastReview).one()
    assert row.outcome == "unknown", "已落库的结论不许被后来的证据改写"


def test_review_pending_ignores_other_sections(session):
    """`douyin_peer` 之类**不是预判链**,不参与回验。"""
    _alert(session, "douyin_peer", "forecast:某个东西", hours_ago=100)
    st = fr.review_pending(session, 1, _S)
    assert st["due"] == 0


def test_review_pending_only_due_rows(session):
    """没到 72h 的**不进 due**(它连候选都不该是)。"""
    _alert(session, "hot_breakout", "breakout:起势:新东西", hours_ago=10)
    _alert(session, "hot_breakout", "breakout:起势:老东西", hours_ago=100)
    st = fr.review_pending(session, 1, _S)
    assert st["due"] == 1
    assert st["done"] == 1


# ---------------------------------------------------------------- 报表

def test_report_keeps_unknown_out_of_the_denominator(session):
    """`unknown` **不进命中率分母** —— "我们看不见"不等于"它没火"。"""
    key_hit = "breakout:起势:巫师4"
    key_unk = "breakout:起势:某个冷门独立游戏"
    _alert(session, "hot_breakout", key_hit, hours_ago=100, reason="起势档/0人/IT之家#2")
    _alert(session, "hot_breakout", key_unk, hours_ago=100, reason="起势档/0人/知乎#9")
    _ledger(session, "《巫师4》完整版", hours_ago=10)
    fr.review_pending(session, 1, _S)

    text = fr.build_report(session, 1, _S, days=14)
    assert "命中 1 / 未命中 0 / 无数据 1" in text
    assert "100%" in text, "unknown 不许进分母(否则这句话会变成 50%)"


def test_report_reports_confirmation_rate_over_all_due(session):
    """★ 三分档都要出:**确认率**的分母是全部到期预警,**判对率**只算有结果的。

    第一版没有 miss 档,`确认率` 会被算成 100%(1 hit / 1 有结果),
    而真实情况是"3 条到期,只兑现了 1 条"。
    """
    _alert(session, "hot_breakout", "breakout:起势:甲作品", hours_ago=100,
           reason="起势档/0人/IT之家#2")
    _alert(session, "hot_breakout", "breakout:起势:乙作品", hours_ago=100,
           reason="起势档/0人/IT之家#3")
    _alert(session, "hot_breakout", "breakout:起势:丙作品", hours_ago=100,
           reason="起势档/0人/IT之家#4")
    _ledger(session, "《甲作品》完整版", hours_ago=10)          # hit
    _ledger(session, "《乙作品》最新消息", pan="", hours_ago=8)  # miss(只提及)
    # 丙作品:什么也没有 → unknown
    fr.review_pending(session, 1, _S)

    text = fr.build_report(session, 1, _S, days=14)
    assert "命中 1 / 未命中 1 / 无数据 1" in text
    assert "确认率(命中/到期) 33%" in text, "分母必须是全部到期,不能只算有结果的"
    assert "判对率(有结果) 50%" in text


def test_report_flags_legacy_rows_without_miss_tier(session):
    """旧口径行(无 miss 档)必须单独标出来,不许和新口径混成一个百分比。"""
    session.add(ForecastReview(
        user_id=1, section="hot_breakout", key="breakout:起势:老东西",
        title="老东西", alerted_at=datetime.now() - timedelta(days=2),
        outcome="hit", evidence="peer", reviewed_at=fr.MISS_RULE_SINCE - timedelta(days=1)))
    session.commit()

    text = fr.build_report(session, 1, _S, days=14)
    assert "旧口径回验" in text
    assert "1 条是 2026-11-28 前" in text


def test_report_separates_evidence_tiers(session):
    """报表必须**分行写三档**,不许只报合计。"""
    _alert(session, "hot_breakout", "breakout:起势:青蛇", hours_ago=100,
           reason="起势档/0人/知乎#9")
    session.add(DiscoveredPanLink(
        user_id=1, platform="tieba", origin_url="https://pan.baidu.com/s/z",
        title="漫剧合集 《青蛇传 - 前世今生》", status="ok",
        found_at=datetime.now() - timedelta(hours=5)))
    session.commit()
    fr.review_pending(session, 1, _S)

    text = fr.build_report(session, 1, _S, days=14)
    assert "证据档:同行 0 · 抖音 0 · 公开盘链 1" in text


def test_report_says_so_when_nothing_due(session):
    text = fr.build_report(session, 1, _S, days=14)
    assert "没有到期可回验" in text


def test_reason_tag_per_section():
    """三条链的 `reason` 形态**毫无共性**,各解各的 —— 不许一刀切。

    第一版按 `split("/")[-1]` 剥数字,`11 人/24h` 变成了 `h`,
    报表里印出「分源命中:h 2/2」。
    """
    #: hot_breakout 的源名在最后一段
    assert fr._reason_tag("hot_breakout", "起势档/0人/IT之家#15") == "IT之家"
    assert fr._reason_tag("hot_breakout", "预告档/0人/AI HOT#9") == "AI HOT"
    #: promo_forecast 的 reason 是推手计数,不是源 —— 不给标签
    assert fr._reason_tag("promo_forecast", "11 人/24h") == ""
    #: hot_forecast 是热榜名次
    assert fr._reason_tag("hot_forecast", "42名/4616 万/39x") == "42名"
