# -*- coding: utf-8 -*-
"""网盘链路**自检**(2026-10-19,用户口径「增加一道自检功能」)。

## 它解决什么

清理与转存这两条链最大的风险不是"报错",而是「**看着在跑、其实空转**」:
  · 10-12~10-14 夸克连续三轮一字不差地「跳过(没扫完)」—— 数字纹丝不动;
  · 10-18 迅雷周去重「扫 2581 个目录 / 800 次调用」后撞预算,**一个都没删**;
  · 10-18 23:40 一次测试把百度生产清单覆盖成 `dup0.apk` 这种造出来的数据。

这些在 `runs` 里**都记了**,但没人每天去翻。本模块就是那个"每天替你翻一遍"的作业。

## 边界

1. **只读**:不列盘、不转存、不删除 —— 只查库、查文件、查配置。撞锁/网络都不可能。
2. **正常也要留一行**:`runs.kind='pan_self_check'` 每轮都写,detail 写清各项结果 ——
   否则「自检自己没跑」会是整个体系里**最安静的那个静默失败**。
3. **异常才推管理群**:正常不打扰;异常的标题带具体项,不写"请检查"这种没用的话。

## 检查项

| 项 | 判据 |
|---|---|
| 作业时效 | `pan_auto_clean`/`xunlei_cleanup` ≤26h;`xunlei_dedupe` ≤8d;从未跑过 = 错误 |
| 最近一轮结果 | `failed` 或 detail 命中「没扫完 / 撞上限 / 撞 API 预算 / 不可信 / 异常」 |
| 盘容量 | 账号池里任一账号 ≥95% 警告、≥99% 错误（转存随时会停) |
| 资源库复用 | 近 N 天有成功搬链、却**一次复用都没有** ⇒ 复用入口可能没接上 |
| 百度清单 | 不存在 / 超过 48h / 内容是测试造的(`dup0.apk` 形状) |
| 配置 | 自动清理关着、dry-run 还开着、单轮上限或周去重预算偏小 |
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 作业 → 允许的最大间隔(小时)。日作业留 2h 余量;周作业留 1 天余量。
_JOB_MAX_AGE_H: dict[str, float] = {
    "pan_auto_clean": 26.0,
    "xunlei_cleanup": 26.0,
    "xunlei_dedupe": 24 * 8.0,
}

_JOB_LABEL: dict[str, str] = {
    "pan_auto_clean": "三盘自动清理",
    "xunlei_cleanup": "迅雷过期清理",
    "xunlei_dedupe": "迅雷整包去重",
}

#: 运行记录里出现这些词 = 这一轮「看着成功、其实空转/受阻」。
_BAD_MARKERS: tuple[tuple[str, str], ...] = (
    ("异常", "error"),
    ("failed", "error"),
    ("没扫完", "warn"),
    ("撞上限", "warn"),
    ("撞 API 预算", "warn"),
    ("不可信", "warn"),
)


def _item(name: str, level: str, detail: str, **extra: Any) -> dict:
    """一项检查结果。`level` ∈ `ok` / `warn` / `error`(同 `info` 不入告警)。"""
    return {"name": name, "level": level, "detail": detail, **extra}


def _age_hours(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return round((datetime.now() - dt).total_seconds() / 3600, 1)


def _last_run(db: Session, user_id: int, kind: str):
    from app.db.models import RunRecord

    return db.execute(
        select(RunRecord)
        .where(RunRecord.user_id == user_id, RunRecord.kind == kind)
        .order_by(RunRecord.id.desc())
        .limit(1)
    ).scalars().first()


def _last_heartbeat(db: Session, job_id: str):
    """调度器的「作业真的跑过」心跳。`runs` 不是每个作业都写(如 `xunlei_cleanup`)。"""
    from app.db.models import JobHeartbeat

    return db.get(JobHeartbeat, job_id)


def _check_job_age(db: Session, user_id: int, kind: str, max_age_h: float) -> dict:
    label = _JOB_LABEL.get(kind, kind)
    run = _last_run(db, user_id, kind)
    if run is not None:
        age = _age_hours(run.started_at)
        detail = f"最近一轮 {run.started_at:%Y-%m-%d %H:%M}({run.status}),{age}h 前"
        if age is None or age > max_age_h:
            return _item(f"{label}时效", "error",
                         f"{detail};超过阈值 {max_age_h:.0f}h")
        if run.status == "failed":
            return _item(f"{label}时效", "error", f"{detail};该轮失败")
        return _item(f"{label}时效", "ok", detail)
    #: ★ **`runs` 不是每个作业都写**(2026-10-19 实测):`xunlei_cleanup` 只写
    #: `job_heartbeats`。没有这一层,自检会把它误报成「从未运行」—— 而误报会让
    #: 人开始忽略自检,那是告警体系最坏的结局。
    hb = _last_heartbeat(db, kind)
    if hb is None or hb.last_run_at is None:
        return _item(f"{label}时效", "error", f"{kind} **从未运行过**")
    age = _age_hours(hb.last_run_at)
    detail = (f"心跳:最近 {hb.last_run_at:%Y-%m-%d %H:%M}"
              f"({hb.run_count} 次,错误 {hb.error_count}),{age}h 前")
    if age is None or age > max_age_h:
        return _item(f"{label}时效", "error", f"{detail};超过阈值 {max_age_h:.0f}h")
    return _item(f"{label}时效", "ok", detail)


def _check_run_detail(db: Session, user_id: int, kind: str) -> dict:
    """最近一轮的 detail 是否命中「空转/受阻」标记。"""
    label = _JOB_LABEL.get(kind, kind)
    run = _last_run(db, user_id, kind)
    if run is None:
        #: 同 `_check_job_age`:只写心跳的作业没有 detail,不能因此判失败。
        hb = _last_heartbeat(db, kind)
        if hb is None or hb.last_run_at is None:
            return _item(f"{label}结果", "error", "没有运行记录")
        if int(hb.error_count or 0) > 0 and str(hb.last_error or "").strip():
            return _item(f"{label}结果", "warn",
                         f"心跳里有 {hb.error_count} 次错误:{str(hb.last_error)[:160]}")
        return _item(f"{label}结果", "ok",
                     f"该作业只写心跳({hb.run_count} 次,错误 0),无 detail")
    text = str(run.detail or "")
    hits = [word for word, _ in _BAD_MARKERS if word.lower() in text.lower()]
    if run.status == "failed" and "failed" not in [h.lower() for h in hits]:
        hits.insert(0, "failed")
    if not hits:
        return _item(f"{label}结果", "ok", text[:160] or "无异常标记")
    level = "error" if run.status == "failed" or any(
        word in ("异常", "failed") for word in hits) else "warn"
    return _item(f"{label}结果", level,
                 f"命中「{'/'.join(hits)}」:{text[:180]}")


def _check_quota(db: Session, user_id: int) -> dict:
    """账号池里最满的那个号:≥95% 警告、≥99% 错误(转存随时会停)。"""
    from app.services import pan_accounts

    worst: tuple[float, str] | None = None
    checked = 0
    for platform, label in (("quark", "夸克"), ("baidupan", "百度"), ("xunlei", "迅雷")):
        try:
            refs = pan_accounts.accounts(db, platform, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 - 池读坏了不该让整轮自检挂掉
            return _item("盘容量", "warn",
                         f"账号池读取失败({label}):{type(exc).__name__}: {str(exc)[:80]}")
        for ref in refs:
            if ref.last_ratio is None:
                continue
            checked += 1
            ratio = float(ref.last_ratio)
            if worst is None or ratio > worst[0]:
                worst = (ratio, f"{label}/{ref.label}")
    if not checked or worst is None:
        return _item("盘容量", "info", "账号池里还没有使用率数据(等下一次转存/巡检)")
    ratio, who = worst
    pct = ratio * 100
    if pct >= 99:
        return _item("盘容量", "error", f"{who} 已用 {pct:.1f}% —— 转存随时会因盘满停摆")
    if pct >= 95:
        return _item("盘容量", "warn", f"{who} 已用 {pct:.1f}% —— 建议尽快清理/加号")
    return _item("盘容量", "ok", f"最满的是 {who} {pct:.1f}%")


def _check_reuse(db: Session, user_id: int, days: int) -> dict:
    """近 N 天「复用已有链」/「成功搬链」的比例 —— 复用入口是否真的在工作。"""
    from app.db.models import DiscoveredPanLink, XunleiGroupShare

    since = datetime.now() - timedelta(days=days)
    disc_ok = int(db.scalar(
        select(func.count()).select_from(DiscoveredPanLink).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.found_at >= since,
            DiscoveredPanLink.status == "ok")) or 0)
    disc_reuse = int(db.scalar(
        select(func.count()).select_from(DiscoveredPanLink).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.found_at >= since,
            DiscoveredPanLink.status == "ok",
            DiscoveredPanLink.message.like("%库里已有%"))) or 0)
    grp_ok = int(db.scalar(
        select(func.count()).select_from(XunleiGroupShare).where(
            XunleiGroupShare.user_id == user_id,
            XunleiGroupShare.synced_at >= since,
            XunleiGroupShare.status == "ok")) or 0)
    grp_reuse = int(db.scalar(
        select(func.count()).select_from(XunleiGroupShare).where(
            XunleiGroupShare.user_id == user_id,
            XunleiGroupShare.synced_at >= since,
            XunleiGroupShare.status == "ok",
            XunleiGroupShare.message.like("%三盘互通%"))) or 0)
    total, reused = disc_ok + grp_ok, disc_reuse + grp_reuse
    detail = (f"近 {days} 天:公开发现 {disc_ok} 条(复用 {disc_reuse})、"
              f"迅雷群 {grp_ok} 条(复用 {grp_reuse})")
    if total >= 3 and reused == 0:
        return _item("资源库复用", "warn",
                     detail + ";**一次复用都没有** —— 可能是全是新资源,"
                     "也可能是复用入口没接上(看 `pan_discovery.reuse_if_have` 的调用点)")
    return _item("资源库复用", "ok", detail)


def _check_baidu_report(path: str | Path) -> dict:
    """百度重复清单:存在、新鲜、且**不是测试造的数据**。

    ⚠️ 为什么专门查"测试数据":`tests/test_baidu_dup.py` 曾经直接调用真实的
    `report_baidu()`(路径写死 `data/pan_dedupe_baidu.json`)⇒ 跑一次测试就把生产清单
    覆盖成 `dup0.apk / 100 字节`。清单是人工决定"要不要开百度删除"的唯一依据,
    污染它比没有清单更坏。
    """
    p = Path(path)
    if not p.exists():
        return _item("百度清单", "warn", f"清单不存在:{p}")
    age = _age_hours(datetime.fromtimestamp(p.stat().st_mtime))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return _item("百度清单", "warn", f"清单读不出来:{type(exc).__name__}: {str(exc)[:80]}")
    dup_files = data.get("dup_files") or []
    looks_test = (
        str(data.get("root") or "") == "/"
        and bool(dup_files)
        and all("dup" in str(x.get("path") or "") for x in dup_files)
        and all(int(x.get("size") or 0) <= 1024 for x in dup_files)
    )
    if looks_test:
        return _item("百度清单", "warn",
                     f"清单**疑似测试数据**(root=/、dupN 形状),文件 {p};"
                     "重扫用 `python scripts/baidu_dedupe.py`")
    if age is not None and age > 48:
        return _item("百度清单", "warn",
                     f"清单已 {age}h 未更新(阈值 48h):{p}")
    return _item("百度清单", "ok",
                 f"{data.get('n_groups', 0)} 组 / 多出 {data.get('n_dup', 0)} 份 / "
                 f"{int(data.get('bytes') or 0) / 2 ** 30:.2f} GiB,{age}h 前")


def _check_config(settings) -> dict:
    """配置是否处在"真的会干活"的状态。"""
    enabled = bool(getattr(settings, "pan_auto_clean_enabled", False))
    dry = bool(getattr(settings, "pan_auto_clean_dry_run", True))
    cap = int(getattr(settings, "pan_auto_clean_max_deletes", 30) or 0)
    budget = int(getattr(settings, "xunlei_dedupe_budget", 800) or 0)
    detail = f"自动清理={'开' if enabled else '关'},dry_run={'开' if dry else '关'},单轮上限={cap},周去重预算={budget}"
    if not enabled:
        return _item("清理配置", "warn", detail + ";清理作业不会执行")
    if dry:
        return _item("清理配置", "warn", detail + ";dry-run 开着,不会真删")
    if cap < 100:
        return _item("清理配置", "warn", detail + ";单轮上限偏小,积压可能追不上新增")
    if budget < 2000:
        return _item("清理配置", "warn", detail + ";周去重预算对 2500+ 目录偏小,可能每次撞上限")
    return _item("清理配置", "ok", detail)


def run_checks(db: Session, user_id: int, settings=None) -> dict:
    """跑一遍自检(**只读**)。返回 `{"status", "checks", "warnings", "checked_at"}`。"""
    from config.settings import get_settings

    settings = settings or get_settings()
    checks = [
        *(_check_job_age(db, user_id, kind, max_h)
          for kind, max_h in _JOB_MAX_AGE_H.items()),
        *(_check_run_detail(db, user_id, kind) for kind in _JOB_MAX_AGE_H),
        _check_quota(db, user_id),
        _check_reuse(db, user_id, int(getattr(settings, "pan_self_check_reuse_days", 7) or 7)),
        _check_baidu_report(getattr(settings, "pan_auto_clean_baidu_report_path",
                                    "data/pan_dedupe_baidu.json")),
        _check_config(settings),
    ]
    warnings = [c for c in checks if c["level"] in ("warn", "error")]
    status = "error" if any(c["level"] == "error" for c in checks) else (
        "warn" if warnings else "ok")
    return {"status": status, "user_id": user_id,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "checks": checks, "warnings": warnings}


def _format_detail(out: dict) -> str:
    return " | ".join(f"[{c['level']}] {c['name']}: {c['detail']}" for c in out["checks"])


def self_check_all_users(settings=None) -> int:
    """计划任务入口:对全部启用用户各跑一轮自检。返回**有异常的用户数**。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User
    from app.services.tenant_base import _record_run

    settings = settings or get_settings()
    if not getattr(settings, "pan_self_check_enabled", True):
        return 0
    db = get_session_local()()
    warned = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                out = run_checks(db, uid, settings)
                detail = _format_detail(out)
                _record_run(db, uid, "pan_self_check",
                            "failed" if out["status"] == "error" else "success",
                            detail[:900])
                db.commit()
                if out["warnings"]:
                    warned += 1
                    from app.services.alert_service import notify_incident

                    notify_incident(
                        db, uid, "pan_clean",
                        f"网盘自检:{len(out['warnings'])} 项需要处理",
                        "\n".join(f"· {c['name']}:{c['detail']}"
                                  for c in out["warnings"])[:1500],
                        settings=settings)
            except Exception:  # noqa: BLE001 - 单用户自检失败不影响其余
                db.rollback()
                logger.exception("网盘自检失败 user=%s", uid)
    finally:
        db.close()
    return warned
