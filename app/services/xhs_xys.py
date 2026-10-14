# -*- coding: utf-8 -*-
"""小红书 `XYS_` 签名的**完整版**(2026-10-11,源码来自站点自己的 bundle)。

## 为什么另起一个
`xhshow` 那套只产出 `x0..x4`,**缺 `x5`/`x6`/`x7`** —— 而站点源码里 `seccore_signv2`
是这么拼的(逐字抄自 `vendor-dynamic.*.js`):

```js
function seccore_signv2(e, a) {            // e = 真实URL(含 query), a = body
  var c = e; a && (c += JSON.stringify(a));            // content = url + body
  var u = md5(c), p = md5(e), v = window.mnsv2(c, u, p);
  var S = { x0: <签名版本>, x1: "xhs-pc-web", x2: "Windows", x3: v,
            x4: a ? typeof a : "", x5: u };
  var R = buildEncSskSign(u);
  if (R) { S.x6 = R.encSskSign; S.x7 = R.encSsk; }
  return "XYS_" + <乱序base64>(JSON(S));
}
```

## `x6`/`x7` 是什么(同一份 bundle 里的 `buildEncSskSign`)
它读 **`localStorage["webSsk"]`**(服务端用 X25519 加密下发、页面解密后存进去的 `ssk`):

```js
S = nonce(4)  ‖  sha1(ssk ‖ nonce)  ‖  ssk[32:]
encSsk     = b64(S)
encSskSign = b64(sha1(u ‖ S))          // u = 上面那个 md5 十六进制串
```
(`nonce` 4 字节 = `uint32LE(Date.now() + uint32LE(random4))`;常量 `ea=en?..` 见下。)

⚠️ **`ssk` 必须与那枚会话同源**:2026-10-11 实测 —— 拿**别的上下文**的 `ssk` 去签
**已登录**的请求,服务端回 `-100 登录已过期`(而同一枚 cookie 在页面里明明是登录状态)。
**匿名**请求则带不带 `x6/x7` 都 `200`。⇒ **cookie 和 `ssk` 必须成对取、成对用**
(`tools/xhs_page_login.py` 已改成两者一起导出)。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.parse
from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

#: `localStorage` 里存 ssk 的键(源码:`W="webSsk"`)
LS_KEY = "webSsk"
#: 站点源码里的常量:`ea=32`(ssk 最小长度 / 取尾巴的偏移)、`et=32`(X25519 长度)
SSK_HEAD = 32
APP_ID = "xhs-pc-web"


def _ssk_key(user_id: int) -> str:
    return f"xhs_ssk_{user_id}"


# ------------------------------------------------------------------ 存取


def save_ssk(db, user_id: int, b64_ssk: str) -> None:
    """把 `ssk`(base64)与那枚 cookie **成对**存起来(存 `system_config`,与 cookie 库分离)。"""
    from sqlalchemy import select

    from app.db.models import SystemConfig

    row = db.scalar(select(SystemConfig).where(SystemConfig.key == _ssk_key(user_id)))
    if row is None:
        db.add(SystemConfig(key=_ssk_key(user_id), value=b64_ssk))
    else:
        row.value = b64_ssk


def load_ssk(db, user_id: int) -> bytes:
    """读 `ssk` 字节;**读不到回空**(调用方退回不带 `x6/x7` 的签名,而不是报错)。"""
    try:
        from sqlalchemy import select

        from app.db.models import SystemConfig

        row = db.scalar(select(SystemConfig).where(SystemConfig.key == _ssk_key(user_id)))
        if row is not None and row.value:
            return base64.b64decode(str(row.value))
    except Exception:  # noqa: BLE001 - 读不到就当没有
        logger.debug("读小红书 ssk 失败(按没有处理)", exc_info=True)
    return b""


# ------------------------------------------------------------------ 签名


def build_enc_ssk(u_hex: str, ssk: bytes) -> tuple[str, str]:
    """按站点源码算 `(encSskSign, encSsk)`;`ssk` 长度不足 `SSK_HEAD` 时回空串。

    ⚠️★ **2026-10-12 修正一个真 bug**。站点源码(`vendor-dynamic.js` 的 `buildEncSskSign`)是:
    ```js
    var a = hexDecode(e);                      // ★ 16 个原始字节
    r[appId] = stdB64(sha1(concat(a, S)));     // sha1(md5字节 ‖ S)
    ```
    而旧实现写的是 `sha1(u_hex.encode() + payload)` —— 把 **32 字符的十六进制串**喂进了
    sha1。二者输入长度(32 vs 16)与内容都不同 ⇒ **算出来的 `x6` 是错的**。
    这一处修掉;`tools/xhs_sign_verify.py` 会核对它(用旧写法必然对不上)。
    """
    if not ssk or len(ssk) < SSK_HEAD:
        return "", ""
    r = int.from_bytes(os.urandom(4), "little")
    nonce = ((int(time.time() * 1000) & 0xFFFFFFFF) + r & 0xFFFFFFFF).to_bytes(4, "little")
    payload = nonce + hashlib.sha1(ssk + nonce).digest() + ssk[SSK_HEAD:]
    #: ★ 修:喂 **16 个原始字节**(hex 解码后),不是 32 字符的 hex 串。
    #: ⚠️ `u_hex` 正常永远是 `md5().hexdigest()`(32 个十六进制字符);万一不是
    #: (调用方传错),**宁可不算**(回空 ⇒ 退化成不带 `x6/x7` 的匿名签名,服务端接受),
    #: 也不要抛出去把整条链打挂 —— 与本函数"不够就不带"的一贯原则一致。
    try:
        md5_bytes = bytes.fromhex(u_hex)
    except (ValueError, TypeError):
        logger.debug("`u` 不是合法的 md5 十六进制串(%r),不带 x6/x7", u_hex)
        return "", ""
    sign = hashlib.sha1(md5_bytes + payload).digest()
    return base64.b64encode(sign).decode(), base64.b64encode(payload).decode()


def _custom_b64(data: bytes) -> str:
    """`ed.xE`:标准 base64 → **乱序码表**(与库解 `XYS_` 用的是同一张表)。"""
    from xhshow.config.config import CryptoConfig as C

    t = str.maketrans(C.STANDARD_BASE64_ALPHABET, C.CUSTOM_BASE64_ALPHABET)
    return base64.b64encode(data).decode().translate(t)


def content_string(uri: str, params: dict | None = None, payload: dict | None = None) -> str:
    """`content = 真实URL(含 query) + 紧凑 JSON body` —— 与站点源码逐字一致。"""
    out = uri
    if params:
        q = "&".join(f"{k}={urllib.parse.quote(str(v), safe=',')}"
                     for k, v in params.items() if v is not None)
        out = f"{uri}?{q}" if q else uri
    if payload is not None:
        out += json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return out


def sign_xys(signer: Any, method: str, uri: str, a1: str, *, params: dict | None = None,
             payload: dict | None = None, ssk: bytes = b"", sign_version: str = "4.4.3",
             platform: str = "Windows") -> str:
    """产出**完整**的 `XYS_` 签名(含 `x5`/`x6`/`x7`)。

    `x3`(`mnsv2`)沿用库算的那份 —— 库那条路**没有**用 `SessionManager` 对齐
    `page_load_ts`/`sequence`,所以它与真浏览器**不是逐字节相同**,服务端实测接受
    (匿名请求 200)。真正卡人的是 `x5`/`x6`/`x7`,这里补上。
    """
    content = content_string(uri, params, payload)
    u_hex = hashlib.md5(content.encode()).hexdigest()
    lib_xys = signer.sign_xs(method, uri, a1, payload=payload)
    s_obj = signer.decode_xs(lib_xys)
    s_obj["x0"] = sign_version
    s_obj["x2"] = platform
    s_obj["x5"] = u_hex
    # x4 = body 的类型(站点源码:`a ? typeof a : ""`)
    s_obj["x4"] = ("object" if isinstance(payload, dict)
                   else "string" if isinstance(payload, str) else "")
    sign_v, raw_v = build_enc_ssk(u_hex, ssk)
    if sign_v:
        s_obj["x6"] = {APP_ID: sign_v}
        s_obj["x7"] = {APP_ID: raw_v}
    return "XYS_" + _custom_b64(json.dumps(s_obj, separators=(",", ":"),
                                           ensure_ascii=False).encode())


def headers(signer: Any, method: str, uri: str, cookies: dict[str, str], *,
            params: dict | None = None, payload: dict | None = None,
            ssk: bytes = b"", xs_common: str = "", extra: dict | None = None) -> dict[str, str]:
    """造一份可直接发的请求头(`x-s`/`x-t`/`x-s-common` + 可选覆盖)。"""
    out = {
        "x-s": sign_xys(signer, method, uri, str(cookies.get("a1") or ""),
                        params=params, payload=payload, ssk=ssk),
        "x-t": str(int(time.time() * 1000)),
        "x-s-common": xs_common or signer.sign_xs_common(cookies),
        "x-b3-traceid": signer.get_b3_trace_id(),
        "x-xray-traceid": signer.get_xray_trace_id(),
        "x-mns": "unload",
    }
    if extra:
        out.update(extra)
    return out
