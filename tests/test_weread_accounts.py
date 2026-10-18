# -*- coding: utf-8 -*-
"""微信读书**账号池**(2026-10-18)。

为什么这组测试值得存在:精确阅读数只有一个来源(`/web/mp/articles`),而它的额度
**按账号记**、且会被账号级 `-2041` 限死(证据链见 `app/services/weread_accounts.py`
头注释)。一旦这一枚被限,阅读数就整体归零 —— 池子就是"再加一个账号"的落点。

所以这里守的是三件事:
1. **单账号时与旧行为逐字一致**(scope、异常、请求次数);
2. **一个账号被挡不会终止本轮**,换下一个继续;
3. **每账号有单轮上限**,池子多一个号不等于可以对同一个号超发。
"""
from __future__ import annotations

import pytest

from app.services import weread_accounts as wa
from app.services import wechat_monitor
from app.services.cookie_store import set_cookie
from app.services.weread_budget import Blocked, SCOPE_WEB_LIST, blocked_until
from app.services.weread_client import WereadAuthError, WereadError


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


class _Fake:
    """按 cookie 决定行为的替身(`mp_articles` 是唯一吃列表额度的动作)。"""

    modes: dict[str, str] = {}
    instances: list["_Fake"] = []

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie
        self.mode = _Fake.modes.get(cookie, "ok")
        self.articles_calls: list[str] = []
        self.cover_calls: list[str] = []
        _Fake.instances.append(self)

    def mp_articles(self, book_id: str, offset: int = 0, count: int = 20) -> dict:
        self.articles_calls.append(book_id)
        if self.mode == "quota":
            raise WereadError("微信读书接口不可用/被拦截(-2041):")
        if self.mode == "auth":
            raise WereadAuthError("微信读书登录态失效(-2012):请重新扫码")
        if self.mode == "net":
            raise WereadError("ConnectionError: 网络不通")
        return {"reviews": [], "synckey": 1}

    def latest_article(self, book_id: str) -> dict:
        self.cover_calls.append(book_id)
        if self.mode == "auth":
            raise WereadAuthError("微信读书登录态失效(-2012):请重新扫码")
        return {"title": "t", "url": "https://mp.weixin.qq.com/s/x", "review_id": "r"}


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch):
    _Fake.modes = {}
    _Fake.instances = []
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: _Fake(cookie))
    yield


# ---------------- 账号发现 ----------------

def test_账号按序号排序而不是字典序(db) -> None:
    """`weread#10` 必须排在 `weread#2` 之后 —— 字典序会把它排到前面,轮转就乱了。"""
    for key in ("weread", "weread#10", "weread#2"):
        set_cookie(db, 1, key, f"ck-{key}")
    assert wa.account_keys(db, 1) == ["weread", "weread#2", "weread#10"]


def test_app_凭据不算列表账号(db) -> None:
    """`weread_app` 是另一套鉴权(accessToken),混进来会让列表用一个没 cookie 的"账号"。"""
    set_cookie(db, 1, "weread", "ck-base")
    set_cookie(db, 1, "weread_app", '{"accessToken":"a","vid":"1"}')
    assert wa.account_keys(db, 1) == ["weread"]


def test_基础账号的额度scope不许改名() -> None:
    """改名 = 历史熔断状态失联,下一轮会对着一个"以为没被挡"的账号重打一次。"""
    assert wa.web_scope("weread") == SCOPE_WEB_LIST
    assert wa.web_scope("weread#2") == f"{SCOPE_WEB_LIST}@weread#2"


# ---------------- 单账号 = 旧行为 ----------------

def test_单账号时请求与熔断口径与旧版一致(db) -> None:
    set_cookie(db, 1, "weread", "ck-base")
    pool = wa.build(db, 1)
    assert len(pool) == 1

    payload, key = pool.ask_list(db, 1, "MP_WXS_1", what="mp/articles(号A)")
    assert key == "weread" and payload["synckey"] == 1
    assert _Fake.instances[0].articles_calls == ["MP_WXS_1"]   # 只打了一发

    # 被挡之后,scope 仍然是**基础那个**(不是 `web_list@weread`)
    _Fake.instances[0].mode = "quota"
    with pytest.raises(WereadError):
        pool.ask_list(db, 1, "MP_WXS_2", what="mp/articles(号B)")
    assert blocked_until(db, 1, SCOPE_WEB_LIST) is not None


def test_登录态失效也换号_全挂才抛登录态错误(db) -> None:
    """★ 账号 A 的 cookie 过期**不代表**账号 B 不行 —— 那正是多一个号最该顶上的时刻。

    第一版实现只对**额度错**换号,结果基础账号一过期(两次续期之间会有一段空档),
    整轮就直接红,池子等于白加。判据:`ask_list` 在 `-2012` 上也要换下一个。
    """
    set_cookie(db, 1, "weread", "ck-base")
    set_cookie(db, 1, "weread#2", "ck-2")
    _Fake.modes = {"ck-base": "auth"}
    pool = wa.build(db, 1)
    _payload, key = pool.ask_list(db, 1, "MP_WXS_1")
    assert key == "weread#2", "第一枚登录态失效时必须换第二枚"

    # 两枚都登录态失效 ⇒ 抛**登录态错**(调用方据此去续期),不是别的
    _Fake.modes = {"ck-base": "auth", "ck-2": "auth"}
    pool2 = wa.build(db, 1)
    with pytest.raises(WereadAuthError):
        pool2.ask_list(db, 1, "MP_WXS_1")


def test_两个号都网络错时抛错而不是静默返回(db) -> None:
    """换号是"多试一次",不是"吞掉错误" —— 全挂必须抛,否则又是假成功。"""
    set_cookie(db, 1, "weread", "ck-base")
    set_cookie(db, 1, "weread#2", "ck-2")
    _Fake.modes = {"ck-base": "net", "ck-2": "net"}
    pool = wa.build(db, 1)
    with pytest.raises(WereadError, match="网络不通"):
        pool.ask_list(db, 1, "MP_WXS_1")
    assert all(i.articles_calls for i in _Fake.instances)   # 两枚都真试过


# ---------------- 多账号:被挡就换下一个 ----------------

def test_第一个账号被挡时换下一个继续(db) -> None:
    """★ 池子的全部意义:一个账号被 `-2041` 限掉,不该让整条链归零。"""
    set_cookie(db, 1, "weread", "ck-base")
    set_cookie(db, 1, "weread#2", "ck-2")
    _Fake.modes = {"ck-base": "quota"}
    pool = wa.build(db, 1)

    payload, key = pool.ask_list(db, 1, "MP_WXS_1", what="mp/articles(号A)")
    assert key == "weread#2" and payload is not None
    # 被挡的那一枚记在自己的 scope 上,基础账号的历史状态不受影响
    assert blocked_until(db, 1, wa.web_scope("weread")) is not None
    assert blocked_until(db, 1, wa.web_scope("weread#2")) is None


def test_被挡过的账号下一发排到后面(db) -> None:
    set_cookie(db, 1, "weread", "ck-base")
    set_cookie(db, 1, "weread#2", "ck-2")
    _Fake.modes = {"ck-base": "quota"}
    pool = wa.build(db, 1)
    pool.ask_list(db, 1, "MP_WXS_1")
    # 第二次:ck-base 已在熔断期 ⇒ 先问 ck-2(它没被挡)
    pool.ask_list(db, 1, "MP_WXS_2")
    second = [i for i in _Fake.instances if i.cookie == "ck-2"][0]
    assert second.articles_calls == ["MP_WXS_1", "MP_WXS_2"]


def test_全被挡才抛Blocked(db) -> None:
    set_cookie(db, 1, "weread", "ck-base")
    set_cookie(db, 1, "weread#2", "ck-2")
    _Fake.modes = {"ck-base": "quota", "ck-2": "quota"}
    pool = wa.build(db, 1)
    with pytest.raises(WereadError):
        pool.ask_list(db, 1, "MP_WXS_1")
    # 都进了熔断期 ⇒ 下一发**请求根本不发**(保护账号,而不是再撞一次)
    with pytest.raises(Blocked):
        pool.ask_list(db, 1, "MP_WXS_2")


def test_每账号有单轮上限(db, monkeypatch) -> None:
    """池里多一个号 ≠ 可以对同一个号超发(实测额度 29,超发只会把它也打进熔断)。"""
    monkeypatch.setattr(wa, "PER_ACCOUNT_ROUND", 2)
    set_cookie(db, 1, "weread", "ck-base")
    set_cookie(db, 1, "weread#2", "ck-2")
    pool = wa.build(db, 1)
    keys = [pool.ask_list(db, 1, f"MP_WXS_{i}")[1] for i in range(4)]
    assert keys == ["weread", "weread", "weread#2", "weread#2"]


def test_窗口宽度按账号数放大(db) -> None:
    set_cookie(db, 1, "weread", "ck-base")
    assert wa.WereadPool([("weread", "ck-base")]).window_size() == wa.PER_ACCOUNT_ROUND
    two = wa.WereadPool([("weread", "ck-base"), ("weread#2", "ck-2")])
    assert two.window_size() == wa.PER_ACCOUNT_ROUND * 2


# ---------------- cover 也要能换号 ----------------

def test_cover登录态失效时换下一个账号(db) -> None:
    """cover 不花列表额度,但它依赖账号登录态;两次续期之间的空档不该让整轮瞎掉。"""
    set_cookie(db, 1, "weread", "ck-base")
    set_cookie(db, 1, "weread#2", "ck-2")
    _Fake.modes = {"ck-base": "auth"}
    pool = wa.build(db, 1)
    assert pool.latest_article("MP_WXS_1")["title"] == "t"
    assert _Fake.instances[0].cover_calls == ["MP_WXS_1"]       # 第一枚试过了才换
    assert [i for i in _Fake.instances if i.cookie == "ck-2"][0].cover_calls == ["MP_WXS_1"]
    # ★ 第二个号:坏掉的那枚**不再重试**(cover 每号每轮都打,不记住就整轮成本翻倍)
    assert pool.latest_article("MP_WXS_2")["title"] == "t"
    assert _Fake.instances[0].cover_calls == ["MP_WXS_1"]


def test_with_base_把门面解析出的cookie放第一位(db) -> None:
    set_cookie(db, 1, "weread", "stale")
    set_cookie(db, 1, "weread#2", "ck-2")
    pool = wa.build(db, 1).with_base("fresh")
    assert pool.keys == ["weread", "weread#2"]
    assert pool.cookie_of("weread") == "fresh"


# ---------------- 源码级守卫 ----------------

def test_池子打列表也必须经统一额度入口() -> None:
    """★ 防复发:`app/services/wechat/` 之外的**新模块**同样是"另一条链"。

    `tests/test_weread_budget.py` 的守卫只扫 `app/services/wechat/*.py`;账号池是新文件,
    它自己打 `mp_articles` —— 不在守卫范围内就等于**又开了一个绕过熔断的口子**。
    ⚠️ 判据用 **AST 查真实调用**,不用子串(本仓栽过:注释里写一句就让人工守卫变红/变绿)。
    """
    import ast

    tree = ast.parse(open("app/services/weread_accounts.py", encoding="utf-8").read())
    calls_list, calls_budget = False, False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            if fn.attr == "mp_articles":
                calls_list = True
            if (fn.attr == "call" and isinstance(fn.value, ast.Name)
                    and fn.value.id == "weread_budget"):
                calls_budget = True
    assert calls_list, "池子竟然不直接打列表?调用点变了,这条守卫要跟着更新"
    assert calls_budget, "池子里打 mp_articles 却没经 weread_budget.call —— 会脱离跨路径熔断"

