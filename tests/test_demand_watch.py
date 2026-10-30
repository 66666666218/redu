"""评论区「在求什么」监控(2026-10-30)。不联网。

用户口径:「监控一些**评论区都在求的大瓜名称**…只需要**监控名称**就行,不用保存到网盘」。

这组测试钉三件事:
1. **抽名字**:书名号 / 「求…」「谁有…」后面的那段;泛词与半句话**不许**当名字;
2. **求 vs 提及要分开算** —— 只看「提到名字」会把榜单灌成"热门",而不是"被求";
3. **推送有门槛和冷却** —— 作业 4 小时一轮,不冷却就是一天推 6 次同样的名字。
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
from app.db.models import DemandName, User
from app.services import demand_watch as dw


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
    """够用的 settings 替身(只放 `run_user`/`source_videos` 会读的那几项)。"""
    demand_watch_enabled = True
    demand_source_days = 7
    demand_seed_words = ""
    demand_min_comments = 20
    demand_max_videos = 2
    demand_comment_pages = 1
    demand_min_hits = 2
    demand_push_cooldown_hours = 48
    demand_push_limit = 10
    feishu_webhook = ""
    feishu_webhook_douyin = ""
    feishu_secret = ""


# ---------------------------------------------------------------- 抽名字

def test_书名号里的名字会被抽出来_带求语气算一次需求():
    hits = dw.extract_names("求《狗爹和小桃》全集,谢谢大佬")
    assert ("狗爹和小桃", True) in hits


def test_只提到不求的名字也算提及_但不算需求():
    """**分开算是这张表的意义**:「这个《X》真好看」是热度,不是需求。"""
    hits = dw.extract_names("这个《兽人与人类的禁忌羁绊》真好看")
    assert hits == [("兽人与人类的禁忌羁绊", False)]


def test_求字后面的名字():
    hits = dw.extract_names("谁有香香公主与爱犬金毛啊")
    assert any(n == "香香公主与爱犬金毛" and d for n, d in hits), hits


def test_在哪看前面的名字():
    hits = dw.extract_names("pupu噗噗在哪看呀")
    assert any(n == "pupu噗噗" and d for n, d in hits), hits


@pytest.mark.parametrize("text", [
    "求个真实有效的资源",          # 剥掉尾巴只剩半句话
    "求资源",                     # 整句就是泛词
    "有没有大佬分享一下",          # 没有人名
    "《合集》",
    #: ★ 实跑抓到的假名字:聊「给孩子起名」的评论里有「…你爷爷叫什么」
    "现在父母起名字有没有什么大气之类的，等到你年纪大了之后，别人问你孙子你爷爷叫什么，",
])
def test_泛词与半句话不许当名字(text):
    assert dw.extract_names(text) == [], f"{text!r} 不应抽出名字"


def test_一句话里多个名字逐个算():
    hits = dw.extract_names("求《小小绘》和《兔兔图图》的资源")
    assert {n for n, _ in hits} == {"小小绘", "兔兔图图"}
    assert all(d for _, d in hits)


def test_is_name_like_的边界():
    assert dw.is_name_like("Alli Skye")
    assert dw.is_name_like("sunny水水水")
    assert not dw.is_name_like("资")            # 太短
    assert not dw.is_name_like("资源")
    assert not dw.is_name_like("有没有的")       # 半句话
    assert not dw.is_name_like("12345")          # 纯数字


# ---------------------------------------------------------------- 单视频聚合

def test_一个视频的评论聚合成名字计数(monkeypatch):
    pages = [
        {"ok": True, "has_more": False, "error": "", "comments": [
            {"text": "求《狗爹和小桃》全集", "like": 3, "user": "a", "cid": "1"},
            {"text": "《狗爹和小桃》在哪看", "like": 1, "user": "b", "cid": "2"},
            {"text": "这个《小小绘》真好看", "like": 9, "user": "c", "cid": "3"},
        ]},
    ]
    monkeypatch.setattr(dw, "fetch_comments", lambda *a, **k: pages.pop(0))

    out = dw.collect_for_video("123", session=None, pages=1)

    assert out["error"] == ""
    assert out["names"]["狗爹和小桃"]["demand"] == 2
    assert out["names"]["狗爹和小桃"]["mention"] == 2
    assert out["names"]["小小绘"]["demand"] == 0
    assert out["names"]["小小绘"]["mention"] == 1


def test_拉评论失败不写空行(monkeypatch):
    monkeypatch.setattr(dw, "fetch_comments",
                        lambda *a, **k: {"ok": False, "comments": [], "has_more": False,
                                         "error": "HTTP 400"})
    out = dw.collect_for_video("123", session=None, pages=1)
    assert out["names"] == {}
    assert "400" in out["error"]


# ---------------------------------------------------------------- 推送门槛

def _row(session, name, demand, mention=0, pushed_hours_ago=None):
    now = datetime.now()
    r = DemandName(user_id=1, platform="douyin", name=name, demand_hits=demand,
                   mention_hits=mention, last_seen_at=now,
                   last_pushed_at=(now - timedelta(hours=pushed_hours_ago)
                                   if pushed_hours_ago is not None else None))
    session.add(r)
    session.commit()
    return r


def test_不够格的不推_冷却期内的不推(session):
    _row(session, "只求过一次", 1)
    _row(session, "刚推过", 5, pushed_hours_ago=1)
    good = _row(session, "该推", 4, mention=9)

    rows = session.query(DemandName).all()
    chosen = dw.pick_pushable(rows, min_demand=2, cooldown_hours=48, limit=10)

    assert [r.name for r in chosen] == ["该推"]
    assert chosen[0].id == good.id


def test_冷却过后可以再推_并按次数排序(session):
    _row(session, "旧的", 3, pushed_hours_ago=72)
    _row(session, "更热", 9, mention=20)

    rows = session.query(DemandName).all()
    chosen = dw.pick_pushable(rows, min_demand=2, cooldown_hours=48, limit=10)

    assert [r.name for r in chosen] == ["更热", "旧的"]


def test_卡片里有名字_次数与样例(session):
    r = _row(session, "狗爹和小桃", 3, mention=5)
    r.samples = '{"comments": ["求《狗爹和小桃》全集"], "video": "https://www.douyin.com/video/1"}'
    session.commit()

    card = dw.build_card([r])
    body = str(card)

    assert "狗爹和小桃" in body
    assert "求 3 次" in body
    assert "https://www.douyin.com/video/1" in body


def test_拉了评论却一个名字都没有时记partial(session, monkeypatch):
    """链路坏了 / 这轮没人求 —— 两种都要留下痕迹,不能都写成 success。"""
    from app.db.models import RunRecord

    _lead(session, "111")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {"error": "", "names": {}})

    dw.run_user(session, 1, _S())

    run = session.query(RunRecord).filter(RunRecord.kind == "demand_watch").one()
    assert run.status == "partial"


# ---------------------------------------------------------------- 回复里找答案

def test_回复里的名字会被当成所求的名字():
    """「这女的叫什么」+ 回复「Alli Skye」—— 名字在回复里,不在正文。"""
    replies = ["Alli Skye", "不知道"]
    assert dw.names_from_replies(replies) == ["Alli Skye"]


def test_问了但正文没名字时去回复里取(monkeypatch):
    pages = [{"ok": True, "has_more": False, "error": "", "comments": [
        {"text": "这女的叫什么", "like": 1, "user": "a", "cid": "1",
         "replies": ["小小绘"]},
    ]}]
    monkeypatch.setattr(dw, "fetch_comments", lambda *a, **k: pages.pop(0))

    out = dw.collect_for_video("1", session=None, pages=1)

    assert out["names"]["小小绘"]["demand"] == 1


def test_有求但抽不到名字要计数(monkeypatch):
    """`求N(无名)` 是判断「素材对不对」的唯一证据 —— 短剧类评论就是这样。"""
    pages = [{"ok": True, "has_more": False, "error": "", "comments": [
        {"text": "求免费的，可赞", "like": 1, "user": "a", "cid": "1", "replies": []},
        {"text": "好看", "like": 1, "user": "b", "cid": "2", "replies": []},
    ]}]
    monkeypatch.setattr(dw, "fetch_comments", lambda *a, **k: pages.pop(0))

    out = dw.collect_for_video("1", session=None, pages=1)

    assert out["names"] == {}
    assert out["no_name"] == 1


# ---------------------------------------------------------------- 入口

def _lead(session, aweme_id="111", title="求《狗爹和小桃》全集"):
    from app.db.models import DouyinLead

    session.add(DouyinLead(user_id=1, aweme_id=aweme_id, title=title, author="a",
                           url=f"https://www.douyin.com/video/{aweme_id}",
                           found_at=datetime.now(), found_date="2026-10-30"))
    session.commit()


def test_素材优先取我们自己的线索_而不是搜吃瓜(session, monkeypatch):
    """实跑教训:搜 `吃瓜` 抽出来的是「8折外卖」—— 那不是这一类。

    素材应该来自**我们自己收的推广视频**(`douyin_leads`),它们的评论区才会有人求资源。
    """
    _lead(session, "111")
    _lead(session, "222", title="《小小绘》vlog")
    monkeypatch.setattr("app.services.douyin_protocol_source.search",
                        lambda *a, **k: pytest.fail("有库内素材时不该去搜"))
    monkeypatch.setattr(dw, "collect_for_video",
                        lambda vid, **k: {"error": "", "names": {}})

    vids = dw.source_videos(session, 1, _S())

    assert {v["aweme_id"] for v in vids} == {"111", "222"}


def test_run_user_把名字写进库并推一次(session, monkeypatch):
    _lead(session, "111")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {
        "error": "", "names": {"狗爹和小桃": {"demand": 3, "mention": 4,
                                            "samples": ["求《狗爹和小桃》全集"]}}})

    sent = []
    monkeypatch.setattr(dw, "push_card", lambda rows, settings: sent.append(list(rows)) or True)

    out = dw.run_user(session, 1, _S())

    assert out["names"] == 1
    assert out["pushed"] == 1
    assert sent and sent[0][0].name == "狗爹和小桃"
    row = session.query(DemandName).one()
    assert row.demand_hits == 3
    assert row.last_pushed_at is not None


def test_没有名字时既不建行也不推(session, monkeypatch):
    _lead(session, "111")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {"error": "", "names": {}})
    monkeypatch.setattr(dw, "push_card", lambda rows, settings: pytest.fail("不该推空卡"))

    out = dw.run_user(session, 1, _S())

    assert out["names"] == 0
    assert session.query(DemandName).count() == 0
