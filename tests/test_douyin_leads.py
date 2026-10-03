"""抖音推广线索单测(2026-10-02):判据(标题以《》开头)、去重、卡片推送。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")


class _Settings:
    """最小 settings 替身(只覆盖 push_leads 用到的字段)。"""
    feishu_webhook_admin = "https://example.com/hook"
    feishu_webhook = ""
    feishu_webhook_douhot = "https://example.com/douhot"   # 抖音专属群
    feishu_secret = ""
    brand_name = "念飞思雪"                                # 我们自己的品牌词


def test_lead_regex_requires_leading_book_title() -> None:
    """判据:**标题以《…》开头**。句中的《》不算——实测那样会误伤正常内容。"""
    from app.services.douyin_leads import _LEAD_RE

    assert _LEAD_RE.match("《白泽的梦》diplay软件下载教程").group(1) == "白泽的梦"
    assert _LEAD_RE.match("《三岁分享》#diplay车机互联").group(1) == "三岁分享"
    assert not _LEAD_RE.match("不用加盒子，一个车机软件就可以实现无线CarPlay")
    assert not _LEAD_RE.match("终于给我找到《My Dearest》资源了")   # 句中 = 剧名,不是推广标识


def test_find_leads_keeps_only_book_prefix_and_dedupes(monkeypatch) -> None:
    """只留《》开头**且能拿到视频链接**的;同一个视频被多个词命中的去重。"""
    from app.services import douyin_leads as dl
    from app.services import mediacrawler_source as mc

    monkeypatch.setattr(mc, "available", lambda: (True, "ok"))
    monkeypatch.setattr(mc, "crawl", lambda p, ks, timeout=600: [
        {"uid": "u1", "url": "https://www.douyin.com/video/1",
         "snippet": "《白泽的梦》diplay软件下载教程", "keyword": "diplay"},
        {"uid": "u2", "url": "https://www.douyin.com/video/1",              # 同一视频
         "snippet": "《白泽的梦》diplay软件下载教程", "keyword": "carplay"},
        {"uid": "u3", "url": "https://www.douyin.com/video/2",
         "snippet": "不用加盒子，一个软件就能实现无线CarPlay", "keyword": "diplay"},
        {"uid": "u4", "url": "",                                            # 拿不到视频链
         "snippet": "《三岁分享》车机互联", "keyword": "diplay"},
    ])
    leads = dl.find_leads(["diplay"])
    assert len(leads) == 1
    assert leads[0]["mark"] == "白泽的梦"
    assert leads[0]["url"] == "https://www.douyin.com/video/1"


def test_find_leads_takes_bracket_anywhere_in_title(monkeypatch) -> None:
    """《》**嵌在句中**也要收 —— 用户 2026-10-02 给的样本就是这么写的:

        苹果安卓手车互联更新《玩车不求人》新版本…

    并且用户明确纠正过:"账号名称跟关键词并没有特殊关联,只是这个凑巧了" ——
    所以**不拿"《》内容=账号名"当判据**,只把带《》的摘出来,真伪由人判断。
    代价是 `《My Dearest》`(剧名)这类噪音会一起进来,靠排序把强信号放前面。
    """
    from app.services import douyin_leads as dl
    from app.services import mediacrawler_source as mc

    monkeypatch.setattr(mc, "available", lambda: (True, "ok"))
    monkeypatch.setattr(mc, "crawl", lambda p, ks, timeout=600: [
        {"uid": "a", "url": "https://d/1", "name": "玩***人",
         "snippet": "苹果安卓手车互联更新《玩车不求人》新版本", "keyword": "k"},
        {"uid": "b", "url": "https://d/2", "name": "睡***着",
         "snippet": "《白泽的梦》diplay软件下载教程", "keyword": "k"},
    ])
    leads = dl.find_leads(["玩车不求人"])
    assert len(leads) == 2
    assert leads[0]["mark"] == "白泽的梦"          # 开头《》优先级最高,排前面
    assert leads[1]["mark"] == "玩车不求人"        # 句中的排后面,但**要收**


def test_lead_rank_orders_strong_signals_first() -> None:
    """排序:开头《》=0 / 与昵称吻合=1 / 其它=2(只影响先后,不影响收不收)。"""
    from app.services.douyin_leads import _lead_rank

    assert _lead_rank("《白泽的梦》diplay教程", "睡***着") == 0
    assert _lead_rank("更新《玩车不求人》新版本", "玩***人") == 1
    assert _lead_rank("终于找到《My Dearest》资源了", "汶***汝") == 2


def test_find_leads_without_tool_is_noop(monkeypatch) -> None:
    """工具没装/不可用 → **抛 `MediaCrawlerError`**(2026-10-03 改)。

    此前是"返回空列表",于是"扫码没通过/工具没装"与"真的一条都没搜到"完全一样,
    `douyin_leads_tick` 会把它记成 `success(线索0)` —— 而这条链**只在每天 11:00
    无人值守时跑**,失败收不到任何信号(与闲鱼/知乎那次"假成功"同一类)。
    现在冒泡给 tick 记 `failed`。
    """
    from app.services import douyin_leads as dl
    from app.services import mediacrawler_source as mc

    monkeypatch.setattr(mc, "available", lambda: (False, "未安装"))
    monkeypatch.setattr(mc, "crawl", lambda *a, **k: (_ for _ in ()).throw(
        mc.MediaCrawlerError("未安装")))
    with pytest.raises(mc.MediaCrawlerError):
        dl.find_leads(["x"])


def test_push_leads_builds_card_with_video_links(monkeypatch) -> None:
    """卡片里每条线索都要带**可点的视频链接**——那是运营唯一能看到作者的入口。"""
    from app.services import douyin_leads as dl
    from app.services import feishu_client

    sent = {}

    class _C:
        def __init__(self, webhook, secret):
            sent["webhook"] = webhook

        def send_card(self, card):
            sent["card"] = card
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)
    ok = dl.push_leads([{"mark": "白泽的梦", "title": "《白泽的梦》diplay…",
                         "url": "https://www.douyin.com/video/1", "keyword": "diplay"}],
                       _Settings())
    assert ok is True
    # 用户口径:"既然是抖音的来源就推送到抖音群聊里面" → 推**抖音专属群**
    assert sent["webhook"] == "https://example.com/douhot"
    body = str(sent["card"])
    assert "https://www.douyin.com/video/1" in body        # 视频链接必须有(运营看作者的唯一入口)
    assert "白泽的梦" not in body                          # ⚠️ 别人的口令《…》要被抹掉


def test_push_leads_no_webhook_is_noop() -> None:
    """没配 webhook / 没有线索 → 直接返回 False,不报错。"""
    from app.services import douyin_leads as dl

    class _S:
        feishu_webhook_admin = ""
        feishu_webhook = ""
        feishu_secret = ""

    assert dl.push_leads([{"mark": "x", "title": "t", "url": "u", "keyword": ""}], _S()) is False
    assert dl.push_leads([], _Settings()) is False


# ---------------------------------------------------------------- 口令 → 资源(2026-10-02)

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: F401,E402
from app.db.models import User  # noqa: E402
from app.services import douyin_leads as dl  # noqa: E402
from app.services import xunlei_kouling as kk  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="admin", email="a@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _leads(*marks: str) -> list[dict]:
    return [{"mark": m, "title": f"{m}的内容", "url": f"https://douyin/{i}"}
            for i, m in enumerate(marks)]


def test_apply_kouling_transfers_share_and_joins_group(session, monkeypatch) -> None:
    """能解的**自动转存**、指向群组的**加群**、解不出的**不动** —— 三种结果要分得清。"""
    monkeypatch.setattr(kk, "known_koulings", lambda s, u: set())
    monkeypatch.setattr(kk, "resolve", lambda m: {
        "三岁宝库": {"kind": "share", "share_url": "https://pan.xunlei.com/s/A", "group_id": ""},
        "三岁分享": {"kind": "group", "share_url": "", "group_id": "1550069837"},
        "My Dearest": {"kind": "none", "share_url": "", "group_id": ""},
    }[m])
    monkeypatch.setattr(kk, "ingest", lambda s, u, m, settings=None: (
        {"status": "ok", "our_url": "https://pan.xunlei.com/s/OUR?pwd=1"}
        if m == "三岁宝库" else {"status": "deferred"}))

    out = dl.apply_kouling(_leads("三岁宝库", "三岁分享", "My Dearest"), session, 1, _Settings())
    assert out[0]["kouling"]["status"] == "ok" and "OUR" in out[0]["kouling"]["our_url"]
    assert out[1]["kouling"]["kind"] == "group" and out[1]["kouling"]["group_id"] == "1550069837"
    assert out[2]["kouling"] == {"kind": "none"}
    assert "已自动转存" in dl._kouling_line(out[0])
    assert "群组" in dl._kouling_line(out[1])
    assert "未解析出资源" in dl._kouling_line(out[2])


def test_apply_kouling_respects_budget_and_skips_already_done(session, monkeypatch) -> None:
    """转存慢且占盘,所以**每轮限量**;已经搬过的词**不再重复搬**。"""
    monkeypatch.setattr(kk, "known_koulings", lambda s, u: {"甲"})
    monkeypatch.setattr(kk, "resolve",
                        lambda m: {"kind": "share", "share_url": f"s/{m}", "group_id": ""})
    monkeypatch.setattr(kk, "ingest",
                        lambda s, u, m, settings=None: {"status": "ok", "our_url": f"our/{m}"})

    class _S(_Settings):
        douyin_leads_transfer_limit = 1

    out = dl.apply_kouling(_leads("甲", "乙", "丙"), session, 1, _S())
    assert out[0]["kouling"]["status"] == "already"          # 已搬过 → 跳过
    assert out[1]["kouling"]["status"] == "ok"               # 额度内 → 真搬
    assert out[2]["kouling"]["status"] == "over_budget"      # 超额度 → 只解析
    assert "额度用完" in dl._kouling_line(out[2])


def test_apply_kouling_disabled_only_annotates(session, monkeypatch) -> None:
    """关掉自动转存 → 只标注、**完全不碰迅雷**(不解析也不转存)。"""
    def _boom(*a, **k):
        raise AssertionError("关掉开关后不该调用任何迅雷接口")

    monkeypatch.setattr(kk, "resolve", _boom)
    monkeypatch.setattr(kk, "known_koulings", _boom)

    class _S(_Settings):
        douyin_leads_auto_transfer = False

    out = dl.apply_kouling(_leads("三岁宝库"), session, 1, _S())
    assert out[0]["kouling"] == {"kind": "off"}


# ---------------------------------------------------------------- 取词:群组新资源 + 资源库

from datetime import datetime, timedelta  # noqa: E402

from app.db.models import XunleiGroupShare  # noqa: E402


def test_to_search_word_keeps_subject_and_drops_generic() -> None:
    """群标题 → 搜索词:取主体名(丢括号补充与版本号);**泛化合集名整个丢掉**。

    实测样本:群里真是这么写的 —— 整句丢进抖音搜不到,泛词搜出来全是噪音。
    """
    assert dl._to_search_word("手机警报器（警笛模拟器）2.0版") == "手机警报器"
    assert dl._to_search_word("日乙安卓直装（先存后下,否则会乱🐎的）") == "日乙安卓直装"
    assert dl._to_search_word("【全网最齐】游戏软件资源合集") == ""      # 泛词 → 丢
    assert dl._to_search_word("最全文件") == ""                          # 泛词 → 丢
    assert dl._to_search_word("短的") == ""                              # 太短 → 丢
    assert dl._to_search_word("") == ""


def test_group_keywords_prefers_recent_specific(session) -> None:
    """群组取词:只要**最近 7 天**的、**够具体**的,按消息时间倒序,去重。"""
    now = datetime.now()
    rows = [
        XunleiGroupShare(user_id=1, group_id="g", share_id="s1", title="蓝河工具箱",
                         msg_time=now - timedelta(hours=2)),
        XunleiGroupShare(user_id=1, group_id="g", share_id="s2", title="警笛模拟器（2.0）",
                         msg_time=now - timedelta(days=1)),
        XunleiGroupShare(user_id=1, group_id="g", share_id="s3", title="【全网最齐】资源合集",
                         msg_time=now - timedelta(hours=1)),          # 泛词 → 丢
        XunleiGroupShare(user_id=1, group_id="g", share_id="s4", title="老货工具箱",
                         msg_time=now - timedelta(days=30)),          # 太旧 → 丢
        XunleiGroupShare(user_id=1, group_id="g", share_id="s5", title="蓝河工具箱",
                         msg_time=now - timedelta(hours=3)),          # 重复 → 去重
    ]
    session.add_all(rows)
    session.commit()
    assert dl.group_keywords(session, 1, top=5) == ["蓝河工具箱", "警笛模拟器"]


def test_search_keywords_merges_group_names_and_library_names(session, monkeypatch) -> None:
    """汇总:候选 = **群里的资源名** + **资源库名称**,去重后按 top 截断。

    用户口径(2026-10-04):"你抖音搜索就跟着**群里面的资源名字**走,**结合资源库里面的名称**"。
    (本轮类目 = 轮换游标当前值,默认第一个类目「资料」;这里两个词都属它,避免走类目兜底。)
    """
    session.add(XunleiGroupShare(user_id=1, group_id="g", share_id="s1",
                                 title="四级真题 网盘", msg_time=datetime.now()))
    session.commit()
    monkeypatch.setattr("app.services.cross_accounts._keywords_from_library",
                        lambda s, u, top: ["四级真题 网盘", "考公资料"])

    class _S:
        douyin_leads_group_keywords = 3

    # top=2:两个候选都属「资料」(默认类目)且够用,不会走"话题词兜底"补位
    assert dl.search_keywords(session, 1, top=2, settings=_S()) == ["四级真题 网盘", "考公资料"]


def test_push_leads_falls_back_when_douhot_missing(monkeypatch) -> None:
    """抖音群没配时回落主群/管理员群 —— 不能因为少配一个群就整条推送丢掉。"""
    from app.services import douyin_leads as dl
    from app.services import feishu_client

    sent = {}

    class _C:
        def __init__(self, webhook, secret):
            sent["webhook"] = webhook

        def send_card(self, card):
            return True

    class _S(_Settings):
        feishu_webhook_douhot = ""

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)
    dl.push_leads([{"mark": "甲", "title": "t", "url": "u", "author": "", "keyword": ""}], _S())
    assert sent["webhook"] == "https://example.com/hook"


def test_push_leads_card_is_grid_with_author_work_link(monkeypatch) -> None:
    """版式对齐公众号:**四列网格**(作者/作品/资源/链接)——用户口径
    "格式按照公众号的格式 作者名字 作品名字 以及链接"。"""
    from app.services import douyin_leads as dl
    from app.services import feishu_client

    sent = {}

    class _C:
        def __init__(self, webhook, secret):
            pass

        def send_card(self, card):
            sent["card"] = card
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)
    dl.push_leads([{
        "mark": "白泽的梦", "title": "《白泽的梦》diplay车机互联教程",
        "url": "https://www.douyin.com/video/1", "author": "籽***", "keyword": "diplay",
        "kouling": {"kind": "share", "status": "ok", "our_url": "https://pan.xunlei.com/s/OUR"}}],
        _Settings())

    cols = [e for e in sent["card"]["elements"] if e.get("tag") == "column_set"]
    assert len(cols) == 2, "应为表头 + 一条数据行"
    header = [c["elements"][0]["text"]["content"] for c in cols[0]["columns"]]
    assert header == ["**作者**", "**作品**", "**资源**", "**链接**"]
    row = str(cols[1])
    # **v2 口径**(2026-10-02 用户在三个版本里选定):
    assert "籽***" in row                                 # 作者列 = 抖音账号名(工具脱敏过的)
    assert "车机互联教程" in row                           # 作品名其余部分保留
    assert "白泽的梦" not in row                           # 别人的口令《…》被**删掉**(不是替换)
    assert "https://www.douyin.com/video/1" in row        # 作品链接
    assert "https://pan.xunlei.com/s/OUR" in row          # 资源链仍可点
    # ⚠️ **口径变更(2026-10-04 用户)**:「资源」列写**盘名**(迅雷),不再写"我方链" ——
    # 客户群里"我方"二字会暴露运营方身份。见 `_pan_name` 的说明。
    assert "迅雷" in row and "我方链" not in row
    # ⚠️ **口径变更(2026-10-04 用户:"不要带念飞思雪")**:卡片头**不再挂品牌名**
    # —— 它是对 2026-10-02"品牌词只进标题"那条的**变更**(客户群里挂运营方品牌没价值)。
    assert "念飞思雪" not in str(sent["card"]["header"])
    assert "抖音推广线索" in str(sent["card"]["header"])


def test_strip_others_removes_others_name() -> None:
    """把**别人的名字**《…》删掉(v2 口径:删掉,不替换 —— 品牌词只进卡片标题)。"""
    from app.services.feishu._cards import strip_others

    assert strip_others("《三岁分享》diplay车机互联") == "diplay车机互联"
    assert strip_others("《白泽的梦》资源更新了") == "资源更新了"
    assert strip_others("普通标题无书名号") == "普通标题无书名号"   # 没有《》就不动
    assert strip_others("") == ""


# ---------------------------------------------------------------- 平台泛化(2026-10-02)

def test_platforms_of_parses_and_ignores_unknown() -> None:
    """平台列表来自配置;拼错的名字**忽略并告警**,不能让一个笔误停掉整轮。"""
    from app.services import douyin_leads as dl

    class _S:
        leads_platforms = "douyin, kuaishou ,, 不存在的平台, douyin"

    assert dl.platforms_of(_S()) == ["douyin", "kuaishou"]     # 去空、去重、忽略未知

    class _Empty:
        leads_platforms = ""

    assert dl.platforms_of(_Empty()) == ["douyin"]             # 空配置回落抖音


def test_find_leads_passes_platform_to_crawler(monkeypatch) -> None:
    """搜哪个平台要**真的传下去**(此前写死 "douyin",接新平台就白搭)。"""
    from app.services import douyin_leads as dl, mediacrawler_source as mc

    seen = {}
    monkeypatch.setattr(mc, "available", lambda: (True, "ok"))
    monkeypatch.setattr(mc, "crawl",
                        lambda plat, kws: seen.update(plat=plat, kws=kws) or [])
    dl.find_leads(["词"], platform="kuaishou")
    assert seen["plat"] == "kuaishou" and seen["kws"] == ["词"]


def test_push_leads_routes_by_platform(monkeypatch) -> None:
    """每个平台推**自己的专属群**:快手→快手群;没配就回落主群/管理员群。"""
    from app.services import douyin_leads as dl
    from app.services import feishu_client

    sent = {}

    class _C:
        def __init__(self, webhook, secret):
            sent["webhook"] = webhook

        def send_card(self, card):
            sent["card"] = card
            return True

    class _S(_Settings):
        feishu_webhook_kuaishou = "https://example.com/kuaishou"

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)
    dl.push_leads([{"mark": "甲", "title": "t", "url": "u", "author": "", "keyword": ""}],
                  _S(), platform="kuaishou")
    assert sent["webhook"] == "https://example.com/kuaishou"
    assert "快手" in str(sent["card"]["header"])            # 卡片也要说清来自哪个平台

    dl.push_leads([{"mark": "甲", "title": "t", "url": "u", "author": "", "keyword": ""}],
                  _Settings(), platform="douyin")
    assert sent["webhook"] == "https://example.com/douhot"  # 抖音仍走历史的 douhot 群


def test_apply_kouling_survives_dead_xunlei_credentials(session, monkeypatch) -> None:
    """⚠️ 迅雷登录态失效(`resolve` 抛)时,**线索照推**,只是标成"未解析"。

    实测 2026-10-03:迅雷 refresh token 失效(`invalid_grant`),而 `apply_kouling` 原本
    没有兜底 → 整轮 `douyin_leads` 记 failed、**连卡片都推不出去**。可是"发现线索"和
    "能不能转存"是两件事:转存挂了,线索本身仍然有值(人要看的)。
    """
    from app.services import xunlei_kouling as kk

    monkeypatch.setattr(kk, "known_koulings", lambda s, u: set())

    def _boom(m):
        raise RuntimeError("迅雷刷新 token 失败:{'error': 'invalid_grant'}")

    monkeypatch.setattr(kk, "resolve", _boom)
    out = dl.apply_kouling(_leads("三岁宝库", "白泽的梦"), session, 1, _Settings())
    assert len(out) == 2                                   # 线索一条没丢
    assert all(x["kouling"]["kind"] == "error" for x in out)
    line = dl._kouling_line(out[0])
    assert "迅雷登录态失效" in line and "invalid_grant" in line


# ---------------------------------------------------------------- 线索落库(结算归因)

def test_aweme_id_extracted_from_video_url() -> None:
    """去重键必须是**作品维度**。

    ⚠️ 不能用 `_parse_record` 给的 `uid` —— 那是 **creator_hash**(作者维度),
    同一个作者的多条视频会全撞在一起,落库时把线索丢了。
    """
    assert dl._aweme_id("https://www.douyin.com/video/7412345678901234567") == "7412345678901234567"
    assert dl._aweme_id("https://www.douyin.com/note/7412345678901234567") == "7412345678901234567"
    assert dl._aweme_id("https://www.douyin.com/user/abc") == ""


def test_save_leads_is_idempotent_and_keeps_share_count(session) -> None:
    """同一作品重复发现只存一条(幂等 upsert),转发量要落下来 —— 它是结算要用的量级代理。"""
    from app.db.models import DouyinLead

    leads = [
        {"aweme_id": "111", "mark": "三岁分享", "title": "t1", "author": "a", "url": "u1",
         "keyword": "k", "share_count": 176, "kouling": {"kind": "group"}},
        {"aweme_id": "", "mark": "没有作品id", "title": "t2", "url": "u2"},   # 缺去重键 → 跳过
    ]
    assert dl._save_leads(session, 1, leads) == 1
    session.commit()
    # 第二次(转发量涨了)→ 覆盖更新,不新增行
    leads[0]["share_count"] = 300
    assert dl._save_leads(session, 1, leads) == 1
    session.commit()
    rows = session.query(DouyinLead).all()
    assert len(rows) == 1 and rows[0].share_count == 300 and rows[0].kind == "group"


class TestKoulingSummary:
    """运行记录要能看出这条链在**往外扩**还是在**原地打转**(2026-10-04 加)。

    搜索词有一路是从**我们自己已有的群**取的(`group_keywords` 拿群里的资源标题当词),
    于是容易形成自循环:搜出来的口令反复指向**已经加过**的群 ——
    线索数看着不少、**新群一个没有**。用户原话:"而不是一直用着一个口令进群,
    我们是需要创新的"。没有"新群"这个数,自循环永远看不出来。
    """

    def test_counts_new_groups_separately(self) -> None:
        from app.services import douyin_leads as dl

        leads = [
            {"kouling": {"kind": "group", "newly_joined": True}},
            {"kouling": {"kind": "group", "newly_joined": False}},   # 已有的群
            {"kouling": {"kind": "group", "newly_joined": False}},
            {"kouling": {"kind": "share"}},
            {"kouling": {"kind": "none"}},
            {},                                                       # 没解析
        ]
        assert dl._kouling_summary(leads) == "新群1/群3/链1"

    def test_self_loop_shows_new_group_zero(self) -> None:
        from app.services import douyin_leads as dl

        """**自循环的样子**:解析出一堆群口令,但**一个新群都没有**。"""
        leads = [{"kouling": {"kind": "group", "newly_joined": False}} for _ in range(9)]
        assert dl._kouling_summary(leads) == "新群0/群9/链0"


class TestUsedWordsGoLast:
    """**已经解析出过群的词,下轮排到最后**(2026-10-04,用户口径)。

    "而不是一直用着一个口令进群,我们是需要创新的" —— 抖音搜索次数有限、每轮又慢,
    名额要优先给**没试过的词**。

    ⚠️ **只排后、不删掉**:同一个词将来还可能解析出**别的**群(推广号会换口令);
    硬删就把它堵死了 —— 正是本项目反复踩过的"把能搬的判成终态"那种反向错误。
    """

    def test_seen_words_are_sorted_last(self, session, monkeypatch) -> None:
        from app.db.models import DouyinLead
        from app.services import douyin_leads as dl

        session.add(DouyinLead(user_id=1, aweme_id="a1", keyword="用过的资料真题", kind="group",
                               title="t", found_at=datetime.now()))
        session.commit()
        monkeypatch.setattr("app.services.cross_accounts._keywords_from_library",
                            lambda s, u, top: ["用过的资料真题", "没试过的资料模板"])

        class _S:
            douyin_leads_group_keywords = 0

        assert dl.search_keywords(session, 1, top=2, settings=_S()) == ["没试过的资料模板",
                                                                        "用过的资料真题"]

    def test_word_is_not_dropped_just_deprioritised(self, session, monkeypatch) -> None:
        """唯一一个词即使"用过"也仍在结果里 —— **排后不等于删除**。"""
        from app.db.models import DouyinLead
        from app.services import douyin_leads as dl

        session.add(DouyinLead(user_id=1, aweme_id="a1", keyword="唯一的资料真题", kind="group",
                               title="t", found_at=datetime.now()))
        session.commit()
        monkeypatch.setattr("app.services.cross_accounts._keywords_from_library",
                            lambda s, u, top: ["唯一的资料真题"])

        class _S:
            douyin_leads_group_keywords = 0

        assert dl.search_keywords(session, 1, top=1, settings=_S()) == ["唯一的资料真题"]

    def test_share_kind_does_not_deprioritise(self, session, monkeypatch) -> None:
        """只有**解析成群**的才算"用过的" —— 解析出**链**的词不影响(它能反复产出链接)。"""
        from app.db.models import DouyinLead
        from app.services import douyin_leads as dl

        session.add(DouyinLead(user_id=1, aweme_id="a1", keyword="出链的资料真题", kind="share",
                               title="t", found_at=datetime.now()))
        session.commit()
        monkeypatch.setattr("app.services.cross_accounts._keywords_from_library",
                            lambda s, u, top: ["出链的资料真题", "乙资料模板"])

        class _S:
            douyin_leads_group_keywords = 0

        assert dl.search_keywords(session, 1, top=2, settings=_S()) == ["出链的资料真题", "乙资料模板"]


class TestResourceColumnShowsPanName:
    """「资源」列写**盘名**(夸克/百度/迅雷),并**说清为什么没搬**(2026-10-04 用户口径)。

    用户原话:"**资源不要写我方链接,那个网盘就写那个网盘名称**"、"**未搬运是什么问题**"。
    ① 原来写 `🔴我方链` —— "我方"二字**在客户群里暴露运营方身份**,换成中性的**盘名**;
    ② 原来没搬的一律写 `⏸未搬`,**看不出为什么** —— 而最常见的原因是**网盘满了**
       (只有人能清),那正是最该让人看到的一种。
    """

    def _render(self, monkeypatch, **kouling) -> str:
        from app.services import douyin_leads as dl
        from app.services import feishu_client

        sent = {}

        class _C:
            def __init__(self, *a) -> None: ...
            def send_card(self, card):
                sent["card"] = card
                return True

        monkeypatch.setattr(feishu_client, "FeishuClient", _C)
        dl.push_leads([{"mark": "m", "title": "《x》某资源", "url": "https://d/v/1",
                        "author": "籽***", "keyword": "k", "kouling": kouling}], _Settings())
        return str(sent["card"])

    def test_pan_name_from_url(self) -> None:
        from app.services.douyin_leads import _pan_name

        assert _pan_name("https://pan.quark.cn/s/x") == "夸克"
        assert _pan_name("https://pan.baidu.com/s/1x") == "百度"
        assert _pan_name("https://pan.xunlei.com/s/x") == "迅雷"
        assert _pan_name("https://example.com/x") == ""

    def test_transferred_shows_pan_name_not_our_link_wording(self, monkeypatch) -> None:
        card = self._render(monkeypatch, kind="share", status="ok",
                            our_url="https://pan.quark.cn/s/OUR")
        assert "夸克" in card and "我方链" not in card
        assert "https://pan.quark.cn/s/OUR" in card        # 链接仍可点

    def test_disk_full_says_why(self, monkeypatch) -> None:
        """⚠️ 盘满是最该让人看到的一种 —— 只有人能清。"""
        card = self._render(monkeypatch, kind="share", status="disk_full",
                            share_url="https://pan.quark.cn/s/S")
        assert "网盘已满" in card, f"盘满却没说原因:{card[:200]}"

    def test_over_budget_and_skipped_have_distinct_wording(self, monkeypatch) -> None:
        assert "本轮额度" in self._render(monkeypatch, kind="share", status="over_budget",
                                        share_url="https://pan.xunlei.com/s/S")
        assert "被挡下" in self._render(monkeypatch, kind="share", status="skipped",
                                      share_url="https://pan.baidu.com/s/1S")
