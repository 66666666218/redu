"""Telegram 源单测(2026-10-01):页面解析、盘链提取、去重、开关。

所有用例都不联网:解析走内存里的 HTML 样本(结构对齐 t.me 的 `tgme_widget_message_*` 骨架,
与 CloudSaver 的 cheerio 选择器一致),抓取走 monkeypatch。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User, WechatArticle, WechatPanLink
from app.services import telegram_source as tg


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=1, username="admin", email="a@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _page(*messages: str) -> str:
    """拼一个 t.me 频道预览页;每条消息给 <div class="tgme_expandable_quote"> 之外的正文。"""
    wraps = "".join(
        f'<div class="tgme_widget_message_wrap js-widget_message_wrap">'
        f'<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="{mid}">'
        f'<div class="tgme_widget_message_bubble">'
        f'<div class="tgme_widget_message_text js-message_text" dir="auto">{body}</div>'
        f'<div class="tgme_widget_message_footer compact js-message_footer">'
        f'<time datetime="{ts}"></time></div></div></div></div>'
        for mid, body, ts in messages)
    return f'<html><body><div class="tgme_channel_info">…</div>{wraps}</body></html>'


def test_parse_messages_extracts_id_text_links_and_time() -> None:
    html = _page(("chan/101",
                  '最新《流浪地球3》4K<br/>夸克：<a href="https://pan.quark.cn/s/abc123">点击获取</a>',
                  "2026-10-01T10:30:00+00:00"))
    msgs = tg.parse_messages(html)
    assert len(msgs) == 1
    m = msgs[0]
    assert m["msg_id"] == "chan/101"
    assert "流浪地球3" in m["text"] and "<" not in m["text"]      # 标签已剥掉
    assert "https://pan.quark.cn/s/abc123" in m["links"]
    assert m["published_at"] == datetime(2026, 10, 1, 10, 30)


def test_parse_messages_keeps_order_and_limit() -> None:
    html = _page(("c/1", "第一条", "2026-10-01T01:00:00+00:00"),
                 ("c/2", "第二条", "2026-10-01T02:00:00+00:00"),
                 ("c/3", "第三条", "2026-10-01T03:00:00+00:00"))
    assert [m["msg_id"] for m in tg.parse_messages(html)] == ["c/1", "c/2", "c/3"]
    assert [m["msg_id"] for m in tg.parse_messages(html, limit=2)] == ["c/2", "c/3"]  # 取最近两条


def test_parse_messages_tolerates_broken_time() -> None:
    """时间解析失败不该让整条消息作废——盘链才是我们要的东西。"""
    msgs = tg.parse_messages(_page(("c/1", 'x <a href="https://pan.quark.cn/s/a">链</a>', "not-a-date")))
    assert msgs and msgs[0]["published_at"] is None


def test_store_keeps_only_messages_with_pan_links(session) -> None:
    """闲聊/纯文字消息不入库,否则资源库的共振榜会被稀释。"""
    items = [("chan", {"msg_id": "chan/1", "text": "今天聊聊天气", "links": [], "published_at": None}),
             ("chan", {"msg_id": "chan/2", "text": "资源来了",
                       "links": ["https://pan.quark.cn/s/keepme"], "published_at": None})]
    assert tg._store(session, 1, items) == 1
    rows = session.scalars(select(WechatArticle)).all()
    assert len(rows) == 1 and rows[0].source == "telegram" and rows[0].author == "@chan"
    assert "https://pan.quark.cn/s/keepme" in rows[0].pan_urls
    assert session.scalars(select(WechatPanLink)).all()[0].pan_url.startswith("https://pan.quark.cn/s/")


def test_store_dedupes_by_message_permalink(session) -> None:
    """频道页每次抓的都是最近 N 条:不按消息链接去重,每轮都会重写同一批。"""
    items = [("chan", {"msg_id": "chan/9", "text": "x",
                       "links": ["https://pan.quark.cn/s/same"], "published_at": None})]
    assert tg._store(session, 1, items) == 1
    assert tg._store(session, 1, items) == 0
    assert len(session.scalars(select(WechatArticle)).all()) == 1


def test_collect_tick_disabled_by_default(session) -> None:
    """默认关闭:本机出不了网,开着只会每轮刷失败日志。"""
    from config.settings import Settings

    out = tg.collect_tick(Settings(_env_file=None, is_dev=True), db=session)
    assert out["status"] == "disabled"


def test_collect_tick_without_channels(session) -> None:
    from config.settings import Settings

    out = tg.collect_tick(Settings(_env_file=None, is_dev=True, tg_enabled=True), db=session)
    assert out["status"] == "no_channels"


def test_collect_tick_fetches_and_stores(session, monkeypatch) -> None:
    from config.settings import Settings

    monkeypatch.setattr(tg, "fetch_channel", lambda ch, proxy="", limit=30: [
        {"msg_id": f"{ch}/1", "text": "资源", "links": ["https://pan.quark.cn/s/zzz"],
         "published_at": None}])
    settings = Settings(_env_file=None, is_dev=True, tg_enabled=True, tg_channels="@chan_a, chan_b")
    out = tg.collect_tick(settings, db=session)
    assert out["status"] == "ok" and out["channels"] == 2 and out["new"] == 2
    authors = sorted(a.author for a in session.scalars(select(WechatArticle)).all())
    assert authors == ["@chan_a", "@chan_b"]     # @ 前缀被剥掉后按原样记频道


def test_collect_tick_survives_one_channel_failing(session, monkeypatch) -> None:
    """一个频道挂了(网络抽风/频道改名)不该拖垮整轮。"""
    from config.settings import Settings

    def boom(ch, proxy="", limit=30):
        raise RuntimeError("连不上")

    monkeypatch.setattr(tg, "fetch_channel", boom)
    settings = Settings(_env_file=None, is_dev=True, tg_enabled=True, tg_channels="dead_chan")
    out = tg.collect_tick(settings, db=session)
    assert out["status"] == "ok" and out["failed"] == ["dead_chan"] and out["new"] == 0
