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


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """限速(_REQ_GAP)是给线上防封用的,测试里不该真等——每个请求省 4 秒。"""
    monkeypatch.setattr(cp, "_REQ_GAP", 0)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    # autoflush=False **与生产一致**(app/db/database.py 就是这么配的)。默认 True 会让
    # `select` 前先 flush pending,从而**掩盖"同一轮里重复 add 撞唯一键"这类 bug**——
    # 2026-10-02 实跑就在生产配置上撞到了(cross_platform_accounts 唯一键冲突)。
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
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


def test_search_bilibili_reads_user_search_and_flags_pan_account(monkeypatch) -> None:
    """B站走**搜用户**:拿 mid/uname/usign;号名或签名含网盘词 → looks_like_pan 置位。

    走搜用户而非搜视频的原因(2026-10-02 实测):视频搜索的 `description` 是空的
    (接口不返回视频简介),捞不到链;而搜用户能把**名字里就写着"网盘资源"**的号直接捞出。
    """
    class _Nav:
        def json(self):
            return {"code": -101, "data": {"wbi_img": {      # 未登录也照给 wbi_img
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png"}}}

    class _Search:
        def json(self):
            return {"code": 0, "data": {"result": [
                {"mid": 123, "uname": "网盘资源商行", "usign": "持续更新，加我QQ:12345"},
                {"mid": 456, "uname": "老王的日常", "usign": "记录生活"},        # 与网盘无关
                {"mid": 789, "uname": "小站", "usign": "夸克 https://pan.quark.cn/s/xyz"},
            ]}}

    import requests
    monkeypatch.setattr(requests, "get", lambda url, **k: _Nav() if "nav" in url else _Search())
    monkeypatch.setattr(cp, "_bili_mixin_cache", {"key": "", "ts": 0.0})   # 清缓存以强制走 nav

    hits = cp._search_bilibili("", "网盘资源")
    assert len(hits) == 3
    assert hits[0]["uid"] == "123" and hits[0]["url"] == "https://space.bilibili.com/123"
    assert hits[0]["looks_like_pan"] is True                # 号名明写网盘
    assert hits[1]["looks_like_pan"] is False               # 生活号,不受影响
    assert hits[2]["pan_link"].startswith("https://pan.quark.cn")   # 签名里有真链


def test_pan_account_hints_ignore_generic_word() -> None:
    """"资源"是泛词**不算**(会收进一大堆无关号);须命中"网盘"或具体网盘品牌。"""
    assert not any(h in "资源分享家" for h in cp._PAN_ACCOUNT_HINTS)
    assert any(h in "夸克网盘资源" for h in cp._PAN_ACCOUNT_HINTS)


def test_discover_keeps_bili_account_flagged_by_name(session, monkeypatch) -> None:
    """B站口径:号名/签名明写网盘就收(该平台搜索层给不出链),泛词不算。"""
    monkeypatch.setattr(cp, "SEARCHERS", {"bilibili": lambda ck, kw, limit=20: [
        {"uid": "m1", "name": "网盘资源商行", "url": "", "snippet": "",
         "pan_link": "", "looks_like_pan": True},
        {"uid": "m2", "name": "资源分享家", "url": "", "snippet": "",
         "pan_link": "", "looks_like_pan": False},      # 只含"资源"不算
    ]})
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {})
    out = cp.discover_cross_accounts(session, 1, keywords=["测试词"])
    assert out["new"] == 1
    rows = session.scalars(select(CrossPlatformAccount)).all()
    assert [r.name for r in rows] == ["网盘资源商行"]


def test_bili_mixin_is_cached(monkeypatch) -> None:
    """wbi mixin 要缓存:一轮里多次搜索不该反复打 nav(访问频率是红线)。"""
    calls = []

    class _Nav:
        def json(self):
            return {"data": {"wbi_img": {
                "img_url": "https://x/" + "a" * 32 + ".png",
                "sub_url": "https://x/" + "b" * 32 + ".png"}}}

    import requests
    monkeypatch.setattr(requests, "get", lambda url, **k: (calls.append(url), _Nav())[1])
    monkeypatch.setattr(cp, "_bili_mixin_cache", {"key": "", "ts": 0.0})
    first = cp._bili_mixin()
    second = cp._bili_mixin()
    assert first and first == second
    assert len(calls) == 1                                  # 第二次命中缓存,没有再请求


def test_discover_without_cookie_still_runs_anon_platforms(session, monkeypatch) -> None:
    """没配 Cookie 时:免 Cookie 的平台(B站)照跑,需要登录态的平台(知乎)跳过。"""
    called = []

    def _zhihu(ck, kw, limit=20):
        called.append("zhihu")          # 不该被调用
        return []

    monkeypatch.setattr(cp, "SEARCHERS", {
        "bilibili": lambda ck, kw, limit=20: [
            {"uid": "m1", "name": "UP主", "url": "", "snippet": "",
             "pan_link": "https://pan.quark.cn/s/b"}],
        "zhihu": _zhihu,
    })
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {})
    out = cp.discover_cross_accounts(session, 1, keywords=["测试词"])
    assert out["platforms"] == ["bilibili"] and out["new"] == 1
    assert called == []                 # 没 Cookie 就不去撞知乎的风控


def test_discover_dedupes_across_keywords_in_one_round(session, monkeypatch) -> None:
    """同一轮内**多个搜索词命中同一个号**不能撞唯一键。

    这是 2026-10-02 真实跑出来的 bug:三个行业词各搜一次,"夸克网盘资源"这类号会在
    "网盘资源"和"夸克网盘"两次搜索里**都出现**;`_save` 查重时前一次 add 还在 pending
    (生产 session 是 `autoflush=False`),于是重复 add → commit 时 IntegrityError →
    异常冒泡使**整轮白跑**。所以 fixture 特意配成 autoflush=False 才抓得住。
    """
    monkeypatch.setattr(cp, "SEARCHERS", {"bilibili": lambda ck, kw, limit=20: [
        {"uid": "same", "name": "网盘资源商行", "url": "", "snippet": "",
         "pan_link": "", "looks_like_pan": True},
    ]})
    monkeypatch.setattr(cp, "_account_keywords", lambda s: ["词A", "词B", "词C"])
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {})
    out = cp.discover_cross_accounts(session, 1, keywords=["占位"])
    assert out["found"] == 3                # 三次搜索都命中
    assert out["new"] == 1                  # 但入库只一次
    assert len(session.scalars(select(CrossPlatformAccount)).all()) == 1


def test_discover_noop_when_no_usable_platform(session, monkeypatch) -> None:
    """连免 Cookie 的平台都没有时才 no_cookie。"""
    monkeypatch.setattr(cp, "SEARCHERS", {})
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {})
    out = cp.discover_cross_accounts(session, 1, keywords=["测试词"])
    assert out["status"] == "no_cookie" and out["new"] == 0
