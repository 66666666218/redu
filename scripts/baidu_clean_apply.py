"""百度盘**重复副本**清理(默认 dry-run,2026-10-21)。

## 背景
百度盘 2026-10-21 实测**只剩 2.75 GiB / 99.95%**,`pan_discovery` 里 28 条候选
全部卡在 `转存空间不足(-32)`。而 `data/pan_dedupe_baidu.json` 里已经有
**258 组 / 多出 1381 份 / 114.49 GiB** 的重复副本清单。

## 安全设计(百度**没有回收站**,删了不可逆)
1. **默认只打印计划**,不加 `--yes` 一个字节都不删;
2. 只删清单里的 `dup_files`(**keep 那份永远不删**),逐条校验:
   绝对路径 + 路径 ≠ keep + 确实在清单里;
3. `--max N` 控制单批数量(先小批,确认空间真的回来了再继续);
4. `--only-ext apk` 之类可以进一步缩小范围;
5. 删完**立刻探一次配额**,把"腾出了多少"如实打出来。

用法:
    python scripts/baidu_clean_apply.py                 # 只看计划
    python scripts/baidu_clean_apply.py --max 20        # 只看前 20 条
    python scripts/baidu_clean_apply.py --only-ext apk  # 只看 APK 类
    python scripts/baidu_clean_apply.py --yes --max 20  # 真删前 20 条
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_PLAN = ROOT / "data" / "pan_dedupe_baidu.json"


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PiB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default=str(DEFAULT_PLAN))
    ap.add_argument("--max", type=int, default=0, help="本批最多删几条(0=全部)")
    ap.add_argument("--only-ext", default="", help="只处理这个扩展名(如 apk),空=全部")
    ap.add_argument("--yes", action="store_true", help="真删(默认只打印)")
    ap.add_argument("--user-id", type=int, default=1)
    args = ap.parse_args()

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"❌ 清单不存在:{plan_path}(先跑 python scripts/baidu_dedupe.py)", flush=True)
        return 2
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    dups = list(plan.get("dup_files") or [])
    if args.only_ext:
        ext = args.only_ext.lower().lstrip(".")
        dups = [x for x in dups if str(x.get("name") or "").lower().endswith("." + ext)]
    #: 大文件优先:腾空间最快,且它们的 keep 副本最可靠(同名同大小)。
    dups.sort(key=lambda x: -int(x.get("size") or 0))
    todo = dups[: args.max] if args.max > 0 else dups

    #: ★ 逐条校验:**不在清单里的、等于 keep 的、非绝对路径的,一律剔除**。
    clean: list[dict] = []
    for x in todo:
        p = str(x.get("path") or "")
        keep = str(x.get("keep") or "")
        if not p.startswith("/") or p == keep:
            continue
        clean.append(x)
    total = sum(int(x.get("size") or 0) for x in clean)
    print(f"清单 {plan_path.name}:共 {plan.get('n_dup', 0)} 份重复 / "
          f"{_human(float(plan.get('bytes') or 0))}"
          f"{'(⚠️ 清单没扫完,数字偏小)' if plan.get('truncated') else ''}", flush=True)
    print(f"本批 {len(clean)} 条 / {_human(total)}", flush=True)
    for x in clean[:15]:
        print(f"  - {_human(int(x.get('size') or 0)):>10}  {x['path']}", flush=True)
    if len(clean) > 15:
        print(f"  … 其余 {len(clean) - 15} 条", flush=True)
    if not clean:
        print("没有可删条目。", flush=True)
        return 0
    if not args.yes:
        print("\n(dry-run:没有删除任何东西。确认后加 --yes)", flush=True)
        return 0

    from app.db.database import get_session_local, init_db
    from app.services import pan_accounts
    from app.services.baidupan_transfer import BaiduPanClient

    init_db()
    db = get_session_local()()
    try:
        ck = (pan_accounts.cookie_for(db, "baidupan", user_id=args.user_id) or "").strip()
        if not ck:
            print("❌ 没有百度 Cookie", flush=True)
            return 1
        client = BaiduPanClient(ck)
        before = client.quota_ratio()
        try:
            n = client.delete_paths([str(x["path"]) for x in clean])
        except Exception as exc:  # noqa: BLE001 - 删失败如实报,别静默
            print(f"❌ 删除失败:{type(exc).__name__}: {exc}", flush=True)
            return 1
        try:
            after = client.quota_ratio()
        except Exception:  # noqa: BLE001
            after = None
        print(f"✓ 已提交删除 {n} 条", flush=True)
        if before is not None and after is not None:
            print(f"  使用率 {before * 100:.2f}% → {after * 100:.2f}%"
                  f"(本次计划 {_human(total)})", flush=True)
            print("  ⚠️ 百度删除是异步的,使用率可能稍后才更新;过几分钟再探一次。", flush=True)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
