"""看门狗重启应用时推一条飞书告警(2026-10-03)。

**为什么必须由看门狗(而不是应用自己)来推**:本项目**所有告警都跑在应用进程内** ——
应用一死,告警也一起死(`doc/operations.md` 自己承认这条)。看门狗是**进程外**唯一
还活着的东西,所以"应用挂了"这件事**只能由它来报**。这是全项目唯一"进程死即全盲"的缺口。

⚠️ **密钥绝不写进 `app_watchdog.bat`**:那个文件**受 git 跟踪**,而这个项目刚刚因为
"线上飞书 webhook 落在公开仓库"排查过一轮(见 `doc/体检与优化方案-2026-10-03.md`)。
所以这里统一走 `config.settings` 读 `.env`(gitignored)。

⚠️ 用 `pythonw` 调用时**没有 stdout**,所以本脚本**自己往 `data/app.log` 追加一行**,
不依赖重定向。推送失败只记日志 —— **绝不能因为告警失败而耽误重启**。

⚠️ **两类"不是崩溃"的停机都不该报**(第二种于 2026-10-13 实测补齐):
① **计划内停机** —— 应用优雅关停/`stop_app.bat` 写 `data/last_shutdown.txt`,180s 内跳过;
② **机器刚开机** —— 开机时长 < `BOOT_GRACE_SECONDS` 就跳过(重启/断电/蓝屏/强制关机都盖住)。
   第 ② 条是必需的:**Windows 关机不走 lifespan**,第 ① 条在这种场景下**整条失效**。

用法: `python scripts/win/notify_restart.py`(推管理员群)
      `python scripts/win/notify_restart.py --dry-run`(只组装 + 记日志,**不发**)
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

LOG = ROOT / "data" / "app.log"

#: 开机后多久之内,**不再把"应用没应答"当成崩溃**(秒)。
#:
#: ⚠️ **为什么需要**(2026-10-13 实测):18:03:46 用户从开始菜单点了**重启**(事件 id=1074,
#: 原因「其他(计划外)」)→ 18:04:35 开机 → 18:05:55 登录时启动文件夹里的看门狗发现
#: `/healthz` 还没起来 ⇒ 推了一条 **🔴 应用进程失联 —— 看门狗已重启**。
#: 但**它根本没崩**:是 Windows 关机把进程带走了。而 **Windows 关机不走 uvicorn 的 lifespan**,
#: 所以上面那条 `last_shutdown.txt` 保护**整条失效**(实测该文件停在 10-11 18:08,
#: 而 18:05:55 那条推送正是因此没被拦下)。
#:
#: 换判据成「机器刚开机」的好处:**与应用的关停方式无关** —— 重启/断电/蓝屏/强制关机
#: 一律盖住,不用再逐个补标记。600s 足够覆盖「开机 → 登录 → 看门狗补跑」这段。
BOOT_GRACE_SECONDS = 600


def machine_uptime() -> float | None:
    """本机已开机多少秒;**取不到返回 None**。

    取不到时调用方**照旧告警** —— 这层保护只用于**少报假红**,绝不能因为探测失败就
    连真崩溃也不报了(告警没响 = 假成功,是本仓最忌讳的那类)。
    """
    try:
        if sys.platform != "win32":
            return None
        import ctypes

        return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except Exception:  # noqa: BLE001 - 探测失败不影响告警主流程
        return None


def _log(line: str) -> None:
    """自己落盘(不依赖 stdout —— 调用方是 pythonw,没有控制台)。

    ⚠️ **不能只写 `data/app.log`**:那个文件被服务进程占着,Windows 下追加会
    `PermissionError: [Errno 13]`(2026-10-03 实测:跑完 grep 一条都没有)—— 而"应用挂了"
    恰恰是本脚本最该留下痕迹的时刻。所以 app.log 写不进就**退到独立文件**。
    """
    stamp = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] notify_restart: {line}\n"
    for path in (LOG, LOG.with_name("notify_restart.log")):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(stamp)
            return
        except OSError:
            continue


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    try:
        # ⚠️ **先看是不是"计划内停机"**(2026-10-03):应用正常关停时会写 `last_shutdown.txt`
        # (`app.platform._mark_graceful_shutdown`,或 `stop_app.bat` 显式写)。刚关过就说明是
        # 人主动停的,不是崩溃 —— 这种也报警的话,告警当天就当噪音被无视了(实测一天响 4 次)。
        marker = ROOT / "data" / "last_shutdown.txt"
        try:
            age = time.time() - marker.stat().st_mtime
            if age < 180:
                _log(f"跳过:应用 {age:.0f}s 前才**优雅关停**(计划内重启,不是崩溃)")
                return 0
        except OSError:
            pass                      # 没有标记 = 没优雅关停过 → 正常告警

        # ⚠️ **再看是不是"机器刚开机"**(2026-10-13 实测):重启/断电/蓝屏同样会让应用
        # "失联",但那不是崩溃。**这条比上面那个标记更根本** —— 标记要应用自己写,
        # 而 Windows 关机**不给**它这个机会;开机时长则与关停方式无关。
        up = machine_uptime()
        if up is not None and up < BOOT_GRACE_SECONDS:
            _log(f"跳过:本机 {up:.0f}s 前才开机(<{BOOT_GRACE_SECONDS}s,应用是被重启带走的,"
                 f"不是崩溃)—— 想确认这次重启是谁触发的,查事件查看器 System 日志 id=1074/109")
            return 0

        from config.settings import get_settings

        settings = get_settings()
        webhook = str(getattr(settings, "feishu_webhook_admin", "") or "").strip()
        # 正文**自己说清是哪一种坏**(2026-10-13):收到 🔴 的人第一句会问「是它崩了,
        # 还是机器刚起来?」—— 把开机时长写进去,就不必再去翻日志和事件查看器。
        if up is None:
            cause = "⚠️ 读不到本机开机时长,无法排除「刚开机」这一因 —— 请顺手确认。\n"
        else:
            cause = (f"本机已开机 {up / 3600:.1f} 小时(已超过 {BOOT_GRACE_SECONDS}s 宽限)"
                     f"⇒ **不是刚开机**,是进程真的没了。\n")
        text = (
            "🔴 应用进程失联 —— 看门狗已重启\n"
            f"时间:{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            "判据:健康检查 http://127.0.0.1:8080/healthz 当时不通。\n"
            + cause
            + "⚠️ 应用内的所有告警在这种情况下会**一起失效**,所以这条由看门狗(进程外)发出。"
        )
        if not webhook:
            _log("未配管理员群 webhook,跳过(不回落主群:这是运维告警)")
            return 0
        if dry_run:
            # 演练用:应用**没挂**时不该往群里发"失联"假警报,所以只组装不发送。
            _log(f"[dry-run] 组装完成(未发送),webhook 已配=True,正文 {len(text)} 字")
            print(text)
            return 0
        from app.services.feishu_client import FeishuClient

        ok = FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send(text)
        _log("已推送" if ok else "推送失败")
    except Exception as exc:  # noqa: BLE001 - 告警失败绝不能耽误重启
        _log(f"异常(忽略):{type(exc).__name__}: {str(exc)[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
