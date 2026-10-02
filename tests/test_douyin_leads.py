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
