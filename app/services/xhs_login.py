# -*- coding: utf-8 -*-
"""小红书:**协议扫码登录**(2026-10-11)。

## 为什么需要它
我们卡住的从来不是**采集**,是**登录** —— 纯协议那枚 cookie 活不过几分钟,
而现行兜底是「人在浏览器档案里重登一次 + 再导一次 cookie」(要开 Edge、要人工过验证、
远程机还做不了)。扫码登录把这一步换成**扫一次码**:二维码印出来,手机扫一下就行。

## 流程(三步)
```
① POST /api/sns/web/v1/login/qrcode/create      {"qr_type": 1}
     → data: {qr_id, code, url}          url 用来【本地】生成二维码
② 轮询两条通道(约 2~3 秒一轮;二维码寿命实测 ~6.9 分钟,不是 60 秒)
   a) GET  …/login/qrcode/status?qr_id=…&code=…&client_public_key_base64=…
        —— **既注册 X25519 公钥,也是真凭据 `login_info` 的【唯一】下发通道**
   b) POST /api/qrcode/userinfo          {"qrId": …, "code": …}
        → `codeStatus` —— **过期判定以它为准**
③ 把 `login_info.session` 写进 `web_session` cookie 即完成登录
```

## ⚠️ 四个坑(每条都有人栽过;出处:上游 `suimi8/xhs-studio` 的 `login-flow.md`)
1. **凭据在 `qrcode/status` 的 `login_info`,不在 `activate`。**
   `activate` 是**独立铸发会话**的接口:**即使完全没人扫码**它也回 `code:0` + 一个格式正常的 session,
   但那是**游客档**(`user/me` 的 `guest=true`,session 前缀 `030037…`;真登录态前缀 `040069…`)。
   丢掉 `login_info` 去调 `activate` = 把真凭据扔了换回一张游客档,**表现为静默失效**(而"登录态"从一开始就不存在)。
2. **成功判据只能是 `user/me` 的 `guest === false`。**
   `userId` **任何阶段**都返回(未登录时是游客 id)、外层 `code` 只表示"接口调用成功" —— 都不能当判据。
   并且**要把「确定未登录」和「检测失败」分开**:`user/me` 自己报错(406/网络)不等于未登录,
   否则人会一直看到"未登录"、反复重扫。
3. **带登录 session 的无头浏览器再去导航 = 自杀。**
   实测:注入真 `web_session` 后**纯 API** 调 `user/me` 一切正常;一旦无头页面 `goto`
   小红书,服务端会**下发删除指令**把 `web_session` 作废。
   ⇒ **但也别**把这里拿到的 session 交给页面路去"刷新一下完整 cookie" —— 那正是把刚拿到的东西烧掉。

   ⚠️ **一处更正(2026-10-11)**:"全程不开浏览器"这句话**不再成立**:实测自己"编"的 `a1`
   在扫码确认后会被网关挡住(`status` 回 `471 + 空 data`),所以**创建二维码之前**会开一个
   **一次性、无档案、不登录**的 headless 页面,只为把**站点自己造的**身份 cookie 取回来
   (`browser_identity()`;`xhs_qr_login_browser=False` 可关掉,关掉就回落合成身份)。
   它与上面那条**不冲突**:烧掉登录态的是"**带着登录 session** 去导航",而这里取身份时
   **手里还没有任何登录态**。参考实现走的正是这一步。
4. **`qrcode/status` 是带 query 的 GET,query 必须参与签名**;而且**每一轮都要调它** ——
   它不是"备选通道",公钥注册与凭据下发都是它。

## 与 `xhs_protocol_source` 的关系
共用同一个**打过模板版本补丁**的签名器(`xhs_protocol_source._signer`),
以及同一条纪律:**签名用的字节 = 发出去的字节**
(POST body 一律用 `Xhshow.build_json_body()`,不自己 `json.dumps`,免得"两边各序列化一次")。

## 证据边界(别夸大)
上游的实测记录分列:`create` 由**纯协议自证**(200/`code=0`);`登录态存活`(`guest:false`)与
`0→1→2` 状态机是**真人扫码**跑出来的。本模块落地时**只验证了第 ① 步**(本机实测)。
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from app.utils import get_logger

logger = get_logger(__name__)

API_BASE = "https://edith.xiaohongshu.com"
PLATFORM = "xiaohongshu"
PATH_QR_CREATE = "/api/sns/web/v1/login/qrcode/create"
PATH_QR_STATUS = "/api/sns/web/v1/login/qrcode/status"
PATH_QR_USERINFO = "/api/qrcode/userinfo"
PATH_USER_ME = "/api/sns/web/v2/user/me"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
#: 二维码寿命实测 **~6.9 分钟**(414s),不是早期以为的 60 秒;真正的退出条件是 `codeStatus==3`。
#: 480s 预算刚好盖住一整个寿命,不会像"60 秒自动刷新"那样把还能用的码刷掉。
DEADLINE_S = 480.0
INTERVAL_S = 3.0
#: 真登录态 session 前缀(`04…`);`activate` 铸的游客档是 `03…`。判据用 `user/me`,这只是日志。
SESSION_PREFIX_REAL = "04"


class XhsQrLoginError(Exception):
    """扫码登录的硬失败。`kind` 说清是哪一种,**`needs_human` 表示要人动手**(扫码/重来)。

    `code` 是**业务码**(信封里的 `code`),只在 `kind == "api"` 时有意义 ——
    判「确定未登录」要靠它,不能靠 HTTP 状态码(见 `LOGGED_OUT_CODES`)。
    """

    def __init__(self, msg: str, kind: str = "api", needs_human: bool = False,
                 code: int | None = None) -> None:
        super().__init__(msg)
        self.kind = kind
        self.needs_human = needs_human
        self.code = code


#: **确定"没登录"**的业务码。⚠️ 与"检测失败"必须分开(上游《login-flow》陷阱 2):
#: 把 406/网络/签名失败也当成未登录,人会一直重扫,而真因是自查请求根本没发出去。
LOGGED_OUT_CODES: frozenset[int] = frozenset({-100, -101, -104, 300011})


@dataclass
class QrTicket:
    """一次扫码会话的全部状态(二维码 + 身份 + 签名器 + 已探明的签名格式)。"""

    qr_id: str
    code: str
    url: str
    cookies: dict[str, str]
    signer: Any                       # xhshow.Xhshow(带模板补丁)
    sign_format: str = "xyw"
    x_rap: bool = False
    _params_extra: dict[str, str] = field(default_factory=dict)
    public_key_b64: str = ""
    #: **从真实页面截获的 `x-s-common`**(指纹头)。空 = 用库自合成的那份(见 `PageIdentity`)。
    xs_common: str = ""


@dataclass
class PollResult:
    """一轮轮询的**原始事实**。`error` 有值 ⇒ 这一轮**不能**解释成"还在等扫码"。"""

    code_status: int | None = None    # 0=待扫 1=已扫待确认 2=已确认(唯一成功信号) 3=过期
    login_info: dict[str, Any] = field(default_factory=dict)
    expired: bool = False
    error: str = ""
    busy: bool = False                # 这一轮的失败**全是** 471(服务端忙)⇒ 重试即可,不是链坏了

    @property
    def confirmed(self) -> bool:
        return self.code_status == 2 or bool(self.login_info.get("session"))


# --------------------------------------------------------------------------- 签名/HTTP


def _query(params: dict[str, Any]) -> str:
    """按**库自己的规则**拼 query(值 urlencode、不是键),顺序照 dict。

    ⚠️ 必须与 `Xhshow._build_content_string` 对 GET 的拼法**逐字节一致** ——
    `x5` 是拿这个串算的,少一个 `%2F` 都会被网关当成签名不符
    (上游原话:漏了 query "直接被网关拒")。`tests/test_xhs_qr_login.py` 里有对拍断言。
    """
    parts: list[str] = []
    for k, v in params.items():
        if isinstance(v, (list, tuple)):
            s = ",".join(str(x) for x in v)
        elif v is None:
            s = ""
        else:
            s = str(v)
        parts.append(f"{k}={urllib.parse.quote(s, safe=',')}")
    return "&".join(parts)


def _transport():
    """传输层:**优先 `curl_cffi`(impersonate=chrome,模拟真实 TLS/HTTP2 指纹)**,缺了回落 `requests`。

    ⚠️ **为什么登录链上必须是它**(2026-10-11 实测):HTTP 层的变量我们已经**改遍了**都没用 ——
    签名格式(`xyw`/`xys`)、补 `Referer`/`Origin`、去掉公钥参数、cookie 罐、**身份来源**
    (库"编"的 `a1` vs 站点页面 JS 造的)、轮询次数、**连换一个全新账号都一样**
    ⇒ 剩下的最大嫌疑就是 **HTTP 以下的指纹**:裸 `requests` 的 TLS 握手(JA3/JA4)一看就是脚本,
    而 WAF 完全可能**只对"像脚本的客户端"扣住"发凭据"这个动作**,同时放行浏览类接口
    (这正好解释:`create`/`userinfo` 都正常,**只有"发凭据"那一下被换成空 `data`**)。
    **本仓的闲鱼那条链早就为同样的理由换成了 `curl_cffi`**(见 `app/services/xianyu.py` 开头),
    这里沿用它 —— 不新造第二套说法。
    """
    try:
        from curl_cffi import requests as creq

        return creq
    except Exception:  # noqa: BLE001 - 没有就回落,别把工具卡死
        logger.warning("没有 curl_cffi,登录链回落裸 requests(TLS 指纹是脚本特征)")
        import requests

        return requests


def _raw(signer: Any, cookies: dict[str, str], *, method: str, path: str,
         params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None,
         sign_format: str = "xyw", x_rap: bool = False, timeout: int = 25,
         extra_headers: dict[str, str] | None = None,
         xs_common: str = "") -> tuple[int, str]:
    """**不做任何判读**地打一次请求,原样返回 `(HTTP 状态码, 响应原文)`。

    单独拆出来是给**诊断探针**用的:那种"只在某个状态下才出现"的怪象
    (例:扫完确认后 `status` 开始回 471/空 `data`)**没法事后复现**(`qr_id` 一次性),
    必须在当场用"同一张票、同一时刻、只变一个变量"的方式把原文抓下来。
    """
    if method.upper() == "GET":
        q = _query(params or {})
        url = API_BASE + path + (f"?{q}" if q else "")
        headers = signer.sign_headers_get(path, cookies, params=params or {},
                                          sign_format=sign_format, x_rap=x_rap)
        body = None
    else:
        url = API_BASE + path
        headers = signer.sign_headers_post(path, cookies, payload=payload or {},
                                           sign_format=sign_format, x_rap=x_rap)
        body = signer.build_json_body(payload or {})
    headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers["content-type"] = "application/json;charset=UTF-8"
    headers["user-agent"] = _UA
    if xs_common:
        # ⚠️ 用**真实页面截获的那份**替换库自合成的 `x-s-common`(指纹头)。理由见 `PageIdentity`。
        headers["x-s-common"] = xs_common
    if extra_headers:
        headers.update(extra_headers)
    # ⚠️ **HTTP 层一个都不动,只换传输层**(TLS/HTTP2 指纹)——这样"下一个变量"才只有一个。
    tp = _transport()
    kw: dict[str, Any] = {"data": body, "headers": headers, "timeout": timeout}
    if tp.__name__.startswith("curl_cffi"):
        kw["impersonate"] = "chrome"
    r = tp.request(method.upper(), url, **kw)
    return int(r.status_code), r.text


def _request(signer: Any, cookies: dict[str, str], *, method: str, path: str,
             params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None,
             sign_format: str = "xyw", x_rap: bool = False, timeout: int = 25,
             xs_common: str = "") -> dict:
    """带签名请求一次,并**判读信封**。**签名用的字节 = 发出去的字节**(见 `_raw`)。

    GET 的 query 由 `_query()` 拼(与库同规则),POST 的 body 由 `build_json_body()` 出 ——
    两处都**不自己** `json.dumps`,因为"两边各序列化一次"是本仓反复踩的坑
    (上游实测:重新序列化哪怕只多一个空格,签名立刻失效,**症状是 406**,极易误判成风控)。
    """
    try:
        r_status, r_text = _raw(signer, cookies, method=method, path=path, params=params,
                                payload=payload, sign_format=sign_format, x_rap=x_rap,
                                timeout=timeout, xs_common=xs_common)
    except Exception as exc:  # noqa: BLE001 - 网络层失败要单独归一类,别混进"没登录"
        raise XhsQrLoginError(f"请求失败({path}):{type(exc).__name__}: {str(exc)[:90]}",
                              kind="network") from exc
    if r_status in (406, 461):
        raise XhsQrLoginError(
            f"签名被拒(HTTP {r_status})·{path} —— 多半是格式不匹配或 body 被重新序列化;"
            f"响应:{r_text[:110]}", kind="sign")
    if r_status == 471:
        # ⚠️ **471 是"服务端忙/限频",不是失败**(2026-10-11 实测:连续创建二维码后立刻轮询撞到过,
        # 而响应体仍是 `{"code":0,"success":true,"msg":"成功","data":{}}`;隔一会儿再打就正常)。
        # 单列一类,是为了让上层**下一轮接着试**,而不是把它当成"这条链坏了"。
        # ⚠️ **body 必须带出来**:2026-10-11 撞到一个"只有被扫过的那张票才 471、新票同一时刻 200"的怪象,
        # 而当时这条消息里没有 body ⇒ **完全无法排查**。凡"说不清是什么"的错误,都要把原文带上。
        raise XhsQrLoginError(f"服务端忙/限频(HTTP 471)·{path} —— 下一轮重试即可;"
                              f"响应:{r_text[:160]}", kind="busy")
    if r_status != 200:
        raise XhsQrLoginError(f"HTTP {r_status}·{path}:{r_text[:110]}", kind="api")
    try:
        d = json.loads(r_text)
    except ValueError as exc:
        raise XhsQrLoginError(f"响应不是 JSON ·{path}:{r_text[:110]}", kind="api") from exc
    # ⚠️⚠️ **HTTP 200 不等于成功** —— 2026-10-11 实测:`status` 在被扫过之后会回
    # `HTTP 200 + {"code":-1,"success":false,"msg":"登录异常"}`。只按状态码判断的话,
    # 这**会被静默吞成"还在等扫码"**(本仓反复栽的那一类),而它其实是服务端明确在拒绝。
    # ⇒ 信封里的 `code`/`success` 必须一起看。
    d = d if isinstance(d, dict) else {}
    code = d.get("code")
    if d.get("success") is False or (isinstance(code, int) and code != 0):
        raise XhsQrLoginError(f"接口报错 ·{path}:msg={str(d.get('msg'))[:60]} code={code}",
                              kind="api", code=code if isinstance(code, int) else None)
    return d


# --------------------------------------------------------------------------- 三个接口


def _signer_for(settings=None, env_flags: str = "") -> Any:
    """签名器只能从**那一个门**出来(带模板版本 + 环境标志两处补丁,见 `make_signer`)。

    `env_flags` 传**页面实时算出来的**那串 —— 它比任何写死的值都准,见 `_probe_env_flags`。
    """
    from app.services.xhs_protocol_source import make_signer

    return make_signer(settings, env_flags)


def new_identity(settings=None) -> tuple[dict[str, str], Any]:
    """造一份**合成的**设备身份 + 打好补丁的签名器(纯协议,**一个字节都不出网**)。
    ⚠️ 它是**回落**用的 —— 首选 `browser_identity()`(站点自己造的 `a1`),理由见其 docstring。
    """
    signer = _signer_for(settings)
    a1 = signer.generate_a1()
    return {"a1": a1, "webId": signer.generate_web_id(a1),
            "xsecappid": "xhs-pc-web"}, signer


def _gen_public_key() -> str:
    """X25519 公钥(base64,raw 32 字节)。

    `qrcode/status` 每轮都要带上它(**注册公钥**)—— 虽然我们不用 `activate`,
    但这个参数本身就是那条 GET 的 query 的一部分,少一个签名就对不上。
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    priv = X25519PrivateKey.generate()
    return base64.b64encode(priv.public_key().public_bytes_raw()).decode()


def _try_formats(fn: Callable[[str], Any], fmts: tuple[str, ...] = ("xyw", "xys")) -> tuple[Any, str]:
    """签名格式的**降级梯**:`xyw` 不行就试 `xys`,并把**哪个成了**回传(后续要同款)。

    `xyw` 优先是因为它和我们的搜索路一致;`xys` 是上游扫码登录用的那条(2026-08 实测可用)。
    只有**签名被拒**才降级 —— 网络错/业务错降级毫无意义,只会多打一次。
    """
    last: Exception | None = None
    for f in fmts:
        try:
            return fn(f), f
        except XhsQrLoginError as exc:
            if exc.kind != "sign":
                raise
            last = exc
            logger.info("小红书扫码登录:`%s` 格式被拒,降级下一个", f)
    raise last or XhsQrLoginError("签名格式试完仍不行", kind="sign")


def diagnose_status(t: QrTicket) -> list[tuple[str, int, str]]:
    """★ **一次性诊断**:同一张票、同一时刻、**只变一个变量**,把原文全打出来。

    为什么非要有它:有些怪象**只在特定状态下出现**(例:扫完确认后 `status` 开始回
    `471 + 空 data`),而 `qr_id` 是**一次性**的 —— 状态一过就**没法事后复现**,
    只能趁它当场发生时抓。2026-10-11 就吃过这个亏:第一轮卡住时手里没有 qr_id/公钥,
    连"换个进程复现一次"都做不到。

    返回 `[(变量说明, HTTP 状态码, 响应原文), …]`。
    """
    params = {"qr_id": t.qr_id, "code": t.code, "client_public_key_base64": t.public_key_b64}
    cases: list[tuple[str, dict]] = [
        (f"① 原样({t.sign_format}, 带公钥)", {}),
        (f"② 换签名格式({_other_format(t.sign_format)})", {"sign_format": _other_format(t.sign_format)}),
        ("③ 补 Referer/Origin(上游有、我们没发)", {
            "extra_headers": {"Referer": "https://www.xiaohongshu.com/",
                              "Origin": "https://www.xiaohongshu.com"}}),
        ("④ 不带公钥参数", {"params": {"qr_id": t.qr_id, "code": t.code}}),
    ]
    out: list[tuple[str, int, str]] = []
    for name, over in cases:
        try:
            st, tx = _raw(t.signer, t.cookies, method="GET", path=PATH_QR_STATUS,
                          params=over.get("params", params),
                          sign_format=over.get("sign_format", t.sign_format),
                          x_rap=t.x_rap, extra_headers=over.get("extra_headers"),
                          xs_common=t.xs_common)
        except Exception as exc:  # noqa: BLE001 - 诊断本身不许把主流程带崩
            st, tx = -1, f"{type(exc).__name__}: {exc}"
        out.append((name, st, tx.replace("\n", " ")[:200]))
    return out


def _other_format(fmt: str) -> str:
    return "xys" if str(fmt) == "xyw" else "xyw"


def enrich_cookies(cookies: dict[str, str], *, paths: tuple[str, ...] = ("/",),
                   timeout: int = 25) -> dict[str, str]:
    """**用纯 HTTP 打几个页面,把站点自己下发的 cookie 并进罐里**(不开浏览器)。

    ⚠️ 为什么需要:我们自己造的罐只有 `a1`/`webId`/`xsecappid` 三项,**全是本地编的**;
    而真实浏览器还带着**站点下发**的那几枚 —— 尤其 `acw_tc` 是 **WAF 网关 cookie**。

    ⚠️ 为什么把它当嫌疑:2026-10-11 实测,扫码确认之后 `status` 稳定回
    `471 + {"code":0,"success":true,"data":{}}`,而四种**请求形状**的对照
    (签名格式 `xyw`/`xys`、补 `Referer`/`Origin`、去掉公钥参数)**全部一模一样**
    ⇒ 不是形状问题。四个对照里**唯一没验过的变量就是 cookie 罐**。

    ⚠️ 拿不到就**原样返回**(调用方不用管失败):这几枚是"更像真实浏览器",不是硬前提。
    """
    import requests

    out = dict(cookies)
    try:
        s = requests.Session()
        s.cookies.update(out)
        for p in paths:
            s.get("https://www.xiaohongshu.com" + p, timeout=timeout,
                  headers={"user-agent": _UA,
                           "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                           "accept-language": "zh-CN,zh;q=0.9"})
        got = {c.name: c.value for c in s.cookies if c.value}
        new = [k for k in got if k not in out]
        out.update(got)
        if new:
            logger.info("小红书扫码登录:从页面补到 cookie %s(共 %d 项)", new, len(out))
    except Exception:  # noqa: BLE001 - 补不到就照原样走,别把登录带崩
        logger.warning("小红书扫码登录:补页面 cookie 失败(照原样继续)", exc_info=True)
    return out


@dataclass
class PageIdentity:
    """一次性页面取回来的**真实身份**:cookie 罐 + **真实 `x-s-common`**。

    ⚠️ `xs_common` 为什么必须一起取(2026-10-11):参考实现(已端到端验证)发送的是
    **从真实页面截获的** `x-s-common`,而我们此前一直用**库自己合成**的那份。
    它那份文档明确记着登录链**三个接口全都带着** `x-s-common` 里的 **`x6`/`x7`
    (`encSskSign`/`encSsk`)**—— 那两个字段**我们的签名库产不出来**。
    而 `x-s-common` 是**指纹头**,服务端完全可能拿它判断"这个客户端配不配拿凭据"。
    """

    cookies: dict[str, str] = field(default_factory=dict)
    xs_common: str = ""
    #: **页面当场算出来的 `XYW_` 环境标志**(19 位)。取值含义见 `xhs_protocol_source.SIGN_ENV_FLAGS`。
    env_flags: str = ""


def browser_identity(*, timeout_ms: int = 30000, settle_ms: int = 2500) -> PageIdentity:
    """用**一次性 headless 页面**取一份**真实身份** cookie(站点 JS 自己造的 `a1` 等)。

    ⚠️ **这不是"登录"**:开的是**全新的、无档案的**上下文 —— 不碰任何账号、不读任何登录态、
    不留档案,拿完就关。它只让页面跑一遍 JS,把站点**真正下发**的那几枚 cookie 取回来。

    ⚠️ **为什么要它**(2026-10-11 实测):我们自己"编"的 `a1`(库的 `generate_a1()`)在
    **扫码确认之后**会被网关挡住 —— `status` 稳定回 `471 + {"code":0,"success":true,"data":{}}`。
    而四个**请求形状**的对照(签名格式 `xyw`/`xys`、补 `Referer`/`Origin`、去掉公钥参数)
    **全部一模一样** ⇒ 不是形状问题;`userinfo` 同一时刻还是好的 ⇒ 也不是 IP 整体被封。
    剩下的差异就只有"身份是不是站点自己造的" —— 而站点**不会**从 HTTP 响应里发 `a1`
    (实测:不带 cookie 打首页,只回 `acw_tc`/`abRequestId`),`a1` 只能由页面 JS 生成。
    参考实现(已端到端验证过)走的正是这一步。
    """
    from playwright.sync_api import sync_playwright

    jar: dict[str, str] = {}
    xs_common = ""
    seen: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        try:
            ctx = b.new_context(user_agent=_UA, locale="zh-CN")
            page = ctx.new_page()

            def _grab(req) -> None:
                """截获页面自己发的 **真实 `x-s-common`**(它对每个业务请求都带)。"""
                if seen or "xiaohongshu.com" not in req.url:
                    return
                try:
                    v = (req.headers or {}).get("x-s-common") or ""
                except Exception:  # noqa: BLE001 - 头拿不到就当没有,别把取身份带崩
                    return
                if v:
                    seen.append(v)

            page.on("request", _grab)
            page.goto("https://www.xiaohongshu.com/explore",
                      wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(settle_ms)      # 等页面把 a1/webId/gid 这些写出来
            jar = {c["name"]: c["value"]
                   for c in ctx.cookies("https://www.xiaohongshu.com") if c.get("value")}
            env_flags = _probe_env_flags(page)
        finally:
            b.close()
    if not jar.get("a1"):
        raise XhsQrLoginError("页面没给出 a1(站点结构可能变了)", kind="env")
    xs_common = seen[0] if seen else ""
    logger.info("小红书扫码登录:取到真实身份 cookie %d 项:%s;真实 x-s-common %s;环境标志 %s",
                len(jar), sorted(jar), f"{len(xs_common)} 字节" if xs_common else "**没截到**",
                env_flags or "**没取到**")
    return PageIdentity(cookies=jar, xs_common=xs_common, env_flags=env_flags)


def _probe_env_flags(page) -> str:
    """**让页面自己签一个废包**,再用公开的密钥/IV 解开,读出那串环境标志。

    ⚠️ 这是"拿真值"最省的一条路:**不用逆任何 JS** —— `XYW_` 是 AES-128-CBC,
    密钥/IV 都写死在网页里(公开常量),所以我们签的、解的都能对上。
    取不到就回空串,由 `xhs_protocol_source._env_flags()` 逐级兜底。
    """
    import hashlib
    import json as _json

    from app.services.xhs_protocol_source import parse_env_flags

    body = {"qr_type": 1}
    c = PATH_QR_CREATE + _json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    try:
        out = page.evaluate("([c,u,p]) => window._webmsxyw(c,u,p)",
                            [c, hashlib.md5(c.encode()).hexdigest(),
                             hashlib.md5(PATH_QR_CREATE.encode()).hexdigest()])
        return parse_env_flags((out or {}).get("X-s", "") if isinstance(out, dict) else "")
    except Exception:  # noqa: BLE001 - 页面没这个函数/结构变了,都只是"没取到"
        logger.info("小红书扫码登录:页面上取不到 _webmsxyw(环境标志走内置兜底)", exc_info=True)
        return ""


def create_qr(settings=None) -> QrTicket:
    """第 ① 步:创建二维码。"""
    if settings is None:
        from config.settings import get_settings
        settings = get_settings()
    cookies: dict[str, str] = {}
    xs_common = ""
    env_flags = ""
    if bool(getattr(settings, "xhs_qr_login_browser", True)):
        try:
            ident = browser_identity()      # ← 站点造的身份 + 真实 x-s-common + **真实环境标志**
            cookies, xs_common, env_flags = ident.cookies, ident.xs_common, ident.env_flags
        except Exception:  # noqa: BLE001 - 取不到就回落合成身份,别把工具卡死
            logger.warning("小红书扫码登录:取真实身份失败,回落合成身份", exc_info=True)
    if not cookies.get("a1"):
        cookies = new_identity(settings)[0]
    cookies = enrich_cookies(cookies)
    signer = _signer_for(settings, env_flags)
    payload = {"qr_type": 1}
    resp, fmt = _try_formats(lambda f: _request(signer, cookies, method="POST",
                                                path=PATH_QR_CREATE, payload=payload,
                                                sign_format=f, xs_common=xs_common))
    if resp.get("code") != 0 or not (resp.get("data") or {}).get("qr_id"):
        raise XhsQrLoginError(f"创建二维码失败:{str(resp)[:160]}", kind="api")
    d = resp["data"]
    logger.info("小红书扫码登录:已创建二维码(格式 %s)", fmt)
    return QrTicket(qr_id=str(d["qr_id"]), code=str(d.get("code") or ""),
                    url=str(d.get("url") or ""), cookies=cookies, signer=signer,
                    sign_format=fmt, public_key_b64=_gen_public_key(), xs_common=xs_common)


def poll_once(t: QrTicket) -> PollResult:
    """第 ② 步:轮询**两条**通道一次。

    ⚠️ 两条通道的字段名不同(驼峰 `codeStatus` vs 下划线 `code_status`),**过期以 `userinfo` 为准**;
    ⚠️ 凭据**只**从 `status` 的 `login_info` 取,**不看 `code_status==2` 当前置条件**
    (状态与凭据下发不同步时,多加一道判断只会白等到超时 —— 上游就是这么踩的)。
    ⚠️ 任何一路失败都**不能让这一轮看起来像"还在等扫码"**:`error` 必须带上。
    """
    out = PollResult()
    errs: list[str] = []
    kinds: list[str] = []
    params = {"qr_id": t.qr_id, "code": t.code, "client_public_key_base64": t.public_key_b64}
    try:
        r2 = _request(t.signer, t.cookies, method="GET", path=PATH_QR_STATUS, params=params,
                      sign_format=t.sign_format, x_rap=t.x_rap, xs_common=t.xs_common)
        d2 = r2.get("data") or {}
        li = d2.get("login_info") or {}
        if li.get("session"):
            out.login_info = li
        if d2.get("code_status") is not None:
            out.code_status = int(d2["code_status"])
    except XhsQrLoginError as exc:
        errs.append(f"status:{exc}")
        kinds.append(exc.kind)
    try:
        r3 = _request(t.signer, t.cookies, method="POST", path=PATH_QR_USERINFO,
                      payload={"qrId": t.qr_id, "code": t.code},
                      sign_format=t.sign_format, x_rap=t.x_rap, xs_common=t.xs_common)
        cs = ((r3.get("data") or {}).get("codeStatus"))
        if cs is not None:
            out.code_status = int(cs)          # ← 过期判定以这条为准
        if cs == 3:
            out.expired = True
    except XhsQrLoginError as exc:
        errs.append(f"userinfo:{exc}")
        kinds.append(exc.kind)
    if errs and not out.login_info:
        out.error = " | ".join(errs)
        out.busy = bool(kinds) and all(k == "busy" for k in kinds)
    return out


def wait_for_login(t: QrTicket, *, on_status: Callable[[PollResult], None] | None = None,
                   deadline_s: float = DEADLINE_S, interval_s: float = INTERVAL_S) -> dict:
    """轮询到**拿到凭据**或**过期/超时**为止。返回 `login_info`(含 `session`/`user_id`)。"""
    end = time.time() + float(deadline_s)
    last_err = ""
    while time.time() < end:
        r = poll_once(t)
        if on_status is not None:
            on_status(r)
        if r.login_info.get("session"):
            return r.login_info
        if r.expired:
            raise XhsQrLoginError("二维码已过期/失效 —— 重新跑一次本工具即可", kind="expired")
        if r.error and r.error != last_err:
            last_err = r.error
            # ⚠️ 471(服务端忙)是**正常波动**,不该按"链坏了"告警 —— 但也绝不能不吭声
            if r.busy:
                logger.info("小红书扫码登录:服务端忙(471),下一轮重试")
            else:
                logger.warning("小红书扫码登录:一轮里有通道失败(不是「还在等扫码」):%s", r.error)
        time.sleep(float(interval_s))
    raise XhsQrLoginError(f"等待超时({int(deadline_s)}s)未扫码", kind="timeout", needs_human=True)


def me_is_guest(cookie: dict[str, str], signer: Any, *, sign_format: str = "xyw",
                x_rap: bool = False, xs_common: str = "") -> bool:
    """`user/me` 的 `guest` 字段。**登录成功与否唯一的判据。**

    ⚠️ **「确定未登录」与「检测失败」必须分开**(上游陷阱 2):
    - 服务端**明确**说了"没登录/过期/受限"(`LOGGED_OUT_CODES`) ⇒ `True`,是真没登录;
    - 请求**自己**没发成功(签名被拒/网络/471) ⇒ **原样抛出**,别糊成"未登录" ——
      否则人会一遍遍重扫,而真因根本不在登录上。
    """
    try:
        r = _request(signer, cookie, method="GET", path=PATH_USER_ME,
                     sign_format=sign_format, x_rap=x_rap, xs_common=xs_common)
    except XhsQrLoginError as exc:
        if exc.code in LOGGED_OUT_CODES:
            logger.info("小红书自查:服务端明确说没登录(code=%s)—— 不是检测失败", exc.code)
            return True
        raise
    d = r.get("data") or {}
    return bool(d.get("guest", True))


def adopt_session(t: QrTicket, login_info: dict) -> dict[str, str]:
    """把 `login_info.session` 合进身份 cookie,得到一枚**完整**的登录 cookie。

    ⚠️ 只取 `session`(→ `web_session`);`secure_session`/`ssk` 是**另一类凭据**,
    不进 cookie 头(页面路会自己带着它们,纯协议用不上)。
    """
    session = str(login_info.get("session") or "")
    if not session:
        raise XhsQrLoginError("login_info 里没有 session", kind="api")
    ck = dict(t.cookies)
    ck["web_session"] = session
    return ck
