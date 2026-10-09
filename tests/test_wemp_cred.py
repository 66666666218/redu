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


# ---------------------------------------------------------------------------
# ★★ 2026-10-09:凭据**录入接口**(用户问的"有没有界面我自己粘")
# ---------------------------------------------------------------------------


class _User:
    id = 1


def _put(session, monkeypatch, *, cookie="CK", token="TK", probe=None):
    """调录入接口(直接调函数,不走 HTTP)。"""
    from app.api.cookies import wemp_cred_put
    from app.api.deps import WempCredIn

    if probe is not None:
        monkeypatch.setattr(wemp_cred, "probe", probe)
    return wemp_cred_put(WempCredIn(cookie=cookie, token=token), _User(), session)


def test_探针成功才落库(session, monkeypatch) -> None:
    """★ 保存时会**当场打一枪验活**;成功了才真的把凭据写进去。"""
    out = _put(session, monkeypatch, probe=lambda db, uid, ck, tk: {
        "ok": True, "items": 7, "mp": "某号", "titles": ["a"]})
    assert out["saved"] is True and out["items"] == 7
    assert wemp_cred.load(session, 1).get("cookie") == "CK"


def test_会话失效时不许落库_别覆盖还能用的那份(session, monkeypatch) -> None:
    """★★ **这是这个接口最要紧的一条**:顺序必须是「先验活、成功了才落库」。

    反过来(先存再验)的话,一次**粘错**就会把**还能用的那份覆盖成死的** ——
    而症状是"公众号列表源悄悄少一个",属于**静默退化**(本仓最恨的那类)。
    """
    from fastapi import HTTPException

    from app.services.wechat.wemp_client import WempAuthError

    wemp_cred.save(session, 1, "OLD_GOOD", "OLD_TOKEN")          # 先放一份"还能用的"

    def _dead(db, uid, ck, tk):
        raise WempAuthError("公众号后台会话失效(200003)")

    with pytest.raises(HTTPException) as e:
        _put(session, monkeypatch, probe=_dead)
    assert e.value.status_code == 400
    assert "没有保存" in e.value.detail and "mp.weixin.qq.com" in e.value.detail
    got = wemp_cred.load(session, 1)
    assert got.get("cookie") == "OLD_GOOD", f"把还能用的凭据覆盖掉了:{got}"


def test_被限流仍然落库_凭据本身是有效的(session, monkeypatch) -> None:
    """反面对照:**被频率限制(200013)说明凭据是好的**,只是配额满了 ⇒ 该存。"""
    from fastapi import HTTPException

    from app.services.wechat.wemp_client import WempRateLimited

    def _limited(db, uid, ck, tk):
        raise WempRateLimited("200013")

    with pytest.raises(HTTPException) as e:
        _put(session, monkeypatch, probe=_limited)
    assert "有效" in e.value.detail and "已保存" in e.value.detail
    assert wemp_cred.load(session, 1).get("cookie") == "CK", "被限流的凭据该留下"


def test_两个字段缺一不可(session, monkeypatch) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        _put(session, monkeypatch, cookie="", token="TK",
             probe=lambda *a: {"ok": True, "items": 1})
    assert "都要填" in e.value.detail


def test_探测函数_库里没对标号时不算失败(session, monkeypatch) -> None:
    """⚠️ 探不了 ≠ 失败:凭据已保存,监听轮会用它 —— 别把它报成"验活失败"。"""
    out = wemp_cred.probe(session, 1, "CK", "TK")
    assert out["ok"] is True and out["items"] == -1 and out["note"]
