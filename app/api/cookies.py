"""Cookie 与用户 SMTP 路由:/api/cookies/*、/api/user/smtp。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services.cookie_store import delete_cookie as del_cookie
from app.services.cookie_store import list_cookies, set_cookie
from app.api.deps import CookieIn, CookieOut, UserSmtpIn

router = APIRouter()


@router.get("/api/cookies", response_model=list[CookieOut])
def cookies_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[CookieOut]:
    return [CookieOut(**c) for c in list_cookies(db, user.id)]


@router.put("/api/cookies/{platform}", response_model=CookieOut)
def cookies_put(platform: str, body: CookieIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> CookieOut:
    try:
        set_cookie(db, user.id, platform, body.cookie.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return CookieOut(platform=platform, configured=bool(body.cookie.strip()))


@router.delete("/api/cookies/{platform}")
def cookies_del(platform: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    del_cookie(db, user.id, platform)
    return {"platform": platform, "deleted": True}


@router.get("/api/user/smtp")
def user_smtp_get(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"host": user.smtp_host or "", "port": user.smtp_port or 465,
            "user": user.smtp_user or "", "from_name": user.smtp_from or ""}


@router.put("/api/user/smtp")
def user_smtp_put(body: UserSmtpIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 用户可填 host 若原样入库,notifier.get_user_notifier 会 smtplib.SMTP_SSL(host,port)
    # 直连,容器即被当作内网端口探测跳板(`host=mysql`/`169.254.169.254` 等);
    # 与 assert_public_url 同一咽喉,只是入口是 (host,port)。清空 host(=None) 允许。
    if body.host:
        from app.utils.net import UnsafeUrlError, assert_public_host

        try:
            assert_public_host(body.host, body.port or 465)
        except UnsafeUrlError as exc:
            raise HTTPException(400, f"SMTP 主机不合法:{exc}") from exc
    user.smtp_host = body.host or None
    user.smtp_port = body.port or None
    user.smtp_user = body.user or None
    # SMTP 密码加密存储(gAAAAAB 前缀标识密文;历史明文在下次保存时自然替换)
    if body.password:
        from app.security import encrypt_cookie

        user.smtp_pass = "enc:" + encrypt_cookie(body.password)
    else:
        user.smtp_pass = None
    user.smtp_from = body.from_name or None
    db.commit()
    return {"saved": True}
