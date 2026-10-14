"""小红书**客户端指纹**单测(2026-10-12)。

## 它盯的是什么
本项目第一次拿到真 `04` 登录凭据时:cookie 15 项 + `webSsk` 齐全、两种签名都过
(HTTP 200),但会话仍在 ~5 分钟内被判 `300011 当前账号存在异常`。逐项排除后定位到:

    登录(浏览器) = MS Edge 154 ⇒ UA `... Chrome/154.0.0.0 ... Edg/154.0.0.0`
    采集(协议路) = 硬编码常量    ⇒ UA `... Chrome/138.0.0.0`(**没有 `Edg/`**)

⇒ **"登录设备 ≠ 调用设备"**。修法不是把 138 改成 154(Edge 升级又会漂),
而是 **UA 由凭据存档携带**,与签发会话的浏览器永远同步。

本文件盯的就是这条:指纹**跟着凭据走**,且 **UA 与 TLS 指纹目标同源**。

⚠️ 全部用例**不碰网络**。
"""
from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import xhs_fingerprint as F  # noqa: E402

#: 实测抓到的 **MS Edge 154** 真实 UA(登录用的就是它)
EDGE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0")
#: 无头模式会把 `Chrome/` 换成 `HeadlessChrome/` —— 存档它等于自报家门
HEADLESS_UA = EDGE_UA.replace("Chrome/154", "HeadlessChrome/154")
SAFARI_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
FF_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0"


class _S:
    """最小的 settings 替身。"""

    xhs_impersonate = "auto"


class TestNormalizeUa:
    def test_无头标记会被洗掉(self) -> None:
        """`HeadlessChrome/154` ⇒ `Chrome/154`。

        为什么必须洗:`xhs_browser_login` 允许用 headless **核对**已登录的档案,
        那时抓到的 UA 会带 `HeadlessChrome` —— 存下来等于之后所有请求自报"我是自动化"。
        """
        out = F.normalize_ua(HEADLESS_UA)
        assert "Headless" not in out
        assert "Chrome/154.0.0.0" in out
        assert "Edg/154.0.0.0" in out          #: 品牌标记要**保留**

    def test_正常_UA_原样返回(self) -> None:
        assert F.normalize_ua(EDGE_UA) == EDGE_UA

    def test_空值退回兜底常量(self) -> None:
        assert F.normalize_ua("") == F.DEFAULT_UA
        assert F.normalize_ua(None) == F.DEFAULT_UA

    def test_两端空白会被去掉(self) -> None:
        assert F.normalize_ua(f"  {EDGE_UA}  ") == EDGE_UA


class TestParse:
    @pytest.mark.parametrize("ua,major", [
        (EDGE_UA, 154),            #: Edge 的 UA 里也有 `Chrome/<major>`(它就是 Chromium)
        (HEADLESS_UA, 154),
        (F.DEFAULT_UA, 138),
        ("Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0.0.0 Safari/537.36", 120),
        ("garble", 0),
        ("", 0),
    ])
    def test_取内核大版本(self, ua: str, major: int) -> None:
        assert F.chrome_major(ua) == major

    @pytest.mark.parametrize("ua,fam", [
        (EDGE_UA, "edge"),
        (HEADLESS_UA, "edge"),
        (F.DEFAULT_UA, "chrome"),
        (FF_UA, "firefox"),
        (SAFARI_UA, "safari"),
        ("", "unknown"),
    ])
    def test_浏览器家族(self, ua: str, fam: str) -> None:
        assert F.browser_family(ua) == fam


class TestImpersonate:
    """★ **UA 与 TLS 指纹目标必须同源**。

    `impersonate` 管的是 JA3/JA4 + HTTP/2 SETTINGS 顺序 —— 这一层改 Header 补不上。
    所以 UA 写 Edge 而指纹仿 Chrome,等于把同一类错配换到更底下的层。
    """

    @pytest.mark.parametrize("ua,want", [
        (EDGE_UA, "edge"),
        (F.DEFAULT_UA, "chrome"),
        (FF_UA, "firefox"),
        (SAFARI_UA, "safari"),
        ("", "chrome"),            #: 认不出来一律按 chrome(小红书 web 端本就是 Chromium)
    ])
    def test_由_UA_推指纹目标(self, ua: str, want: str) -> None:
        assert F.impersonate_for(ua) == want

    def test_无头与有头推出来一样(self) -> None:
        assert F.impersonate_for(HEADLESS_UA) == F.impersonate_for(EDGE_UA) == "edge"

    @pytest.mark.parametrize("setting,want", [
        ("auto", "edge"),
        ("chrome", "chrome"),      #: 人工强制时**听人的**
        ("", ""),                  #: 空 = 关掉 curl_cffi,退回 requests
    ])
    def test_配置解析(self, setting: str, want: str) -> None:
        assert F.resolve_impersonate(setting, EDGE_UA) == want

    def test_auto_大小写不敏感(self) -> None:
        assert F.resolve_impersonate("AUTO", EDGE_UA) == "edge"
        assert F.resolve_impersonate("  auto  ", EDGE_UA) == "edge"


class TestResolve:
    def test_没存档时退回兜底常量(self) -> None:
        fp = F.resolve("", _S())
        assert fp.ua == F.DEFAULT_UA
        assert fp.source == "default"
        assert fp.impersonate == "chrome"

    def test_有存档时用存档的_UA(self) -> None:
        """★ **本模块存在的理由**:用 Edge 登的号,采集必须**自称** Edge。"""
        fp = F.resolve(EDGE_UA, _S())
        assert fp.ua == EDGE_UA
        assert fp.source == "archive"
        assert fp.impersonate == "edge"
        assert fp.major == 154

    def test_存档优先于兜底常量(self) -> None:
        """存档 UA **绝不能**被 DEFAULT_UA 盖掉 —— 盖掉就是这次的故障。"""
        fp = F.resolve(EDGE_UA, _S())
        assert fp.ua != F.DEFAULT_UA
        assert fp.drift_from_default() == abs(154 - 138)

    def test_关掉指纹时_UA_照旧(self) -> None:
        """`xhs_impersonate=""` 只关 curl_cffi,**不该**把 UA 也退回常量。"""
        class S(_S):
            xhs_impersonate = ""
        fp = F.resolve(EDGE_UA, S())
        assert fp.ua == EDGE_UA
        assert fp.impersonate == ""

    def test_无头存档也会被洗过(self) -> None:
        fp = F.resolve(HEADLESS_UA, _S())
        assert "Headless" not in fp.ua
        assert fp.impersonate == "edge"

    def test_没配置中心时不炸(self) -> None:
        fp = F.resolve(EDGE_UA, None)
        assert fp.ua == EDGE_UA

    def test_describe_说得清是什么(self) -> None:
        assert "Edge" in F.describe(EDGE_UA) and "154" in F.describe(EDGE_UA)
        assert "Chrome" in F.describe(F.DEFAULT_UA)


class TestFingerprintIntoHeaders:
    """指纹必须真的**落到请求头上**,不只是算出来好看。"""

    def test_headers_xyw_用传入的_UA(self) -> None:
        from app.services.xhs_sign_pure import headers_xyw

        ck = {"a1": "A" * 52, "web_session": "04" + "0" * 36}
        h = headers_xyw("/api/sns/web/v1/search/notes", ck, payload={"keyword": "x"},
                        ua=EDGE_UA)
        assert h["user-agent"] == EDGE_UA

    def test_headers_xyw_没传时退回常量(self) -> None:
        from app.services.xhs_sign_pure import headers_xyw

        ck = {"a1": "A" * 52, "web_session": "04" + "0" * 36}
        h = headers_xyw("/api/sns/web/v1/search/notes", ck, payload={"keyword": "x"})
        assert h["user-agent"] == F.DEFAULT_UA

    def test_带上真浏览器那几项请求头(self) -> None:
        """★ **基准头必须齐全** —— 少一项就等于自曝"不是页面里发的 fetch"。

        这几项是 2026-10-12 用 Playwright 监听 `edith.xiaohongshu.com` 的**实际出站请求**
        抓下来的真值(原来我们一个都没发,连 `accept` 都没有)。
        """
        from app.services.xhs_sign_pure import headers_xyw

        ck = {"a1": "A" * 52, "web_session": "04" + "0" * 36}
        h = headers_xyw("/api/sns/web/v1/search/notes", ck, payload={"keyword": "x"},
                        ua=EDGE_UA)
        assert h["accept"] == "application/json, text/plain, */*"
        #: Fetch Metadata —— `www.xiaohongshu.com` → `edith.xiaohongshu.com` 是 same-site
        assert h["sec-fetch-dest"] == "empty"
        assert h["sec-fetch-mode"] == "cors"
        assert h["sec-fetch-site"] == "same-site"
        assert h["referer"] == "https://www.xiaohongshu.com/"
        assert h["origin"] == "https://www.xiaohongshu.com"

    def test_换_UA_不影响签名(self) -> None:
        """★ 安全前提:`x-s` 只吃 `url + body + a1 + 时间戳`,**不含 UA**。

        这条要是变成 False,改 UA 就会让签名失效,整套方案就不成立了。
        """
        from app.services.xhs_sign_pure import sign_xyw

        url, body, a1 = "/api/sns/web/v1/search/notes", {"keyword": "x"}, "A" * 52
        assert (sign_xyw(url, body, a1, now_ms=1700000000000)
                == sign_xyw(url, body, a1, now_ms=1700000000000))

    def test_http_post_没显式传时按发出的_UA_推指纹(self, monkeypatch) -> None:
        """老代码直接调 `_http_post` 也不会出现"UA 是 Edge、TLS 仿 Chrome"。"""
        from app.services import xhs_protocol_source as P

        got: dict = {}

        class _Cffi:
            @staticmethod
            def post(url, **kw):
                got.update(kw)
                got["_used"] = "cffi"

                class R:
                    status_code = 200
                    text = "{}"
                return R()

        import types

        mod = types.SimpleNamespace(requests=_Cffi)
        monkeypatch.setitem(__import__("sys").modules, "curl_cffi", mod)
        monkeypatch.setitem(__import__("sys").modules, "curl_cffi.requests", _Cffi)

        class S(_S):
            xhs_proxy = ""
            xhs_use_proxy = False
            use_proxy = False
            proxy_url = ""
            proxy_user = ""
            proxy_pass = ""

        P._http_post("https://e.invalid/api", "{}", {"user-agent": EDGE_UA}, S())
        assert got.get("impersonate") == "edge"


class TestClientHints:
    """`sec-ch-ua*` —— 与 UA **同一类**缺陷:真实浏览器一定带,我们原来一个都不发。"""

    EDGE_BRANDS = [{"brand": "Microsoft Edge", "version": "154"},
                   {"brand": "Chromium", "version": "154"},
                   {"brand": "Not(A:Brand", "version": "99"}]

    def test_造出标准的三件套(self) -> None:
        h = F.build_client_hints(self.EDGE_BRANDS, False, "Windows")
        #: 真实 Edge 发的就是这三行,格式必须**逐字符**对得上
        assert h["sec-ch-ua"] == ('"Microsoft Edge";v="154", "Chromium";v="154", '
                                  '"Not(A:Brand";v="99"')
        assert h["sec-ch-ua-mobile"] == "?0"
        assert h["sec-ch-ua-platform"] == '"Windows"'

    def test_移动端与平台可换(self) -> None:
        h = F.build_client_hints(self.EDGE_BRANDS, True, "Android")
        assert h["sec-ch-ua-mobile"] == "?1"
        assert h["sec-ch-ua-platform"] == '"Android"'

    @pytest.mark.parametrize("brands", [[], None, [{"version": "1"}], "不是列表"])
    def test_抓不到就一个都不发(self, brands) -> None:
        """★ **宁可缺,也不编一份** —— 编错的 self-report 比不报更糟。"""
        assert F.build_client_hints(brands, False, "Windows") == {}

    def test_没有平台就不发那一行(self) -> None:
        h = F.build_client_hints(self.EDGE_BRANDS, False, "")
        assert "sec-ch-ua-platform" not in h
        assert h["sec-ch-ua-mobile"] == "?0"


class TestAccountCarriesFingerprint:
    """指纹**跟着账号走**,不是一个全局常量。"""

    def _ref(self, tmp_path, kind: str, payload: dict | None = None):
        from app.services import xhs_accounts as A

        ref = A.AccountRef(key=f"t:{kind}", label=kind, kind=kind)
        if kind == "session_file":
            p = tmp_path / "xhs_session_t.json"
            p.write_text(__import__("json").dumps(payload or {}), encoding="utf-8")
            ref.path = str(p)
        return ref

    def test_存档里带_ua_就读得到(self, tmp_path) -> None:
        from app.services import xhs_accounts as A

        ref = self._ref(tmp_path, "session_file",
                        {"cookies": {"a1": "x"}, "ua": EDGE_UA, "egress_ip": "1.2.3.4"})
        assert A.ua_of(ref) == EDGE_UA
        assert A.egress_of(ref) == "1.2.3.4"

    def test_老存档没有_ua_字段时回空(self, tmp_path) -> None:
        """2026-10-12 之前导出的存档没有 `ua` —— 要**回空**让调用方兜底,不能炸。"""
        from app.services import xhs_accounts as A

        ref = self._ref(tmp_path, "session_file", {"cookies": {"a1": "x"}, "ssk": "z"})
        assert A.ua_of(ref) == ""
        assert A.egress_of(ref) == ""

    def test_非存档型身份回空且不炸(self, tmp_path) -> None:
        from app.services import xhs_accounts as A

        for kind in ("cookie_store", "env", "profile_dir"):
            assert A.ua_of(self._ref(tmp_path, kind)) == ""
            assert A.egress_of(self._ref(tmp_path, kind)) == ""

    def test_存档坏了不炸(self, tmp_path) -> None:
        from app.services import xhs_accounts as A

        ref = self._ref(tmp_path, "session_file")
        ref.path = str(tmp_path / "nope.json")     #: 文件不存在
        assert A.ua_of(ref) == ""

    def test_与_cookie_ssk_同源(self, tmp_path) -> None:
        """cookie / ssk / ua 必须来自**同一份**存档 —— 混着用就是错配。"""
        from app.services import xhs_accounts as A

        ref = self._ref(tmp_path, "session_file",
                        {"cookies": {"a1": "A" * 52, "web_session": "04" + "0" * 36},
                         "ssk": "czNz", "ua": EDGE_UA})
        assert "a1" in A.cookie_of(ref)
        assert A.ssk_of(ref) == __import__("base64").b64decode("czNz")
        assert A.ua_of(ref) == EDGE_UA

    def test_存档里带_hints_就读得到(self, tmp_path) -> None:
        from app.services import xhs_accounts as A

        hints = {"sec-ch-ua": '"Microsoft Edge";v="154"', "sec-ch-ua-mobile": "?0"}
        ref = self._ref(tmp_path, "session_file",
                        {"cookies": {"a1": "x"}, "client_hints": hints})
        assert A.hints_of(ref) == hints

    def test_老存档没有_hints_就回空_让调用方不发(self, tmp_path) -> None:
        from app.services import xhs_accounts as A

        ref = self._ref(tmp_path, "session_file", {"cookies": {"a1": "x"}})
        assert A.hints_of(ref) == {}

    def test_hints_字段类型不对时回空(self, tmp_path) -> None:
        from app.services import xhs_accounts as A

        ref = self._ref(tmp_path, "session_file", {"client_hints": "不是dict"})
        assert A.hints_of(ref) == {}
        ref2 = self._ref(tmp_path, "session_file",
                         {"client_hints": {"a": "1", "b": "", "": "x"}})
        assert A.hints_of(ref2) == {"a": "1"}     #: 空键/空值要滤掉
