"""出口 IP 变化自检的判据(2026-10-20,用户出差/换网络)。

核心:**变化要报、重复要防、取不到 IP 不能猜**。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import RunRecord, SystemConfig, User
from app.services import network_guard as ng


class _S:
    network_guard_enabled = True
    network_guard_ip_urls = "https://api.ipify.org"
    network_guard_failure_window_hours = 2
    network_guard_alert_cooldown_minutes = 60


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


@pytest.fixture
def alerts(monkeypatch):
    calls: list[tuple[str, str]] = []

    def _notify(db, uid, kind, title, detail, **kw):
        calls.append((title, detail))
        return True

    monkeypatch.setattr("app.services.alert_service.notify_incident", _notify)
    return calls


def _last_ip(session) -> str:
    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == ng._LAST_IP_KEY))
    return str(row.value or "") if row else ""


class TestCheck:
    def test_首次只记录不报警(self, session, alerts, monkeypatch) -> None:
        monkeypatch.setattr(ng, "current_ip", lambda s: "1.1.1.1")
        out = ng.check(session, 1, _S())
        assert out["status"] == "init" and _last_ip(session) == "1.1.1.1"
        assert alerts == [], "首次运行不该报'IP 变了'"

    def test_同一_IP_不报警(self, session, alerts, monkeypatch) -> None:
        monkeypatch.setattr(ng, "current_ip", lambda s: "1.1.1.1")
        ng.check(session, 1, _S())
        out = ng.check(session, 1, _S())
        assert out["status"] == "same" and alerts == []

    def test_IP_变化要报警并记_runs(self, session, alerts, monkeypatch) -> None:
        session.add(RunRecord(user_id=1, run_id="r1", kind="xiaohongshu",
                              status="failed", detail="300011 账号异常",
                              started_at=datetime.now()))
        session.commit()
        monkeypatch.setattr(ng, "current_ip", lambda s: "1.1.1.1")
        ng.check(session, 1, _S())
        monkeypatch.setattr(ng, "current_ip", lambda s: "2.2.2.2")
        out = ng.check(session, 1, _S())
        assert out["status"] == "changed" and out["alerted"] is True
        assert len(alerts) == 1
        title, detail = alerts[0]
        assert "IP 已变化" in title and "1.1.1.1" in detail and "2.2.2.2" in detail
        assert "xiaohongshu" in detail, "变化告警要附带近时段失败摘要"
        runs = session.scalars(select(RunRecord).where(
            RunRecord.kind == "network_guard")).all()
        assert len(runs) == 1 and "IP 变化" in runs[0].detail

    def test_变化告警要说明它会影响绑_IP_的会话(self, session, alerts, monkeypatch) -> None:
        """★ 2026-10-22:IP 漂移不是"网络小告警",而是**跨平台会话批量失效的共同原因**。

        当天真因:Windows IPv6 临时地址(RFC 4941)定期换随机后缀 + 随身 WiFi/热点切换
        ⇒ 一天出口变 10 次;微博/小红书/闲鱼/抖音/微信读书的会话都绑出口 IP
        ⇒ 同一账号在服务端看来"不停换地方登录"。
        旧文案只说"IP 变了",没说要去找谁 —— 于是我们一直在单条链上调 cookie/签名。
        """
        monkeypatch.setattr(ng, "current_ip", lambda s: "1.1.1.1")
        ng.check(session, 1, _S())
        monkeypatch.setattr(ng, "current_ip", lambda s: "2.2.2.2")
        ng.check(session, 1, _S())
        detail = alerts[0][1]
        assert "绑出口 IP" in detail, "必须点明会连带哪些平台"
        assert "微博" in detail and "小红书" in detail
        assert "临时地址" in detail, "要给出排查入口(netsh interface ipv6 show privacy)"
        assert "近 24 小时出口变化" in detail

    def test_冷却期内只记_runs_不重复推(self, session, alerts, monkeypatch) -> None:
        monkeypatch.setattr(ng, "current_ip", lambda s: "1.1.1.1")
        ng.check(session, 1, _S())
        monkeypatch.setattr(ng, "current_ip", lambda s: "2.2.2.2")
        ng.check(session, 1, _S())
        monkeypatch.setattr(ng, "current_ip", lambda s: "3.3.3.3")
        out = ng.check(session, 1, _S())
        assert out["status"] == "changed" and out["alerted"] is False
        assert len(alerts) == 1, "同一小时反复换 IP 不该刷屏"
        runs = session.scalars(select(RunRecord).where(
            RunRecord.kind == "network_guard")).all()
        assert len(runs) == 2, "变化本身必须留痕(不推 ≠ 不记)"

    def test_取不到_IP_不猜也不更新(self, session, alerts, monkeypatch) -> None:
        monkeypatch.setattr(ng, "current_ip", lambda s: "1.1.1.1")
        ng.check(session, 1, _S())
        monkeypatch.setattr(ng, "current_ip", lambda s: "")
        out = ng.check(session, 1, _S())
        assert out["status"] == "no_ip"
        assert _last_ip(session) == "1.1.1.1", "取不到 IP 时不许把上次 IP 抹掉"
        assert alerts == []

    def test_非法_IP_不认(self, monkeypatch) -> None:
        class _R:
            status_code = 200
            text = "not-an-ip"

        monkeypatch.setattr(ng.requests, "get", lambda *a, **k: _R())
        assert ng.current_ip(_S()) == ""
