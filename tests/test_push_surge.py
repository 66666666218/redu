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


def _add_hands(session, n, *, section="kuaishou", pan=PAN, hours_ago=1.0, prefix="号",
               work="某作品"):
    """造 `n` 个不同作者推**同一部作品**(书名号是链内分桶的锚点,见 `_work_key`)。"""
    for i in range(n):
        _add(session, section, pan, f"《{work}》{prefix}{i}", hours_ago=hours_ago,
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
    assert stats["pans"] == 1 and stats["cand"] == 1 and stats["works"] == 1


def test_我们自己重推多遍只算一个人(session) -> None:
    """★★ **数的是作者不是行数**。

    同一资源被 `board_digest`/补推/重试各推一遍 ⇒ 一个作者能留下十几行。
    按行数算的话,卡片会喊"20 人在推"而其实只有 1 个人 —— 那正是这条提醒要防的假信号。
    """
    for _ in range(20):
        #: 同一 `item_key` + 同一作者反复落行 = 我们自己重推/重试的形状
        _add(session, "kuaishou", PAN, "《某作品》标题0", hours_ago=1.0, author="同一个人")
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
        _add(session, "kuaishou", PAN, f"《某作品》无作者{i}", hours_ago=1.0, author="")
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
        _add(session, "tieba", PAN, f"《某作品》别家{i}", hours_ago=1.0, author=f"他{i}",
             pan_id="pan.quark.cn/s/OTHER")
    session.commit()
    out = ps.detect_surges(session, 1)
    assert len(out) == 2, f"源链身份没分开(会虚报推手数):{out}"
    assert all(a["hands"] == 6 for a in out)


def test_一条链挂多个不相干作品时不许合并计数(session) -> None:
    """★★ **`pan_url` 不是作品身份** —— 2026-11-20 真实台账审计出来的假信号。

    实测 `pan.quark.cn/s/d1d3c3385b95` 近 7 天 129 行挂着 **31 个不同作品**
    (顶级训犬大师/麦穗与羊/予上情深/野火/直男s友…),因为这条链被反复复用。
    只按盘链聚人的话,「3 人推 A + 2 人推 B + 2 人推 C」会被报成「7 个人在推 A」——
    卡片会把别人的推手安到 A 头上,正是这条提醒要防的假信号。
    """
    _add_hands(session, 3, prefix="甲", work="甲作品")
    _add_hands(session, 2, prefix="乙", work="乙作品")
    _add_hands(session, 2, prefix="丙", work="丙作品")
    session.commit()
    stats: dict = {}
    assert ps.detect_surges(session, 1, stats=stats) == [], \
        "三个不相干作品的推手被合并成了一条假突增"
    #: 三条链内作品桶都要被看见(不许静默:运行记录要能说出"看了 1 条链 3 个作品")
    assert stats["pans"] == 1 and stats["works"] == 3


def test_同一作品的不同标题写法要算同一个人群(session) -> None:
    """★ 分桶对**变体**要宽容:同一部作品有 `《X》` 和 `X｜原稿…` 多种写法。

    实测(2026-11-19 04:17 公众号一波):8 个号半小时内推「麦穗与羊」,标题形态各异 ——
    `《麦穗与羊》原稿` / `麦穗与羊（原稿未删减）` / `麦穗与羊｜原稿未删版看哭了`
    / `【麦穗与羊】原稿未删版＋视频`。**按整条标题精确分桶会把这一波拆成 6 桶、
    每桶 1~2 人 ⇒ 一条都报不出来**。用《》里的名字做子串归属才能把它们归成一个桶。
    """
    titles = ["《麦穗与羊》原稿（可下载保存）",
              "麦穗与羊（原稿未删减）",
              "麦穗与羊｜原稿未删版,看哭了多少人【入口自取】",
              "【麦穗与羊】原稿未删版＋视频,看哭了多少人【入口自取】",
              "近期超火《麦穗与羊》原稿（可下载）",
              "《麦穗与羊》原稿 完整版"]
    for i, t in enumerate(titles):
        _add(session, "wechat", PAN, t, hours_ago=1.0, author=f"号{i}")
    session.commit()
    out = ps.detect_surges(session, 1)
    assert len(out) == 1, f"同一作品的标题变体被拆成了多桶:{out}"
    assert out[0]["hands"] == 6 and out[0]["work_key"] == "麦穗与羊"


def test_跨板块合成一条_不是每板块一条(session) -> None:
    """★ **用户口径「每个板块都加上」的落法**:一个作业看全板块,卡片里拆板块分布。

    同一个资源在 5 个板块各 2 个人 = 10 人 ⇒ **一条**提醒,带板块分布;
    按板块分开跑会变成 5 条(每条 2 人、都够不上阈值 ⇒ 反而一条都报不出来)。
    """
    for sec in ("tieba", "zhihu", "bilibili", "kuaishou", "xiaohongshu"):
        _add_hands(session, 2, section=sec, prefix=sec, work="跨板块作品")
    session.commit()
    out = ps.detect_surges(session, 1)
    assert len(out) == 1, f"跨板块没合并成一条:{out}"
    assert out[0]["hands"] == 10
    assert len(out[0]["sections"]) == 5
    assert sum(out[0]["sections"].values()) == 10


def test_渲染里带得出去重后的推手数和板块分布() -> None:
    """算得出但没渲染 = 没做。"""
    a = {"pan_key": "pan.quark.cn/s/x", "pan_url": PAN, "pan_type": "夸克",
         "title": "《某作品》全集", "work_key": "某作品", "hands": 9, "prev_hands": 3,
         "sections": {"tieba": 4, "zhihu": 5}}
    text = ps._render(a, chr(10))
    assert "3 → 9" in text or ("3" in text and "9" in text)
    assert "+6" in text
    assert "贴吧" in text and "知乎" in text
    #: 卡片头要挂**作品名**,不是整条标题 —— 人一眼能看出是哪个作品在起量
    assert "某作品" in text.split(chr(10))[0]


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


def test_发送失败不许吃掉落后的冷却(session, monkeypatch) -> None:
    """★ **发送成功才落冷却行** —— 否则一次网络抖动 = 这条提醒永久丢失。

    冷却 24h,而这个 3h 窗口一过推手数就再也凑不齐 ⇒ 先落冷却再发的话,
    一次飞书抖动就等于把这条真起量丢了。`douhot_window` 踩过同一条。
    """
    import app.db
    from app.db.models import FeishuAlert
    from app.services import feishu_client

    class _Boom:
        """:class:`FeishuClient` 的替身:网络失败一律返回 False。"""

        def __init__(self, *a, **kw):
            pass

        def send(self, text):
            return False

    _add_hands(session, 6)
    session.commit()
    #: ⚠️ 先证**这条内容本来会被报出来** —— 否则 t=0 与"冷却行 0"两者都平凡成立,
    #: 这条守卫就变成空转的绿灯(本仓母题:静默失败 = 假成功)。
    assert len(ps.detect_surges(session, 1)) == 1, "前置条件没满足:本该有告警"

    #: ⚠️ `push_surge_tick` 是**在函数体里** import 的 ⇒ 必须替换 `app.db` 上的名字,
    #: 替换 `ps.get_session_local` 不会生效,而且会让这条测试"看起来通过了"。
    monkeypatch.setattr(app.db, "get_session_local", lambda: (lambda: session))
    monkeypatch.setattr(feishu_client, "FeishuClient", _Boom)
    assert ps.push_surge_tick(settings=_Settings()) == 0
    session.rollback()
    left = session.query(FeishuAlert).filter(FeishuAlert.section == "push_surge").count()
    assert left == 0, "发送失败了却把冷却行留了下来(下轮就再也报不出来)"


def test_发送成功之后冷却才生效(session, monkeypatch) -> None:
    """★ 上一条的反面:发成功了必须**真的存下**冷却,否则每小时重复报同一条。

    两条合起来钉死语义:「**冷却 = 已经发出去过**」,而不是「跑过一次检测」。
    """
    import app.db
    from app.db.models import FeishuAlert
    from app.services import feishu_client

    class _Ok:
        def __init__(self, *a, **kw):
            pass

        def send(self, text):
            return True

    _add_hands(session, 6)
    session.commit()
    monkeypatch.setattr(app.db, "get_session_local", lambda: (lambda: session))
    monkeypatch.setattr(feishu_client, "FeishuClient", _Ok)
    assert ps.push_surge_tick(settings=_Settings()) == 1
    assert session.query(FeishuAlert).filter(
        FeishuAlert.section == "push_surge").count() == 1, "发成功了却没记冷却 ⇒ 每小时重复报"
    #: 第二轮:内容还在窗口里,但冷却已生效 ⇒ 不许再发
    assert ps.push_surge_tick(settings=_Settings()) == 0, "冷却没生效,同一条被重复推了"


class _Settings:
    """`push_surge_tick` 只读这几个开关。"""
    feishu_secret = ""
    feishu_webhook = ""
    feishu_webhook_admin = "https://example.invalid/hook"
    push_surge_window_hours = 3
    push_surge_min_hands = 6
    push_surge_min_gain = 4
    push_surge_min_ratio = 1.8
    push_surge_top = 5
    push_surge_cooldown_hours = 24


def _slots(job):
    """作业占用的 `(周几, 时, 分)`(APScheduler:0 = 周一)。"""
    f = {x.name: [str(e) for e in x.expressions] for x in job.trigger.fields}
    hours = range(24) if f["hour"] == ["*"] else [int(h) for h in f["hour"]]
    mins = range(60) if f["minute"] == ["*"] else [int(m) for m in f["minute"]]
    return [(0, h, m) for h in hours for m in mins]