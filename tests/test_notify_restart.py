"""看门狗失联告警单测(2026-10-03)。

**它守的是全项目唯一"进程死即全盲"的缺口**:所有告警都跑在应用进程内,应用一死告警也死
(`doc/operations.md` 自己承认)。看门狗是进程外唯一还活着的东西 —— 所以"应用挂了"只能由它报。
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "win" / "notify_restart.py"


def _load():
    spec = importlib.util.spec_from_file_location("notify_restart_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_exists_and_composes_outage_message(tmp_path, monkeypatch) -> None:
    """告警正文要**说清判据与为什么由看门狗发**,而不是只丢一句"应用挂了"。"""
    mod = _load()
    # ⚠️ **ROOT 也要隔离**:只看 LOG 的话,脚本会读到**真实的** `data/last_shutdown.txt`
    # (计划内停机标记)从而走"跳过"分支 —— 测试就跟着机器状态飘(2026-10-03 实测撞到)。
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "LOG", tmp_path / "app.log")
    monkeypatch.setattr("sys.argv", ["notify_restart.py", "--dry-run"])
    assert mod.main() == 0
    text = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "dry-run" in text and "未发送" in text       # dry-run **不发送**


def test_log_falls_back_when_applog_is_not_writable(tmp_path, monkeypatch) -> None:
    """⚠️ **`data/app.log` 被服务占着时追加会失败**(Windows 实测 `PermissionError: [Errno 13]`)。

    而"应用挂了"恰恰是这个脚本最该留下痕迹的时刻 —— 所以它必须能**退到独立文件**,
    否则最需要记录的那一刻反而一条日志都没有。
    """
    mod = _load()
    blocked = tmp_path / "blocked"                      # 指向**目录**:open('a') 必失败
    blocked.mkdir()
    monkeypatch.setattr(mod, "LOG", blocked)
    mod._log("落盘测试")
    fallback = tmp_path / "notify_restart.log"
    assert fallback.exists() and "落盘测试" in fallback.read_text(encoding="utf-8")


def test_script_never_raises_on_bad_config(monkeypatch) -> None:
    """告警失败**绝不能耽误重启** —— 任何异常都要被吞掉、返回 0。"""
    mod = _load()
    monkeypatch.setattr("sys.argv", ["notify_restart.py"])

    def _boom(*a, **k):
        raise RuntimeError("settings 炸了")

    monkeypatch.setattr(mod, "_log", lambda *_a, **_k: None)
    import config.settings as cs

    monkeypatch.setattr(cs, "get_settings", _boom)
    assert mod.main() == 0                              # 不抛


def test_skips_alert_after_planned_shutdown(tmp_path, monkeypatch) -> None:
    """**计划内停机不报警**(2026-10-03 实测需求)。

    看门狗只知道"健康检查不通",分不清是人关的还是它崩的 —— 上线当天就响了 4 次,
    大部分是开发时主动重启触发的。照这样下去告警会变成噪音然后被无视,**那就白做了**。
    所以应用优雅关停时写 `last_shutdown.txt`,刚关过就跳过告警。
    """
    mod = _load()
    data = tmp_path / "data"
    data.mkdir()
    (data / "last_shutdown.txt").write_text("2026-10-03T16:27:00", encoding="utf-8")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "LOG", data / "app.log")
    monkeypatch.setattr("sys.argv", ["notify_restart.py", "--dry-run"])
    assert mod.main() == 0
    text = (data / "app.log").read_text(encoding="utf-8")
    assert "优雅关停" in text and "跳过" in text


def test_alerts_when_no_shutdown_marker(tmp_path, monkeypatch) -> None:
    """**没有标记 = 崩溃** → 正常走告警(别把修复做成一刀切,那真崩了就没人知道)。"""
    mod = _load()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "LOG", data / "app.log")
    monkeypatch.setattr("sys.argv", ["notify_restart.py", "--dry-run"])
    assert mod.main() == 0
    text = (data / "app.log").read_text(encoding="utf-8")
    assert "dry-run" in text and "跳过" not in text
