"""小红书**客户端指纹** —— 让协议路"长成"**签发会话的那个浏览器**。

★ **为什么要有这个模块**(2026-10-12 实测定位):
本项目第一次拿到真 `04` 登录凭据时,凭据**齐全**(cookie 15 项 + `webSsk` 61 字符)、
签名**两种都过**(`XYS_` 与纯 `XYW_` 都 HTTP 200,不是 461),但会话仍在 ~5 分钟内被判
`300011 当前账号存在异常` 并作废。逐项排除后定位到的**真缺陷**是:

    登录(浏览器) = **MS Edge 154**  → UA `... Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0`
    采集(协议路) = 硬编码          → UA `... Chrome/138.0.0.0 Safari/537.36`(**没有 `Edg/`**)

差 16 个大版本,还换了浏览器品牌 —— 这是教科书级的"**登录设备 ≠ 调用设备**"。

⚠️ **顺手证伪了一条之前的猜测**(记在这里免得后人再走一遍):
当时怀疑"登录走代理、采集直连 ⇒ 出口 IP 不一致"。实测(`tools/xhs_egress.py` + 强制
`ProxyHandler({})` 对照):直连与走系统代理(`127.0.0.1:25703`)**都是 112.247.45.87** ——
本地那个代理是**不改出口**的透明代理。所以出口不一致**不是**这次的成因。
(`app/services/xhs_net.py` 那套收口仍然保留:以后真配了代理,它能保证两边同源。)

★ **修法的关键不是"把 138 改成 154"** —— 那样 Edge 下次升级又会漂。
正解是:**登录时把浏览器的真实 UA 存进凭据存档,协议路从存档里取**。
指纹于是**由凭据自己携带**,与浏览器版本永远同步,不会再有第二次漂移。

安全前提(已核对 `xhs_sign_pure.sign_xyw`):
`x-s` 只吃 `url + body + a1 + 时间戳`,**不含 UA** ⇒ 换 UA 不会让签名失效。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.utils import get_logger

logger = get_logger(__name__)

#: **兼容旧存档**的红利用 UA —— 2026-10-12 之前导出的凭据文件里**没有** `ua` 字段,
#: 那时只能退回这个常量。新导出的存档都会带 `ua`,这条只在"老存档没重登"时兜底。
#: ⚠️ 它同时也是"指纹漂移"的**参照物**:`chrome_major()` 拿它与存档 UA 比对,
#: 差太多就在 `tools/xhs_accounts.py doctor` 里报出来。
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

#: 无头浏览器会在 UA 里塞这个词 —— 存档它等于**主动告诉服务端"我是自动化"**,
#: 所以读到的 UA 一律先过 `normalize_ua()` 洗一遍。
_HEADLESS_TOKEN = "HeadlessChrome/"

_UA_RE = re.compile(r"(Chrome|Firefox|Version|Edg|EdgA|OPR)/(\d+)")


def normalize_ua(ua: str | None) -> str:
    """把无头浏览器的 UA 洗成正常的,空值回 `DEFAULT_UA`。

    `HeadlessChrome/154.0.0.0` ⇒ `Chrome/154.0.0.0`。
    为什么要洗:`tools/xhs_browser_login.py` 允许用 `headless=True` 去**核对**一个已登录的
    档案(`is_real_session` 那条早退分支),此时抓到的 UA 会带 `HeadlessChrome` ——
    把它写进存档,等于之后所有请求自报"我是无头浏览器"。
    """
    u = str(ua or "").strip()
    if not u:
        return DEFAULT_UA
    if _HEADLESS_TOKEN in u:
        u = u.replace(_HEADLESS_TOKEN, "Chrome/")
    return u


def chrome_major(ua: str | None) -> int:
    """从 UA 里取 **Chrome/Chromium 大版本号**(取不到回 0)。

    Edge 的 UA 也含 `Chrome/<major>`(它就是 Chromium 内核),所以 Edge 也能取到 ——
    这正是我们要的:**比对"登录时的内核版本"与"协议路发出的内核版本"是否同源**。
    """
    u = str(ua or "")
    m = re.search(r"(?:Chrome|HeadlessChrome)/(\d+)", u)
    try:
        return int(m.group(1)) if m else 0
    except (TypeError, ValueError):
        return 0


def browser_family(ua: str | None) -> str:
    """UA 属于哪个浏览器家族:`edge` / `firefox` / `safari` / `chrome` / `unknown`。"""
    u = str(ua or "").lower()
    if "edg/" in u or "edga/" in u or "edge/" in u:
        return "edge"
    if "firefox/" in u and "chrome/" not in u:
        return "firefox"
    if "chrome/" in u or "headlesschrome/" in u or "chromium/" in u:
        return "chrome"
    if "safari/" in u:
        return "safari"
    return "unknown"


def impersonate_for(ua: str | None) -> str:
    """**由 UA 推出 `curl_cffi` 的指纹目标**。

    `impersonate` 决定的是 **TLS(JA3/JA4)+ HTTP/2 SETTINGS 顺序 + 头部顺序** ——
    这一层是 `requests` 单靠改 Header **补不上**的(服务端看的是握手,不是字符串)。
    所以**不能** UA 写 Edge 而 TLS 指纹却仿 Chrome —— 那和这次的故障是同一类错配,
    只是换到了更底下的层。

    ⇒ 家族与指纹目标必须**同源**,都由这一支函数推出来。
    """
    fam = browser_family(ua)
    #: `chrome` / `edge` / `firefox` / `safari` 都是 curl_cffi 认的目标名;
    #: `unknown` 一律按 chrome 处理(小红书 web 端本来就是 Chromium 场景)。
    return fam if fam in ("edge", "firefox", "safari") else "chrome"


@dataclass(frozen=True)
class Fingerprint:
    """一份**可直接用**的指纹:`ua` 进 Header,`impersonate` 进 curl_cffi。"""

    ua: str
    impersonate: str          #: 空串 = 关掉 curl_cffi,退回 requests
    source: str = "default"   #: `archive`(凭据存档) / `settings`(显式配) / `default`(兜底)

    @property
    def major(self) -> int:
        return chrome_major(self.ua)

    def drift_from_default(self) -> int:
        """与 `DEFAULT_UA` 的**内核版本差**。差 ≥2 说明兜底常量已经过时了(该更新)。"""
        return abs(self.major - chrome_major(DEFAULT_UA))


def resolve_impersonate(setting: str | None, ua: str | None) -> str:
    """把 **`xhs_impersonate` 配置**解析成真正要传给 curl_cffi 的目标。

    | 配置值        | 结果                                  |
    |---------------|---------------------------------------|
    | `""` / 空     | `""` —— 关掉指纹伪装,退回 `requests`  |
    | `auto`        | 由 `ua` 自动推(`impersonate_for`)     |
    | `chrome`/`edge`/… | 原样用(人工强制)                  |
    """
    s = str(setting or "").strip()
    if not s:
        return ""
    if s.lower() == "auto":
        return impersonate_for(ua)
    return s


def resolve(ua_hint: str = "", settings=None) -> Fingerprint:
    """**定下这一次请求用哪份指纹。**

    优先级(★ 关键:存档**优先于**硬编码常量,这是整个修复的落点):
      1. `ua_hint` —— **凭据存档里记的 UA**(登录时从浏览器抓的真值);
      2. `settings` 里的 `xhs_impersonate` == `auto` 时,指纹目标跟着 `ua_hint` 走;
      3. 都没有 ⇒ `DEFAULT_UA` + chrome(旧行为,不回退)。
    """
    ua = normalize_ua(ua_hint) if str(ua_hint or "").strip() else DEFAULT_UA
    src = "archive" if str(ua_hint or "").strip() else "default"

    if settings is None:
        try:
            from config.settings import get_settings

            settings = get_settings()
        except Exception:  # noqa: BLE001 - 没配置中心时按"开着 chrome"处理
            settings = None

    imp = resolve_impersonate(getattr(settings, "xhs_impersonate", ""), ua)

    fp = Fingerprint(ua=ua, impersonate=imp, source=src)
    if fp.drift_from_default() >= 2:
        #: 只**提示**不报错 —— 用老凭据跑是合法的,但常量过时了值得说一声。
        logger.info("小红书指纹:存档 UA 内核 %d,与兜底常量 %d 差 %d(建议更新 DEFAULT_UA)",
                    fp.major, chrome_major(DEFAULT_UA), fp.drift_from_default())
    return fp


def build_client_hints(brands=None, mobile: bool = False, platform: str = "") -> dict[str, str]:
    """造 `sec-ch-ua*` 这组**客户端提示头**(UA-CH)。

    ★ 与 UA 同一类缺陷(顺手一起补的):真实 Chrome/Edge 的 `fetch` **一定**会带
    ```
    sec-ch-ua:          "Microsoft Edge";v="154", "Chromium";v="154", "Not(A:Brand";v="99"
    sec-ch-ua-mobile:   ?0
    sec-ch-ua-platform: "Windows"
    ```
    而我们原来一个都不发 ⇒ 对服务端等于"自称浏览器却拿不出浏览器该有的头",
    与"UA 写 Chrome 138 而登录是 Edge 154"是**同一类自曝**。

    ⇒ 同样**从登录浏览器抓**:`navigator.userAgentData` 的 `brands` / `mobile` / `platform`。
    抓不到(老存档 / 无头)就**返回空**,由调用方**不发** —— 宁可缺,也不发一份编的。
    """
    bs = [b for b in (brands or []) if isinstance(b, dict) and b.get("brand")]
    if not bs:
        return {}
    parts = [f'"{b["brand"]}";v="{b.get("version", "")}"' for b in bs]
    out = {"sec-ch-ua": ", ".join(parts),
           "sec-ch-ua-mobile": "?1" if mobile else "?0"}
    plat = str(platform or "").strip()
    if plat:
        out["sec-ch-ua-platform"] = f'"{plat}"'
    return out


def describe(ua: str | None) -> str:
    """一句话说清这份 UA 是什么(给 `doctor` / 日志用)。"""
    u = normalize_ua(ua)
    fam = browser_family(u)
    mj = chrome_major(u)
    tag = {"edge": "Edge", "chrome": "Chrome", "firefox": "Firefox",
           "safari": "Safari"}.get(fam, "未知")
    return f"{tag} / 内核 {mj}(指纹目标 {impersonate_for(u)})"
