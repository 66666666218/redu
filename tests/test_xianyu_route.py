"""闲鱼采集**选路**测试(2026-10-08):协议优先 / 浏览器兜底 / 什么时候该早退。

## 为什么单独一个文件
`tests/test_xianyu.py|browser|risk` 那三个测的是**解析与错误映射**。本文件测的是
"**用哪个客户端**" —— 这是 2026-10-08 才存在的分支,此前闲鱼**只有**浏览器一条路。

## 背景一句话
`RGV587_ERROR::被挤爆啦` 的归因被推翻(`tools/xianyu_replay_probe.py` 的决定性实验证明
卡的是 **cookie 而不是出口**),所以闲鱼改回**协议优先**(约 0.35 秒/词),
浏览器只作兜底 —— 它要 10~20 秒,还偶发 `TargetClosedError` 让整轮失败。

⚠️ 本文件**一个真请求都不发**,也不开浏览器。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import RunRecord, User

#: 完整 jar:`unb`+`cookie2` 是协议路的硬门槛
_FULL_COOKIE = "_m_h5_tk=tk_1_1; unb=1; cookie2=c2; sgcookie=SG; tfstk=TF"
#: 残缺:没有 unb ⇒ 协议路必 TOKEN_ILLEGAL
_PARTIAL_COOKIE = "_m_h5_tk=tk_1_1; sgcookie=SG"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(username="t", password_hash="x"))
    db.commit()
    yield db
    db.close()


def _settings(**kw):
    from config.settings import Settings

    return Settings(_env_file=None, **kw)


class _Stub:
    """客户端替身。**必须带 `cookie_header()`** —— 真客户端都有,而 `run_xianyu`
    会拿它回写轮换后的令牌;少了它只会在日志里刷一条 WARNING,把测试搞脏。"""

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def cookie_header(self) -> str:
        return ""          # 浏览器路返回空串,协议路返回整套 jar;空串 = 不回写

    def __repr__(self) -> str:
        return f"<{self.tag}>"


@pytest.fixture
def seen(monkeypatch):
    """记录"用了哪个客户端" —— 这就是本文件要验的东西。"""
    from app.services import tenant, xianyu as xy
    from app.services import xianyu_browser

    log: list[str] = []

    monkeypatch.setattr(xy, "XianyuClient",
                        lambda ck, proxy=None: (log.append("协议"), _Stub("P"))[1])
    monkeypatch.setattr(xianyu_browser, "get_client",
                        lambda s: (log.append("浏览器"), _Stub("B"))[1])
    monkeypatch.setattr(tenant, "persist_refreshed_cookie", lambda *a, **k: False)
    # ⚠️ 关掉"搜索接力深采":那是**另一条**链路(它自己也选路),留着只会往 `log` 里
    # 多塞一次客户端、把本文件的断言搅浑。深采的选路由 `pick_xianyu_client` 那一份规则
    # 统一保证(见 tenant_base 的 docstring)。
    monkeypatch.setattr(tenant, "xianyu_deep_due", lambda *a, **k: False)
    return log


def _fake_collect(rows=None, boom: list | None = None):
    """`collect_hot` 替身:`boom` 里的异常按调用次数依次抛。"""
    calls: list[object] = []

    def _c(settings, client, start_offset=0, stats=None):
        calls.append(client)
        if boom and len(calls) <= len(boom) and boom[len(calls) - 1] is not None:
            raise boom[len(calls) - 1]
        return rows if rows is not None else [
            {"item_id": "i1", "title": "PS教程 全套", "hit_keywords": 1,
             "keywords": "ps教程", "best_rank": 1}]

    _c.calls = calls          # type: ignore[attr-defined]
    return _c


def _detail(db) -> str:
    r = db.scalars(select(RunRecord).where(RunRecord.kind == "xianyu")
                   .order_by(RunRecord.id.desc()).limit(1)).first()
    return (r.detail or "") if r else ""


def _put_cookie(session, value: str = _FULL_COOKIE) -> None:
    from app.services import cookie_store

    cookie_store.set_cookie(session, 1, "goofish", value)
    session.commit()


# ---------------------------------------------------------------------------
# 选路
# ---------------------------------------------------------------------------


def test_默认走协议_且不碰浏览器(session, monkeypatch, seen):
    """★ 这是这次改动的全部意义:完整 jar ⇒ 协议优先,连浏览器都不建。"""
    from app.services import tenant, xianyu as xy

    _put_cookie(session)
    monkeypatch.setattr(xy, "collect_hot", _fake_collect())
    tenant.run_xianyu(session, 1, settings=_settings())
    assert seen == ["协议"], f"期望只走协议,实际:{seen}"
    assert "[协议]" in _detail(session)


def test_cookie不全但允许浏览器_就直接走浏览器(session, monkeypatch, seen):
    """协议跑不了的硬门槛是 `unb`/`cookie2`;缺了**别硬撞** —— 先走浏览器拿数据。"""
    from app.services import tenant, xianyu as xy

    _put_cookie(session, _PARTIAL_COOKIE)
    monkeypatch.setattr(xy, "collect_hot", _fake_collect())
    tenant.run_xianyu(session, 1, settings=_settings())
    assert seen == ["浏览器"], f"cookie 不全时不该硬走协议:{seen}"


def test_cookie不全且禁止浏览器_要早退而不是硬撞(session, monkeypatch, seen):
    """★ 两条路都不通时**早退**:硬撞只会多吃一次 `TOKEN_ILLEGAL`,还会在风控那边记一笔。"""
    from app.services import tenant, xianyu as xy

    _put_cookie(session, _PARTIAL_COOKIE)
    monkeypatch.setattr(xy, "collect_hot", _fake_collect())
    out = tenant.run_xianyu(session, 1,
                            settings=_settings(xianyu_use_browser=False))
    assert out["status"] == "skipped" and "cookie_incomplete" in out["reason"]
    assert seen == [], f"早退就不该碰任何客户端:{seen}"
    assert "cookie_incomplete" in _detail(session)


def test_关掉协议优先就完全按老路走(session, monkeypatch, seen):
    """`xianyu_prefer_protocol=False` 要真能回滚到"走浏览器"。"""
    from app.services import tenant, xianyu as xy

    _put_cookie(session)
    monkeypatch.setattr(xy, "collect_hot", _fake_collect())
    tenant.run_xianyu(session, 1, settings=_settings(xianyu_prefer_protocol=False))
    assert seen == ["浏览器"], f"回滚开关没生效:{seen}"


# ---------------------------------------------------------------------------
# 兜底
# ---------------------------------------------------------------------------


def test_协议失败_回落浏览器并留痕(session, monkeypatch, seen, caplog):
    """★ 回落必须**既拿到数据、又留下痕迹**。

    两条路都会产出 0 条;不标来源的话,「协议挂了被回落」与「真没货上架」长得一模一样
    —— 那正是本仓最忌讳的假阴性。
    """
    from app.services import tenant, xianyu as xy

    _put_cookie(session)
    collect = _fake_collect(boom=[xy.XianyuRateLimit("被挤爆啦")])
    monkeypatch.setattr(xy, "collect_hot", collect)
    with caplog.at_level("WARNING"):
        tenant.run_xianyu(session, 1, settings=_settings())
    assert seen == ["协议", "浏览器"], f"协议失败后应回落浏览器,实际:{seen}"
    assert len(collect.calls) == 2, "回落要**真的重跑一次采集**"
    assert collect.calls[1].tag == "B", "第二次必须用浏览器那个 client"
    assert any("回落浏览器" in r.message for r in caplog.records), "回落没留痕"
    assert "[浏览器兜底]" in _detail(session), "运行记录里要能看出是被回落的"


def test_协议失败但禁止浏览器_就抛出去(session, monkeypatch, seen):
    """★ **不许吞**:吞掉会被记成"成功但 0 条",而这条链是无人值守跑的。"""
    from app.services import tenant, xianyu as xy

    _put_cookie(session)
    monkeypatch.setattr(xy, "collect_hot",
                        _fake_collect(boom=[xy.XianyuCookieExpired("令牌过期")]))
    with pytest.raises(xy.XianyuCookieExpired):
        tenant.run_xianyu(session, 1, settings=_settings(xianyu_use_browser=False))
    assert seen == ["协议"], "关掉浏览器就不该有回落"


def test_浏览器路自己失败不回落_直接抛(session, monkeypatch, seen):
    """反面对照:已经是浏览器了就没有"再回落"一说,别绕成死循环。"""
    from app.services import tenant, xianyu as xy

    _put_cookie(session, _PARTIAL_COOKIE)          # 逼它一开始就走浏览器
    monkeypatch.setattr(xy, "collect_hot",
                        _fake_collect(boom=[xy.XianyuVerify("滑块")]))
    with pytest.raises(xy.XianyuVerify):
        tenant.run_xianyu(session, 1, settings=_settings())
    assert seen == ["浏览器"], f"不该二次回落:{seen}"


# ---------------------------------------------------------------------------
# ★ 深采必须与搜索同路(2026-10-03 就是在"两边分叉"上栽的)
# ---------------------------------------------------------------------------


def test_深采与搜索走同一条路_协议(session, monkeypatch, seen):
    """★ **2026-10-03 的真实事故**:搜索换了浏览器、深采还在用协议 ⇒ 行情
    (想要数/收藏/出单)一直卡在"被挤爆"那条路上,而症状看起来像"接口没数据"。

    现在两边共用 `tenant_base.pick_xianyu_client`,这条测试把它钉住 ——
    谁要是只改一边,这里会红。
    """
    from app.services import xianyu_analytics as xa

    _put_cookie(session)
    xa.run_xianyu_deep(session, 1, settings=_settings(), hot=[])
    assert seen == ["协议"], f"深采没跟搜索同路:{seen}"


def test_深采在_cookie_不全时也走浏览器(session, monkeypatch, seen):
    """同一条规则的另一面 —— 免得只修了一个方向。"""
    from app.services import xianyu_analytics as xa

    _put_cookie(session, _PARTIAL_COOKIE)
    xa.run_xianyu_deep(session, 1, settings=_settings(), hot=[])
    assert seen == ["浏览器"], f"深采没跟搜索同路:{seen}"
