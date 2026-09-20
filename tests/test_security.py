"""Cookie 加密密钥派生的安全边界单测(不联网)。

锁住 _fernet_key 的行为:两个密钥来源均为空时会退化成"固定公开密钥",
必须告警(但不改派生值,以免锁死以该固定密钥加密的历史 Cookie)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import base64
import hashlib
import logging

import pytest

from app import security


class _S:
    def __init__(self, cookie_encrypt_key="", jwt_secret=""):
        self.cookie_encrypt_key = cookie_encrypt_key
        self.jwt_secret = jwt_secret


@pytest.fixture(autouse=True)
def _reset_warn():
    security._fernet_weak_warned = False
    yield
    security._fernet_weak_warned = False


def test_empty_secrets_derive_public_constant_key(monkeypatch, caplog) -> None:
    """两密钥均空:派生值恰为 sha256(b"") 的固定公开密钥(锁定行为——防误改为静默换键)。"""
    monkeypatch.setattr(security, "get_settings", lambda: _S())
    with caplog.at_level(logging.CRITICAL):
        key = security._fernet_key()
    assert key == base64.urlsafe_b64encode(hashlib.sha256(b"").digest())
    assert any("COOKIE_ENCRYPT_KEY" in r.getMessage() and r.levelno >= logging.CRITICAL
               for r in caplog.records)


def test_weak_key_warning_fires_once(monkeypatch, caplog) -> None:
    """告警只发一次(热路径每次加解密都走 _fernet_key,勿刷屏)。"""
    monkeypatch.setattr(security, "get_settings", lambda: _S())
    with caplog.at_level(logging.CRITICAL):
        security._fernet_key()
        security._fernet_key()
        security._fernet_key()
    n = sum(1 for r in caplog.records if "COOKIE_ENCRYPT_KEY" in r.getMessage())
    assert n == 1


def test_configured_key_no_warning(monkeypatch, caplog) -> None:
    """配了 cookie_encrypt_key:派生自该值,不告警。"""
    monkeypatch.setattr(security, "get_settings", lambda: _S(cookie_encrypt_key="real-secret"))
    with caplog.at_level(logging.CRITICAL):
        key = security._fernet_key()
    assert key == base64.urlsafe_b64encode(hashlib.sha256(b"real-secret").digest())
    assert not any("COOKIE_ENCRYPT_KEY" in r.getMessage() for r in caplog.records)


def test_encrypt_decrypt_roundtrip_under_configured_key(monkeypatch) -> None:
    """正常密钥下 Cookie 加解密可往返。"""
    monkeypatch.setattr(security, "get_settings", lambda: _S(cookie_encrypt_key="k1"))
    assert security.decrypt_cookie(security.encrypt_cookie("weibo-cookie-值")) == "weibo-cookie-值"
