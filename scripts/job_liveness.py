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
本脚本把两边并排打出来。

⚠️ **"没有心跳"要再分两类**(2026-10-04 补):心跳机制**自己是哪天上线的**很重要 ——
在它上线之前跑过的作业当然没有记录。所以脚本会报出**心跳基线**,并把"没记录"的作业
按 `should_have_fired` 分成**真·漏跑**与**还没到点**(如每周作业、当天稍晚才到点的日作业)。
不这么分,一次就会列出 4 个**假问题** —— 而"重复劳动"正是本仓反复吃亏的那件事。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Windows 控制台默认 GBK,直接 print 非 ASCII(如 ❌)→ UnicodeEncodeError。
# ⚠️ 本脚本**就是靠 ❌ 那几行报问题**,所以缺了这行不是"显示难看",而是**报错清单打不出来**:
#    2026-10-05 实测它正好在打印"注册了却从没执行过"时崩掉,退出码非 0、清单全无。
#    (其余 scripts/ 下的脚本都有这一行,这个漏了。)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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


def _should_have_fired(trigger, since: datetime, now: datetime) -> bool:
    """自 `since` 起,这个 trigger **本该已经触发过**吗?

    ⚠️ **为什么不能只看"有没有心跳行"**(2026-10-04 实测踩到):心跳表是**当天 03:10 才上线**的
    (`data_cleanup` 是第一条),于是所有"每天/每周跑一次"的作业当天都显示"从没执行过" ——
    一次列出 4 个,**全是假的**:`douyin_leads`(每天 11:00)、`pan_discovery`(每天 11:30)
    当天晚些就会自己补上;`cross_account_discover`(周一/周四)、`recruit_reminder`(周一)
    本来就是今天(周日)还不到。**误报的代价是"重复劳动"** —— 这正是本仓今天刚反思过的那件事。

    判据交给 APScheduler 自己算(别手搓 cron 解析);**算不出就保守放行**,维持旧行为 ——
    宁可报,也别静默漏掉一个真·没跑的作业。

    ⚠️ 时区必须对齐:trigger 给的是 **aware**,而库里的 `baseline` 是 **naive**,
    直接比会 `TypeError` 把整个脚本打崩。所以比较也放进 try 里,任何意外一律走保守分支。
    """
    try:
        nxt = trigger.get_next_fire_time(None, since)
        if nxt is None:
            return True                      # 再也不会触发了(如 end_date 已过)= 该管
        a, b = nxt, now
        if a.tzinfo is not None and b.tzinfo is None:
            a = a.astimezone().replace(tzinfo=None)
        elif a.tzinfo is None and b.tzinfo is not None:
            b = b.astimezone().replace(tzinfo=None)
        return a <= b
    except Exception:  # noqa: BLE001 - 算不出就不改判
        return True


def classify_missing(jobs, beats, now: datetime, baseline: datetime | None
                     ) -> tuple[list[str], list[str]]:
    """把**没有心跳**的作业分成两类,别把"还没到点"报成"从没跑过"。

    返回 `(真·漏跑, 还没到点)`。

    ⚠️ **基线要按作业各算各的**(2026-10-05 修):此前一律用**全表最早一条**当 `since`,
    但那是"**心跳机制上线时刻**",不是"**这个作业的注册时刻**"。于是当天 13:10 才注册的
    `chain_report`(每天 09:30 触发)被误报成"注册了却从没执行过" —— 它只是还没到第一次点。
    **误报的代价是"重复劳动"**,正是本脚本 docstring 里反思过的那件事。

    现在优先用**该作业自己的** `first_seen_at`(`scheduler._register_beat` 在注册时写入),
    取不到才退回全表基线(旧库/旧行)。
    """
    missing: list[str] = []
    not_yet: list[str] = []
    for j in jobs:
        b = beats.get(j.id)
        if b is not None and b.last_run_at is not None:
            continue                      # 真跑过,不在这份清单里
        since = (b.first_seen_at if b is not None and b.first_seen_at else None) or baseline
        if since is not None and not _should_have_fired(j.trigger, since, now):
            not_yet.append(j.id)
        else:
            missing.append(j.id)
    return missing, not_yet


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="有问题时退出码 1")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.db.database import get_session_local, init_db
    from app.db.models import JobHeartbeat

    # ⚠️ **必须先 init_db()**:`first_seen_at` 这一列是后加的,而 `_migrate()` 只在应用启动时跑。
    # 独立跑本脚本时若不补这一步,就会在只有旧表的库上炸
    # `no such column: job_heartbeats.first_seen_at` —— 对账脚本自己先挂,等于没对账。
    init_db()

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)

    with get_session_local()() as db:
        beats = {b.job_id: b for b in db.scalars(select(JobHeartbeat)).all()}

    now = datetime.now()
    # 心跳基线 = 表里最早一条(机制上线时刻)。**早于它的"没跑过"是"查不到",不是真没跑。**
    baseline = min((b.last_run_at for b in beats.values() if b.last_run_at), default=None)

    jobs = sorted(sched.get_jobs(), key=lambda x: x.id)
    stale: list[tuple[str, datetime]] = []
    print(f"{'作业':<26}{'上次执行':<18}{'跑了':>6}{'错':>5}  期望间隔")
    print("-" * 82)
    for j in jobs:
        b = beats.get(j.id)
        iv = _interval_seconds(j.trigger)
        last_txt = (b.last_run_at.strftime("%m-%d %H:%M") if b and b.last_run_at
                    else "—— 无记录 ——")
        cnt = b.run_count if b else 0
        err = b.error_count if b else 0
        iv_txt = f"{int(iv)}s" if iv else "—"
        print(f"{j.id:<26}{last_txt:<18}{cnt:>6}{err:>5}  {iv_txt}")
        if b is not None and iv and b.last_run_at and (now - b.last_run_at).total_seconds() > iv * 3:
            stale.append((j.id, b.last_run_at))

    never, not_yet = classify_missing(jobs, beats, now, baseline)

    print()
    if baseline is not None:
        print(f"(心跳基线 {baseline:%m-%d %H:%M} —— 早于它跑过的作业查不到记录,别当成「没跑」)")
    if never:
        print(f"❌ **注册了却从没执行过**({len(never)} 个):{', '.join(never)}")
        print("   → 作业注册成功 ≠ 它跑过。查 trigger / 是否被角色挡下 / 是否每次都提前 return。")
    if not_yet:
        print(f"· 无记录、但自基线起**还没到过触发点**({len(not_yet)} 个):{', '.join(not_yet)}")
        print("   → 不是问题(每周作业 / 当天稍晚才到点的日作业),到点会自己补上。")
    if stale:
        print(f"⚠️ **执行间隔远超预期**({len(stale)} 个):")
        for jid, last in stale:
            print(f"   {jid}:上次 {last:%m-%d %H:%M}")
    if not never and not stale:
        print("✅ 所有已注册作业都有心跳,且间隔正常。")
    return 1 if (args.strict and (never or stale)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
