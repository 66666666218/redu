"""看门狗重启应用时推一条飞书告警(2026-10-03)。

**为什么必须由看门狗(而不是应用自己)来推**:本项目**所有告警都跑在应用进程内** ——
应用一死,告警也一起死(`doc/operations.md` 自己承认这条)。看门狗是**进程外**唯一
还活着的东西,所以"应用挂了"这件事**只能由它来报**。这是全项目唯一"进程死即全盲"的缺口。

⚠️ **密钥绝不写进 `app_watchdog.bat`**:那个文件**受 git 跟踪**,而这个项目刚刚因为
"线上飞书 webhook 落在公开仓库"排查过一轮(见 `doc/体检与优化方案-2026-10-03.md`)。
所以这里统一走 `config.settings` 读 `.env`(gitignored)。

⚠️ 用 `pythonw` 调用时**没有 stdout**,所以本脚本**自己往 `data/app.log` 追加一行**,
不依赖重定向。推送失败只记日志 —— **绝不能因为告警失败而耽误重启**。

用法: `python scripts/win/notify_restart.py`(推管理员群)
      `python scripts/win/notify_restart.py --dry-run`(只组装 + 记日志,**不发**)
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

LOG = ROOT / "data" / "app.log"


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
        from config.settings import get_settings

        settings = get_settings()
        webhook = str(getattr(settings, "feishu_webhook_admin", "") or "").strip()
        text = (
            "🔴 应用进程失联 —— 看门狗已重启\n"
            f"时间:{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            "判据:健康检查 http://127.0.0.1:8080/healthz 当时不通。\n"
            "⚠️ 应用内的所有告警在这种情况下会**一起失效**,所以这条由看门狗(进程外)发出。"
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
