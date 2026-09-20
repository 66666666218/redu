"""闲鱼风控优化单测:验证冷却指数退避 + 详情当天去重(无网络)。"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import RunRecord, User, XianyuDaily
from config.settings import Settings
from app.services import xianyu_analytics
from app.services.cookie_store import set_cookie
from app.services.tenant_base import _record_run, verify_cooldown_active


def _settings(**kw) -> Settings:
    base = {"xianyu_cooldown_minutes": 30, "xianyu_detail_limit": 10}
    base.update(kw)
    return Settings(_env_file=None, is_dev=True, **base)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def test_verify_cooldown_exponential_backoff(session) -> None:
    """连续触发验证 → 冷却时间指数退避(base→2x),无 IP 可换时避免反复撞滑块。"""
    st = _settings()
    assert verify_cooldown_active(session, 1, st) is False  # 无记录 → 不冷却

    _record_run(session, 1, "xianyu", "failed", "XianyuVerify: 滑块")
    session.commit()
    assert verify_cooldown_active(session, 1, st) is True  # 刚触发 → 冷却中

    # 把唯一记录挪到 40 分钟前:单次退避=30min → 已过期
    row = session.scalars(select(RunRecord)).first()
    row.started_at = datetime.now() - timedelta(minutes=40)
    session.commit()
    assert verify_cooldown_active(session, 1, st) is False

    # 再加第二条(近 24h 共 2 次)→ 退避 60min,40 分钟前的记录仍在窗口内
    _record_run(session, 1, "xianyu", "failed", "XianyuVerify: 滑块")
    session.commit()
    latest = session.scalars(select(RunRecord).order_by(RunRecord.id.desc())).first()
    latest.started_at = datetime.now() - timedelta(minutes=40)
    session.commit()
    assert verify_cooldown_active(session, 1, st) is True


def test_xianyu_deep_skips_items_already_cached_today(monkeypatch, session) -> None:
    """详情当天已抓过的商品不再重复请求,额度只给未抓商品。"""
    session.add(User(id=1, username="u", password_hash="x"))
    session.commit()
    set_cookie(session, 1, "goofish", "ck")
    today = datetime.now().date().isoformat()
    session.add(XianyuDaily(user_id=1, snap_date=today, item_id="i1", title="已抓"))
    session.commit()

    calls: list[str] = []
    monkeypatch.setattr(xianyu_analytics.xianyu, "XianyuClient", lambda ck, proxy=None: object())
    monkeypatch.setattr(xianyu_analytics.xianyu, "fetch_detail",
                        lambda client, iid: calls.append(iid) or {"want_count": 1})
    out = xianyu_analytics.run_xianyu_deep(
        session, 1, settings=_settings(),
        hot=[{"item_id": "i1", "title": "已抓", "price": "1"},
             {"item_id": "i2", "title": "新商品", "price": "2"}])
    assert calls == ["i2"]  # i1 当天已抓 → 跳过;i2 新商品 → 抓
    assert out["count"] == 1

    # 全部商品当天都抓过 → 跳过整轮(不再空打详情接口)
    out2 = xianyu_analytics.run_xianyu_deep(
        session, 1, settings=_settings(), hot=[{"item_id": "i2", "title": "新商品", "price": "2"}])
    assert out2["status"] == "skipped" and out2["reason"] == "cached_today"
    assert calls == ["i2"]  # 没有新的详情请求


def test_xianyu_deep_tolerates_none_detail_without_losing_round(monkeypatch, session) -> None:
    """单品详情普通失败(返回 None)只跳过该商品:不得崩成整轮 rollback 丢掉已采数据。"""
    session.add(User(id=1, username="u", password_hash="x"))
    session.commit()
    set_cookie(session, 1, "goofish", "ck")

    monkeypatch.setattr(xianyu_analytics.xianyu, "XianyuClient", lambda ck, proxy=None: object())
    # i1 → None(普通失败),i2 → 正常。修复前:None.get() 抛错 → 外层 rollback 丢 i2 + raise。
    monkeypatch.setattr(xianyu_analytics.xianyu, "fetch_detail",
                        lambda client, iid: None if iid == "i1" else {"want_count": 7, "category": "C"})
    out = xianyu_analytics.run_xianyu_deep(
        session, 1, settings=_settings(),
        hot=[{"item_id": "i1", "title": "坏详情", "price": "1"},
             {"item_id": "i2", "title": "正常", "price": "2"}])
    assert out["status"] == "success" and out["count"] == 1  # i1 跳过、i2 保住
    today = datetime.now().date().isoformat()
    ids = {r.item_id for r in session.scalars(select(XianyuDaily).where(XianyuDaily.snap_date == today))}
    assert ids == {"i2"}  # 坏详情不落成假 0 快照


def test_waf_block_triggers_same_cooldown(session) -> None:
    """WAF 空响应(XianyuWafBlock)与滑块同级:计入冷却、指数退避。"""
    st = _settings()
    _record_run(session, 2, "xianyu", "failed", "XianyuWafBlock: 闲鱼网关空响应(疑似 WAF 风控拦截)")
    session.commit()
    assert verify_cooldown_active(session, 2, st) is True  # 刚触发 → 冷却中

    # 挪出默认冷却窗口(30min)后,单次 WAF 不再冷却
    row = session.scalars(select(RunRecord).where(RunRecord.user_id == 2)).first()
    row.started_at = datetime.now() - timedelta(minutes=40)
    session.commit()
    assert verify_cooldown_active(session, 2, st) is False

    # 24h 内第 2 次 → 退避翻倍(60min),40 分钟前的那次仍在窗口内
    _record_run(session, 2, "xianyu_deep", "failed", "XianyuWafBlock: 网关空响应")
    session.commit()
    latest = session.scalars(
        select(RunRecord).where(RunRecord.user_id == 2).order_by(RunRecord.id.desc())).first()
    latest.started_at = datetime.now() - timedelta(minutes=40)
    session.commit()
    assert verify_cooldown_active(session, 2, st) is True


def test_post_empty_ret_raises_waf_block(monkeypatch) -> None:
    """_post 遇网关空 ret → XianyuWafBlock(不再误报'限流:None')。"""
    from app.services import xianyu as xy

    class _Resp:
        status_code = 200
        text = '{"api":"mtop.taobao.idle.pc.search"}'
        headers = {}

        def json(self):
            return {"api": "mtop.taobao.idle.pc.search", "ret": [], "data": {}}

    qt = xy.XianyuClient("cookie=x")
    monkeypatch.setattr(qt.session, "post", lambda *a, **kw: _Resp())
    try:
        qt._post("mtop.taobao.idle.pc.search", {"keyword": "x"})
        raised = ""
    except xy.XianyuWafBlock as exc:
        raised = str(exc)
    except Exception as exc:  # noqa: BLE001
        raised = f"wrong-type:{type(exc).__name__}"
    assert "WAF" in raised, raised


def test_token_loop_exhaustion_raises_cookie_expired(monkeypatch) -> None:
    """令牌循环过期(刷新成功但新令牌一用即过期)→ CookieExpired,不再误报'限流:None'。"""
    import re
    from app.services import xianyu as xy

    class _Resp:
        status_code = 200
        text = "{}"
        headers = {"set-cookie": "_m_h5_tk=abcdef0123456789abcdef0123456789_1760000000; Path=/; Domain=.goofish.com"}

        def json(self):
            return {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"], "data": {}}

    qt = xy.XianyuClient("cookie=x")
    monkeypatch.setattr(qt.session, "post", lambda *a, **kw: _Resp())
    try:
        qt._post("mtop.taobao.idle.pc.search", {"keyword": "x"})
        raised = ""
    except xy.XianyuCookieExpired as exc:
        raised = str(exc)
    except Exception as exc:  # noqa: BLE001
        raised = f"wrong-type:{type(exc).__name__}: {exc}"
    assert "令牌循环过期" in raised, raised
