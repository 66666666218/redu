# -*- coding: utf-8 -*-
"""小红书:**纯协议**(2026-10-07,替换 MediaCrawler 与页面渲染)。

链路拆解、算法、以及"**停更了自己怎么修**"都在
`doc/小红书纯协议-链路拆解.md` —— 读它比读这个文件重要。

## 一句话
`xhshow` 算出 `x-s` / `x-s-common`,我们把它连同 **Cookie 头**发出去直连
`edith.xiaohongshu.com`。实测 4 词 × 2 页全部 HTTP 200、**~0.85 秒/请求**
(浏览器那条路 ~15 秒/词)。

## ⚠️ 三条纪律
1. **"没登录" 与 "没搜到" 必须分开**(本仓反复栽的假阴性):
   `code:-101 无登录信息` / `300011 账号异常` ⇒ **抛错**,绝不返回空列表。
2. **一次搜索的所有页共用一个 `search_id`** —— 2026-10-07 控制变量实测:
   同 id 翻两页交集 **0**(真在翻);每页各新生成交集 **5**(静默重复)。
3. **凭据来自加密 cookie 库**(`tools/xhs_export_cookie.py` 从浏览器档案解密导出)。
   手抄那份实测**丢了 `id_token`**,站点不认 ⇒ **纯协议的是"采集",不是"登录"**。

## 与页面渲染那条路的关系
`app/services/xhs_page_source.py` **保留作降级兜底** ——
`xhshow` 是第三方签名库(且 PyPI 上 `license: None`),它哪天跟不上小红书改版,
页面渲染还能顶上(慢、脆,但不需要签名)。
"""
from __future__ import annotations

import json
import time

from app.utils import get_logger

logger = get_logger(__name__)

API_BASE = "https://edith.xiaohongshu.com"
URI_SEARCH = "/api/sns/web/v1/search/notes"
PLATFORM = "xiaohongshu"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
#: 逐词之间的间隔。实测连发 8 次没问题,但没必要贴着上限跑。
_GAP = 1.2
#: 单页条数(接口实测稳定给 20~22)
PAGE_SIZE = 20


class XhsProtocolError(Exception):
    """纯协议硬失败。`kind` 说清是哪一种,**`needs_human` 表示要人动手**。"""

    def __init__(self, msg: str, kind: str = "api", needs_human: bool = False) -> None:
        super().__init__(msg)
        self.kind = kind
        self.needs_human = needs_human


def _cookie(session=None, user_id: int = 1, settings=None) -> str:
    """登录态:优先**加密 cookie 库**,回落 `.env` 的 `xhs_web_session`。

    ⚠️ 只有 `web_session` 是**不够**的 —— 实测手抄那份(只有 web_session/a1/webId)
    站点回 `-101 无登录信息`;从浏览器档案 `ctx.cookies()` **解密**读出来的全套才行
    (关键是 `id_token`)。所以缺它时要**明确报错 + 给修法**,别让它表现成"没热搜"。
    """
    if settings is None:
        from config.settings import get_settings
        settings = get_settings()
    blob = ""
    if session is not None:
        try:
            from app.services.cookie_store import get_cookie

            blob = (get_cookie(session, user_id, PLATFORM) or "").strip()
        except Exception:  # noqa: BLE001
            logger.debug("读小红书 cookie 失败(回落 .env)", exc_info=True)
    if not blob:
        blob = (getattr(settings, "xhs_web_session", "") or "").strip()
    return blob


def _cookie_dict(cookie: str) -> dict:
    """`a=1; b=2` → dict(签名与 `Cookie` 头都要用)。"""
    out: dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def needs_login(text: str) -> bool:
    """响应/页面里是不是"**没登录/被踢**"(而不是"没搜到")。

    ⚠️ 这几句都要认:`-101 无登录信息`、`300011 检测到账号异常`、
    `电脑设备登录超限,请重新登录`(被踢)。
    """
    t = str(text or "")
    return ("无登录信息" in t or "登录信息为空" in t or "-101" in t
            or "300011" in t or "检测到账号异常" in t or "请重新登录" in t
            or "登录超限" in t
            # ⚠️ **`-100 登录已过期` 必须在内**(2026-10-07 压测当场踩到):
            # 少了它,那次失败被归成 `kind="api"`(像"接口出错")而不是
            # "要人重新登录" —— 于是**不会推需人工的告警**,得等人自己发现。
            or "-100" in t or "登录已过期" in t or "登录过期" in t)


#: ★★ **签名模板的版本号**(2026-10-11,来源:`Cloxl/xhshow` 的 **issue #110**)。
#: 该库自带的 `SIGNATURE_DATA_TEMPLATE` 是 **`x0="4.3.5", x4="object"`**,而当前应当是
#: **`x0="4.4.3", x4=""`** —— 版本号过旧会让**服务端把它当成异常客户端**。
#: 实测(**同一 cookie、同一时刻,只改这一处**):
#:   · 补丁前:`HTTP 461 + code 300011 当前账号存在异常`
#:   · 补丁后:`HTTP 200 + code -100 登录已过期`  ← **错误确实变了**
#: ⇒ 所以这不是"玄学调参",是**版本号**;以后小红书再改版,先看这个数。
SIGN_TEMPLATE = {"x0": "4.4.3", "x1": "xhs-pc-web", "x2": "Windows", "x3": "", "x4": ""}


#: ★★ **`XYW_` 签名里的"环境标志"19 位**(2026-10-11 实测**解密**得到的真值)。
#: `XYW_` 的明文只有一个式子:`x1=md5("url="+路径);x2=<这串标志>;x3=<a1>;x4=<毫秒时间戳>;`
#: (AES-128-CBC,密钥/IV 都是公开常量 ⇒ **谁都能解开,不需要私钥**)。
#: ⚠️ **库自带的默认值是错的** —— 逐位比:真值第 11、18 位是 `1`、末位是 `0`,
#: 而库默认那串是 `0`/`1`(`0|0|0|1|0|0|1|0|0|0|1|1|0|0|0|1|0|0|0` vs
#: `0|0|0|1|0|0|1|0|0|0|1|0|0|0|0|1|0|0|1`)。**就这三位之差,签名就有 60% 的字节不一样。**
#: **证据(控制变量)**:同一 a1、同一毫秒时间戳下,把这一串换成真值 ⇒
#: 我们库算出的 payload 与**页面 `window._webmsxyw` 算出的**逐字节相同;
#: 换之前从第 80 字节起分叉(416 个十六进制字符里 239 个不同)。
#: 取得方式(不逆任何 JS):让页面签一个废包 → 用公开密钥/IV 解开 → 读出 `x2`。
#: 它**跨调用是稳定的**(实测两次一致),但仍**建议按页面实时值覆盖**(见 `xhs_login.browser_identity`)。
#: 可用 `.env` 的 `xhs_sign_env_flags` 覆盖。
SIGN_ENV_FLAGS = "0|0|0|1|0|0|1|0|0|0|1|1|0|0|0|1|0|0|0"


def _template(settings=None) -> dict:
    """最终模板 = 内置常量,可被 **`xhs_sign_template`(JSON)** 覆盖。

    ⚠️ 为什么要能覆盖:小红书改版是**我们改代码/发版赶不上的那种事**(上一版 `x0`
    用了小半年),而"改一个常量"不该要重新部署一遍整个服务。配错(不是 JSON / 不是对象)
    **只告警、用内置的**,绝不让签名器起不来。
    """
    out = dict(SIGN_TEMPLATE)
    raw = str(getattr(settings, "xhs_sign_template", "") or "").strip()
    if not raw:
        return out
    try:
        got = json.loads(raw)
    except ValueError:
        logger.warning("`xhs_sign_template` 不是合法 JSON,忽略、用内置模板:%s", raw[:60])
        return out
    if not isinstance(got, dict):
        logger.warning("`xhs_sign_template` 不是 JSON 对象,忽略、用内置模板:%s", raw[:60])
        return out
    out.update({str(k): str(v) for k, v in got.items()})
    logger.info("小红书签名模板被 `xhs_sign_template` 覆盖为:%s", out)
    return out


def _env_flags(settings=None, live: str = "") -> str:
    """最终的环境标志 = **实时值(优先)** → 配置 → 内置实测值。

    ⚠️ 优先级是刻意的:**页面当场算出来的那个**永远最准(它是站点自己的环境描述);
    配置次之;内置值只是"上一次量到的真值",是**兜底**,不是权威。
    """
    for cand in (live, str(getattr(settings, "xhs_sign_env_flags", "") or "").strip(),
                 SIGN_ENV_FLAGS):
        if cand:
            return cand
    return SIGN_ENV_FLAGS


def _signer(settings=None, env_flags: str = ""):
    """造一个**打过补丁**的签名器:模板版本号(见 `SIGN_TEMPLATE`)+ 环境标志(见 `SIGN_ENV_FLAGS`)。

    任何一处补丁失败都**退回库自带**,绝不让签名器起不来(那会让整条链 500)。
    """
    from xhshow import Xhshow

    try:
        from xhshow.config.config import CryptoConfig

        cfg = CryptoConfig(SIGNATURE_DATA_TEMPLATE=_template(settings),
                           XYW_ENV_FLAGS_DEFAULT=_env_flags(settings, env_flags))
        return Xhshow(config=cfg)
    except Exception:  # noqa: BLE001 - 补丁失败不该让整条链崩
        logger.warning("小红书签名补丁失败,退回库自带", exc_info=True)
        return Xhshow()


def parse_env_flags(xyw_sig: str) -> str:
    """从一枚**真实 `XYW_` 签名**里解出环境标志 `x2`(取值含义见 `SIGN_ENV_FLAGS`)。

    为什么解得开:`XYW_` 是 AES-128-CBC,而**密钥与 IV 都是写死在网页 JS 里的公开常量**
    —— 没有私钥的概念 ⇒ **谁都能解**。所以"拿真值"不需要逆向,只要让页面签一个包就行
    (`xhs_login.browser_identity()` 就是这么做的)。拿不到一律回空串,由调用方兜底。
    """
    import base64 as _b64
    import json as _json
    import re as _re

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        from xhshow.config.config import CryptoConfig as _C

        blob = str(xyw_sig or "").split("_", 1)[-1]
        payload = _json.loads(_b64.b64decode(blob).decode("utf-8"))["payload"]
        d = Cipher(algorithms.AES(_C.XYW_AES_KEY), modes.CBC(_C.XYW_AES_IV)).decryptor()
        raw = bytes.fromhex(payload)
        pt = d.update(raw) + d.finalize()
        pt = pt[:-pt[-1]] if pt and 0 < pt[-1] <= 16 else pt      # 去 PKCS7 填充
        text = _b64.b64decode(pt).decode("utf-8", "replace")
        m = _re.search(r"x2=([^;]*)", text)
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001 - 解析只是"取真值"的便利,失败就当没取到
        logger.debug("从 XYW_ 签名里解环境标志失败", exc_info=True)
        return ""


def make_signer(settings=None, env_flags: str = ""):
    """**给别的模块用的公开入口**(如 `xhs_login` 的扫码登录)。

    伸手拿 `_signer` 也能跑,但那会让"补丁只此一处"这条纪律失去落点 ——
    签名器必须**总是**从这一个门出来(带模板版本 + 环境标志两处补丁)。
    `env_flags` 传**页面实时算出来的**那串(见 `xhs_login.browser_identity`)。
    """
    return _signer(settings, env_flags)


def _xhs_proxies(st) -> dict[str, str] | None:
    """小红书的出口 —— **必须**与登录时(浏览器)是同一个,否则会话会立刻被判异常。

    全部规则收在 `app/services/xhs_net.py`(**两条路同源**,别在这里另写一套):
    `XHS_PROXY` → 全局隧道 → **系统代理**(浏览器用它)。
    ⚠️ **绝不**回落"提取式轮换池" —— 那个池每 3 分钟换出口,而小红书会话绑出口 IP。
    """
    try:
        from app.services import xhs_net

        return xhs_net.for_requests(st)
    except Exception:  # noqa: BLE001 - 出口算不出来就直连,不该让采集停摆
        logger.debug("算小红书出口失败,按直连处理", exc_info=True)
        return None


#: ★ **最近一次请求是否真的用上了指纹伪装**(2026-10-14 加)。
#:
#: 为什么需要它:curl_cffi 那一层失败时会**静默降级**成 `requests`(那是**有意的** ——
#: 指纹是增强不是依赖,不能让它断链)。但降级之后,我们手上那些"账号异常/被挡"的结论
#: **就不再可信** —— 今晚就栽在这上面:一次 461+`300011` 的单发里,指纹层其实降级了,
#: 而结论里**看不出这件事**,差点又把它当成"账号级、只能换号"。
#: ⇒ 降级必须**流进结论**里,不能只躺在日志里(日志没人看,本仓母题)。
LAST_FP_OK: bool = True


def _fp_note() -> str:
    """给"指认不了"的错误补一句:这次**用没用指纹** —— 决定了结论能不能采信。"""
    return (";⚠️ **本次未用指纹**(降级到 requests)⇒ 这类结论**先别采信**,先修指纹再复测"
            if not LAST_FP_OK else "")


def _http_post(url: str, body: str, headers: dict, st, timeout: int = 25,
               impersonate: str | None = None):
    """★ **按"浏览器形状"发一个 POST** —— 指纹与出口都在这一层收口。

    ## 为什么要有它
    2026-10-12 第一次拿到真 `04` 登录凭据的实测:cookie 15 项 + `webSsk` 齐全,
    **两种签名**(带不带 `x6/x7`)都过了签名校验(HTTP 200),但会话仍在 ~5 分钟内
    被判 `300011 当前账号存在异常` 并作废(随后 `-100 登录已过期`)。
    ⇒ 剩下的差距**不在算法层**,在这两处:

      1. **TLS / HTTP2 指纹** —— `requests` 的 JA3 是 `python-requests` 自己的,
         与 Chrome 差得很远;**改 Header 补不上**(服务端看的是握手包)。
         `curl_cffi` 的 `impersonate` 会把 **JA3/JA4 + HTTP/2 SETTINGS 顺序 + 头部顺序**
         一起仿成真实浏览器。
      2. **出口 IP 与登录时不一致** —— 见 `_xhs_proxies`。

    ⚠️ **第 2 条后来被证伪了**(2026-10-12 补记,免得照着旧注释白改):
    实测直连与走系统代理 **都是 112.247.45.87** —— 本地那个代理不改出口,两边本来就一致。
    真正的第 2 条其实是 **UA 与签发会话的浏览器不一致**(`xhs_fingerprint`):
    用 Edge 154 登的号却自称 Chrome 138 ⇒ 同样被判 `300011`。
    ⇒ 所以 `impersonate` **必须按账号解析**:UA 写着 Edge 就仿 Edge 的 TLS,
    否则等于把同一类错配换到更底下的层。`xhs_impersonate="auto"` 就是干这个的。

    ⚠️ **任何一层失败都退回 `requests`**,绝不让"换了个发送器"变成整条链断掉。
       指纹是**增强**,不是依赖。

    `impersonate`:调用方**已解析好**的指纹目标(推荐,这样它能按账号带 UA 推出来);
    不传则按 `headers["user-agent"]` + 配置**现解析**一次(兼容直接调用的老代码)。
    """
    import requests

    from app.services import xhs_fingerprint as fp

    proxies = _xhs_proxies(st)
    if impersonate is None:
        #: 没显式传 ⇒ 按**这一次真正发出去的 UA** 解析(保证 UA 与 TLS 指纹同源)
        impersonate = fp.resolve_impersonate(getattr(st, "xhs_impersonate", ""),
                                             headers.get("user-agent", ""))
    imp = str(impersonate or "").strip()
    if imp:
        try:
            from curl_cffi import requests as cffi

            from app.services import xhs_net

            kw: dict = dict(data=body.encode("utf-8"), headers=headers,
                            timeout=timeout, impersonate=imp)
            if proxies:
                kw["proxies"] = proxies
            #: ★★ **CA 证书包必须是"纯 ASCII 路径"** —— 否则 BoringSSL 打不开它,
            #: **所有 HTTPS 请求会无一例外地失败**(curl error 77)。
            #: 而小红书接口全是 HTTPS ⇒ 不修这里,上面的 `impersonate` **等于没生效**,
            #: 请求会全部静默退回 `requests`,指纹伪装形同虚设。详见 `xhs_net.ca_bundle`。
            ca = xhs_net.ca_bundle()
            if ca:
                kw["verify"] = ca
            resp = cffi.post(url, **kw)
            globals()["LAST_FP_OK"] = True
            logger.debug("小红书请求:curl_cffi(impersonate=%s%s) → %s",
                         imp, "+代理" if proxies else "", resp.status_code)
            return resp
        except Exception as exc:  # noqa: BLE001
            #: ⚠️ **故意用 warning 而不是 info**:这次事故的根因就是"指纹层悄悄失效了
            #: 却没人看见"。退回 `requests` 是**降级**,必须留痕。
            globals()["LAST_FP_OK"] = False
            logger.warning("小红书 curl_cffi 不可用(%s: %s)—— "
                           "**指纹伪装未生效**,本次退回 requests(⇒ 本次的错误结论**不可信**)",
                           type(exc).__name__, str(exc)[:120])
            # ★★ 诊断(2026-10-14 加):`curl: (16)` 是 **HTTP/2 层**的错,而 h2 对**头部**
            #    比对 h1 严得多 —— 值里只要有一个**控制字符/非 ASCII**,h1 照过、h2 直接报 16。
            #    ⚠️ **只打名字与长度 + 异常字符码点,绝不打值**(值里有 cookie 与签名)。
            try:
                for _k, _v in (headers or {}).items():
                    _bad = [hex(ord(_c)) for _c in str(_v) if ord(_c) < 32 or ord(_c) > 126]
                    if _bad:
                        logger.warning("  ⚠️ 头部 `%s` 含非常规字符(%s…) 长度 %d —— "
                                       "h2 会因此报 16,而 h1 照过",
                                       _k, ",".join(_bad[:6]), len(str(_v)))
                logger.debug("  本次头部: %s", {k: len(str(v)) for k, v in (headers or {}).items()})
            except Exception:  # noqa: BLE001 - 诊断本身不许影响主流程
                pass
    return requests.post(url, data=body, headers=headers, timeout=timeout,
                         proxies=proxies)


def _search_with(ck: dict, ssk: bytes, kws: list[str], st, pages: int,
                 session=None, user_id: int = 1, ua: str = "",
                 hints: dict | None = None) -> list[dict]:
    """**用一套凭据**跑完所有关键词。失败抛 `XhsProtocolError`(带 `kind`,供账号池判状态)。

    ★ `ua`:**这份凭据登录时的真实 UA**(由 `pool.ua_of(ref)` 从存档取,可以为空)。
    为什么不写死一个常量:会话是**某个浏览器**签发的,采集时必须**长得像它** ——
    用 Edge 154 登的号却自称 Chrome 138,实测就是 `300011 当前账号存在异常`。
    见 `app/services/xhs_fingerprint` 的模块头。

    `hints`:`sec-ch-ua*` 客户端提示头(由 `pool.hints_of(ref)` 从存档取,可为 `None`)。
    真实浏览器发请求时一定带这一组;我们原来一个都不发 ⇒ 与 UA 那处是**同一类自曝**。
    """
    import requests

    from app.services import xhs_fingerprint

    #: ★ **一次解析,全程复用** —— `ua` 进 Header,`impersonate` 进 TLS 层,两者同源。
    fingerprint = xhs_fingerprint.resolve(ua, st)

    #: ★ **签名格式必须显式传**(2026-10-10 实测矩阵,见 `config/settings.py` 那段):
    #: 库的默认是 `xys`,而小红书 2026-03 起对数据接口拒 `XYS_` ⇒ 永远 461、
    #: 且**响应是 `code:0, data:{}` 的"静默空"**(最容易被读成"这个资源没人发")。
    _fmt = str(getattr(st, "xhs_sign_format", "xyw") or "xyw")
    _rap = bool(getattr(st, "xhs_sign_x_rap", True))

    #: ★ **签名三档,从"最自主"到"最依赖"**(2026-10-12):
    #:   1. **纯 Python XYW_**(`xhs_sign_pure`)—— 实测与站点**逐字节一致**,不开浏览器、
    #:      不依赖 xhshow,且 `x3` 用**我们自己的 a1**(oracle 那条会用页面的 a1,会错配)。
    #:      这是现在的主路。
    #:   2. oracle(让页面算)—— 保底:万一站点改了 XYW_ 结构,页面算的仍然对。
    #:   3. `xhshow` 复刻 —— 最后兜底(第三方库,`license: None`)。
    #: 开关 `xhs_sign_pure`(默认 true)控制第 1 档是否启用。
    _use_pure = bool(getattr(st, "xhs_sign_pure", True))
    _use_oracle = bool(getattr(st, "xhs_sign_oracle", True))

    x = _signer(st)      #: `search_id` 由它生成;纯 Python 路也复用它(它只生成 id,不签名)
    rows: list[dict] = []
    for i, kw in enumerate(kws):
        if i:
            time.sleep(_GAP)
        # ★ **一次搜索共用一个 search_id**(控制变量实测:每次新生成会让翻页混进重复)
        sid = x.get_search_id()
        for page in range(1, max(1, pages) + 1):
            payload = {"keyword": kw, "page": page, "page_size": PAGE_SIZE,
                       "search_id": sid, "sort": "general", "note_type": 0,
                       "ext_flags": [], "image_formats": ["jpg", "webp", "avif"]}
            try:
                if _use_pure:
                    # ★ 纯 Python XYW_:x-s 逐字节等同站点,且 x3 = 我们自己的 a1
                    from app.services.xhs_sign_pure import headers_xyw as _pure_headers

                    headers = _pure_headers(URI_SEARCH, ck, payload=payload,
                                            ua=fingerprint.ua)
                    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                elif _use_oracle:
                    # ★ oracle:页面自己的 JS 算(永远是当前线上版本)。
                    #   body 必须**逐字节**与签名时一致(紧凑 JSON)。
                    from app.services.xhs_sign_oracle import headers as _oracle_headers

                    headers = _oracle_headers(URI_SEARCH, ck, payload=payload)
                    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                elif ssk:
                    # ★ **完整 `XYS_`**(站点自己的算法:`x5`/`x6`/`x7` 都在)。
                    #   注意:body 也必须**逐字节**用同一份紧凑 JSON(签名用的字节 = 发出去的字节)
                    from app.services.xhs_xys import headers as _xys_headers

                    headers = _xys_headers(x, "POST", URI_SEARCH, ck, payload=payload, ssk=ssk)
                    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                else:
                    headers = x.sign_headers_post(URI_SEARCH, ck, payload=payload,
                                                  sign_format=_fmt, x_rap=_rap)
                    body = json.dumps(payload)
            except Exception as exc:  # noqa: BLE001 - 签名库自己炸了要说清是哪一层
                raise XhsProtocolError(f"算签名失败:{type(exc).__name__}: "
                                       f"{str(exc)[:90]}", kind="sign") from exc
            # ⚠️ **`xhshow` 不会替你设 `Cookie` 头** —— 少了它签名照样被接受(HTTP 200、
            # 没有 461),但接口回 `-101 无登录信息`。这个坑极有欺骗性,写在这里。
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in ck.items())
            headers["content-type"] = "application/json;charset=UTF-8"
            #: ★ 用**这份凭据自己**的 UA,不是全局常量(见本函数 docstring)
            headers["user-agent"] = fingerprint.ua
            #: ★ `sec-ch-ua*` —— 同样是**登录浏览器抓来的**,抓不到就不发(不编)
            for _k, _v in (hints or {}).items():
                headers[_k] = _v
            try:
                r = _http_post(API_BASE + URI_SEARCH, body, headers, st, timeout=25,
                               impersonate=fingerprint.impersonate)
            except Exception as exc:  # noqa: BLE001
                raise XhsProtocolError(f"请求失败:{type(exc).__name__}: {str(exc)[:90]}",
                                       kind="network") from exc
            if r.status_code == 461:
                # ⚠️ **461 其实是两种,必须分开**(2026-10-10 实测矩阵):
                #   · 响应体 `code:300011 当前账号存在异常` ⇒ **账号级**,与签名无关
                #     (换号 / 过验证才对,改代码是白折腾);
                #   · 响应体 `code:0, data:{}`(**静默空**)⇒ **签名/指纹没通过**。
                # 混成一句话的代价:账号被限时我们会一直去"比对签名常量"。
                _body = r.text[:300]
                if "300011" in _body or "账号存在异常" in _body:
                    raise XhsProtocolError(
                        f"小红书**账号异常**(HTTP 461;{_body[:110]})—— **账号级,与签名无关**。"
                        f"修法:换号或过验证 `python tools/xhs_pass_verify.py <档案目录>`"
                        + _fp_note(),
                        kind="restricted", needs_human=True)
                raise XhsProtocolError(
                    f"**HTTP 461** —— 签名/指纹没通过(不是「没搜到」)。"
                    f"当前格式 `{_fmt}`、x_rap={_rap};响应体:{_body[:90]}"
                    f" —— 照 `doc/小红书纯协议-链路拆解.md` §5 排查", kind="sign")
            if r.status_code == 406:
                raise XhsProtocolError(
                    "**HTTP 406** —— 这个接口要 `XYW_` 格式(不是 `XYS_`)。"
                    "同一个文档 §2 写了第二种格式", kind="sign")
            if r.status_code != 200:
                raise XhsProtocolError(f"HTTP {r.status_code}:{r.text[:120]}", kind="api")
            try:
                d = r.json()
            except ValueError as exc:
                raise XhsProtocolError(f"响应不是 JSON(多半被挡):{r.text[:120]}",
                                       kind="api") from exc
            if needs_login(r.text[:400] + str(d.get("msg") or "")):
                # ⚠️ **`-100` 与 `-101` 要分开报**:前者是"会话过期"(刷新/重登),
                # 后者多是"没带全凭据"。账号池据此判 `expired` 还是 `guest`。
                _code = d.get("code")
                _why = ("登录已过期" if _code == -100 else
                        "无登录信息/凭据不全" if _code == -101 else
                        f"登录态失效(code={_code})")
                raise XhsProtocolError(
                    f"小红书 {_why}({d.get('msg') or _code})—— "
                    f"⚠️ **重导同一枚死会话解决不了**(2026-10-10 实测:重导出来还是它)"
                    f"⇒ **需要人在浏览器里重新登录**:跑 "
                    f"`tools/xhs_browser_login.py <档案>`,之后 `tools/xhs_go_live.py` 验证",
                    kind="need_login", needs_human=True)
            if not d.get("success"):
                code = d.get("code")
                # ⚠️ **`-104 没有权限访问` 要单独说**(2026-10-07 压测后实测):
                # 它意味着**凭据能认证、但账号被限制**(实测:那次连发 ~200 次之后,
                # 先 461、再 -100、重新登录后变成 -104)。这是**账号级**的,
                # 与签名无关 —— 修法是**等它解封**,不是改代码。
                if code == -104 or "没有权限" in str(d.get("msg") or ""):
                    raise XhsProtocolError(
                        f"小红书**账号被限制**(code={code} "
                        f"{str(d.get('msg'))[:60]})—— 凭据能认证,是账号级风控。"
                        f"修法:**停手等它解封**(几小时~一天),别反复重登/重试",
                        kind="restricted", needs_human=True)
                raise XhsProtocolError(f"接口不成功:code={code} "
                                       f"msg={str(d.get('msg'))[:80]}", kind="api")
            items = ((d.get("data") or {}).get("items") or [])
            for it in items:
                nc = it.get("note_card") or {}
                title = str(nc.get("display_title") or "").strip()
                if not title:
                    continue
                user = nc.get("user") or {}
                rows.append({"uid": str(user.get("user_id") or ""),
                             "name": str(user.get("nickname") or "")[:60],
                             "url": f"https://www.xiaohongshu.com/explore/{it.get('id')}",
                             "snippet": title[:255], "pan_link": "", "keyword": kw})
            logger.info("小红书纯协议:「%s」第 %d 页 +%d 条", kw[:24], page, len(items))
            if not items:
                break
    return rows


def search(keywords: list[str], settings=None, session=None, user_id: int = 1,
           pages: int = 1) -> list[dict]:
    """按关键词搜小红书 —— **带账号池轮换**。返回 `{uid, name, url, snippet, pan_link, keyword}`。

    形状与 `mediacrawler_source.crawl` 一致,上层(`resource_presence.probe`)不用改。

    ★ **2026-10-12 起的取号逻辑**(`app/services/xhs_accounts.py`):
    不再"死死用一枚 cookie",而是从**账号池**按 `healthy > unknown > 游客 > 过期 > …`
    的顺序取号,先试最该用的;**失败了把结果报回池里**(状态机 + 冷却),
    然后**换下一个账号**接着试。这样:

      · 一个号被限/过期,**不会拖垮整轮**;
      · 已知在冷却里的号**不会被继续撞**(越撞越糟是本仓的教训);
      · 两条路(协议 / 页面)**共用同一份健康状态**。

    ⚠️ **签名类错误(`kind="sign"`)不换号** —— 那是算法问题,换号没用,直接抛。
    """
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return []
    from config.settings import get_settings

    from app.services import xhs_accounts as pool

    st = settings or get_settings()

    #: 候选:池里发现的账号;**池空**(数据库不可用/什么都没配)时退回旧单 cookie 逻辑,
    #: 保证"最坏情况还能跑",而不是直接抛。
    #: ⚠️ **只认"能直接拿出 cookie 串"的来源**(`COOKIE_KINDS`):纯浏览器档案
    #: (`profile_dir`)里没有 cookie 串,协议路取了它只会每轮白报一次失败,
    #: 还把那个其实好好的档案标脏。
    refs = pool.discover(st)
    cookie_refs = [r for r in refs if r.kind in pool.COOKIE_KINDS]

    if not cookie_refs:
        # 池里**一个能直接拿 cookie 的都没有** —— 分两种情形,别混成一句话:
        # ① `.env`/库里有单枚 cookie ⇒ 退回"单 cookie"那套(最坏也能跑);
        # ② 只有浏览器档案 ⇒ 协议路真用不了它们(那是页面路的东西) ——
        #    这条要**说准**,否则会跟"全在冷却中"混在一起,把人引到错误的修法上。
        if _cookie(session, user_id, st):
            refs = [pool.AccountRef(key="legacy", label="单 cookie 回落",
                                    kind="cookie_store", user_id=user_id)]
        elif refs:
            raise XhsProtocolError(
                "小红书**只有浏览器档案、没有协议凭据** —— 协议路需要一份"
                "`cookie + ssk` 的成套凭据。两条取法(按你的情形挑):\n"
                "  · **成套(cookie + ssk,推荐)**:`tools/xhs_browser_login.py <档案>` "
                "在浏览器里登录一次,它会同时导出 cookie 与 `localStorage[\"webSsk\"]`;\n"
                "  · **只要 cookie(档案已登录时最快)**:`tools/xhs_export_cookie.py <档案>` "
                "从已登录档案导出全套 cookie(**不含 ssk**),存进加密 cookie 库。\n"
                "之后 `tools/xhs_go_live.py` 一键验证归档", kind="need_login",
                needs_human=True)
        else:
            raise XhsProtocolError(
                "小红书**没有任何可用凭据** —— 两条取法(按你的情形挑):\n"
                "  · **成套(cookie + ssk,推荐)**:`tools/xhs_browser_login.py <档案>` "
                "在浏览器里登录一次,它会同时导出 cookie 与 `localStorage[\"webSsk\"]`;\n"
                "  · **只要 cookie(档案已登录时最快)**:`tools/xhs_export_cookie.py <档案>` "
                "从已登录档案导出全套 cookie(**不含 ssk**),存进加密 cookie 库。\n"
                "之后 `tools/xhs_go_live.py` 一键验证并归档", kind="need_login",
                needs_human=True)

    logger.info("小红书搜索:%s", pool.summary(st))

    tried: set[str] = set()
    last_exc: XhsProtocolError | None = None
    #: ⚠️ **归因要留全貌,不能只留最后一个** —— 账号池一轮会走过好几个号,
    #: "最后那个为什么失败"往往不是"整轮为什么失败"(本仓有过:只剩一句
    #: "全在冷却中",把"其实只是凭据缺 `web_session`"这个**立刻能修**的原因埋掉了)。
    failures: list[tuple[str, str]] = []      #: (账号标签, 原因)
    missing_items: set[str] = set()           #: 凭据缺了哪些关键项
    attempted = False                        #: 有没有**真的发出去过请求**
    while True:
        ref = pool.pick(st, exclude=tried, kinds=pool.COOKIE_KINDS)
        if ref is None:
            break
        tried.add(ref.key)
        label = ref.label or ref.key

        ck_str = pool.cookie_of(ref, st, session) or _cookie(session, user_id, st)
        ck = _cookie_dict(ck_str)
        if not ck.get("a1") or not ck.get("web_session"):
            miss = "a1" if "a1" not in ck else "web_session"
            missing_items.add(miss)
            pool.report(ref, pool.STATE_GUEST, f"凭据不全(缺 {miss})")
            failures.append((label, f"凭据不全(缺 {miss})"))
            logger.warning("小红书账号 %s 凭据不全(缺 %s),换下一个", label, miss)
            continue
        attempted = True
        if "id_token" not in ck:
            logger.warning("小红书账号 %s 没有 `id_token`(手抄的常见缺它)", label)

        ssk = pool.ssk_of(ref) or b""
        if not ssk and session is not None:
            try:
                from app.services.xhs_xys import load_ssk

                ssk = load_ssk(session, user_id)
            except Exception:  # noqa: BLE001
                logger.debug("读 ssk 失败,按没有处理", exc_info=True)

        #: ★ **指纹跟着凭据走** —— cookie / ssk / ua 三者同源(见 `xhs_fingerprint`)
        _ua = pool.ua_of(ref)
        _hints = pool.hints_of(ref)
        try:
            rows = _search_with(ck, ssk, kws, st, pages, session=session,
                                user_id=user_id, ua=_ua, hints=_hints)
        except XhsProtocolError as exc:
            state = pool.from_error(exc.kind, str(exc))
            pool.report(ref, state, str(exc))
            if exc.kind == "sign":
                #: 签名/算法问题 —— 换号无用,别浪费轮次
                raise
            last_exc = exc
            failures.append((label, f"{exc.kind}: {str(exc)[:120]}"))
            logger.warning("小红书账号 %s 失败(%s → %s),换下一个", label, exc.kind, state)
            continue
        pool.report(ref, pool.STATE_HEALTHY, f"{len(rows)} 条")
        return rows

    #: 所有账号都试过 / 都在冷却里 —— 按**已知最具体**的原因报,别一律说"冷却"
    if last_exc is not None:
        raise last_exc
    if missing_items and not attempted:
        #: 一枚请求都没发出去过 ⇒ **不是账号被限**,是**凭据本身不全**。
        #: 硬失败只认 `a1`/`web_session`(见 `_cookie_dict` 的判据)——
        #: 缺任一个必然不行,提前说清比"发一次再回 -101"有用。
        raise XhsProtocolError(
            f"小红书凭据**不完整**(缺 {', '.join(sorted(missing_items))})—— "
            f"协议路硬失败只认 `a1`/`web_session`,缺任一个必然不行。取一枚完整凭据:"
            f"`tools/xhs_export_cookie.py <档案>`(从**已登录**档案导出全套 cookie,不含 ssk)"
            f"或 `tools/xhs_browser_login.py <档案>`(成套 cookie + ssk)",
            kind="need_login", needs_human=True)
    recap = ";".join(f"{l}→{d}" for l, d in failures) or "(没有可试的账号)"
    raise XhsProtocolError(
        f"小红书账号**全在冷却中**(都是近期失败过的号)。{pool.summary(st)} —— "
        f"本轮各号结果:{recap}。"
        f"**别再撞了**:等冷却结束,或换一个新号登录 "
        f"(`tools/xhs_browser_login.py <新档案>`)", kind="restricted", needs_human=True)
