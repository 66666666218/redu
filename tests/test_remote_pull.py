"""远程 → 本机 拉取(2026-11-18,档4)的守卫。

守的是四件**容易退化**的事:

  ① **不拉原始热榜表**:`weibo/baidu/douhot` 在本机有别的消费者(`focus_alert` 等),
     而两端的飞书 webhook 指纹**完全相同** ⇒ 拉回来就会**重复告警**,而且没人看得出来。
  ② **不用远程 id 对行**:两边各自自增过,拿远程 id 落到本机会指错行
     (与 `remote_sync` 的 `_ARTICLE_COLS` 同一条纪律)。
  ③ **幂等**:同一批拉两次,事件不重、成员不重、热榜不重复插。
  ④ **`sample_count` 不能相加**:它是"快照条数",相加会在每轮拉取时翻倍。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import EventMembership, HotSourceItem, HotspotEvent, User
from app.services import remote_pull as rp


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _ev(remote_id=101, norm="某明星官宣结婚", title="某明星官宣结婚", plats=("weibo",),
        pc=1, mins_ago=5, **kw):
    now = datetime.now()
    base = dict(remote_id=remote_id, primary_title=title, norm_title=norm,
                platforms='["' + '","'.join(plats) + '"]' if plats else "",
                platform_count=pc, first_seen=now - timedelta(hours=2),
                last_seen=now - timedelta(minutes=mins_ago), peak_value=1000.0,
                peak_at=now - timedelta(minutes=mins_ago), sample_count=3,
                status="active", ended_at=None, reappear_count=0, last_growth=None)
    base.update(kw)
    return base


def _mb(remote_event_id=101, board="weibo", norm="某明星官宣结婚", value=1000.0, mins_ago=5):
    return dict(remote_event_id=remote_event_id, board=board, norm_title=norm,
                latest_value=value, last_seen=datetime.now() - timedelta(minutes=mins_ago))


class TestPullEvents:
    def test_新建与成员映射(self, session) -> None:
        out = rp.pull_events(session, settings=None,
                             rows=([_ev()], [_mb(board="weibo")]))
        assert out["created"] == 1 and out["members"] == 1
        ev = session.scalar(select(HotspotEvent))
        assert ev.norm_title == "某明星官宣结婚" and ev.user_id == rp.REMOTE_USER_ID
        m = session.scalar(select(EventMembership))
        assert m.event_id == ev.id, "成员必须挂到本地 id 上,不是远程 event_id"

    def test_重跑幂等_事件不重_成员不重(self, session) -> None:
        rows = ([_ev()], [_mb()])
        rp.pull_events(session, settings=None, rows=rows)
        out = rp.pull_events(session, settings=None, rows=rows)
        assert out["created"] == 0 and out["members"] == 0
        assert len(session.scalars(select(HotspotEvent)).all()) == 1
        assert len(session.scalars(select(EventMembership)).all()) == 1

    def test_sample_count不相加(self, session) -> None:
        """★ 拉两次不能让 `sample_count` 翻倍 —— 它记的是**快照条数**,不是拉取次数。"""
        rows = ([_ev(sample_count=7)], [_mb()])
        rp.pull_events(session, settings=None, rows=rows)
        rp.pull_events(session, settings=None, rows=rows)
        ev = session.scalar(select(HotspotEvent))
        assert ev.sample_count == 7

    def test_本地更新的行不被远程旧数据冲掉(self, session) -> None:
        """★ 取"更超前的那个",不做覆盖:本机若已更新(手动归属过),远程那份旧的不能盖回来。"""
        rp.pull_events(session, settings=None, rows=([_ev(mins_ago=10)], [_mb()]))
        ev = session.scalar(select(HotspotEvent))
        ev.last_seen = datetime.now()                       # 模拟本机更新过
        newer = ev.last_seen
        session.commit()
        rp.pull_events(session, settings=None, rows=([_ev(mins_ago=120)], []))
        session.refresh(ev)
        assert ev.last_seen >= newer - timedelta(seconds=1)

    def test_平台数按并集涨(self, session) -> None:
        """远程在别的平台也见到它 ⇒ 并进来,但**不清掉**本地已知的平台。"""
        rp.pull_events(session, settings=None, rows=([_ev(plats=("weibo",), pc=1)], [_mb()]))
        rp.pull_events(session, settings=None,
                       rows=([_ev(plats=("weibo", "baidu"), pc=2)], []))
        ev = session.scalar(select(HotspotEvent))
        assert ev.platform_count == 2 and "baidu" in ev.platforms

    def test_远程event_id不同也按norm_title认行(self, session) -> None:
        """★ 两边 id 各自自增 ⇒ 远程换了个 id,本地也必须认成**同一行**(靠 norm_title)。"""
        rp.pull_events(session, settings=None, rows=([_ev(remote_id=1)], [_mb()]))
        out = rp.pull_events(session, settings=None,
                             rows=([_ev(remote_id=999, mins_ago=1)], [_mb(remote_event_id=999)]))
        assert out["created"] == 0
        assert len(session.scalars(select(HotspotEvent)).all()) == 1

    def test_远程同标题多行先折成一条(self, session) -> None:
        """★ 远程按时间窗分行:同一个词可以先是 `ended`、过两天又 `active`
        (实测近 7 天 3858 行里有 3 组)。不先折,两条会**轮流**写 `status`,
        同一批跑两遍 `updated` 在 0 和 6 之间抖 —— 幂等就破了。
        """
        rows = ([_ev(remote_id=207345, mins_ago=60 * 24 * 4, status="ended",
                     ended_at=datetime.now() - timedelta(days=4), plats=("weibo",), pc=1),
                 _ev(remote_id=209583, mins_ago=5, status="active", ended_at=None,
                     plats=("douhot",), pc=1)],
                [_mb(remote_event_id=207345, board="weibo"),
                 _mb(remote_event_id=209583, board="douhot")])
        out = rp.pull_events(session, settings=None, rows=rows)
        assert out["created"] == 1, "同 norm_title 只能建一条"
        ev = session.scalar(select(HotspotEvent))
        assert ev.status == "active" and ev.ended_at is None, "生命周期取 last_seen 最靠前那条"
        assert ev.platform_count == 2, "平台取并集"
        #: 两条远程 id 都要能映射到这一条本地行(成员表按远程 id 指过来)
        assert out["members"] == 2
        assert {m.board for m in session.scalars(select(EventMembership)).all()} == {"weibo", "douhot"}
        #: 再跑一遍必须完全不动
        again = rp.pull_events(session, settings=None, rows=rows)
        assert again["created"] == 0 and again["updated"] == 0 and again["members"] == 0

    def test_成员指向不存在的本地事件时跳过(self, session) -> None:
        """远程给了成员、但事件因粒度/窗口被跳过 ⇒ **不许顺着建一条孤儿成员**。"""
        out = rp.pull_events(session, settings=None, rows=([], [_mb(remote_event_id=404)]))
        assert out["members"] == 0
        assert session.scalars(select(EventMembership)).all() == []


class TestPullHotSources:
    def _row(self, source="36kr", rank=1, title="某条新闻", ts=None):
        return {"source": source, "rank": rank, "title": title, "url": "",
                "extra": "", "captured_at": ts or datetime.now(), "published_at": None}

    def test_bili_pan必须跳过(self, session) -> None:
        """★ 那是**本机推上去的**,拉回来就是回环(每次拉取多一份自己造的副本)。"""
        out = rp.pull_hot_sources(session, settings=None,
                                  rows=[self._row(source="bili-pan", title="对标视频")])
        assert out["inserted"] == 0
        assert session.scalars(select(HotSourceItem)).all() == []

    def test_整批幂等(self, session) -> None:
        ts = datetime.now()
        rows = [self._row(rank=1, title="甲", ts=ts), self._row(rank=2, title="乙", ts=ts)]
        first = rp.pull_hot_sources(session, settings=None, rows=rows)
        second = rp.pull_hot_sources(session, settings=None, rows=rows)
        assert first["inserted"] == 2 and second["inserted"] == 0
        assert len(session.scalars(select(HotSourceItem)).all()) == 2

    def test_新一轮才插_老一轮不重复(self, session) -> None:
        old = datetime.now() - timedelta(hours=1)
        new = datetime.now()
        rp.pull_hot_sources(session, settings=None,
                            rows=[self._row(title="上一轮", ts=old)])
        out = rp.pull_hot_sources(session, settings=None,
                                  rows=[self._row(title="这一轮", ts=new)])
        assert out["inserted"] == 1
        assert len(session.scalars(select(HotSourceItem)).all()) == 2

    def test_源数为零时如实返回(self, session) -> None:
        out = rp.pull_hot_sources(session, settings=None, rows=[])
        assert out == {"sources": 0, "rows": 0, "inserted": 0}


class TestTickDiscipline:
    def test_没配远端就不记运行记录(self, session, monkeypatch) -> None:
        """空转不许刷"success 但什么也没干"的空轮(与 `remote_sync_tick` 同一条)。"""
        from config.settings import get_settings

        st = get_settings()
        monkeypatch.setattr(st, "remote_ssh_host", "", raising=False)
        monkeypatch.setattr(st, "remote_db_url", "", raising=False)
        assert rp.remote_pull_tick(st) == 0

    def test_拉取范围不含原始热榜表(self) -> None:
        """★★ **源码级守卫**:拉取 SQL 里绝不能出现 weibo/baidu/douhot 那三张表。

        为什么必须钉死:那三张表在本机有别的消费者(`focus_alert`/`early_agent`/
        `cross_platform`),而两端的飞书 webhook 指纹**相同** ⇒ 拉回原始热榜会让
        "跨板块共振"在本机被**再判一次**,往同一个群推第二遍 —— 纯重复告警。
        这个错误**不会报错、也不会有人发现**,只能靠这条守卫拦。
        """
        import ast
        import inspect

        src = inspect.getsource(rp)
        tree = ast.parse(src)
        banned = {"weibo_hot_items", "baidu_hot_items", "douhot_words", "xianyu_items"}
        #: ⚠️ **必须排除 docstring**:模块头注里正写着「为什么不拉这三张表」的名字,
        #: 按子串扫会把**解释**当成**违规**(本仓已栽过两次的同一族)。
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        seen_tables: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings:
                for t in banned:
                    if t in node.value:
                        seen_tables.add(t)
        assert not seen_tables, (
            f"`remote_pull` 里出现了不该拉的原始热榜表:{sorted(seen_tables)} —— "
            "那三张表本机有别的消费者,拉回来会重复告警(见模块头注)")


class TestAssignGuard:
    def test_wechat侧不跑事件归并(self, session, monkeypatch) -> None:
        """★ 镜像事件的生命周期归**远程**管,本机不许改写。

        本机 `scheduler_role=wechat` **不采**那三张原始热榜表(`weibo_hot_items`
        52 行停在 11-06、`baidu_hot_items` 0 行)⇒ 在本机点「立即归属一轮」,
        `assign_tick` 扫不到新快照却照样跑「>72h 无 seen ⇒ ended」那段。
        实测(2026-11-18)本机镜像来的 3843 条事件里有 **5 条**远程仍判 `active`
        但 `last_seen` 超 72h —— 会被置成 ended,下一轮拉取再被远程纠正回来,
        来回翻状态而没人知道为什么。
        """
        from app.api.events_api import events_assign
        from config.settings import get_settings

        st = get_settings()
        monkeypatch.setattr(st, "scheduler_role", "wechat", raising=False)
        session.add(HotspotEvent(user_id=1, primary_title="远程仍活跃", norm_title="远程仍活跃",
                                 platforms='["weibo"]', platform_count=1,
                                 first_seen=datetime.now() - timedelta(days=10),
                                 last_seen=datetime.now() - timedelta(hours=100),
                                 peak_value=1.0, status="active", sample_count=1))
        session.commit()

        out = events_assign(user=session.get(User, 1), db=session)
        assert out.get("skipped") == "not_hotspot_role", "必须说清为什么没跑,不能静默返回 0"
        ev = session.scalar(select(HotspotEvent))
        assert ev.status == "active" and ev.ended_at is None
