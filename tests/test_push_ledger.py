"""推送台账单测(2026-10-16):"客户昨天真的收到了什么" —— 日结/周结的唯一数据源。"""
import json
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import PushLedger, User
from app.services import push_ledger as pl


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _presence_item(**kw):
    """**小红书/快手/贴吧那条链**的 item 形状(它把链藏在 `link` 里)。"""
    base = {"name": "花少2人格测试", "platform": "xiaohongshu", "count": 3,
            "sample_urls": ["https://www.xiaohongshu.com/explore/abc"],
            "author": "某号", "metrics": {"liked_count": 12000},
            "link": {"my_link": "https://pan.quark.cn/s/OUR", "pan_url": ""}}
    base.update(kw)
    return base


class Test记账:
    def test_presence_形状要认得出来(self, session) -> None:
        """那条链的链在 `link.my_link` 里 —— 台账得认得,否则日结里资源全是"没链"。"""
        assert pl.record(session, 1, "xiaohongshu", [_presence_item()]) == 1
        row = session.query(PushLedger).one()
        assert row.title == "花少2人格测试" and row.item_key == "花少2人格测试"
        assert row.pan_url == "https://pan.quark.cn/s/OUR"
        assert row.pan_type == "夸克", "盘名要落下来(客户卡片上写的就是它)"
        assert row.author == "某号"
        #: ★★ 原帖链在 `sample_urls` 里(那条链**没有 `url` 这个键**)——
        #: 2026-10-16 拿真形状渲染预览时**当场抓到**不认它 ⇒ 日结卡上「对标作品」整列恒 `—`,
        #: 而数据一直就在。这条断言就是防它再退化。
        assert row.url == "https://www.xiaohongshu.com/explore/abc"

    def test_摊平在_item_上的互动数也要收进_metrics(self, session) -> None:
        """`douyin_leads` 那种把 `share_count` 摊平在 item 上的形状。"""
        pl.record(session, 1, "douyin_peer", [
            {"title": "某作品", "url": "https://www.douyin.com/video/1", "share_count": 88}])
        row = session.query(PushLedger).one()
        assert '"share_count": 88' in row.metrics

    def test_抖音那两路只有_desc_也要落成标题(self, session) -> None:
        """★ 2026-11-18:`discover_latest`(最新大瓜)与求1型视频的条目**只有 `desc`**。

        实测近 7 天 `section='douyin'` 的台账 30 行里 **24 行 title 为空**、
        `item_key` 全退化成 aweme_id ⇒ 日结/周结里那批资源没有名字可显示。
        """
        pl.record(session, 1, "douyin", [
            {"desc": "扣1看《反差直男》", "url": "https://www.douyin.com/video/1"}])
        row = session.query(PushLedger).one()
        assert row.title == "扣1看《反差直男》"
        assert row.item_key == "扣1看《反差直男》", "身份也跟着标题走,不再是裸 aweme_id"

    def test_title_优先于_desc(self, session) -> None:
        """`desc` 是"描述"不是全仓的标题主键 ⇒ 两者都在时必须用 `title`。"""
        pl.record(session, 1, "douyin", [{"title": "真标题", "desc": "描述"}])
        assert session.query(PushLedger).one().title == "真标题"

    def test_什么都没有的条目跳过(self, session) -> None:
        """记一行**什么都显示不出来**的东西,只会让日结里多一条 `—`。"""
        assert pl.record(session, 1, "wechat", [{}, {"title": "   "}, {"desc": "  "}]) == 0
        assert session.query(PushLedger).count() == 0

    def test_超长字段要截断_不许让_insert_抛(self, session) -> None:
        pl.record(session, 1, "wechat", [{"title": "长" * 900, "url": "u" * 900}])
        row = session.query(PushLedger).one()
        assert len(row.title) == 255 and len(row.url) == 500

    def test_写失败不许抛_也不许连坐调用方的事务(self, session, caplog) -> None:
        """★ 纪律 2 的正题:台账写失败**只撤销那几行**。

        整段 `rollback()` 会把**调用方尚未提交的推送数据**一起抹掉
        (本仓在告警冷却那里踩过同一个坑)。⇒ SAVEPOINT + 只记 warning,不抛。
        """
        session.add(models.RunRecord(user_id=1, run_id="r1", kind="k", status="success"))
        session.flush()                       # "已发出但未提交"的那类写
        with caplog.at_level("WARNING"):
            n = pl.record(_BoomProxy(session), 1, "wechat", [{"title": "x"}])
        session.commit()
        assert n == 0, "写失败要返回 0 —— 绝不抛给调用方"
        assert session.query(models.RunRecord).count() == 1, "台账失败把别人的写连坐了"
        assert "推送台账写入失败" in caplog.text, \
            "不许静默:失败必须留痕,否则「今天没内容」与「台账没写进去」同形"

    def test_一条坏不牵连整批(self, session) -> None:
        class _Bad(dict):
            def get(self, k, d=None):
                if k == "title":
                    raise RuntimeError("坏条目")
                return super().get(k, d)

        assert pl.record(session, 1, "wechat", [_Bad(), {"title": "好的"}]) == 1


class _BoomProxy:
    """把 add_all 打爆的假会话(其余属性转发给真 session,好让 savepoint 能用)。"""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def add_all(self, rows):
        raise RuntimeError("boom")


class Test读窗口:
    def _seed(self, session, times: list[datetime]) -> None:
        for t in times:
            pl.record(session, 1, "wechat", [{"title": "同一资源", "url": "u",
                                              "our_url": "https://pan.quark.cn/s/A"}], when=t)

    def test_同一资源推三次是一条_但要说出推了三次(self, session) -> None:
        """★ 客户抱怨的就是"同一条反复出现" ⇒ 日结必须先把它**并成一条**,
        同时**把次数说出来**(否则看的人以为只推过一次)。"""
        base = datetime(2026, 10, 16, 9, 0)
        self._seed(session, [base, base + timedelta(hours=4), base + timedelta(hours=8)])
        w = pl.window(session, 1, "wechat", datetime(2026, 10, 16), datetime(2026, 10, 17))
        assert w["rows"] == 3, "推送**行数**照数"
        assert w["resources"] == 1, "但**资源**只有一个"
        assert w["repeats"] == 1
        assert w["items"][0]["pushes"] == 3

    def test_窗口外的行不算进来(self, session) -> None:
        self._seed(session, [datetime(2026, 10, 15, 23, 59), datetime(2026, 10, 16, 0, 1)])
        w = pl.window(session, 1, "wechat", datetime(2026, 10, 16), datetime(2026, 10, 17))
        assert w["rows"] == 1 and w["items"][0]["pushes"] == 1

    def test_热度优先_推得多的不一定排前面(self, session) -> None:
        """★ 2026-10-26 用户口径:「每日总结里按热度排序,让用户更好的观看」。

        原来是 `pushes`(我们推了几次)主导 —— 那是**我们自己**的行为,
        不是"这件事有多火"。现在热度说了算,推得多只在**同热度**时做次序。
        """
        pl.record(session, 1, "wechat", [{"title": "没人推的", "metrics": {"read_num": 99999}}],
                  when=datetime(2026, 10, 16, 7))
        for h in (6, 8):
            pl.record(session, 1, "wechat", [{"title": "被推两次的"}],
                      when=datetime(2026, 10, 16, h))
        w = pl.window(session, 1, "wechat", datetime(2026, 10, 16), datetime(2026, 10, 17))
        assert [g["title"] for g in w["items"]] == ["没人推的", "被推两次的"], \
            "热的那条必须排前面,哪怕它只被推过一次"

    def test_有数据按热度排_没数据按原顺序沉底(self, session) -> None:
        """★ 2026-10-26 用户口径订正:**有数据的按热度排,没数据的按原顺序排**。

        没有互动数据的那种(贴吧/部分源)不该被 `pushes`/标题拼音编出一个**假次序** ——
        它们整体沉底,内部保持**首次出现**的顺序。
        """
        pl.record(session, 1, "wechat", [{"title": "冷的-先出现"}], when=datetime(2026, 10, 16, 7))
        pl.record(session, 1, "wechat", [{"title": "热的-小", "metrics": {"liked_count": 100}}],
                  when=datetime(2026, 10, 16, 8))
        pl.record(session, 1, "wechat", [{"title": "冷的-后出现", "metrics": {}}],
                  when=datetime(2026, 10, 16, 9))
        for h in (10, 11):
            pl.record(session, 1, "wechat", [{"title": "热的-大", "metrics": {"liked_count": 500}}],
                      when=datetime(2026, 10, 16, h))
        w = pl.window(session, 1, "wechat", datetime(2026, 10, 16), datetime(2026, 10, 17))
        assert [g["title"] for g in w["items"]] == [
            "热的-大", "热的-小", "冷的-先出现", "冷的-后出现"], \
            "有数据按热度降序在前;没数据的按首次出现顺序沉底"

    def test_都没数据时完全保持首次出现顺序(self, session) -> None:
        """两条都没指标 ⇒ **只看谁先出现**;被推三次也不许插到前面去。"""
        pl.record(session, 1, "wechat", [{"title": "先出现的"}], when=datetime(2026, 10, 16, 7))
        for h in (8, 9, 10):
            pl.record(session, 1, "wechat", [{"title": "后出现但推了三次"}],
                      when=datetime(2026, 10, 16, h))
        w = pl.window(session, 1, "wechat", datetime(2026, 10, 16), datetime(2026, 10, 17))
        assert [g["title"] for g in w["items"]] == ["先出现的", "后出现但推了三次"]

    def test_没有互动数据的不许插队到有数据的前面(self, session) -> None:
        """⚠️ 贴吧/部分源本来就没有点赞播放(heat=0)⇒ 它们会落到最后。
        卡片上必须写明"按热度排序、没有互动数据的排后面",否则会被读成"这些不重要"。"""
        pl.record(session, 1, "wechat", [{"title": "有数据的", "metrics": {"liked_count": 5}}],
                  when=datetime(2026, 10, 16, 7))
        for h in (8, 9, 10):
            pl.record(session, 1, "wechat", [{"title": "没数据但推了三次"}],
                      when=datetime(2026, 10, 16, h))
        w = pl.window(session, 1, "wechat", datetime(2026, 10, 16), datetime(2026, 10, 17))
        assert w["items"][0]["title"] == "有数据的"

    def test_后来的行只补空位不覆盖(self, session) -> None:
        """第一次那条通常就是"**第一次发现它**"时的原话 —— 后一轮缺链时不该把标题抹掉。"""
        pl.record(session, 1, "wechat", [{"title": "原标题", "url": "u1"}],
                  when=datetime(2026, 10, 16, 6))
        pl.record(session, 1, "wechat", [{"title": "原标题", "our_url": "https://pan.quark.cn/s/B"}],
                  when=datetime(2026, 10, 16, 9))
        w = pl.window(session, 1, "wechat", datetime(2026, 10, 16), datetime(2026, 10, 17))
        assert w["items"][0]["title"] == "原标题"
        assert w["items"][0]["pan_url"] == "https://pan.quark.cn/s/B", "空位要补上"
        assert w["items"][0]["url"] == "u1"

    def test_板块清单只数窗口内的(self, session) -> None:
        pl.record(session, 1, "wechat", [{"title": "a"}], when=datetime(2026, 10, 16, 9))
        pl.record(session, 1, "kuaishou", [{"title": "b"}], when=datetime(2026, 10, 14, 9))
        have = pl.boards_with_content(session, 1, datetime(2026, 10, 16), datetime(2026, 10, 17))
        assert have == {"wechat": 1}


def test_drop_recent_filters_same_section_within_window(session) -> None:
    """★ 2026-10-29 数据驱动:同一板块 24h 内推过的不再推(跨板块不去重)。"""
    from datetime import datetime, timedelta

    from app.db.models import PushLedger
    from app.services import push_ledger as pl

    now = datetime.now()
    session.add_all([
        PushLedger(user_id=1, section="bilibili", item_key="甲", title="甲",
                   pushed_at=now - timedelta(hours=2)),
        PushLedger(user_id=1, section="bilibili", item_key="乙", title="乙",
                   pushed_at=now - timedelta(hours=30)),
        PushLedger(user_id=1, section="kuaishou", item_key="丙", title="丙",
                   pushed_at=now - timedelta(hours=2)),
    ])
    session.commit()

    class _S:
        push_dedupe_hours = 24

    kept, dropped = pl.drop_recent(session, 1, "bilibili",
                                   [{"name": "甲"}, {"name": "乙"}, {"name": "丙"}], _S())
    assert [x["name"] for x in kept] == ["乙", "丙"]
    assert dropped == 1


def test_重复但流量好要放行(session) -> None:
    """★ 2026-11-05 用户:「如果是重复的资源但是流量好也需要推出来」。

    去重只看时间窗会误杀"同一资源突然爆了" —— 那恰恰是客户最想知道的。
    热度达到门槛就放行,并打 `_dedupe_hot` 标记(卡片上可标 🔥)。
    """
    from datetime import datetime, timedelta

    from app.db.models import PushLedger
    from app.services import push_ledger as pl

    now = datetime.now()
    session.add_all([
        PushLedger(user_id=1, section="douyin", item_key="热帖", title="热帖",
                   pushed_at=now - timedelta(hours=2)),
        PushLedger(user_id=1, section="douyin", item_key="冷帖", title="冷帖",
                   pushed_at=now - timedelta(hours=2)),
    ])
    session.commit()

    class _S:
        push_dedupe_hours = 24

    items = [{"key": "热帖", "share_count": 5000},
             {"key": "冷帖", "share_count": 10}]
    kept, dropped = pl.drop_recent(session, 1, "douyin", items, _S(),
                                   hot_of=lambda it: it.get("share_count"),
                                   hot_min=500)

    assert [it["key"] for it in kept] == ["热帖"]
    assert kept[0].get("_dedupe_hot") is True
    assert dropped == 1


def test_热度数字带万字的要能解析():
    from app.services.push_ledger import _num

    assert _num("1.2万") == 12000
    assert _num("3,000") == 3000
    assert _num(None) == 0


def test_pan_key_归一化同一份资源的写法差异():
    """★ 2026-11-17:同一条盘链在不同来源里的写法差异不该被当成两个资源。

    `http`/`https`、末尾斜杠、渠道参数(`entry`/`utm_*`)都是噪声;
    **提取码必须保留** —— 它会改变"能不能打开"。
    """
    from app.services.push_ledger import pan_key

    base = "pan.quark.cn/s/752504085d45"
    assert pan_key("https://pan.quark.cn/s/752504085d45") == base
    assert pan_key("http://pan.quark.cn/s/752504085d45/") == base
    assert pan_key("https://www.pan.quark.cn/s/752504085d45?entry=share") == base
    assert pan_key("https://pan.quark.cn/s/752504085d45?pwd=ab12") == base + "?pwd=ab12"
    #: 不同资源必须**认得出不一样**(否则去重会把真资源挡掉)
    assert pan_key("https://pan.quark.cn/s/aaaaaaaaaaaa") != base
    #: 认不出的给空串 ⇒ 调用方跳过盘链去重,而不是误判成"同一个"
    assert pan_key("") == ""
    assert pan_key("不是链接") == ""


def test_drop_recent_按盘链去重(session) -> None:
    """★★ 2026-11-17 用户口径「减少重复资源」。

    实测(近 7 天):带盘推送 1979 行只涉及 **378 个不同盘链**(81% 的行是重复),
    最极端的 5 个盘链被 **7 个板块各推一遍**。旧闸门只按 `item_key`(=内容身份)去重,
    同一份资源换个号/换篇文章就全放过。
    """
    from datetime import datetime, timedelta

    from app.db.models import PushLedger
    from app.services import push_ledger as pl

    pan = "https://pan.quark.cn/s/752504085d45"
    now = datetime.now()
    session.add(PushLedger(user_id=1, section="zhihu", item_key="旧帖", title="旧帖",
                           url="https://zhihu.com/q/1", pan_url=pan,
                           pushed_at=now - timedelta(hours=30)))
    session.commit()

    class _S:
        push_dedupe_hours = 24      #: 内容去重的窗口已过 ⇒ item_key 那道闸**不拦**
        push_pan_dedupe_hours = 72  #: 盘链那道闸还没过 ⇒ 必须拦

        push_hot_bypass_count = 3

    items = [
        #: 换了内容、换了写法(``http`` + 渠道参数),但**是同一份资源**
        {"key": "新帖", "url": "https://zhihu.com/q/2",
         "pan_url": "http://pan.quark.cn/s/752504085d45?entry=share"},
        #: 真·新资源 ⇒ 要留下
        {"key": "另一份", "url": "https://zhihu.com/q/3",
         "pan_url": "https://pan.quark.cn/s/000000000000"},
        #: 没有盘链的条目只走内容去重 ⇒ 留下
        {"key": "无盘", "url": "https://zhihu.com/q/4"},
    ]
    kept, dropped = pl.drop_recent(session, 1, "zhihu", items, _S())
    assert [it["key"] for it in kept] == ["另一份", "无盘"]
    assert dropped == 1


def test_drop_recent_同一批里两条同盘链只留一条(session) -> None:
    """同一批里两条指向同一份资源,后一条也不该推 —— 这是跨板块刷屏的最小形态。"""
    from app.services import push_ledger as pl

    class _S:
        push_dedupe_hours = 24
        push_pan_dedupe_hours = 72

    pan = "https://pan.xunlei.com/s/VP5RHJ4osQZQn3E5RWTCJMFFA"
    items = [{"key": "甲", "pan_url": pan},
             #: 同一份资源的另一种写法(渠道参数是噪声,不是提取码)
             {"key": "乙", "pan_url": pan + "/?entry=share"},
             {"key": "丙"}]
    kept, dropped = pl.drop_recent(session=None, user_id=1, section="tieba", items=items,
                                   settings=_S())
    #: session=None ⇒ 连库都不查,但**同批内的盘链去重仍然有效**(它不依赖库)
    assert [it["key"] for it in kept] == ["甲", "丙"]
    assert dropped == 1


def test_盘链重复但热度爆了仍放行(session) -> None:
    """与内容去重同一条口径(用户 2026-11-05):流量真的爆了的,重复也要推。"""
    from datetime import datetime, timedelta

    from app.db.models import PushLedger
    from app.services import push_ledger as pl

    pan = "https://pan.quark.cn/s/752504085d45"
    session.add(PushLedger(user_id=1, section="bilibili", item_key="旧",
                           pan_url=pan, pushed_at=datetime.now() - timedelta(hours=2)))
    session.commit()

    class _S:
        push_dedupe_hours = 24
        push_pan_dedupe_hours = 72

    items = [{"key": "新", "pan_url": pan, "count": 9}]
    kept, dropped = pl.drop_recent(session, 1, "bilibili", items, _S(),
                                   hot_of=lambda it: it.get("count"), hot_min=3)
    assert [it["key"] for it in kept] == ["新"]
    assert kept[0]["_dedupe_hot"] is True
    assert dropped == 0


def test_热度只是绝对值高不算又火了(session) -> None:
    """★★ 2026-11-18(档2 前置):`hot_of` 判**绝对值**时,presence 那条链的豁免
    **永远成立** —— 实测同一份资源的"同资源内容数"是 20~99,而门槛默认 **3**。

    后果(近 30 天实测):知乎把同一份资源推了 **162 次**、B站 61 次、贴吧 25 次;
    贴吧 5 天里同一条链推了 **14 次**,`item_key` 与 `pan_url` 完全相同。

    ⇒ 正确语义是「**比上次推它时更火了**」。`hot_rise_only=True` 只认涨:
    绝对值高但没涨 ⇒ 仍按重复处理。
    """
    from datetime import datetime, timedelta

    from app.db.models import PushLedger
    from app.services import push_ledger as pl

    pan = "https://pan.quark.cn/s/c2c8b6d058c3"
    session.add(PushLedger(user_id=1, section="tieba", item_key="犬系人格测试链接",
                           pan_url=pan, metrics='{"play_count": 3256}',
                           pushed_at=datetime.now() - timedelta(hours=3)))
    session.commit()

    class _S:
        push_dedupe_hours = 24
        push_pan_dedupe_hours = 72

    #: ⚠️ `hot_of` 吃的是**整个 item**(与 presence 那边的调用形状一致),不是 metrics 字典
    _hot = lambda it: pl.peak_metric(it.get("metrics"))  # noqa: E731

    #: 同一份资源、同一个 key —— 热度**原地不动**(还是 3256)⇒ 不许再推
    flat = [{"key": "犬系人格测试链接", "pan_url": pan,
             "metrics": {"play_count": 3256}}]
    kept, dropped = pl.drop_recent(session, 1, "tieba", flat, _S(),
                                   hot_of=_hot, hot_min=1, hot_rise_only=True)
    assert kept == [] and dropped == 1, "热度没涨就不该再推一遍"

    #: 真的涨了(3256 → 9000)⇒ 放行并打 🔥 标记
    risen = [{"key": "犬系人格测试链接", "pan_url": pan,
              "metrics": {"play_count": 9000}}]
    kept2, dropped2 = pl.drop_recent(session, 1, "tieba", risen, _S(),
                                     hot_of=_hot, hot_min=1, hot_rise_only=True)
    assert len(kept2) == 1 and dropped2 == 0
    assert kept2[0]["_dedupe_hot"] is True


def test_group_of_只认同一个专属群_回落主群的不算() -> None:
    """★★ 2026-11-18:`tieba`/`zhihu` 配的是**同一个专属群** ⇒ 它们要共用一道闸。

    ⚠️ 判据必须用 `platform_webhook`(**专属**群)而不是 `webhook_for`(会回落主群)——
    否则所有**没配专属群**的板块都会被判成"同群",按群去重会把它们互相挡死。
    """
    from app.services.push_ledger import group_of

    class _S:
        feishu_webhook = "https://hook/main"
        feishu_webhook_tieba = "https://hook/shared"
        feishu_webhook_zhihu = "https://hook/shared"
        feishu_webhook_bilibili = ""          #: 没配专属群 ⇒ 回落主群
        feishu_webhook_kuaishou = ""          #: 同上

    assert group_of(_S(), "tieba") == ("tieba", "zhihu")
    assert group_of(_S(), "zhihu") == ("tieba", "zhihu")
    #: 没配专属群的两个板块都回落主群,但**不算同群**(否则它们会互相挡内容)
    assert group_of(_S(), "bilibili") == ("bilibili",)
    assert group_of(_S(), "kuaishou") == ("kuaishou",)
    assert group_of(_S(), "") == ()


def test_by_group_同群的两个板块共用盘链闸(session) -> None:
    """★ 贴吧推过的盘,知乎在**同一个群里**不许再推一遍。

    实测近 7 天:10 个盘链在同一群里被两板块各推、**166 行**;
    而 `item_key` 跨板块重复 **0 条** ⇒ 内容级去重抓不到,只有盘链闸能抓。
    """
    from datetime import datetime, timedelta

    from app.db.models import PushLedger
    from app.services import push_ledger as pl

    class _S:
        push_dedupe_hours = 24
        push_pan_dedupe_hours = 72
        feishu_webhook = "https://hook/main"
        feishu_webhook_tieba = "https://hook/shared"
        feishu_webhook_zhihu = "https://hook/shared"
        feishu_webhook_bilibili = ""

    pan = "https://pan.quark.cn/s/aabbccdd1122"
    session.add(PushLedger(user_id=1, section="tieba", item_key="贴吧那条",
                           pan_url=pan, pushed_at=datetime.now() - timedelta(hours=3)))
    session.commit()

    #: 知乎那条:内容完全不同(new item_key)、盘链还是那一份
    items = [{"key": "知乎这条", "pan_url": pan}]
    kept, dropped = pl.drop_recent(session, 1, "zhihu", items, _S(), by_group=True)
    assert kept == [] and dropped == 1, "同一个群里,贴吧推过的盘链知乎不许再推"

    #: 对照:**不**开 by_group 时(旧行为),它照推 —— 证明这条闸确实是 by_group 起的作用
    kept2, _ = pl.drop_recent(session, 1, "zhihu", [{"key": "知乎这条", "pan_url": pan}],
                              _S())
    assert len(kept2) == 1

    #: 对照:没配专属群的 bilibili **不能被牵连**(它和谁都不是同群)
    kept3, _ = pl.drop_recent(session, 1, "bilibili", [{"key": "B站这条", "pan_url": pan}],
                              _S(), by_group=True)
    assert len(kept3) == 1, "不同群之间跨板块去重会把别的群饿死"


def test_peak_metric_混着字符串也能比() -> None:
    """各源指标格式不一(`"1.2万"` 是字符串)—— 直接 `max()` 会在 py3 里当场 TypeError。"""
    from app.services.push_ledger import peak_metric

    assert peak_metric({"liked_count": 53, "play_count": "1.2万"}) == 12000
    assert peak_metric({"share_count": "3,000"}) == 3000
    assert peak_metric({}) == 0
    assert peak_metric(None) == 0


class TestHotRiseFloor:
    """★★ 2026-11-19:**「涨了」还不够,涨多少要有下限**。

    7 天台账逐条重放(按 `(section, item_key)` 比前后两次):
      · bilibili **99 次**"涨了"的重复推送涨幅**全在 10% 以内** ——
        `pan.quark.cn/s/8fa4f71f7430` 一天推 4 次,播放
        `99893 → 100194 → 100382 → 100957`(+0.3% / +0.2% / +0.6%);
      · 贴吧 32 次"涨了"里**一次也没超 50%**;
      · 真爆的是另一个量级:同窗口有 `17160 → 226451 → 1001939`(涨 13 倍)。

    ⇒ `hot_rise_pct`(默认读 `push_hot_rise_pct`=0.30)必须被越过才放行。
    """

    def _seed(self, session, pan: str, metrics: dict, hours: float = 2) -> None:
        session.add(PushLedger(user_id=1, section="bilibili", item_key="旧",
                               pan_url=pan, metrics=json.dumps(metrics),
                               pushed_at=datetime.now() - timedelta(hours=hours)))
        session.commit()

    def test_涨一点点不算又火了(self, session) -> None:
        pan = "https://pan.quark.cn/s/8fa4f71f7430"
        self._seed(session, pan, {"play_count": 99893})

        class _S:
            push_dedupe_hours = 24
            push_pan_dedupe_hours = 72

        #: +0.3%:平台自然波动,不是"资源火了" ⇒ 仍是同一条
        items = [{"key": "新", "pan_url": pan, "metrics": {"play_count": 100194}}]
        #: ⚠️ `hot_of` 吃的是**整个 item**,不是 metrics 字典 —— 直接塞
        #: `peak_metric` 会让它拿到 `{"metrics": {...}}` 外层、恒返回 0,
        #: 判定退化成"没有基准 ⇒ 一律挡",测试**看着绿、其实没测到涨幅**。
        kept, dropped = pl.drop_recent(
            session, 1, "bilibili", items, _S(),
            hot_of=lambda it: pl.peak_metric(it.get("metrics")),
            hot_min=1, hot_rise_only=True)
        assert kept == [] and dropped == 1

    def test_真爆量的仍放行(self, session) -> None:
        pan = "https://pan.quark.cn/s/9b1c2d3e4f5a"
        self._seed(session, pan, {"play_count": 17160})

        class _S:
            push_dedupe_hours = 24
            push_pan_dedupe_hours = 72

        items = [{"key": "新", "pan_url": pan, "metrics": {"play_count": 226451}}]
        kept, dropped = pl.drop_recent(
            session, 1, "bilibili", items, _S(),
            hot_of=lambda it: pl.peak_metric(it.get("metrics")),
            hot_min=1, hot_rise_only=True)
        assert len(kept) == 1 and dropped == 0
        assert kept[0]["_dedupe_hot"] is True, "放行的重复要打 🔥 标记"

    def test_显式传0退回只看有没有涨(self, session) -> None:
        """逃生门:调用方明确要放宽时传 `hot_rise_pct=0`,行为与 2026-11-18 一致。

        ⚠️ 判据必须是 `is None` 而不是 `or 0.30` —— 后者会把显式传的 `0` 也吞成 30%,
        逃生门**看着有、其实永远打不开**(本仓「看着有其实没有」那一族)。
        """
        pan = "https://pan.quark.cn/s/0a1b2c3d4e5f"
        self._seed(session, pan, {"play_count": 1000})

        class _S:
            push_dedupe_hours = 24
            push_pan_dedupe_hours = 72

        #: 同一内容、同一盘链、涨 0.1% —— 默认口径挡,`hot_rise_pct=0` 时放行
        #: ⚠️ 两个下限是**独立开关**(2026-11-25 加绝对增量后):只关比例的话,
        #: `bilibili` 的绝对下限(1 万)仍在生效,+1 的涨幅照样被挡 —— 要完全放开
        #: 必须两个都传 0。这条测试钉的就是"两个都传才真的退回旧行为"。
        items = [{"key": "旧", "pan_url": pan, "metrics": {"play_count": 1001}}]
        kept, _ = pl.drop_recent(
            session, 1, "bilibili", items, _S(),
            hot_of=lambda it: pl.peak_metric(it.get("metrics")),
            hot_min=1, hot_rise_only=True, hot_rise_pct=0, hot_rise_min_abs=0)
        assert len(kept) == 1

    def test_上次没有热度数据时按挡处理(self, session) -> None:
        """⚠️ 旧写法把缺失基准当 0 ⇒ 任何正数都放行 —— 那正是本参数要堵的口子。"""
        pan = "https://pan.quark.cn/s/ffeeddccbbaa"
        self._seed(session, pan, {})          # 上次推的时候没有互动数据

        class _S:
            push_dedupe_hours = 24
            push_pan_dedupe_hours = 72

        items = [{"key": "新", "pan_url": pan, "metrics": {"play_count": 500}}]
        kept, dropped = pl.drop_recent(
            session, 1, "bilibili", items, _S(),
            hot_of=lambda it: pl.peak_metric(it.get("metrics")),
            hot_min=1, hot_rise_only=True)
        assert kept == [] and dropped == 1


class TestHotRiseMinAbs:
    """★★ 2026-11-25:**比例尺在小量级板块上是错的,要卡绝对增量**。

    30 天台账 8553 行按生产口径重放(盘链身份优先、`item_key` 兜底),把「过 30%」
    的那批再按绝对增量分档 —— 各板块热度口径量级差 4 个数量级:

      · `tieba` 回复数中位 **2** ⇒ 过 30% 的 38 条里 **34 条增量 <20**(`2→16`);
      · `wechat` 阅读数 ⇒ 过 30% 的 428 条里 **35.6% 增量 ≤5**(`1→2`);
      · `weibo_peer` 噪音(涨幅 <5%)的增量最大只有 **16**;
      · 真爆:`bilibili 218676→2947509`、`xiaohongshu 106→17000`。

    门槛是**在候选池(已过 30%)内部按分位数挑的**,不是"天然断点"(只有 `tieba`
    在 +17 与 +72 之间是空的)。每个值"为什么不是相邻那一档"写在
    `HOT_RISE_MIN_ABS` 的行内注释里。

    ⚠️ **不能只抬比例**:`277239→497704` 只涨 80%、增量 22 万,是 bilibili 排前几
    的真爆 —— 抬比例会把它和 `2→3` 一起挡掉。所以卡的是**增量**,不是百分比。
    """

    def _seed(self, session, section: str, pan: str, metrics: dict) -> None:
        session.add(PushLedger(user_id=1, section=section, item_key="旧",
                               pan_url=pan, metrics=json.dumps(metrics),
                               pushed_at=datetime.now() - timedelta(hours=2)))
        session.commit()

    def test_个位数基数涨_50_不算又火了(self, session) -> None:
        """`tieba` 6→23:涨幅 283% 远过 30%,但增量只有 17 < 门槛 20 ⇒ 挡。

        ⚠️ 17 是候选池里**真实被切掉的最大一条**,而空档之上最小的是 +72 ——
        门槛取 20 与取 50 切掉的行数完全相同(`tieba` 的候选池在那段是空的)。
        """
        pan = "https://pan.quark.cn/s/tieba0001"
        self._seed(session, "tieba", pan, {"total_replay_num": 6})

        class _S:
            push_dedupe_hours = 24
            push_pan_dedupe_hours = 72

        items = [{"key": "新", "pan_url": pan, "metrics": {"total_replay_num": 23}}]
        kept, dropped = pl.drop_recent(
            session, 1, "tieba", items, _S(),
            hot_of=lambda it: pl.peak_metric(it.get("metrics")),
            hot_min=1, hot_rise_only=True)
        assert kept == [] and dropped == 1, \
            "贴吧涨 17 个回复是自然波动,不是'资源火了'(比例 283% 会误放行)"

    def test_同一份增量在大板块算真爆(self, session) -> None:
        """**同一个 `6→23` 放 bilibili 也挡** —— 门槛是按板块的,不是按增量绝对值一刀切。

        `bilibili` 的播放量中位 1.8 万,涨 17 次播放连噪音都算不上。
        """
        pan = "https://pan.quark.cn/s/bili00001"
        self._seed(session, "bilibili", pan, {"play_count": 6})

        class _S:
            push_dedupe_hours = 24
            push_pan_dedupe_hours = 72

        items = [{"key": "新", "pan_url": pan, "metrics": {"play_count": 23}}]
        kept, _ = pl.drop_recent(
            session, 1, "bilibili", items, _S(),
            hot_of=lambda it: pl.peak_metric(it.get("metrics")),
            hot_min=1, hot_rise_only=True)
        assert kept == []

    def test_只涨_80_但增量巨大_仍是真爆(self, session) -> None:
        """★ 这条是**为什么不用比例**的核心证据:`277239→497704`(涨 80%、增量 22 万)。

        把 `push_hot_rise_pct` 抬到 1.0 会把它挡掉 —— 而它是 bilibili 排前几的真爆。
        按绝对增量判:22 万 ≥ 1 万 ⇒ 放行。
        """
        pan = "https://pan.quark.cn/s/bili00002"
        self._seed(session, "bilibili", pan, {"play_count": 277239})

        class _S:
            push_dedupe_hours = 24
            push_pan_dedupe_hours = 72

        items = [{"key": "新", "pan_url": pan, "metrics": {"play_count": 497704}}]
        kept, _ = pl.drop_recent(
            session, 1, "bilibili", items, _S(),
            hot_of=lambda it: pl.peak_metric(it.get("metrics")),
            hot_min=1, hot_rise_only=True)
        assert len(kept) == 1 and kept[0]["_dedupe_hot"] is True

    def test_表里没登记的板块不启用这一档(self, session) -> None:
        """`zhihu` 不在 `HOT_RISE_MIN_ABS` 里 ⇒ 门槛 0 ⇒ 只按比例判。

        ⚠️ 这是**故意的**:知乎的热度是「同资源内容数」,实测过 30% 的 15 条
        **全部增量 <20** —— 再加绝对门槛会一条不剩,那是把整条链关死,不是提纯。
        """
        assert "zhihu" not in pl.HOT_RISE_MIN_ABS
        pan = "https://pan.quark.cn/s/zhihu0001"
        self._seed(session, "zhihu", pan, {"comment_count": 1})

        class _S:
            push_dedupe_hours = 24
            push_pan_dedupe_hours = 72

        #: 1 → 3:涨幅 200%、增量 2;若这条链被误配了门槛就会挡掉
        items = [{"key": "新", "pan_url": pan, "metrics": {"comment_count": 3}}]
        kept, _ = pl.drop_recent(
            session, 1, "zhihu", items, _S(),
            hot_of=lambda it: pl.peak_metric(it.get("metrics")),
            hot_min=1, hot_rise_only=True)
        assert len(kept) == 1, "知乎没有绝对门槛,涨了就该放行"

    def test_配置可以按板块覆盖标定值(self, session) -> None:
        """`push_hot_rise_min_abs` = `板块:数值,...` —— 不改代码就能调。"""
        pan = "https://pan.quark.cn/s/tieba0002"
        self._seed(session, "tieba", pan, {"total_replay_num": 100})

        class _S:
            push_dedupe_hours = 24
            push_pan_dedupe_hours = 72
            push_hot_rise_min_abs = "tieba:5"     #: 标定值是 20,这里调到 5

        #: 100→110:涨幅 10%(过 30% 那档靠 `hot_rise_pct=0` 放),增量 10 ——
        #: 按标定值 20 挡,覆盖成 5 放行
        items = [{"key": "新", "pan_url": pan, "metrics": {"total_replay_num": 110}}]
        kept, _ = pl.drop_recent(
            session, 1, "tieba", items, _S(),
            hot_of=lambda it: pl.peak_metric(it.get("metrics")),
            hot_min=1, hot_rise_only=True, hot_rise_pct=0)
        assert len(kept) == 1

    def test_覆盖率解析失败不抛_退回标定值(self, session) -> None:
        """一个写错的配置不该让整条推送链挂掉(但要 warning,别静默)。"""
        class _S:
            push_hot_rise_min_abs = "tieba:abc,bilibili:100"

        assert pl._rise_min_abs_of(_S(), "tieba") == 20.0, "解析不出数字 ⇒ 退回标定值"
        assert pl._rise_min_abs_of(_S(), "bilibili") == 100.0, "后面那条不受影响"
        assert pl._rise_min_abs_of(_S(), "wechat") == 50.0

    def test_两个下限是两个独立开关(self, session) -> None:
        """⚠️ 只传 `hot_rise_pct=0` **不等于**回到旧行为(见 `drop_recent` 头注)。"""
        pan = "https://pan.quark.cn/s/tieba0003"
        self._seed(session, "tieba", pan, {"total_replay_num": 100})

        class _S:
            push_dedupe_hours = 24
            push_pan_dedupe_hours = 72

        #: 100→104:涨幅 4% —— 比例关了之后**只有**增量门槛能挡它
        items = [{"key": "新", "pan_url": pan, "metrics": {"total_replay_num": 104}}]
        #: 比例关了(涨幅无所谓),但绝对下限 20 仍在 ⇒ 挡
        kept, _ = pl.drop_recent(
            session, 1, "tieba", items, _S(),
            hot_of=lambda it: pl.peak_metric(it.get("metrics")),
            hot_min=1, hot_rise_only=True, hot_rise_pct=0)
        assert kept == [], "只关比例、没关绝对增量 ⇒ 仍然挡"

        #: 两个都关 ⇒ 才真的退回"涨了就算"
        kept, _ = pl.drop_recent(
            session, 1, "tieba", items, _S(),
            hot_of=lambda it: pl.peak_metric(it.get("metrics")),
            hot_min=1, hot_rise_only=True, hot_rise_pct=0, hot_rise_min_abs=0)
        assert len(kept) == 1


def test_盘链闸的热度基准按盘链取(session) -> None:
    """★★ 2026-11-19:同一份资源**换了篇文章**(item_key 不同)时,基准也得取到。

    公众号那条链上同一份资源被不同号各发一遍 ⇒ `item_key` 每次都是新的 `art{id}`,
    按内容取基准永远查不到"上次同一份资源的热度" ⇒ 盘链闸一挡就退到"没法证明涨了"。
    盘链闸判的是「同一份资源」,基准就必须按 `pan_key` 取(见 `last_pan_hot`)。
    """
    pan = "https://pan.quark.cn/s/7ea21dde7fbb"
    session.add(PushLedger(user_id=1, section="wechat", item_key="art9001",
                           pan_url=pan, metrics='{"read_num": 5000}',
                           pushed_at=datetime.now() - timedelta(hours=3)))
    session.commit()

    class _S:
        push_dedupe_hours = 24
        push_pan_dedupe_hours = 72

    _hot = lambda it: float((it.get("metrics") or {}).get("read_num") or 0)  # noqa: E731

    #: 另一篇文章、同一份资源,阅读 5000 → 20000(涨 4 倍)⇒ 该放行
    risen = [{"item_key": "art9002", "pan_url": pan,
              "metrics": {"read_num": 20000}}]
    kept, dropped = pl.drop_recent(
        session, 1, "wechat", risen, _S(),
        hot_of=_hot, hot_min=1, hot_rise_only=True)
    assert len(kept) == 1 and dropped == 0, "换篇文章不影响'同一份资源'的热度基准"

    #: 同一份资源、阅读 **5000 → 5200**(涨 4%)⇒ 挡
    flat = [{"item_key": "art9003", "pan_url": pan,
             "metrics": {"read_num": 5200}}]
    kept2, dropped2 = pl.drop_recent(
        session, 1, "wechat", flat, _S(),
        hot_of=lambda it: float((it.get("metrics") or {}).get("read_num") or 0),
        hot_min=1, hot_rise_only=True)
    assert kept2 == [] and dropped2 == 1


class Test判重身份:
    """★★ 2026-11-19:**我方链不是稳定的资源身份**,判重不能只看它。

    实测(近 3 天 2011 行带盘台账):310 条我方链里 **67 条(21.6%)挂着 >1 个不同源链**。
    最典型 `pan.quark.cn/s/7728b469a574` —— 资源库里登记的是「儿童涂色填色模板大全」,
    台账上却挂着**公考行测答题卡 / 作文稿纸 / 2027 农历日历**三份毫不相干的东西
    (`_enrich` 的「资源级复用」按标题近似匹配,把不同资源错配到了同一条链)。
    拿我方链判重,72h 会**挡掉 282 行真实的不同资源** —— 客户口径是
    「**带网盘链接 + 最新**」,**丢资源比重复推一遍严重得多**。

    ⇒ 判重身份(`pan_id`)取**源链优先**;我方链那一档必须**再过标题判据**才挡。
    """

    class _S:
        push_dedupe_hours = 24
        push_pan_dedupe_hours = 72

    def test_同一条我方链但标题是两份资源_两篇都推(self, session) -> None:
        """★ 这条就是那 282 行的最小复现。

        书法字帖那条链 `424edbd0f970` 下,「小学语文二年级上册」和「小学必备古诗词」
        **是两份不同的资源**(各有各的源链) —— 60 多个号在发,挡掉就是真丢资源。
        """
        #: ⚠️ 公众号那条链**一定会带 `src_pan_url` 键**(它手上同时有两条链)——
        #: 少了它,这条用例就落到"没区分两条链的板块"那一档,测的是别的判据。
        base = {"pan_url": "https://pan.quark.cn/s/OUR_SHARED",
                "metrics": {"read_num": 0}}
        first = dict(base, item_key="art1",
                     src_pan_url="https://pan.quark.cn/s/SRC_A",
                     title="书法字帖｜小学语文二年级上册硬笔综合训练")
        assert pl.record(session, 1, "wechat", [first]) == 1
        session.commit()

        second = dict(base, item_key="art2",
                      src_pan_url="https://pan.quark.cn/s/SRC_B",
                      title="小学必备古诗词硬笔字帖——书法字帖分享")
        kept, dropped = pl.drop_recent(session, 1, "wechat", [second], self._S())
        assert len(kept) == 1 and dropped == 0, \
            "同一条我方链下挂着**两份不同的资源**(链被错配了)⇒ 不许互相挡"

    def test_同一条我方链且标题确是同一份_仍然挡(self, session) -> None:
        """副本要压住:5 个号各转存一份同一 PDF 时,源链各不相同、我方链会收敛。

        此时**标题是同资源的另一种写法** ⇒ 该挡。
        """
        assert pl.record(session, 1, "wechat", [
            {"item_key": "art1", "pan_url": "https://pan.quark.cn/s/OUR_SHARED",
             "src_pan_url": "https://pan.quark.cn/s/SRC_ONE",
             "title": "危险人格测试入口链接（自取）"}]) == 1
        session.commit()

        kept, dropped = pl.drop_recent(session, 1, "wechat", [
            {"item_key": "art2", "pan_url": "https://pan.quark.cn/s/OUR_SHARED",
             "src_pan_url": "https://pan.quark.cn/s/SRC_TWO",
             "title": "危险人格测试 最新入口【链接自取】"}], self._S())
        assert kept == [] and dropped == 1

    def test_源链相同就挡_哪怕我方链不同(self, session) -> None:
        """反过来那档:**同一份资源**被不同号各转存一份(我方链不同),源链是同一条。

        这条判据**零误伤**(同一源链就是同一份资源),命中直接挡。
        """
        assert pl.record(session, 1, "wechat", [
            {"item_key": "art1", "pan_url": "https://pan.quark.cn/s/OUR_A",
             "src_pan_url": "https://pan.quark.cn/s/SRC_SAME",
             "title": "《高性价比人生指南》PDF电子版（免费保存）"}]) == 1
        session.commit()

        #: 标题故意写成**看不出是同一份**的串 —— 只要源链一样就必须挡
        kept, dropped = pl.drop_recent(session, 1, "wechat", [
            {"item_key": "art2", "pan_url": "https://pan.quark.cn/s/OUR_B",
             "src_pan_url": "https://pan.quark.cn/s/SRC_SAME",
             "title": "高性价比人生指南 共338页 pdf/epub电子版【可保存】"}], self._S())
        assert kept == [] and dropped == 1

    def test_源链不同就放行_哪怕标题撞车(self, session) -> None:
        """标题相似但源链不同 ⇒ 是两份资源(引流号常把标题写成同一个套路)⇒ 放行。"""
        assert pl.record(session, 1, "wechat", [
            {"item_key": "art1", "pan_url": "https://pan.quark.cn/s/OUR_A",
             "src_pan_url": "https://pan.quark.cn/s/SRC_ONE",
             "title": "冬天到了，我想问你 19 个问题 pdf完整版（免费获取）"}]) == 1
        session.commit()

        kept, dropped = pl.drop_recent(session, 1, "wechat", [
            {"item_key": "art2", "pan_url": "https://pan.quark.cn/s/OUR_B",
             "src_pan_url": "https://pan.quark.cn/s/SRC_TWO",
             "title": "《冬天到了，我想问你19个问题》pdf完整版"}], self._S())
        assert len(kept) == 1 and dropped == 0, \
            "源链不同 = 两份资源(各号各传一份),不该因为标题像就挡"

    def test_非公众号链没有源链字段_仍按pan_url判重(self, session) -> None:
        """抖音对标号/资源库那条链给的 `pan_url` **本来就是别人的源链**,不该被削弱。

        ⚠️ 回归:我一度无条件加标题判据,结果抖音"同一份资源换作品 id"立刻不再被挡。
        """
        assert pl.record(session, 1, "douyin_peer", [
            {"item_key": "w1", "pan_url": "https://pan.xunlei.com/s/VP6XqobV5vdt",
             "title": "第一条"}]) == 1
        session.commit()

        kept, dropped = pl.drop_recent(session, 1, "douyin_peer", [
            {"item_key": "w2", "pan_url": "https://pan.xunlei.com/s/VP6XqobV5vdt",
             "title": "完全不同的标题"}], self._S())
        assert kept == [] and dropped == 1, \
            "没有 src_pan_url 这条链的 pan_url 就是资源身份,标题不同也照挡"

    def test_台账把判重身份单独落一列(self, session) -> None:
        """`pan_url` 必须留我方链(日结的「资源」列要客户能点),身份另存 `pan_id`。"""
        assert pl.record(session, 1, "wechat", [
            {"item_key": "art1", "pan_url": "https://pan.quark.cn/s/OUR_A",
             "src_pan_url": "https://pan.quark.cn/s/SRC_SAME?pwd=abcd",
             "title": "某资源"}]) == 1
        row = session.query(PushLedger).one()
        assert row.pan_url == "https://pan.quark.cn/s/OUR_A", "展示链不许被源链顶掉"
        assert row.pan_id == "pan.quark.cn/s/SRC_SAME?pwd=abcd", "判重身份取归一后的源链"
