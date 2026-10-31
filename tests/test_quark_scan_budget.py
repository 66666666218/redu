"""夸克清理的**扫描预算**必须能从 `.env` 调(2026-10-31)。不联网。

## 背景

`pan_auto_clean` 连续多轮报「夸克跳过(没扫完:已扫19个家,还差19个;调用2500)」——
2500 是 `quark_dup.BUDGET`,而它**只读 `os.environ`**。本项目的配置全在 `.env`
(pydantic 读,**不进** `os.environ`)⇒ 在 `.env` 里写 `QUARK_CLEANUP_BUDGET` **不生效**,
"加预算让它扫完"这个动作做了等于没做。现在优先读 settings,再回落环境变量。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import quark_dup as qd


def test_优先读settings(monkeypatch):
    class S:
        quark_cleanup_budget = 6000

    monkeypatch.setattr("config.settings.get_settings", lambda: S())
    assert qd.scan_budget() == 6000


def test_settings读不到时回落环境变量(monkeypatch):
    def boom():
        raise RuntimeError("no settings")

    monkeypatch.setattr("config.settings.get_settings", boom)
    monkeypatch.setattr(qd, "BUDGET", 4321)
    assert qd.scan_budget() == 4321


def test_settings为0时也回落(monkeypatch):
    class S:
        quark_cleanup_budget = 0

    monkeypatch.setattr("config.settings.get_settings", lambda: S())
    monkeypatch.setattr(qd, "BUDGET", 2500)
    assert qd.scan_budget() == 2500
