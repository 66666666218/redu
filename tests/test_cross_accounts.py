"""跨平台同类资源号发现单测(2026-10-01):知乎解析、盘链筛选、去重。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import CrossPlatformAccount, User
from app.services import cross_accounts as cp


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=1, username="admin", email="a@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def test_search_zhihu_skips_non_account_items_and_reads_pan_link(monkeypatch) -> None:
    """知乎返回里混着 hot_timing/ring_box 这类非账号条目,要跳过;
    盘链从标题+正文里判(用户口径:确认是推广网盘的才收)。"""
    class _R:
        def json(self):
            return {"data": [
                {"type": "hot_timing", "object": {}},                       # 非账号条目
                {"type": "search_result", "object": {
                    "author": {"name": "亚楼", "url_token": "ya-lou-1"},
                    "title": "资源整理", "content": "夸克 https://pan.quark.cn/s/abc 自取"}},
                {"type": "search_result", "object": {
                    "author": {"name": "路人", "url_token": "lu-ren"},
                    "title": "聊聊资源", "content": "大家平时用什么网盘?"}},   # 无盘链
            ]}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    hits = cp._search_zhihu("ck", "网盘资源")
    assert len(hits) == 2                       # 非账号条目被跳过
    assert hits[0]["uid"] == "ya-lou-1" and hits[0]["name"] == "亚楼"
    assert hits[0]["pan_link"].startswith("https://pan.quark.cn")
    assert hits[1]["pan_link"] == ""            # 光聊资源的没有链


def test_discover_keeps_only_accounts_with_pan_link(session, monkeypatch) -> None:
    """核心口径:只收录**内容里真含网盘链**的号,光聊资源的不算。"""
    monkeypatch.setattr(cp, "SEARCHERS", {"zhihu": lambda ck, kw, limit=20: [
        {"uid": "u1", "name": "资源号", "url": "https://www.zhihu.com/people/u1",
         "snippet": "x", "pan_link": "https://pan.quark.cn/s/real"},
        {"uid": "u2", "name": "路人", "url": "", "snippet": "y", "pan_link": ""},
    ]})
    monkeypatch.setattr("app.services.cookie_store.get_cookies",
                        lambda s, u: {"zhihu": "ck"})
    out = cp.discover_cross_accounts(session, 1, keywords=["测试词"])
    assert out["found"] == 1 and out["new"] == 1     # found 统计的是"带链"的命中(2 条里 1 条)
    rows = session.scalars(select(CrossPlatformAccount)).all()
    assert [r.name for r in rows] == ["资源号"]
    assert rows[0].hit_keyword == "测试词"


def test_discover_dedupes_by_platform_uid(session, monkeypatch) -> None:
    """同一账号重复发现不重复入库(唯一键 user+platform+uid)。"""
    monkeypatch.setattr(cp, "SEARCHERS", {"zhihu": lambda ck, kw, limit=20: [
        {"uid": "u1", "name": "资源号", "url": "", "snippet": "", "pan_link": "https://pan.quark.cn/s/a"},
    ]})
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {"zhihu": "ck"})
    assert cp.discover_cross_accounts(session, 1, keywords=["词A"])["new"] == 1
    assert cp.discover_cross_accounts(session, 1, keywords=["词B"])["new"] == 0
    assert len(session.scalars(select(CrossPlatformAccount)).all()) == 1


def test_discover_without_cookie_is_noop(session, monkeypatch) -> None:
    """没配该平台 Cookie 就跳过——不去撞别人的风控。"""
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {})
    out = cp.discover_cross_accounts(session, 1, keywords=["测试词"])
    assert out["status"] == "no_cookie" and out["new"] == 0
