"""推送台账单测(2026-10-16):"客户昨天真的收到了什么" —— 日结/周结的唯一数据源。"""
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

    def test_什么都没有的条目跳过(self, session) -> None:
        """记一行**什么都显示不出来**的东西,只会让日结里多一条 `—`。"""
        assert pl.record(session, 1, "wechat", [{}, {"title": "   "}]) == 0
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

    def test_被反复推的排最前(self, session) -> None:
        pl.record(session, 1, "wechat", [{"title": "没人推的", "metrics": {"read_num": 99999}}],
                  when=datetime(2026, 10, 16, 7))
        for h in (6, 8):
            pl.record(session, 1, "wechat", [{"title": "被推两次的"}],
                      when=datetime(2026, 10, 16, h))
        w = pl.window(session, 1, "wechat", datetime(2026, 10, 16), datetime(2026, 10, 17))
        assert w["items"][0]["title"] == "被推两次的"

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
