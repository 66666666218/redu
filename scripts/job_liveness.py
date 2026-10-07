"""作业落实性对账 —— **薄壳**,逻辑在 `app/services/job_liveness.py`(2026-10-07 移过去)。

    python scripts/job_liveness.py            # 当前 SCHEDULER_ROLE 下的全部作业
    python scripts/job_liveness.py --strict   # 有"从没跑过/已超期"的作业时退出码 1

⚠️ 逻辑为什么不在这个脚本里:① 服务层不能依赖 `scripts/`(Docker 镜像根本不 COPY 它);
② 更要紧的是**它已经接进每天的链路体检**(`chain_health.check_job_liveness`),
   靠人手动跑脚本的守卫等于没有守卫 —— 这条教训本仓吃过不止一次。
   本脚本保留,是为了"想细看时**能**细看"。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Windows 控制台默认 GBK,直接 print 非 ASCII(如 ❌)→ UnicodeEncodeError。
# ⚠️ 本脚本**就是靠 ❌ 那几行报问题**,缺了它不是"显示难看",而是**报错清单打不出来**。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="有问题时退出码 1")
    args = ap.parse_args()

    from app.db.database import get_session_local, init_db
    from app.services import job_liveness as jl

    # ⚠️ **必须先 init_db()**:`first_seen_at` 是后加的列,而 `_migrate()` 只在应用启动时跑。
    # 独立跑本脚本时若不补这一步,就会在只有旧表的库上炸
    # `no such column: job_heartbeats.first_seen_at` —— 对账脚本自己先挂,等于没对账。
    init_db()

    with get_session_local()() as db:
        r = jl.audit(db)

    print(f"{'作业':<26}{'上次执行':<18}{'跑了':>6}{'错':>5}{'上次耗时':>10}  期望最大空档")
    print("-" * 95)
    for j in r["jobs"]:
        b = r["beats"].get(j.id)
        iv = jl.trigger_interval_seconds(j.trigger)
        last_txt = (b.last_run_at.strftime("%m-%d %H:%M") if b and b.last_run_at
                    else "—— 无记录 ——")
        dur = getattr(b, "last_duration_ms", None) if b else None
        print(f"{j.id:<26}{last_txt:<18}{b.run_count if b else 0:>6}"
              f"{b.error_count if b else 0:>5}{f'{dur / 1000:.1f}s' if dur else '—':>10}"
              f"  {f'{iv / 3600:.2f}h' if iv else '—'}")

    print()
    if r["baseline"] is not None:
        print(f"(心跳基线 {r['baseline']:%m-%d %H:%M} —— 早于它跑过的作业查不到记录,别当成「没跑」)")
    if r["never"]:
        print(f"❌ **注册了却从没执行过**({len(r['never'])} 个):{', '.join(r['never'])}")
        print("   → 作业注册成功 ≠ 它跑过。查 trigger / 是否被角色挡下 / 是否每次都提前 return。")
    if r["not_yet"]:
        print(f"· 无记录、但自基线起**还没到过触发点**({len(r['not_yet'])} 个):"
              f"{', '.join(r['not_yet'])}")
        print("   → 不是问题(每周作业 / 当天稍晚才到点的日作业),到点会自己补上。")
    if r["stale"]:
        print(f"⚠️ **执行间隔远超预期**({len(r['stale'])} 个):")
        for s in r["stale"]:
            print(f"   {s['job_id']}:上次 {s['last_run_at']:%m-%d %H:%M},"
                  f"已 {s['over_s'] / 3600:.1f}h(期望最大空档 {s['expect_s'] / 3600:.1f}h)")
    if not r["never"] and not r["stale"]:
        print("✅ 所有已注册作业都有心跳,且间隔正常。")
    return 1 if (args.strict and (r["never"] or r["stale"])) else 0


if __name__ == "__main__":
    raise SystemExit(main())
