"""抖音推广线索单测(2026-10-02):判据(标题以《》开头)、去重、卡片推送。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")


class _Settings:
    """最小 settings 替身(只覆盖 push_leads 用到的字段)。"""
    feishu_webhook_admin = "https://example.com/hook"
    feishu_webhook = ""
    feishu_secret = ""


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
    """工具没装/不可用 → 空返回,不该抛异常拖垮调度。"""
    from app.services import douyin_leads as dl
    from app.services import mediacrawler_source as mc

    monkeypatch.setattr(mc, "available", lambda: (False, "未安装"))
    assert dl.find_leads(["x"]) == []


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
    assert sent["webhook"] == "https://example.com/hook"      # 推的是**管理员群**
    body = str(sent["card"])
    assert "https://www.douyin.com/video/1" in body
    assert "白泽的梦" in body


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
