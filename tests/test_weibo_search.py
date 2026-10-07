"""微博**纯协议**搜索单测(2026-10-07)。

换成纯协议的理由:移动端 `m.weibo.cn/api/container/getIndex` **公开且稳定**、
**不需要任何签名**(不像抖音的 a_bogus、小红书的 x-s),只要一枚登录 cookie。
实测 16 张 card / 15 条带正文 ⇒ 省掉一个浏览器(更快、更省内存、没有指纹暴露面)。

⚠️ 这个文件最要紧的一条:**"没登录/被拦" 必须抛错,不能返回空** ——
否则日志会把它说成"微博没热度"(本仓反复栽的假阴性)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import weibo_search as ws  # noqa: E402


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _cards(*texts):
    return {"ok": 1, "data": {"cards": [
        {"mblog": {"text": t, "user": {"screen_name": "某人", "id": 7},
                   "reposts_count": 3}} for t in texts]}}


class _S:
    weibo_cookie = "SUB=abc"


class TestCookie:
    def test_没有登录态要抛错而不是搜空(self) -> None:
        """★ 没有 cookie 时接口**会返回空**(或干脆被挡)—— 那会被读成"没热度"。"""
        class _No:
            weibo_cookie = ""
        with pytest.raises(ws.WeiboSearchError) as e:
            ws.search(["甲"], settings=_No(), session=None)
        assert "登录态" in str(e.value)

    def test_空词表直接返回(self) -> None:
        assert ws.search([], settings=_S(), session=None) == []


class TestSearch:
    def _patch(self, monkeypatch, resp):
        import requests
        monkeypatch.setattr(requests, "get", lambda *a, **k: resp)
        monkeypatch.setattr(ws, "_GAP", 0.0)

    def test_正常解析标题与转发数(self, monkeypatch) -> None:
        self._patch(monkeypatch, _Resp(payload=_cards("<b>高性价比人生指南</b> 自取", "第二条")))
        rows = ws.search(["甲"], settings=_S(), session=None)
        assert len(rows) == 2
        assert rows[0]["snippet"].startswith("高性价比人生指南")     # 标签被剥掉
        assert "转3" in rows[0]["snippet"] and rows[0]["keyword"] == "甲"

    def test_被拦要抛错(self, monkeypatch) -> None:
        """HTTP 非 200 / 非 JSON(**风控页**)都不能静默成空。"""
        self._patch(monkeypatch, _Resp(status=403))
        with pytest.raises(ws.WeiboSearchError):
            ws.search(["甲"], settings=_S(), session=None)
        self._patch(monkeypatch, _Resp(status=200, text="<html>验证</html>"))
        with pytest.raises(ws.WeiboSearchError):
            ws.search(["甲"], settings=_S(), session=None)

    def test_ok不是1也要抛错(self, monkeypatch) -> None:
        self._patch(monkeypatch, _Resp(payload={"ok": 0, "msg": "登录"}))
        with pytest.raises(ws.WeiboSearchError):
            ws.search(["甲"], settings=_S(), session=None)

    def test_真的没搜到返回空(self, monkeypatch) -> None:
        """★ 反例:`ok=1` 但没卡片 = **真的没有**,必须返回空而不是抛错。"""
        self._patch(monkeypatch, _Resp(payload={"ok": 1, "data": {"cards": []}}))
        assert ws.search(["甲"], settings=_S(), session=None) == []


class TestWiring:
    def test_微博不再走浏览器(self) -> None:
        from app.services import resource_presence as rp

        assert "weibo" in rp.API_PLATFORMS
        assert "weibo" not in rp.browser_platforms_of(_S2())

    def test_微博走纯协议而不是_MediaCrawler(self, monkeypatch) -> None:
        from app.services import resource_presence as rp

        called: dict = {}
        monkeypatch.setattr(ws, "search", lambda names, **k: (
            called.update(names=list(names)) or [{"keyword": names[0], "snippet": "x",
                                                  "uid": "", "name": "", "url": "",
                                                  "pan_link": ""}]))
        out = rp._crawl_platform("weibo", ["网盘资源"], session=None)
        assert called["names"] == ["网盘资源"] and out[0]["snippet"] == "x"

    def test_纯协议被拦要转成平台硬失败(self, monkeypatch) -> None:
        from app.services import resource_presence as rp
        from app.services.mediacrawler_source import MediaCrawlerError

        def _boom(names, **k):
            raise ws.WeiboSearchError("没有微博登录态")
        monkeypatch.setattr(ws, "search", _boom)
        with pytest.raises(MediaCrawlerError) as e:
            rp._crawl_platform("weibo", ["甲"], session=None)
        assert "weibo" in str(e.value)


class _S2:
    presence_platforms = "xiaohongshu,kuaishou,tieba,weibo,bilibili"
