"""多租户平台:安全(Cookie加密/JWT)与 Cookie 存取单测(SQLite 会话)。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")  # >=32字节
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.security import create_access_token, decode_token, decrypt_cookie, encrypt_cookie
from app.services import cookie_store


def test_cookie_encrypt_roundtrip() -> None:
    plain = "SESSION=abc; TOKEN=xyz"
    assert decrypt_cookie(encrypt_cookie(plain)) == plain


def test_jwt_roundtrip() -> None:
    token = create_access_token(42)
    assert decode_token(token) == 42
    assert decode_token("garbage") is None


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    yield db
    db.close()


def test_cookie_store_crud(session) -> None:
    cookie_store.set_cookie(session, 1, "weibo", "ABC")
    assert cookie_store.get_cookie(session, 1, "weibo") == "ABC"
    lst = cookie_store.list_cookies(session, 1)
    assert lst[0]["platform"] == "weibo" and lst[0]["configured"] is True
    assert lst[0]["preview"].startswith("ABC")
    cookie_store.delete_cookie(session, 1, "weibo")
    assert cookie_store.get_cookie(session, 1, "weibo") is None


def test_cookie_store_rejects_bad_platform(session) -> None:
    with pytest.raises(ValueError):
        cookie_store.set_cookie(session, 1, "unknown", "x")
