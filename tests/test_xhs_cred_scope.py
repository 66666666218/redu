"""小红书**凭据口径**:按域名收口 + 指纹与 cookie 同源(2026-10-14)。

## 这两条各自盯什么(都是当晚实测交的学费)
① **`ctx.cookies()` 不带 URL 会把档案里所有域名的 cookie 都带回来。**
   实测一枚用过的 Edge 档案回来 **41 枚 / 5700 字符**,其中 `elt`(1886)+ `lt`(1673)
   加一串 `_EDGE_S`/`SRCHUID`/`ANON`/`WLS` **全是微软/Bing 的**。后果有两个:
   · `Cookie` 头加其余头约 **8 KB** ⇒ 撞 HTTP/2 `MAX_HEADER_LIST_SIZE` ⇒
     服务端断开 ⇒ `curl_cffi` 报 **`curl: (16)`** ⇒ 整条链**静默退回裸 requests**
     (JA3 是 python-requests 的 ⇒ 那一层的结论**不可信**);
   · 请求本身**自曝**:真实浏览器绝不会把 `live.com` 的 cookie 发去小红书的域。
   ⇒ 按域名收口后是 **15 枚 / 779 字符**,`(16)` 当场消失(实测)。

② **指纹必须跟着 cookie 走。**
   库里那枚租户凭据**只放得下 cookie**,指纹没有落脚点 ⇒ 协议路退回硬编码
   `Chrome/138`,而会话是 **Edge/155** 签发的 ⇒ 正是本仓已定性过的
   「登录设备 ≠ 调用设备」(`300011`)。修法是 `fingerprint_of`(**存档优先、库里兜底**)。

⚠️ 全部用例**不碰网络**(浏览器与回声都用桩)。
"""
from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import xhs_cookie_export as CE  # noqa: E402

#: 一个小号/大号的对照数字(不是真实凭据,只为量宽度)
_CLEAN = {f"c{i}": "v" * 40 for i in range(15)}              #: 约 700 字符
_BLOATED = dict(_CLEAN, elt="x" * 1886, lt="y" * 1673)       #: 混了"微软"那两枚


class _FakeCtx:
    """最小 context 替身 —— 只实现 `cookies(url)`,并**记下调用时有没有带 url**。"""

    def __init__(self, by_url: dict | None = None, unscoped: dict | None = None) -> None:
        self._by_url = by_url or {}
        self._unscoped = unscoped or {}
        self.calls: list = []

    def cookies(self, url=None):
        self.calls.append(url)
        if url is None:
            #: 真实行为:不带 url ⇒ 返回**所有域名**的项(字典形式就够表达"混进来了")
            return [{"name": k, "value": v} for k, v in self._unscoped.items()]
        return [{"name": k, "value": v} for k, v in self._by_url.items()]


class TestCookieScope:
    """① 按域名收口。"""

    def test_必须带_url_去问浏览器(self) -> None:
        """★ 核心护栏:`ctx.cookies()` 不带 url 就是那个 bug。"""
        ctx = _FakeCtx(by_url={"a1": "A", "web_session": "04x"})
        CE.read_ctx_cookies(ctx)
        assert ctx.calls == [CE.COOKIE_URL], "必须按小红书域名问,不能问「全部域名」"

    def test_不拿别的域名的_cookie(self) -> None:
        """就算浏览器肯给,我们也只取小红书那条域的。"""
        ctx = _FakeCtx(by_url={"a1": "A"}, unscoped={"a1": "A", "elt": "x" * 1886,
                                                     "lt": "y" * 1673})
        got = CE.read_ctx_cookies(ctx)
        assert got == {"a1": "A"}, f"混进了别的域名:{sorted(got)}"

    def test_空值不要(self) -> None:
        """空值 cookie 发出去没有任何好处,只会把头发长。"""
        ctx = _FakeCtx(by_url={"a1": "A", "junk": ""})
        assert CE.read_ctx_cookies(ctx) == {"a1": "A"}


class TestCookieBloatGuard:
    """① 的另一半:**用了**污染的凭据要能看见(静默降级是本仓最贵的一类错)。"""

    def test_正常凭据不报警(self, caplog) -> None:
        from app.services import xhs_protocol_source as P

        with caplog.at_level("WARNING"):
            assert P._warn_if_cookie_bloated(_CLEAN) is False
        assert not caplog.records

    def test_混进别的域名要报警且点名成因(self, caplog) -> None:
        from app.services import xhs_protocol_source as P

        with caplog.at_level("WARNING"):
            assert P._warn_if_cookie_bloated(_BLOATED) is True
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "别的域名" in text, "报警要说清是「混进了别的域名」"
        assert "curl: (16)" in text, "要把后果(HTTP/2 头表 ⇒ 16)说出来,否则没人知道它多严重"

    def test_门槛在两个实测值之间(self) -> None:
        """★ 门槛不能拍脑袋:必须 **大于真凭据、小于污染凭据**(两个数都是实测的)。"""
        from app.services import xhs_protocol_source as P

        assert P.cookie_bytes(_CLEAN) < P._COOKIE_MAX < P.cookie_bytes(_BLOATED)


class TestFingerprintStore:
    """② 指纹与 cookie 同源(库里那枚凭据只放得下 cookie,指纹得另有落脚点)。"""

    @pytest.fixture()
    def db(self):
        """自建一个**内存库**(与别处的 DB 用例同一套做法,不碰真实 `data/`)。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db import models  # noqa: F401 - 建表前先导入映射
        from app.db.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        s = sessionmaker(bind=engine)()
        try:
            yield s
        finally:
            s.close()

    def test_存了能读回来(self, db) -> None:
        from app.services import xhs_accounts as A

        A.save_fp(db, 1, "Mozilla/5.0 (X11) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0",
                  {"sec-ch-ua": '"Microsoft Edge";v="155"'})
        db.commit()
        got = A.load_fp(db, 1)
        assert "Edg/155" in got["ua"] and got["hints"]["sec-ch-ua"]

    def test_空_ua_不写(self, db) -> None:
        """★ 宁可**没有**,也不要**编一份** —— 编错的自报比不报更糟。"""
        from app.services import xhs_accounts as A

        A.save_fp(db, 1, "真 UA", {})
        db.commit()
        A.save_fp(db, 1, "", {"sec-ch-ua": "x"})        #: 这次读不到 ⇒ 不许覆盖
        db.commit()
        assert A.load_fp(db, 1)["ua"] == "真 UA"

    def test_读不到回空字典不抛(self, db) -> None:
        from app.services import xhs_accounts as A

        assert A.load_fp(db, 987654) == {}


class TestFingerprintOf:
    """★ **存档优先、库里兜底** —— 优先级只此一处写死。"""

    def test_存档有就用存档(self, monkeypatch) -> None:
        from app.services import xhs_accounts as A

        ref = A.AccountRef(key="session_file:x", kind="session_file", user_id=1)
        monkeypatch.setattr(A, "ua_of", lambda r: "存档UA")
        monkeypatch.setattr(A, "hints_of", lambda r: {"h": "1"})
        monkeypatch.setattr(A, "load_fp", lambda s, u: {"ua": "库里UA"})
        assert A.fingerprint_of(ref, object()) == ("存档UA", {"h": "1"})

    def test_存档没有才回落库里(self, monkeypatch) -> None:
        """★ 这条正是租户那条路(池里唯一可用的那个)过去踩的坑。"""
        from app.services import xhs_accounts as A

        ref = A.AccountRef(key="cookie_store:1", kind="cookie_store", user_id=1)
        monkeypatch.setattr(A, "ua_of", lambda r: "")
        monkeypatch.setattr(A, "hints_of", lambda r: {})
        monkeypatch.setattr(A, "load_fp", lambda s, u: {"ua": "库里UA", "hints": {"h": "2"}})
        assert A.fingerprint_of(ref, object()) == ("库里UA", {"h": "2"})

    def test_没有_session_就不读库(self, monkeypatch) -> None:
        """没给 session 时**不猜** —— 直接回空,让调用方按"没有指纹"处理(它会留痕)。"""
        from app.services import xhs_accounts as A

        ref = A.AccountRef(key="cookie_store:1", kind="cookie_store", user_id=1)
        monkeypatch.setattr(A, "ua_of", lambda r: "")
        monkeypatch.setattr(A, "hints_of", lambda r: {})
        called: list = []
        monkeypatch.setattr(A, "load_fp", lambda s, u: called.append(u) or {"ua": "库里UA"})
        assert A.fingerprint_of(ref, None) == ("", {})
        assert called == []


class TestExportWritesBoth:
    """★ cookie 与指纹**必须同一次写入** —— 只写一半比不写更糟(错值看着像对的)。"""

    def test_导出会把指纹一起写(self, monkeypatch) -> None:
        from app.services import xhs_accounts as A

        written: dict = {}
        monkeypatch.setattr(CE, "read_profile",
                            lambda p=None: ({"a1": "A", "web_session": "04x", "webId": "W", "id_token": "T"}, "真UA", {"h": "1"}))
        monkeypatch.setattr("app.services.cookie_store.set_cookie",
                            lambda db, uid, plat, blob: written.update(cookie=blob))
        monkeypatch.setattr(A, "save_fp",
                            lambda db, uid, ua, hints=None: written.update(ua=ua, hints=hints))
        msg = CE.export_from_profile(object(), 1)
        assert written["cookie"].startswith("a1=A")
        assert written["ua"] == "真UA" and written["hints"] == {"h": "1"}
        assert "指纹" in msg, "**读了指纹就要说出来** —— 静默正是上次没人发现的原因"

    def test_没读到指纹要说出来(self, monkeypatch) -> None:
        from app.services import xhs_accounts as A

        monkeypatch.setattr(CE, "read_profile",
                            lambda p=None: ({"a1": "A", "web_session": "04x", "webId": "W", "id_token": "T"}, "", {}))
        monkeypatch.setattr("app.services.cookie_store.set_cookie", lambda *a, **k: None)
        monkeypatch.setattr(A, "save_fp", lambda *a, **k: None)
        assert "没读到指纹" in CE.export_from_profile(object(), 1)
