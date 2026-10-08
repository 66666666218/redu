"""SQLite **写事务看门狗**测试(2026-10-08)。全部离线。

## 它守的是什么
本机是 SQLite **单写者**,而 `busy_timeout=30000` 只能让**等的人**多等 30 秒 ——
它挡不住**持有的人**开着写事务去跑几分钟慢活(网络/浏览器/LLM)。
2026-10-08 实测代价:公众号转存**在夸克那边已经存成、空间也花了**,记录却因
`_commit_now` 撞锁没落盘 ⇒ 卡片显示「⏳待转存」,下一轮还要**重存一份**。

看门狗做两件事(都只报不修):
1. 持有太久就告警,带**开它那一处**的调用栈;
2. 撞锁那一刻把"当前开着的写事务"全打出来 —— 直接点名,不用等下个窗口对时间戳猜。

## ⚠️ 这里为什么用 **ORM flush** 而不是 `conn.execute`
我第一版测试用 `conn.execute()` 直连写,栈很**浅**(离我真凶帧只隔 7 层),
于是 `traceback.format_stack(limit=14)` 的**截断 bug 在测试里完全暴露不出来**:
测试绿、而生产里 30 条诊断**一条都没点到名**(栈尾永远停在监听器自己)。

生产走的是 `session.commit() → flush → persistence → execute`,**光 SQLAlchemy 那条链
就十几层**。所以这里必须**同样走 flush**,否则这个守卫是假的。
"""
from __future__ import annotations

import logging
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db import database as dbm
from app.db.models import SystemConfig


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.records.append(record)


@pytest.fixture
def cap():
    h = _Collect()
    lg = logging.getLogger("app.db.database")
    lg.addHandler(h)
    lg.setLevel(logging.WARNING)
    yield h
    lg.removeHandler(h)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """真实文件库 + 看门狗。⚠️ 阈值调小,免得测试真等 20 秒。"""
    monkeypatch.setattr(dbm, "WRITE_TX_WARN_SEC", 0.5)
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"timeout": 2})
    dbm._install_write_tx_watchdog(eng)
    SystemConfig.__table__.create(eng)          # 只建这一张,不拖整个 metadata
    yield eng
    eng.dispose()


def _hold_write_tx_in_named_function(session: Session) -> None:
    """★ 真凶帧 —— 撞锁时那份栈里应当出现这个名字。

    ⚠️⚠️ **它不直接 `flush()`,而是走 `_commit_path` 那一层** —— 这不是装饰:

    生产里 `_enrich._commit_now` 的**第一条写语句是在 `session.commit()` 里**发出的,
    路径是 `commit → _prepare_impl → flush → persistence → execute`;
    而直接 `session.flush()` **少一层**。实测:ORM 那条链本身只有 **13 帧**,
    ⇒ 少这一层时 `format_stack(limit=14)` **刚刚好还能截到真凶帧**,测试照绿 ——
    于是 `limit=14` 那个真 bug 在测试里**完全暴露不出来**(生产里它让 30 条诊断一条都没点到名)。

    加这一层之后,真凶帧落在第 15 帧上,`limit` 只要不够就会把它切掉 —— 守卫才是真的。
    """
    session.add(SystemConfig(key="culprit", value="x", updated_at=None))
    _commit_path(session)


def _commit_path(session: Session) -> None:
    """模拟 `session.commit()` 里那段 flush(生产的第一条写就发生在这里)。"""
    _flush_inner(session)


def _flush_inner(session: Session) -> None:
    session.flush()                             # 写下去但**不提交** ⇒ 一直抱着锁


def _deep_wrapper(session: Session, depth: int) -> None:
    """再叠 20 层,模拟"作业入口 → runner → _enrich → …"那一路。"""
    if depth <= 0:
        return _hold_write_tx_in_named_function(session)
    return _deep_wrapper(session, depth - 1)


# ---------------------------------------------------------------------------
# ① 持有太久 → 告警(带调用栈)
# ---------------------------------------------------------------------------


def test_写事务持有太久要告警并带调用栈(engine, cap):
    with engine.begin() as c:
        c.execute(text("SELECT 1"))
    s = Session(bind=engine)
    try:
        s.add(SystemConfig(key="slow", value="1", updated_at=None))
        s.flush()
        time.sleep(0.8)                         # > 阈值 0.5s
        s.commit()
    finally:
        s.close()
    warn = [r for r in cap.records if "写事务**持有" in r.getMessage()]
    assert warn, "持有超过阈值却没有告警"
    assert "test_db_write_tx_watchdog" in warn[0].getMessage()


def test_快事务不该告警(engine, cap):
    """反面对照 —— 不然告警会被噪音淹掉(本仓反复讲过的"告警变噪音"教训)。"""
    for i in range(3):
        s = Session(bind=engine)
        s.add(SystemConfig(key=f"fast{i}", value="1", updated_at=None))
        s.commit()
        s.close()
    assert not [r for r in cap.records if "写事务**持有" in r.getMessage()]


# ---------------------------------------------------------------------------
# ② 撞锁那一刻 → 点名持有者
# ---------------------------------------------------------------------------


def test_撞锁要点名是谁抱着锁(engine, cap):
    """★ 核心用例:受害者报错那一瞬间快照"当前开着的写事务",栈里指到**开它的那处**。"""
    holder = Session(bind=engine)
    _deep_wrapper(holder, 20)                  # 抱着写事务不放(隔 20 层 + flush 深链)
    try:
        victim = Session(bind=engine)
        try:
            with pytest.raises(Exception):
                victim.add(SystemConfig(key="victim", value="1", updated_at=None))
                victim.commit()
        finally:
            victim.close()
    finally:
        holder.rollback()
        holder.close()

    errs = [r for r in cap.records if "撞锁" in r.getMessage()]
    assert errs, "撞锁了却没有点名"
    msg = errs[0].getMessage()
    assert "_hold_write_tx_in_named_function" in msg, (
        f"栈里没指到**开写事务那一处**(多半是 format_stack 的 limit 太小,"
        f"把调用方那几帧截掉了):\n{msg}")
    assert "持有" in msg and "s(线程" in msg, f"没报持有时长/线程:\n{msg}"


def test_没有别的持有者时要说清是别的进程(engine, cap):
    """★ **区分"我们内部"与"外部进程"**:两种的修法完全不同,含糊过去就会白查。

    用一个**独立引擎**(不在本进程的登记表里)模拟"别的进程"抱着锁。
    """
    other = create_engine(f"sqlite:///{engine.url.database}", connect_args={"timeout": 1})
    s2 = Session(bind=other)
    s2.add(SystemConfig(key="outside", value="1", updated_at=None))
    s2.flush()                                  # 外部持有者:不进本进程登记表
    try:
        with pytest.raises(Exception):
            v = Session(bind=engine)
            try:
                v.add(SystemConfig(key="v2", value="1", updated_at=None))
                v.commit()
            finally:
                v.close()
    finally:
        s2.rollback()
        s2.close()
        other.dispose()
    errs = [r for r in cap.records if "撞锁" in r.getMessage()]
    assert errs and "别的进程" in errs[0].getMessage(), (
        f"外部持有时应说清是别的进程,实际:\n{errs[0].getMessage() if errs else '(没报)'}")


# ---------------------------------------------------------------------------
# ★ 主动采样:不依赖任何错误路径
# ---------------------------------------------------------------------------


def test_主动采样_不用等撞锁也能点名(engine, cap):
    """★★ 对着 2026-10-08 那次「**撞锁却零诊断**」加的。

    原来只有两条路会说话:**持有者提交时**、**受害者报错时**。而那次两条都没响
    (原因至今没定位)⇒ 我只能看到"又撞锁了",看不到是谁,一整天都在瞎修。

    这条守护线程**不依赖任何错误路径**:每 5 秒扫一遍开着的写事务,谁超时就报一次。
    """
    holder = Session(bind=engine)         # ⚠️ 这个帮手收的是 **Session**,不是 Connection
    _deep_wrapper(holder, 3)              # 抱着写事务不放(比阈值长得多)
    try:
        time.sleep(6.5)                   # 采样线程 5 秒一轮
    finally:
        holder.rollback()
        holder.close()
    warns = [r for r in cap.records if "已经持有" in r.getMessage()]
    assert warns, "长持有的写事务没有被主动采样抓到 —— 那么再撞锁时我们又是瞎的"
    assert "_hold_write_tx_in_named_function" in warns[0].getMessage(), \
        f"采样要能点名到开事务那一处:\n{warns[0].getMessage()[:200]}"


def test_主动采样_同一个事务只报一次(engine, cap):
    """⚠️ 每 5 秒报一次会刷屏 —— 而刷屏的告警最终会被无视(本仓反复讲过的教训)。"""
    holder = Session(bind=engine)
    _deep_wrapper(holder, 3)
    try:
        time.sleep(13)                    # 跨两轮采样
    finally:
        holder.rollback()
        holder.close()
    warns = [r for r in cap.records if "已经持有" in r.getMessage()]
    assert len(warns) == 1, f"同一个事务报了 {len(warns)} 次,会刷屏"
