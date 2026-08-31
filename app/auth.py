"""用户鉴权:注册(邮箱)、登录(邮箱/用户名)、找回密码、当前用户依赖(JWT Bearer)。

密码 bcrypt 加盐哈希;登录签发 JWT;受保护接口用 `get_current_user`。
找回密码:生成一次性重置令牌(存哈希+过期),经 SMTP 邮件发送重置链接。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, Header, HTTPException, status
from passlib.context import CryptContext
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import User
from app.security import create_access_token, decode_token

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

RESET_TTL_MINUTES = 30


def hash_password(plain: str) -> str:
    return pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd.verify(plain, hashed)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register_user(db: Session, email: str, password: str, username: str | None = None) -> User:
    """按邮箱注册;用户名缺省取邮箱前缀。email 唯一。"""
    email = email.strip().lower()
    name = (username or email.split("@")[0]).strip()
    if db.scalar(select(User).where(or_(User.email == email, User.username == name))):
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱或用户名已注册")
    user = User(username=name, email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, login: str, password: str) -> str | None:
    """以用户名或邮箱登录,成功返回 token,失败返回 None。"""
    login = login.strip()
    user = db.scalar(select(User).where(or_(User.username == login, User.email == login.lower())))
    if not user or not verify_password(password, user.password_hash):
        return None
    return create_access_token(user.id)


def create_password_reset_token(db: Session, email: str) -> str | None:
    """为某邮箱生成一次性重置令牌;邮箱不存在返回 None(不暴露是否存在)。"""
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user:
        return None
    token = secrets.token_urlsafe(32)
    user.reset_token = _token_hash(token)
    user.reset_expires = datetime.now() + timedelta(minutes=RESET_TTL_MINUTES)
    db.commit()
    return token


def reset_password(db: Session, token: str, new_password: str) -> bool:
    """用重置令牌改密码;无效/过期返回 False。"""
    user = db.scalar(select(User).where(User.reset_token == _token_hash(token.strip())))
    if not user or not user.reset_expires or user.reset_expires < datetime.now():
        return False
    user.password_hash = hash_password(new_password)
    user.reset_token = None
    user.reset_expires = None
    db.commit()
    return True


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未提供登录凭证")
    token = authorization.split(" ", 1)[1].strip()
    user_id = decode_token(token)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期,请重新登录")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    return user
