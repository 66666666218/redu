"""看门狗失联告警单测(2026-10-03)。

**它守的是全项目唯一"进程死即全盲"的缺口**:所有告警都跑在应用进程内,应用一死告警也死
(`doc/operations.md` 自己承认)。看门狗是进程外唯一还活着的东西 —— 所以"应用挂了"只能由它报。

⚠️ **但它必须能分辨"应用崩了"与"机器重启把应用带走了"**(2026-10-13 实测补):那晚 18:03:46
用户点了重启、18:04:35 开机、18:05:55 看门狗就推了一条 🔴「应用进程失联」—— **假红灯**。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "win" / "notify_restart.py"


def _load(uptime: float | None = 99_999.0):
    """加载脚本,并**显式钉死「机器开机时长」**。

    ⚠️ **不钉就是测试跟着机器状态飘**(2026-10-03 撞过同类:脚本读到真实的
    `data/last_shutdown.txt` 导致用例时红时绿):这台机器**刚重启完**时(<
    `BOOT_GRACE_SECONDS`),所有"应当告警"的用例都会走进"机器刚开机 ⇒ 跳过"分支而变红。
    真实探测由 `test_machine_uptime_is_plausible_on_windows` 单独量。
    """
    spec = importlib.util.spec_from_file_location("notify_restart_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.machine_uptime = lambda: uptime       # type: ignore[assignment]
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
    """**没有标记 = 崩溃** → 正常走告警(别把修复做成一刀切,那真崩了就没人知道了)。"""
    mod = _load()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "LOG", data / "app.log")
    monkeypatch.setattr("sys.argv", ["notify_restart.py", "--dry-run"])
    assert mod.main() == 0
    text = (data / "app.log").read_text(encoding="utf-8")
    assert "dry-run" in text and "跳过" not in text


def test_skips_alert_when_machine_just_booted(tmp_path, monkeypatch) -> None:
    """**机器刚开机 ⇒ 应用"失联"不是崩溃**(2026-10-13 实测那条假红灯)。

    实测链条:18:03:46 用户点重启(事件 id=1074,原因「其他(计划外)」)→ 18:04:35 开机
    → 18:05:55 登录时看门狗发现 `/healthz` 还没起来 ⇒ 推 🔴「应用进程失联」。

    ⚠️ 这条**盖不住的那一层**:`last_shutdown.txt` 要应用自己写,而 **Windows 关机不走
    lifespan**(实测该文件停在两天前)—— 所以必须**另立**一个与应用无关的判据。
    """
    mod = _load(uptime=120.0)               # 开机才 2 分钟
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "LOG", data / "app.log")
    # ⚠️ 仍用 --dry-run:万一这层守卫将来被改坏,**测试绝不能真往管理员群发假红灯**。
    # 判据改成"没走到组装那一步"(下面断言 `组装完成 not in`),同样能把回归钉住。
    monkeypatch.setattr("sys.argv", ["notify_restart.py", "--dry-run"])
    assert mod.main() == 0
    text = (data / "app.log").read_text(encoding="utf-8")
    assert "跳过" in text and "才开机" in text
    assert "组装完成" not in text           # ← 关键:在组消息之前就返回了


def test_boot_grace_must_not_swallow_a_real_crash(tmp_path, monkeypatch) -> None:
    """⚠️ **探测失效时照旧告警** —— 这层保护只用来**少报假红**,不能变成"漏报真崩溃"。

    ("告警没响 = 假成功"是本仓最忌讳的一类,所以宁可多报。)
    """
    mod = _load(uptime=None)                # 模拟拿不到开机时长
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "LOG", data / "app.log")
    monkeypatch.setattr("sys.argv", ["notify_restart.py", "--dry-run"])
    assert mod.main() == 0
    text = (data / "app.log").read_text(encoding="utf-8")
    assert "dry-run" in text and "跳过" not in text


def test_machine_uptime_is_plausible_on_windows() -> None:
    """**真实量一次**开机时长(本仓惯例:这条不是模拟,是实测)。

    量不到就等于这层保护不存在 —— 那种情况下 18:05:55 那条假红灯会原样复发。
    """
    spec = importlib.util.spec_from_file_location("notify_restart_raw", SCRIPT)
    raw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(raw)            # 不经 _load():要的就是**没被钉死**的那个
    up = raw.machine_uptime()
    if sys.platform != "win32":
        assert up is None, "非 Windows 不该去调 Win32 API"
        return
    assert up is not None and up > 0, f"Windows 上必须拿得到开机时长,实得 {up!r}"


def test_machine_uptime_returns_none_off_windows(monkeypatch) -> None:
    """非 Windows 直接返回 None(本脚本只在 Windows 上跑,但不该因此炸)。"""
    spec = importlib.util.spec_from_file_location("notify_restart_offwin", SCRIPT)
    raw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(raw)
    monkeypatch.setattr(sys, "platform", "linux")
    assert raw.machine_uptime() is None


def _run_capturing_text(mod, tmp_path, monkeypatch) -> str:
    """跑一次 `main()`,把**真正要发出去的那段正文**截回来。

    ⚠️ 打桩点选在 `FeishuClient`(**保证不往真群发**),**不是**靠 `--dry-run` ——
    dry-run 分支只记长度、看不到正文,那样就测不到"到底写了什么"。
    """
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "LOG", data / "app.log")
    monkeypatch.setattr("sys.argv", ["notify_restart.py"])

    box: dict[str, str] = {}

    class _FakeFeishu:
        def __init__(self, *_a, **_k) -> None:
            pass

        def send(self, text: str) -> bool:
            box["text"] = text
            return True

    class _S:
        feishu_webhook_admin = "https://example.invalid/hook"
        feishu_secret = ""

    import config.settings as cs

    monkeypatch.setattr(cs, "get_settings", lambda: _S())
    monkeypatch.setattr("app.services.feishu_client.FeishuClient", _FakeFeishu)
    assert mod.main() == 0
    return box.get("text", "")


def test_alert_body_says_it_is_not_a_fresh_boot(tmp_path, monkeypatch) -> None:
    """🔴 正文要**自己说清是哪一种坏**(开机时长),别让收到的人再去翻日志和事件查看器。

    —— 这正是 2026-10-13 那条假红灯的教训:它只说"失联",读到的人只能靠猜。
    """
    text = _run_capturing_text(_load(uptime=3 * 3600), tmp_path, monkeypatch)
    assert "3.0 小时" in text and "不是刚开机" in text


def test_alert_body_admits_when_uptime_is_unknown(tmp_path, monkeypatch) -> None:
    """读不到开机时长时**如实说**,不许假装已排除 —— 那正是假红灯的温床。"""
    text = _run_capturing_text(_load(uptime=None), tmp_path, monkeypatch)
    assert "读不到本机开机时长" in text
