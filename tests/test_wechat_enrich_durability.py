"""转存结果**当场落盘**单测(2026-10-06)。

## 事故(实测证据)
`14:01:49` 四条「盘链复用(免重复转存)」成功落日志,之后那一轮**再没有任何日志**;
`14:29:31` 服务重启 ⇒ 库里那几行的 `my_pan_urls` **全是空的**,卡片只能显示「⏳待转存」,
而下一轮还会把同一批资源**重转一遍**(多建一份分享、多占一份空间)。
**钱花了,账没了。**

## ⚠️ 写这个测试时先踩了自己一脚(值得记下来)
第一版是"跑完 `_enrich_new_articles` 再 rollback" —— **它是假绿的**:抽掉修复照样通过。
原因:函数内部(`_enrich.py` 第 503 行)**本来就有一个 commit**(注释里记着 10-05 的同类事故),
所以只要函数能跑到底,结果一定会落库。真正的故障是**中途被杀**:
那一轮死在夸克段之后、第 503 行之前(后面还有百度段的网络转存),**根本没走到那个 commit**。

⇒ 所以这个测试必须**在函数中途模拟进程死亡**,只验"已完成的转存不依赖后续代码跑不跑得到"。
**做实验要控制变量:抽掉修复必须变红**,否则测试只是装饰。
"""
import os
import sys

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.models import WechatArticle  # noqa: E402
from app.services import wechat_monitor  # noqa: E402   # 先导门面,否则循环导入
from app.services.cookie_store import set_cookie  # noqa: E402
from config.settings import Settings  # noqa: E402

EN = sys.modules["app.services.wechat._enrich"]

QUARK = "https://pan.quark.cn/s/abc123"


@pytest.fixture
def engine():
    # ⚠️ StaticPool:两个会话要看到**同一个**内存库,才能验"落盘了没有"。
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    set_cookie(db, 1, "quark", "CK")
    set_cookie(db, 1, "baidupan", "BDUSS=fake")
    db.commit()
    yield db
    db.close()


def _settings(**kw) -> Settings:
    """⚠️ 用**真的 Settings**而不是手工桩 —— 后处理还会用到共振等一串配置项,
    桩会一个个漏(实测:先缺 `quark_fid_store`、再缺 `wechat_resonance_hours`)。"""
    base = {"pan_transfer_enabled": True, "quark_cookie": "CK",
            "quark_fid_store": "", "pan_transfer_backfill_limit": 8}
    base.update(kw)
    return Settings(_env_file=None, is_dev=True, **base)


class _FakeQT:
    """假夸克客户端:给一条新的我方链。"""

    calls: list = []

    def __init__(self, *a, **k) -> None:
        pass

    def transfer_and_share(self, url, **k):
        _FakeQT.calls.append(url)
        return {"share_url": f"https://pan.quark.cn/s/OUR{len(_FakeQT.calls)}", "password": ""}


class _KilledProcess:
    """**模拟这一轮在百度段中途被杀/重启**:抛 `BaseException` 子类,绕过那些
    `except Exception` 兜底 —— 就像进程真的没了,后面的代码一行都不会跑。

    这正是 2026-10-06 14:00 那一轮的形态:夸克段 `14:01:49` 已成功,
    但那一轮**再没产出任何日志**,也就**没走到第 503 行那个 commit**。
    """

    def __init__(self, *a, **k) -> None:
        pass

    def transfer_and_share(self, url, **k):
        raise KeyboardInterrupt("模拟进程在这一步被杀(重启/卡死后被 watchdog 拉起)")


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    _FakeQT.calls = []
    monkeypatch.setattr(EN, "QuarkTransfer", _FakeQT)
    monkeypatch.setattr(EN, "_relink_notify", lambda *a, **k: None)   # 别真推飞书
    import app.services.baidupan_transfer as bp
    monkeypatch.setattr(bp, "BaiduPanClient", _KilledProcess)


def _art(session, url: str, title: str) -> WechatArticle:
    r = WechatArticle(user_id=1, author="某号", title=title,
                      url=f"https://mp.weixin.qq.com/s/{title}",
                      pan_types="夸克网盘", pan_urls=url, my_pan_urls="")
    session.add(r)
    session.commit()
    return r


def _run_until_killed(session, rows) -> None:
    """跑到"百度段被杀"为止;那之后的代码一行都不会执行。

    ⚠️ 百度段只在**存在百度链**时才进 —— 所以每个用例都带一条百度链,
    否则根本走不到"被杀"那一步(那样测的就是通关路径,回到假绿)。
    """
    rows = list(rows) + [_art(session, "https://pan.baidu.com/s/1sAwxOe7", "陪跑百度链")]
    with pytest.raises(KeyboardInterrupt):
        EN._enrich_new_articles(session, 1, _settings(), rows, run_backfill=False)


def _my_link(engine, aid: int) -> str:
    with sessionmaker(bind=engine)() as fresh:
        return str(fresh.scalar(select(WechatArticle.my_pan_urls)
                                .where(WechatArticle.id == aid)) or "")


class TestTransferResultIsDurable:
    def test_中途被杀_已完成的转存也必须还在(self, session, engine) -> None:
        """★ **核心断言**:夸克段已完成的结果,不能因为后面那段把进程弄死了就消失。"""
        r = _art(session, QUARK, "甲")
        _run_until_killed(session, [r])
        assert _FakeQT.calls == [QUARK], "前提:夸克那段确实转成功了"

        # ⚠️ 模拟进程死亡:这一轮的会话被丢弃(14:29 那次重启)
        session.rollback()

        got = _my_link(engine, r.id)
        assert got, ("转存已经**真花了**(文件进盘、分享已建),记录却随那一轮没了 —— "
                     "卡片会显示「⏳待转存」,下一轮还会把同一资源重转一遍。"
                     "修法:转存成功后**立刻** `_commit_now`,别挂在函数后面那次 commit 上。")

    def test_多条转存各自落盘(self, session, engine) -> None:
        """一条的结果不能挂在另一条的存亡上。"""
        a = _art(session, QUARK, "甲")
        b = _art(session, "https://pan.quark.cn/s/def456", "乙")
        _run_until_killed(session, [a, b])
        session.rollback()
        assert _my_link(engine, a.id), "甲的我方链丢了"
        assert _my_link(engine, b.id), "乙的我方链丢了"


class TestFailureStaysQueued:
    """反向:失败的**不能**写出 `my_pan_urls`,否则补转存队列永远轮不到它。"""

    def test_真失败留空好让下轮重试(self, session, engine, monkeypatch) -> None:
        from app.services.quark_transfer import QuarkError

        class _Bad(_FakeQT):
            def transfer_and_share(self, url, **k):
                raise QuarkError("夸克接口失败(41006): 分享不存在")
        monkeypatch.setattr(EN, "QuarkTransfer", _Bad)

        r = _art(session, QUARK, "丙")
        _run_until_killed(session, [r])
        session.rollback()
        assert not _my_link(engine, r.id).strip(), "真失败要留空,好让下轮重试"

    def test_源永久被封要落标记出队(self, session, engine, monkeypatch) -> None:
        """41031=对方分享已被封,重试不可能成功 ⇒ 必须落标记出队,否则永久霸占队头。"""
        from app.services.quark_transfer import QuarkError

        class _Dead(_FakeQT):
            def transfer_and_share(self, url, **k):
                raise QuarkError("夸克接口失败(41031): 分享已失效")
        monkeypatch.setattr(EN, "QuarkTransfer", _Dead)

        r = _art(session, QUARK, "丁")
        _run_until_killed(session, [r])
        session.rollback()
        assert "41031" in _my_link(engine, r.id), "源永久失效的必须落标记出队"


def test_做后处理之前必须先落盘() -> None:
    """★ **源码级守卫**:`_enrich_new_articles` 之前必须有一次 `session.commit()`。

    ⚠️ 为什么值得钉(2026-10-07 实测代价,很贵):后处理原来跑在**同一个未提交事务**里,
    会话一旦进了失败状态(某处吞了异常没回滚),**`savepoint` 自己就抛
    `PendingRollbackError`** ⇒ **整段后处理全废**。实测 20:08 那轮:210 个号、
    70 篇新文、**转存一条没做** ⇒ 卡片全是「⏳待转存」、阅读数全「—」,
    **而这一轮记的是 success**。

    先 commit 才敢在会话坏掉时回滚 —— 也就是说"**先落盘**"是"**坏了还能接着做**"的前提。
    这条不变式一破,那个故障模式就原样回来(而且它长得跟"今天没东西可搬"一模一样)。
    """
    import inspect

    from app.services.wechat import _listen as L

    src = inspect.getsource(L._listen_round)
    i_enrich = src.index("_enrich_new_articles")
    before = src[:i_enrich]
    assert "session.commit()" in before, (
        "`_enrich_new_articles` 之前没有 commit —— 会话一坏,整段后处理会陪葬")
    assert "is_active" in before, (
        "缺「会话已进失败状态就先回滚」那一步 —— 坏掉的会话会让 savepoint 自己抛错")


# ---------------------------------------------------------------------------
# ★ 插完文章**必须结束写事务**(看门狗点名查出来的,2026-10-08)
# ---------------------------------------------------------------------------


def test_插入新文章之后不许留着未提交的写事务(session):
    """★★ **看门狗实测**点名:监听轮**按号循环**调 `_insert_new_articles`,
    第一个号就在里面 `flush()` 把写锁拿住,一直抱到**整轮末尾**才提交。

    抓到的那条是「SQLite 写事务**持有 208.5s** 才 commit」,栈是:

        wechat_collect_tick → run_wechat_listen → _listen_round
          → _weread_collect → _insert_new_articles → (flush)

    SQLite 单写者 + 别人 `busy_timeout` 只有 30 秒 ⇒ 这 208 秒里
    「作业心跳写入」「转存结果落盘」「轮转计数」**成批失败** ——
    卡片上的「⏳待转存」和 04:02 / 08:06 / 13:13 那几批 `database is locked` 都出在这个窗口里。

    ⚠️ 判据用 **`session.in_transaction()`**,不去匹配源码 —— 重构了照样有效。
    """
    from datetime import datetime

    from app.db.models import WechatBenchmark

    b = WechatBenchmark(user_id=1, nickname="号A")
    session.add(b)
    session.commit()
    wechat_monitor._insert_new_articles(
        session, 1, b,
        [{"title": "新资源", "url": "https://mp.weixin.qq.com/s/x",
          "publish_at": datetime(2026, 10, 4)}],
        source="listen", require_pan=False)
    assert not session.in_transaction(), (
        "插完文章还留着**未提交的写事务** ⇒ 写锁会被抱到整轮末尾(实测 208 秒),"
        "别人的心跳/转存落盘会成批撞锁")
