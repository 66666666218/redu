"""跨平台资源热度单测(2026-10-02):抓**资源名** → 网盘链从**资源库**匹配。

用户口径:"小红书可以只抓取资源名称,然后网盘链接可以从资源库里面匹配,别的也照这个模式"
—— 关键洞察:平台上**有没有链不重要**,只要有人在做同一个资源,库里就有链。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User, WechatArticle, WechatPanLink
from app.services import resource_presence as rp


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
    brand_name = "念飞思雪"
    presence_platforms = "xiaohongshu,kuaishou"
    presence_names = 3
    feishu_webhook_xiaohongshu = "https://example.com/xhs"
    feishu_webhook_kuaishou = "https://example.com/ks"
    feishu_webhook = ""
    feishu_secret = ""
    presence_enabled = True


def _mk_library(session, title, author, pan_url, my=""):
    a = WechatArticle(user_id=1, title=title, author=author,
                      url=f"https://mp.weixin.qq.com/s/{author}", pan_urls=pan_url,
                      my_pan_urls=my, created_at=datetime.now() - timedelta(days=1))
    session.add(a)
    session.flush()
    session.add(WechatPanLink(user_id=1, article_id=a.id, pan_url=pan_url))
    session.commit()


def test_platforms_of_parses_and_ignores_unknown() -> None:
    class _K:
        presence_platforms = "xiaohongshu, kuaishou ,, 不存在的, xiaohongshu"

    assert rp.platforms_of(_K()) == ["xiaohongshu", "kuaishou"]

    class _Empty:
        presence_platforms = ""

    assert rp.platforms_of(_Empty()) == []          # 空配置 = 不探(而不是默认探一堆)


def test_probe_groups_by_name_and_attaches_library_link(session, monkeypatch) -> None:
    """核心链路:平台搜到的内容按**搜索词**归组 → 回库里匹配链 → 只留有命中的。

    这里是这条路的**关键洞察**:平台内容里没有链没关系,链来自**我们自己的资源库**。
    """
    from app.services import mediacrawler_source as mc

    _mk_library(session, "花少2人格测试", "号甲", "https://pan.quark.cn/s/LIB",
                my="https://pan.quark.cn/s/OUR")
    monkeypatch.setattr(mc, "available", lambda: (True, "ok"))
    monkeypatch.setattr(rp, "library_names", lambda s, u, top: ["花少2人格测试", "没人做的资源"])
    monkeypatch.setattr(mc, "crawl", lambda plat, kws: (
        [{"keyword": "花少2人格测试", "snippet": "《花少2》中你和谁最像？", "name": "森*", "url": "u"},
         {"keyword": "花少2人格测试", "snippet": "花少2旅行人格测试", "name": "6***6", "url": "u2"}]
        if plat == "xiaohongshu" else []))

    out = rp.probe(session, 1, settings=_S())
    assert out["status"] == "ok"
    assert len(out["items"]) == 1                       # 快手没结果 → 不产生条目
    it = out["items"][0]
    assert it["name"] == "花少2人格测试" and it["label"] == "小红书" and it["count"] == 2
    assert it["link"]["my_link"] == "https://pan.quark.cn/s/OUR"   # ← 链是从**库里**来的
    assert len(it["samples"]) == 2


def test_probe_without_tool_is_noop(session, monkeypatch) -> None:
    """MediaCrawler 不可用 → 明确 no_tool,不报错(发现任务不该被工具拖垮)。"""
    from app.services import mediacrawler_source as mc

    monkeypatch.setattr(mc, "available", lambda: (False, "未安装"))
    monkeypatch.setattr(rp, "library_names", lambda s, u, top: ["甲"])
    assert rp.probe(session, 1, settings=_S())["status"] == "no_tool"


def test_library_link_prefers_wechat_then_discovered(session) -> None:
    """匹配口径与资源库一致:公众号优先(带验证强度),发现链补缺口;都没有 → 空。"""
    _mk_library(session, "某资源", "号甲", "https://pan.quark.cn/s/W")
    r = rp._library_link(session, 1, "某资源")
    assert r["pan_url"] == "https://pan.quark.cn/s/W" and r["source"] == "公众号"
    assert rp._library_link(session, 1, "库里完全没有的词") == {}


def test_push_items_cards_per_platform(monkeypatch) -> None:
    """**每个平台推自己的群**,卡片里给出**库里匹配到的可用链**(点开即用)。"""
    from app.services import feishu_client

    sent = []

    class _C:
        def __init__(self, webhook, secret):
            self.hook = webhook

        def send_card(self, card):
            sent.append((self.hook, card))
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)
    ok = rp.push_items([
        {"name": "花少2人格测试", "platform": "xiaohongshu", "label": "小红书", "count": 2,
         "samples": [], "link": {"my_link": "https://pan.quark.cn/s/OUR", "pan_url": ""}},
        {"name": "车机软件", "platform": "kuaishou", "label": "快手", "count": 1,
         "samples": [], "link": {}},
    ], _S())
    assert ok is True and len(sent) == 2
    hooks = {h for h, _ in sent}
    assert hooks == {"https://example.com/xhs", "https://example.com/ks"}
    xhs_card = next(c for h, c in sent if h == "https://example.com/xhs")
    assert "https://pan.quark.cn/s/OUR" in str(xhs_card)      # 可用链在卡片里
    ks_card = next(c for h, c in sent if h == "https://example.com/ks")
    assert "库内暂无链" in str(ks_card)                        # 库里没有就如实说
