# -*- coding: utf-8 -*-
"""小红书:**出口(网络)与客户端指纹**的统一收口(2026-10-12)。

## 为什么单独一个模块
小红书那条链最后剩下的两种失败,**都不在算法层**:

### ① 出口 IP 与登录时不一致(本模块主要解决这个)
小红书的会话**绑定出口 IP**(与本项目闲鱼那条同理 —— 那条注释就写着
"`mtop token/session` 绑定出口 IP,勿用轮换代理池")。

而本项目有一条**全局策略**:`app/platform.py` / `app/main.py` 会调
`app.utils.proxy.disable_env_proxies()`,把 `requests` 读系统代理的能力**关掉**
(2026-09-29 事故加固:代理软件退出后留下死端口 `127.0.0.1:7892`,毒杀全部采集)。

于是出现这个**错配**:

```
  登录  → 走浏览器              → 浏览器**用系统代理**  → 出口 A
  采集  → 走服务内的 requests    → 被强制**直连**        → 出口 B   ← 与 A 不同
```

⇒ 出口一变,服务端回 `300011 当前账号存在异常` 并作废会话。
   表现就是"刚登上的号 5 分钟就 `-100 登录已过期`",而签名其实全对。

本模块让**两条路显式收敛到同一个出口**:

```
  XHS_PROXY(小红书专用固定出口) → 全局隧道(USE_PROXY+PROXY_URL) → **系统代理**(浏览器用它)
```

⚠️ 最后一级回落**系统代理**是刻意的,而且只对小红书这样:全局策略禁用它是因为
"代理软件可能留死端口",但**浏览器不听我们的**——它照样走系统代理。
两边不一致比"死代理"更糟:死代理会让采集**明明白白失败**,而出口不一致会让
**账号被判异常**(症状是"凭据全对却取不到数",极难归因)。
死端口那一层由 `_http_post` 的失败日志兜住。

### ② TLS / HTTP2 指纹
`requests` 的 JA3 是 `python-requests` 自己的,与 Chrome 差得很远,
**改 Header 补不上**(服务端看的是 TLS 握手包)。这一层由
`curl_cffi` 的 `impersonate` 解决,见 `xhs_protocol_source._http_post`。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from app.utils import get_logger

logger = get_logger(__name__)


def _root() -> Path:
    """仓库根(`app/services/` 往上两级)。"""
    return Path(__file__).resolve().parents[2]

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

#: 量"出口 IP"用的几个回声服务(按序尝试,取第一个通的)。
#:
#: ⚠️ **顺序是实测定的,别随手改**(2026-10-12):本机那条出口(`127.0.0.1:25703`)
#: 会把 **HTTPS 的回声服务全部挡掉**(`api.ipify.org` → `ERR_CONNECTION_RESET`、
#: `myip.ipip.net` / `taobao.com/getip` → 超时),**只有 `http://ip-api.com/json` 通**。
#: ⇒ 它必须**排第一**:否则每条都要先等 15 秒超时才轮到它,浏览器那侧常常
#: 还没轮到就已经超时放弃了(实测就这么白跑过一轮)。
#: ★★ **回声服务必须分组,国产的排前面**(2026-10-14 实测踩到,代价是差点把整条
#: 排查带偏):本机是**分流路由**(国内直连 / 国外走梯子)。拿**境外**回声去量"出口",
#: 量到的是**梯子那个出口** —— 它反复变、而且是境外机房段(实测 5 次采样出 3 个
#: Linode IP),读数**与"小红书看到了什么"毫无关系**:小红书是**国内目标、走直连**,
#: 一直稳定在同一个家宽 IP 上。
#: ⇒ 先用国产回声(= 国内目标真正看到的那条路),境外的只作兜底,**且要标出来**。
_EGRESS_URLS_DOMESTIC = (
    "https://myip.ipip.net",               #: 纯文本 + 省市/ISP,信息量最大,首选
    "https://ip.3322.net",
    "https://www.taobao.com/help/getip.php",
)
#: 兜底(前一组全不通时才用)。⚠️ 它量的是**境外**那条路 ⇒ 可能不是目标看到的那个出口。
_EGRESS_URLS_FOREIGN = (
    "http://ip-api.com/json",              #: 只有它带 hosting/proxy 字段(见 egress_profile)
    "https://api.ipify.org?format=json",
)
#: ⚠️ 保持"国产在前"的顺序 —— `xhs_browser_login._probe_egress` 取的是 `[:3]`,
#: 序号一改,登录时记进凭据存档的那枚 `egress_ip` 就会变成梯子的 IP。
_EGRESS_URLS = _EGRESS_URLS_DOMESTIC + _EGRESS_URLS_FOREIGN


def system_proxy() -> dict[str, str]:
    """读**系统代理**(环境变量 → Windows 注册表),与 `requests` 自己的优先级一致。

    ⚠️ 这里**故意绕开** `requests.utils.getproxies` —— 那个函数被
    `disable_env_proxies()` 打成了恒返 `{}`(全局策略)。而我们要的恰恰是
    **浏览器正在用的那个**系统代理,只能自己去读。

    ★★ **2026-10-22:读完必须探活**。代理软件退出后注册表 `ProxyEnable=1` 会残留
    一条死端口(实测 `127.0.0.1:7892`)—— 而本模块原来的口径是"浏览器用它,所以我们也用",
    于是小红书两条页面路一起 `ERR_PROXY_CONNECTION_FAILED`。
    死代理**不是**"出口不一致"那种隐性伤害,而是让整条链明明白白地发不出去:
    此时**直连**永远是更正确的选择(目标全是国内站)。
    ⇒ 探活不通就返回 `{}`,调用方自然回落直连。

    ★ **2026-10-22 二次修正:必须读 `urllib.request`,不是 `requests.utils`**。
    `requests` 2.34 里 `requests.utils.getproxies` 只是 `urllib.request.getproxies`
    的别名,而 `getproxies_registry` **根本没被导出到 `requests.utils`**
    (实测 `dir()` 里没有它)。旧写法对它 `getattr(...)` 抛异常、被宽 `except` 吞掉,
    结果**恒返 `{}`** —— 与"这里读的是系统代理"的注释正好相反。
    改成直接问 `urllib.request`:它不受 `disable_env_proxies()` 影响
    (那个 patch 只改 `requests.utils` 命名空间里的名字),正是本函数要的语义。
    """
    try:
        import urllib.request as ur

        got: dict[str, str] = dict(ur.getproxies_environment() or {})
        if not got:
            got = dict(ur.getproxies_registry() or {})
    except Exception:  # noqa: BLE001 - 非 Windows 没有 registry 那个
        return {}
    out = {k: v for k, v in got.items() if v}
    url = out.get("https") or out.get("http") or ""
    if url:
        from app.utils.proxy import system_proxy_alive

        if not system_proxy_alive(url):
            logger.warning("系统代理 %s 不可达(代理软件已退出?)⇒ 小红书改直连", url)
            return {}
    return out


def _normalize(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    return u if "://" in u else "http://" + u


def xhs_proxy_url(settings=None) -> str:
    """小红书该走的那**一个**出口(单条 URL);空串 = 直连。

    ⚠️ **绝不回落"提取式轮换池"**(`app.utils.get_proxies`)—— 那个池每个 IP 约 3 分钟
    就换一次,而小红书会话绑出口 IP;换出口 = 主动把账号送进 `300011`。
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    if not bool(getattr(settings, "xhs_use_proxy", True)):
        return ""

    raw = str(getattr(settings, "xhs_proxy", "") or "").strip()
    if raw:
        return _normalize(raw)

    try:
        from app.utils.proxy import proxy_url       #: 只用**隧道**那支(固定出口)

        got = proxy_url(settings)
    except Exception:  # noqa: BLE001
        got = None
    if got:
        return _normalize(got)

    #: 最后回落**系统代理** —— 因为浏览器就是用它(见模块 docstring)
    sysp = system_proxy()
    return _normalize(sysp.get("https") or sysp.get("http") or "")


def for_requests(settings=None) -> dict[str, str] | None:
    """给 `requests` / `curl_cffi` 用的 proxies 字典;`None` = 直连。"""
    u = xhs_proxy_url(settings)
    return {"http": u, "https": u} if u else None


def for_playwright(settings=None) -> dict[str, str] | None:
    """给 `launch_persistent_context(proxy=...)` 用的字典。**与 `for_requests` 同源**。

    ★★ **2026-10-12 修**(实测定位,别改回去):
    原来在没有 `XHS_PROXY`、且全局 `USE_PROXY=False` 时返回 `None`,
    意思是"让浏览器按系统设置自己走"。**实测这台机器上登录页根本打不开**:
    Chromium 的"自动检测系统代理"拿到的是错的东西,直接
    `net::ERR_PROXY_CONNECTION_FAILED`(而**同一个代理显式告诉它**就正常)。

    实测对照(导航到 `https://www.xiaohongshu.com/`):

    | 给浏览器的方式                      | 结果                            |
    |-------------------------------------|---------------------------------|
    | 不指定(让它自己检测系统代理)       | ❌ `ERR_PROXY_CONNECTION_FAILED` |
    | `proxy={"server": "http://127.0.0.1:25703"}` | ✅ HTTP 200             |
    | `args=["--no-proxy-server"]`(直连)  | ✅ HTTP 200                     |
    | `proxy={"server": "direct://"}`      | ❌ 不生效,仍走系统代理探测      |

    ⇒ 改成与 `for_requests` **共用 `xhs_proxy_url()`**(它会回落到系统代理),
    于是"两条路同一个出口"才是**真的成立**,而不是只在文档里写着。

    ⚠️ 返回 `None` 表示**走直连** —— 但 `proxy=` 不传**不等于**直连(见上表),
    所以调用方必须同时带上 `playwright_direct_args()`,否则又会掉进自动探测的坑。
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    if not bool(getattr(settings, "xhs_use_proxy", True)):
        return None
    u = xhs_proxy_url(settings)
    return {"server": u} if u else None


def playwright_direct_args() -> list[str]:
    """`for_playwright()` 返回 `None`(走直连)时,**必须**一并给浏览器的启动参数。

    ★★ **2026-10-22 实测把这里的结论改了过来(别再照抄旧注释)**。
    同一台机器、同一条死代理(`127.0.0.1:7892`)、Playwright 1.61 与 1.63 各测一遍:

    | 给浏览器的方式                          | 结果                             |
    |-----------------------------------------|----------------------------------|
    | 不指定(让它自己走系统代理)              | ❌ `ERR_PROXY_CONNECTION_FAILED`  |
    | `--no-proxy-server`                     | ❌ **仍是** `ERR_PROXY_CONNECTION_FAILED` |
    | `--proxy-server=direct://`              | ✅ HTTP 200                       |

    旧注释写的正好相反(说 `direct://` 不生效、`--no-proxy-server` 可靠)。
    当时为什么测出相反结果已无从复现,但**这次是两套 Playwright、连续三组对照**,
    以它为准 —— 判据统一成 `direct://`。
    """
    return ["--proxy-server=direct://"]


def egress_ip(settings=None, timeout: int = 15) -> tuple[str, str]:
    """量"**协议路**发出去时的公网 IP"。返回 `(ip, 说明)`;量不到回 `("", 原因)`。

    用途:`tools/xhs_egress.py` 把它与**浏览器**量到的 IP 比对 ⇒ 判断两条路
    是不是同一个出口(这是"刚登上的号就被判异常"最常被漏掉的一条)。

    ⚠️ **为什么要整体重试一轮**(2026-10-12 修):`ip-api.com` 免费档有速率限制,
    连着量会**偶发**失败。而它排在 `_EGRESS_URLS` 第一位、是本机**唯一**能通的那个,
    它一失败就会顺位掉到 `api.ipify.org`(HTTPS,本网络不通)⇒ 表现为"量不到出口"。
    那枚假象会让人以为"换网络把网搞坏了",非常误导。⇒ 整轮失败后再来一次。

    ★★ **顺序按"国内 → 境外"**(2026-10-14 修,见 `_EGRESS_URLS` 注释):
    本机是分流路由(国内直连 / 国外走梯子),境外回声量到的是**梯子**的出口。
    拿它当"出口"会得出**两个都错**的结论 —— 既会误判"出口在漂"(它是梯子在轮换),
    也会误判"出口是境外机房段"(小红书看不到那个 IP)。所以:
      · 国产回声在前,先拿到**目标真正看到的那条路**;
      · 真回落到境外时,`说明` 里会**明确写出来**,别让它冒充国内读数。
    """
    import requests

    proxies = for_requests(settings)
    last = ""
    for _round in range(2):                        #: 偶发失败 ⇒ 整轮重来一次
        for url in _EGRESS_URLS:
            try:
                r = requests.get(url, timeout=timeout, proxies=proxies)
                txt = (r.text or "")[:400]
                ip = ""
                try:
                    import json

                    d = json.loads(txt)
                    ip = str(d.get("ip") or d.get("query") or "")
                except Exception:  # noqa: BLE001 - 不是 JSON 就按正文里找 IPv4
                    m = _IPV4.search(txt)
                    ip = m.group(0) if m else ""
                if ip:
                    via = "代理" if proxies else "直连"
                    #: ★ 说清这是**哪条路**量的 —— 境外回声的读数**不能**当作
                    #: "小红书看到的出口"(分流路由下它是梯子的 IP,见 `_EGRESS_URLS` 注释)。
                    if url in _EGRESS_URLS_DOMESTIC:
                        scope = "国内回声"
                    else:
                        scope = "⚠️**境外回声**(报的可能是梯子出口,国内目标看不到它)"
                    return ip, f"{via} · {scope} · {url.split('//')[-1][:34]}"
                last = f"{url.split('//')[-1][:34]} 没返回 IP"
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {str(exc)[:60]}"
                continue
        if _round == 0:
            time.sleep(1.5)                        #: 被限流时缓一下再试,别撞得更狠
    return "", last


#: ip-api 上我们要的那几个字段(多要字段不加钱,但**响应会更大** ⇒ 别贪多)
_IP_FIELDS = ("status,country,regionName,city,isp,org,as,query,proxy,hosting,mobile")


def egress_profile(settings=None, timeout: int = 15) -> dict:
    """一次请求拿**完整出口画像**。取不到回 `{}`。

    为什么要有它:只量到一个 IP **判断不了这枚代理好不好**。
    小红书这类站点对 IP 的**种类**极敏感 —— 同样是"能出网",
    `hosting=true`(机房/云主机)的段基本一上来就在高风险名单里,
    比家宽更容易被判异常。⇒ 买完代理**第一天**就要查这几个字段。
    """
    import json

    import requests

    proxies = for_requests(settings)
    try:
        r = requests.get(f"http://ip-api.com/json/?fields={_IP_FIELDS}",
                         timeout=timeout, proxies=proxies)
        d = json.loads((r.text or "")[:800])
        return d if isinstance(d, dict) and d.get("query") else {}
    except Exception as exc:  # noqa: BLE001 - 量画像是诊断行为,失败不该抛
        logger.debug("量出口画像失败:%s", exc)
        return {}


def egress_stability(samples: int = 5, settings=None, timeout: int = 15,
                     gap_s: float = 1.5) -> tuple[set[str], int]:
    """★ **出口是不是固定的** —— 连着量 `samples` 次,回 `(IP 集合, 成功次数)`。

    ## 为什么这一条对本项目是**硬要求**
    小红书会话**绑定出口 IP**:登录在 A、采集走 B ⇒ 立刻 `300011`。
    而市面上卖的"住宅代理"绝大多数是**轮换型**(每次/每几分钟换一个出口),
    用在**登录态**上恰恰是最糟的一种 —— 服务端看到的是"同一个账号从不同 IP 跳来跳去",
    那正是**账号被盗**的样子。

    ⇒ 本项目只能买**静态**住宅/ISP 代理;买回来第一件事就是用这一条确认
    "它真的不漂"。`len(集合) == 1` 才算静态。

    ⚠️ **读集合之前先看 `说明` 里写的是国内还是境外回声**(2026-10-14 实测):
    本机是**分流路由**时,境外回声走的是**梯子**那条路,梯子本身常常是轮换池 ⇒
    集合里出现一堆**境外机房 IP** 时,**不是"出口在漂"**,而是"量的不是目标那条路"。
    `egress_ip` 已按"国内优先"排序并标注来源,拿它的 `说明` 一起判断,别只看集合大小。
    """
    ips: set[str] = set()
    ok = 0
    for i in range(max(1, samples)):
        if i:                                      #: 采样之间留间隔 —— 免费回声服务有
            time.sleep(gap_s)                      #: 速率限制,连发会被限 ⇒ 误报"量不到"
        ip, _ = egress_ip(settings=settings, timeout=timeout)
        if ip:
            ok += 1
            ips.add(ip)
    return ips, ok


# ------------------------------------------------------------------ CA 证书包

_CA_ASCII: str = ""
_CA_NAME = "_xhs_ca_bundle.pem"


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _ascii_dirs() -> list[Path]:
    """候选的**纯 ASCII**落盘目录(按优先级)。"""
    out: list[Path] = []
    try:
        d = _root() / "data"
        if _is_ascii(str(d)):
            out.append(d)
    except Exception:  # noqa: BLE001
        pass
    try:
        import tempfile

        for d in (tempfile.gettempdir(), r"C:\Windows\Temp", r"C:\Temp"):
            if d and _is_ascii(d):
                out.append(Path(d))
    except Exception:  # noqa: BLE001
        pass
    return out


def ca_bundle() -> str:
    """★ 给 `curl_cffi` 一个**纯 ASCII 路径**的 CA 证书包(2026-10-12 定位)。

    ## 它解决的问题
    `curl_cffi` 的 libcurl 是 **BoringSSL** 构建(实测 `libcurl/8.21.0-IMPERSONATE BoringSSL`),
    加载 CA 文件走的是 **ANSI 编码的 `fopen`** ⇒ **路径里有非 ASCII 字符就打不开**:

        curl: (77) error adding trust anchors from locations:
              CAfile: C:\\Users\\周万鹏\\AppData\\...\\certifi\\cacert.pem

    实测:用默认路径 ⇒ **所有 HTTPS 请求无一例外全失败**;把**同一个文件**复制到
    `D:/code/redian/data/_xhs_ca_bundle.pem`(纯 ASCII)⇒ **立刻 HTTP 200**。

    ## 为什么这个坑特别难发现
    **HTTP 不需要证书 ⇒ 完全不受影响**。而本项目此前所有 `curl_cffi` 验证都拿
    `http://ip-api.com/json` 做的(HTTPS 回声服务本来就全被这条出口挡掉了),
    于是"指纹伪装能用"被"验证通过"了 —— 可小红书接口**全是 HTTPS**
    ⇒ **指纹层一直静默退回 `requests`,从没真正生效过**。

    ⇒ 返回可直接传给 `curl_cffi` 的 `verify=` 的路径;拿不到就回空串
    (调用方**不要**传 `verify`,退回 curl_cffi 自己的默认行为)。
    """
    global _CA_ASCII
    if _CA_ASCII and Path(_CA_ASCII).exists():
        return _CA_ASCII
    try:
        import certifi

        src = str(certifi.where())
    except Exception:  # noqa: BLE001 - 没 certifi 就别折腾了
        return ""
    if _is_ascii(src):
        _CA_ASCII = src
        return _CA_ASCII

    try:
        blob = Path(src).read_bytes()
    except Exception:  # noqa: BLE001
        return ""

    for d in _ascii_dirs():
        try:
            d.mkdir(parents=True, exist_ok=True)
            dst = d / _CA_NAME
            #: 内容没变就别重写(省 IO,也避免多进程互相踩)
            if not dst.exists() or dst.stat().st_size != len(blob):
                dst.write_bytes(blob)
            _CA_ASCII = str(dst)
            logger.info("小红书:CA 证书包路径含非 ASCII 字符,已改用 %s", _CA_ASCII)
            return _CA_ASCII
        except Exception:  # noqa: BLE001
            continue
    logger.warning("小红书:找不到可用的 ASCII 目录放 CA 证书包 ⇒ "
                   "curl_cffi 的 HTTPS 会失败并退回 requests")
    return ""


#: ★★ **出口 IP 门禁**(2026-10-13)
#: 动用账号**之前**,先用一枚**不带任何登录态**的请求确认:这条出口还干净吗?
#:
#: ## 为什么要这道门禁
#: 服务端对 IP 的记账是**独立于账号**的。实测(2026-10-13):在一台机器上连登
#: 4 个档案之后,**连游客请求都开始回 `300012`**:
#:
#:     error_code=300012
#:     error_msg=IP存在风险，请切换可靠网络环境后重试
#:
#: ⇒ 这时候**再换任何新号上去都没用** —— 号换了,出口没换。
#: 表现就是"一个号都活不过一天",而**真相是号一直在同一条被标记的 IP 上**。
#:
#: ## ⚠️ 之前把"IP 没问题"当成已排除项,是个**判据错误**
#: 我们量的**只是 IP 的静态属性**(`hosting`/`proxy`/`mobile` 全 False、家宽、不漂移)。
#: 那些属性说明"这是个正常的住宅 IP",**说明不了"小红书有没有给它记账"**。
#: ⇒ 要分开看:
#:     · IP **是不是住宅段**        —— 静态属性,`egress_profile()` 量
#:     · 这条 IP **有没有被本平台标记** —— 动态状态,只有**发一个游客请求**才试得出来
#: 前者合格**不代表**后者合格。这次就是前者全绿、后者已红。

_IP_CODE_300012 = "300012"
_IP_CODE_300011 = "300011"

#: 游客 UA(刻意用**常见**的桌面 Chrome,与任何存档无关 —— 探针不扮演任何一个号)
_GUEST_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")


def guest_request(settings=None, timeout: int = 25,
                  keyword: str = "美食") -> tuple[int, str]:
    """发**一枚不带任何登录态**的搜索请求。返回 `(HTTP 状态码, 响应体)`。

    ⚠️ 刻意**不读任何凭据存档**,`a1` 也是临时随机生成的 —— 游客也有设备指纹,
    且签名算法要用它(`x3` = a1)。⇒ **零账号成本**,可以放心频繁跑。
    """
    import json
    import random
    import string

    alphabet = string.ascii_lowercase + string.digits
    stamp = f"{time.time() * 1000:.0f}"
    ck = {"a1": stamp + "".join(random.choice(alphabet)
                                 for _ in range(96 - len(stamp)))}

    #: ⚠️ 走**生产那一个** `_http_post`(含 CA 证书包与 `curl_cffi` 指纹),
    #: 别自己裸 `requests.post` —— 否则绕过指纹层,结果与线上不一致。
    from app.services.xhs_protocol_source import _http_post
    from app.services.xhs_sign_pure import headers_xyw

    payload = {"keyword": keyword, "page": 1, "page_size": 20,
               "search_id": "", "sort": "general", "note_type": 0,
               "ext_flags": [], "image_formats": ["jpg", "webp", "avif"]}
    uri = "/api/sns/web/v1/search/notes"
    h = headers_xyw(uri, ck, payload=payload, ua=_GUEST_UA)
    h["Cookie"] = "; ".join(f"{k}={v}" for k, v in ck.items())
    h["content-type"] = "application/json;charset=UTF-8"
    h["user-agent"] = _GUEST_UA
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    r = _http_post("https://edith.xiaohongshu.com" + uri, blob, h, None,
                   timeout=timeout, impersonate="chrome")
    return int(r.status_code), str(r.text or "")[:400]


def ip_blocked(settings=None, timeout: int = 25) -> tuple[bool, str]:
    """★ 这条出口**还能不能跟小红书说话**?返回 `(是否被封, 证据)`。

    ★★ **这是"动用账号之前的门禁"** —— 顺序绝不能反:
        ① 先跑游客请求(零成本)
        ② 确认回 `-101` ⇒ **这条出口是通的**
        ③ 然后**才**值得拿真号去试

    反过来(拿真号直接试未知出口)就是本项目这几天的做法 ⇒
    每试一次就给账号记一笔账,于是"新号活不过一天"。

    ⚠️ **判定成"被封"要保守**:只在**明确拿到风控码**时才说封。
    网络抖一下就报"IP 被封",会害人不必要地去换网络 ——
    而换网络本身**也有成本**(在风控眼里还可能更像"换马甲")。
    """
    try:
        code, txt = guest_request(settings, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 探测没发出去 ⇒ 说不清,别当封了
        return False, f"探测没发出去({type(exc).__name__})⇒ 说不清,别当成封了"

    if _IP_CODE_300012 in txt or code == 300012:
        return True, "服务端回 `300012 / IP处于安全限制中` ⇒ **换出口**"
    if _IP_CODE_300011 in txt:
        return True, ("连游客都被判 `300011 账号存在异常` ⇒ 出口已被标记"
                      ",**换出口**")
    if "-101" in txt or "无登录信息" in txt:
        return False, "`-101 无登录信息` ⇒ 出口是通的(只是没登录)"
    return False, f"服务端回「{txt[:120]}」⇒ 说不清,别当成封了"
