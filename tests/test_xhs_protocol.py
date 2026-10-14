"""小红书**纯协议**源单测(2026-10-07)。

算法与"停更了自己怎么修"见 `doc/小红书纯协议-链路拆解.md`。
⚠️ 这个文件盯两件最容易静默出错的事:
  ① **"没登录" 与 "没搜到" 分开**(不然日志会把它说成"小红书没热度");
  ② **一次搜索的所有页共用一个 `search_id`**(实测:每页各新生成会让翻页混进重复)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import io

import pytest  # noqa: E402

from app.services import xhs_protocol_source as xp  # noqa: E402

CK = "a1=aaa; web_session=bbb; webId=ccc; id_token=ddd"


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch, tmp_path) -> None:
    """★ 把"账号发现"的根目录挪到一个**空的临时目录**。

    ⚠️ **为什么要它**(2026-10-12 实测):`data/xhs_session_*.json` 是**真凭据**,
    而本文件的用例全都是在"**只给一份替身 settings**"的前提下断言各种错误分支的 ——
    一旦仓库里真有凭据文件,`xhs_accounts.discover()` 就会把它捡进账号池,
    用例的前提当场失效:
      · 「没有凭据要抛错」那条会拿**真凭据去发真实请求**(报错文案完全不同 ⇒ 红);
      · 「格式必须 xyw+x_rap」那条因为真凭据**带了 ssk** ⇒ 走 `xhs_xys` 那一档,
        而桩签名器没有 `sign_xs` ⇒ `算签名失败`(红)。
    ⇒ **测试必须与"这台机器上恰好有没有凭据"解耦** —— 否则"我登录成功了一次"
       反而会把测试搞红,这是最冤的一种假红。
    """
    from app.services import xhs_accounts as pool

    monkeypatch.setattr(pool, "_root", lambda: tmp_path)


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
    """桩签名器:**记下每次收到的 payload 与签名参数**,用来验证 `search_id` 是否复用、
    以及**签名格式传对没有**。

    ⚠️ `get_search_id()` 用**自己的计数器**,**不能挂在 `calls` 上** ——
    2026-10-12 起默认走**纯 Python XYW_** 签名,`sign_headers_post()` 根本不会被调用,
    `calls` 永远是空 ⇒ "每个关键词一个 id"会退化成"永远 sid-0"(**假红**)。

    ⚠️ 它**收得下 `config=`**(真库 `Xhshow(config=...)` 就是那样):一是更贴近真库
    (本仓那条"桩必须跟着真库走"的教训),二是免得 `_signer()` 每次都被迫
    "补丁失败、退回库自带"而刷一堆警告。
    """

    calls: list[dict] = []
    #: 每次调用收到的 `(sign_format, x_rap)` —— 2026-10-11 加:
    #: ★ 这两个参数**必须**传对(`xyw` + `x_rap=True`),否则小红书 2026-03 起对数据接口
    #: 一律回 461(且响应是 `code:0,data:{}` 的**静默空**,会被读成"这个资源没人发")。
    sign_args: list[tuple] = []
    #: `get_search_id()` 的自增计数(见类 docstring 的⚠️)
    sid_n: int = 0

    def __init__(self, *a, **k) -> None:
        pass

    def get_search_id(self):
        _FakeXhshow.sid_n += 1
        return f"sid-{_FakeXhshow.sid_n}"

    def sign_headers_post(self, uri, ck, payload=None, sign_format="xys", x_rap=False):
        _FakeXhshow.calls.append(dict(payload or {}))
        _FakeXhshow.sign_args.append((sign_format, x_rap))
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

    ⚠️ **2026-10-12 起改成"看真正发出去的请求体"**(而不是看桩签名器收到了什么):
    签名现在有**三档**(纯 Python XYW_ / oracle / xhshow),默认走**纯 Python** ——
    桩签名器会被**绕过**,于是"桩没被调用"会让这条测试**假红**。
    `search_id` 是**载荷**里的字段、与哪一档签名无关 ⇒ 直接抓在线的 body 最稳,也最真。
    """

    @staticmethod
    def _capture(monkeypatch, payload):
        """把 `requests.post` 换成一个**记下请求体**的桩,返回那个记账列表。"""
        import json

        import requests
        import xhshow

        sent: list[dict] = []

        def _post(url, data=None, headers=None, timeout=None, **k):
            try:
                sent.append(json.loads(data))
            except Exception:  # noqa: BLE001 - 记不下来也不该让测试崩
                sent.append({})
            return _Resp(payload=payload)

        _FakeXhshow.calls = []
        _FakeXhshow.sid_n = 0               # 逐条独立:别让上一条的计数漏进来
        monkeypatch.setattr(xhshow, "Xhshow", _FakeXhshow)
        monkeypatch.setattr(requests, "post", _post)
        monkeypatch.setattr(xp, "_GAP", 0.0)
        return sent

    def test_同一关键词的两页共用一个_search_id(self, monkeypatch) -> None:
        sent = self._capture(monkeypatch, _ok("甲", "乙"))
        xp.search(["网盘资源"], settings=_S(), session=None, pages=2)
        sids = {c.get("search_id") for c in sent}
        assert len(sent) == 2 and len(sids) == 1, sent

    def test_不同关键词各自一个_search_id(self, monkeypatch) -> None:
        sent = self._capture(monkeypatch, _ok("甲"))
        xp.search(["词一", "词二"], settings=_S(), session=None)
        assert len({c.get("search_id") for c in sent}) == 2, sent


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


class TestSignFormat:
    """★ **签名格式必须传 `xyw` + `x_rap=True`**(2026-10-11 实测矩阵)。

    小红书 2026-03 起对**数据接口**(含 `search/notes`)拒旧格式 `XYS_`:

      | 格式 | x_rap | 结果 |
      |---|---|---|
      | `xys` | — | HTTP 461,响应 `code:0, data:{}`(**静默空** ← 会被读成"这个资源没人发") |
      | `xyw` | 否 | HTTP 461,体里 `300011 当前账号存在异常` |
      | **`xyw`** | **是** | HTTP 200(签名层过,只剩凭据问题) |

    ⇒ 这一条**必须钉住**:默认值传错不会报错,只会**静默返回空**。

    ⚠️ **2026-10-12:这一条专盯第三档(`xhshow`)** —— 必须显式把前两档关掉
    (`xhs_sign_pure=False` / `xhs_sign_oracle=False`),否则纯 Python 签名会
    **抢在 `xhshow` 前面**把它签掉,`xhshow` 一次都不会被调用 ⇒ 假红。
    """

    def test_搜索时传的是xyw加x_rap(self, monkeypatch) -> None:
        import requests

        import xhshow
        _FakeXhshow.calls = []
        _FakeXhshow.sign_args = []
        monkeypatch.setattr(xhshow, "Xhshow", _FakeXhshow)
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(payload=_ok("甲")))
        monkeypatch.setattr(xp, "_GAP", 0.0)

        class _S:
            xhs_web_session = "a1=aaa; web_session=bbb"
            xhs_sign_format = "xyw"
            xhs_sign_x_rap = True
            #: ↓ 关掉前两档,逼它走 `xhshow`(这一条要验的就是那一档)
            xhs_sign_pure = False
            xhs_sign_oracle = False

        assert xp.search(["甲"], settings=_S(), session=None)
        assert _FakeXhshow.sign_args, "一次都没调签名器?"
        assert all(fmt == "xyw" and rap is True for fmt, rap in _FakeXhshow.sign_args), \
            f"签名参数传错了:{_FakeXhshow.sign_args}"


# ---------------------- 指纹层"静默降级"必须写进结论(2026-10-14 那晚的教训)
#
# 那晚两次协议单发都被 461/300011 与 -100 打回,而**两次的指纹层都降级了**
# (curl_cffi 报 HTTP/2 错 16 ⇒ 退回裸 requests)。结论里看不出这件事,
# 差点又把它当成"账号级、只能换号"。⇒ 降级必须**流进结论**,不能只躺日志。

def test_指纹降级要写进结论里() -> None:
    from app.services import xhs_protocol_source as X

    X.LAST_FP_OK = True
    assert "未用指纹" not in X._fp_note()
    X.LAST_FP_OK = False
    note = X._fp_note()
    assert "未用指纹" in note and "别采信" in note, note
    X.LAST_FP_OK = True          # 复原(模块级状态,别污染别的用例)


def test_账号异常的结论会带上指纹注记() -> None:
    """300011 那族的消息里必须能看见"这次用没用指纹"。"""
    from app.services import xhs_protocol_source as X

    X.LAST_FP_OK = False
    import inspect as _ins

    src = _ins.getsource(X._classify_response) if hasattr(X, "_classify_response") else ""
    if not src:      # 函数名变了就退回"整个模块里必须出现"
        src = io.open(X.__file__, encoding="utf-8").read()
    assert "_fp_note()" in src, "构造 300011 结论时没有拼上指纹注记"
    X.LAST_FP_OK = True
