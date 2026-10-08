"""把夸克清理计划**落到回收站**(2026-10-08)。

## 安全设计(删除不可逆,所以每一条都是故意的)
1. **默认 dry-run** —— 不加 `--yes` 时只打印计划,一个字节都不删;
2. **删进回收站**(`delete_files(..., to_recycle=True)`),不是彻底删 —— 可捞回;
3. 计划与报告**用同一份代码**(`quark_dup.build_plan`),不另写一套;
4. **没扫完要说出来** —— `complete=False` 时打警告,而且**默认拒绝执行**
   (没扫完 ⇒「只出现在一个包里」是假判断 ⇒ 计划不可信);
5. **一票否决**在 `classify` 里已经做了:我方分享链的 `first_fid`、我们自己的简介,永不删;
6. `--max N` 只处理前 N 个(先小批试)。

用法:
    python scripts/quark_dup_apply.py            # 只看计划(默认)
    python scripts/quark_dup_apply.py --max 5    # 只看前 5 个
    python scripts/quark_dup_apply.py --yes      # 真删(进回收站)
    python scripts/quark_dup_apply.py --yes --allow-partial   # 明知没扫完也要删
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app.services.quark_dup import (  # noqa: E402
    PROTECT_NAMES,
    apply_plan,
    build_plan,
    protected_fids,
)


def _load_plan(path: str) -> dict:
    """读一份已扫好的计划。⚠️ 扫一轮要 14 分钟,而计划是可复用的 ——
    但**保护名单必须在这一刻重新取**(它是会变的:这期间又发了新链)。"""
    import json

    import io

    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    from app.db import get_session_local
    from app.services.cookie_store import get_cookie
    from app.services.quark_transfer import QuarkTransfer
    from config.settings import get_settings

    yes = "--yes" in sys.argv
    allow_partial = "--allow-partial" in sys.argv
    plan_file = ""
    cap = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--max" and i + 1 < len(args):
            cap = int(args[i + 1])
        elif a == "--plan-file" and i + 1 < len(args):
            plan_file = args[i + 1]

    s = get_settings()
    db = get_session_local()()
    try:
        ck = get_cookie(db, 1, "quark") or getattr(s, "quark_cookie", "") or ""
    finally:
        db.close()
    qt = QuarkTransfer(ck, fid_store=getattr(s, "quark_fid_store", "") or None)

    plan = _load_plan(plan_file) if plan_file else build_plan(qt)
    # ⚠️ **保护名单现场重取**:计划可能是十几分钟前扫的,这期间又发了新链 ——
    # 用旧名单删就是拿一份过期的安全清单去删盘。
    pf, pn, ns = protected_fids(qt)
    dropped = [x for x in plan["delete"]
               if x["fid"] in pf or x["name"] in pn or x["name"] in PROTECT_NAMES]
    items = [x for x in plan["delete"]
             if x["fid"] not in pf and x["name"] not in pn and x["name"] not in PROTECT_NAMES]
    if dropped:
        print(f"🛡 现场保护名单({ns} 条分享)又拦下 {len(dropped)} 个候选"
              f"(计划是早先扫的):{[x['name'] for x in dropped[:5]]}")
    plan = {**plan, "delete": items}
    if cap is not None:
        items = items[:cap]
        plan = {**plan, "delete": items}
    plan["n_delete"] = len(items)
    plan["freed"] = sum(x["size"] for x in items)

    print(f"计划删除 {plan['n_delete']} 个文件(约 {plan['freed'] / 2 ** 30:.2f} GiB)"
          f"{'' if plan.get('complete', True) else ' —— ⚠️ **没扫完**'}"
          f"{' [--max %d]' % cap if cap is not None else ''}")
    for d in items[:30]:
        print(f"   ✂ {d['name'][:50]:50} ← {d['home']}  ({d['why']})")
    if len(items) > 30:
        print(f"   …… 还有 {len(items) - 30} 个")

    if not yes:
        print("\n(dry-run:什么都没删。要真删加 --yes;先小批可加 --max N)")
        return 0
    if not plan.get("complete", True) and not allow_partial:
        # ⚠️ 没扫完 ⇒「只出现在一个包里」是假判断 ⇒ 计划不可信。**默认拒绝**,而不是"提醒一下就删"。
        print("\n❌ 扫描没跑完(撞预算),计划不可信 —— 拒绝执行。"
              "抬 QUARK_CLEANUP_BUDGET 重跑,或明知故犯地加 --allow-partial。")
        return 2

    out = apply_plan(qt, plan)
    print(f"\n✅ 已删 {out['deleted']} 个(约 {out['bytes'] / 2 ** 30:.2f} GiB),全部进**回收站**可捞回")
    if out["failed"]:
        print(f"⚠️ {len(out['failed'])} 批失败:{out['failed'][:3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
