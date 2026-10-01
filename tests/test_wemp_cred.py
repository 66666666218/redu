"""公众号后台凭据存取单测(2026-10-01):加密落库 + 历史明文值兼容。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import SystemConfig
from app.services import wemp_cred


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def test_save_stores_ciphertext(session) -> None:
    """凭据必须加密落库——它能操作公众号后台,权限比用户面 Cookie 还大,没理由明文。"""
    wemp_cred.save(session, 1, "slave_sid=SECRET", "1234567")
    raw = session.scalar(select(SystemConfig).where(SystemConfig.key == "wemp_cred_1")).value
    assert raw.startswith("enc:")
    assert "SECRET" not in raw                       # 明文不出现在库里
    assert wemp_cred.load(session, 1) == {"cookie": "slave_sid=SECRET", "token": "1234567"}


def test_load_reads_legacy_plaintext(session) -> None:
    """向后兼容:`enc:` 之前写入的是明文 JSON,改存储方式不能让它读不出来
    ——读不出来等于凭据凭空失效,得让人重新登录一次后台。"""
    session.add(SystemConfig(key="wemp_cred_1",
                             value='{"cookie": "slave_sid=OLD", "token": "999"}'))
    session.commit()
    assert wemp_cred.load(session, 1) == {"cookie": "slave_sid=OLD", "token": "999"}


def test_load_returns_empty_on_broken_ciphertext(session) -> None:
    """密文坏掉(密钥变更等)按"未配置"处理、不抛异常:调用方是择源链,
    单源失效必须能安静降级到下一个源。"""
    session.add(SystemConfig(key="wemp_cred_1", value="enc:not-a-valid-fernet-token"))
    session.commit()
    assert wemp_cred.load(session, 1) == {}


def test_save_is_idempotent_and_exists_flips(session) -> None:
    assert wemp_cred.exists(session, 1) is False
    wemp_cred.save(session, 1, "a", "1")
    assert wemp_cred.exists(session, 1) is True
    wemp_cred.save(session, 1, "b", "2")             # 覆盖而非新增一行
    assert wemp_cred.load(session, 1)["cookie"] == "b"
    assert len(session.scalars(select(SystemConfig)).all()) == 1
