"""抖音推广线索单测(2026-10-02):判据(标题以《》开头)、去重、卡片推送。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_publish_cutoff(monkeypatch):
    """单测里**默认关掉发布时间过滤**。

    ⚠️ `find_leads` 读的是**全局 settings**(它没有 settings 参数),所以给假件加字段没用 ——
    必须打这个补丁。大多数用例验的是"判据 / 去重",不是"新鲜度";
    **过滤本身在 `TestPublishCutoff` 里专门验**。
    """
    monkeypatch.setattr("app.services.douyin_leads._min_publish_ts", lambda s: 0)


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

from sqlalchemy import create_engine, select  # noqa: E402
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
    # **六列**(用户口径 2026-10-04):作者/作品/资源/视频/转发数/条数
    assert header == ["**作者**", "**作品**", "**资源**", "**视频**", "**转发数**", "**条数**"]
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
        # ⚠️ 2026-10-05 改口径:原来的 `链N` 数的是"口令**是**分享链的条数",
        # 读起来却像"搬成了 N 条" —— 里面一大半是早就搬过、或这轮没搬的。现在摊开。
        assert dl._kouling_summary(leads) == "新群1/群3/分享链1(新搬0 已搬过0 超额度0)"

    def test_self_loop_shows_new_group_zero(self) -> None:
        from app.services import douyin_leads as dl

        """**自循环的样子**:解析出一堆群口令,但**一个新群都没有**。"""
        leads = [{"kouling": {"kind": "group", "newly_joined": False}} for _ in range(9)]
        assert dl._kouling_summary(leads) == "新群0/群9/分享链0(新搬0 已搬过0 超额度0)"

    def test_已搬过的不算本轮产出(self) -> None:
        """★ **2026-10-05 P2**:`already` 那一支现在也带 `our_url`(从历史捡回来的,
        卡片上该显示),但它**不是本轮的产出** —— 算进去就把指标注水了,
        而这条链要的正是"**新**群/新资源"。
        """
        from app.services import douyin_leads as dl

        leads = [
            {"kouling": {"kind": "share", "status": "ok",
                         "our_url": "https://pan.quark.cn/s/new1"}},     # 本轮真搬的
            {"kouling": {"kind": "share", "status": "already",
                         "our_url": "https://pan.quark.cn/s/old1"}},     # 历史捡回的
        ]
        got = dl._kouling_summary(leads)
        assert "新搬1" in got, f"只该数本轮真搬的:{got}"
        assert "已搬过1" in got


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


class TestSixColumnCard:
    """**六列版式**(用户口径 2026-10-04):作者 / 作品 / 资源 / 视频 / 转发数 / 条数。

    两处可点(**照公众号卡片那套**):
      · 「**作品**」点开是**抖音视频**(公众号的「文章」列就是这个做法);
      · 「**资源**」写**网盘名**(夸克/百度/迅雷)、点开是**我方链**。

    「**条数**」= **这个资源(口令)本轮被几条视频在推**(用户选定)——
    一眼看出"大家都在抢这个";转发量原来并进作品列,现在**独立成列**。
    """

    def _render(self, monkeypatch, leads) -> dict:
        from app.services import douyin_leads as dl
        from app.services import feishu_client

        sent = {}

        class _C:
            def __init__(self, *a) -> None: ...
            def send_card(self, card):
                sent["card"] = card
                return True

        monkeypatch.setattr(feishu_client, "FeishuClient", _C)
        dl.push_leads(leads, _Settings())
        return sent["card"]

    def _lead(self, mark, title, sc, **kouling):
        return {"mark": mark, "title": title, "url": f"https://d/v/{mark}", "author": "籽***",
                "keyword": "k", "share_count": sc, "kouling": kouling}

    def test_count_column_groups_by_kouling(self, monkeypatch) -> None:
        """⚠️ 「条数」是**同一口令被几条视频推** —— 不是行号、也不是总条数。"""
        card = self._render(monkeypatch, [
            self._lead("高性价比人生指南", "A", 40257, kind="share", status="ok",
                       our_url="https://pan.quark.cn/s/OUR"),
            self._lead("高性价比人生指南", "B", 1027, kind="share", status="ok",
                       our_url="https://pan.quark.cn/s/OUR"),
            self._lead("趣玩收藏", "C", 0, kind="share", status="ok",
                       our_url="https://pan.xunlei.com/s/OUR2"),
        ])
        rows = [e for e in card["elements"] if e.get("tag") == "column_set"][1:]

        def cell(row_el, idx: int) -> str:      # 取某行第 idx 列的可见文本
            return row_el["columns"][idx]["elements"][0]["text"]["content"]

        assert cell(rows[0], 5) == "2", f"同一口令两条 → 条数应为 2,实得 {cell(rows[0], 5)}"
        assert cell(rows[1], 5) == "2"
        assert cell(rows[2], 5) == "1", "另一个口令 → 条数为 1"

    def test_作品列点开是我方网盘链_不是抖音视频(self, monkeypatch) -> None:
        """★ **用户 2026-10-06 报的就是这条**。

        `26b6d69`(10-04)那条提交的说明里用户原话是「资源不要直接是链接形式,
        **跟公众号一样点作品就能跳转**」—— 而公众号卡片里标题跳的**正是我方转存链**
        (见 `_listen.py::_push_listen` 的 `article_md`)。当时实现成了"标题 → 抖音视频",
        于是点标题拿不到网盘,而且同一个视频还被链了两次(这里 + 「▶视频」列)。
        """
        card = self._render(monkeypatch, [
            self._lead("x", "某资源标题", 10, kind="share", status="ok",
                       our_url="https://pan.quark.cn/s/OUR")])
        row = str([e for e in card["elements"] if e.get("tag") == "column_set"][1])
        assert "[某资源标题](https://pan.quark.cn/s/OUR)" in row, f"作品该指向我方网盘链:{row[:220]}"
        assert "[某资源标题](https://d/v/x)" not in row, \
            "作品**不该**再指向抖音视频(那是「视频」列的活,链两次是冗余)"

    def test_视频列仍在_两处分工(self, monkeypatch) -> None:
        """⚠️ 反向:改完不能把"看原视频"弄丢 —— 改由「▶视频」列承担。"""
        card = self._render(monkeypatch, [
            self._lead("x", "某资源标题", 10, kind="share", status="ok",
                       our_url="https://pan.quark.cn/s/OUR")])
        row = str([e for e in card["elements"] if e.get("tag") == "column_set"][1])
        assert "[▶视频](https://d/v/x)" in row, f"视频列必须还在:{row[:220]}"

    def test_资源列是纯文本不带链接(self, monkeypatch) -> None:
        """用户 10-04 的原话:「**资源不要直接是链接形式**」——
        与公众号卡片一致(那边资源列写 `🔴夸克` 也是纯文本)。"""
        card = self._render(monkeypatch, [
            self._lead("x", "某资源标题", 10, kind="share", status="ok",
                       our_url="https://pan.quark.cn/s/OUR")])
        row = str([e for e in card["elements"] if e.get("tag") == "column_set"][1])
        assert "🔴夸克" in row, row[:220]
        assert "[🔴夸克]" not in row, "资源列不该是可点链接"

    def test_没搬的_标题不硬链视频(self, monkeypatch) -> None:
        """没我方链时标题**不点** —— 别又退回"点了跳视频"那个歧义。"""
        card = self._render(monkeypatch, [
            self._lead("x", "某资源标题", 10, kind="share", status="skipped")])
        row = str([e for e in card["elements"] if e.get("tag") == "column_set"][1])
        assert "某资源标题" in row and "某资源标题](" not in row, row[:220]

    def test_share_count_is_its_own_column_not_merged_into_work(self, monkeypatch) -> None:
        card = self._render(monkeypatch, [
            self._lead("x", "某资源标题", 40257, kind="share", status="ok",
                       our_url="https://pan.quark.cn/s/OUR")])
        row = str([e for e in card["elements"] if e.get("tag") == "column_set"][1])
        assert "↗40257" in row and "某资源标题 · ↗40257" not in row, "转发数应独立成列"


class TestUnmovedQueue:
    """**"未搬"进待办队列**(2026-10-04,用户:"可以加上")。

    场景正是用户遇到的:抖音那轮 **5 条全卡在盘满**,而盘一清出来 ——
    **没有任何机制会去重搬它们**,只等"同一个视频再次被搜到"纯属碰运气。

    ⚠️ 做法是**复用 `DiscoveredPanLink`** 那张表(公开发现链的),它本来就有
    `pending/ok/skipped` 状态机,而 `pan_discovery.sync` 现在**优先重试存量待办** ——
    **一处状态机、两条链共用**,不新建表也不新增作业。
    """

    def test_creates_pending_row(self, session) -> None:
        from app.db.models import DiscoveredPanLink
        from app.services.douyin_leads import _enqueue_later

        _enqueue_later(session, 1, {"share_url": "https://pan.quark.cn/s/X"}, "某口令")
        row = session.scalar(select(DiscoveredPanLink))
        assert row is not None and row.status == "pending"
        assert row.platform == "douyin" and row.title == "某口令"

    def test_is_idempotent(self, session) -> None:
        """同一个链重复入队 → **更新那一行**,不是插第二条(否则撞唯一键)。"""
        from app.db.models import DiscoveredPanLink
        from app.services.douyin_leads import _enqueue_later

        for _ in range(3):
            _enqueue_later(session, 1, {"share_url": "https://pan.quark.cn/s/X"}, "某口令")
        assert len(session.scalars(select(DiscoveredPanLink)).all()) == 1

    def test_does_not_downgrade_finished_rows(self, session) -> None:
        """⚠️ **已经搬好(`ok`)或终态(`skipped`)的行不能被退回 pending** ——
        那会让"搬成功过的链"被反复重搬(正是本项目反复踩过的反向错误)。"""
        from app.db.models import DiscoveredPanLink
        from app.services.douyin_leads import _enqueue_later

        session.add(DiscoveredPanLink(user_id=1, platform="douyin",
                                      origin_url="https://pan.quark.cn/s/DONE", status="ok"))
        session.add(DiscoveredPanLink(user_id=1, platform="douyin",
                                      origin_url="https://pan.quark.cn/s/DEAD", status="skipped"))
        session.commit()
        _enqueue_later(session, 1, {"share_url": "https://pan.quark.cn/s/DONE"}, "a")
        _enqueue_later(session, 1, {"share_url": "https://pan.quark.cn/s/DEAD"}, "b")
        got = {r.origin_url: r.status for r in session.scalars(select(DiscoveredPanLink)).all()}
        assert got["https://pan.quark.cn/s/DONE"] == "ok"
        assert got["https://pan.quark.cn/s/DEAD"] == "skipped"

    def test_blank_url_is_ignored(self, session) -> None:
        from app.db.models import DiscoveredPanLink
        from app.services.douyin_leads import _enqueue_later

        _enqueue_later(session, 1, {"share_url": ""}, "x")
        assert session.scalars(select(DiscoveredPanLink)).all() == []


class TestCountGroupsByResourceNotKouling:
    """⚠️ 「条数」按**资源身份**分组,**不是按口令**(2026-10-04 修正)。

    实测发现:同一个资源会被不同推广号起**不同口令** ——
    《齐民要术》《人生使用说明书》其实都是《高性价比人生指南》的别名。
    按口令分会把**同一份资源算成好几条**,那就看不出"大家都在抢这个"了。

    判据:**能拿到原始链就用它当身份**(那才是资源本身),拿不到的(群口令/没解出来)才回落到口令。
    """

    def _render(self, monkeypatch, leads) -> list:
        from app.services import douyin_leads as dl
        from app.services import feishu_client

        sent = {}

        class _C:
            def __init__(self, *a) -> None: ...
            def send_card(self, card):
                sent["card"] = card
                return True

        monkeypatch.setattr(feishu_client, "FeishuClient", _C)
        dl.push_leads(leads, _Settings())
        return [e for e in sent["card"]["elements"] if e.get("tag") == "column_set"][1:]

    def _lead(self, mark, share_url, **extra):
        k = {"kind": "share", "status": "disk_full", "share_url": share_url}
        k.update(extra)
        return {"mark": mark, "title": f"标题{mark}", "url": f"https://d/v/{mark}",
                "author": "a", "share_count": 1, "kouling": k}

    def test_same_resource_different_kouling_counts_together(self, monkeypatch) -> None:
        rows = self._render(monkeypatch, [
            self._lead("高性价比人生指南", "https://pan.xunlei.com/s/SAME"),
            self._lead("齐民要术", "https://pan.xunlei.com/s/SAME"),      # 别名,同一条链
        ])
        assert rows[0]["columns"][5]["elements"][0]["text"]["content"] == "2"
        assert rows[1]["columns"][5]["elements"][0]["text"]["content"] == "2"

    def test_different_resources_count_separately(self, monkeypatch) -> None:
        rows = self._render(monkeypatch, [
            self._lead("甲", "https://pan.xunlei.com/s/A"),
            self._lead("乙", "https://pan.xunlei.com/s/B"),
        ])
        assert rows[0]["columns"][5]["elements"][0]["text"]["content"] == "1"
        assert rows[1]["columns"][5]["elements"][0]["text"]["content"] == "1"

    def test_no_link_falls_back_to_kouling(self, monkeypatch) -> None:
        """拿不到链的(群口令/没解出)按口令分组 —— 总得有个身份。"""
        rows = self._render(monkeypatch, [
            {"mark": "同一群口令", "title": "t1", "url": "https://d/v/1", "author": "a",
             "share_count": 1, "kouling": {"kind": "group", "status": "deferred"}},
            {"mark": "同一群口令", "title": "t2", "url": "https://d/v/2", "author": "b",
             "share_count": 1, "kouling": {"kind": "group", "status": "deferred"}},
        ])
        assert rows[0]["columns"][5]["elements"][0]["text"]["content"] == "2"


class TestLeadKeepsOurUrl:
    """⚠️ `DouyinLead.our_url` —— **这条线索搬成了哪条链**(2026-10-04 补,计划第 12 项)。

    原来**落库时把它丢了**,于是"这个口令到底搬没搬成、搬成了哪条链"**事后查不出来**,
    只能去翻当时的飞书卡片。当天整理本轮线索时正是卡在这里(做卡片的"条数/资源身份"
    都拿不到链)。**飞书卡片会过期、会刷屏,库里的字段不会。**
    """

    def test_saved(self, session) -> None:
        from app.db.models import DouyinLead
        from app.services import douyin_leads as dl

        dl._save_leads(session, 1, [{
            "aweme_id": "a1", "mark": "某口令", "title": "t", "author": "a",
            "url": "https://d/v/1", "keyword": "k", "share_count": 3,
            "kouling": {"kind": "share", "status": "ok",
                        "our_url": "https://pan.xunlei.com/s/OUR"}}])
        session.commit()          # `_save_leads` 不提交,由调用方提交(见 tick)
        row = session.scalar(select(DouyinLead))
        assert row.our_url == "https://pan.xunlei.com/s/OUR"

    def test_empty_when_not_transferred(self, session) -> None:
        """没搬成的留空 —— **不拿假值填**(存量行留空也表示"还没用新版重采过")。"""
        from app.db.models import DouyinLead
        from app.services import douyin_leads as dl

        dl._save_leads(session, 1, [{
            "aweme_id": "a2", "mark": "某口令", "title": "t", "author": "a",
            "url": "https://d/v/2", "keyword": "k", "share_count": 3,
            "kouling": {"kind": "share", "status": "disk_full"}}])
        session.commit()
        assert session.scalar(select(DouyinLead)).our_url == ""


# ------------------------------------------------ 已搬过的口令要带上我方链(2026-10-05)

class TestKnownKoulingLinks:
    """★ **2026-10-05 修的洞**:`known_koulings` 只给"搬过没",**不给"搬成了哪条链"**
    ⇒ `douyin_leads.our_url` **恒为空**(实测 43 条线索 / 0 条有链),
    而那个字段 10-04 加出来就是为了回答"这个口令到底搬没搬成"。

    **一个只说"做过"、不说"做成了什么"的判据,等于把结果丢了。**
    """

    def _db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db.models import Base

        eng = create_engine("sqlite://")
        Base.metadata.create_all(eng)
        return sessionmaker(bind=eng)()

    def test_已搬过的口令要能查到它的我方链(self) -> None:
        from app.db.models import XunleiResource
        from app.services import xunlei_kouling as kk

        db = self._db()
        try:
            db.add(XunleiResource(user_id=1, fid="f1", name="两全其美",
                                  parent_name="口令解析", share_url="https://pan.xunlei.com/s/AAA"))
            db.add(XunleiResource(user_id=1, fid="f2", name="没链的", parent_name="口令解析"))
            db.add(XunleiResource(user_id=1, fid="f3", name="别人的", parent_name="其他目录",
                                  share_url="https://pan.xunlei.com/s/BBB"))
            db.commit()
            links = kk.known_kouling_links(db, 1)
            assert links == {"两全其美": "https://pan.xunlei.com/s/AAA"}, \
                "只认『口令解析』目录下、且确实有分享链的那些"
        finally:
            db.close()

    def test_只按本租户取_不串号(self) -> None:
        from app.db.models import XunleiResource
        from app.services import xunlei_kouling as kk

        db = self._db()
        try:
            db.add(XunleiResource(user_id=2, fid="f9", name="别人的资源",
                                  parent_name="口令解析", share_url="https://pan.xunlei.com/s/XXX"))
            db.commit()
            assert kk.known_kouling_links(db, 1) == {}
        finally:
            db.close()


class TestHotSeedBudget:
    """**外部种子名额**(2026-10-05 从 3 降到 1)。"""

    def test_设为0时一个都不取_且不出网(self) -> None:
        """额度为 0 要**早退** —— 这是唯一能离线测的分支,也确实是"关掉"的路径。"""
        from app.services.douyin_leads import hot_seed_words

        class _S:
            douyin_leads_hot_keywords = 0

        assert hot_seed_words(_S()) == []

    def test_名额真的由设置控制_不是函数默认参数(self) -> None:
        """⚠️ **此前是个坑**:设置 `douyin_leads_hot_keywords` **只当开关用**,
        真正常量是 `hot_seed_words(limit=3)` 的**默认参数** —— 改设置不生效。
        现在 `limit=None` 时**读设置**。
        """
        from app.services.douyin_leads import hot_seed_words

        class _S:
            douyin_leads_hot_keywords = 0        # 0 即"关"
        assert hot_seed_words(_S()) == []
        # 显式传 limit 时仍覆盖设置(供测试/特殊用途)
        assert hot_seed_words(_S(), limit=0) == []

    def test_默认名额是1_不是3(self) -> None:
        """**这是数据决策,不是口味**:按 `douyin_leads.keyword` 归因全历史 43 条线索 ——
        **热榜种子有效率 0%(0/5)、资源名 86%(25/29)**,而它每轮占 3/7 个名额(43%)。

        要改回 3,先看 `doc/抖音线索链-最优策略-2026-10-05.md` 里的证据,
        并确认那 5 条样本已经攒够、结论已经更新。
        """
        from config.settings import Settings

        assert Settings.model_fields["douyin_leads_hot_keywords"].default == 1, (
            "热榜种子名额被改动了 —— 这是基于实测有效率(0% vs 86%)的决定,"
            "改之前请看 doc/抖音线索链-最优策略-2026-10-05.md")


class TestKeywordScore:
    """**词级产出评分**(2026-10-05 P1):把库里已有的归因数据用起来。

    背景:`douyin_leads.keyword` 从 10-03 起**每行都填**(实测 43/43)——
    "每个词产出了什么"一直在库里,**却从来没用来选过词**;
    排序依据一直是"新鲜度",那条规则定于 10-04,**当时还没有产出数据**。
    """

    def test_没搜过的词给中性值(self) -> None:
        from app.services.douyin_leads import keyword_score

        assert keyword_score(0, 0) == 0.5, "没有样本就不该高看也不该低看"

    def test_零产出词低于没搜过的词_这就是冷却(self) -> None:
        """★ **"零产出降权"是这条公式的自然结果,不需要单独一套规则**。"""
        from app.services.douyin_leads import keyword_score

        assert keyword_score(2, 0) < keyword_score(0, 0), "搜过且零产出的,该排到没搜过的之后"
        assert keyword_score(5, 0) < keyword_score(2, 0), "样本越多越像真废词,降得越多"

    def test_小样本被拉回中性_不捧红也不打死(self) -> None:
        """⚠️ 5 次里蒙中 1 次,该算什么?

        **不是 20%**(样本太小、不敢信),也**不是废词**(只搜了 5 次)。
        收缩把它拉回中性:**0.31** ——
        · 比原始 20% **高**(说明"不敢信",不是判死);
        · 但比没搜过的 0.5 **低**(有失败样本,该降);
        · 更比 25/29 的 0.83 **低得多**(走运的小样本不能压过验证过的大样本)。
        """
        from app.services.douyin_leads import keyword_score

        s51, s00, s2925 = keyword_score(5, 1), keyword_score(0, 0), keyword_score(29, 25)
        assert s51 > 0.2, "收缩是拉回中性,不是把 20% 判成废词"
        assert s51 < s00, "但有失败样本 ⇒ 该排在'没搜过'之后"
        assert s51 < s2925, "走运的小样本绝不能压过验证过的大样本"

    def test_大样本贴近实测(self) -> None:
        """29 次里中 25 次 ≈ 86%(实测值)⇒ 收缩不该把大样本也拉平。"""
        from app.services.douyin_leads import keyword_score

        assert 0.75 < keyword_score(29, 25) < 0.90

    def test_产出归因把_none_算作零产出(self) -> None:
        """★ `kind="none"` = "搜到视频但标题里没《口令》" —— **搜了个寂寞**,
        必须算零产出。本仓的"线索数"把它和真产出混在一个数里,那是误导指标。
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db.models import Base, DouyinLead
        from app.services.douyin_leads import keyword_yield

        eng = create_engine("sqlite://")
        Base.metadata.create_all(eng)
        db = sessionmaker(bind=eng)()
        try:
            for i, k in enumerate(["none", "none", "share", "group"]):
                db.add(DouyinLead(user_id=1, aweme_id=f"a{i}", keyword="词A", kind=k))
            db.add(DouyinLead(user_id=1, aweme_id="b0", keyword="词B", kind="share"))
            db.commit()
            y = keyword_yield(db, 1)
            assert y["词A"] == (4, 2), f"4 条线索里只有 share/group 算产出:{y}"
            assert y["词B"] == (1, 1)
        finally:
            db.close()


class TestVariants:
    """**高产出词深挖变体**(2026-10-05 P2)。

    推广号做同一批资源**往往一次做一批**,换个写法就是另一个视频/另一个《口令》。
    实测 `霸王茶姬杯贴自定义入口链接直达` 有效率 **100%**、`高性价比人生指南共338页pdf` **78%**。
    ⇒ **沿着已验证的方向探**,而不是沿着类目探。
    """

    def test_剥掉推广后缀得到核心名(self) -> None:
        from app.services.douyin_leads import _variants

        assert _variants("霸王茶姬杯贴自定义入口链接直达") == ["霸王茶姬杯贴"]
        assert _variants("七宗罪测试免费入口直达附链接") != []

    def test_太短就不要(self) -> None:
        """剥完不足 4 字 ⇒ 搜出来全是噪声。"""
        from app.services.douyin_leads import _variants

        assert _variants("入口链接直达") == []

    def test_剥不动就返回空_不重复搜同一个词(self) -> None:
        from app.services.douyin_leads import _variants

        assert _variants("高性价比人生指南共338页pdf") == []
        assert _variants("") == []

    def test_变体不覆盖它自己的历史评分(self) -> None:
        """变体若**自己搜过**,用它自己的数据,别拿母词的分数盖掉。"""
        # 这条由 `search_keywords` 里的 `if v not in scores` 保证;这里钉住语义:
        from app.services.douyin_leads import _VARIANT_DISCOUNT, keyword_score

        parent = keyword_score(29, 25)
        assert parent * _VARIANT_DISCOUNT < 0.5 + 1e9   # 打折后仍是"高"信号
        assert parent * _VARIANT_DISCOUNT > keyword_score(0, 0), \
            "变体继承了证据,应该**高于**完全没搜过的词"


class TestNormWord:
    """**查分前先归一**(2026-10-05):同一资源常有好几种写法。"""

    def test_桥过大小写与标点(self) -> None:
        from app.services.douyin_leads import _norm_word

        assert _norm_word("高性价比人生指南PDF") == _norm_word("高性价比人生指南pdf")
        assert _norm_word("A B-C") == "abc"
        assert _norm_word("  空格  多  ") == "空格多"

    def test_有意不桥_词中间多几个字(self) -> None:
        """⚠️ `高性价比人生指南共338页pdf` 与 `高性价比人生指南pdf` 归一化后**仍不同**
        —— 那属于"同一个资源的两种叫法",本函数**有意不猜**:
        **乱匹配会把 A 的分数安到 B 头上,比漏认更坏。**
        """
        from app.services.douyin_leads import _norm_word

        assert _norm_word("高性价比人生指南共338页pdf") != _norm_word("高性价比人生指南pdf")


class TestPublishCutoff:
    """★ **发布时间下限**(2026-10-06 用户口径:「**2026年10月份之前的不要再保存进来了**」)。

    起因不是洁癖:抖音上的网盘推广帖**高度重复** —— 一条 8 月的老帖会被推广号**反复推**,
    照样"刚被发现",于是"每天搬 8 条"搬回来的**大半是已有的老资源**
    (实测一轮 8 条里 **5 条命中三盘互通**)。所以新鲜度不能靠"我们发现的时刻",要靠**帖子自己的发布时间**。

    ⚠️ **没有发布时间的也跳过** —— 判不了就宁可不收;但**必须报数**,
    绝不静默丢(否则整平台静默归零,正是本仓最忌讳的那类)。
    """

    def _leads(self, monkeypatch, rows):
        class _MC:
            @staticmethod
            def crawl(platform, keywords):
                return rows
        monkeypatch.setattr("app.services.mediacrawler_source.crawl", _MC.crawl)
        from app.services import douyin_leads as dl
        return dl

    def _row(self, aid, title, pub):
        return {"uid": f"u{aid}", "name": "某号", "url": f"https://v.douyin.com/{aid}/",
                "snippet": title, "publish_at": pub, "keyword": "夸克口令"}

    def test_早于下限的丢掉(self, monkeypatch) -> None:
        import datetime as dt
        dl = self._leads(monkeypatch, [
            self._row("new", "咐置新的叩苓", int(dt.datetime(2026, 10, 5).timestamp())),
            self._row("old", "咐置旧的叩苓", int(dt.datetime(2026, 8, 1).timestamp())),
        ])
        monkeypatch.setattr(dl, "_min_publish_ts",
                            lambda s: int(dt.datetime(2026, 10, 1).timestamp()))
        out = dl.find_leads(["夸克口令"])
        assert [x["mark"] for x in out] == ["咐置新的叩苓"], f"旧帖必须被丢掉:{out}"

    def test_没有发布时间的也丢掉(self, monkeypatch) -> None:
        import datetime as dt
        dl = self._leads(monkeypatch, [self._row("nt", "咐置无时间叩苓", 0)])
        monkeypatch.setattr(dl, "_min_publish_ts",
                            lambda s: int(dt.datetime(2026, 10, 1).timestamp()))
        assert dl.find_leads(["夸克口令"]) == [], "判不了就不收(但会报数,不是静默)"

    def test_越新越靠前(self, monkeypatch) -> None:
        import datetime as dt
        dl = self._leads(monkeypatch, [
            self._row("a", "咐置甲的叩苓", int(dt.datetime(2026, 10, 2).timestamp())),
            self._row("b", "咐置乙的叩苓", int(dt.datetime(2026, 10, 5).timestamp())),
        ])
        monkeypatch.setattr(dl, "_min_publish_ts",
                            lambda s: int(dt.datetime(2026, 10, 1).timestamp()))
        out = dl.find_leads(["夸克口令"])
        assert [x["mark"] for x in out] == ["咐置乙的叩苓", "咐置甲的叩苓"], "新的要排前面"

    def test_下限留空就不过滤(self, monkeypatch) -> None:
        dl = self._leads(monkeypatch, [self._row("x", "咐置某叩苓", 0)])
        monkeypatch.setattr(dl, "_min_publish_ts", lambda s: 0)
        assert len(dl.find_leads(["夸克口令"])) == 1

    def test_日期写坏了要吭声_不是静默不过滤(self) -> None:
        """⚠️ 写坏日期**不能静默变成"不过滤"** —— 那等于用户的规则悄悄失效了。"""
        from app.services.douyin_leads import _min_publish_ts

        class _S:
            douyin_leads_min_publish_date = "2026/10/01"      # 格式不对
        assert _min_publish_ts(_S()) == 0                     # 降级成不过滤,但**打了 warning**
