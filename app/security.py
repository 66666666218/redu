"""安全模块:JWT 签发/校验 + 用户 Cookie 加密(Fernet)。

- 登录令牌:PyJWT,HS256,`sub`=用户 id。
- Cookie 加密:Cryptography Fernet,密钥来自 `cookie_encrypt_key`,为空则用 `jwt_secret` 派生。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets as _secrets
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet

from config.settings import get_settings

logger = logging.getLogger(__name__)
_fallback_secret: str | None = None


def _secret() -> str:
    """返回 JWT 密钥。

    - 配置了 `jwt_secret` 用配置值;
    - 否则生成临时密钥并告警(避免注册/登录因密钥缺失而 500;生产务必备份并设置 JWT_SECRET)。
    """
    global _fallback_secret
    s = get_settings().jwt_secret
    if not s:
        if _fallback_secret is None:
            _fallback_secret = _secrets.token_urlsafe(48)
            logger.warning("未配置 JWT_SECRET,已生成临时密钥;生产请设置 JWT_SECRET(否则重启后登录态失效)")
        return _fallback_secret
    return s


def _fernet_key() -> bytes:
    settings = get_settings()
    source = settings.cookie_encrypt_key or settings.jwt_secret
    return base64.urlsafe_b64encode(hashlib.sha256(source.encode()).digest())


def get_fernet() -> Fernet:
    return Fernet(_fernet_key())


def encrypt_cookie(plain: str) -> str:
    return get_fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_cookie(cipher: str) -> str:
    return get_fernet().decrypt(cipher.encode("utf-8")).decode("utf-8")


def create_access_token(user_id: int) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "iat": now, "exp": now + timedelta(minutes=settings.jwt_expire_minutes)}
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_token(token: str) -> int | None:
    """解析令牌,返回用户 id;无效返回 None。"""
    try:
        payload = jwt.decode(token, _secret(), algorithms=["HS256"])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
