"""用户鉴权:注册(邮箱)、登录(邮箱/用户名)、找回密码、当前用户依赖(JWT Bearer)。

密码 bcrypt 加盐哈希;登录签发 JWT;受保护接口用 `get_current_user`。
找回密码:生成一次性重置令牌(存哈希+过期),经 SMTP 邮件发送重置链接。
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta

import bcrypt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import User
from app.security import create_access_token, decode_token

RESET_TTL_MINUTES = 30
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 72   # bcrypt 只取前 72 字节,更长的部分会被静默截断
MAX_USERNAME_LEN = 64   # 与 User.username 列宽一致,超长会被数据库截断/报错
# 邮箱格式:不引入 email-validator 依赖(其未列在 requirements.txt,容器内不保证存在),
# 用正则做够用的校验——挡掉空值与明显非邮箱,同时能返回中文提示。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
# 标准 bcrypt 哈希:$2a/2b/2x/2y$ + 两位 cost + $ + 53 位 salt&hash,共 60 字符
_BCRYPT_HASH_RE = re.compile(r"^\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}$")


def validate_email(email: str) -> str:
    """校验并规范化邮箱(去空格、转小写);非法时抛 400。"""
    value = (email or "").strip().lower()
    if not value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请填写邮箱")
    if len(value) > 128:  # 与 User.email 列宽一致
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "邮箱过长(最多 128 个字符)")
    if not _EMAIL_RE.match(value):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "邮箱格式不正确,请填写如 name@example.com")
    return value


def validate_password(password: str) -> str:
    """校验密码强度;过短/过长抛 400。"""
    value = password or ""
    if len(value) < MIN_PASSWORD_LEN:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(value.encode("utf-8")) > MAX_PASSWORD_LEN:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码过长(最多 72 字节)")
    return value


def hash_password(plain: str) -> str:
    """bcrypt 加盐哈希。

    直接用官方 `bcrypt` 而非 passlib:passlib 1.7.4 停更于 2020 年,启动时会读
    `bcrypt.__about__`(bcrypt 4.1+ 已移除),在 bcrypt 5.x 下探测失败后走错分支,
    连 8 字节的密码都会报 "password cannot be longer than 72 bytes",
    使注册/登录/改密码全部 500。两者产出的都是标准 `$2b$` 哈希,老密码无缝兼容。
    """
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """校验密码;哈希损坏/为空一律判为不匹配,绝不抛异常。

    必须先做格式预检:bcrypt 的 Rust 后端遇到畸形哈希(如 `$2b$12$short`)
    会抛 `PanicException`——它继承自 `BaseException`,`except Exception`
    根本拦不住,一条脏数据就能把登录接口打成 500。
    """
    value = (hashed or "").strip()
    if not _BCRYPT_HASH_RE.match(value):
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), value.encode("ascii"))
    except (ValueError, TypeError):  # 超长密码等
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register_user(db: Session, email: str, password: str, username: str | None = None) -> User:
    """按邮箱注册;用户名缺省取邮箱前缀。email 唯一。匹配 admin_email 自动设为 admin。

    邮箱与密码在此统一校验(而非只靠前端),非法输入返回 400 中文提示。
    """
    from config.settings import get_settings

    email = validate_email(email)
    validate_password(password)
    name = (username or email.split("@")[0]).strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名不能为空")
    if len(name) > MAX_USERNAME_LEN:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"用户名过长(最多 {MAX_USERNAME_LEN} 个字符)")
    if db.scalar(select(User).where(or_(User.email == email, User.username == name))):
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱或用户名已注册")
    admins = {a.strip().lower() for a in get_settings().admin_email.split(",") if a.strip()}
    user = User(username=name, email=email, password_hash=hash_password(password))
    if email in admins:
        user.role = "admin"
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
    """用重置令牌改密码;无效/过期返回 False。

    新密码同样走 `validate_password`,否则重置流程可绕过注册时的强度要求。
    """
    validate_password(new_password)
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
    if not user.enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """仅 admin/operator 可访问。"""
    if user.role not in ("admin", "operator"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无管理员权限")
    return user


def log_login(db: Session, user_id: int | None, username: str, ip: str, ua: str, ok: bool) -> None:
    from app.db.models import LoginLog

    db.add(LoginLog(user_id=user_id, username=username, ip=ip[:64], ua=ua[:255], ok=ok))
    db.commit()
