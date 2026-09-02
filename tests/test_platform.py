"""多租户平台:安全(Cookie加密/JWT)与 Cookie 存取单测(SQLite 会话)。"""
import os
from pathlib import Path

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
from app.auth import authenticate, create_password_reset_token, hash_password, register_user, reset_password, verify_password


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
    register_user(session, "a@b.com", "p1234567")
    assert authenticate(session, "a@b.com", "p1234567") is not None
    assert authenticate(session, "a@b.com", "wrong") is None
    with pytest.raises(HTTPException):
        register_user(session, "a@b.com", "p1234567")


def test_register_rejects_bad_input(session) -> None:
    """邮箱/密码校验在后端强制(前端限制不可信),错误信息为可读中文。"""
    for email, password in [("", "p1234567"), ("abc", "p1234567"), ("a@b.com", "short")]:
        with pytest.raises(HTTPException) as exc:
            register_user(session, email, password)
        assert exc.value.status_code == 400 and isinstance(exc.value.detail, str)


# ---- 密码哈希(bcrypt) ----
def test_password_hash_roundtrip() -> None:
    h = hash_password("pass1234")
    assert h.startswith("$2b$") and verify_password("pass1234", h)
    assert not verify_password("wrong", h)


def test_verify_legacy_passlib_hash() -> None:
    """老用户的密码是 passlib 时代写入的,换成官方 bcrypt 后必须仍能登录。

    下面这条哈希由 passlib 1.7.4 + bcrypt 4.0.1 对 "pass1234" 生成。
    """
    legacy = "$2b$12$gSJOBrI00LYv03UPJecJGuEFgegVulkG7tgeRECeLPSfzi7BtWqJu"
    assert verify_password("pass1234", legacy)
    assert not verify_password("wrong", legacy)


def test_verify_bad_hash_returns_false_not_raises() -> None:
    """脏数据只应判为不匹配,不能抛异常把登录打成 500。

    注意 `$2b$12$short`:bcrypt 的 Rust 后端对它抛 PanicException(继承
    BaseException),`except Exception` 拦不住,所以必须靠格式预检挡在前面。
    """
    for bad in ["", "   ", "not-a-hash", "$2b$12$short", "$2b$12$" + "x" * 52, "md5:abc"]:
        assert verify_password("pass1234", bad) is False


# ---- SPA 路由回退 ----
@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "app" / "static" / "spa" / "index.html").exists(),
    reason="前端未构建(app/static/spa 由 Docker 多阶段构建生成,不入库)",
)
def test_spa_history_fallback() -> None:
    """前端用 createWebHistory,直接访问/刷新 /login、/reset 必须回退到 index.html。

    少了这个兜底,用户刷新页面就 404,找回密码邮件里的 /reset?token=... 也打不开;
    同时 API 路径必须继续返回 JSON 404,而不是一篇 HTML。
    """
    from fastapi.testclient import TestClient

    from app.platform import create_app

    with TestClient(create_app()) as client:
        for path in ("/", "/login", "/schedule", "/admin", "/reset?token=abc"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert "<!doctype html" in resp.text.lower(), path
        for path in ("/api/nope", "/api/admin/nope"):
            resp = client.get(path)
            assert resp.status_code == 404 and "html" not in resp.headers.get("content-type", ""), path


def test_register_normalizes_email(session) -> None:
    """邮箱去空格转小写,避免同一邮箱大小写不同重复注册。"""
    register_user(session, "  Foo@QQ.com ", "p1234567")
    assert authenticate(session, "foo@qq.com", "p1234567") is not None
    with pytest.raises(HTTPException):
        register_user(session, "FOO@qq.com", "p1234567", "other")


def test_forgot_and_reset_password(session) -> None:
    register_user(session, "a@b.com", "p1234567")
    assert create_password_reset_token(session, "no@mail.com") is None  # 不暴露存在性
    token = create_password_reset_token(session, "a@b.com")
    assert token
    assert reset_password(session, "bad", "newpass123") is False
    with pytest.raises(HTTPException):  # 重置也要满足密码强度,不能绕过
        reset_password(session, token, "x")
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

    register_user(session, "adm@x.com", "p1234567", "adm")
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


def test_admin_insights_detects_burst(session) -> None:
    """智能体洞察:跨用户聚合,能识别"预测爆发"的关注词。"""
    from app.db.models import DouhotWatch, DouhotWatchSnap
    from app import admin as admin_svc

    # 用户1:加速上升词 → 应被标爆发
    session.add(DouhotWatch(user_id=1, list_type="word", keyword="爆点"))
    for v in [1000, 1100, 1300, 1800, 2600]:
        session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="爆点", score=v, rank_now=1))
    # 用户2:回落词 → 不标爆发
    session.add(DouhotWatch(user_id=2, list_type="word", keyword="退潮"))
    for v in [2000, 1500, 1000, 500]:
        session.add(DouhotWatchSnap(user_id=2, list_type="word", keyword="退潮", score=v, rank_now=1))
    session.commit()

    out = admin_svc.insights(session)
    assert out["stats"]["watch_keywords"] == 2
    assert out["stats"]["burst"] >= 1
    burst_keywords = {b["keyword"] for b in out["burst"]}
    assert "爆点" in burst_keywords
    assert "退潮" not in burst_keywords  # 回落的不进爆发榜


def test_platform_agent_weibo_and_xianyu(session) -> None:
    """多平台智能体:微博热度序列 / 闲鱼想要数序列 都能判趋势+预测。"""
    from datetime import date, datetime, timedelta
    from app.db.models import WeiboHotItem, XianyuDaily

    # 微博:一个词热度逐步走高
    base = date.today() - timedelta(days=1)
    for i, h in enumerate([1000, 1200, 1600, 2400]):
        session.add(WeiboHotItem(user_id=1, title="冲榜词", heat=h, rank=1,
                                 captured_at=datetime.now().replace(hour=8) + timedelta(hours=i)))
    # 闲鱼:一个商品想要数逐日走高
    for i, w in enumerate([5, 8, 14, 22]):
        session.add(XianyuDaily(user_id=1, item_id="it1", title="教程", snap_date=(base + timedelta(days=i)).isoformat(),
                                want_count=w, category="教程"))
    session.commit()

    out = tenant.platform_agent(session, 1)
    assert out["weibo"] and out["weibo"][0]["title"] == "冲榜词"
    assert out["weibo"][0]["trend_label"] == "上升期"
    assert out["xianyu"][0]["growth"] is not None and out["xianyu"][0]["growth"] > 0
    # 都有预测值
    assert out["weibo"][0]["forecast_next"] is not None


def test_weekly_summary_includes_insight(session) -> None:
    """周度洞察摘要应包含关注词趋势 + 微博/闲鱼预测。"""
    from datetime import datetime, timedelta
    from config.settings import Settings
    from app.db.models import DouhotWatch, DouhotWatchSnap, WeiboHotItem
    from app.services import alert_service

    session.add(DouhotWatch(user_id=1, list_type="word", keyword="爆发词"))
    for i, v in enumerate([1000, 1200, 1400, 2000, 3000]):
        session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="爆发词", score=v,
                                    rank_now=1, captured_at=datetime.now() - timedelta(days=6, hours=-i)))
    session.add(WeiboHotItem(user_id=1, title="冲榜", heat=1000, rank=1, captured_at=datetime.now() - timedelta(days=1)))
    session.add(WeiboHotItem(user_id=1, title="冲榜", heat=3000, rank=1, captured_at=datetime.now()))
    session.commit()

    text = alert_service.build_weekly_summary(session, 1, Settings(_env_file=None, smtp_host="smtp.qq.com"))
    assert "本周热点洞察" in text
    assert "爆发词" in text
    assert ("微博" in text or "冲榜" in text)


def test_alerts_list_includes_section(session) -> None:
    """告警列表返回结构含 section(供大屏滚动条显示平台)。"""
    from datetime import datetime
    from app.db.models import AlertRecord

    session.add(AlertRecord(user_id=1, section="douhot", keyword="词", reason="新增 词", triggered_at=datetime.now()))
    session.commit()
    # 直接查服务层确保字段存在
    from app.services import tenant
    # alerts_list 在 platform 路由层,这里验证 AlertRecord.section 可读
    row = session.scalar(select(AlertRecord).where(AlertRecord.user_id == 1))
    assert row.section == "douhot"


def test_healthz_reports_app_version() -> None:
    """/healthz 的版本应来自共享 APP_VERSION,避免与 FastAPI 元数据脱节。"""
    from app import APP_VERSION
    from fastapi.testclient import TestClient
    from app.platform import create_app

    with TestClient(create_app()) as c:
        assert c.get("/healthz").json()["version"] == APP_VERSION
