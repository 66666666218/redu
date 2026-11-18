"""跨板块重复通报(2026-11-18,档2)的守卫。

守的是三件容易退化的事:
  ① **只认盘链身份**:`item_key` 跨板块重复实测是 0 条(各平台抄的是资源、标题不同),
     拿它聚合会得到一张恒空的表 —— 那种"永远说没事"的报告比没有更糟。
  ② **规模数是截断前的**(`cross_total`):`top=10` 只截清单,不截"有多少个"。
  ③ **没有带盘行 ≠ 没有跨板块重复**:前者可能是台账/盘链抽取坏了,必须在报告里**说清**。
"""
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
from app.services import cross_section_report as rep


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _add(session, section, pan, title, hours_ago=2, **kw):
    session.add(PushLedger(user_id=1, section=section, item_key=kw.pop("item_key", title),
                           title=title, pan_url=pan, pan_type="夸克",
                           pushed_at=datetime.now() - timedelta(hours=hours_ago), **kw))


def test_跨板块聚合只认盘链_内容身份跨板块是零(session) -> None:
    """★★ 实测依据:各平台抄的是**同一份资源**、标题各不相同。

    近 7 天台账里 `item_key` 跨板块重复 **0 条** —— 所以按内容聚合会得到一张恒空的表。
    """
    pan = "https://pan.quark.cn/s/aabbccdd1122"
    _add(session, "tieba", pan, "贴吧的标题")
    _add(session, "bilibili", pan + "?entry=share", "B站完全不同的标题")   #: 写法差异是噪声
    _add(session, "zhihu", "https://pan.quark.cn/s/000000000000", "知乎另一份")
    session.commit()

    until = datetime.now() + timedelta(minutes=1)
    since = until - timedelta(days=7)
    s = pl.cross_section_summary(session, 1, since, until)
    assert s["pan_rows"] == 3 and s["pans"] == 2
    assert s["cross_total"] == 1, "只有那份重复的盘链算跨板块"
    g = s["cross"][0]
    assert set(g["sections"]) == {"tieba", "bilibili"}
    assert g["total"] == 2
    #: ⚠️ 标题取**先出现的那条**(与 `window()` 同口径)—— 后来的行只补空位
    assert g["title"] == "贴吧的标题"


def test_规模数是截断前的(session) -> None:
    """`top` 只截**清单**,不截"有多少个" —— 拿 `len(cross)` 当规模会把 5 个说成 1 个。"""
    for i in range(5):
        pan = f"https://pan.quark.cn/s/p{i:011d}"
        _add(session, "tieba", pan, f"资源{i}", item_key=f"t{i}")
        _add(session, "zhihu", pan, f"资源{i}-知乎版", item_key=f"z{i}")
    session.commit()
    until = datetime.now() + timedelta(minutes=1)
    s = pl.cross_section_summary(session, 1, until - timedelta(days=7), until, top=2)
    assert len(s["cross"]) == 2, "清单按 top 截断"
    assert s["cross_total"] == 5, "但**规模**必须是截断前的"
    assert s["cross_rows"] == 10


def test_没有带盘行要说清是两种可能(session) -> None:
    """⚠️ 「一条盘链都没有」可能是**台账/抽取坏了**,与「没跨板块重复」是两件事。"""
    _add(session, "douyin", "", "只有视频没有盘")
    session.commit()
    text = rep.build_report(session, 1)
    assert "一条盘链都没有" in text
    assert "断了" in text or "人工核" in text, \
        "必须提示这可能不是'正常',否则读的人会当成好消息"
    assert "✅ 没有跨板块重复" not in text


def test_没有跨板块重复时是好消息(session) -> None:
    _add(session, "tieba", "https://pan.quark.cn/s/aaaaaaaaaaaa", "甲")
    _add(session, "zhihu", "https://pan.quark.cn/s/bbbbbbbbbbbb", "乙")
    session.commit()
    text = rep.build_report(session, 1)
    assert "✅ 没有跨板块重复" in text
    assert "一条盘链都没有" not in text


def test_报告里带板块中文名与比例(session) -> None:
    pan = "https://pan.quark.cn/s/cccccccccccc"
    _add(session, "tieba", pan, "同一份")
    _add(session, "zhihu", pan, "同一份-知乎", item_key="另一条")
    _add(session, "bilibili", "https://pan.quark.cn/s/dddddddddddd", "另一份")
    session.commit()
    text = rep.build_report(session, 1)
    assert "贴吧" in text and "知乎" in text, "板块要显示中文名,不是原始 section 键"
    assert "2 个板块" in text
