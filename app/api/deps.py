"""平台 API 共享依赖:请求体模型 + 依赖 + 登录限速(供各 router 复用)。"""
from __future__ import annotations

import time

import pydantic
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require_admin
from app import admin as admin_svc
from app.db import get_db  # noqa: F401  (各 router 复用)

# 轻量登录限速(内存滑窗,防爆破)
_LOGIN_WINDOW = 600
_LOGIN_MAX = 10
_login_attempts: dict[str, list[float]] = {}


def _login_allowed(key: str) -> bool:
    now = time.time()
    arr = [t for t in _login_attempts.get(key, []) if now - t < _LOGIN_WINDOW]
    # 剪空后即删键:否则攻击者用不重复用户名探测会把 ip:username 组合永久留在
    # dict 里,每次访问只是重写空 list,键基数随探测数线性增长且永不回落(重启前)。
    if arr:
        _login_attempts[key] = arr
    else:
        _login_attempts.pop(key, None)
    return len(arr) < _LOGIN_MAX


def _record_login(key: str) -> None:
    _login_attempts.setdefault(key, []).append(time.time())


def _clear_login(key: str) -> None:
    _login_attempts.pop(key, None)


def require_perm(perm: str):
    """按钮级权限依赖:require_admin + 角色权限点校验。"""

    def dep(user=Depends(require_admin)):
        if not admin_svc.has_perm(user.role, perm):
            raise HTTPException(403, f"无权限:{perm}")
        return user

    return dep


# ---- 请求/响应模型 ----
class RegisterIn(pydantic.BaseModel):
    email: str
    password: str
    username: str | None = None


class LoginIn(pydantic.BaseModel):
    login: str
    password: str


class ForgotIn(pydantic.BaseModel):
    email: str


class ResetIn(pydantic.BaseModel):
    token: str
    new_password: str


class AlertRuleIn(pydantic.BaseModel):
    section: str
    rule_type: str
    metric: str | None = pydantic.Field(default=None, max_length=32)
    threshold: float | None = None
    keyword: str | None = pydantic.Field(default=None, max_length=128)
    alert_time: str | None = pydantic.Field(default=None, max_length=8)


class UserSmtpIn(pydantic.BaseModel):
    host: str = pydantic.Field(default="", max_length=128)
    port: int = pydantic.Field(default=465, ge=1, le=65535)
    user: str = pydantic.Field(default="", max_length=128)
    password: str = pydantic.Field(default="", max_length=255)
    from_name: str = pydantic.Field(default="", max_length=128)


class TokenOut(pydantic.BaseModel):
    token: str
    username: str


class MeOut(pydantic.BaseModel):
    id: int
    username: str
    role: str = "user"


class CookieIn(pydantic.BaseModel):
    cookie: str


class CookieOut(pydantic.BaseModel):
    platform: str
    configured: bool
    preview: str = ""
    updated_at: str | None = None


class ScheduleIn(pydantic.BaseModel):
    interval_minutes: int | None = None
    enabled: bool | None = None


class ConfigIn(pydantic.BaseModel):
    value: str
