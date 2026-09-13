"""cookie_store 单测:旧密钥坏行的自愈回写(2026-09-13 实战故障回归)。"""
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, UserCookie
from app.services import cookie_store as cs


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _old_key() -> bytes:
    """模拟'当年'的另一把密钥(JWT_SECRET 轮换前)。"""
    return Fernet.generate_key()


def test_broken_row_heals_from_env_fallback(monkeypatch):
    db = _session()
    old = Fernet(_old_key())
    cipher = old.encrypt("微博旧Cookie".encode()).decode()
    db.add(UserCookie(user_id=1, platform="weibo", cookie=cipher))
    db.commit()

    class _St:
        weibo_cookie = "微博新Cookie来自env"

    out = cs.get_cookies(db, 1, settings=_St())
    assert out["weibo"] == "微博新Cookie来自env"
    # 行已用当前密钥重写(自愈),再次读取直接成功
    row = db.scalar(select(UserCookie).where(UserCookie.user_id == 1, UserCookie.platform == "weibo"))
    assert cs.decrypt_cookie(row.cookie) == "微博新Cookie来自env"


def test_broken_row_without_fallback_warns_and_skips(monkeypatch):
    db = _session()
    old = Fernet(_old_key())
    db.add(UserCookie(user_id=1, platform="goofish", cookie=old.encrypt(b"x").decode()))
    db.commit()

    class _St:
        goofish_cookie_file = "data/__不存在__.txt"

    out = cs.get_cookies(db, 1, settings=_St())
    assert "goofish" not in out  # 无兜底源:保持缺失,提示人工重贴


def test_missing_row_not_backfilled(monkeypatch):
    """行缺失=用户从未配置,不代填(避免多用户复制同一份全局 Cookie)。"""
    db = _session()

    class _St:
        weibo_cookie = "全局env的Cookie"

    out = cs.get_cookies(db, 1, settings=_St())
    assert "weibo" not in out


def test_get_cookie_broken_returns_none():
    db = _session()
    old = Fernet(_old_key())
    db.add(UserCookie(user_id=1, platform="weread", cookie=old.encrypt(b"x").decode()))
    db.commit()
    assert cs.get_cookie(db, 1, "weread") is None
