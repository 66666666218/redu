"""磁盘水位守卫:磁盘写满会**整站 502**(写不进库、写不进日志),而水位是**逐渐**涨上来的 ——
远在崩溃之前就有征兆,属于最典型"本可以预警却没预警"的故障。每天查一次,超阈值推**管理员群**。

**为什么单独一个模块**:它是**主机级**指标(和具体哪个用户、哪个平台都没关系),放进
`health.py`(按平台/表算新鲜度)或 `alert_service.py`(按用户规则算告警)都会串味。

**判据与纪律**:
  - 按**文件系统**(`st_dev`)去重,而不是按路径 —— 同一个盘上挂多个目录只报一次;
    Docker 里 `/app/data` 常是独立挂载卷,与 `/app` 不是同一个 fs,所以两个都要查。
  - 阈值是**比例**不是绝对值:磁盘大小差异很大,85% 才是可比的信号。
  - 告警标题**不含数字**(只到"哪个盘")。含数字(如 `92.3%`)会让每天的标题都不同,
    `notify_incident` 的冷却门按标题去重,于是**每天都响一次** —— 这正是 2026-10-03
    看门狗告警"一天响 4 次变噪音"的同一个坑。数字放在 detail 里。
  - 读不到用量不视为告警(容器里可能没有该路径):记 warning 后跳过,不伪造 "0%"。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from sqlalchemy import select

from app.utils import get_logger
from config.settings import Settings, get_settings

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]


def default_paths() -> list[Path]:
    """默认盯这几处:项目盘(日志/静态产物)、数据目录(SQLite 库)、备份目录。

    三者常在同一块盘上 —— 去重后只报一次,不必让用户自己算。
    """
    return [ROOT, ROOT / "data", ROOT / "data" / "backups"]


def _unique_filesystems(paths: list[Path]) -> list[Path]:
    """按 `st_dev` 去重:同一文件系统只留第一个能访问到的路径。

    不存在的路径直接跳过(容器里 `data/backups` 可能还没建),**不报错、不伪造**。
    """
    out: list[Path] = []
    seen: set[int] = set()
    for p in paths:
        try:
            dev = os.stat(p).st_dev
        except OSError:
            continue
        if dev in seen:
            continue
        seen.add(dev)
        out.append(p)
    return out


def disk_report(paths: list[Path] | None = None) -> list[dict]:
    """每个**独立文件系统**一行:total/free/used_ratio(GB 用 1e9 换算,便于人读)。"""
    rows: list[dict] = []
    for p in _unique_filesystems(paths or default_paths()):
        try:
            u = shutil.disk_usage(str(p))
        except OSError as exc:  # noqa: BLE001 - 读不到就当没有,绝不伪造 0
            logger.warning("磁盘用量读取失败 %s: %s", p, exc)
            continue
        rows.append({
            "path": str(p),
            "total_gb": round(u.total / 1e9, 1),
            "free_gb": round(u.free / 1e9, 1),
            "used_ratio": round(u.used / u.total, 4) if u.total else 0.0,
        })
    return rows


def _fmt(row: dict) -> str:
    return (f"{row['path']}:已用 {row['used_ratio'] * 100:.1f}%"
            f"(剩 {row['free_gb']}G / 共 {row['total_gb']}G)")


def disk_guard_tick(settings: Settings | None = None) -> int:
    """主机级磁盘水位作业:超 warn 比值的盘推管理员群,返回实际推送条数。

    推给"首个启用用户"(与 `data_cleanup` 的备份失败告警同款 —— 这是主机级事件,
    不属于任何一个用户,库里的 `FeishuAlert` 又必须挂 user_id)。

    ⚠️ **整段包在 try 里**:本模块跑在调度器进程内,读盘/建会话/推送任何一步抛异常
    都会终止这一轮 —— 而同一轮里还有别的作业排队。守卫自己失败,不能变成新故障。
    """
    settings = settings or get_settings()
    if not getattr(settings, "disk_guard_enabled", True):
        return 0
    warn = float(getattr(settings, "disk_warn_ratio", 0.85) or 0.85)
    crit = float(getattr(settings, "disk_crit_ratio", 0.95) or 0.95)

    from app.db.database import get_session_local
    from app.db.models import User
    from app.services import alert_service

    try:
        hot = [r for r in disk_report() if r["used_ratio"] >= warn]
        if not hot:
            return 0
        db = get_session_local()()
        try:
            uid = db.scalar(select(User.id).where(User.enabled.is_(True)).order_by(User.id))
            if not uid:
                return 0
            sent = 0
            for r in hot:
                level = "🔴 磁盘严重吃紧" if r["used_ratio"] >= crit else "🟠 磁盘水位偏高"
                # ⚠️ 标题**只到盘**,不带百分比:带数字则每天标题都不同 → 冷却门失效 → 天天响
                title = f"{level}:{r['path']}"
                detail = (f"{_fmt(r)} —— 阈值 {warn * 100:.0f}%。磁盘写满会**整站 502**"
                          f"(写不进库/日志),请清理 data/backups、data/app.log 或做大扫除。")
                if alert_service.notify_incident(db, int(uid), "ops", title, detail,
                                                 settings=settings, push_feishu=True):
                    sent += 1
            return sent
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - 守卫自身失败不该影响调度器其它作业
        logger.exception("磁盘水位检查失败")
        return 0
