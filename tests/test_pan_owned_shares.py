"""我方自有分享登记(2026-10-20)。

判据是 **`pwd_id`(分享链路径段)**,不是资源名:
同名资源满街都是,按名字判会把别人的链误判成我们的(整类文章静默不推)。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import PanOwnedShare, User
from app.services import pan_accounts
from app.services import pan_owned_shares as pos
from app.services import quark_transfer as qtmod


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _item(pwd: str, created: datetime, title: str = "") -> dict:
    return {"pwd_id": pwd, "share_id": "sid-" + pwd,
            "share_url": f"https://pan.quark.cn/s/{pwd}",
            "title": title or ("资源-" + pwd), "first_fid": "fid-" + pwd,
            "status": 1, "created_at": _ms(created)}


class _FakeQt:
    pages: dict[int, list] = {}

    def __init__(self, *a, **k):
        pass

    def _request(self, method, path, api=None, params=None):
        page = int((params or {}).get("_page") or 1)
        return {"data": {"list": list(self.pages.get(page, []))}}


def _wire(monkeypatch, pages: dict[int, list]) -> None:
    ref = pan_accounts.AccountRef(label="夸克", platform="quark", cookie_key="quark")
    monkeypatch.setattr(pan_accounts, "accounts",
                        lambda session, platform, user_id=1: [ref])
    monkeypatch.setattr(pan_accounts, "cookie_of", lambda ref, session: "ck")
    _FakeQt.pages = pages
    monkeypatch.setattr(qtmod, "QuarkTransfer", _FakeQt)


def test_pwd_id_只认分享路径段():
    assert pos.pwd_id_of("https://pan.quark.cn/s/abc123?pwd=xy12") == "abc123"
    assert pos.normalize_url("https://pan.quark.cn/s/abc123?pwd=xy12") == \
        "https://pan.quark.cn/s/abc123"
    assert pos.pwd_id_of("abc123") == "abc123"
    assert pos.pwd_id_of("https://example.com/s/abc") == ""
    assert pos.pwd_id_of("") == ""


def test_全量登记_两页_短页收尾(session, monkeypatch):
    now = datetime.now()
    _wire(monkeypatch, {
        1: [_item(f"p{i}", now - timedelta(minutes=i)) for i in range(50)],
        2: [_item("tail", now - timedelta(hours=2))],
    })
    stats = pos.refresh(session, 1, full=True)
    assert stats == {"accounts": 1, "fetched": 51, "added": 51,
                     "updated": 0, "errors": 0}
    rows = session.query(PanOwnedShare).all()
    assert len(rows) == 51
    assert {r.account_key for r in rows} == {"quark"}
    assert pos.owned_urls(session, 1) == {"https://pan.quark.cn/s/tail"} | \
        {f"https://pan.quark.cn/s/p{i}" for i in range(50)}


def test_增量只收比库里新的(session, monkeypatch):
    old = datetime.now() - timedelta(days=2)
    session.add(PanOwnedShare(user_id=1, platform="quark", account_key="quark",
                              pwd_id="old", url="https://pan.quark.cn/s/old",
                              created_at=old))
    session.commit()
    now = datetime.now()
    _wire(monkeypatch, {
        1: [_item("new", now), _item("old", old)],   # 倒序:新在前,旧在后
        2: [_item("older", old - timedelta(days=1))],
    })
    stats = pos.refresh(session, 1)
    assert stats["fetched"] == 1 and stats["added"] == 1 and stats["updated"] == 0
    assert {r.pwd_id for r in session.query(PanOwnedShare).all()} == {"old", "new"}


def test_翻到上限仍没到底必须抛(session, monkeypatch):
    now = datetime.now()
    _wire(monkeypatch, {
        1: [_item(f"a{i}", now - timedelta(minutes=i)) for i in range(50)],
        2: [_item(f"b{i}", now - timedelta(hours=1, minutes=i)) for i in range(50)],
    })
    with pytest.raises(RuntimeError):
        pos.fetch_quark_shares(_FakeQt(), page_cap=2)


def test_41017实时登记_幂等(session):
    assert pos.record_owned(session, 1, "https://pan.quark.cn/s/own1", title="A")
    assert pos.record_owned(session, 1, "https://pan.quark.cn/s/own1?pwd=x", title="B")
    rows = session.query(PanOwnedShare).all()
    assert len(rows) == 1
    assert rows[0].title == "B"                 # 后到的名字覆盖,但不新增行
    assert not pos.record_owned(session, 1, "https://example.com/x")


def test_登记表读失败不挡推送(session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(session, "scalars", boom)
    assert pos.owned_urls(session, 1) == set()
