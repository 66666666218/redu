"""多租户平台:安全(Cookie加密/JWT)与 Cookie 存取单测(SQLite 会话)。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")  # >=32字节
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import DouhotWatchSnap, XianyuDaily
from app.security import create_access_token, decode_token, decrypt_cookie, encrypt_cookie
from app.services import cookie_store, tenant
from app.auth import authenticate, create_password_reset_token, register_user, reset_password


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


def test_register_and_authenticate(session) -> None:
    register_user(session, "a@b.com", "p123456")
    assert authenticate(session, "a@b.com", "p123456") is not None
    assert authenticate(session, "a@b.com", "wrong") is None
    with pytest.raises(HTTPException):
        register_user(session, "a@b.com", "p123456")


def test_forgot_and_reset_password(session) -> None:
    register_user(session, "a@b.com", "p123456")
    assert create_password_reset_token(session, "no@mail.com") is None  # 不暴露存在性
    token = create_password_reset_token(session, "a@b.com")
    assert token
    assert reset_password(session, "bad", "x") is False
    assert reset_password(session, token, "newpass123") is True
    assert authenticate(session, "a@b.com", "newpass123") is not None


def _xy(user_id, snap, iid, want, cat):
    return XianyuDaily(user_id=user_id, snap_date=snap, item_id=iid, title=iid, want_count=want, category=cat)


def test_xianyu_analytics(session) -> None:
    from datetime import date, timedelta

    today = date.today().isoformat()
    yest = (date.today() - timedelta(days=1)).isoformat()
    session.add_all([
        _xy(1, yest, "a", 100, "教程"),
        _xy(1, today, "a", 150, "教程"),
        _xy(1, today, "b", 10, "软件"),
    ])
    session.commit()
    a = tenant.xianyu_analytics(session, 1)
    assert a["count"] == 2
    assert a["total_want"] == 160
    it = [x for x in a["items"] if x["item_id"] == "a"][0]
    assert it["want_today"] == 150 and it["want_yesterday"] == 100 and it["delta"] == 50
    assert abs(it["pct"] - 0.5) < 1e-6
    cats = {c["name"]: c["count"] for c in a["categories"]}
    assert cats == {"教程": 1, "软件": 1}


def test_run_xianyu_deep(session, monkeypatch) -> None:
    from config.settings import Settings

    monkeypatch.setattr(
        "app.services.xianyu.collect_hot",
        lambda settings, client: [{"item_id": "a", "title": "A", "price": "¥1", "seller": "s", "pic": "",
                                   "hit_keywords": 1, "best_rank": 1, "keywords": "kw"}],
    )
    monkeypatch.setattr(
        "app.services.xianyu.fetch_detail",
        lambda client, iid: {"category": "教程", "want_count": 150, "view_count": 300, "seller_fans": 99},
    )
    cookie_store.set_cookie(session, 1, "goofish", "fake-cookie")
    r = tenant.run_xianyu_deep(session, 1, Settings(_env_file=None))
    assert r["count"] == 1
    row = session.scalar(select(XianyuDaily).where(XianyuDaily.user_id == 1))
    assert row.want_count == 150 and row.category == "教程"


def test_douhot_watch_analytics(session) -> None:
    tenant.add_douhot_watch(session, 1, "word", "景甜")
    assert len(tenant.list_douhot_watch(session, 1)) == 1
    session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="景甜", score=500, rank_now=1))
    session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="景甜", score=700, rank_now=1))
    session.commit()
    out = tenant.douhot_watch_analytics(session, 1)
    assert out[0]["keyword"] == "景甜" and out[0]["last_score"] == 700 and out[0]["points"] == 2


def _alert_settings():
    from config.settings import Settings

    return Settings(_env_file=None, is_dev=True)  # NullNotifier,不真发邮件


def test_alert_rules_crud(session) -> None:
    from app.services import alert_service

    r = alert_service.add_rule(session, 1, "weibo", "threshold", metric="growth", threshold=0.3)
    assert alert_service.list_rules(session, 1)[0]["rule_type"] == "threshold"
    assert alert_service.delete_rule(session, 1, r.id) is True
    assert alert_service.list_rules(session, 1) == []


def test_alert_threshold_and_cooldown(session) -> None:
    from app.db.models import AlertRecord
    from app.services import alert_service

    alert_service.add_rule(session, 1, "weibo", "threshold", metric="growth", threshold=0.3)
    s = _alert_settings()
    n = alert_service.evaluate(session, 1, "weibo", [{"key": "A", "growth": 0.5}], set(), s)
    assert n > 0
    rec = session.scalar(select(AlertRecord).where(AlertRecord.user_id == 1))
    assert rec and rec.keyword == "A"
    # 冷却期内,同一规则不再重复触发
    assert alert_service.evaluate(session, 1, "weibo", [{"key": "A", "growth": 0.5}], set(), s) == 0


def test_alert_new(session) -> None:
    from app.services import alert_service

    alert_service.add_rule(session, 1, "douhot", "new")
    n = alert_service.evaluate(session, 1, "douhot", [{"key": "新词", "score": 1}], {"旧词"}, _alert_settings())
    assert n > 0


def test_alert_keyword_filter(session) -> None:
    from app.services import alert_service

    alert_service.add_rule(session, 1, "douhot", "new", keyword="B")
    n = alert_service.evaluate(session, 1, "douhot", [{"key": "A", "score": 1}], set(), _alert_settings())
    assert n == 0


def test_admin_dashboard_and_users(session) -> None:
    from app import admin as admin_svc
    from app.db.models import User

    register_user(session, "adm@x.com", "p", "adm")
    u = session.scalar(select(User).where(User.email == "adm@x.com"))
    u.role = "admin"
    session.commit()
    assert admin_svc.list_users(session, "")[0]["role"] == "admin"
    d = admin_svc.dashboard(session)
    assert d["counts"]["users"] >= 1
    assert d["today_runs"] == 0
    # 切换启用/禁用
    assert admin_svc.toggle_user(session, u.id)["enabled"] is False


def test_rbac_perms() -> None:
    from app import admin as admin_svc

    # admin 拥有全部
    for perm in ("users.delete", "users.import", "config.set", "users.toggle"):
        assert admin_svc.has_perm("admin", perm)
    # operator:可查/启停/导出,不可删/导入/改配置
    assert admin_svc.has_perm("operator", "users.toggle")
    assert admin_svc.has_perm("operator", "data.export")
    assert not admin_svc.has_perm("operator", "users.delete")
    assert not admin_svc.has_perm("operator", "users.import")
    assert not admin_svc.has_perm("operator", "config.set")
    # 普通用户无任何后台权限
    assert admin_svc.perms_for("user") == set()
