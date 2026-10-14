# -*- coding: utf-8 -*-
"""小红书:**纯 Python 复刻签名算法**(2026-10-12,逐字对照站点真实源码)。

## 这份实现的来历(与"用第三方库"的区别)
`xhshow` 是 PyPI 上 `license: None` 的库,哪天停更我们就得干等。本模块的每一行都来自
**从站点 bundle 里读出来的真实源码**(`data/xhs_sign_dump/vendor-dynamic.7cd8cd04.js`
的 module 384 与 ssk 段),不是猜的。抽取方式见 `tools/xhs_sign_dump.py`。

## 真实源码里读到的关键事实(★ = 与我们旧实现不同,已修正)

| 项 | 真实源码 | 旧实现 | 处理 |
|---|---|---|---|
| `buildEncSskSign` 的输入 | `hexDecode(u)` → **16 字节**再 hash | `u_hex.encode()` → **32 字符 ASCII** | ✅ **已修**(这是真 bug) |
| `ed.lz` | `encodeUtf8`(UTF-8 编码,**不是**洗牌) | 当成乱序 | ✅ 已正名 |
| `ed.xE` | 自定义字母表 base64,**带 `=` 补垫** | 翻译标准表 | ✅ 等价,已按原样实现 |
| `ed.tb`(`x-s-common.x9`) | CRC32 **末位异或 `0xedb88320`**(不是 `0xffffffff`) | 用标准 crc32 | ✅ 已按原样实现 |
| `x6`/`x7` | **按 appId 的字典**(`{"xhs-pc-web": …}`) | 已是对 | ✅ 一致 |
| `x4` | `a ? typeof a : ""`(body 的类型) | 已对 | ✅ 一致 |
| `mnsv2`(`x3`) | 重度混淆,**须浏览器执行** | 库复刻(非逐字节) | ⚠️ 见下 |

## ⚠️ `x3` 是唯一无法纯 Python 复刻的一项
`x3 = window.mnsv2(content, md5(content), md5(url))`,而 `mnsv2` 是控制流平坦化 +
十六进制改名(`_0x30ce91` 那类)的重度混淆,**逆向成本极高**。
⇒ 所以本模块把 `x3` 做成**可注入**(`x3_provider`):
  · 生产环境用 `xhs_sign_oracle`(让页面算,**永远正确**);
  · 离线/调试用 `xhshow` 复刻的那份(服务端实测接受,非逐字节)。
除此之外的一切(x0/x1/x2/x4/x5/x6/x7 与最终编码)**都是纯 Python、可离线复现的**。

## 用法
```python
from app.services.xhs_sign_pure import seccore_signv2, decode_xys
sig = seccore_signv2(url, payload, ssk=ssk_bytes, x3=..., sign_version="4.4.3")
fields = decode_xys(sig)          # 解回来核对(验证工具用)
```
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import urllib.parse
from typing import Any, Callable

# ------------------------------------------------------------------ 常量
#: ★ 自定义 base64 字母表(真实源码 module 384,原样抄)
CUSTOM_B64_ALPHABET = ("ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5")
_STD_B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_TO_CUSTOM = str.maketrans(_STD_B64_ALPHABET, CUSTOM_B64_ALPHABET)
_TO_STD = str.maketrans(CUSTOM_B64_ALPHABET, _STD_B64_ALPHABET)

#: ssk 相关常量(真实源码 ssk 段,原样抄)
LS_KEY = "webSsk"                  #: localStorage 里存 ssk 的键
APP_ID = "xhs-pc-web"              #: getCurrentWebSskAppId() 的返回值
SSK_MIN_LEN = 32                   #: `ea`
SSK_HEAD = 32                      #: `ea`(取尾巴的偏移,与最小长度同值)
NONCE_LEN = 4                      #: `er`
X25519_LEN = 32                    #: `et`
AES_GCM_NONCE_LEN = 12             #: `en`
#: ★ **服务端 X25519 公钥**(真实源码里的硬编码常量 `ef`)。
#: 有了它,X25519 密钥交换可以**完全自己算** —— 不必依赖浏览器。
SERVER_PUBLIC_KEY_B64 = "Kr0iygsCu3inYJNXCL4k4JuzaYQ2afI1xbwc7BH6sm8="

#: `x-s-common` 里的 app 版本(真实源码 `x4:"6.57.2"`)
APP_VERSION = "6.57.2"

# ------------------------------------------------------------------ XYW_
#: ★★ **`XYW_` 格式的完整参数**(2026-10-12 从真实签名里解出来的,不是猜的)。
#: 它是当前线上**实际在用**的格式(oracle 探针拿到的一律是 `XYW_`)。
#: 外层是 base64 包 JSON,内层 AES-128-CBC 加密的是一段 `x1=..;x2=..;x3=..;x4=..`。
#:
#: | 字段 | 值 | 来源 |
#: |---|---|---|
#: | `x1` | `md5("url=" + content)` | **实测三例全中**(content = URI+body) |
#: | `x2` | 19 位环境标志,`|` 分隔 | 实测与 `xhs_protocol_source.SIGN_ENV_FLAGS` 逐位相同 |
#: | `x3` | `a1`(设备标识) | 实测等于 cookie 里的 a1 |
#: | `x4` | 毫秒时间戳 | 实测 |
#:
#: ⚠️ **`x3` 在页面加载时就被冻结**:改 `document.cookie` 不跟随、加载前注入会被页面覆盖
#: (2026-10-12 实测两次)。⇒ 用 oracle 时,签名里的 a1 永远是**页面自己的**那一枚,
#: 与我们真实凭据里的 a1 **不是同一枚** —— 这很可能就是 `-100 登录已过期` 的成因之一。
#: 纯 Python 复刻则没有这个问题:`x3` 用我们自己的 a1。
XYW_SIGN_SVN = "56"
XYW_SIGN_TYPE = "x2"
XYW_SIGN_VERSION = "1"
#: ★ AES-128 的密钥与 IV —— **写死在网页里的公开常量**(16 字节 ASCII)。
#: 没有"私钥"概念,谁都能加解密;这也是我们能解出上面那张表的原因。
XYW_AES_KEY = b"7cc4adla5ay0701v"
XYW_AES_IV = b"4uzjr7mbsibcaldp"


# ------------------------------------------------------------------ 基础编码

def md5_hex(s: str) -> str:
    """`ed.Pu` —— 标准 MD5,返回十六进制串。"""
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def crc32_variant(s: str) -> int:
    """`ed.tb`(源码里叫 `w`)—— CRC32 变体。

    ⚠️ 与标准 CRC32 **只差最后一步**:标准是 `-1 ^ p`,这里是 `-1 ^ p ^ poly`。
    用于 `x-s-common` 的 `x9`。逐字对照源码实现:
    ```js
    var w = function(poly){ ... return function(e){
        var c = -1;
        for (...) c = p[255 & c ^ e.charCodeAt(i)] ^ c >>> 8;
        return -1 ^ c ^ a;      // ← a = 0xedb88320
    }}();
    ```
    """
    poly = 0xEDB88320
    table = []
    for u in range(255, -1, -1):
        r = u
        for _ in range(8):
            r = (r >> 1) ^ poly if (r & 1) else (r >> 1)
        table.append(r & 0xFFFFFFFF)
    #: 源码建表是 `for(u--; p[u]=r>>>0; )`(从 255 递减到 0),这里按同样顺序
    c = -1
    for ch in s:
        c = table[255 & (c ^ ord(ch))] ^ (c >> 8)
    return (-1 ^ c ^ poly) & 0xFFFFFFFF


def crc32_std(s: str) -> int:
    """`ed.kn` —— 标准 CRC32(末位异或 `0xffffffff`)。与 `crc32_variant` 区分开。"""
    return __import__("zlib").crc32(s.encode("utf-8")) & 0xFFFFFFFF


def custom_b64_encode(data: bytes) -> str:
    """`ed.xE` —— 自定义字母表 base64(**带 `=` 补垫**,与源码一致)。

    源码:全 3 字节组用 `tripletToBase64` 拼;余 1 字节补 `==`、余 2 字节补 `=`。
    标准库 `b64encode` + 字母表翻译与之**逐字节等价**(`=` 不在翻译表内,原样保留)。
    """
    return base64.b64encode(data).decode("ascii").translate(_TO_CUSTOM)


def custom_b64_decode(s: str) -> bytes:
    """自定义字母表 base64 的解码(验证工具用)。"""
    return base64.b64decode(s.translate(_TO_STD))


def encode_utf8(s: str) -> bytes:
    """`ed.lz` = `encodeUtf8`。

    源码是"先 `encodeURIComponent` 再把 `%XX` 还原成字节",其净效果就是
    **字符串的 UTF-8 字节** —— 这里直接 `s.encode('utf-8')`,等价且可验证。
    """
    return s.encode("utf-8")


# ------------------------------------------------------------------ ssk 部分

def hex_decode(h: str) -> bytes:
    """源码 `hexDecode`:hex 串 → 字节;非 hex 直接抛错(与源码同样严格)。"""
    if len(h) % 2 or not all(c in "0123456789abcdefABCDEF" for c in h):
        raise ValueError("httpDataMd5 must be a hex string")
    return bytes.fromhex(h)


def create_enc_ssk_nonce(now_ms: int | None = None) -> bytes:
    """源码 `createEncSskNonce`:**4 字节小端** `uint32(Date.now() + random4)`。

    ```js
    var e = randomBytes(4),
        a = DataView(e).getUint32(0, little),
        r = (Date.now()>>>0) + a >>> 0;
    new DataView(c).setUint32(0, r, little);   // 4 字节
    ```
    ⚠️ `Date.now()` 是**毫秒**,`>>>0` 截成 uint32(会回绕,正常)。
    """
    import os
    import time

    if now_ms is None:
        now_ms = int(time.time() * 1000)
    rnd = int.from_bytes(os.urandom(NONCE_LEN), "little")
    return struct.pack("<I", ((now_ms & 0xFFFFFFFF) + rnd) & 0xFFFFFFFF)


def build_enc_ssk_sign(md5_hex_str: str, ssk: bytes, *,
                       app_id: str = APP_ID, nonce: bytes | None = None) -> dict[str, str]:
    """源码 `buildEncSskSign`:算出 `x6`/`x7`(**按 appId 的字典**)。

    ```js
    var a = hexDecode(e);                       // ★ 16 字节,不是 32 字符!
    var v = createEncSskNonce(),
        w = ssk.slice(32),
        S = concat(v, sha1(concat(ssk, v)), w);
    encSskSign[appId] = stdB64(sha1(concat(a, S)));   // ★ sha1(md5字节 ‖ S)
    encSsk[appId]     = stdB64(S);
    ```

    ⚠️ **旧实现的 bug**:`sha1(u_hex.encode() + S)` 把 **32 字符十六进制串**喂给了 sha1,
    而源码喂的是 **16 个原始字节**。二者结果完全不同 ⇒ 旧签名在这一项上是错的。

    返回 `{"xhs-pc-web": "<b64>"}` 形状的字典(与源码 `r`/`c` 一致);
    `ssk` 长度不足 32 时返回 `{}`(源码是 `return`/跳过,效果等同)。
    """
    if not ssk or len(ssk) < SSK_MIN_LEN:
        return {}
    a = hex_decode(md5_hex_str)                      # ★ 16 字节
    if len(a) != 16:
        raise ValueError("httpDataMd5 must be a 16-byte MD5 hex string")
    if nonce is None:
        nonce = create_enc_ssk_nonce()
    s = nonce + hashlib.sha1(ssk + nonce).digest() + ssk[SSK_HEAD:]
    sign = hashlib.sha1(a + s).digest()              # ★ 修:用 16 字节
    return {app_id: base64.b64encode(sign).decode("ascii")}


def build_enc_ssk(md5_hex_str: str, ssk: bytes, *,
                  app_id: str = APP_ID, nonce: bytes | None = None) -> dict[str, str]:
    """`x7` 那一半:`encSsk = stdB64(nonce ‖ sha1(ssk‖nonce) ‖ ssk[32:])`。"""
    if not ssk or len(ssk) < SSK_MIN_LEN:
        return {}
    if nonce is None:
        nonce = create_enc_ssk_nonce()
    s = nonce + hashlib.sha1(ssk + nonce).digest() + ssk[SSK_HEAD:]
    return {app_id: base64.b64encode(s).decode("ascii")}


# ------------------------------------------------------------------ 主签名

def seccore_signv2(url: str, body: Any, *, ssk: bytes = b"",
                   x3: str = "", sign_version: str = "4.4.3",
                   platform: str = "Windows",
                   x3_provider: Callable[[str, str, str], str] | None = None,
                   nonce: bytes | None = None) -> str:
    """源码 `seccore_signv2(e, a)`:**e = 完整 URL(含 query)**,a = body。

    ```js
    var c = e;  isObj(a) ? c += JSON.stringify(a) : (typeof a === "string" && (c += a));
    var u = md5(c),                       // x5
        p = md5(e),                       // 只喂给 mnsv2,不进签名体
        v = window.mnsv2(c, u, p),        // x3
        S = {x0:版本, x1:"xhs-pc-web", x2:平台, x3:v, x4: a?typeof a:"", x5:u},
        R = buildEncSskSign(u);
    if (R) { S.x6 = R.encSskSign; S.x7 = R.encSsk; }
    return "XYS_" + customB64(encodeUtf8(JSON.stringify(S)));
    ```

    ⚠️ `p = md5(url)` **不参与最终签名体**,只作为 `mnsv2` 的第三个参数 ——
    所以纯 Python 侧不需要它(除非自己复刻 mnsv2)。

    `x3_provider(content, md5_content, md5_url) -> str` 可覆盖 `x3` 的来源
    (生产走 oracle;不传则用 `x3` 字面量)。
    """
    c = url
    if body is None:
        pass
    elif isinstance(body, (dict, list)):
        c += json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    elif isinstance(body, str):
        c += body
    else:
        c += json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    u = md5_hex(c)                                   # x5
    p = md5_hex(url)                                 # 仅喂 mnsv2
    if x3_provider is not None:
        v = x3_provider(c, u, p)
    else:
        v = x3

    s: dict[str, Any] = {
        "x0": sign_version,
        "x1": "xhs-pc-web",
        "x2": platform,
        "x3": v,
        "x4": ("" if body is None else
               "object" if isinstance(body, (dict, list)) else
               "string" if isinstance(body, str) else "object"),
        "x5": u,
    }
    sign_map = build_enc_ssk_sign(u, ssk, nonce=nonce)
    enc_map = build_enc_ssk(u, ssk, nonce=nonce)
    if sign_map:
        s["x6"] = sign_map
        s["x7"] = enc_map
    return "XYS_" + custom_b64_encode(
        encode_utf8(json.dumps(s, separators=(",", ":"), ensure_ascii=False)))


def decode_xys(sig: str) -> dict[str, Any]:
    """把一枚 `XYS_` 签名**解回字段**——验证工具靠它核对。

    ⚠️ `x3` 是 mnsv2 的输出,解出来只是"当时那一份",不代表能复算。
    """
    if not sig.startswith("XYS_"):
        raise ValueError(f"不是 XYS_ 签名:{sig[:12]}")
    raw = custom_b64_decode(sig[4:])
    return json.loads(raw.decode("utf-8"))


# ------------------------------------------------------------------ XYW_(线上格式)

def _aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """AES-128-CBC + PKCS7 填充(与站点 JS 端同一套)。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def _aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def xyw_inner_plaintext(content: str, a1: str, *, env_flags: str = "",
                        now_ms: int | None = None) -> str:
    """`XYW_` 内层那段 `x1=..;x2=..;x3=..;x4=..`(**分号结尾**)。

    `content` = 完整 URL(含 query) + 紧凑 JSON body,与 `seccore_signv2` 同一套拼法。
    """
    import time

    if now_ms is None:
        now_ms = int(time.time() * 1000)
    x2 = env_flags or ENV_FLAGS_DEFAULT
    return (f"x1={md5_hex('url=' + content)};"
            f"x2={x2};"
            f"x3={a1};"
            f"x4={now_ms};")


def sign_xyw(url: str, body: Any, a1: str, *, env_flags: str = "",
             now_ms: int | None = None,
             sign_svn: str = XYW_SIGN_SVN) -> str:
    """★ **纯 Python 造一枚 `XYW_` 签名** —— 不开浏览器、不依赖 xhshow。

    结构(全部实测解出,见 `XYW_AES_KEY` 上方那张表):
    ```
    plaintext = "x1=md5('url='+content);x2=<环境标志>;x3=<a1>;x4=<毫秒>;"
    payload   = hex(AES-128-CBC(key, iv, base64(plaintext)))
    外层      = {"signSvn":"56","signType":"x2","appId":"xhs-pc-web",
                 "signVersion":"1","payload":payload}
    签名      = "XYW_" + base64(紧凑 JSON)
    ```
    """
    c = url
    if body is None:
        pass
    elif isinstance(body, (dict, list)):
        c += json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    elif isinstance(body, str):
        c += body
    else:
        c += json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    plaintext = xyw_inner_plaintext(c, a1, env_flags=env_flags, now_ms=now_ms)
    #: ★ **内层要先 base64 一遍再加密** —— 实测解出来的结构是
    #: `AES(base64("x1=..;x2=..;x3=..;x4=.."))`。少了这一层,密文解回来不是合法 base64。
    inner = base64.b64encode(plaintext.encode("utf-8"))
    ct = _aes_cbc_encrypt(XYW_AES_KEY, XYW_AES_IV, inner)
    outer = {"signSvn": sign_svn, "signType": XYW_SIGN_TYPE,
             "appId": APP_ID, "signVersion": XYW_SIGN_VERSION,
             "payload": ct.hex()}
    return "XYW_" + base64.b64encode(
        json.dumps(outer, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def decode_xyw(sig: str) -> dict[str, Any]:
    """把一枚 `XYW_` 签名**解回字段**(验证工具靠它核对)。"""
    if not sig.startswith("XYW_"):
        raise ValueError(f"不是 XYW_ 签名:{sig[:12]}")
    outer = json.loads(base64.b64decode(sig[4:]).decode("utf-8"))
    ct = bytes.fromhex(outer.get("payload") or "")
    pt = _aes_cbc_decrypt(XYW_AES_KEY, XYW_AES_IV, ct)
    text = base64.b64decode(pt.decode("utf-8")).decode("utf-8", "replace")
    fields = dict(kv.split("=", 1) for kv in text.strip().rstrip(";").split(";") if "=" in kv)
    return {"outer": outer, "fields": fields, "plaintext": text}


# ------------------------------------------------------------------ 现成请求头

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")


#: xhshow 那一路产出的**反爬头**(与 `x-s` 相互独立 ⇒ 可以与我们的 `x-s` 混用)
_ANTISPAM_KEYS = ("x-rap-param", "xy-direction", "x-mns")


def _antispam_headers(signer, url: str, cookies: dict, payload: dict | None,
                      x_rap: bool) -> dict[str, str]:
    """取**反爬头**:`x-rap-param` / `xy-direction` / `x-mns`(取不到回 `{}`,绝不抛)。

    ## ★★ 为什么必须补:这是 2026-10-15 那一整轮「账号异常」的真因
    本仓 2026-10-10 的实测矩阵(见 `config/settings.py` 的 `xhs_sign_x_rap`)写得清清楚楚:

        | 格式   | x_rap | 结果 |
        | `xyw`  | 否    | **HTTP 461 + code 300011「当前账号存在异常」** |
        | `xyw`  | 是    | **HTTP 200** —— 签名层过了,只剩凭据问题 |

    而 2026-10-12 切到**纯 Python 签名器**(本模块)之后,`x_rap` **根本不在它的参数表里**
    ⇒ **`x-rap-param` 从那天起就没再发出去过** ⇒ 每一次协议请求都回到
    `461 + 300011`。⚠️ 那句话读起来像"账号被封了",于是整条排查被带去查账号
    (**实测:两个不同的小红书号、两枚全新会话,回的都是同一个 300011**)。
    ⚠️ 与它一起丢的还有 `xy-direction` 与 `x-mns`:一份 2026-07 的公开逆向
    (`tryna26/xhs-web-reverse`)列出真浏览器的**完整头集是 8 个**,我们只剩 4 个。

    ⚠️ **为什么可以"两个签名器混用"**:`x-rap-param` 是**独立于 `x-s`** 的反爬信封
    (TLV → gzip → XOR → SM4 变体,内容只与 body 有关) ⇒
    我们的 `x-s`(逐字节等同站点、且 `x3` 用**我们自己的 a1**)+ 它的反爬头,不冲突。
    """
    try:
        h = signer.sign_headers_post(url, cookies, payload=payload,
                                     sign_format="xyw", x_rap=bool(x_rap))
    except Exception:  # noqa: BLE001 - 拿不到就**不发**,由调用方自己决定要不要报警
        return {}
    return {k: str(h[k]) for k in _ANTISPAM_KEYS if h.get(k)}


def headers_xyw(url: str, cookies: dict[str, str], *, payload: dict | None = None,
                env_flags: str = "", with_xs_common: bool = True,
                ua: str = "", x_rap: bool = True) -> dict[str, str]:
    """★ **造一份可直接发的请求头** —— `x-s` 用**纯 Python 的 XYW_**(实测逐字节一致)。

    为什么它比 oracle 更适合已登录请求:
    oracle 让页面算签名,但页面签出来的 `x3` 是**页面自己的 a1**(注入会被覆盖,实测两次),
    与我们凭据里的 a1 **不是同一枚** ⇒ 设备标识对不上,很可能就是 `-100 登录已过期` 的成因。
    纯 Python 这一路 `x3` 用**我们自己的 a1**,没有这个错配。

    `x-s-common`(指纹头)仍可由 `xhshow` 合成一份 —— 实测接口在**没有**它时也回 200,
    所以它是"锦上添花",不是必需品;合成失败就直接不发,不阻塞。

    ⚠️ 返回的头里**已带 `Cookie`**(调用方不用自己加)。

    ★ `x_rap`(2026-10-15 补):**是否发 `x-rap-param` 这个反爬头**。默认 `True`。
    ⚠️ 它过去**根本不在本函数的参数表里** ⇒ 配置里 `xhs_sign_x_rap=True` 是**空转的**,
    而实测矩阵里它正是 `461+300011` 与 `200` 的分水岭。详见 `_antispam_headers`。

    ★ `ua`(2026-10-12):**这份凭据登录时的真实 UA**,留空才用 `_UA` 兜底。
    为什么:会话是**某个浏览器**签发的 —— 用 Edge 154 登的号却自称 Chrome 138,
    服务端直接判"登录设备 ≠ 调用设备"(实测 `300011 当前账号存在异常`)。
    ⇒ 指纹必须跟着凭据走。这里**不影响签名**:`x-s` 只吃 `url + body + a1 + 时间戳`。
    """
    a1 = str(cookies.get("a1") or "")
    _use_ua = str(ua or "").strip() or _UA
    out: dict[str, str] = {
        "x-s": sign_xyw(url, payload, a1, env_flags=env_flags),
        "x-t": str(int(__import__("time").time() * 1000)),
        "content-type": "application/json;charset=UTF-8",
        #: ★ 下面三项是**从真浏览器抓来的基准**(2026-10-12,Playwright 监听
        #: `edith.xiaohongshu.com` 的实际出站请求)。原来我们**一个都没发** ——
        #: 少一个 `accept` 就等于告诉服务端"你不是页面里发出的 fetch"。
        "accept": "application/json, text/plain, */*",
        #: Fetch Metadata:页面里的 `fetch()` 一定带(`www` → `edith` 是 **same-site**)
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": _use_ua,
        "referer": "https://www.xiaohongshu.com/",
        "origin": "https://www.xiaohongshu.com",
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if v),
    }
    if with_xs_common:
        try:
            from app.services.xhs_protocol_source import _signer

            s = _signer()
            out["x-s-common"] = s.sign_xs_common(cookies)
            out["x-b3-traceid"] = s.get_b3_trace_id()
            out["x-xray-traceid"] = s.get_xray_trace_id()
            #: ★★ **反爬头必须一起发** —— 少了 `x-rap-param` 就是 `461 + 300011`
            #: (本仓 2026-10-10 的矩阵;2026-10-12 换成纯 Python 签名器时**丢过它**)。
            #: 详见 `_antispam_headers`。
            out.update(_antispam_headers(s, url, cookies, payload, x_rap))
        except Exception:  # noqa: BLE001 - 指纹头是可选增强,失败不该阻塞
            pass
    return out


#: ★ **`XYW_` 的 19 位环境标志**(`x2`)。
#: 2026-10-12 实测:与 `xhs_protocol_source.SIGN_ENV_FLAGS` **逐位相同** ——
#: 说明那枚"从真签名里解出来的兜底值"是对的,且**跨调用稳定**。
#: 仍建议优先用页面实时值(`xhs_sign_oracle` 会截获),这里只是纯 Python 侧的兜底。
ENV_FLAGS_DEFAULT = "0|0|0|1|0|0|1|0|0|0|1|1|0|0|0|1|0|0|0"

# ------------------------------------------------------------------ 登录交换

def make_client_keypair() -> tuple[bytes, bytes, str]:
    """源码 `createWebSskExchange`:生成 X25519 密钥对,返回 (私钥, 公钥, 公钥b64)。

    ⚠️ 需要 `cryptography` 库;没有就抛 `ImportError`(调用方可回落浏览器方案)。
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    priv = X25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes_raw()
    return priv.private_bytes_raw(), pub_raw, base64.b64encode(pub_raw).decode()


def decrypt_ssk(our_private: bytes, encrypted_b64: str) -> bytes:
    """源码 `acceptEncryptedSsk`:**X25519 + AES-256-GCM** 解密服务端下发的 ssk。

    ```js
    var u = stdB64Decode(ef);                 // 服务端公钥(硬编码常量)
    var p = scalarMult(ourPriv, u);           // 共享密钥
    S = IQ.decrypt(c.slice(12), p, c.slice(0,12));   // nonce = 前 12 字节
    ```
    `encrypted_b64` 即 `login_info` 里那一串。解出来就是**与 cookie 同源的 ssk**。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = base64.b64decode(encrypted_b64)
    if len(blob) < AES_GCM_NONCE_LEN + 16:
        raise ValueError("encrypted SSK is too short")
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    priv = X25519PrivateKey.from_private_bytes(our_private)
    server_pub = base64.b64decode(SERVER_PUBLIC_KEY_B64)
    shared = priv.exchange(server_pub) if hasattr(priv, "exchange") else None
    if shared is None:                          # 不同版本 API 名不同,兜底
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
        shared = priv.exchange(X25519PublicKey.from_public_bytes(server_pub))
    nonce, ct = blob[:AES_GCM_NONCE_LEN], blob[AES_GCM_NONCE_LEN:]
    return AESGCM(shared).decrypt(nonce, ct, None)
