"""夸克口令「喂进客机」这一步的守卫(2026-10-09)。全部离线。

## 背景:这一步原来**三个 bug 叠在一起**,而且都是"看起来失败实则成功"那一族
1. **宿主写剪贴板 → 客机拿不到**:`vms/<vm>/Logs/VBox.log` 每次启动都写着
   `Shared Clipboard: **Mode: Off**`(四份轮转日志全是)—— 也就是说**这条路从来没通过**。
   旧注释里"提供剪贴板的进程存活 20 秒就弹卡片"是**假相关**(真正相关的变量是
   「**客机自己的剪贴板里有没有东西**」,那次多半是上一次手动 Ctrl+C 的残留)。
   ⇒ 换成 **adb 的 IME 通道**(`am broadcast` 打文本进客机输入框,再在客机内 Ctrl+A/C)。
2. **`activate_emulator` 硬闸**:窗口不在前台就 `env=True` 直接不干活 —— 实测**一天 7/7 轮全栽在这**
   (运行记录主因写着「雷电窗口不在前台」)。而 adb 的 `input`/`uiautomator` **与宿主焦点无关**。
3. **判据少认了一个写法**:夸克渲染的是「来自剪**切**板」,而代码只认「来自剪**贴**板」——
   **一个字的差别**,卡片明明弹了却判成「口令无效」,**调用方据此撤回线索**。
"""
from __future__ import annotations

import pytest

from app.services import quark_kouling as kk


# ---------------------------------------------------------------------------
# 判据(卡片上那三个词)
# ---------------------------------------------------------------------------


def test_两种写法都要认() -> None:
    """★ 一个字的差别:`剪**切**板` vs `剪**贴**板` —— 实测夸克渲染的是**切**。"""
    assert kk.judge_card(["监听宣传", "来自剪切板", "立即查看"]) == "监听宣传"
    assert kk.judge_card(["监听宣传", "来自剪贴板", "立即查看"]) == "监听宣传"


def test_没弹卡片要返回空串() -> None:
    """反面对照 —— 不然"没弹"会被读成"解析出来了"(`false` 的另一种写法)。"""
    assert kk.judge_card([]) == ""
    assert kk.judge_card(["夸克", "搜索"]) == ""


def test_判据词在第一行时取不到标题() -> None:
    """卡片的形状是 `[资源名, 判据词, 立即查看]` —— 判据词在首行说明**这不是卡片**,
    不能拿"上一行"去凑(那会取到别的界面的文字,把"没弹"读成"解析出来了")。"""
    assert kk.judge_card(["来自剪切板"]) == ""


# ---------------------------------------------------------------------------
# 前置条件(坏了会静默不弹,与"口令无效"长得一模一样)
# ---------------------------------------------------------------------------


def test_输入法不是_ADBKeyboard_时要判不就绪并给修法(monkeypatch) -> None:
    monkeypatch.setattr(kk, "_adb", lambda a, timeout=30: (True, "com.android.inputmethod.pinyin/.InputService"))
    ok, why = kk.ime_ready()
    assert ok is False
    assert "ADBKeyboard" in why and "default_input_method" in why, why


def test_就绪时返回_True(monkeypatch) -> None:
    monkeypatch.setattr(kk, "_adb", lambda a, timeout=30: (True, kk.IME_ID))
    ok, why = kk.ime_ready()
    assert ok is True and kk.IME_ID in why


def test_adb_掉了不许抛(monkeypatch) -> None:
    monkeypatch.setattr(kk, "_adb", lambda a, timeout=30: (False, "adb 不可用"))
    ok, why = kk.ime_ready()
    assert ok is False and "adb" in why


# ---------------------------------------------------------------------------
# 喂入:命令顺序
# ---------------------------------------------------------------------------


def test_喂入的四步顺序不能乱(monkeypatch) -> None:
    """★ 顺序是「开靶子 → 广播打字 → 客机内 Ctrl+A → Ctrl+C」——
    少一步或换顺序都会静默不弹(而症状与"口令无效"完全一样)。

    ⚠️ 判据是**真实发出的 adb 命令序列**,不是"函数返回 True"。
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(kk, "ime_ready", lambda: (True, "ok"))
    monkeypatch.setattr(kk, "_adb", lambda a, timeout=30: (calls.append(list(a)), (True, ""))[1])
    monkeypatch.setattr("time.sleep", lambda *_: None)

    ok, why = kk.feed_via_ime("伏脂乞台盆蛙盛洞座")
    assert ok is True, why
    joined = [" ".join(c) for c in calls]
    assert any("am start -n " in j and kk.TYPING_TARGET in j for j in joined), joined
    assert any("am broadcast -a ADB_INPUT_TEXT" in j and "伏脂乞台盆蛙盛洞座" in j for j in joined), joined
    assert any("keycombination 113 29" in j for j in joined), "要先 Ctrl+A 全选"
    assert any("keycombination 113 31" in j for j in joined), "再 Ctrl+C 复制"
    # 顺序:广播必须在两个 keycombination 之前
    i_bc = next(i for i, j in enumerate(joined) if "ADB_INPUT_TEXT" in j)
    i_cp = next(i for i, j in enumerate(joined) if "113 31" in j)
    assert i_bc < i_cp


def test_前置不就绪时不许发打字命令(monkeypatch) -> None:
    """⚠️ 断言的是「**不许发打字命令**」,不是"一个 adb 都不许发" ——
    前置检查自己**要探一次**(`ensure_ime` 会 `pm list` / 必要时 `settings put`)。
    第一版写成 `calls == []`,改实现后当场变红(那种断言把实现细节焊死了)。"""
    calls: list = []
    monkeypatch.setattr(kk, "ensure_ime", lambda: (False, "输入法不对"))
    monkeypatch.setattr(kk, "_adb", lambda a, timeout=30: (calls.append(list(a)), (True, ""))[1])
    ok, why = kk.feed_via_ime("口令")
    assert ok is False and "输入法不对" in why
    joined = [" ".join(c) for c in calls]
    assert not any("ADB_INPUT_TEXT" in j or "keycombination" in j for j in joined), joined


def test_口令为空要早退(monkeypatch) -> None:
    monkeypatch.setattr(kk, "ime_ready", lambda: (True, "ok"))
    assert kk.feed_via_ime("   ")[0] is False


# ---------------------------------------------------------------------------
# ★ IME 自愈:这个设置**扛不过模拟器重启**(2026-10-09 实测)
# ---------------------------------------------------------------------------


def test_装了就自己设回去_因为重启必然丢掉(monkeypatch) -> None:
    """★★ 实测:模拟器重启后 `default_input_method` **退回拼音输入法**,
    而 ADBKeyboard **仍然装着**。只检查不修的话,**每次重启后这条链静默失败**
    (症状与"口令无效"一模一样)⇒ 必须自愈。

    判据是**真的发出了那两条 settings 命令**,不是"函数返回 True"。
    """
    calls: list[list[str]] = []
    state = {"ready": False}

    def _adb(a, timeout=30):
        calls.append(list(a))
        if a[:3] == ["shell", "pm", "list"]:
            return True, "package:com.android.adbkeyboard"
        if a[:3] == ["shell", "settings", "put"]:
            state["ready"] = True          # 设完就当生效(下一次 ime_ready 会真查)
            return True, ""
        return True, ""

    monkeypatch.setattr(kk, "_adb", _adb)
    monkeypatch.setattr(kk, "ime_ready",
                        lambda: (state["ready"], "ok" if state["ready"] else "不是 ADBKeyboard"))
    ok, why = kk.ensure_ime()
    assert ok is True, why
    joined = [" ".join(c) for c in calls]
    assert any("settings put secure default_input_method " + kk.IME_ID in j for j in joined), joined
    assert any("settings put secure enabled_input_methods" in j for j in joined), joined


def test_没装_ADBKeyboard_要报清楚而不是瞎设(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(kk, "_adb", lambda a, timeout=30: (calls.append(list(a)), (True, "package:com.other"))[1])
    monkeypatch.setattr(kk, "ime_ready", lambda: (False, "不是 ADBKeyboard"))
    ok, why = kk.ensure_ime()
    assert ok is False and "ADBKeyboard" in why and "adb install" in why, why
    assert not any("settings" in " ".join(c) for c in calls), "没装就不该去改系统设置"
