"""夸克签名服务的**客户端** —— 对本地常驻的 unidbg 签名机说话。

## 为什么要"常驻"

签名本身是**毫秒级**,但 unidbg 里聚安全的**初始化是分钟级**(自检 / 埋点重试)。
所以服务端在**进程启动时初始化一次**,之后一直复用 —— 这也是它能落地的前提。
服务端实现只在本机,不进仓库。

## 契约(很重要)

**拿不到签名一律抛 `QuarkSignError`。**
本仓最忌的就是把失败伪装成"没有数据"(见记忆 `silent-failure-is-fake-success`):
签名服务返回 `ERR`、连不上、超时 —— 全部是**错误**,调用方必须让它们把整条链路打断,
而不是当成"这条口令没有分享码"。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `QUARK_SIGN_HOST` | `127.0.0.1` | 服务端只监听回环,基本不用改 |
| `QUARK_SIGN_PORT` | `29341` | 服务端口 |
| `QUARK_SIGN_TOKEN` | 无 | **必填**;服务端也要求同一个值,否则拒答 |

协议(明文 TCP,一行一问一答,UTF-8,`\t` 分隔):

    请求: {token}\t{密钥号}\t{base64(内容)}\n
    响应: OK {40位小写hex}\n   或   ERR {原因}\n

⚠️ **内容必须 base64** —— 真实输入(抖音标题)里**带换行**,而行式协议用 `readLine()` 收,
不编码就会被截成两截。这个坑是接线后第一次真跑才暴露的。
"""
from __future__ import annotations

import base64
import os
import socket
from typing import Final

DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 29341
ENV_HOST: Final = "QUARK_SIGN_HOST"
ENV_PORT: Final = "QUARK_SIGN_PORT"
ENV_TOKEN: Final = "QUARK_SIGN_TOKEN"

#: 密钥号 —— 夸克口令解析用的是 12001(`2ee1`)。
KEY_NUMBER_DEFAULT: Final = "12001"

_CONNECT_TIMEOUT: Final = 30.0


class QuarkSignError(RuntimeError):
    """签名服务不可用或返回失败。**必须向上抛**,不能吞成"没有签名"。"""


def _endpoint(host: str | None = None, port: int | None = None,
              token: str | None = None) -> tuple[str, int, str]:
    """解析端点。**显式参数 > 环境变量**;

    ⚠️ 为什么要显式参数:pydantic 读 `.env` **不会**写进 `os.environ`,
    所以配置一旦只写在 `.env` 里,这里 `os.environ` 是看不到的 —— 由调用方
    (`quark_kouling_proto`)从 settings 取好再传进来。
    """
    tok = (token if token is not None else os.environ.get(ENV_TOKEN, "")).strip()
    if not tok:
        raise QuarkSignError(
            "没有令牌 —— 请在 .env 配 QUARK_SIGN_TOKEN(签名服务也要求同一个值)")
    h = (host if host is not None else os.environ.get(ENV_HOST, "")).strip() or DEFAULT_HOST
    raw = port if port is not None else os.environ.get(ENV_PORT, str(DEFAULT_PORT))
    try:
        p = int(raw)
    except (TypeError, ValueError) as exc:
        raise QuarkSignError(f"端口不是数字:{raw!r}") from exc
    return h, p, tok


def _check_content(content: str) -> None:
    if not isinstance(content, str):
        raise QuarkSignError(f"待签内容必须是 str,收到 {type(content).__name__}")


def sign(content: str, key_number: str = KEY_NUMBER_DEFAULT, *,
         timeout: float = _CONNECT_TIMEOUT, host: str | None = None,
         port: int | None = None, token: str | None = None) -> str:
    """求 `(密钥号, 内容)` 的 **20 字节裸签名**(40 位小写 hex)。

    Args:
        content: 待签内容(服务端原样送进聚安全,不做任何预处理)。
        key_number: 密钥号,夸克口令用 `"12001"`。
        timeout: 连接与读取超时(秒)。
        host/port/token: 覆盖端点(**显式参数优先于环境变量**;见 `_endpoint`)。

    Returns:
        40 位小写 hex(20 字节)。

    Raises:
        QuarkSignError: 连不上 / 超时 / 服务端返回 `ERR` —— **一律当错误**。
    """
    _check_content(content)
    host, port, token = _endpoint(host, port, token)
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload = f"{token}\t{key_number}\t{b64}\n".encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
    except OSError as exc:
        raise QuarkSignError(
            f"连不上签名服务 {host}:{port} —— {type(exc).__name__}: {exc}"
            "(服务没起?未初始化完?端口被占?)") from exc

    line = bytes(buf).decode("utf-8", "replace").strip()
    if line.startswith("OK "):
        sig = line[3:].strip()
        if len(sig) != 40 or any(c not in "0123456789abcdef" for c in sig.lower()):
            raise QuarkSignError(f"签名服务返回的形状不对:{sig!r}")
        return sig.lower()
    raise QuarkSignError(f"签名服务返回失败:{line or '(空响应)'}")


def ping(timeout: float = 5.0) -> bool:
    """探活:能不能连上(不校验令牌、不消耗签名)。服务端尚未就绪时返回 False。"""
    host = os.environ.get(ENV_HOST, DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port = int(os.environ.get(ENV_PORT, str(DEFAULT_PORT)).strip())
    except ValueError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
