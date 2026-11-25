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
    #: ⚠️ 替身里**关掉热点取词**:它会读热点宝 Cookie 并真的出网(生产里是预期行为,
    #: 单测里会让整条链变慢、结果还取决于当天榜单)。测热点的用例自己打开。
    demand_hot_videos = 0
    demand_comment_pages = 1
    demand_min_hits = 2
    demand_min_ones = 20
    demand_push_cooldown_hours = 48
    demand_push_limit = 10
    #: ⚠️ **替身里默认关掉发布时间闸**(`demand_max_age_days = 0`),而生产是 **3 天**。
    #: 原因是这组用例的 aweme_id 全是 `111`/`999` 这种假 id —— 没有雪花高位、也没带
    #: `create_time` ⇒ 一律"判不了时间",闸一开就会把它们全挡掉,**测的就不再是原来的东西**。
    #: 闸本身由 `test_发布时间闸_*` 那组用例**自己打开**并用真的 19 位雪花 id 验证。
    #: (生产默认值的守卫在 `test_生产默认值_发布时间闸是开着的`。)
    demand_max_age_days = 0
    demand_source_max_age_days = 0
    demand_age_allow_unknown = False
    demand_name_max_age_days = 0
    #: ⚠️ **替身里默认关掉"已扫去重"与"库内保底"**(2026-11-24),而生产是 3 天 / 2 个名额。
    #: 这两个闸会改变**素材条数与来源**,而下面一大半用例断言的是"原来那套名额分配"
    #: (比如 `test_热点名额不被库内线索吃光` 要求正好 4 条)。
    #: 闸本身由 `test_已扫去重_*` / `test_库内保底_*` 那两组**自己打开**验证;
    #: 生产默认值的守卫在 `test_生产默认值_去重与保底是开着的`。
    demand_skip_scanned_days = 0
    demand_lib_reserve = 0
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
    #: 实跑抓到的另一个:承接词开头的半句话
    "还有一部叫什么来着",
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

    _lead(session, "111", title="一条没有书名号的普通视频")
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


# ---------------------------------------------------------------- 作品名 / 扣1 热度

def test_作品名里的资源名会被抽出来():
    """用户口径:「**基本上作品名都有资源名**」—— 标题是名字最稳的来源。"""
    assert dw.names_from_title("终于找到《反差直男》完整版了 #反差") == ["反差直男"]
    assert dw.names_from_title("扣1看《狗爹和小桃》和《小小绘》合集") == ["狗爹和小桃", "小小绘"]
    assert dw.names_from_title("日常vlog #无资源") == []


def test_标题里的长名字不因为超过12字被丢掉():
    assert dw.names_from_title("《sunny水水水/水子哥下海vlog》") == ["sunny水水水/水子哥下海vlog"]


def test_标签不是名字(session):
    """实跑教训:把 `#青筋`/`#vlog日常` 当名字,榜里全是描述词。

    名字只认书名号 —— 推广号的标题用 `《反差直男》`,标签只是描述。
    """
    assert dw.names_from_title("扣1看反差 #覆面肌肉男 #狗爹和小桃") == []
    assert dw.names_from_title("《反差直男》#覆面 #青筋") == ["反差直男"]


def test_种子词按轮换取_避免连发被限流(session):
    seeds = ["a", "b", "c", "d"]
    assert dw._rotate_seeds(session, seeds, 2) == ["a", "b"]
    assert dw._rotate_seeds(session, seeds, 2) == ["c", "d"]
    assert dw._rotate_seeds(session, seeds, 2) == ["a", "b"]   # 游标跨轮保存


def test_参考作品名优先作为素材源(session, monkeypatch):
    """用户口径「我上个对话给你的就是参考作品」⇒ 配了种子词就先按名字搜。"""
    _lead(session, "111", title="《库里随便一条》")

    class S2(_S):
        demand_seed_words = "狗爹和小桃"

    monkeypatch.setattr("app.services.douyin_protocol_source.search", lambda *a, **k: [
        {"aweme_id": "999", "url": "https://www.douyin.com/video/999",
         "snippet": "#狗爹和小桃", "metrics": {"comment_count": 500}},
    ])

    vids = dw.source_videos(session, 1, S2())

    assert vids[0]["aweme_id"] == "999"
    assert vids[0]["comment_count"] == 500


@pytest.mark.parametrize("text", ["1", "1111", "扣1", " 1！！", "1。"])
def test_扣1跟帖算热度不算名字(text):
    pages = [{"ok": True, "has_more": False, "error": "", "comments": [
        {"text": text, "like": 0, "user": "a", "cid": "1", "replies": []},
    ]}]
    import unittest.mock as mock

    with mock.patch.object(dw, "fetch_comments", lambda *a, **k: pages[0]):
        out = dw.collect_for_video("1", session=None, pages=1)
    assert out["ones"] == 1
    assert out["names"] == {}


def test_句子里的1不算扣1():
    pages = [{"ok": True, "has_more": False, "error": "", "comments": [
        {"text": "第1集真好看", "like": 0, "user": "a", "cid": "1", "replies": []},
    ]}]
    import unittest.mock as mock

    with mock.patch.object(dw, "fetch_comments", lambda *a, **k: pages[0]):
        out = dw.collect_for_video("1", session=None, pages=1)
    assert out["ones"] == 0


def test_扣1够多也能上卡_不需要有人求(session):
    r = DemandName(user_id=1, platform="douyin", name="狗爹和小桃",
                   demand_hits=0, ones_hits=50, title_hits=1,
                   last_seen_at=datetime.now())
    session.add(r)
    session.commit()

    chosen = dw.pick_pushable(session.query(DemandName).all(),
                              min_demand=2, min_ones=20, cooldown_hours=48, limit=10)
    assert [x.name for x in chosen] == ["狗爹和小桃"]


def test_run_user把标题名与扣1一起入库(session, monkeypatch):
    from app.db.models import DouyinLead

    session.add(DouyinLead(user_id=1, aweme_id="111", title="扣1看《狗爹和小桃》合集",
                           author="a", url="https://www.douyin.com/video/111",
                           found_at=datetime.now(), found_date="2026-10-30"))
    session.commit()
    monkeypatch.setattr(dw, "collect_for_video",
                        lambda *a, **k: {"error": "", "names": {}, "ones": 30, "no_name": 0})
    monkeypatch.setattr(dw, "push_card", lambda rows, settings: True)

    dw.run_user(session, 1, _S())

    row = session.query(DemandName).one()
    assert row.name == "狗爹和小桃"
    assert row.ones_hits == 30
    assert row.title_hits == 1


# ---------------------------------------------------------------- 求1型识别

def test_评论区在求在扣1的视频会被识别并推卡(session, monkeypatch):
    """用户口径:「特点就是评论区都在发求**和1**」。

    判据 = (整条 `1` + 带求语气) / 已拉评论数 ≥ `DEMAND_REQUEST_DENSITY`(默认 0.2)。
    """
    _lead(session, "111", title="扣1看《反差直男》")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {
        "error": "", "names": {}, "ones": 4, "no_name": 1, "total": 10})

    sent = []
    monkeypatch.setattr(dw, "push_request",
                        lambda items, settings: sent.append(list(items)) or True)

    dw.run_user(session, 1, _S())

    assert sent, "求1型视频应单独推一张卡"
    assert sent[0][0]["name"] == "反差直男"
    assert sent[0][0]["ones"] == 4 and sent[0][0]["demand"] == 1


def test_求1密度不够就不算这类视频(session, monkeypatch):
    _lead(session, "111", title="扣1看《反差直男》")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {
        "error": "", "names": {}, "ones": 1, "no_name": 0, "total": 40})
    monkeypatch.setattr(dw, "push_request",
                        lambda items, settings: pytest.fail("密度不够不该推"))

    dw.run_user(session, 1, _S())


def test_满屏重复短句也算求1型(session, monkeypatch):
    """用户给的样本(范小勤那条)实算:59 条评论、6 个 `1`、重复率 36%。

    按"求/1 密度 20%"会被漏掉(实际只有 12%),所以补了**重复短句**这条路。
    """
    _lead(session, "111", title="智慧蛊回来了 #范小勤 #范马宇宙 #唯有蹦迪")
    #: 密度只有 2/59≈3%(不够 10%),但重复率 40% ⇒ 只能靠**重复短句**这条路命中
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {
        "error": "", "names": {}, "ones": 2, "no_name": 0, "total": 59, "dup_ratio": 0.4})

    sent = []
    monkeypatch.setattr(dw, "push_request",
                        lambda items, settings: sent.append(list(items)) or True)

    dw.run_user(session, 1, _S())

    assert sent, "重复评论占比 ≥ 30% 时应判为求1型"
    assert sent[0][0]["why"] == "重复评论"
    assert sent[0][0]["name"] == "范马宇宙"


def test_带表情的1也算跟帖(monkeypatch):
    """实测样本里 `111[尴尬流汗]` 是真实形状 —— 不剥表情就认不出。"""
    pages = [{"ok": True, "has_more": False, "error": "", "comments": [
        {"text": "111[尴尬流汗]", "like": 0, "user": "a", "cid": "1", "replies": []},
        {"text": "1秒", "like": 0, "user": "b", "cid": "2", "replies": []},
    ]}]
    monkeypatch.setattr(dw, "fetch_comments", lambda *a, **k: pages.pop(0))

    out = dw.collect_for_video("1", session=None, pages=1)

    assert out["ones"] == 1, "`111[尴尬流汗]` 要算 1,`1秒` 不算"


def test_重复短句按归一后比对(monkeypatch):
    """`刚看完` / `刚看完[宕机]` / `刚看完。` 是同一条重复评论(第二条样本实测)。"""
    pages = [{"ok": True, "has_more": False, "error": "", "comments": [
        {"text": "刚看完", "like": 0, "user": "a", "cid": "1", "replies": []},
        {"text": "刚看完[宕机]", "like": 0, "user": "b", "cid": "2", "replies": []},
        {"text": "刚看完。", "like": 0, "user": "c", "cid": "3", "replies": []},
        {"text": "冰露", "like": 0, "user": "d", "cid": "4", "replies": []},
        {"text": "好看", "like": 0, "user": "e", "cid": "5", "replies": []},
    ]}]
    monkeypatch.setattr(dw, "fetch_comments", lambda *a, **k: pages.pop(0))

    out = dw.collect_for_video("1", session=None, pages=1)

    assert out["dup_ratio"] >= 0.35, out


def test_标题里出现参考作品名_语序反了也认(monkeypatch):
    """第三条样本:《我的天呐!!! **小桃和狗爹**简直是仙品》—— 没书名号、没标签。"""
    seeds = ["狗爹和小桃", "小小绘"]
    assert dw.match_seed("我的天呐！！！ 小桃和狗爹简直是仙品啊啊啊", seeds) == "狗爹和小桃"
    assert dw.match_seed("今天天气不错", seeds) == ""


def test_求1型名字取不到标签时用参考作品名(session, monkeypatch):
    _lead(session, "111", title="我的天呐！！！ 小桃和狗爹简直是仙品啊啊啊")

    class S(_S):
        demand_seed_words = "狗爹和小桃"

    monkeypatch.setattr("app.services.douyin_protocol_source.search", lambda *a, **k: [])
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {
        "error": "", "names": {}, "ones": 1, "no_name": 34, "total": 60, "dup_ratio": 0.52})
    sent = []
    monkeypatch.setattr(dw, "push_request",
                        lambda items, settings: sent.append(list(items)) or True)

    dw.run_user(session, 1, S())

    assert sent and sent[0][0]["name"] == "狗爹和小桃"


def test_求1型卡片名字为空时用视频标题顶上():
    """★ 2026-11-18:取不到名字时**不许**整卡都是一模一样的「(标题里没名字)」。

    实测台账 `section='douyin'` 30 行里 24 行 title 为空 —— 管理群看到的一张卡上
    好几行长得一模一样,分不出是哪条视频;而视频标题本来就在 `desc` 里。
    """
    card = str(dw.build_request_card([
        {"name": "", "desc": "扣1看《反差直男》", "url": "https://www.douyin.com/video/1",
         "demand": 3, "ones": 9, "total": 30, "author": "某号"},
        {"name": "", "desc": "", "url": "https://www.douyin.com/video/2",
         "demand": 2, "ones": 5, "total": 21, "author": "某号"},
    ]))
    assert "扣1看《反差直男》" in card, "名字为空时要用视频标题顶上"
    assert card.count("(标题里没名字)") == 1, "只有连标题都没有的那条才写占位符"
    assert "[原视频](https://www.douyin.com/video/1)" in card, "名字即标题时仍要能点开原视频"
    assert "[原视频](https://www.douyin.com/video/2)" in card


def test_没有书名号时取最像名字的标签_通用词挡掉():
    assert dw.best_tag("#覆面系 #高壮男 #狗爹和小桃") == "狗爹和小桃"
    assert dw.best_tag("#反差 #覆面 #高壮男") == ""
    #: 作者自己也是标签时不能当名字(实测用户的样本里最长标签 `#阿玛特拉斯` 就是作者)
    assert dw.best_tag("智慧蛊回来了# 范小勤 # 范马宇宙 # 阿玛特拉斯",
                       exclude="阿玛特拉斯") == "范马宇宙"


# ---------------------------------------------------------------- 最新的大瓜

def _latest_cfg():
    class S(_S):
        demand_seed_words = "狗爹和小桃"
        demand_latest_days = 3
        demand_latest_authors = 1
        demand_latest_limit = 5
    return S()


def test_最新大瓜_顺参考作品找同款账号_只要最近几天的(session, monkeypatch):
    import time as _t

    now = int(_t.time())
    monkeypatch.setattr("app.services.douyin_protocol_source.search", lambda *a, **k: [
        {"uid": "sec1", "name": "某号", "aweme_id": "1", "url": "u", "snippet": "x"}])
    monkeypatch.setattr(dw, "fetch_author_posts", lambda sec, **k: {"ok": True, "error": "", "items": [
        {"aweme_id": "9", "desc": "最新一条 狗爹和小桃", "publish_at": now,
         "author": "某号", "url": "https://www.douyin.com/video/9"},
        {"aweme_id": "8", "desc": "十天前 狗爹和小桃", "publish_at": now - 10 * 86400,
         "author": "某号", "url": "https://www.douyin.com/video/8"},
    ]})

    items = dw.discover_latest(session, 1, _latest_cfg())

    assert [i["aweme_id"] for i in items] == ["9"]


def test_最新大瓜卡片带时间作者与链接():
    import time as _t

    card = dw.build_latest_card([{"aweme_id": "9", "desc": "新瓜",
                                  "publish_at": int(_t.time()), "author": "某号",
                                  "url": "https://www.douyin.com/video/9"}])
    body = str(card)
    assert "最新大瓜" in body and "某号" in body
    assert "https://www.douyin.com/video/9" in body


# ---------------------------------------------------------------- 发现新名字

def test_同款作者的新作品也进候选池(session, monkeypatch):
    """用户口径「根据这些来找方法去发现新的大瓜」:参考作品 → 作者 → 他们的新作品。"""
    import time as _t

    now = int(_t.time())

    class S(_S):
        demand_seed_words = "小小绘"
        demand_max_videos = 2
        demand_latest_days = 3

    monkeypatch.setattr("app.services.douyin_protocol_source.search", lambda *a, **k: [
        {"uid": "sec1", "name": "作者A", "aweme_id": "aaa", "url": "u",
         "snippet": "#小小绘", "metrics": {"comment_count": 100}}])
    monkeypatch.setattr(dw, "fetch_author_posts", lambda *a, **k: {"ok": True, "error": "", "items": [
        {"aweme_id": "bbb", "desc": "新作品 #新名字", "publish_at": now,
         "author": "作者A", "url": "https://www.douyin.com/video/bbb"}]})

    vids = dw.source_videos(session, 1, S())

    ids = [v["aweme_id"] for v in vids]
    assert "bbb" in ids, ids


def test_发现新名字_要同款作者多条作品都出现(session, monkeypatch):
    """判据:一个名字在他**多条**作品里反复出现;只出现一次的多半是描述词。"""
    class S(_S):
        demand_seed_words = "小小绘"
        demand_expand_min = 2
        demand_expand_limit = 10

    monkeypatch.setattr("app.services.douyin_protocol_source.search",
                        lambda *a, **k: [{"uid": "sec1", "name": "作者A", "aweme_id": "1"}])
    monkeypatch.setattr(dw, "fetch_author_posts", lambda *a, **k: {"ok": True, "error": "", "items": [
        {"aweme_id": "1", "desc": "合集 #小小绘 #覆面系", "publish_at": 1, "author": "作者A", "url": "u1"},
        {"aweme_id": "2", "desc": "又一条 #小小绘 #新名字", "publish_at": 2, "author": "作者A", "url": "u2"},
        {"aweme_id": "3", "desc": "第三条 #新名字", "publish_at": 3, "author": "作者A", "url": "u3"},
    ]})

    out = dw.discover_names(session, 1, S())
    names = {x["name"] for x in out}

    assert names == {"新名字"}, names          # 小小绘是参考作品本身,不该再报
    #: `#覆面系` 是通用词、只出现一次,不该进来
    assert "覆面系" not in names


def test_新名字卡片列出出现次数():
    card = dw.build_expand_card([{"name": "新名字", "hits": 3, "author": "作者A", "url": "u"}])
    assert "新名字" in str(card) and "3" in str(card)


# ---------------------------------------------------------------- 主题闸门

@pytest.mark.parametrize("title,ok", [
    ("英语学习和日常聊天", False),        # 纯社交群
    ("建了一个扩列群扫码可进", False),
    ("失业再出发，建了个群大家互相交流", False),
    #: ★★ 2026-11-25 改判:这两条原来都是 True(「主题词优先」,标题里带「瓜/八卦」
    #: 就直接放行)。实测近 30 天 `topic_ok=1` 且 flagged 且有名字的 59 条里 25 条
    #: 就是这种**群招募帖** —— 「想进群」比「在聊瓜」更能说明这条帖的性质。
    ("刚创了一个八卦群，想进就进", False),
    ("所有人，瓜主亲友还是吃瓜人，来进群吧", False),
    #: ★ 实测里真实推出去的那几条(全带「瓜/八卦」、全是群公告)现在必须挡住:
    ("八卦群🍉群公告", False),
    ("吃瓜八卦群🍉群公告", False),
    ("像这种的吃瓜群谁要？", False),
    ("谁懂有个女生八卦群的快乐！！！！", False),
    ("又想进吃🍉大队的么", False),        # 用「大队」回避「群」—— 已补进社交词
    ("互助（还有几个席位）", False),
    ("招募：互帮互助，一起成长", False),
    ("仅限姐妹！", False),
    ("大学副业兼职，找工作", False),
    ("护肤教程（毛孔篇）纯享版教程", True),  # 既无主题也无社交词 ⇒ 先算相关
    #: ★★ 2026-11-08:小红书**为避审核把「群」写成「裙」** —— 实测这条被判成
    #: 主题相关(topic_ok=1)并推了出去,是一条纯"招男生进群"的社交帖。
    ("完全女儿国来的，裙里女生超多，急招男生！", False),
    ("已婚搭子！摸鱼搭子！聊天搭子！快来！", False),
])
def test_主题相关判定(title, ok):
    assert dw.topic_hit(title) is ok


def _xhs_stub(monkeypatch, notes, texts, has_group=False):
    """把 CDP 那条路整个替掉:不出网,只喂固定的笔记与评论。"""
    from app.services import xhs_demand as X

    class _P:
        def stop(self):
            pass

    class _Ctx:
        def new_page(self):
            class _Page:
                def close(self):
                    pass
            return _Page()

    class _Browser:
        contexts = [_Ctx()]

        def close(self):
            pass

    monkeypatch.setattr(X, "_connect", lambda: (_P(), _Browser()))
    monkeypatch.setattr(X, "available", lambda settings=None: (True, ""))
    monkeypatch.setattr(X, "search_notes", lambda page, kw, limit=2: notes)
    monkeypatch.setattr(X, "note_comments", lambda page, nid, token="": texts)
    monkeypatch.setattr(X, "page_has_group", lambda page: has_group)
    monkeypatch.setattr(dw, "push_request", lambda items, settings, platform="douyin": True)


class _XS(_S):
    demand_xhs_enabled = True
    demand_xhs_seed_words = "吃瓜群"
    demand_xhs_seeds_per_round = 1
    demand_xhs_notes_per_seed = 5
    demand_xhs_min_comments = 8
    demand_request_density = 0.1
    demand_repeat_ratio = 0.25


def _notes(*titles):
    return [{"id": f"n{i}", "token": "t", "title": t, "author": "a", "comments": 30}
            for i, t in enumerate(titles)]


def test_小红书纯社交群不推送_但仍入库当分母(session, monkeypatch):
    """2026-11-08 实测:近 14 条推出去的小红书卡片里,`互帮互助群`/`摸鱼群招人啦`/
    `90.00后05后宝妈孕妈群` 这类**社交招募帖**占了大半 —— 它们求/1 密度一样高
    (拉群也让人扣1),但**不是大瓜**。

    改为:照旧落 `demand_scans`(阈值要靠这些数据调),但**不推送**。
    """
    from app.db.models import DemandScan

    _xhs_stub(monkeypatch, _notes("摸鱼群招人啦"), ["1"] * 30)
    pushed: list = []
    monkeypatch.setattr(dw, "push_request",
                        lambda items, settings, platform="douyin": pushed.append(items) or True)

    res = dw.scan_xhs(session, 1, _XS())

    assert res["flagged"] == 1 and res["pushed"] == 0, res
    assert res["skipped"] == 1, "被闸门拦下的条数必须单独计数,否则'没命中'和'拦掉了'分不清"
    assert session.query(DemandScan).count() == 1, "分母要留"


def test_小红书取不到名字的也不推送_与抖音不同(session, monkeypatch):
    """抖音那条链**取不到名字仍然推**(用户要的是"这类视频"本身);

    小红书不同:它的评论区「求/1」信号本来就弱一个量级(`doc` 9.5),
    再推一张"没名字"的卡就纯是噪音 —— 而且实测推出去的多是空串名。
    """
    _xhs_stub(monkeypatch, _notes("这次真的要替我们猫一样的文吉儿说两句话了"), ["1"] * 30)
    pushed: list = []
    monkeypatch.setattr(dw, "push_request",
                        lambda items, settings, platform="douyin": pushed.append(items) or True)

    res = dw.scan_xhs(session, 1, _XS())

    assert res["flagged"] == 1 and res["skipped"] == 1 and not pushed, res


def test_小红书主题相关且取得到名字的照常推送(session, monkeypatch):
    """闸门只拦"社交群/没名字",**不许把真大瓜一起拦掉**。"""
    _xhs_stub(monkeypatch, _notes("《小小绘》全集资源"), ["1"] * 30)
    pushed: list = []
    monkeypatch.setattr(dw, "push_request",
                        lambda items, settings, platform="douyin": pushed.append(items) or True)

    res = dw.scan_xhs(session, 1, _XS())

    assert res["pushed"] == 1 and res["skipped"] == 0, res
    assert pushed[0][0]["name"] == "小小绘"


# ---------------------------------------------------------------- 数据回传管理群

def test_每条扫过的视频都落一行_不管命不命中(session, monkeypatch):
    """用户口径「先收集数据…再根据数据改策略」:没有分母,阈值只能靠猜。"""
    from app.db.models import DemandScan

    _lead(session, "111", title="《甲》")
    _lead(session, "222", title="《乙》")
    calls = {"n": 0}

    def fake_collect(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:      # 命中
            return {"error": "", "names": {}, "ones": 6, "no_name": 1, "total": 20,
                    "dup_ratio": 0.4}
        return {"error": "", "names": {}, "ones": 0, "no_name": 0, "total": 20,
                "dup_ratio": 0.05}

    monkeypatch.setattr(dw, "collect_for_video", fake_collect)
    monkeypatch.setattr(dw, "push_request", lambda items, settings: True)

    dw.run_user(session, 1, _S())

    rows = session.query(DemandScan).all()
    assert len(rows) == 2
    assert sum(1 for r in rows if r.flagged) == 1


def test_汇总给出分母与命中率(session):
    from app.db.models import DemandScan

    now = datetime.now()
    session.add_all([
        DemandScan(user_id=1, aweme_id="1", scanned_date="2026-10-30", total=20, ones=6,
                   demand=1, dup_ratio=0.4, flagged=True, why="求/1密度", scanned_at=now),
        DemandScan(user_id=1, aweme_id="2", scanned_date="2026-10-30", total=20, ones=0,
                   demand=0, dup_ratio=0.05, flagged=False, scanned_at=now),
    ])
    session.commit()

    st = dw.digest_stats(session, 1, hours=24)

    assert st["scanned"] == 2 and st["flagged"] == 1
    assert abs(st["rate"] - 0.5) < 1e-6
    card = str(dw.build_digest_card(st))
    assert "扫描" in card and "求1型" in card


# ---------------------------------------------------------------- 入口

def _lead(session, aweme_id="111", title="求《狗爹和小桃》全集", found_at=None):
    from app.db.models import DouyinLead

    session.add(DouyinLead(user_id=1, aweme_id=aweme_id, title=title, author="a",
                           url=f"https://www.douyin.com/video/{aweme_id}",
                           found_at=found_at or datetime.now(), found_date="2026-10-30"))
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


def test_热点名额不被库内线索吃光(session, monkeypatch):
    """★ **库内素材永远是满的,热点名额必须预留**(2026-11-13 实测的断供)。

    改了之前:近 7 天 332 条线索 ⇒ ② 一填满就 `return`,③④ 一次都轮不到 ——
    40 轮运行记录全是 `视频4 扫4`,`hot_word_seen` 停在 11-11、
    日志里「热点差分」**零次出现**;而探针词的另一个消费者
    (`douyin_leads.unfed_search_words`)读的就是那张表 ⇒ **两条路同时断供**。
    """
    for i in range(10):                       # 库内素材远多于名额
        _lead(session, f"lead{i:03d}")

    class _S3(_S):
        demand_max_videos = 4
        demand_hot_videos = 1                 #: 留 1 个给热点

    from app.services import hot_cycle

    asked: list[list[str]] = []

    def _fake_new_hot_words(settings, seeds, *, session=None, user_id=0, limit=3, **k):
        asked.append(list(seeds))
        return [hot_cycle.HotWord(word="新热点词", last=1, peak=200000,
                                  days=3, emerge=0, source="形态词:后续")]

    monkeypatch.setattr(hot_cycle, "new_hot_words", _fake_new_hot_words)
    monkeypatch.setattr("app.services.douyin_protocol_source.search",
                        lambda words, **k: [{
                            "aweme_id": "hot1", "url": "https://x/hot1",
                            "snippet": "某事件后续", "name": "a",
                            "metrics": {"comment_count": 500}}])

    vids = dw.source_videos(session, 1, _S3())

    assert asked, "热点取词根本没被调用 —— 名额又被库内线索吃光了"
    ids = [v["aweme_id"] for v in vids]
    assert "hot1" in ids, f"热点视频没进来:{ids}"
    assert len(ids) == 4, f"总名额应当是 4:{ids}"
    assert ids[-1] == "hot1", "热点应排在库内素材之后(顺序口径不变)"
    assert [v["src"] for v in vids].count("hot") == 1, "热点来源没打标,运行记录就数不出来"


def test_对标作品那一步不许多吃一条_否则热点名额永远为0(session, monkeypatch):
    """★★ 2026-11-17 生产实测定位到的真断供点。

    原写法在 ②③ 两个循环里都是**先 `_add` 再 `break`** ——
    进入循环时 `len(out)` 已经等于 `lib_need`,仍会**多塞一条**。
    于是 ② 填满 3 条后 ③ 又加第 4 条 ⇒ `need - len(out) == 0` ⇒
    **④ 热点段恒被跳过**:`demand_hot_videos=1` 而 `hot_budget` 算出 0,
    实测 `hot_word_seen` 停在 11-11、日志里「热点差分」零次出现。

    ⚠️ 原来的测试只造了线索(`douyin_leads`)、没造对标作品(`douyin_peer_works`),
    所以 ② 自己填满就停、**③ 那条多塞的没被触发** —— 测试是绿的,生产是断的。
    这个用例把 ③ 也填上,才守得住。
    """
    from app.db.models import DouyinPeerWork

    for i in range(3):                        # ② 正好填满 lib_need=3
        _lead(session, f"lead{i:03d}")
    for i in range(5):                        # ③ 有大量候选,旧写法必然多塞一条
        session.add(DouyinPeerWork(
            user_id=1, aweme_id=f"peer{i:03d}", account_uid=f"u{i}",
            account_name="对标号", title="某个资源", url=f"https://x/peer{i:03d}",
            first_seen_at=datetime.now()))
    session.commit()

    class _S3(_S):
        demand_max_videos = 4
        demand_hot_videos = 1                 #: 留 1 个给热点

    from app.services import hot_cycle

    limits: list[int] = []

    def _fake_new_hot_words(settings, seeds, *, session=None, user_id=0, limit=3, **k):
        limits.append(limit)
        return [hot_cycle.HotWord(word="新热点词", last=1, peak=200000,
                                  days=3, emerge=0, source="形态词:后续")]

    monkeypatch.setattr(hot_cycle, "new_hot_words", _fake_new_hot_words)
    monkeypatch.setattr("app.services.douyin_protocol_source.search", lambda *a, **k: [
        {"aweme_id": "hot1", "url": "https://x/hot1", "snippet": "某事件后续",
         "name": "a", "metrics": {"comment_count": 500}}])

    vids = dw.source_videos(session, 1, _S3())

    assert limits == [1], f"热点名额被吃掉了(limit={limits})—— 库内那两步又多塞了"
    ids = [v["aweme_id"] for v in vids]
    assert "hot1" in ids, f"热点视频没进来:{ids}"
    assert len(ids) == 4, f"总名额应当是 4,实际 {len(ids)}:{ids}"


def test_运行记录写出热点条数_否则看不出这条路有没有跑(session, monkeypatch):
    """`视频4 扫4` 在"全来自库内"和"3 库内 + 1 热点"两种情况下长得一模一样。"""
    from app.db.models import RunRecord

    _lead(session, "111")

    class _S3(_S):
        demand_max_videos = 2
        demand_hot_videos = 1

    from app.services import hot_cycle

    monkeypatch.setattr(hot_cycle, "new_hot_words", lambda *a, **k: [
        hot_cycle.HotWord(word="新热点词", last=1, peak=200000,
                          days=3, emerge=0, source="形态词:后续")])
    monkeypatch.setattr("app.services.douyin_protocol_source.search", lambda *a, **k: [
        {"aweme_id": "hot1", "url": "u", "snippet": "某事件后续",
         "metrics": {"comment_count": 500}}])
    monkeypatch.setattr(dw, "collect_for_video",
                        lambda *a, **k: {"error": "", "names": {}})

    dw.run_user(session, 1, _S3())
    session.flush()
    detail = session.query(RunRecord).filter_by(kind="demand_watch").one().detail
    assert "其中热点1" in detail, detail


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
    _lead(session, "111", title="一条没有书名号的普通视频")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {"error": "", "names": {}})
    monkeypatch.setattr(dw, "push_card", lambda rows, settings: pytest.fail("不该推空卡"))

    out = dw.run_user(session, 1, _S())

    assert out["names"] == 0
    assert session.query(DemandName).count() == 0


@pytest.mark.parametrize("name,keep", [
    ("偷偷跑去女厕所过", False),
    ("谈过恋爱", False),
    ("直接买", False),
    ("可换", False),
    ("愿意花米", False),
    ("主包用的柠胶", False),
    ("反差直男", True),
    ("仙人指路", True),
    ("狗爹和小桃", True),
])
def test_动词短语不算名字(name, keep):
    """2026-11-02 数据:16 个名字里 11 个是从评论里截出来的**动词短语**。

    真名字很少以「直接/愿意/偷偷/可/谈过」开头,也很少以「过/买/换」结尾。
    """
    assert dw.is_name_like(name) is keep


def test_单条评论里的偶发片段不入库(session, monkeypatch):
    """2026-11-04 数据:名字表 20 条里大半是评论片段(`抖音号`/`过人`/`有偿`/`去汉中路`…),
    共同点是**只在一个视频的评论里出现过一次**。规则:标题来的留、≥2 视频留、被求≥2 次留。
    """
    _lead(session, "111", title="一条没有书名号的普通视频")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {
        "error": "", "names": {
            "偶发片段": {"demand": 1, "mention": 1, "samples": ["求个偶发片段"]},
            "被求两次": {"demand": 2, "mention": 2, "samples": ["求被求两次", "求被求两次"]},
        }, "ones": 0, "no_name": 0, "total": 20, "dup_ratio": 0.0})
    monkeypatch.setattr(dw, "push_request", lambda *a, **k: True)
    monkeypatch.setattr(dw, "push_card", lambda *a, **k: True)

    dw.run_user(session, 1, _S())

    names = {r.name for r in session.query(DemandName).all()}
    assert "被求两次" in names
    assert "偶发片段" not in names


def test_和网盘资源库对得上的名字要留(session, monkeypatch):
    """★ 2026-11-04 用户:「可以结合多个平台和网盘资源来判断」。

    评论里抽到的名字哪怕只出现一次,只要**网盘资源库/线索名/别平台需求名**里也有,
    就不是偶发片段 —— 留。
    """
    from app.db.models import XunleiResource

    session.add(XunleiResource(user_id=1, fid="f1", name="反差直男", parent_name="男频"))
    session.commit()
    _lead(session, "111", title="一条没有书名号的普通视频")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {
        "error": "", "names": {
            "反差直男": {"demand": 0, "mention": 1, "samples": ["这个是反差直男吧"]},
        }, "ones": 0, "no_name": 0, "total": 20, "dup_ratio": 0.0})
    monkeypatch.setattr(dw, "push_request", lambda *a, **k: True)
    monkeypatch.setattr(dw, "push_card", lambda *a, **k: True)

    dw.run_user(session, 1, _S())

    assert {r.name for r in session.query(DemandName).all()} == {"反差直男"}


def test_大瓜候选名只收作品名_群名不要(session):
    """2026-11-04:小红书挂群笔记的标题多是**群名**(`欢迎所有爱吃瓜…`/`互助`/`急招男生`),
    拿去抖音搜一条都对不上 ⇒ 只收像作品名的。
    """
    from datetime import datetime

    from app.db.models import DemandScan

    now = datetime.now()
    session.add_all([
        DemandScan(user_id=1, platform="xiaohongshu", aweme_id="n1", scanned_date="2026-11-04",
                   title="欢迎所有爱吃瓜的小伙伴加入", name="欢迎所有爱吃瓜的小伙伴加入",
                   has_group=True, flagged=True, total=20, scanned_at=now),
        DemandScan(user_id=1, platform="xiaohongshu", aweme_id="n2", scanned_date="2026-11-04",
                   title="《反差直男》番外", name="反差直男",
                   has_group=True, flagged=True, total=20, scanned_at=now),
    ])
    session.commit()

    assert dw.guagua_candidates(session, 1, top=5) == ["反差直男"]


def test_迅雷群里有同名资源要标出来(session):
    """用户:「迅雷群里面是否有人发这些资源,因为这种也都会发网盘」。"""
    from datetime import datetime

    from app.db.models import XunleiGroupShare

    session.add(XunleiGroupShare(user_id=1, group_id="g", share_id="s1",
                                 title="【反差直男】全套资源", msg_time=datetime.now()))
    session.add(XunleiGroupShare(user_id=1, group_id="g", share_id="s2",
                                 title="别的剧", msg_time=datetime.now()))
    session.commit()

    assert dw.xunlei_group_hits(session, "反差直男") == 1
    assert dw.xunlei_group_hits(session, "不存在的名字") == 0


# ---------------------------------------------------------------- 评论区高频实体
#
# 2026-11-06 实测发现:观众**不写书名号、也不说「求 XX」**,他们直接**说名字**。
# 例:「水逆塌方」那条的 39 条评论里 `水子哥` 出现在 14 条,原抽取器抓到 **0 个**。

def test_高频实体_观众直接说名字也能捞出来():
    """真实评论原文(`水子哥` 那条视频,截取代表样本)。"""
    texts = [
        "水子哥略显青涩", "我有水子哥了", "声音像水子哥", "水子哥真下海了",
        "水子哥这颜值", "水子哥是谁啊", "第一次看水子哥",
    ]
    ents = dw.frequent_entities(texts, min_docs=3)
    assert any(e.text == "水子哥" for e in ents), ents


def test_高频实体按评论去重_同一条刷一百遍只算一次():
    """防刷屏:同一句复制 100 次不能让它的 DF 变成 100。"""
    texts = ["水子哥好帅"] * 100
    ents = dw.frequent_entities(texts, min_docs=1)
    assert [e.df for e in ents if e.text == "水子哥"] == [1]
    assert all(e.df <= 1 for e in ents), "所有片段的 DF 都不该超过评论条数"


def test_边界率_句中切出的碎片低_名字高():
    """`水子哥` 常常整条评论就是它(边界出现);`个面具` 只出现在句中。"""
    texts = ["水子哥", "水子哥", "我有水子哥了", "水子哥真下海了",
             "他戴个面具出镜", "她戴个面具直播"]
    by = {e.text: e for e in dw.frequent_entities(texts, min_docs=2)}
    assert by["水子哥"].edge > by["个面具"].edge
    assert by["水子哥"].edge >= dw._ENTITY_EDGE_MIN


def test_虚字开头或结尾的片段不算名字():
    """实测残留:`潘甜甜是`(DF 3,edge 1.0)/`是谁`/`不烧心` —— 边界率过关但起止是虚词。"""
    cands = [dw.Entity("潘甜甜是", 3, 1.0), dw.Entity("是谁", 3, 1.0),
             dw.Entity("潘甜甜", 11, 0.727)]
    assert [e.text for e in dw._entity_names(cands)] == ["潘甜甜"]


def test_碎片只在前后缀上被判为半截_简称要留():
    """`林予`/`林予曦`:前者是全名的前缀 ⇒ 丢;`甜甜`/`潘甜甜`:中间包含 ⇒ 两个都留。"""
    cands = [dw.Entity("林予曦", 15, 0.867), dw.Entity("林予", 16, 0.812),
             dw.Entity("潘甜甜", 11, 0.727), dw.Entity("甜甜", 18, 0.222)]
    kept = {e.text for e in dw._drop_subsumed(cands)}
    assert "林予" not in kept and "林予曦" in kept
    assert {"甜甜", "潘甜甜"} <= kept


def test_标题里出现的片段不算_那是视频本身在讲什么():
    """实测 `古见`(标题「猫系古见同学」)上榜 —— 评论区在讨论剧情,不是新资源名。"""
    cands = [dw.Entity("古见", 7, 0.5), dw.Entity("水子哥", 4, 0.5)]
    names = [e.text for e in dw._entity_names(cands, title="猫系古见同学！")]
    assert names == ["水子哥"]


def test_泛词仍然要靠词表挡_边界率挡不住它们():
    """`电影` 在评论区整句就是「什么电影」⇒ 边界率 1.0,属于必要不充分条件。"""
    cands = [dw.Entity("电影", 7, 1.0), dw.Entity("水子哥", 14, 0.56)]
    assert [e.text for e in dw._entity_names(cands)] == ["水子哥"]


def test_重复名够多也能推_不需要有人求(session):
    """`水子哥`×9 观众根本没在「求」,是在议论这个人 —— demand/ones 恒为 0。"""
    r = DemandName(user_id=1, platform="douyin", name="水子哥",
                   demand_hits=0, mention_hits=9, last_seen_at=datetime.now())
    session.add(r)
    session.commit()

    chosen = dw.pick_pushable(session.query(DemandName).all(),
                              min_demand=2, min_ones=20, min_mention=6,
                              cooldown_hours=48, limit=10)
    assert [x.name for x in chosen] == ["水子哥"]


def test_重复3次还不够_那通常还是短语(session):
    r = DemandName(user_id=1, platform="douyin", name="好可爱",
                   demand_hits=0, mention_hits=3, last_seen_at=datetime.now())
    session.add(r)
    session.commit()

    assert dw.pick_pushable(session.query(DemandName).all(),
                           min_demand=2, min_ones=20, min_mention=6,
                           cooldown_hours=48, limit=10) == []


def test_纯重复词跨多个视频反而是泛词证据(session, monkeypatch):
    """实测 `电影` 出现在 4 个视频、`什么` 7 个;而真名字**只在一个视频**里刷屏。"""
    _lead(session, "111", title="视频一")
    _lead(session, "222", title="视频二")
    collect = {
        "111": {"error": "", "names": {"电影": {"demand": 0, "mention": 5, "samples": [],
                                                "entity": True}}, "ones": 0, "no_name": 0,
                "total": 20, "dup_ratio": 0.0},
        "222": {"error": "", "names": {"电影": {"demand": 0, "mention": 5, "samples": [],
                                                "entity": True}}, "ones": 0, "no_name": 0,
                "total": 20, "dup_ratio": 0.0},
    }
    monkeypatch.setattr(dw, "collect_for_video", lambda vid, **k: collect[vid])
    monkeypatch.setattr(dw, "push_card", lambda *a, **k: False)
    monkeypatch.setattr(dw, "push_request", lambda *a, **k: False)

    dw.run_user(session, 1, _S())

    assert session.query(DemandName).count() == 0


def test_只在一个视频里刷屏的真名字入库(session, monkeypatch):
    _lead(session, "111", title="我超，啊水逆咋塌方了啊")
    monkeypatch.setattr(dw, "collect_for_video", lambda vid, **k: {
        "error": "", "names": {"水子哥": {"demand": 0, "mention": 14, "samples": [],
                                        "entity": True}}, "ones": 0, "no_name": 0,
        "total": 39, "dup_ratio": 0.0})
    monkeypatch.setattr(dw, "push_card", lambda *a, **k: False)
    monkeypatch.setattr(dw, "push_request", lambda *a, **k: False)

    dw.run_user(session, 1, _S())

    row = session.query(DemandName).one()
    assert row.name == "水子哥"


def test_同一名字既有句式又有重复时不算纯重复词(session, monkeypatch):
    """只要有一条视频里它是靠书名号/「求 X」抽到的,`entity_only` 就该翻成 False。"""
    _lead(session, "111", title="视频一")
    _lead(session, "222", title="视频二")
    collect = {
        "111": {"error": "", "names": {"反差直男": {"demand": 2, "mention": 2,
                                                    "samples": []}}, "ones": 0,
                "no_name": 0, "total": 20, "dup_ratio": 0.0},
        "222": {"error": "", "names": {"反差直男": {"demand": 0, "mention": 5,
                                                    "samples": [], "entity": True}},
                "ones": 0, "no_name": 0, "total": 20, "dup_ratio": 0.0},
    }
    monkeypatch.setattr(dw, "collect_for_video", lambda vid, **k: collect[vid])
    monkeypatch.setattr(dw, "push_card", lambda *a, **k: False)
    monkeypatch.setattr(dw, "push_request", lambda *a, **k: False)

    dw.run_user(session, 1, _S())

    assert {r.name for r in session.query(DemandName).all()} == {"反差直男"}

# ---------------------------------------------------------------- 公众号事件名(2026-11-17)

def _wa(session, title, author, hours_ago=1.0):
    from app.db.models import WechatArticle

    session.add(WechatArticle(
        user_id=1, author=author, title=title,
        created_at=datetime.now() - timedelta(hours=hours_ago)))
    session.commit()


def test_公众号事件名要抽得出来_大美与高迪(session) -> None:
    """★ 用户 2026-11-17 问「高迪和大美最近就是大瓜是否监控到了」。

    真实标题形态:「深夜大瓜!大美与高迪视频【完整版】」 —— **没有书名号**,
    所以 `names_from_title` 抽不出来(近 7 天 1733 条标题全部返回空)。
    """
    _wa(session, "深夜大瓜!大美与高迪视频【完整版】", "考研外刊阅读", 40)
    _wa(session, "大美与高迪出轨对象摄影师聊天记录曝光,太刺激了", "雪儿5025", 30)
    n = dw.wechat_event_names(session, 1, top=3, hours=48, settings=_S())
    assert n == 1
    row = session.query(DemandName).one()
    assert row.name == "大美与高迪", "载体词(视频/出轨对象…)要被截掉,留下纯实体对"
    assert row.platform == dw.WEVENT_PLATFORM
    assert row.demand_hits == 2, "两个作者 = 两个人在写"
    assert row.mention_hits == 2


def test_只有一个作者写过的事件名不许进榜(session) -> None:
    """单作者的事件名就是"某一篇的标题",不是"在传的瓜" —— 拿它搜是白烧额度。"""
    _wa(session, "深夜大瓜!某某与某人视频完整版", "独此一家", 5)
    assert dw.wechat_event_names(session, 1, top=3, hours=48, settings=_S()) == 0
    assert session.query(DemandName).count() == 0


def test_跨作者但没有吃瓜语义的不许进榜(session) -> None:
    """⚠️ **跨作者只能证明"多人在写",证明不了"这件事是瓜"**。

    实测反例就是这个形状:资料/模板类标题同样是 `X和Y`,而且**也会被多个号反复发**。
    少了吃瓜语义这一道,每轮都会拿资料模板名去烧一次搜索额度。
    """
    _wa(session, "小白与小黑", "秋天5778", 5)
    _wa(session, "小白与小黑", "洋芋光", 6)
    assert dw._wevent_name("小白与小黑") == "小白与小黑", "实体对本身抽得出来"
    assert dw.wechat_event_names(session, 1, top=3, hours=48, settings=_S()) == 0


def test_吃瓜语义在_但只有一个作者_也不进榜(session) -> None:
    """反过来也要验:语义标记**不能替代**跨作者证据。"""
    _wa(session, "深夜大瓜!小白与小黑视频【完整版】", "独此一家", 5)
    assert dw.wechat_event_names(session, 1, top=3, hours=48, settings=_S()) == 0


def test_半句话不许被当成事件名(session) -> None:
    """「相处模式与性格契合点是什么」前 6 字像实体对,截断后必须**仍是纯实体对**。"""
    t = "两人相性卡片测试来了|查看彼此的相处模式与性格契合点是什么"
    assert dw._wevent_name(t) == "", "截断后还剩整句 ⇒ 不认"


def test_公众号事件名的门槛不放行_够格门槛只卡评论区那批(session) -> None:
    """★ 公众号那批的 `demand_hits` 是**作者数**(2~3),拿 `demand_min_mention=6`
    去卡会把 48h 窗口里唯一过闸的瓜整个挡掉;而 `douyin` 那批的门槛必须照旧生效。
    """
    _wa(session, "深夜大瓜!大美与高迪视频【完整版】", "甲", 40)
    _wa(session, "大美与高迪出轨聊天记录曝光", "乙", 30)
    dw.wechat_event_names(session, 1, top=3, hours=48, settings=_S())
    #: douyin 那批:1 次求 / 1 次提及 —— 三个门槛全不够,不许放行
    session.add(DemandName(user_id=1, platform="douyin", name="滥竽充数",
                           demand_hits=1, mention_hits=1,
                           last_seen_at=datetime.now()))
    session.commit()

    got = dw.unfed_demand_names(session, 1, top=5, settings=_S(),
                                platforms=(dw.WEVENT_PLATFORM,))
    assert got == ["大美与高迪"]

    got2 = dw.unfed_demand_names(session, 1, top=5, settings=_S())
    assert got2 == [], "douyin 那批仍要被三条门槛挡住"


def test_公众号事件名打完戳就不再重复取(session) -> None:
    """不显式带 platform 打戳的话,这个事件每轮都会被重新抽出来、重新搜一遍。"""
    _wa(session, "深夜大瓜!大美与高迪视频【完整版】", "甲", 40)
    _wa(session, "大美与高迪出轨聊天记录曝光", "乙", 30)
    dw.wechat_event_names(session, 1, top=3, hours=48, settings=_S())
    words = dw.unfed_demand_names(session, 1, top=3, settings=_S(),
                                  platforms=(dw.WEVENT_PLATFORM,))
    assert words == ["大美与高迪"]
    assert dw.mark_demand_names_fed(session, 1, words,
                                    platforms=(dw.WEVENT_PLATFORM,)) == 1
    session.flush()
    assert dw.unfed_demand_names(session, 1, top=3, settings=_S(),
                                 platforms=(dw.WEVENT_PLATFORM,)) == []


def test_过了窗口的老事件要沉下去(session) -> None:
    """48h 窗口是实测定的(这次瓜 11-14 首篇、11-16 才有第二个作者);
    但再老的事件不该继续占搜索名额。"""
    _wa(session, "深夜大瓜!大美与高迪视频【完整版】", "甲", 80)
    _wa(session, "大美与高迪出轨聊天记录曝光", "乙", 70)
    assert dw.wechat_event_names(session, 1, top=3, hours=48, settings=_S()) == 0


# ------------------------------------------------ 内容发布时间闸(2026-11-24)
#: 用户口径:「我们大瓜监测内容已经能监测到了但是就差新鲜度了有的大瓜已经是很久之前的了」
#: 这一组钉三件事:
#:   ① 判新旧用**作品发布时间**,不是 `found_at`/`first_seen_at`(我们抓到的时刻);
#:   ② 判不了时间**默认不放行**,而且必须计数显形(不许静默当"新鲜");
#:   ③ 卡片上要**看得见时间**,否则客户只能看到「求 12 · 扣1 30」。

def _snow(ts: int) -> str:
    """造一个"发布时间 = ts"的抖音 aweme_id(雪花高位就是 Unix 秒,与真实 id 同构)。"""
    return str((int(ts) << 32) | 0x5A5A)


def _age_s(now_days_ago: float) -> int:
    return int((datetime.now() - timedelta(days=now_days_ago)).timestamp())


class _SA(_S):
    """打开发布时间闸的替身(生产口径:3 天,判不了不放行)。"""
    demand_max_age_days = 3
    demand_source_max_age_days = 3
    demand_age_allow_unknown = False


def test_雪花高位就是发布时间_与_create_time_同量级():
    ts = _age_s(2)
    assert abs(dw.publish_ts_of(_snow(ts)) - ts) <= 1


def test_create_time优先于雪花高位():
    ts = _age_s(1)
    assert dw.publish_ts_of(_snow(ts), declared=1700000000) == 1700000000


def test_位数不对的id不许当时间用():
    """`111` 只有 3 位 —— 拿高位当时间会算出 1970 年前后,那是假新鲜度。"""
    assert dw.publish_ts_of("111") == 0
    assert dw.publish_ts_of("") == 0
    assert dw.publish_ts_of(None) == 0


def test_三年前的老视频进不了素材池(session):
    """实测根因:`金智秀flower` 的 aweme 是 2023-04-10,被推了 9 次。"""
    from app.db.models import DouyinLead

    old = _snow(_age_s(1300))
    session.add(DouyinLead(user_id=1, aweme_id=old, title="《金智秀flower》",
                           author="a", url="https://www.douyin.com/video/" + old,
                           found_at=datetime.now(), found_date="2026-11-24"))
    session.commit()

    stats = {}
    vids = dw.source_videos(session, 1, _SA(), stats=stats)
    assert vids == []
    assert stats["stale"] == 1


def test_近几天的新视频照常进池(session):
    from app.db.models import DouyinLead

    new = _snow(_age_s(1))
    session.add(DouyinLead(user_id=1, aweme_id=new, title="《新瓜》",
                           author="a", url="https://www.douyin.com/video/" + new,
                           found_at=datetime.now(), found_date="2026-11-24"))
    session.commit()

    stats = {}
    vids = dw.source_videos(session, 1, _SA(), stats=stats)
    assert [v["aweme_id"] for v in vids] == [new]
    assert stats.get("stale", 0) == 0


def test_判不了时间默认不放行_而且要计数显形(session):
    """`allow_unknown=False` ⇒ 没有 create_time、也不是 19 位雪花 id 的素材不扫。
    ⚠️ 必须留下 `unknown` 计数 —— 否则"闸门挡了全部"和"今天没素材"长得一样。"""
    from app.db.models import DouyinLead

    session.add(DouyinLead(user_id=1, aweme_id="111", title="《来路不明》",
                           author="a", url="https://www.douyin.com/video/111",
                           found_at=datetime.now(), found_date="2026-11-24"))
    session.commit()

    stats = {}
    vids = dw.source_videos(session, 1, _SA(), stats=stats)
    assert vids == []
    assert stats["unknown"] == 1


def test_同一条老货同时在线索表和对标号表只算一次(session):
    """两表都命中同一条时,`老N` 不许虚高 —— `_add` 里 `seen` 必须**先记**再判新鲜。"""
    from app.db.models import DouyinLead, DouyinPeerWork

    old = _snow(_age_s(500))
    session.add(DouyinLead(user_id=1, aweme_id=old, title="《老货》", author="a",
                           url="u", found_at=datetime.now(), found_date="2026-11-24"))
    session.add(DouyinPeerWork(user_id=1, aweme_id=old, account_uid="x",
                               account_name="甲", title="《老货》", url="u",
                               first_seen_at=datetime.now()))
    session.commit()

    stats = {}
    dw.source_videos(session, 1, _SA(), stats=stats)
    assert stats["stale"] == 1


def test_闸门关掉时无时间也放行_窗口0():
    """`demand_max_age_days=0` = 整道闸关(退回旧行为)。"""
    from app.db.models import DouyinLead

    #: 用一次单独构造的会话,免得和别的用例抢行
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.add(DouyinLead(user_id=1, aweme_id="111", title="《来路不明》", author="a",
                      url="u", found_at=datetime.now(), found_date="2026-11-24"))
    db.commit()

    stats = {}
    vids = dw.source_videos(db, 1, _S(), stats=stats)   #: `_S` 的窗口是 0
    assert [v["aweme_id"] for v in vids] == ["111"]
    assert stats.get("unknown", 0) == 0
    db.close()


def test_run_user把发布时间写进运行记录(session, monkeypatch):
    """拦下的条数不进 `runs` ⇒ 闸门把素材全挡了,看起来和"今天没素材"一模一样。"""
    from app.db.models import DouyinLead
    from app.db.models import RunRecord

    old = _snow(_age_s(900))
    session.add(DouyinLead(user_id=1, aweme_id=old, title="《老货》", author="a",
                           url="u", found_at=datetime.now(), found_date="2026-11-24"))
    session.commit()

    dw.run_user(session, 1, _SA())
    row = session.query(RunRecord).filter(
        RunRecord.kind == "demand_watch").order_by(RunRecord.id.desc()).first()
    assert "拦:老1" in (row.detail or "")


def test_求1型卡片上带发布时间(session):
    """用户投诉的就是"很久之前的",卡片上必须一眼可见。"""
    ts = _age_s(1)
    card = dw.build_request_card([{
        "name": "狗爹和小桃", "desc": "扣1看", "author": "甲", "url": "https://x/1",
        "why": "求/1密度", "demand": 3, "ones": 20, "total": 60, "publish_at": ts}])
    body = card["elements"][0]["text"]["content"]
    assert f"{datetime.fromtimestamp(ts):%m-%d}" in body


def test_求1型卡片没有时间时不留占位符(session):
    """拿不到时间就**不写**,别摆一列「—」(那会被读成别的东西)。"""
    card = dw.build_request_card([{
        "name": "狗爹和小桃", "desc": "", "author": "甲", "url": "https://x/1",
        "why": "求/1密度", "demand": 3, "ones": 20, "total": 60, "publish_at": 0}])
    body = card["elements"][0]["text"]["content"]
    #: 拆行首的「**名字** — 」那个破折号是**分隔符**(每行都有),不是时间占位符。
    time_part = body.split(" · 求/1密度", 1)[1]
    assert "—" not in time_part


# ------------------------------------------------- 名字凉了就不再推(同一族)

def test_三天没被提起的名字不再推(session):
    """实测:`七夕` 11-04 最后一次被见到,11-24 12:50 还在推。"""
    session.add(DemandName(user_id=1, platform="douyin", name="七夕",
                           demand_hits=5, last_seen_at=datetime.now() - timedelta(days=20)))
    session.add(DemandName(user_id=1, platform="douyin", name="在传的",
                           demand_hits=5, last_seen_at=datetime.now() - timedelta(hours=2)))
    session.commit()

    dropped = {}
    chosen = dw.pick_pushable(session.query(DemandName).all(), min_demand=2,
                              cooldown_hours=48, limit=10, max_age_days=3, dropped=dropped)
    assert [r.name for r in chosen] == ["在传的"]
    assert dropped["name_stale"] == 1


def test_名字新鲜度闸关掉时照旧推_窗口0():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(DemandName(user_id=1, platform="douyin", name="七夕", demand_hits=5,
                      last_seen_at=datetime.now() - timedelta(days=20)))
    db.commit()
    assert [r.name for r in dw.pick_pushable(db.query(DemandName).all(), min_demand=2,
                                             cooldown_hours=48, limit=10, max_age_days=0)] \
        == ["七夕"]
    db.close()


# ------------------------------------------------- 生产默认值守卫

def test_生产默认值_发布时间闸是开着的():
    """⚠️ 测试替身把闸关了(假 id 判不了时间);**生产默认必须开着**,否则这次改动等于没做。"""
    from config.settings import Settings

    s = Settings()
    assert s.demand_max_age_days == 7
    assert s.demand_age_allow_unknown is False
    assert s.demand_name_max_age_days == 3
    assert s.demand_xhs_max_age_days == 3


def test_最新大瓜_判不了发布时间的作品不推(session, monkeypatch):
    """⚠️ 原写法 `if it["publish_at"] and ...` 是"判不了就放行" —— 主页接口少给
    `create_time` 时,几个月前的老作品会被当成「最新的大瓜」推出去(用户投诉的就是这张卡)。"""
    class S(_S):
        demand_seed_words = "狗爹和小桃"
        demand_latest_days = 3
        demand_latest_authors = 1
        demand_latest_limit = 5

    monkeypatch.setattr("app.services.douyin_protocol_source.search", lambda *a, **k: [
        {"uid": "sec1", "name": "某号", "aweme_id": "1", "url": "u", "snippet": "x"}])
    monkeypatch.setattr(dw, "fetch_author_posts", lambda sec, **k: {"ok": True, "error": "", "items": [
        #: 主页接口没给 `create_time`,id 也不是雪花(判不了)⇒ 不许推
        {"aweme_id": "9", "desc": "老作品 狗爹和小桃", "publish_at": 0,
         "author": "某号", "url": "https://www.douyin.com/video/9"},
    ]})

    assert dw.discover_latest(session, 1, S()) == []


def test_最新大瓜_只有雪花id也能判新旧(session, monkeypatch):
    """主页接口没给 `create_time` 时,雪花 id 高位就是发布时间 —— 新的照样要推。"""
    import time as _t

    new = _snow(int(_t.time()) - 3600)

    class S(_S):
        demand_seed_words = "狗爹和小桃"
        demand_latest_days = 3
        demand_latest_authors = 1
        demand_latest_limit = 5

    monkeypatch.setattr("app.services.douyin_protocol_source.search", lambda *a, **k: [
        {"uid": "sec1", "name": "某号", "aweme_id": "1", "url": "u", "snippet": "x"}])
    monkeypatch.setattr(dw, "fetch_author_posts", lambda sec, **k: {"ok": True, "error": "", "items": [
        {"aweme_id": new, "desc": "新作品 狗爹和小桃", "publish_at": 0,
         "author": "某号", "url": "https://www.douyin.com/video/" + new},
    ]})

    assert [i["aweme_id"] for i in dw.discover_latest(session, 1, S())] == [new]


# ---------------------------------------------------------------------------
# 素材侧:已扫去重 + 库内保底 + 来源显形(2026-11-24)
# ---------------------------------------------------------------------------


class _SD(_S):
    """打开"已扫去重"的替身(生产 3 天)。"""
    demand_skip_scanned_days = 3


class _SD1(_SD):
    """去重 + **只要 1 条**素材。

    ⚠️ 上面两条用例要验证的是「跳过」和「回填」本身,而 `_SD` 继承来的
    `demand_max_videos = 2` 会让**回填把空出来的名额填满** —— 那是对的行为,
    但会把"跳过是否生效"与"回填取谁"两件事混在一条断言里。钉成 1 条更清楚。
    """
    demand_max_videos = 1


class _SR(_S):
    """打开"库内保底"的替身(生产 2 个名额)。"""
    demand_lib_reserve = 2
    demand_max_videos = 4


def _scanned(session, aweme_id, days_ago=0):
    """造一条"我们今天扫过它"的台账行(去重闸的输入就是这张表)。"""
    from app.db.models import DemandScan

    ts = datetime.now() - timedelta(days=days_ago)
    session.add(DemandScan(user_id=1, platform="douyin", aweme_id=aweme_id,
                           scanned_date=ts.strftime("%Y-%m-%d"),
                           scanned_at=ts))
    session.commit()


def test_近三天扫过的视频不再重复进池(session):
    """实测:近 7 天 114 行扫描只覆盖 82 个视频,28% 是重复,单个最多跨 7 天扫 7 次。"""
    #: 显式给出入库顺序(`ORDER BY found_at DESC`),让「已扫」的那条排最前 ——
    #: 否则三条同秒入库,谁先被取到取决于数据库的物理顺序,用例会飘。
    base = datetime.now()
    _lead(session, "111", found_at=base)
    _lead(session, "222", found_at=base - timedelta(seconds=1))
    _lead(session, "333", found_at=base - timedelta(seconds=2))
    _scanned(session, "111", days_ago=1)

    stats = {}
    vids = dw.source_videos(session, 1, _SD(), stats=stats)

    assert {v["aweme_id"] for v in vids} == {"222", "333"}, "扫过的 111 又进池了"
    assert stats["rescanned"] == 1
    assert stats["refilled"] == 0, "没被跳过的素材够填满名额 ⇒ 不该回填"


def test_超过去重窗口的可以再扫(session):
    """3 天窗口外的不算重复 —— 视频更新慢的时候它还是唯一素材。"""
    _lead(session, "111")
    _scanned(session, "111", days_ago=5)

    vids = dw.source_videos(session, 1, _SD())
    assert [v["aweme_id"] for v in vids] == ["111"]


def test_候选全被跳过时按发布时间回填_不许把素材池饿死(session):
    """去重是为了别每轮扫同一批,**不是**为了把池子扫空。

    ⚠️ 回填必须显形(`回填N`):否则"回填的老素材"与"今天真没新素材"在 `runs` 里
    长得一模一样 —— 本仓母题(静默失败 = 假成功)。
    """
    from app.db.models import DouyinLead

    newer, older = _snow(_age_s(1)), _snow(_age_s(2))
    for vid in (older, newer):
        session.add(DouyinLead(user_id=1, aweme_id=vid, title="《老素材》", author="a",
                               url="u", found_at=datetime.now(), found_date="2026-11-24"))
        _scanned(session, vid, days_ago=1)
    session.commit()

    stats = {}
    vids = dw.source_videos(session, 1, _SD1(), stats=stats)

    assert [v["aweme_id"] for v in vids] == [newer], "回填应当按发布时间新→旧"
    assert stats["refilled"] == 1
    assert stats["rescanned"] == 2


def test_去重开着也不影响从没扫过的素材(session):
    _lead(session, "111")
    stats = {}
    vids = dw.source_videos(session, 1, _SD(), stats=stats)
    assert [v["aweme_id"] for v in vids] == ["111"]
    assert stats["rescanned"] == 0 and stats["refilled"] == 0


def test_库内线索和作者作品都能拿到名额(session, monkeypatch):
    """★★ 2026-11-24 实测的失衡:`①b 同款作者` 把名额填满 ⇒ ②③ **永远 0 个名额**。

    生产:历史 263 行扫描里 260 行来自搜索/热点、`douyin_leads` 只 3 行、
    `douyin_peer_works` **0 行**;而库内近 7 天有 240 条新鲜素材一条没扫过。
    """
    from app.db.models import DouyinPeerWork

    _lead(session, "111")
    session.add(DouyinPeerWork(user_id=1, aweme_id="peer1", account_uid="u1",
                               account_name="对标号", title="《对标》", url="u",
                               first_seen_at=datetime.now()))
    session.commit()

    class S(_SR):
        demand_seed_words = "小小绘"

    monkeypatch.setattr("app.services.douyin_protocol_source.search", lambda *a, **k: [
        {"uid": "sec1", "name": "作者A", "aweme_id": "srch1", "url": "u",
         "snippet": "#小小绘", "metrics": {"comment_count": 100}}])
    monkeypatch.setattr(dw, "fetch_author_posts", lambda *a, **k: {"ok": True, "error": "", "items": [
        {"aweme_id": f"auth{i}", "desc": "新作品 #小小绘", "publish_at": 0,
         "author": "作者A", "url": "u"} for i in range(10)]})

    vids = dw.source_videos(session, 1, S())

    srcs = [v["src"] for v in vids]
    assert "lead" in srcs, f"线索没拿到名额:{srcs}"
    assert "peer" in srcs, f"对标号没拿到名额:{srcs}"
    assert len(vids) == 4, f"总名额应当是 4:{srcs}"


def test_库里没货时保底名额全部让给搜索(session, monkeypatch):
    """保底**只在库内真有候选时才算** —— 否则会把搜索这条路也饿死。"""

    class S(_SR):
        demand_seed_words = "小小绘"

    monkeypatch.setattr("app.services.douyin_protocol_source.search", lambda *a, **k: [
        {"uid": "sec1", "name": "作者A", "aweme_id": "srch1", "url": "u",
         "snippet": "#小小绘", "metrics": {"comment_count": 100}}])
    monkeypatch.setattr(dw, "fetch_author_posts", lambda *a, **k: {"ok": True, "error": "", "items": [
        {"aweme_id": f"auth{i}", "desc": "新作品 #小小绘", "publish_at": 0,
         "author": "作者A", "url": "u"} for i in range(10)]})

    vids = dw.source_videos(session, 1, S())

    assert len(vids) == 4, f"库里没货时应当用满 4 个名额:{[v['aweme_id'] for v in vids]}"
    assert all(v["src"] in ("search", "author") for v in vids)


def test_运行记录写出素材来源_失衡才看得见(session, monkeypatch):
    """★★ 历史 263 行扫描里 260 行来自搜索 —— 这个失衡在 `runs` 里**一个字都没有**,
    只能靠事后 join 两张表反推,所以存在了一个月没人发现。"""
    from app.db.models import RunRecord

    _lead(session, "111")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {"error": "", "names": {}})

    dw.run_user(session, 1, _S())
    session.flush()
    detail = session.query(RunRecord).filter_by(kind="demand_watch").one().detail

    assert "源:" in detail, detail
    assert "线1" in detail, f"线索来源没写出来:{detail}"


def test_运行记录写出已扫去重与回填条数(session, monkeypatch):
    from app.db.models import RunRecord

    _lead(session, "111")
    _scanned(session, "111", days_ago=1)
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {"error": "", "names": {}})

    dw.run_user(session, 1, _SD())
    session.flush()
    detail = session.query(RunRecord).filter_by(kind="demand_watch").one().detail

    assert "已扫1" in detail and "回填1" in detail, detail


def test_生产默认值_去重与保底是开着的():
    """替身把两个闸都关着(为了不改老用例的名额假设),生产默认值必须在这里守。"""
    from config.settings import Settings

    s = Settings(_env_file=None)
    assert s.demand_skip_scanned_days == 3
    assert s.demand_lib_reserve == 2
    assert s.demand_name_per_event == 2


# ---------------------------------------------------------------------------
# 大瓜准入:事件合并 + 碎片剔除 + 新鲜度排序(2026-11-25)
# ---------------------------------------------------------------------------
#
# ★★ 用户原话:「**大瓜还是不能单纯的依赖名字,因为大瓜要追求新鲜度肯定就是未知的名字**」。
# 实测印证:37 个够格且新鲜的名字里只有 1 个能被资源库/线索名印证;
# `狗爹`/`小桃`/`困困狗` **零印证** —— 它们正因为新才没被库收录。
# ⇒ 准入靠**结构信号**(同一个瓜的多个叫法合并),不靠"名字已知"。


def _named(session, name, *, demand=6, mention=0, first_seen_days_ago=0,
           video="", videos=()):
    """造一个够格(被人求过)的名字行,`samples` 里带上它出现在哪条作品上。"""
    import json as _json

    now = datetime.now()
    r = DemandName(user_id=1, platform="douyin", name=name, demand_hits=demand,
                   mention_hits=mention, last_seen_at=now,
                   first_seen_at=now - timedelta(days=first_seen_days_ago),
                   samples=_json.dumps({"comments": [], "video": video,
                                        "videos": list(videos)}))
    session.add(r)
    session.commit()
    return r


def test_同一个瓜的六个叫法只推两个(session):
    """★★ 实测:`狗爹`(提74)/`小桃`(37)/`狗爹小桃`(11)/`换狗爹`(10)/
    `狗爹身材`(9)/`困困狗`(10) —— **全部来自同一条视频** `7710851964850384805`。
    它们不是六个瓜,是同一个瓜的六种叫法;不合并的话一张卡里全是同一件事。"""
    vid = "https://www.douyin.com/video/7710851964850384805"
    _named(session, "狗爹", demand=8, mention=74, video=vid)
    _named(session, "小桃", demand=7, mention=37, video=vid)
    _named(session, "狗爹小桃", demand=6, mention=11, video=vid)
    _named(session, "换狗爹", demand=6, mention=10, video=vid)
    _named(session, "狗爹身材", demand=6, mention=9, video=vid)
    _named(session, "困困狗", demand=6, mention=10, video=vid)

    dropped = {}
    chosen = dw.pick_pushable(session.query(DemandName).all(), min_demand=2,
                              cooldown_hours=0, limit=10, max_age_days=0,
                              per_event=2, dropped=dropped)

    assert len(chosen) == 2, f"同一条视频最多占 2 个名额:{[r.name for r in chosen]}"
    assert {r.name for r in chosen} == {"狗爹", "小桃"}, "组内按提及数取前二"
    #: ★ 拦了多少必须显形,否则「只推 2 个」分不清是没新瓜还是全被合并了。
    assert dropped["name_fragment"] == 3, dropped       # 狗爹小桃 / 换狗爹 / 狗爹身材
    assert dropped["name_event_cap"] == 1, dropped      # 困困狗:不碎片但超员


def test_加料的长碎片被剔除_真名字优先(session):
    """`换狗爹`(10) 含 `狗爹`(74) 且提及更少 ⇒ 是"真名字加动词"的碎片。"""
    vid = "https://www.douyin.com/video/7710851964850384805"
    _named(session, "狗爹", demand=8, mention=74, video=vid)
    _named(session, "换狗爹", demand=6, mention=10, video=vid)

    chosen = dw.pick_pushable(session.query(DemandName).all(), min_demand=2,
                              cooldown_hours=0, limit=10, max_age_days=0, per_event=5)
    assert [r.name for r in chosen] == ["狗爹"]


def test_正式作品名靠书名号保住_不会被提及更多的加料版挤掉(session):
    """★★ 用户举的「麦穗与羊」:正式名 `麦穗与羊`(书名号出处 1 次、提及 5),
    评论里更多人写的是 `完了麦穗与羊`(书名号 0 次、提及 8) ——
    纯看提及数会把**加料版**当真名、把正式作品名当碎片剔掉。"""
    vid = "https://www.douyin.com/video/7706155613260187633"
    official = DemandName(user_id=1, platform="douyin", name="麦穗与羊", demand_hits=0,
                          mention_hits=5, title_hits=1, last_seen_at=datetime.now(),
                          first_seen_at=datetime.now(),
                          samples='{"comments": [], "video": "%s"}' % vid)
    noisy = DemandName(user_id=1, platform="douyin", name="完了麦穗与羊", demand_hits=0,
                       mention_hits=8, title_hits=0, last_seen_at=datetime.now(),
                       first_seen_at=datetime.now(),
                       samples='{"comments": [], "video": "%s"}' % vid)
    session.add_all([official, noisy])
    session.commit()

    dropped = {}
    chosen = dw.pick_pushable(session.query(DemandName).all(), min_demand=2,
                              cooldown_hours=0, limit=10, max_age_days=0,
                              per_event=5, dropped=dropped)

    assert [r.name for r in chosen] == ["麦穗与羊"], "书名号里的是正式名,加料版才是碎片"
    assert dropped["name_fragment"] == 1, dropped


def test_两边都没书名号时仍按提及数判碎片(session):
    """书名号优先只在**两边不同**时生效;都靠评论区重复来的(`狗爹` 那类)照旧按提及数。"""
    vid = "https://www.douyin.com/video/7710851964850384805"
    for name, mention in (("狗爹", 74), ("换狗爹", 10)):
        session.add(DemandName(user_id=1, platform="douyin", name=name, demand_hits=8,
                               mention_hits=mention, title_hits=0,
                               last_seen_at=datetime.now(), first_seen_at=datetime.now(),
                               samples='{"comments": [], "video": "%s"}' % vid))
    session.commit()

    chosen = dw.pick_pushable(session.query(DemandName).all(), min_demand=2,
                              cooldown_hours=0, limit=10, max_age_days=0, per_event=5)
    assert [r.name for r in chosen] == ["狗爹"]


def test_加料版有书名号时正式名不被剔(session):
    """反方向:长名带书名号、短名没有 ⇒ 都留着(`小桃` 与书名号里的 `小桃身材`)。"""
    vid = "https://www.douyin.com/video/7710851964850384805"
    session.add(DemandName(user_id=1, platform="douyin", name="小桃", demand_hits=8,
                           mention_hits=37, title_hits=0, last_seen_at=datetime.now(),
                           first_seen_at=datetime.now(),
                           samples='{"comments": [], "video": "%s"}' % vid))
    session.add(DemandName(user_id=1, platform="douyin", name="小桃身材", demand_hits=8,
                           mention_hits=4, title_hits=1, last_seen_at=datetime.now(),
                           first_seen_at=datetime.now(),
                           samples='{"comments": [], "video": "%s"}' % vid))
    session.commit()

    chosen = dw.pick_pushable(session.query(DemandName).all(), min_demand=2,
                              cooldown_hours=0, limit=10, max_age_days=0, per_event=5)
    assert {r.name for r in chosen} == {"小桃", "小桃身材"}


def test_不同的瓜不会被误并(session):
    """`_event_key` 取不到出处时退回按名字分组 —— 不把两个瓜合成一个。"""
    _named(session, "甲瓜", demand=6, mention=10,
           video="https://www.douyin.com/video/7000000000000000001")
    _named(session, "乙瓜", demand=6, mention=10,
           video="https://www.douyin.com/video/7000000000000000002")

    chosen = dw.pick_pushable(session.query(DemandName).all(), min_demand=2,
                              cooldown_hours=0, limit=10, max_age_days=0, per_event=1)
    assert {r.name for r in chosen} == {"甲瓜", "乙瓜"}


def test_没有出处的名字各自独立_不互相合并(session):
    _named(session, "甲瓜", demand=6, mention=10)
    _named(session, "乙瓜", demand=6, mention=10)

    chosen = dw.pick_pushable(session.query(DemandName).all(), min_demand=2,
                              cooldown_hours=0, limit=10, max_age_days=0, per_event=1)
    assert {r.name for r in chosen} == {"甲瓜", "乙瓜"}


def test_新鲜度优先_老名字不再压新瓜(session):
    """★★ 原排序是累计量降序 ⇒ 实测 top10 里 **7 个**是首见 4~7 天前的老名字
    (`后续` 11-19 / `女主` 11-15 / `红果` 11-18),新瓜被压在后面。"""
    _named(session, "老瓜", demand=40, mention=90, first_seen_days_ago=7)
    _named(session, "新瓜", demand=4, mention=10, first_seen_days_ago=0)

    chosen = dw.pick_pushable(session.query(DemandName).all(), min_demand=2,
                              cooldown_hours=0, limit=1, max_age_days=0)
    assert [r.name for r in chosen] == ["新瓜"]


def test_事件键从samples里认作品id():
    """`video` 与 `videos` 都要认;小红书 `/explore/` 也认。"""
    import json as _json

    class R:
        name = "x"
        samples = _json.dumps({"video": "https://www.douyin.com/video/771"} )

    class R2:
        name = "y"
        samples = _json.dumps({"videos": ["https://www.xiaohongshu.com/explore/abc123"]})

    class R3:
        name = "甲瓜"
        samples = "not json"

    assert dw._event_key(R()) == "771"
    assert dw._event_key(R2()) == "abc123"
    assert dw._event_key(R3()) == "name:甲瓜"


# ---------------------------------------------------------------------------
# 扣1 热度的归属(`ones_hits` 死档,2026-11-25)
# ---------------------------------------------------------------------------


def test_扣1热度归给视频里抽到的所有名字(session, monkeypatch):
    """★★ 修复前 `ones` **只在书名号循环里**累加,而近期标题几乎不带《》 ⇒
    生产库 328 行里 **326 行 `ones_hits` 恒 0**,`demand_min_ones=20` 这条准入
    从来没生效过。现在评论里抽到的名字也要拿到**整条视频**的热度。"""
    _lead(session, "111", title="一条没有书名号的普通视频")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {
        "error": "", "ones": 30, "no_name": 0, "total": 50,
        "names": {"狗爹": {"demand": 4, "mention": 9, "samples": []}}})
    monkeypatch.setattr(dw, "push_card", lambda rows, settings: True)

    dw.run_user(session, 1, _S())

    row = session.query(DemandName).filter_by(name="狗爹").one()
    assert row.ones_hits == 30, "整条视频的扣1 应当归给它"


def test_同名既在标题又在评论里_扣1只算一次(session, monkeypatch):
    """用 `credited` 去重 —— 否则同一个名字会拿到双份热度,`ones` 门槛就失真了。"""
    _lead(session, "111", title="扣1看《狗爹和小桃》合集")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {
        "error": "", "ones": 30, "no_name": 0, "total": 50,
        "names": {"狗爹和小桃": {"demand": 4, "mention": 9, "samples": []}}})
    monkeypatch.setattr(dw, "push_card", lambda rows, settings: True)

    dw.run_user(session, 1, _S())

    row = session.query(DemandName).filter_by(name="狗爹和小桃").one()
    assert row.ones_hits == 30
    assert row.title_hits == 1
    assert row.mention_hits == 10, "提及仍然两条来源都算(1 标题 + 9 评论)"


def test_运行记录写出事件合并与碎片条数(session, monkeypatch):
    """闸门不显形的话,「只推 2 个」分不清是没新瓜还是全被合并了。"""
    from app.db.models import RunRecord

    _lead(session, "7710851964850384805", title="普通视频")
    monkeypatch.setattr(dw, "collect_for_video", lambda *a, **k: {
        "error": "", "ones": 0, "no_name": 0, "total": 0,
        "names": {"狗爹": {"demand": 6, "mention": 74, "samples": []},
                  "换狗爹": {"demand": 6, "mention": 10, "samples": []},
                  "狗爹身材": {"demand": 6, "mention": 9, "samples": []}}})
    monkeypatch.setattr(dw, "push_card", lambda rows, settings: True)

    dw.run_user(session, 1, _S())
    session.flush()
    detail = session.query(RunRecord).filter_by(kind="demand_watch").one().detail

    #: 三个名字都落在同一条视频上(`_lead` 的 url),后两个是 `狗爹` 的加料碎片。
    assert "推1" in detail, detail
    assert "碎片2" in detail, detail
