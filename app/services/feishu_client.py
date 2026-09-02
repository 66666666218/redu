"""飞书自定义机器人客户端 + 签名(见 doc/dev.md §5.11)。

与作业逻辑分开:本模块只负责"如何发一条带签名的消息到某个 webhook"。
日报/实时/周报等编排在 `app/services/feishu.py`。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

import requests

from app.utils import get_logger

logger = get_logger(__name__)

_TIMEOUT = 15


def _sign(secret: str, ts: int) -> str:
    """飞书签名。

    官方算法:`string_to_sign = '{timestamp}\\n{secret}'`,以它为 **HMAC key**,
    消息体为**空字符串**,`sign = base64(HmacSHA256(string_to_sign, ""))`。
    注意不是拿 secret 当 key——两者写反会 `sign match fail`。
    """
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, msg=b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuClient:
    """飞书自定义机器人客户端(单条文本消息)。"""

    def __init__(self, webhook: str, secret: str) -> None:
        self.webhook = webhook
        self.secret = secret or ""

    def send(self, text: str) -> bool:
        """推送一条文本。成功返回 True;网络/签名/风控错误记日志并返回 False。"""
        if len(text) > 18000:
            text = text[:17000] + "\n…(内容过长已截断)"
        ts = int(time.time())
        body: dict = {"msg_type": "text", "content": {"text": text}}
        if self.secret:
            body["timestamp"] = str(ts)
            body["sign"] = _sign(self.secret, ts)
        try:
            resp = requests.post(self.webhook, json=body, timeout=_TIMEOUT)
            obj = resp.json()
            code, msg = obj.get("code"), obj.get("msg")
            if code == 0:
                return True
            logger.warning("飞书推送失败 code=%s msg=%s", code, msg)
            return False
        except (requests.RequestException, ValueError) as exc:  # 网络错误/非 JSON
            logger.error("飞书推送异常:%s", exc)
            return False
