"""B站对标号**投稿标题**采集单测(2026-10-05)。

链路:59 个 B站网盘推广号 → 取它们最近投稿的**标题** → 落 `hot_source_items`
(`source="bili-pan"`)→ 选题 Agent 当候选 → 回**资源库**查"这个资源我们有没有"。
(用户口径:「b站如果没有链可以只采集标题,从资源库里搜然后完善」)

⚠️ 本文件**最要紧**的一条是 `TestRateLimitIsNotSilentEmpty`:
B站 `space` 接口匿名额度极低(实测连发即 `412`/`-352`),而**"被限流"与"这号没投稿"
都表现为"拿不到条目"** —— 一旦吞成空列表,"被挡住"就会被记成"正常空轮",
与[[静默失败 = 假成功]]完全同源。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import CrossPlatformAccount, HotSourceItem, User, SystemConfig
from app.services import bili_account_scan as bas


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
    bili_scan_enabled = True
    bili_scan_accounts_per_run = 1


def _mk_accounts(session, n: int = 3) -> None:
    for i in range(n):
        session.add(CrossPlatformAccount(user_id=1, platform="bilibili", uid=f"60000{i}",
                                         name=f"网盘号{i}", status="active"))
    session.commit()


class TestFetchUserTitles:
    def test_正常返回标题(self, monkeypatch) -> None:
        monkeypatch.setattr(bas, "_signed_get", lambda params, referer, cookie="": {
            "code": 0, "message": "OK",
            "data": {"list": {"vlist": [
                {"title": "野鹅敢死队 经典影片", "bvid": "BV1", "created": 100},
                {"title": "", "bvid": "BV2"},                       # 空标题要丢掉
                {"title": "赤橙黄绿青蓝紫 大陆老剧", "bvid": "BV3"}]}}})
        out = bas.fetch_user_titles("650752289")
        assert [t["title"] for t in out] == ["野鹅敢死队 经典影片", "赤橙黄绿青蓝紫 大陆老剧"]
        assert out[0]["url"] == "https://www.bilibili.com/video/BV1"

    def test_空mid不请求(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise AssertionError("空 mid 不该发请求")
        monkeypatch.setattr(bas, "_signed_get", _boom)
        assert bas.fetch_user_titles("") == []


class TestRateLimitIsNotSilentEmpty:
    """★ **本文件的重点** —— 限流绝不能吞成空列表。"""

    def test_限流码要抛异常(self, monkeypatch) -> None:
        for code in (-352, -412, -509):
            monkeypatch.setattr(bas, "_signed_get",
                                lambda params, referer, cookie="", _c=code: {
                                    "code": _c, "message": "请求过于频繁", "data": None})
            with pytest.raises(bas.BiliScanError):
                bas.fetch_user_titles("650752289")

    def test_非JSON即412拦截页也要抛(self, monkeypatch) -> None:
        """B站风控返回的是 **HTML 拦截页**(HTTP 412),`.json()` 会炸 ——
        ⚠️ 这里**走真的 `_signed_get`**(只把 mixin 与 requests 换掉),
        否则测的只是"我 monkeypatch 的东西会抛",等于没测到那句 `except` 包装。
        """
        import requests

        class _Resp:
            status_code = 412
            text = "<!DOCTYPE html><html>..."

            def json(self):
                raise ValueError("Expecting value: line 1 column 1")

        monkeypatch.setattr("app.services.cross_accounts._bili_mixin", lambda: "mixin")
        monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
        with pytest.raises(bas.BiliScanError) as ei:
            bas.fetch_user_titles("650752289")
        assert "非 JSON" in str(ei.value) and "412" in str(ei.value), \
            f"错误信息要能一眼看出是风控拦截,实际:{ei.value}"

    def test_其它非零码也要抛(self, monkeypatch) -> None:
        monkeypatch.setattr(bas, "_signed_get", lambda *a, **k: {
            "code": -404, "message": "啥都没有", "data": None})
        with pytest.raises(bas.BiliScanError):
            bas.fetch_user_titles("650752289")


class TestScanAccounts:
    def test_标题落进热榜表且source正确(self, session, monkeypatch) -> None:
        _mk_accounts(session, 1)
        monkeypatch.setattr(bas, "fetch_user_titles", lambda mid, **k: [
            {"title": f"资源{i}", "bvid": f"BV{i}", "url": f"u{i}", "created": 0}
            for i in range(1, 4)])
        out = bas.scan_accounts(session, 1, settings=_S())
        assert out["status"] == "ok" and out["titles"] == 3
        rows = session.scalars(select(HotSourceItem).order_by(HotSourceItem.rank)).all()
        # `rank` 从 1 开始(第 1 条最新)—— Agent 只取 `rank <= 10`,正好是"最近在推什么"
        assert [r.rank for r in rows] == [1, 2, 3]
        assert {r.source for r in rows} == {"bili-pan"}
        assert rows[0].extra == "网盘号0", "extra 要记账号名,排障时能认出是谁发的"

    def test_游标轮转_不会永远只扫第一个号(self, session, monkeypatch) -> None:
        _mk_accounts(session, 3)
        seen: list[str] = []
        monkeypatch.setattr(bas, "fetch_user_titles",
                            lambda mid, **k: seen.append(mid) or [])
        monkeypatch.setattr(bas.time, "sleep", lambda s: None)   # 别在单测里真睡
        for _ in range(4):
            bas.scan_accounts(session, 1, settings=_S())
            bas.advance_cursor(session, 1)
        assert seen == ["600000", "600001", "600002", "600000"], \
            f"游标要在 3 个号之间轮转,实际 {seen}"

    def test_没有对标号时返回no_accounts(self, session) -> None:
        assert bas.scan_accounts(session, 1, settings=_S())["status"] == "no_accounts"


class TestTickDoesNotAdvanceCursorOnFailure:
    def test_限流时游标不推_下一轮重来(self, session, monkeypatch) -> None:
        """⚠️ 被限流就**不能推游标** —— 否则那个号会被**永久跳过**,
        而它恰恰是"还没成功采过"的那个。"""
        _mk_accounts(session, 3)
        # ⚠️ tick 里是 `from app.db import get_session_local`(**调用时才 import**),
        # 所以要打到 `app.db` 上,打到本模块会 AttributeError。
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        monkeypatch.setattr(bas, "_settings", lambda: _S())
        monkeypatch.setattr(bas, "fetch_user_titles",
                            lambda mid, **k: (_ for _ in ()).throw(
                                bas.BiliScanError("B站限流 code=-352")))

        bas.bili_account_scan_tick(settings=_S())

        for key in (bas._CURSOR_KEY,):
            row = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
            assert row is None or row.value == "0", "限流那轮不该推游标"


class TestBiliCookie:
    """★ space 端点的风控**按 IP 类别**区别对待(受控实测):本机家宽可用、机房 IP 稳定
    `-352 风控校验失败`。登录态能显著放宽风控,所以 cookie 必须真的带上。

    取值口径与 `pan_discovery` 取夸克 cookie 一致:**先 cookie_store,再 `.env` 兜底**
    (后者是给远程用的 —— `user_cookies` 表不在 `remote_sync` 的同步清单里)。
    """

    class _SWithCookie:
        bili_scan_enabled = True
        bili_scan_accounts_per_run = 1
        bili_cookie = "SESSDATA=fromenv; bili_jct=x"

    def test_优先用cookie_store_其次env兜底(self, session) -> None:
        from app.services import cookie_store

        # 库里没有 → 用 .env
        assert bas._bili_cookie(session, 1, self._SWithCookie()) == "SESSDATA=fromenv; bili_jct=x"
        # 库里有 → 库里优先(与夸克那套口径一致)
        cookie_store.set_cookie(session, 1, "bilibili", "SESSDATA=fromdb; bili_jct=y")
        session.commit()
        assert bas._bili_cookie(session, 1, self._SWithCookie()) == "SESSDATA=fromdb; bili_jct=y"

    def test_两处都没有就返回空串而不是报错(self, session) -> None:
        assert bas._bili_cookie(session, 1, _S()) == "", "没配就匿名跑,不该抛"

    def test_扫描时cookie真的传给了抓取函数(self, session, monkeypatch) -> None:
        """防"写了取值函数却忘了传参" —— 那等于没配。"""
        from app.services import cookie_store

        _mk_accounts(session, 1)
        cookie_store.set_cookie(session, 1, "bilibili", "SESSDATA=db; bili_jct=y")
        session.commit()
        seen: list[str] = []
        monkeypatch.setattr(bas, "fetch_user_titles",
                            lambda mid, **k: seen.append(k.get("cookie", "<未传>")) or [])
        monkeypatch.setattr(bas.time, "sleep", lambda s: None)
        bas.scan_accounts(session, 1, settings=_S())
        assert seen == ["SESSDATA=db; bili_jct=y"], f"cookie 没传到抓取函数,实际 {seen}"


class TestCookieWhitespace:
    """★ 2026-10-05 **实测踩到**的坑:`.env` 的值在**行尾**,带 `\r` 是常态,
    而 requests 见到 header 头尾有空白会直接抛
    `Invalid leading whitespace, reserved character(s), or return character(s) in header value`
    —— 报错措辞完全看不出"是 cookie 带了回车"。
    第一次把 cookie 送上远程就是这么炸的。
    """

    def test_cookie带行尾回车不会炸且被strip(self, monkeypatch) -> None:
        import requests

        seen: dict = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"code": 0, "message": "OK", "data": {"list": {"vlist": []}}}

        def fake_get(url, headers=None, timeout=None):
            seen.update(headers or {})
            return _Resp()

        monkeypatch.setattr("app.services.cross_accounts._bili_mixin", lambda: "mixin")
        monkeypatch.setattr(requests, "get", fake_get)
        bas.fetch_user_titles("123", cookie="SESSDATA=abc; bili_jct=x\r\n  ")
        assert seen.get("Cookie") == "SESSDATA=abc; bili_jct=x", \
            f"cookie 必须 strip 掉行尾回车/空格,实际 {seen.get('Cookie')!r}"

    def test_空cookie不设header(self, monkeypatch) -> None:
        import requests

        seen: dict = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"code": 0, "data": {"list": {"vlist": []}}}

        monkeypatch.setattr("app.services.cross_accounts._bili_mixin", lambda: "mixin")
        monkeypatch.setattr(requests, "get",
                            lambda url, headers=None, timeout=None: (seen.update(headers or {}), _Resp())[1])
        bas.fetch_user_titles("123", cookie="   ")
        assert "Cookie" not in seen, "空白 cookie 不该设出空 header"
