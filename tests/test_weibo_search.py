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

    # ------------------------------------------------------------------
    # ★★★ 2026-10-13:`ok=-100` 到底是"登录态失效"还是"风控"?—— 靠响应里的 url 分辨
    #
    # 实测两种响应:
    #   cookie 失效 ⇒ HTTP 200 + {"ok": -100, "url": "https://passport.weibo.com/sso/signin?…"}
    #   不带 cookie ⇒ HTTP 432
    # 旧文案写的是「登录态**或**风控问题」,两件事并列 —— 而上层告警**只能靠"串里有没有
    # 『登录』两个字"**决定要不要叫人去重登,于是那句含糊话直接把方向带偏了。
    # ⇒ 现在:消息说准,**并给出 `kind` 让告警不用再猜**。
    # ------------------------------------------------------------------

    def test_ok为负且带登录页跳转_判登录态失效(self, monkeypatch) -> None:
        self._patch(monkeypatch, _Resp(payload={
            "ok": -100,
            "url": "https://passport.weibo.com/sso/signin?entry=wapsso&source=wapssowb"}))
        with pytest.raises(ws.WeiboSearchError) as e:
            ws.search(["甲"], settings=_S(), session=None)
        assert e.value.kind == "login_expired", e.value.kind
        msg = str(e.value)
        assert "登录态已失效" in msg and "登录页" in msg, msg
        assert "weibo_export_cookie" in msg, f"要给**走得通**的那一步:{msg}"

    def test_ok为负但没有登录页跳转_说疑似风控(self, monkeypatch) -> None:
        """反例:同样 `ok != 1`,但没有登录页跳转 ⇒ **不能说成登录态失效**(否则又是白跑)。"""
        self._patch(monkeypatch, _Resp(payload={"ok": -100, "msg": "系统繁忙"}))
        with pytest.raises(ws.WeiboSearchError) as e:
            ws.search(["甲"], settings=_S(), session=None)
        assert e.value.kind == "risk", e.value.kind
        msg = str(e.value)
        assert "疑似风控" in msg, msg
        assert "系统繁忙" in msg, "接口给的 msg 要带出来(不许吞)"
        # ⚠️ 这条**专门**钉住"MUST NOT 说成登录问题":措辞会决定告警给什么修法
        assert "登录态已失效" not in msg, f"没有证据别把它说成登录问题:{msg}"

    def test_不带cookie的432算登录态失效(self, monkeypatch) -> None:
        """实测:完全不带 cookie 时回 432 ⇒ 也是"要去拿一枚 cookie"。"""
        self._patch(monkeypatch, _Resp(status=432))
        with pytest.raises(ws.WeiboSearchError) as e:
            ws.search(["甲"], settings=_S(), session=None)
        assert e.value.kind == "login_expired", e.value.kind

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

    def test_kind要跟着异常传到告警层(self, monkeypatch) -> None:
        """★ 平台标的 `kind` **必须带上去** —— 告警靠它决定给哪种修法,丢了就只能靠文案猜。"""
        from app.services import resource_presence as rp
        from app.services.mediacrawler_source import MediaCrawlerError

        def _boom(names, **k):
            raise ws.WeiboSearchError("登录态已失效", kind="login_expired")
        monkeypatch.setattr(ws, "search", _boom)
        with pytest.raises(MediaCrawlerError) as e:
            rp._crawl_platform("weibo", ["甲"], session=None)
        assert getattr(e.value, "kind", "") == "login_expired", (
            "kind 没传上去 ⇒ 告警又只能靠「登录」两个字猜了")


class _S2:
    presence_platforms = "xiaohongshu,kuaishou,tieba,weibo,bilibili"


class Test原帖链接与互动数:
    """★★ **2026-10-16 用户报的**:「微博的有些东西没给啊不显示数据」——
    卡片上「对标作品」显示 `—(源未给链接)`、「对标数据」显示 `—`。

    真因是**两处都没往外带**(而数据本来就在手上):
      · `url` 写死成 `""`(帖子 id 就在响应里)⇒ 「对标作品」那一列**永远点不动**；
      · 互动数只塞进 `snippet` 文字里(`(转N)`),没进 `metrics` ⇒ 「对标数据」永远 `—`。
    """

    def _one(self, mblog: dict, monkeypatch):
        # ⚠️ 必须 patch **全局 `requests`**(`search()` 内部是函数内 `import requests`)
        #    —— patch `ws.requests` 会 AttributeError(模块顶层没这个名字)。
        import requests
        monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(
            payload={"ok": 1, "data": {"cards": [{"mblog": mblog}]}}))
        monkeypatch.setattr(ws, "_GAP", 0.0)
        return ws.search(["甲"], settings=_S(), session=None)[0]

    def test_原帖链接要出来_优先短_id(self, monkeypatch) -> None:
        r = self._one({"text": "资源[网盘链接]", "mblogid": "AbC123",
                       "user": {"screen_name": "某人", "id": 7}}, monkeypatch)
        assert r["url"] == "https://weibo.com/7/AbC123"

    def test_没有短_id时退到数字_id的详情页(self, monkeypatch) -> None:
        r = self._one({"text": "资源", "mid": "5354630474107703",
                       "user": {"screen_name": "某人", "id": 7}}, monkeypatch)
        assert r["url"] == "https://m.weibo.cn/detail/5354630474107703", "退路也要能开"

    def test_互动数要进_metrics(self, monkeypatch) -> None:
        r = self._one({"text": "资源", "mid": "1", "user": {"id": 7, "screen_name": "某人"},
                       "reposts_count": 27, "comments_count": 4, "attitudes_count": 116},
                      monkeypatch)
        assert r["metrics"] == {"share_count": 27, "comment_count": 4, "liked_count": 116}

    def test_互动数为0时不放进_metrics(self, monkeypatch) -> None:
        """⚠️ 填 0 会被读成"没人看" ⇒ 与 `cross_accounts._weibo_metrics` 同一条口径。"""
        r = self._one({"text": "资源", "mid": "1", "user": {"id": 7, "screen_name": "某人"},
                       "reposts_count": 0, "comments_count": 0, "attitudes_count": 0},
                      monkeypatch)
        assert r["metrics"] == {}


def test_发帖时间要带出来(monkeypatch) -> None:
    """★ 2026-10-18:微博那条链的「只搬新的」一直**形同虚设** —— 条目里**根本没带**
    `publish_at`(实测 42% 为空),而接口的 `mblog.created_at` 一直都在。

    ⚠️ 它的格式是 **RFC 风格** `Sun Oct 18 10:51:12 +0800 2026`,`fromisoformat` 认不了
    ⇒ `to_dt` 专门补了这一档(否则加了字段还是空)。
    """
    import requests

    from app.utils.timeutil import to_dt

    card = {"ok": 1, "data": {"cards": [{"mblog": {
        "text": "某资源 自取", "user": {"screen_name": "某人", "id": 7},
        "reposts_count": 3, "created_at": "Sun Oct 18 10:51:12 +0800 2026"}}]}}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload=card))
    monkeypatch.setattr(ws, "_GAP", 0.0)
    rows = ws.search(["甲"], settings=_S(), session=None)
    assert rows and rows[0]["publish_at"] > 0, rows
    assert to_dt(rows[0]["publish_at"]) == to_dt("2026-10-18 10:51:12")
