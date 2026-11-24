"""**我方链存活闸**(2026-11-24)。

## 它守的是什么

当天实测:`quark`(夸克1号)**整号被平台封禁** —— 名下 6596 条分享**全部**回
`41031 分享者用户封禁链接查看受限`,而账号本身是好的(配额、列目录都正常)。
近 7 天 `push_ledger` 命中它 **901 次**、公众号 `my_pan_urls` **1477 条**
⇒ **客户点开的绝大多数是死链**。

代码里早有「`41031` = 终态」的判据,但它只加在**源链**侧(`wechat/_enrich.py`);
**没有任何一处看我们自己发的链**。这组用例守的就是补上的那半边:
判据怎么算死、死链怎么拦住、账号级封禁怎么归并、探活回来怎么自愈。

## 三条纪律(用例按它们分组)

1. **只认平台原话** —— 限流/超时/我们自己的配置问题一律不算死
   (`falsification-needs-control-variables`)
2. **读库失败 = 什么也不拦** —— 这是"别把死链给客户"的旁路,不许变成
   "库一抖就整站没链"的新故障源
3. **死链不算"已有"** —— 判"要不要再搬"时也不能拿死链当证据
   (否则会跳过转存、客户一条能点的都没有)
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import (PanLinkHealth, PanOwnedShare, SystemConfig,
                           WechatArticle, WechatPanLink)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _clear_dead_cache():
    """资源库的死链快照是**进程级缓存**,不清会让用例互相污染。"""
    from app.services.resource_library import reset_dead_cache

    reset_dead_cache()
    yield
    reset_dead_cache()


# ------------------------------------------------------------------ 判据

def test_分享链身份只取路径段() -> None:
    """带 `?pwd=` / `(自分享)` / 末尾标记的都要认出**同一条分享**。

    实测库里真存过 `https://pan.quark.cn/s/xxx (自分享)` —— 拿整条 URL 当键
    会把同一条分享登记成好几个身份。
    """
    from app.services.pan_link_health import share_key_of

    assert share_key_of("https://pan.quark.cn/s/abc123") == ("quark", "abc123")
    assert share_key_of("https://pan.quark.cn/s/abc123?pwd=8888") == ("quark", "abc123")
    assert share_key_of("https://pan.quark.cn/s/abc123 (自分享)") == ("quark", "abc123")
    assert share_key_of("https://pan.baidu.com/s/1Ab-c_D") == ("baidupan", "1Ab-c_D")
    assert share_key_of("https://pan.xunlei.com/s/VP42xyz") == ("xunlei", "VP42xyz")
    #: 认不出就**返回空**(不猜);空键永远不会命中死亡集合
    assert share_key_of("https://example.com/x") == ("", "")
    assert share_key_of("") == ("", "")


def test_账号级封禁的措辞() -> None:
    from app.services.pan_link_health import is_account_ban

    assert is_account_ban("夸克接口失败(41031): 分享者用户封禁链接查看受限")
    assert is_account_ban("get_share_user_banned")
    assert not is_account_ban("夸克接口失败(41011): 分享地址已失效")
    assert not is_account_ban("")


def test_只有平台原话才算终态死() -> None:
    """★ 3 条纪律的第 1 条:限流/超时/我们自己的配置问题**一律不算死**。

    拿"我们没看见"当"链坏了"会把好链判死(`absence-of-evidence-is-not-evidence-of-absence`)。
    """
    from app.services.pan_link_health import is_terminal_dead

    #: 平台明确表态 ⇒ 死
    for err in ("41031 分享者用户封禁链接查看受限", "41011 分享地址已失效",
                "share_status=EXPIRED", "SENSITIVE_RESOURCE", "分享已被删除"):
        assert is_terminal_dead(err), err
    #: 限流 / 超时 / 我们自己的问题 ⇒ **不算**(留活,等下一轮)
    for err in ("", "请求过于频繁(429)", "timeout", "解析不出 share_id",
                "未配置百度凭据", "网络错误"):
        assert not is_terminal_dead(err), err


# ------------------------------------------------------------------ 登记与自愈

def test_登记死链后快照能判死_复活后自愈(session) -> None:
    from app.services import pan_link_health as plh

    url = "https://pan.quark.cn/s/abc123"
    assert not plh.DeadLinks(session, 1).is_dead(url)

    assert plh.mark_dead(session, 1, url, "41031 分享者用户封禁", commit=True)
    assert plh.DeadLinks(session, 1).is_dead(url)
    #: 带提取码/标记的同一分享也要判死(身份取路径段)
    assert plh.DeadLinks(session, 1).is_dead(url + " (自分享)")
    #: 别的链不受影响
    assert not plh.DeadLinks(session, 1).is_dead("https://pan.quark.cn/s/other")

    #: 探活回来 ⇒ 自愈(申诉解封/重建分享后不必手工改库)
    assert plh.mark_alive(session, 1, url, commit=True)
    assert not plh.DeadLinks(session, 1).is_dead(url)


def test_自愈不给健康链新建行(session) -> None:
    """健康链是绝大多数 —— `mark_alive` 只清标记,不凭空写行(否则表白撑大)。"""
    from app.services import pan_link_health as plh

    before = session.query(PanLinkHealth).count()
    assert plh.mark_alive(session, 1, "https://pan.quark.cn/s/healthy", commit=True) is False
    assert session.query(PanLinkHealth).count() == before


def test_认不出的链不登记(session) -> None:
    from app.services import pan_link_health as plh

    assert plh.mark_dead(session, 1, "https://example.com/x", "whatever", commit=True) is False
    assert session.query(PanLinkHealth).count() == 0


# ------------------------------------------------------------------ 账号级封禁

def _pool(session, platform: str, items: list[dict]) -> None:
    import json

    session.add(SystemConfig(key="pan_account_pool",
                             value=json.dumps({platform: items}, ensure_ascii=False)))
    session.commit()


def test_停用账号名下的分享全算死_新分享也自动覆盖(session) -> None:
    """★★ 6596 条逐条登记既慢又漏 —— 归并查询让新分享**自动**算死。"""
    from app.services import pan_accounts as pa
    from app.services import pan_link_health as plh

    _pool(session, "quark", [
        {"label": "夸克1", "cookie_key": "quark", "user_id": 1},
        {"label": "夸克2", "cookie_key": "quark#2", "user_id": 1},
    ])
    #: 封禁**之前**建的分享(没有死亡登记行)
    session.add(PanOwnedShare(user_id=1, platform="quark", account_key="quark",
                              pwd_id="olddead"))
    session.add(PanOwnedShare(user_id=1, platform="quark", account_key="quark#2",
                              pwd_id="goodone"))
    session.add(PanOwnedShare(user_id=1, platform="quark", account_key="quark",
                              pwd_id="brandnew"))     # 封禁后新增的,同样该算死
    session.commit()

    assert not plh.DeadLinks(session, 1).is_dead("https://pan.quark.cn/s/olddead")

    assert pa.disable_account(session, "quark", "quark", why="链接体检:41031", user_id=1)

    dead = plh.DeadLinks(session, 1)
    assert dead.is_dead("https://pan.quark.cn/s/olddead")
    assert dead.is_dead("https://pan.quark.cn/s/brandnew"), "新分享必须自动算死"
    assert not dead.is_dead("https://pan.quark.cn/s/goodone"), "同盘的另一个号不受连累"


def test_停用的账号不再被选去转存_但全停用时仍原样返回(session) -> None:
    """★ 停用 = **不选它转存**,不是"池里没有它"。

    全停用时若返回空,调用方会把「账号被封」误诊成「没配账号」—— 这两种故障修法完全不同。
    """
    from app.services import pan_accounts as pa

    _pool(session, "quark", [
        {"label": "夸克1", "cookie_key": "quark", "user_id": 1, "last_ratio": 0.1},
        {"label": "夸克2", "cookie_key": "quark#2", "user_id": 1, "last_ratio": 0.9},
    ])
    assert [r.cookie_key for r in pa.ordered(session, "quark")] == ["quark", "quark#2"]

    assert pa.disable_account(session, "quark", "quark", why="41031", user_id=1)
    assert [r.cookie_key for r in pa.ordered(session, "quark")] == ["quark#2", "quark"]

    assert pa.disable_account(session, "quark", "quark#2", why="41031", user_id=1)
    #: 全停用 ⇒ `ordered` 两行都在(排最后但没消失)
    assert len(pa.ordered(session, "quark")) == 2
    assert pa.disabled_account_keys(session, "quark") == {"quark", "quark#2"}

    #: 恢复之后重新可用
    assert pa.enable_account(session, "quark", "quark", user_id=1)
    assert pa.disabled_account_keys(session, "quark") == {"quark#2"}


def test_没配池时拒绝停用_也不凭空写池(session) -> None:
    """单账号部署时池子是空的;凭空写一条"配置"会让人以为池配过了。

    ⚠️ 判据是「池里有没有这一条」而不是「key 像不像默认账号」—— 见下面那条用例。
    """
    from app.services import pan_accounts as pa

    assert pa.disable_account(session, "quark", "quark", why="41031") is False
    assert pa.disabled_account_keys(session, "quark") == set()
    assert session.query(SystemConfig).filter_by(key="pan_account_pool").count() == 0

    #: 池里配了、但这个 key 不在里面 ⇒ 同样拒绝(不新建条目)
    _pool(session, "quark", [{"label": "夸克2", "cookie_key": "quark#2", "user_id": 1}])
    assert pa.disable_account(session, "quark", "typo#9", why="41031") is False
    assert pa.disabled_account_keys(session, "quark") == set()


def test_key恰好等于平台名也能停用(session) -> None:
    """★★ **2026-11-24 本机的真实形状**:被封的那个号 `cookie_key` 就叫 `quark`。

    第一版守卫写的是 `cookie_key == default_cookie_key(platform)` ⇒ **拒绝停用**,
    而那正是这条护栏唯一的用武之地(6596 条分享全被封的就是它)。
    ⇒ 判据改成「池里有没有这一条」。
    """
    from app.services import pan_accounts as pa

    _pool(session, "quark", [{"label": "夸克1", "cookie_key": "quark", "user_id": 1}])
    assert pa.disable_account(session, "quark", "quark", why="链接体检:41031",
                              user_id=1) is True
    assert pa.disabled_account_keys(session, "quark") == {"quark"}


# ------------------------------------------------------------------ 出链口

def _mk_article(session, title: str, my_blob: str, pan_url: str) -> WechatArticle:
    a = WechatArticle(user_id=1, title=title, author="号A",
                      url=f"https://mp.weixin.qq.com/s/{abs(hash(title)) % 10000}",
                      pan_urls=pan_url, my_pan_urls=my_blob,
                      created_at=datetime.now() - timedelta(days=1))
    session.add(a)
    session.flush()
    session.add(WechatPanLink(user_id=1, article_id=a.id, pan_url=pan_url))
    session.commit()
    return a


def test_资源库出链口不给死链(session) -> None:
    """★★ 最关键的一条:死链**不算"我们已有"**。

    回一条打不开的链比回"没有"更糟 —— 调用方会因此**跳过转存**,
    客户最终一条能点的都没有。
    """
    from app.services import pan_link_health as plh
    from app.services.resource_library import search_resources

    dead_url = "https://pan.quark.cn/s/deadone"
    live_url = "https://pan.quark.cn/s/liveone"
    _mk_article(session, "考公资料合集", dead_url + "\n" + live_url,
                "https://pan.quark.cn/s/src1")
    plh.mark_dead(session, 1, dead_url, "41031 分享者用户封禁", commit=True)

    rows = search_resources(session, 1, "考公资料合集")
    assert rows, "资源本身还在,条目必须留下"
    assert rows[0]["my_link"] == live_url, "死链要跳过,活链照给"

    #: 全部死掉 ⇒ my_link 为空(**不是**回一条死链)
    plh.mark_dead(session, 1, live_url, "41031 分享者用户封禁", commit=True)
    from app.services.resource_library import reset_dead_cache

    reset_dead_cache()
    assert search_resources(session, 1, "考公资料合集")[0]["my_link"] == ""


def test_已判死的链不算已有_要照常转存(session) -> None:
    """`already_have` 的决定是"**要不要再搬**";拿死链当证据会让人不再搬。"""
    from app.services import pan_link_health as plh
    from app.services.pan_discovery import already_have
    from app.services.resource_library import reset_dead_cache

    dead_url = "https://pan.quark.cn/s/deadone"
    _mk_article(session, "麦穗与羊全集", dead_url, "https://pan.quark.cn/s/src2")
    plh.mark_dead(session, 1, dead_url, "41031 分享者用户封禁", commit=True)
    reset_dead_cache()

    assert already_have(session, 1, "麦穗与羊全集") is None


def test_台账快照失败时什么也不拦(monkeypatch, session) -> None:
    """★ 3 条纪律的第 2 条:判据坏了 ≠ 整站没有链接。"""
    from app.services import pan_link_health as plh
    from app.services.resource_library import dead_links

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(plh, "dead_share_keys", boom)
    snap = dead_links(session, 1, fresh=True)
    assert not snap, "读不到就当没有"
    assert snap.is_dead("https://pan.quark.cn/s/anything") is False


def test_推送渲染跳过死链继续往后找(session) -> None:
    """`_live_my_entry`:一条死了就往下找,全死才返回空(调用方退源链/原文)。"""
    from app.services import pan_link_health as plh
    from app.services.wechat._listen import _live_my_entry

    dead_url = "https://pan.quark.cn/s/deadone"
    live_url = "https://pan.quark.cn/s/liveone"
    blob = f"{dead_url} (提取码 aaaa)\n{live_url} (提取码 bbbb) [百度]"
    plh.mark_dead(session, 1, dead_url, "41031", commit=True)
    dead = plh.DeadLinks(session, 1)

    #: 死链跳过,**提取码跟着活链一起给**(不能只回 URL 再解析 —— 码会丢)
    assert _live_my_entry(None, blob, dead) == (live_url, "bbbb")
    #: 本轮新转存的链优先于历史
    assert _live_my_entry([("src", "https://pan.quark.cn/s/fresh", "cccc")],
                          blob, dead) == ("https://pan.quark.cn/s/fresh", "cccc")
    #: 全死 ⇒ 空(不是"随便给一条")
    plh.mark_dead(session, 1, live_url, "41031", commit=True)
    assert _live_my_entry(None, blob, plh.DeadLinks(session, 1)) == ("", "")