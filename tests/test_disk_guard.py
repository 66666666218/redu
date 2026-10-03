"""磁盘水位守卫:比例判定 / 文件系统去重 / 阈值 / 关掉开关 / **告警标题不含数字**。

最后一条是重点:标题里一旦带上"92.3%"这种数字,**每天的标题都不同**,
而 `notify_incident` 是按标题做冷却去重的 → 冷却形同虚设 → **天天响**。
这与 2026-10-03 看门狗告警"一天响 4 次变噪音、然后被无视"是同一个坑,故用测试钉死。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import User


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=1, username="a", email="a@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def test_disk_report_shape_and_range() -> None:
    from app.services import disk_guard

    rows = disk_guard.disk_report()
    assert rows, "至少应能读到项目盘"
    for r in rows:
        assert 0.0 <= r["used_ratio"] <= 1.0
        assert r["total_gb"] > 0 and r["free_gb"] >= 0
        assert r["path"]


def test_disk_report_dedupes_same_filesystem(tmp_path) -> None:
    """同一文件系统上的多个目录只报一次(按 st_dev 去重,不是按路径)。

    否则 data / data/backups / 项目根三个目录会把同一块盘报三遍 → 三条告警。
    """
    from app.services import disk_guard

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    rows = disk_guard.disk_report([a, b])
    assert len(rows) == 1, f"同盘目录未去重:{rows}"


def test_disk_report_skips_missing_path_without_faking_zero() -> None:
    """读不到的路径直接跳过,**不伪造成 0%** —— 伪造的 0% 会被读成"盘很空,没事"。"""
    from app.services import disk_guard

    assert disk_guard.disk_report([disk_guard.ROOT / "no" / "such" / "dir"]) == []


def test_tick_silent_when_below_threshold(monkeypatch) -> None:
    from app.services import disk_guard
    from config.settings import Settings

    monkeypatch.setattr(disk_guard, "disk_report",
                        lambda paths=None: [{"path": "D:\\", "total_gb": 100.0,
                                             "free_gb": 50.0, "used_ratio": 0.50}])
    assert disk_guard.disk_guard_tick(Settings(_env_file=None)) == 0


def test_tick_alerts_once_per_filesystem_when_hot(monkeypatch, session) -> None:
    from app.services import alert_service, disk_guard
    from config.settings import Settings

    monkeypatch.setattr(disk_guard, "disk_report",
                        lambda paths=None: [{"path": "D:\\", "total_gb": 100.0,
                                             "free_gb": 3.0, "used_ratio": 0.97}])
    import app.db.database as appdb
    monkeypatch.setattr(appdb, "get_session_local",
                        lambda: sessionmaker(bind=session.get_bind()))

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda db, uid, kind, title, detail, **kw: seen.append((kind, title)) or True)

    assert disk_guard.disk_guard_tick(Settings(_env_file=None)) == 1
    kind, title = seen[0]
    assert kind == "ops"                       # 运维信息 → 按口径归管理员群
    assert "严重" in title                      # 0.97 ≥ crit(0.95)


def test_alert_title_has_no_digits_so_cooldown_works(monkeypatch, session) -> None:
    """⚠️ 标题只到"哪个盘",**不带百分比** —— 带数字会让每天标题都不同、冷却门失效、天天响。

    这条是防噪音的回归守卫:改标题格式时如果顺手把 `{ratio}%` 塞回去,这里会红。
    """
    from app.services import alert_service, disk_guard
    from config.settings import Settings

    import app.db.database as appdb
    monkeypatch.setattr(appdb, "get_session_local",
                        lambda: sessionmaker(bind=session.get_bind()))

    titles: list[str] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda db, uid, kind, title, detail, **kw: titles.append(title) or True)

    # 同一块盘、两天水位不同 → 标题必须**完全一样**(冷却门才认得出来)
    for ratio in (0.86, 0.93):
        monkeypatch.setattr(disk_guard, "disk_report",
                            lambda paths=None, _r=ratio: [{"path": "D:\\", "total_gb": 100.0,
                                                           "free_gb": 10.0, "used_ratio": _r}])
        disk_guard.disk_guard_tick(Settings(_env_file=None))

    assert len(titles) == 2 and titles[0] == titles[1], f"标题随水位变化了:{titles}"
    assert not any(ch.isdigit() for ch in titles[0]), f"标题含数字,冷却门会失效:{titles[0]}"


def test_tick_respects_disabled_switch(monkeypatch) -> None:
    from app.services import disk_guard
    from config.settings import Settings

    monkeypatch.setattr(disk_guard, "disk_report",
                        lambda paths=None: [{"path": "D:\\", "total_gb": 100.0,
                                             "free_gb": 1.0, "used_ratio": 0.99}])
    assert disk_guard.disk_guard_tick(Settings(_env_file=None, disk_guard_enabled=False)) == 0


def test_tick_swallows_errors(monkeypatch) -> None:
    """守卫自身失败不能把调度器的这一轮带崩(其它作业还在同一调度器里)。"""
    from app.services import disk_guard
    from config.settings import Settings

    def _boom(paths=None):
        raise RuntimeError("模拟磁盘读取炸了")

    monkeypatch.setattr(disk_guard, "disk_report", _boom)
    assert disk_guard.disk_guard_tick(Settings(_env_file=None)) == 0
