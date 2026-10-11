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
   ⇒ 本模块**全程不开浏览器**(签名由 `xhshow` 在 Python 里算),天然躲开这条;
   **但也别**把这里拿到的 session 交给页面路去"刷新一下完整 cookie" —— 那正是把刚拿到的东西烧掉。
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
    """扫码登录的硬失败。`kind` 说清是哪一种,**`needs_human` 表示要人动手**(扫码/重来)。"""

    def __init__(self, msg: str, kind: str = "api", needs_human: bool = False) -> None:
        super().__init__(msg)
        self.kind = kind
        self.needs_human = needs_human


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


def _request(signer: Any, cookies: dict[str, str], *, method: str, path: str,
             params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None,
             sign_format: str = "xyw", x_rap: bool = False, timeout: int = 25) -> dict:
    """带签名请求一次。**签名用的字节 = 发出去的字节**(POST body 用库的 `build_json_body`)。

    GET 的 query 由 `_query()` 拼(与库同规则),POST 的 body 由 `build_json_body()` 出 ——
    两处都**不自己** `json.dumps`,因为"两边各序列化一次"是本仓反复踩的坑
    (上游实测:重新序列化哪怕只多一个空格,签名立刻失效,**症状是 406**,极易误判成风控)。
    """
    import requests

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
    try:
        r = requests.request(method.upper(), url, data=body, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 网络层失败要单独归一类,别混进"没登录"
        raise XhsQrLoginError(f"请求失败({path}):{type(exc).__name__}: {str(exc)[:90]}",
                              kind="network") from exc
    if r.status_code in (406, 461):
        raise XhsQrLoginError(
            f"签名被拒(HTTP {r.status_code})·{path} —— 多半是格式不匹配或 body 被重新序列化;"
            f"响应:{r.text[:110]}", kind="sign")
    if r.status_code == 471:
        # ⚠️ **471 是"服务端忙/限频",不是失败**(2026-10-11 实测:连续创建二维码后立刻轮询撞到过,
        # 而响应体仍是 `{"code":0,"success":true,"msg":"成功","data":{}}`;隔一会儿再打就正常)。
        # 单列一类,是为了让上层**下一轮接着试**,而不是把它当成"这条链坏了"。
        raise XhsQrLoginError(f"服务端忙/限频(HTTP 471)·{path} —— 下一轮重试即可",
                              kind="busy")
    if r.status_code != 200:
        raise XhsQrLoginError(f"HTTP {r.status_code}·{path}:{r.text[:110]}", kind="api")
    try:
        return r.json()
    except ValueError as exc:
        raise XhsQrLoginError(f"响应不是 JSON ·{path}:{r.text[:110]}", kind="api") from exc


# --------------------------------------------------------------------------- 三个接口


def new_identity(settings=None) -> tuple[dict[str, str], Any]:
    """造一份**全新的设备身份** + 打好补丁的签名器。

    ⚠️ 用**新 `a1`** 是有意的:上游的成功路径就是每次开一个全新的 headless 上下文
    (等于新设备)。`a1` 是设备标识、不是账号 —— 换新的不会动到任何账号。
    """
    from app.services.xhs_protocol_source import make_signer

    signer = make_signer(settings)
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


def create_qr(settings=None) -> QrTicket:
    """第 ① 步:创建二维码。"""
    cookies, signer = new_identity(settings)
    payload = {"qr_type": 1}
    resp, fmt = _try_formats(lambda f: _request(signer, cookies, method="POST",
                                                path=PATH_QR_CREATE, payload=payload,
                                                sign_format=f))
    if resp.get("code") != 0 or not (resp.get("data") or {}).get("qr_id"):
        raise XhsQrLoginError(f"创建二维码失败:{str(resp)[:160]}", kind="api")
    d = resp["data"]
    logger.info("小红书扫码登录:已创建二维码(格式 %s)", fmt)
    return QrTicket(qr_id=str(d["qr_id"]), code=str(d.get("code") or ""),
                    url=str(d.get("url") or ""), cookies=cookies, signer=signer,
                    sign_format=fmt, public_key_b64=_gen_public_key())


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
                      sign_format=t.sign_format, x_rap=t.x_rap)
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
                      sign_format=t.sign_format, x_rap=t.x_rap)
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
                x_rap: bool = False) -> bool:
    """`user/me` 的 `guest` 字段。**登录成功与否唯一的判据。**"""
    r = _request(signer, cookie, method="GET", path=PATH_USER_ME,
                 sign_format=sign_format, x_rap=x_rap)
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
