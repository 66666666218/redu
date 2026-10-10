"""微信读书 App 凭据**自愈**单测(2026-10-07)。

## 为什么要有"唤醒"
一直以为"阅读数断了 = 登录态失效,要人扫一次"。查下来**大半不是**:
App 长时间不动时,它库里的 `accessToken` 停在**旧值**,而**只要 App 活动一次
(adb `am start` 把它拉到前台),它就会写一个新的、有效的**。
实测(同一分钟):

    拉到的 `3sg_FVG7` → **-2012 登录超时**
    拉到的 `HbVEHrVJ` → **✓ 88 篇,82 篇带阅读数**(之后连拉 4 次都稳)

## 为什么必须是"唤醒+**验活**"
原来的 `_reget` 只是"重取一次、拿到**同一个值**、再失败"——那是**空转**,
它让"这条路全断了"在日志里长得像"自愈机制在正常工作"。
**有验活的才叫自愈。**
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import weread_app_token as wat  # noqa: E402


class TestWakeApp:
    def test_用显式组件名而不是main意图(self, monkeypatch) -> None:
        """⚠️ 实测 `am start -a MAIN -c LAUNCHER <pkg>` 在这个镜像上**会被拒**
        ("unable to resolve Intent"),必须用显式组件名 —— 这条别改回去。"""
        monkeypatch.setattr(wat, "_find_adb", lambda a="": "adbfake")
        monkeypatch.setattr(wat.time, "sleep", lambda s: None)
        seen: list = []

        class _R:
            stdout = b""
        monkeypatch.setattr(wat.subprocess, "run",
                            lambda cmd, **k: seen.append(cmd) or _R())
        assert wat.wake_app() is True
        assert seen and seen[0][-1] == wat.WEREAD_ACT, seen
        assert "-n" in seen[0], "必须用 -n <组件>,不能用 -a/-c 的隐式意图"

    def test_找不到adb就返回False不抛(self, monkeypatch) -> None:
        monkeypatch.setattr(wat, "_find_adb", lambda a="": "")
        assert wat.wake_app() is False


class TestRefreshWithWake:
    def _patch(self, monkeypatch, outcomes):
        """outcomes: 每次 refresh 的返回(依次取)。"""
        monkeypatch.setattr(wat, "wake_app", lambda *a, **k: True)
        monkeypatch.setattr(wat.time, "sleep", lambda s: None)
        monkeypatch.setattr(wat, "verify_by_api", lambda s, u: (lambda t, v: True))
        calls = {"n": 0}

        def _refresh(session, user_id, *, adb="", verify=None):
            i = calls["n"]
            calls["n"] += 1
            return outcomes[i] if i < len(outcomes) else outcomes[-1]
        monkeypatch.setattr(wat, "refresh", _refresh)
        return calls

    def test_第一次就成就一次收工(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, [{"ok": True, "accessToken": "T1", "vid": "V"}])
        out = wat.refresh_with_wake(None, 1)
        assert out["ok"] is True and out["accessToken"] == "T1"
        assert calls["n"] == 1

    def test_前两次失败会重试直到成功(self, monkeypatch) -> None:
        """★ 核心:唤醒一次不够就再来 —— 这正是"自愈"和"空转"的区别。"""
        calls = self._patch(monkeypatch, [
            {"ok": False, "reason": "取到旧值"},
            {"ok": False, "reason": "还是旧值"},
            {"ok": True, "accessToken": "T3", "vid": "V"},
        ])
        out = wat.refresh_with_wake(None, 1)
        assert out["ok"] is True and out["accessToken"] == "T3"
        assert calls["n"] == 3, f"应当重试到第 3 次,实际 {calls['n']} 次"

    def test_全失败才报错并给出人工步骤(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, [{"ok": False, "reason": "旧值"}])
        out = wat.refresh_with_wake(None, 1, attempts=3)
        assert out["ok"] is False
        assert calls["n"] == 3, "三次都要试满"
        assert "人工" in out["reason"], f"全失败时要给出人要做的那一步:{out['reason']}"


class TestVerifyByApi:
    def test_验证函数真的问接口(self, monkeypatch) -> None:
        """⚠️ 不是"我试着取了"就算过 —— 要真问一次;问了报错 = 验活失败。"""
        class _S:
            def scalar(self, *a, **k):
                return type("B", (), {"weread_book_id": "MP_WXS_1", "nickname": "x"})()

        class _C:
            #: ★ 记下构造参数 —— **`profile=` 必须传进来**(2026-10-11):
            #: 身份档(eink/安卓)如果传错,拿对的 token 去问也回 `-2012` ——
            #: 我当晚就因为这个把自己的好 token 否掉过一次。
            seen: list = []

            def __init__(self, t, v, **k):
                _C.seen.append(k)

            def articles(self, bid, **k):
                raise RuntimeError("token 是旧的")
        monkeypatch.setattr("app.services.weread_app_client.WereadAppClient", _C)
        v = wat.verify_by_api(_S(), 1)
        with pytest.raises(RuntimeError):
            v("T", "V")
        assert _C.seen and "profile" in _C.seen[0], (
            f"验活**必须带着身份档**去问(否则会拿对的 token 判死):{_C.seen!r}")

    def test_没有可试的号时不阻断(self, monkeypatch) -> None:
        class _S:
            def scalar(self, *a, **k):
                return None
        v = wat.verify_by_api(_S(), 1)
        assert v("T", "V") is True, "库里没有对标号时不该把整条路判死"
