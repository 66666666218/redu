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
        #: ⚠️ 2026-10-23:`refresh_with_wake` 现在**总是**先判 `emulator_ready`
        #: (2026-10-22 把顺序改回"模拟器 App token 优先"时加的)。不 mock 它,
        #: 测试就会去问真实 adb —— 机器上模拟器开/关会让断言随机变红。
        monkeypatch.setattr(wat, "emulator_ready", lambda *a, **k: True)
        monkeypatch.setattr(wat, "verify_by_api", lambda s, u: (lambda t, v: True))
        calls = {"n": 0}

        def _refresh(session, user_id, *, adb="", verify=None, serial="", platform=""):
            i = calls["n"]
            calls["n"] += 1
            return outcomes[i] if i < len(outcomes) else outcomes[-1]
        monkeypatch.setattr(wat, "refresh", _refresh)
        return calls

    def test_第一次就成就一次收工(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, [{"ok": True, "accessToken": "T1", "vid": "V"}])
        out = wat.refresh_with_wake(None, 1, prefer_emulator=True)
        assert out["ok"] is True and out["accessToken"] == "T1"
        assert calls["n"] == 1

    def test_前两次失败会重试直到成功(self, monkeypatch) -> None:
        """★ 核心:唤醒一次不够就再来 —— 这正是"自愈"和"空转"的区别。"""
        calls = self._patch(monkeypatch, [
            {"ok": False, "reason": "取到旧值"},
            {"ok": False, "reason": "还是旧值"},
            {"ok": True, "accessToken": "T3", "vid": "V"},
        ])
        out = wat.refresh_with_wake(None, 1, prefer_emulator=True)
        assert out["ok"] is True and out["accessToken"] == "T3"
        assert calls["n"] == 3, f"应当重试到第 3 次,实际 {calls['n']} 次"

    def test_全失败才报错并给出人工步骤(self, monkeypatch) -> None:
        calls = self._patch(monkeypatch, [{"ok": False, "reason": "旧值"}])
        out = wat.refresh_with_wake(None, 1, attempts=3, prefer_emulator=True)
        assert out["ok"] is False
        assert calls["n"] == 3, "三次都要试满"
        assert "人工" in out["reason"], f"全失败时要给出人要做的那一步:{out['reason']}"

    def test_默认先走模拟器_App_token优先(self, monkeypatch) -> None:
        """★★ 2026-10-22 17:45 实测定案:**App token 才能取数**,HTTP 只兜底。

        同一分钟、同一本书、两个身份档:

            纯 HTTP 现铸 eink token + eink 头  ⇒ `-2041`(**换新铸也一样**)
            模拟器 App 签发 token  + app 头   ⇒ **97 篇 / 93 篇带读数**

        当天 00:40 曾按"HTTP 新鲜也能用"把顺序改成 HTTP 优先 —— 那个对照只证明了
        **认证**能过,没证明**能取数**。白天 08:04 / 14:05 / 17:20 全 `-2041`,
        正是因为库里的凭据是 eink 档。⇒ 默认顺序:**模拟器优先**,HTTP 只在取不到时兜底。
        """
        calls = self._patch(monkeypatch, [{"ok": True, "accessToken": "APP",
                                           "profile": "app"}])
        monkeypatch.setattr(wat, "emulator_ready", lambda *a, **k: True)

        def _http(*a, **k):
            raise AssertionError("模拟器已取到 App token,不该再走纯 HTTP")
        monkeypatch.setattr(wat, "refresh_via_http", _http)

        out = wat.refresh_with_wake(None, 1)          # 默认(prefer_emulator=None)
        assert out["ok"] is True and out["accessToken"] == "APP"
        assert calls["n"] == 1

    def test_模拟器不在才回落HTTP并标明只有eink档(self, monkeypatch) -> None:
        """模拟器不可用时 HTTP 兜底仍要写回(认证可用),但必须留下"取不到读数"的痕迹。"""
        monkeypatch.setattr(wat, "emulator_ready", lambda *a, **k: False)
        monkeypatch.setattr(wat, "ensure_emulator", lambda *a, **k: False)
        monkeypatch.setattr(wat, "refresh_via_http",
                            lambda *a, **k: {"ok": True, "via": "http", "profile": "eink"})
        out = wat.refresh_with_wake(None, 1)
        assert out["ok"] is True and out.get("via") == "http"

    def test_默认模拟器不在且HTTP失败_不浪费唤醒(self, monkeypatch) -> None:
        """HTTP 失败 + 模拟器不在 + 自动启停关 ⇒ 直接报错,不空转唤醒。"""
        monkeypatch.setattr(wat, "emulator_ready", lambda *a, **k: False)
        monkeypatch.setattr(wat, "ensure_emulator", lambda *a, **k: False)

        def _boom(*a, **k):
            raise AssertionError("模拟器不在时不该尝试唤醒 App")
        monkeypatch.setattr(wat, "wake_app", _boom)
        monkeypatch.setattr(wat, "refresh_via_http",
                            lambda *a, **k: {"ok": False, "reason": "http fail"})
        out = wat.refresh_with_wake(None, 1)
        assert out["ok"] is False and "两条路都没成" in out["reason"]

    def test_按需启动_取到token后关掉实例(self, monkeypatch) -> None:
        """★ 2026-10-22:HTTP 失败 → 按需拉起模拟器;**取到就关**。"""
        monkeypatch.setattr(wat, "emulator_ready", lambda *a, **k: False)
        monkeypatch.setattr(wat, "ensure_emulator", lambda *a, **k: True)
        monkeypatch.setattr(wat, "refresh_via_http",
                            lambda *a, **k: {"ok": False, "reason": "http fail"})
        monkeypatch.setattr(wat.time, "sleep", lambda s: None)
        monkeypatch.setattr(wat, "wake_app", lambda *a, **k: True)
        monkeypatch.setattr(wat, "refresh",
                            lambda *a, **k: {"ok": True, "accessToken": "T", "vid": "V"})
        quit_calls: list[int] = []
        monkeypatch.setattr(wat, "quit_emulator",
                            lambda *a, **k: (quit_calls.append(1), True)[1])
        out = wat.refresh_with_wake(None, 1)
        assert out["ok"] is True
        assert quit_calls == [1], "是我们自己拉起的实例 ⇒ 取到 token 后必须关掉"

    def test_本来就开着的实例不许关(self, monkeypatch) -> None:
        """反面对照:HTTP 失败、模拟器本来就开着 ⇒ 取完 token 不能把人家的窗口关了。"""
        monkeypatch.setattr(wat, "emulator_ready", lambda *a, **k: True)
        monkeypatch.setattr(wat, "ensure_emulator",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("已经在跑,不该再启")))
        monkeypatch.setattr(wat, "refresh_via_http",
                            lambda *a, **k: {"ok": False, "reason": "http fail"})
        monkeypatch.setattr(wat.time, "sleep", lambda s: None)
        monkeypatch.setattr(wat, "wake_app", lambda *a, **k: True)
        monkeypatch.setattr(wat, "refresh",
                            lambda *a, **k: {"ok": True, "accessToken": "T", "vid": "V"})
        quit_calls: list[int] = []
        monkeypatch.setattr(wat, "quit_emulator",
                            lambda *a, **k: (quit_calls.append(1), True)[1])
        out = wat.refresh_with_wake(None, 1)
        assert out["ok"] is True and quit_calls == []

    def test_serial到雷电实例号的换算(self) -> None:
        assert wat.emulator_index("emulator-5554") == 0
        assert wat.emulator_index("emulator-5556") == 1
        assert wat.emulator_index("127.0.0.1:5555") is None

    def test_模拟器不在就直接走纯HTTP(self, monkeypatch) -> None:
        """模拟器不在 ⇒ **不浪费**三次唤醒,直接走 HTTP。

        依据(2026-10-18):只有 **App 自己签发**的 token 能打 `/book/articles`
        (实测 60 发连打全成功);纯 HTTP 铸的那枚(身份档 `eink`)会被 `-2041` 拒。
        所以"在"时优先 App、"不在"时 HTTP 仍是有效回落(至少认证是活的)。
        """
        def _boom(*a, **k):
            raise AssertionError("模拟器不在时不该尝试唤醒 App")
        monkeypatch.setattr(wat, "wake_app", _boom)
        called = {"http": 0}

        def _http(session, user_id, **k):
            called["http"] += 1
            return {"ok": True, "via": "http"}
        monkeypatch.setattr(wat, "refresh_via_http", _http)
        out = wat.refresh_with_wake(None, 1, prefer_emulator=False)
        assert out["ok"] is True and out.get("via") == "http"
        assert called["http"] == 1


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

    def test_验活撞额度错误不算_token_坏(self, monkeypatch, caplog) -> None:
        """★★★ 2026-10-13 实测定性:接口**先校认证、后校额度** ⇒ 走到额度说明认证已过。

        同一本书、同一个号,只换 token(实测):
            · 库里那枚旧 token ⇒ `-2012 登录超时`(**认证失败**)
            · 刚铸出来的新 token ⇒ `-2041`(**额度**)
        ⇒ `-2041` 恰恰**证明这枚 token 是好的**。

        ⚠️ 不区分的真实代价(2026-10-11~13 实际发生):好 token 被**验活自己否掉**
        ⇒ `refresh_via_http` 报"未成" ⇒ **永远写不回库** ⇒ 库里那枚死 token 永远在用
        ⇒ 监听每轮回 `-2012` ⇒ 推「App 登录态失效」告警 ⇒ 人跑去开雷电模拟器(**白跑**)。
        """
        class _S:
            def scalar(self, *a, **k):
                return type("B", (), {"weread_book_id": "MP_WXS_1", "nickname": "x"})()

        class _C:
            def __init__(self, t, v, **k):
                pass

            def articles(self, bid, **k):
                raise RuntimeError("App 接口报错:-2041 -2041")   # 与实测文本逐字一致

        monkeypatch.setattr("app.services.weread_app_client.WereadAppClient", _C)
        v = wat.verify_by_api(_S(), 1)
        import logging

        with caplog.at_level(logging.WARNING):
            assert v("T", "V") is True, "额度错 ≠ token 坏:新铸的 token 必须被放行"
        assert any("额度" in r.message for r in caplog.records), (
            "放行要留痕 —— 不做静默绿灯(本仓最忌的那种)")

    def test_真登录态错仍然要判死(self, monkeypatch) -> None:
        """反向:拿到 `-2012` 才说明 token 真不能用,必须抛出去别放行。"""
        class _S:
            def scalar(self, *a, **k):
                return type("B", (), {"weread_book_id": "MP_WXS_1", "nickname": "x"})()

        class _C:
            def __init__(self, t, v, **k):
                pass

            def articles(self, bid, **k):
                raise RuntimeError("App 登录态问题:-2012 登录超时")

        monkeypatch.setattr("app.services.weread_app_client.WereadAppClient", _C)
        v = wat.verify_by_api(_S(), 1)
        with pytest.raises(RuntimeError):
            v("T", "V")
