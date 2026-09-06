"""用户 Cookie 存取服务:每用户每平台存一个,加密入库。

采集时按用户取其 Cookie 注入采集器;数据按 user_id 隔离。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import UserCookie
from app.security import decrypt_cookie, encrypt_cookie

PLATFORMS = ("weibo", "baidu", "douyin", "goofish", "weread")


def _mask(cookie: str) -> str:
    """返回前缀用于界面展示,不暴露完整密钥。"""
    return cookie[:24] + "…" if len(cookie) > 24 else cookie


def get_cookie(db: Session, user_id: int, platform: str) -> str | None:
    """解密取某用户某平台 Cookie;未配置返回 None。"""
    row = db.scalar(select(UserCookie).where(UserCookie.user_id == user_id, UserCookie.platform == platform))
    if not row:
        return None
    try:
        return decrypt_cookie(row.cookie)
    except Exception:  # noqa: BLE001 - 解密失败视为未配置
        return None


def get_cookies(db: Session, user_id: int) -> dict[str, str]:
    """取该用户全部(平台->明文 Cookie)。"""
    rows = db.scalars(select(UserCookie).where(UserCookie.user_id == user_id)).all()
    out: dict[str, str] = {}
    for row in rows:
        try:
            out[row.platform] = decrypt_cookie(row.cookie)
        except Exception:  # noqa: BLE001
            continue
    return out


def set_cookie(db: Session, user_id: int, platform: str, cookie: str) -> UserCookie:
    """保存该用户某平台 Cookie(加密;已存在则更新)。"""
    if platform not in PLATFORMS:
        raise ValueError(f"不支持的平台:{platform}")
    row = db.scalar(select(UserCookie).where(UserCookie.user_id == user_id, UserCookie.platform == platform))
    if row is None:
        row = UserCookie(user_id=user_id, platform=platform, cookie=encrypt_cookie(cookie))
        db.add(row)
    else:
        row.cookie = encrypt_cookie(cookie)
    db.commit()
    db.refresh(row)
    return row


def delete_cookie(db: Session, user_id: int, platform: str) -> None:
    row = db.scalar(select(UserCookie).where(UserCookie.user_id == user_id, UserCookie.platform == platform))
    if row:
        db.delete(row)
        db.commit()


def list_cookies(db: Session, user_id: int) -> list[dict]:
    """界面展示:平台 + 是否已配置 + 前缀掩码。"""
    rows = db.scalars(select(UserCookie).where(UserCookie.user_id == user_id)).all()
    result = []
    for p in PLATFORMS:
        row = next((r for r in rows if r.platform == p), None)
        if row:
            try:
                plain = decrypt_cookie(row.cookie)
            except Exception:  # noqa: BLE001
                plain = ""
            result.append({"platform": p, "configured": bool(plain), "preview": _mask(plain), "updated_at": row.updated_at.isoformat()})
        else:
            result.append({"platform": p, "configured": False, "preview": "", "updated_at": None})
    return result
