"""夸克「U口令」→ 我方分享链,走**模拟器 UI**(2026-10-06 实测走通)。

## 为什么走 UI 而不是接口
夸克口令的解析只存在于**手机 App**,而且接口是**带签名的 JSON**:
- 端点已找到(`utoken2.quark.cn/utoken/v2/{parse,create}`),参数也读出来了
  (`app=QUARK` / `kps` / `shareCode` / `clipboard` / `identifier` / `shareSecret` / `timestamp` / `sign`),
  但 **`sign` 的算法没解出来**(可能在 native `.so`),所以接口路线卡住 —— 详见
  `scripts/probe_quark_kouling.py`
- **抓包也走不通**:埋点流量抓得到、网盘 API 抓不到,且报「网络异常」 ⇒ 典型**证书固定**

⇒ 换策略:**让 App 自己去解析,我们只读结果**。

## 已验证的完整链路(每一步都实测过)
```
完整口令文本
  → Set-Clipboard(⚠️ 必须 Unicode 格式)
  → 激活雷电窗口 + 重启夸克
  → 夸克自动读剪贴板 → 弹出卡片「<资源名> / 来自剪贴板 / [立即查看]」   ← uiautomator 可读
  → 点「立即查看」→ 打开分享页(WebView)
  → 点「保存」→ **文件进我们自己的夸克盘** `来自：分享/<资源名>`
  → 用**已有代码** `quark_transfer.QuarkTransfer` 查盘 + 建我方分享链
```
实测产物:`https://pan.quark.cn/s/c0271dae8f1d4860bfa0daae97d0d124`

## ⚠️ 踩过的坑(都别再踩)
1. **`clip.exe` 不行** —— 它只写 ANSI 格式,雷电同步不过去。**必须** PowerShell 的
   `Set-Clipboard`(Unicode)。
2. **光设剪贴板不够,还要激活雷电窗口** —— 同步是在窗口激活时触发的。
3. **分享页是 WebView,`uiautomator` 读不到它的内容** —— 所以能结构化读到的只有
   **卡片上的资源名**;分享页里的信息只能靠截图。「保存」按钮也只能用**固定坐标**。
4. **坐标绝不能复用旧 dump** —— 我用过几分钟前的坐标,结果点进「设备管理」并弹出
   「删除这个设备?」(已及时取消)。`tap_text()` 的纪律是:**先 dump、找不到就拒绝点击**。
5. **别用 8080 当代理端口** —— 本机 app 服务占着它;抓包那次撞端口是白折腾一场。

跑法:
    python scripts/quark_kouling_ui.py            # 读剪贴板里的口令并解析
    python scripts/quark_kouling_ui.py "<口令文本>"
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ADB = r"D:\leidian\LDPlayer14\adb.exe"
EMULATOR_PROC = "dnplayer"
QUARK_PKG = "com.quark.browser"
QUARK_ACT = "com.quark.browser/com.ucpro.MainActivity"


def _run(cmd: list[str], timeout: int = 120) -> str:
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.stdout.decode("utf-8", "replace")


def set_clipboard(text: str) -> None:
    """把口令放进剪贴板 —— ⚠️ **必须走 PowerShell 的 Set-Clipboard(Unicode)**。

    `clip.exe` 只写 ANSI,雷电同步不过去(实测:同样的文本用 clip.exe 无效、
    用 Set-Clipboard 就弹卡片了)。
    """
    tmp = os.path.join(os.environ.get("TEMP", "."), "_quark_kouling.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    ps = ('$t = Get-Content -Raw -Encoding UTF8 "{}"; Set-Clipboard -Value $t'.format(tmp))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=60)


def activate_emulator() -> None:
    """激活雷电窗口 —— 剪贴板同步是在窗口激活时触发的。"""
    ps = (f"$p = Get-Process {EMULATOR_PROC} -ErrorAction SilentlyContinue | "
          "Select-Object -First 1; if ($p) { (New-Object -ComObject WScript.Shell)"
          ".AppActivate($p.Id) | Out-Null }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=60)
    time.sleep(1.2)


def dump_ui() -> str:
    _run([ADB, "shell", "uiautomator", "dump", "/sdcard/ui.xml"])
    return _run([ADB, "shell", "cat", "/sdcard/ui.xml"])


def texts(xml: str) -> list[str]:
    return [t for t in re.findall(r'text="([^"]*)"', xml) if t.strip()]


def find(xml: str, text: str) -> tuple[int, int] | None:
    """**在刚 dump 出来的 XML 里**找节点,返回中心坐标(绝不复用旧坐标)。"""
    for m in re.finditer(r"<node[^>]*>", xml):
        tag = m.group(0)
        if f'text="{text}"' in tag or f'content-desc="{text}"' in tag:
            b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
            if b:
                x1, y1, x2, y2 = map(int, b.groups())
                if x2 > x1 and y2 > y1:
                    return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def tap_text(text: str, wait: float = 5.0) -> bool:
    """dump → 定位 → 点击。**找不到就拒绝点击**(宁可不动手也不瞎点)。"""
    xml = dump_ui()
    pos = find(xml, text)
    if not pos:
        print(f"  !! 找不到 {text!r},**不点**")
        return False
    subprocess.run([ADB, "shell", "input", "tap", str(pos[0]), str(pos[1])], timeout=60)
    time.sleep(wait)
    return True


def resolve(kouling: str, save: bool = False) -> dict:
    """口令 → 卡片上的资源名(可选:一路点到「保存」,把文件搬进我们盘)。"""
    set_clipboard(kouling)
    activate_emulator()
    _run([ADB, "shell", "am", "force-stop", QUARK_PKG])
    time.sleep(2)
    _run([ADB, "shell", "am", "start", "-n", QUARK_ACT])
    time.sleep(10)

    xml = dump_ui()
    ts = texts(xml)
    if not any("来自剪贴板" in t for t in ts):
        return {"ok": False, "reason": "没弹出剪贴板卡片(口令无效?同步没触发?)", "texts": ts[:10]}
    # 卡片上的资源名 = 「来自剪贴板」上一条
    title = ""
    for i, t in enumerate(ts):
        if "来自剪贴板" in t and i > 0:
            title = ts[i - 1]
            break
    out = {"ok": True, "title": title}
    if save and tap_text("立即查看", wait=8):
        # ⚠️ 分享页是 WebView,读不到控件 ⇒ 「保存」只能用固定坐标(实测中心 946,1872)
        subprocess.run([ADB, "shell", "input", "tap", "946", "1872"], timeout=60)
        time.sleep(8)
        out["saved"] = True
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_save = "--save" in sys.argv
    kouling = args[0] if args else ""
    if not kouling:
        print("用法: python scripts/quark_kouling_ui.py \"<完整口令文本>\"")
        print("(不带参数会提示;剪贴板读取在 Android 14 上受隐私限制,所以要求显式传入)")
        return 1
    r = resolve(kouling, save=do_save)
    print("结果:", r)
    return 0 if r.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
