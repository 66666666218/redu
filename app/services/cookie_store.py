"""用户 Cookie 存取服务:每用户每平台存一个,加密入库。

采集时按用户取其 Cookie 注入采集器;数据按 user_id 隔离。
自愈:行存在但用当前密钥解不开(JWT_SECRET 缺失时期进程用随机临时密钥加密,
重启即全部失读,2026-09-13 实战踩坑)→ 用该平台的全局配置/Cookie 文件以当前
密钥回写,避免"静默坏到采集报未配置"。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import UserCookie
from app.security import decrypt_cookie, encrypt_cookie
from app.utils import get_logger

logger = get_logger(__name__)

PLATFORMS = ("weibo", "baidu", "douyin", "goofish", "weread", "dajiala")

# 平台 → 兜底源:env 全局 Cookie / 本机 Cookie 文件(settings 属性名)
_FILE_FALLBACKS = {"goofish": "goofish_cookie_file", "douyin": "douhot_cookie_file"}
_ENV_FALLBACKS = {"weibo": "weibo_cookie", "weread": "weread_cookie"}


def _fallback_plain(platform: str, settings: object) -> str:
    attr = _ENV_FALLBACKS.get(platform) or _FILE_FALLBACKS.get(platform)
    if not attr:
        return ""
    value = (getattr(settings, attr, "") or "").strip()
    if value and platform in _FILE_FALLBACKS:
        # 文件源:值是路径
        try:
            return Path(value).read_text("utf-8").strip()
        except OSError:
            return ""
    return value


def _mask(cookie: str) -> str:
    """返回前缀用于界面展示,不暴露完整密钥。

    只留 8 字符:wr_skey=xxxx 这类 Cookie 前 24 字符往往已覆盖有效凭据本体。
    """
    return cookie[:8] + "…" if len(cookie) > 8 else cookie


def get_cookie(db: Session, user_id: int, platform: str) -> str | None:
    """解密取某用户某平台 Cookie;未配置返回 None。"""
    row = db.scalar(select(UserCookie).where(UserCookie.user_id == user_id, UserCookie.platform == platform))
    if not row:
        return None
    try:
        return decrypt_cookie(row.cookie)
    except Exception:  # noqa: BLE001 - 解密失败视为未配置
        logger.warning("user=%s 平台=%s Cookie 用当前密钥无法解密(视为未配置)", user_id, platform)
        return None


def get_cookies(db: Session, user_id: int, settings: object | None = None) -> dict[str, str]:
    """取该用户全部(平台->明文 Cookie);行损坏时用全局/文件源自愈回写。

    只自愈"行存在但解不开"的行——行缺失=用户从未配置,不代填,避免多用户
    场景把同一份全局 Cookie 复制给所有用户。
    """
    if settings is None:
        from config.settings import get_settings
        settings = get_settings()
    rows = db.scalars(select(UserCookie).where(UserCookie.user_id == user_id)).all()
    out: dict[str, str] = {}
    for row in rows:
        try:
            out[row.platform] = decrypt_cookie(row.cookie)
            continue
        except Exception:  # noqa: BLE001 - 密钥轮换/临时密钥时期的坏行
            pass
        fb = _fallback_plain(row.platform, settings)
        if fb:
            set_cookie(db, user_id, row.platform, fb)  # 以当前密钥回写,自愈
            out[row.platform] = fb
            logger.warning("user=%s 平台=%s Cookie 旧密钥失效,已从全局/文件源自愈回写", user_id, row.platform)
        else:
            logger.warning("user=%s 平台=%s Cookie 旧密钥失效且无兜底源,请在界面重新粘贴", user_id, row.platform)
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
