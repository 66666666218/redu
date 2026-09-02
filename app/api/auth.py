"""认证路由:/api/auth/*。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import (
    authenticate,
    create_password_reset_token,
    get_current_user,
    log_login,
    register_user,
    reset_password,
)
from app.db import get_db
from app.db.models import User
from app.security import create_access_token
from app.services.notifier import get_notifier
from app.api.deps import (
    ForgotIn,
    LoginIn,
    MeOut,
    RegisterIn,
    ResetIn,
    TokenOut,
    _clear_login,
    _login_allowed,
    _record_login,
)

router = APIRouter()


@router.post("/api/auth/register", response_model=TokenOut)
def register(body: RegisterIn, db: Session = Depends(get_db)) -> TokenOut:
    user = register_user(db, body.email, body.password, body.username)
    token = create_access_token(user.id)
    return TokenOut(token=token, username=user.username)


@router.post("/api/auth/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    key = f"{request.client.host if request.client else '?'}:{body.login.strip().lower()}"
    if not _login_allowed(key):
        log_login(db, None, body.login.strip(), request.client.host if request.client else "?", request.headers.get("user-agent", ""), False)
        raise HTTPException(429, "登录尝试过多,请 10 分钟后再试")
    token = authenticate(db, body.login, body.password)
    ip = request.client.host if request.client else "?"
    if token is None:
        _record_login(key)
        log_login(db, None, body.login.strip(), ip, request.headers.get("user-agent", ""), False)
        raise HTTPException(401, "账号或密码错误")
    _clear_login(key)
    uid = db.scalar(select(User.id).where(or_(User.username == body.login.strip(), User.email == body.login.strip().lower())))
    log_login(db, uid, body.login.strip(), ip, request.headers.get("user-agent", ""), True)
    return TokenOut(token=token, username=body.login.strip())


@router.post("/api/auth/forgot")
def forgot(body: ForgotIn, db: Session = Depends(get_db)):
    from config.settings import get_settings

    settings = get_settings()
    token = create_password_reset_token(db, body.email)
    if token:
        try:
            notifier = get_notifier(settings)
            link = f"{settings.public_base_url}/reset?token={token}"
            notifier.send("重置密码", f"请点击以下链接重置密码(30 分钟内有效):\n\n{link}")
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning("发送重置邮件失败:%s", exc)
    return {"message": "如果该邮箱已注册,重置邮件已发送"}


@router.post("/api/auth/reset")
def reset(body: ResetIn, db: Session = Depends(get_db)):
    ok = reset_password(db, body.token, body.new_password)
    if not ok:
        raise HTTPException(400, "重置链接无效或已过期")
    return {"message": "密码已重置,请重新登录"}


@router.get("/api/auth/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)) -> MeOut:
    return MeOut(id=user.id, username=user.username, role=user.role)
