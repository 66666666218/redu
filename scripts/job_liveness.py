"""作业落实性对账:**注册的作业** vs **真实心跳**(2026-10-04)。

用法:
    python scripts/job_liveness.py              # 当前 SCHEDULER_ROLE 下的全部作业
    python scripts/job_liveness.py --strict     # 有"从没跑过/已超期"的作业时退出码 1

**为什么有它**(一次真实的教训):`resource_presence` 注册着、trigger 正确
(`hour=9, minute=0`)、`enabled=True`,却**六天一次没跑** —— 而"小红书/快手每天跑"
这件事我们一直以为是成立的。之所以没人发现,是因为**作业的执行事实没有统一落点**:
有的把痕迹写进 `runs` 但**换个名字**(`wechat_collect_tick` → `wechat_listen`),
有的**压根不写**。于是"配置说每天跑"和"实际跑没跑"之间无从对照。

现在每个作业由 `scheduler._add_job` **自动落一条心跳**(`job_heartbeats` 表),
本脚本把两边并排打出来 —— **注册了却没心跳 = 它根本没执行过**。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402

from app.services.scheduler import build_jobs  # noqa: E402


def _interval_seconds(trigger) -> float | None:
    """从 cron 触发器**估**一个期望间隔(秒);估不出返回 None(不算超期)。

    只做保守估计:能确定性算出的才返回,拿不准的一律 None —— **宁可漏报也不误报**,
    否则"超期"会变成噪音然后被无视(和告警变噪音是同一个坑)。
    """
    f = getattr(trigger, "fields", None)
    if not f:
        return None
    try:
        mins = {x.name: str(x) for x in f}
    except Exception:  # noqa: BLE001
        return None
    m, h = mins.get("minute", "*"), mins.get("hour", "*")
    if m.startswith("*/"):
        try:
            return int(m[2:]) * 60
        except ValueError:
            return None
    if m == "*":
        return 60
    if m.isdigit() and h == "*":
        return 3600
    return None          # 每天/每周定点:间隔由"哪一天"决定,这里不算


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="有问题时退出码 1")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.db.database import get_session_local
    from app.db.models import JobHeartbeat

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)

    with get_session_local()() as db:
        beats = {b.job_id: b for b in db.scalars(select(JobHeartbeat)).all()}

    now = datetime.now()
    never, stale = [], []
    print(f"{'作业':<26}{'上次执行':<18}{'跑了':>6}{'错':>5}  期望间隔")
    print("-" * 82)
    for j in sorted(sched.get_jobs(), key=lambda x: x.id):
        b = beats.get(j.id)
        iv = _interval_seconds(j.trigger)
        last_txt = (b.last_run_at.strftime("%m-%d %H:%M") if b and b.last_run_at
                    else "—— 从没跑过 ——")
        cnt = b.run_count if b else 0
        err = b.error_count if b else 0
        iv_txt = f"{int(iv)}s" if iv else "—"
        print(f"{j.id:<26}{last_txt:<18}{cnt:>6}{err:>5}  {iv_txt}")
        if b is None:
            never.append(j.id)
        elif iv and b.last_run_at and (now - b.last_run_at).total_seconds() > iv * 3:
            stale.append((j.id, b.last_run_at))

    print()
    if never:
        print(f"❌ **注册了却从没执行过**({len(never)} 个):{', '.join(never)}")
        print("   → 作业注册成功 ≠ 它跑过。查 trigger / 是否被角色挡下 / 是否每次都提前 return。")
    if stale:
        print(f"⚠️ **执行间隔远超预期**({len(stale)} 个):")
        for jid, last in stale:
            print(f"   {jid}:上次 {last:%m-%d %H:%M}")
    if not never and not stale:
        print("✅ 所有已注册作业都有心跳,且间隔正常。")
    return 1 if (args.strict and (never or stale)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
