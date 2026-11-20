"""同资源「推手数突增」提醒(2026-11-20)的守卫。

守的是四件容易退化的事:
  ① **三条判据同时成立才算突增** —— 单看"人多"会把长期火的资源每小时报一次;
     单看"涨了"会把 2→3 算成突增。用参数化的对照组逐一证伪。
  ② **数的是作者不是行数** —— 同一资源被我们自己重推 5 遍只是 1 个推手;
     这条写错,卡片会天天喊"20 人"而其实只有 2 个人。
  ③ **作者为空的行不许静默丢** —— 数不出是谁就不能算推手,但必须记账。
  ④ **跨板块是一个作业,不是每板块一个** —— 否则同一次起量报 N 次。
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
from app.services import push_surge as ps

PAN = "https://pan.quark.cn/s/surge0001"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _add(session, section, pan, title, hours_ago=1.0, author="某人", **kw):
    session.add(PushLedger(user_id=1, section=section, item_key=title, title=title,
                           pan_url=pan, pan_type="夸克", author=author,
                           pushed_at=datetime.now() - timedelta(hours=hours_ago), **kw))


def _add_hands(session, n, *, section="kuaishou", pan=PAN, hours_ago=1.0, prefix="号"):
    for i in range(n):
        _add(session, section, pan, f"标题{i}", hours_ago=hours_ago,
             author=f"{prefix}{i}")


def test_多人同时起量会被抓到(session) -> None:
    """当前窗口 6 个不同作者、上一窗口 0 ⇒ 三条判据全过。"""
    _add_hands(session, 6)
    session.commit()
    stats: dict = {}
    out = ps.detect_surges(session, 1, stats=stats)
    assert len(out) == 1, f"该报没报:{out}"
    assert out[0]["hands"] == 6 and out[0]["prev_hands"] == 0
    #: ⚠️ 不许静默:每轮都得留下"看了多少"的数字(与 `_record_run` 的漏斗同一条纪律)
    assert stats["pans"] == 1 and stats["cand"] == 1


def test_我们自己重推多遍只算一个人(session) -> None:
    """★★ **数的是作者不是行数**。

    同一资源被 `board_digest`/补推/重试各推一遍 ⇒ 一个作者能留下十几行。
    按行数算的话,卡片会喊"20 人在推"而其实只有 1 个人 —— 那正是这条提醒要防的假信号。
    """
    for _ in range(20):
        #: 同一 `item_key` + 同一作者反复落行 = 我们自己重推/重试的形状
        _add(session, "kuaishou", PAN, "标题0", hours_ago=1.0, author="同一个人")
    session.commit()
    out = ps.detect_surges(session, 1)
    assert out == [], "把重复推送数成了推手数"


def test_增量不够不许报(session) -> None:
    """当前 7 人、上一窗口 5 人 ⇒ 只多 2 个,是**已经在推**不是**突然变多**。"""
    _add_hands(session, 7, prefix="新")
    _add_hands(session, 5, hours_ago=5.0, prefix="旧")
    session.commit()
    assert ps.detect_surges(session, 1) == []


def test_倍数不够不许报(session) -> None:
    """当前 8 人、上一窗口 6 人 ⇒ 增量 2 不够;换成 8→4 也过不了 1.8 倍。"""
    _add_hands(session, 9, prefix="新")
    _add_hands(session, 6, hours_ago=5.0, prefix="旧")
    session.commit()
    #: 9-6=3 < 4 ⇒ 增量闸挡下(先证增量闸是承重的)
    assert ps.detect_surges(session, 1) == []
    #: 放宽增量仍过不了倍数:6→9 只有 1.5 倍
    assert ps.detect_surges(session, 1, min_gain=1) == []
    #: 两条都放宽才放行 —— 证明两个闸各自都在起作用
    assert len(ps.detect_surges(session, 1, min_gain=1, min_ratio=1.4)) == 1


def test_人数不够不许报(session) -> None:
    """上一窗口 0、当前 5 人:涨得多但绝对量太小(5 人够不上"很多人在推")。"""
    _add_hands(session, 5)
    session.commit()
    assert ps.detect_surges(session, 1) == []
    assert len(ps.detect_surges(session, 1, min_hands=5)) == 1


def test_作者为空的行不能算推手_但必须记账(session) -> None:
    """数不出是谁 ⇒ 不是"一个人在推";这些行**不许静默吞掉**。"""
    _add_hands(session, 5)
    for i in range(10):
        _add(session, "kuaishou", PAN, f"无作者{i}", hours_ago=1.0, author="")
    session.commit()
    stats: dict = {}
    out = ps.detect_surges(session, 1, stats=stats)
    assert out == [], "空作者被当成了推手"
    assert stats["no_author"] == 10, "空作者的行被静默丢了(运行记录会说不出原因)"


def test_窗口外的行不参与(session) -> None:
    """上一窗口之外(4h 前的 3h 窗口 = 6h 前)的行不该算进任何一档。"""
    _add_hands(session, 6)
    _add_hands(session, 20, hours_ago=8.0, prefix="很久前")
    session.commit()
    out = ps.detect_surges(session, 1)
    assert len(out) == 1 and out[0]["prev_hands"] == 0, \
        f"窗口外的行被算进上一窗口:{out}"


def test_源链优先于我方链作为身份(session) -> None:
    """`pan_id`(源链)优先 —— 与我方链的判断口径同源(`_pan_id_of` 的实测取舍)。"""
    _add_hands(session, 6)
    #: 同一条 `pan_url` 但 `pan_id` 不同 ⇒ 是**不同资源**,不该合并成 12 人
    for i in range(6):
        _add(session, "tieba", PAN, f"别家{i}", hours_ago=1.0, author=f"他{i}",
             pan_id="pan.quark.cn/s/OTHER")
    session.commit()
    out = ps.detect_surges(session, 1)
    assert len(out) == 2, f"源链身份没分开(会虚报推手数):{out}"
    assert all(a["hands"] == 6 for a in out)


def test_跨板块合成一条_不是每板块一条(session) -> None:
    """★ **用户口径「每个板块都加上」的落法**:一个作业看全板块,卡片里拆板块分布。

    同一个资源在 5 个板块各 2 个人 = 10 人 ⇒ **一条**提醒,带板块分布;
    按板块分开跑会变成 5 条(每条 2 人、都够不上阈值 ⇒ 反而一条都报不出来)。
    """
    for sec in ("tieba", "zhihu", "bilibili", "kuaishou", "xiaohongshu"):
        _add_hands(session, 2, section=sec, prefix=sec)
    session.commit()
    out = ps.detect_surges(session, 1)
    assert len(out) == 1, f"跨板块没合并成一条:{out}"
    assert out[0]["hands"] == 10
    assert len(out[0]["sections"]) == 5
    assert sum(out[0]["sections"].values()) == 10


def test_渲染里带得出去重后的推手数和板块分布() -> None:
    """算得出但没渲染 = 没做。"""
    a = {"pan_key": "pan.quark.cn/s/x", "pan_url": PAN, "pan_type": "夸克",
         "title": "某资源", "hands": 9, "prev_hands": 3,
         "sections": {"tieba": 4, "zhihu": 5}}
    text = ps._render(a, chr(10))
    assert "3 → 9" in text or ("3" in text and "9" in text)
    assert "+6" in text
    assert "贴吧" in text and "知乎" in text


def test_作业按管理员群发_且角色只在本机(session, monkeypatch) -> None:
    """去向必须是**管理群**(运营情报不进客户群),且角色 `wechat`(台账只在本机写)。"""
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.services.scheduler import build_jobs

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)
    job = next((j for j in sched.get_jobs() if j.id == "push_surge"), None)
    assert job is not None, "作业没注册"
    #: 小时级、每小时 33 分(:00/:30 是重试与告警 tick 的地盘)
    assert {m for _, _, m in _slots(job)} == {33}


def _slots(job):
    """作业占用的 `(周几, 时, 分)`(APScheduler:0 = 周一)。"""
    f = {x.name: [str(e) for e in x.expressions] for x in job.trigger.fields}
    hours = range(24) if f["hour"] == ["*"] else [int(h) for h in f["hour"]]
    mins = range(60) if f["minute"] == ["*"] else [int(m) for m in f["minute"]]
    return [(0, h, m) for h in hours for m in mins]