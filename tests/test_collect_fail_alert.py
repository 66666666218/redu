"""采集失败告警(`check_collect_failures`)的回归测试(2026-10-04)。

**这条 bug 是"作业心跳"上线十分钟后抓到的**:心跳里赫然一行
`collect_failed_alert: 跑了 1 次 / 错 1 次` + `UnmappedInstanceError:
Class 'builtins.NoneType' is not mapped` —— 也就是**"采集失败告警"这条链根本没在工作**。

根因:恢复确认那段(**最常见**的分支:有恢复、当前没坏)里,`db.delete(row)` 用的是
**上一个扫描循环遗留的 `row`**。最后一对恰好没有告警行时它是 `None`,`db.delete(None)`
当场抛 `UnmappedInstanceError`,整个作业崩掉;多对恢复时还会反复删同一行。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: F401,E402
from app.db.models import FeishuAlert, RunRecord, User  # noqa: E402
from app.services import alert_service  # noqa: E402
from config.settings import Settings  # noqa: E402


@pytest.fixture()
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


@pytest.fixture()
def stub_send(monkeypatch):
    """别真发飞书,记下发了什么。

    ⚠️ `check_collect_failures` 是**函数内**导入 `FeishuClient` 的
    (`from app.services.feishu_client import ...`),所以补丁要打在**那个模块**上,
    打在 `alert_service` 上会 AttributeError(实测)。
    """
    sent: list[str] = []

    class _C:
        def __init__(self, *a, **k) -> None: ...
        def send(self, msg, *a, **k):
            sent.append(str(msg))
            return True

    monkeypatch.setattr("app.services.feishu_client.FeishuClient", _C)
    return sent


def _s() -> Settings:
    return Settings(_env_file=None, feishu_webhook="https://example.com/hook",
                    feishu_alert_cooldown_hours=6)


def _runs(session, kind: str, n_failed: int, last_ok: bool = True) -> None:
    """造 n 条 failed + 可选一条"最近成功"(让该板块**当前不算坏**,从而走进恢复分支)。"""
    from datetime import datetime, timedelta
    base = datetime.now() - timedelta(hours=3)
    for i in range(n_failed):
        session.add(RunRecord(user_id=1, run_id=f"{kind}-f{i}", kind=kind, status="failed",
                              detail="boom", started_at=base + timedelta(minutes=i)))
    if last_ok:
        session.add(RunRecord(user_id=1, run_id=f"{kind}-ok", kind=kind, status="success",
                              detail="ok", started_at=datetime.now()))
    session.commit()


def test_recovery_path_does_not_crash_on_trailing_pair_without_alert(
        session, stub_send, monkeypatch) -> None:
    """⚠️ **核心回归**:扫描顺序里**最后一对没有告警行**时,恢复分支不得崩。

    构造:两个板块都"曾有失败、现在已好"(→ 都进入待扫描集合),但只有 `aaa` 有告警行,
    `zzz` 没有 —— 排序后 `zzz` 在最后,于是**遗留变量 `row` 是 `None`**。
    旧代码在这一步 `db.delete(None)` → `UnmappedInstanceError` → 整个作业挂掉。
    """
    _runs(session, "aaa", 3)
    _runs(session, "zzz", 2)
    session.add(FeishuAlert(section="collect_fail", user_id=1, title="aaa", reason="近24h失败3次"))
    session.commit()

    n = alert_service.check_collect_failures(_s(), session)   # 旧代码:这里抛 UnmappedInstanceError

    assert n == 0, "当前没有板块在坏,不该发'持续失败'告警"
    assert any("已恢复" in m for m in stub_send), f"恢复确认没发出去:{stub_send}"
    # **该清的那行被清掉**(而不是清错行/没清)
    assert session.scalars(select(FeishuAlert).where(FeishuAlert.title == "aaa")).all() == []


def test_two_recoveries_each_delete_its_own_row(session, stub_send) -> None:
    """两对同时恢复时,各自清各自的行 —— 旧代码会**反复删同一行**(遗漏另一行)。"""
    _runs(session, "aaa", 3)
    _runs(session, "bbb", 3)
    session.add(FeishuAlert(section="collect_fail", user_id=1, title="aaa", reason="x"))
    session.add(FeishuAlert(section="collect_fail", user_id=1, title="bbb", reason="y"))
    session.commit()

    alert_service.check_collect_failures(_s(), session)
    left = session.scalars(select(FeishuAlert).where(FeishuAlert.section == "collect_fail")).all()
    assert left == [], f"还有没清掉的恢复告警:{[(r.title) for r in left]}"
