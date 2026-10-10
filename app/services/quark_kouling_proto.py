"""夸克口令 → 分享码(**纯协议**:本地签名机 + HTTP)。

## 为什么不走模拟器了

原来的路径是「把口令丢进剪贴板,让夸克 App 自己解析,我们再从 UI 上读结果」
(见 `quark_kouling.py`)。现在**签名能在本机算出来了**(常驻服务,
见 `quark_sign_client`),于是整条链可以在 Python 里跑完:
**不需要模拟器、不需要 Frida、不需要真机。**

## 请求形状(设备侧抓包 + dex 静态读出,双重验证)

    签名内容 = "QUARK" + clipboard + identifier + shareSecret + timestamp
    body    = {app:"QUARK", clipboard, identifier, shareSecret, timestamp, kps:"", sign}
    sign    = hex(2字节大端密钥号) ‖ <20 字节聚安全签名>     # 密钥号 12001 → "2ee1"

⚠️ **timestamp 被拼进签名内容** ⇒ 换了 timestamp 必须重新签名(所以是先取时间再签)。

## 口令的两种形态(推导规则)

| 形态 | 长相 | clipboard | identifier | shareSecret |
|---|---|---|---|---|
| ASCII | `…/~dda43bGyER~:/…` | `/~dda43bGyER~:/` | `"~"` | `dda43bGyER` |
| 中文  | 一串无意义中文 | 整串 | 整串去末字 | `""` |

**为什么 clipboard 要补 `:/`**:野外的口令本身就写成 `/~TOKEN~:/`(库里 11 条真实抖音标题
全部如此),`:/` 是口令的一部分,不是我们加的。

## 契约

**任何失败都必须如实表达** —— 服务端拒绝、签名服务连不上、超时,全部返回 `ok=False` 并把
原因写进 `reason`。**绝不把"没解析出来"和"解析出来是空的"混为一谈**
(见记忆 `silent-failure-is-fake-success`)。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Final

import requests

from app.services import quark_sign_client as signer
from app.utils import get_logger

logger = get_logger(__name__)

PARSE_URL: Final = "https://utoken2.quark.cn/utoken/v2/parse"
APP: Final = "QUARK"
#: 夸克口令解析用的密钥号(`2ee1`)。
KEY_NUMBER: Final = "12001"

#: `/~TOKEN~` —— 夸克 App 拼分享文案时写的令牌,**不是人手打的**,所以最可靠。
_RE_TOKEN: Final = re.compile(r"/~([A-Za-z0-9]{6,16})~")

#: 真实抓包的 UA(设备上跑的夸克,版本按实测的写)。
_UA: Final = ("Mozilla/5.0 (Linux; Android 14; M2011K2C) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Mobile Safari/537.36 Quark/7.5.0")
_HEADERS: Final = {"User-Agent": _UA, "Content-Type": "application/json"}

_DEFAULT_TIMEOUT: Final = 20.0
_RE_PWD_ID: Final = re.compile(r'"pwd_id":"([A-Za-z0-9]+)"')


def derive_fields(text: str) -> tuple[str, str, str]:
    """口令文本 → `(clipboard, identifier, shareSecret)`。

    ASCII 型优先(令牌是 App 写的,最可靠);否则按中文型的退化解析。
    取不到任何形态时返回**空 clipboard**,由调用方判失败 —— 这里不抛。
    """
    s = str(text or "")
    m = _RE_TOKEN.search(s)
    token = m.group(1) if m else ""
    if token:
        return f"/~{token}~:/", "~", token
    stripped = s.strip()
    if not stripped:
        return "", "", ""
    return stripped, stripped[:-1], ""


def content_of(clipboard: str, identifier: str, secret: str, timestamp: str) -> str:
    """待签内容 —— 与 App 逐字一致的拼法。"""
    return APP + clipboard + identifier + secret + timestamp


def _signer_kwargs(settings=None) -> dict:
    """从配置取签名机端点。

    ⚠️ **不能只靠环境变量** —— pydantic 读 `.env` **不会**写进 `os.environ`,
    所以配置写在 `.env` 里时 `os.environ` 是看不到的。这里统一从 settings 取。
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    return {"host": getattr(settings, "quark_sign_host", "") or None,
            "port": int(getattr(settings, "quark_sign_port", 0) or 0) or None,
            "token": getattr(settings, "quark_sign_token", "") or None}


def parse_via_protocol(text: str, *, timeout: float = _DEFAULT_TIMEOUT,
                       settings=None) -> dict:
    """口令文本 → 分享码。**纯协议**,返回结构化 dict(不抛)。

    Returns:
        `{"ok": bool, "pwd_id": str, "share_code": str, "reason": str,
          "clipboard"/"identifier"/"shareSecret"/"timestamp"/"sign": str}`
        `ok=False` 时 `reason` 一定说清楚是哪一步失败的。
    """
    clipboard, identifier, secret = derive_fields(text)
    base = {"clipboard": clipboard, "identifier": identifier, "shareSecret": secret}
    if not clipboard:
        return {**base, "ok": False, "pwd_id": "", "share_code": "",
                "reason": "取不到口令(既不是 /~TOKEN~ 形态,也不是非空文本)"}

    timestamp = str(int(time.time() * 1000))
    content = content_of(clipboard, identifier, secret, timestamp)
    try:
        bare = signer.sign(content, **_signer_kwargs(settings))
    except signer.QuarkSignError as exc:
        # ★ `env: True` = **环境故障**(签名服务没起/连不上),**与"这条口令没内容"是两回事**
        #   (2026-10-11):上层据此**撤回"试过"的章** —— 否则一次环境抖动会把整批线索
        #   **永久判死**(它们再也不会进待办队列)。与 UI 那条 `env` 同一纪律。
        return {**base, "ok": False, "pwd_id": "", "share_code": "", "timestamp": timestamp,
                "env": True, "reason": f"签名服务不可用:{exc}"}

    sign = "2ee1" + bare          # 12001 → 0x2ee1
    body = {"app": APP, "clipboard": clipboard, "identifier": identifier,
            "shareSecret": secret, "timestamp": timestamp, "kps": "", "sign": sign}
    try:
        resp = requests.post(PARSE_URL, headers=_HEADERS,
                             data=json.dumps(body, ensure_ascii=False).encode(),
                             timeout=timeout)
    except requests.RequestException as exc:
        return {**base, "ok": False, "pwd_id": "", "share_code": "", "timestamp": timestamp,
                "sign": sign, "env": True,      # 网络失败同样是**环境**(见上面那条注释)
                "reason": f"HTTP 失败:{type(exc).__name__}: {exc}"}

    try:
        data = resp.json()
    except ValueError:
        return {**base, "ok": False, "pwd_id": "", "share_code": "", "timestamp": timestamp,
                "sign": sign, "reason": f"响应不是 JSON:HTTP {resp.status_code} {resp.text[:120]}"}

    payload = data.get("data") or {}
    android_url = urllib.parse.unquote(payload.get("androidUrl") or "")
    m = _RE_PWD_ID.search(android_url)
    pwd_id = m.group(1) if m else ""
    share_code = str(payload.get("shareCode") or payload.get("share_code") or "")
    ok = bool(resp.status_code == 200 and data.get("success") and pwd_id)

    out = {**base, "ok": ok, "pwd_id": pwd_id, "share_code": share_code,
           "timestamp": timestamp, "sign": sign,
           "reason": "" if ok else f"服务端没给出分享码:HTTP {resp.status_code} "
                                   f"success={data.get('success')} code={data.get('code')}"}
    logger.info("夸克口令(纯协议): clipboard=%r identifier=%r secret=%r → ok=%s pwd_id=%s",
                clipboard[:24], identifier[:8], secret[:16], ok, pwd_id or "-")
    return out
