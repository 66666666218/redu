"""小红书**纯协议**源单测(2026-10-07)。

算法与"停更了自己怎么修"见 `doc/小红书纯协议-链路拆解.md`。
⚠️ 这个文件盯两件最容易静默出错的事:
  ① **"没登录" 与 "没搜到" 分开**(不然日志会把它说成"小红书没热度");
  ② **一次搜索的所有页共用一个 `search_id`**(实测:每页各新生成会让翻页混进重复)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import xhs_protocol_source as xp  # noqa: E402

CK = "a1=aaa; web_session=bbb; webId=ccc; id_token=ddd"


class _S:
    xhs_web_session = CK


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._p = payload
        self.text = text or (str(payload) if payload else "")

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p


def _ok(*titles):
    return {"success": True, "code": 0, "data": {"items": [
        {"id": f"n{i}", "note_card": {"display_title": t,
                                      "user": {"nickname": "某人", "user_id": "7"}}}
        for i, t in enumerate(titles)]}}


class _FakeXhshow:
    """桩签名器:**记下每次收到的 payload**,用来验证 `search_id` 是否复用。"""

    calls: list[dict] = []

    def get_search_id(self):
        return f"sid-{len(self.calls)}"

    def sign_headers_post(self, uri, ck, payload=None):
        _FakeXhshow.calls.append(dict(payload or {}))
        return {"x-s": "XYS_x", "x-s-common": "c", "x-t": "1"}


class TestCookieAndLogin:
    def test_没有凭据要抛错并给修法(self) -> None:
        class _No:
            xhs_web_session = ""
        with pytest.raises(xp.XhsProtocolError) as e:
            xp.search(["甲"], settings=_No(), session=None)
        assert e.value.kind == "need_login" and e.value.needs_human
        assert "xhs_export_cookie" in str(e.value)

    def test_缺关键项要提前判出来(self) -> None:
        """⚠️ 硬失败**只认 `a1`/`web_session`** —— 缺任一个必然不行,提前判出来比
        "发一次请求再回 -101" 好。而 `id_token` 缺失**只告警不当失败**:
        手抄那份确实缺它且回 -101,但那份**同时可能已过期**,归因证据不够硬。
        """
        class _NoSess:
            xhs_web_session = "a1=aaa"                  # 缺 web_session
        with pytest.raises(xp.XhsProtocolError) as e:
            xp.search(["甲"], settings=_NoSess(), session=None)
        assert e.value.kind == "need_login"

    def test_缺id_token只告警不拦(self, monkeypatch, caplog) -> None:
        """★ 反例:缺 `id_token` **不该**被拦死 —— 万一它其实能用,拦了就是自断。"""
        import requests
        import xhshow
        _FakeXhshow.calls = []
        monkeypatch.setattr(xhshow, "Xhshow", _FakeXhshow)
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(payload=_ok("甲")))
        monkeypatch.setattr(xp, "_GAP", 0.0)

        class _NoId:
            xhs_web_session = "a1=aaa; web_session=bbb"
        assert xp.search(["甲"], settings=_NoId(), session=None)

    def test_被判据认得出被踢下线(self) -> None:
        assert xp.needs_login("无登录信息，或登录信息为空")
        assert xp.needs_login("code=-101")
        assert xp.needs_login("检测到账号异常")
        assert xp.needs_login("电脑设备登录超限，请重新登录")
        assert not xp.needs_login("成功")


class TestSearchIdReuse:
    """★ **一次搜索的所有页必须共用同一个 `search_id`**。

    2026-10-07 控制变量实测:同 id 翻两页**交集 0**(真在翻);
    每页各新生成**交集 5**(静默重复) —— 不报错,只让"多少人在推"虚高。
    """

    def test_同一关键词的两页共用一个_search_id(self, monkeypatch) -> None:
        import requests
        import xhshow

        _FakeXhshow.calls = []
        monkeypatch.setattr(xhshow, "Xhshow", _FakeXhshow)
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(payload=_ok("甲", "乙")))
        monkeypatch.setattr(xp, "_GAP", 0.0)
        xp.search(["网盘资源"], settings=_S(), session=None, pages=2)
        sids = {c["search_id"] for c in _FakeXhshow.calls}
        assert len(_FakeXhshow.calls) == 2 and len(sids) == 1, _FakeXhshow.calls

    def test_不同关键词各自一个_search_id(self, monkeypatch) -> None:
        import requests
        import xhshow

        _FakeXhshow.calls = []
        monkeypatch.setattr(xhshow, "Xhshow", _FakeXhshow)
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(payload=_ok("甲")))
        monkeypatch.setattr(xp, "_GAP", 0.0)
        xp.search(["词一", "词二"], settings=_S(), session=None)
        assert len({c["search_id"] for c in _FakeXhshow.calls}) == 2


class TestErrors:
    def _run(self, monkeypatch, resp):
        import requests
        import xhshow
        _FakeXhshow.calls = []
        monkeypatch.setattr(xhshow, "Xhshow", _FakeXhshow)
        monkeypatch.setattr(requests, "post", lambda *a, **k: resp)
        monkeypatch.setattr(xp, "_GAP", 0.0)
        return xp.search(["甲"], settings=_S(), session=None)

    def test_461_是签名问题不是没搜到(self, monkeypatch) -> None:
        with pytest.raises(xp.XhsProtocolError) as e:
            self._run(monkeypatch, _Resp(status=461))
        assert e.value.kind == "sign" and "461" in str(e.value)

    def test_406_提示换XYW格式(self, monkeypatch) -> None:
        with pytest.raises(xp.XhsProtocolError) as e:
            self._run(monkeypatch, _Resp(status=406))
        assert e.value.kind == "sign" and "XYW" in str(e.value)

    def test_未登录响应要抛并标需人工(self, monkeypatch) -> None:
        with pytest.raises(xp.XhsProtocolError) as e:
            self._run(monkeypatch, _Resp(payload={"success": False, "code": -101,
                                                  "msg": "无登录信息"}))
        assert e.value.kind == "need_login" and e.value.needs_human

    def test_正常解析标题与作者(self, monkeypatch) -> None:
        rows = self._run(monkeypatch, _Resp(payload=_ok("标题甲", "标题乙")))
        assert [r["snippet"] for r in rows] == ["标题甲", "标题乙"]
        assert rows[0]["keyword"] == "甲" and rows[0]["url"].startswith("https://www.xiaohongshu.com/")

    def test_真的没搜到返回空(self, monkeypatch) -> None:
        """★ 反例:`success=True` 但没 items = **真的没有**,要返回空而不是抛错。"""
        assert self._run(monkeypatch, _Resp(payload=_ok())) == []


    def test_账号没权限要单独说清并标需人工(self, monkeypatch) -> None:
        """★ **`-104 没有权限访问`** 是**账号级风控**,与签名无关。

        2026-10-07 压测当场走完这条链路:连发 ~200 次 ⇒ 先 `461` ⇒ 再 `-100 登录已过期`
        ⇒ 重新登录后变成 `-104`。**修法是等它解封,不是改代码** ——
        所以必须单独成一类,别混进"接口出错"里让人去查签名。
        """
        with pytest.raises(xp.XhsProtocolError) as e:
            self._run(monkeypatch, _Resp(payload={"success": False, "code": -104,
                                                  "msg": "您当前登录的账号没有权限访问"}))
        assert e.value.kind == "restricted" and e.value.needs_human
        assert "等它解封" in str(e.value)


class TestWiring:
    def test_协议优先页面兜底(self, monkeypatch) -> None:
        from app.services import resource_presence as rp
        from app.services import xhs_page_source

        monkeypatch.setattr(xp, "search", lambda names, **k: (
            _ for _ in ()).throw(xp.XhsProtocolError("461", kind="sign")))
        monkeypatch.setattr(xhs_page_source, "search", lambda names: [
            {"keyword": names[0], "snippet": "兜底来的", "uid": "", "name": "",
             "url": "", "pan_link": ""}])
        out = rp._crawl_platform("xiaohongshu", ["甲"], session=None)
        assert out[0]["snippet"] == "兜底来的"

    def test_两条路都挂要给两个原因(self, monkeypatch) -> None:
        from app.services import resource_presence as rp
        from app.services import xhs_page_source
        from app.services.mediacrawler_source import MediaCrawlerError

        monkeypatch.setattr(xp, "search", lambda names, **k: (
            _ for _ in ()).throw(xp.XhsProtocolError("461", kind="sign")))
        monkeypatch.setattr(xhs_page_source, "search", lambda names: (
            _ for _ in ()).throw(xhs_page_source.XhsPageError("也要安全验证")))
        with pytest.raises(MediaCrawlerError) as e:
            rp._crawl_platform("xiaohongshu", ["甲"], session=None)
        assert "两条路都失败" in str(e.value)
