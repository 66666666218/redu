"""小红书**出口(网络)与指纹**收口单测(2026-10-12)。

## 它盯的是什么
小红书那条链最后剩下的失败**不在算法层**,而在这两条:
① **出口 IP 与登录时不一致**(会话绑出口 IP);
② **TLS/HTTP2 指纹**(`requests` 的 JA3 一眼就被认出来)。

本文件盯的是 ① 的**收敛规则** —— 因为 fail 的方式极难归因:
凭据齐全(`04` 真档 + ssk)、签名也对(HTTP 200),但号一用就被判
`300011 当前账号存在异常` 并作废。光看 cookie 与签名永远查不出来。

⚠️ 全部用例**不碰网络**(回声服务也用桩)。
"""
from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import xhs_net as N  # noqa: E402


class _S:
    """最小的 settings 替身:只放本模块会读的那几个字段。"""

    xhs_proxy = ""
    xhs_use_proxy = True
    use_proxy = False
    proxy_url = ""
    proxy_user = ""
    proxy_pass = ""
    xhs_impersonate = ""


class TestProxyPrecedence:
    """★ 出口的**优先级**必须固定:`XHS_PROXY` → 全局隧道 → **系统代理**(浏览器用它)。"""

    def test_专用出口优先(self, monkeypatch) -> None:
        class S(_S):
            xhs_proxy = "127.0.0.1:9000"      # 不带 scheme
        monkeypatch.setattr(N, "system_proxy", lambda: {"http": "http://sys:1"})
        assert N.xhs_proxy_url(S()) == "http://127.0.0.1:9000"

    def test_带scheme就别再拼一次(self, monkeypatch) -> None:
        class S(_S):
            xhs_proxy = "socks5://u:p@h:1080"
        monkeypatch.setattr(N, "system_proxy", lambda: {})
        assert N.xhs_proxy_url(S()) == "socks5://u:p@h:1080"

    def test_没配专用就回落全局隧道(self, monkeypatch) -> None:
        class S(_S):
            use_proxy = True
            proxy_url = "1.2.3.4:8080"
            proxy_user = "u"
            proxy_pass = "p"
        monkeypatch.setattr(N, "system_proxy", lambda: {"http": "http://sys:1"})
        got = N.xhs_proxy_url(S())
        assert got.startswith("http://u:p@1.2.3.4:8080")

    def test_都没配就跟着系统代理走_因为浏览器用它(self, monkeypatch) -> None:
        """★ **这一条是修"出口不一致"的关键**:全局策略把 `requests` 的系统代理
        关掉了(`disable_env_proxies`),但**浏览器照样走系统代理** ⇒ 两边出口不同。
        所以小红书这里**要**主动去跟系统代理对齐。"""
        monkeypatch.setattr(N, "system_proxy",
                            lambda: {"http": "http://127.0.0.1:25703",
                                     "https": "http://127.0.0.1:25703"})
        assert N.xhs_proxy_url(_S()) == "http://127.0.0.1:25703"

    def test_关掉开关就直连(self, monkeypatch) -> None:
        class S(_S):
            xhs_use_proxy = False
            xhs_proxy = "1.2.3.4:1"
        monkeypatch.setattr(N, "system_proxy", lambda: {"http": "http://sys:1"})
        assert N.xhs_proxy_url(S()) == ""
        assert N.for_requests(S()) is None


class TestForPlaywright:
    """浏览器那条路的代理 —— ★ 必须与 `for_requests` **同源**。

    ⚠️ **2026-10-12 改了语义**(旧的"没显式配就返回 `None`"是个**真缺陷**):
    返回 `None` 会让 Chromium 去"**自动检测系统代理**",本机实测直接
    `net::ERR_PROXY_CONNECTION_FAILED` ⇒ **小红书登录页根本打不开**。
    而把**同一个代理显式**告诉浏览器 ⇒ HTTP 200。
    ⇒ 现在回落 `system_proxy()` 并显式传,与 `for_requests` 共用 `xhs_proxy_url()`。
    """

    def test_显式配了就用它(self, monkeypatch) -> None:
        monkeypatch.setattr(N, "system_proxy", lambda: {})
        class S(_S):
            xhs_proxy = "127.0.0.1:9000"
        assert N.for_playwright(S()) == {"server": "http://127.0.0.1:9000"}

    def test_没显式配就回落到系统代理并显式传给浏览器(self, monkeypatch) -> None:
        monkeypatch.setattr(N, "system_proxy",
                            lambda: {"http": "http://sys:1", "https": "http://sys:1"})
        #: ★ **不是 None** —— 传 None 等于让 Chromium 自己探测,实测会连不上
        assert N.for_playwright(_S()) == {"server": "http://sys:1"}

    def test_与协议路同源_这是本模块存在的理由(self, monkeypatch) -> None:
        monkeypatch.setattr(N, "system_proxy",
                            lambda: {"http": "http://sys:1", "https": "http://sys:1"})
        assert N.for_requests(_S())["https"] == N.for_playwright(_S())["server"]

    def test_关掉开关返回_None_表示直连(self, monkeypatch) -> None:
        monkeypatch.setattr(N, "system_proxy", lambda: {"http": "http://sys:1"})
        class S(_S):
            xhs_use_proxy = False
        assert N.for_playwright(S()) is None
        #: ⚠️ 直连**必须**配启动参数,光"不传 proxy"没用(Chromium 仍会探测系统代理)
        assert N.playwright_direct_args(), "直连时必须给浏览器显式参数"

    def test_direct_写法才是有效的那个启动参数(self) -> None:
        """★ **2026-10-22 实测订正**:

        | 方式                        | 结果                            |
        |-----------------------------|---------------------------------|
        | 不指定(走系统代理)          | ❌ `ERR_PROXY_CONNECTION_FAILED` |
        | `--no-proxy-server`         | ❌ **仍然** `ERR_PROXY_CONNECTION_FAILED` |
        | `--proxy-server=direct://`  | ✅ HTTP 200                      |

        旧断言写的是相反结论(`--no-proxy-server` 可靠、`direct://` 无效),
        当天用 Playwright 1.61 与 1.63 各跑一遍、连续三组对照,结果一致 ⇒ 以它为准。
        """
        assert N.playwright_direct_args() == ["--proxy-server=direct://"]


class TestEgressIp:
    """量出口 IP。⚠️ 全用桩 —— 这个用例**绝不能**真的发请求。"""

    class _R:
        def __init__(self, text: str) -> None:
            self.text = text

    def test_能从json里解出ip(self, monkeypatch) -> None:
        import requests

        monkeypatch.setattr(N, "for_requests", lambda s=None: None)
        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: self._R('{"query":"1.2.3.4","country":"CN"}'))
        ip, via = N.egress_ip()
        assert ip == "1.2.3.4" and "直连" in via

    def test_纯文本页也能解出ip(self, monkeypatch) -> None:
        import requests

        monkeypatch.setattr(N, "for_requests", lambda s=None: None)
        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: self._R("当前 IP：9.8.7.6 来自于 中国"))
        assert N.egress_ip()[0] == "9.8.7.6"

    def test_走代理要标注出来(self, monkeypatch) -> None:
        import requests

        monkeypatch.setattr(N, "for_requests",
                            lambda s=None: {"http": "http://p:1", "https": "http://p:1"})
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._R('{"ip":"5.5.5.5"}'))
        ip, via = N.egress_ip()
        assert ip == "5.5.5.5" and "代理" in via

    def test_全都量不到就回空且不抛(self, monkeypatch) -> None:
        import requests

        def _boom(*a, **k):
            raise RuntimeError("代理是死的")

        monkeypatch.setattr(N, "for_requests", lambda s=None: None)
        monkeypatch.setattr(requests, "get", _boom)
        ip, why = N.egress_ip()
        assert ip == "" and why            # 量不到要**说出原因**,别静默


class TestEgressEchoOrder:
    """★ **回声服务必须"国产优先"** —— 2026-10-14 实测踩到,代价是差点把整条排查带偏。

    本机是**分流路由**(国内直连 / 国外走梯子)。拿**境外**回声量"出口",量到的是
    **梯子**的出口(实测 5 次采样出 3 个 Linode IP);而小红书是**国内目标、走直连**,
    出口一直稳定在同一个家宽 IP(山东联通)上。⇒ 顺序一错就会得到**两个都错**的结论:
    「出口在漂」和「出口是境外机房段」。

    ⚠️ 这几条是护栏,别让顺序悄悄回到"境外优先"。
    """

    class _R:
        def __init__(self, text: str) -> None:
            self.text = text

    def test_国产回声排在境外之前(self) -> None:
        head = list(N._EGRESS_URLS[:len(N._EGRESS_URLS_DOMESTIC)])
        assert head == list(N._EGRESS_URLS_DOMESTIC)
        for u in N._EGRESS_URLS_FOREIGN:
            assert u not in head, f"境外回声 {u} 混进了前面 ⇒ 量到的是梯子出口"

    def test_登录探测取的前三个必须是国产(self) -> None:
        """★ `xhs_browser_login._probe_egress` 取的是 `_EGRESS_URLS[:3]`。

        它记进凭据存档的 `egress_ip` 是"登录时出口"的基准 —— 这里要是切到境外回声,
        存下来的就是**梯子的 IP**,之后比对"出口有没有变"全在比一个与账号无关的东西。
        """
        assert len(N._EGRESS_URLS_DOMESTIC) >= 3, "国产回声至少 3 个,否则 [:3] 会切到境外"
        assert list(N._EGRESS_URLS[:3]) == list(N._EGRESS_URLS_DOMESTIC[:3])

    def test_命中国产回声时不带境外告警(self, monkeypatch) -> None:
        import requests

        monkeypatch.setattr(N, "for_requests", lambda s=None: None)
        monkeypatch.setattr(requests, "get", lambda u, **k: self._R(
            "当前 IP：112.247.45.87 来自于：中国 山东 菏泽 联通"))
        ip, via = N.egress_ip()
        assert ip == "112.247.45.87"
        assert "国内回声" in via and "境外" not in via

    def test_回落到境外回声时说明里必须标明(self, monkeypatch) -> None:
        """★ 境外读数是**允许**的兜底,但绝不能冒充"小红书看到的出口"。"""
        import requests

        def _only_foreign(url, **k):
            if url in N._EGRESS_URLS_DOMESTIC:
                raise RuntimeError("国内回声不通(模拟)")
            return self._R('{"query":"5.6.7.8"}')

        monkeypatch.setattr(N, "for_requests", lambda s=None: None)
        monkeypatch.setattr(requests, "get", _only_foreign)
        ip, via = N.egress_ip()
        assert ip == "5.6.7.8"
        assert "境外回声" in via, "境外读数必须自报家门"


class TestEgressProfile:
    """出口**画像**。⚠️ 全用桩 —— 用例绝不真发请求。"""

    class _R:
        def __init__(self, text: str) -> None:
            self.text = text

    def test_能解出画像字段(self, monkeypatch) -> None:
        import requests

        monkeypatch.setattr(N, "for_requests", lambda s=None: None)
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._R(
            '{"query":"1.2.3.4","isp":"China Unicom","as":"AS4837 X",'
            '"hosting":false,"proxy":false,"mobile":false}'))
        d = N.egress_profile()
        assert d["query"] == "1.2.3.4" and d["hosting"] is False

    def test_机房段要能被认出来(self, monkeypatch) -> None:
        """★ 买代理最怕买到机房段却付住宅的钱 ⇒ 画像必须把 `hosting` 带回来。"""
        import requests

        monkeypatch.setattr(N, "for_requests", lambda s=None: None)
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._R(
            '{"query":"9.9.9.9","as":"AS14061 DigitalOcean","hosting":true}'))
        assert N.egress_profile()["hosting"] is True

    def test_量不到就回空字典不抛(self, monkeypatch) -> None:
        import requests

        def _boom(*a, **k):
            raise RuntimeError("回声服务挂了")

        monkeypatch.setattr(N, "for_requests", lambda s=None: None)
        monkeypatch.setattr(requests, "get", _boom)
        assert N.egress_profile() == {}

    def test_没有query字段就当没量到(self, monkeypatch) -> None:
        import requests

        monkeypatch.setattr(N, "for_requests", lambda s=None: None)
        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: self._R('{"status":"fail"}'))
        assert N.egress_profile() == {}


class TestEgressStability:
    """★ **出口是不是静态的** —— 本项目会话绑定出口 IP,轮换型代理用在登录态上
    就是"同一账号从不同 IP 跳" ⇒ 服务端按**盗号**处理。

    ⚠️ 这三条是"去买代理"这件事的验收标准,别让它悄悄退化。
    """

    def test_固定出口算静态(self, monkeypatch) -> None:
        monkeypatch.setattr(N, "egress_ip", lambda **k: ("1.1.1.1", "直连"))
        ips, ok = N.egress_stability(samples=5, gap_s=0)
        assert ips == {"1.1.1.1"} and ok == 5
        assert len(ips) == 1, "只有 1 个出口才算静态"

    def test_轮换出口要被认出来(self, monkeypatch) -> None:
        seq = iter(["1.1.1.1", "2.2.2.2", "3.3.3.3", "1.1.1.1", "2.2.2.2"])
        monkeypatch.setattr(N, "egress_ip", lambda **k: (next(seq), "代理"))
        ips, ok = N.egress_stability(samples=5, gap_s=0)
        assert len(ips) == 3 and ok == 5, "轮换型必须被看出来"

    def test_量不到的次数不计入成功(self, monkeypatch) -> None:
        seq = iter([("1.1.1.1", "a"), ("", "超时"), ("1.1.1.1", "a")])
        monkeypatch.setattr(N, "egress_ip", lambda **k: next(seq))
        ips, ok = N.egress_stability(samples=3, gap_s=0)
        assert ips == {"1.1.1.1"} and ok == 2

    def test_一次都量不到就回空集合(self, monkeypatch) -> None:
        monkeypatch.setattr(N, "egress_ip", lambda **k: ("", "代理是死的"))
        ips, ok = N.egress_stability(samples=4, gap_s=0)
        assert ips == set() and ok == 0

    def test_采样次数至少一次(self, monkeypatch) -> None:
        monkeypatch.setattr(N, "egress_ip", lambda **k: ("1.1.1.1", "a"))
        ips, ok = N.egress_stability(samples=0, gap_s=0)
        assert ok == 1, "传 0 也该量一次,别回空让人看不懂"


class TestFingerprintFallback:
    """★ 指纹是**增强**,不是依赖 —— `curl_cffi` 不在/坏了必须退回 `requests`。"""

    def test_没开指纹就走requests(self, monkeypatch) -> None:
        import requests

        from app.services import xhs_protocol_source as xp

        called: list[str] = []
        monkeypatch.setattr(requests, "post", lambda url, **k: called.append(url))
        monkeypatch.setattr(xp, "_xhs_proxies", lambda st: None)

        class S:
            xhs_impersonate = ""           # 关掉指纹

        xp._http_post("https://x/y", "{}", {"a": "1"}, S(), timeout=5)
        assert called == ["https://x/y"]

    def test_开了指纹但库不可用要退回requests(self, monkeypatch) -> None:
        """⚠️ 这是"别让换个发送器把整条链带崩"的护栏。"""
        import builtins

        import requests

        from app.services import xhs_protocol_source as xp

        got: list[str] = []
        monkeypatch.setattr(requests, "post", lambda url, **k: got.append(url))
        monkeypatch.setattr(xp, "_xhs_proxies", lambda st: None)

        real_import = builtins.__import__

        def _no_cffi(name, *a, **k):
            if name.startswith("curl_cffi"):
                raise ImportError("装了也不让用(模拟)")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_cffi)

        class S:
            xhs_impersonate = "chrome"

        xp._http_post("https://x/y", "{}", {"a": "1"}, S(), timeout=5)
        assert got == ["https://x/y"], "curl_cffi 不可用时必须退回 requests"


class TestCaBundle:
    """★ CA 证书包必须是**纯 ASCII 路径** —— 否则 `curl_cffi` 的 HTTPS 全军覆没。

    2026-10-12 定位(见 `xhs_net.ca_bundle` 的 docstring):
    `curl_cffi` 的 libcurl 是 **BoringSSL** 构建,加载 CA 走 **ANSI 的 `fopen`**
    ⇒ 路径里有非 ASCII 字符(本机用户名是中文)**就打不开**
    ⇒ `curl: (77) error adding trust anchors` ⇒ **所有 HTTPS 请求无一例外失败**。

    **隐蔽点**:HTTP 不需要证书 ⇒ 完全不受影响。此前所有验证都拿 `http://ip-api.com`
    做的 ⇒ "指纹伪装能用"被"验证通过"了,可小红书接口**全是 HTTPS**
    ⇒ 指纹层一直静默退回 `requests`,从没真正生效。
    """

    def test_拿到的路径是纯_ascii(self) -> None:
        from pathlib import Path

        ca = N.ca_bundle()
        assert ca, "必须拿到一个证书包路径"
        assert all(ord(c) < 128 for c in ca), f"路径含非 ASCII ⇒ BoringSSL 打不开:{ca}"
        assert Path(ca).exists(), f"文件不存在:{ca}"

    def test_内容与_certifi_那一份一致(self) -> None:
        from pathlib import Path

        import certifi

        ca = N.ca_bundle()
        blob = Path(ca).read_bytes()
        assert blob == Path(certifi.where()).read_bytes()
        assert blob.count(b"BEGIN CERTIFICATE") > 50, "证书太少,怕是个空壳"

    def test_重复调用拿同一份_不会反复重写(self) -> None:
        from pathlib import Path

        a = N.ca_bundle()
        mtime = Path(a).stat().st_mtime
        assert N.ca_bundle() == a
        assert Path(a).stat().st_mtime == mtime

    @pytest.mark.parametrize("s,want", [
        ("C:/Users/bob/cacert.pem", True),
        ("C:/Users/周万鹏/cacert.pem", False),
        ("D:/code/redian/data/x.pem", True),
        ("D:/代码/x.pem", False),
    ])
    def test_ascii_判定(self, s: str, want: bool) -> None:
        assert N._is_ascii(s) is want

    def test_http_post_会把证书包传给_curl_cffi(self, monkeypatch) -> None:
        """★ 光"算出来"没用,必须真的**传进去** —— 不传就又回到静默失效。"""
        from app.services import xhs_protocol_source as xp

        got: dict = {}

        class _Cffi:
            @staticmethod
            def post(url, **kw):
                got.update(kw)
                got.setdefault("_used", "cffi")

                class R:
                    status_code = 200
                    text = "{}"
                return R()

        class _Mod:
            requests = _Cffi

        monkeypatch.setitem(__import__("sys").modules, "curl_cffi", _Mod())
        monkeypatch.setitem(__import__("sys").modules, "curl_cffi.requests", _Cffi)
        monkeypatch.setattr(xp, "_xhs_proxies", lambda st: None)

        class S(_S):
            xhs_impersonate = "auto"

        xp._http_post("https://x/y", "{}",
                      {"user-agent": "Mozilla/5.0 Chrome/138.0.0.0 Safari/537.36"},
                      S(), timeout=5)
        assert got.get("_used") == "cffi", "应当走 curl_cffi,而不是退回 requests"
        assert got.get("verify"), "★ 必须把 CA 证书包传进去,否则 HTTPS 全失败"
        assert all(ord(c) < 128 for c in got["verify"])


# ------------------------------------------------ 死代理回落(2026-10-22)

class TestSystemProxyDeadPort:
    """系统代理**还活着吗** —— 死了就必须回落直连。

    2026-10-22 事故:代理软件退出后注册表残留 `127.0.0.1:7892`,
    而 `system_proxy()` 原来无条件把它交给浏览器 ⇒ 小红书页面路与 CDP 路
    一起 `ERR_PROXY_CONNECTION_FAILED`,整平台失败。
    """

    @staticmethod
    def _patch(monkeypatch, url: str, alive: bool) -> None:
        #: ⚠️ 读的是 `urllib.request`,不是 `requests.utils` —— 后者没导出
        #: `getproxies_registry`(requests 2.34),旧写法因此恒返 `{}`(2026-10-22)。
        import urllib.request as ur

        monkeypatch.setattr(ur, "getproxies_environment", lambda: {})
        monkeypatch.setattr(ur, "getproxies_registry",
                            lambda: {"http": url, "https": url})
        import app.utils.proxy as P

        monkeypatch.setattr(P, "system_proxy_alive", lambda u, **kw: alive)

    def test_死代理返回空字典(self, monkeypatch) -> None:
        self._patch(monkeypatch, "http://127.0.0.1:7892", alive=False)
        assert N.system_proxy() == {}
        assert N.for_playwright() is None          # → 调用方会带直连参数
        assert N.for_requests() is None

    def test_活着的代理照旧返回(self, monkeypatch) -> None:
        self._patch(monkeypatch, "http://127.0.0.1:7892", alive=True)
        assert N.system_proxy().get("https") == "http://127.0.0.1:7892"
