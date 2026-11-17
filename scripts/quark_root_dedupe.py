# -*- coding: utf-8 -*-
"""夸克盘 **根目录重复**清理:按「重复 + 很老旧 + 没人推」出计划 / 执行(2026-11-17)。

## 为什么单列这一条 —— 盘满的主因不在这 19 个家,而在**根目录**

实测(2026-11-17):
* 19 个 `redian监听_*` 家共约 **1044 个顶层项 / 5028 GiB(≈5 TiB)**;
* 根目录 `pdir_fid=0` 有 **250540** 个顶层条目(其中 `file_type=1` 文件 **208050** 个);
* 配额 `used = 106.01 TiB` ⇒ **家的部分只占 5%**,现有两条清理链(`pan_auto_clean`
  只扫两层、`pan_retention` 只按监控源)**完全看不到根目录**。

根目录按 `size:desc` 的前 10200 条(接口 `_page` 到 103 就 `61004`,窗口就这么多)
合计 **12560 GiB**,其中 **9022 份是重复副本(10865 GiB)**。

## 判据

* **重复**:同「`_pkg_base(文件名主段)` + 扩展名 + 字节数」的多份。实测 10200 份里
  **8830 份带 `(N)` 后缀**,且**没有一份不带后缀的原件** —— 夸克重名 saveas 只加后缀。
* **很老旧**:`l_created_at`(真入库时间)已 573~849 天;`created_at` 是**操作时间**
  (2026-08-13 批量),不能拿来判新旧。
* **没人推**:① fid 不在 `pan_owned_shares.first_fid`;② 整名不等于任何分享标题;
  ③ 整名不在 `push_ledger.title`。实测 300 份抽样命中 **0**
  (⚠️ 子串匹配全是假阳性:`'4'`、`'9'` 这种单字符标题会命中一切,所以只认**整名相等**)。
* **一票否决**:`is_promo_name` 命中的不动(引流文件不在本脚本职责内)。

## 安全边界

* 删除**永远进回收站**(`to_recycle=True` 硬编码);
* **配额实测会立即释放**(删 597.19 MiB 文件后 `used` 当场 -597.19 MiB)
  ⇒ **回收站不占配额**,所以"进回收站"真的是可逆的、不是把账挪到别处;
* 每组**至少留 1 份**(优先留 `l_created_at` 最老的那份);
* 计划先落盘(`data/quark_root_dup_plan_live.json`),`--yes` 才真删;
* 删前**现场重取**保护名单(`quark_dup.refresh_protect`),不用过期快照。

用法::

    PYTHONIOENCODING=utf-8 python scripts/quark_root_dedupe.py            # 只出计划
    PYTHONIOENCODING=utf-8 python scripts/quark_root_dedupe.py --yes      # 真删
    ... --scan-out data/_qk_root_files.json --max-pages 103
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import get_session_local                             # noqa: E402
from app.services import pan_accounts as A                       # noqa: E402
from app.services import quark_dup as qd                         # noqa: E402
from app.services.quark_transfer import QuarkTransfer            # noqa: E402

DEFAULT_SCAN = Path("data/_qk_root_files.json")
PAGE_SIZE = 100
GAP = 2.5

#: 快照最多能用多久。**它必须短于「两次清理之间的间隔」**,否则就是在用
#: 删除前的视图算删除计划 —— `file/sort` 只返回现存文件,被删掉的不会出现在里面
#: ⇒ 旧快照里的 `keep` 可能早已不在盘上,再按它删就是删掉**仅剩的一份**。
#: 手工跑的话,超过这个年龄就强制 `--rescan`。
MAX_SNAPSHOT_AGE_H = 6.0


def _key(name: str) -> tuple[str, str]:
    """配对键的左半:`_pkg_base(主段)` + 小写扩展名(大小由调用方拼上)。"""
    n = str(name or "").strip()
    stem, ext = (n.rsplit(".", 1) + [""])[:2] if "." in n else (n, "")
    return qd._pkg_base(stem).lower(), ext.lower()


def scan_root(qt, out: Path, max_page: int, resume: bool = False) -> list[dict]:
    """按 `size:desc` 翻根目录(带退避与续跑)。返回条目列表。

    ⚠️ 接口的 `_page` 到 **103**(`_size=100`)就恒回 `61004` —— 不是限流退避能绕过的,
    是**窗口上限**。所以这里翻到失败为止,并把已拿到的部分存盘。
    """
    state = {"total": None, "items": [], "next_page": 1}
    if resume and out.exists():
        state = json.loads(out.read_text(encoding="utf-8"))
        print(f"续跑:{len(state['items'])} 条,从 page {state['next_page']}", flush=True)
    items: list[dict] = state["items"]
    page = int(state["next_page"])
    while page <= max_page:
        got = None
        for attempt in range(5):
            try:
                d = qt._request("GET", "/1/clouddrive/file/sort", api="https://drive-pc.quark.cn",
                                params={"_page": page, "_size": PAGE_SIZE, "_sort": "size:desc",
                                        "pdir_fid": "0", "_fetch_total": 1})
                got = (d.get("data") or {}).get("list") or []
                if state["total"] is None:
                    state["total"] = (d.get("metadata") or {}).get("_total")
                break
            except Exception as exc:                    # noqa: BLE001 - 抖动退避
                wait = 20 + attempt * 15
                print(f"  page {page} 第 {attempt + 1} 次失败,退避 {wait}s:{str(exc)[:56]}",
                      flush=True)
                time.sleep(wait)
        if got is None:
            print(f"page {page} 连续失败 ⇒ 停(通常是到了窗口上限)", flush=True)
            break
        if not got:
            print(f"page {page} 空页 ⇒ 到底", flush=True)
            break
        for it in got:
            items.append({"name": str(it.get("file_name") or ""), "dir": bool(it.get("dir")),
                          "fid": str(it.get("fid") or ""), "size": int(it.get("size") or 0),
                          "created": int(it.get("l_created_at") or it.get("created_at") or 0)})
        page += 1
        state["next_page"] = page
        out.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        if page % 20 == 0:
            print(f"  page {page} 累计 {len(items)}/{state['total']} "
                  f"当前 {items[-1]['size'] / 2 ** 20:.0f}MiB", flush=True)
        time.sleep(GAP)
    return items


def load_protect(db, account: str) -> tuple[set[str], set[str], set[str]]:
    """`(分享 first_fid, 分享标题, 推送台账标题)` —— **按账号**取分享侧。"""
    from sqlalchemy import text

    shr = db.execute(text(
        "select title, first_fid from pan_owned_shares "
        "where platform='quark' and status=1 and account_key=:k"), {"k": account}).all()
    led = db.execute(text("select title from push_ledger where pan_type='夸克'")).all()
    fids = {str(r[1]) for r in shr if r[1]}
    titles = {(r[0] or "").strip() for r in shr if r[0]}
    ledger = {(r[0] or "").strip() for r in led if r[0]}
    return fids, titles, ledger


def build_plan(files: list[dict], share_fids: set[str], share_titles: set[str],
               ledger: set[str]) -> tuple[list[dict], dict]:
    """把条目分成若干「同资源」组,每组留 1 份,其余进计划。"""
    groups: dict[tuple[str, str, int], list[dict]] = defaultdict(list)
    for x in files:
        groups[(_key(x["name"]) + (int(x["size"]),))].append(x)

    plan: list[dict] = []
    prot_groups = prot_gib = 0.0
    for (base, ext, size), g in groups.items():
        if len(g) < 2 or size <= 0 or any(qd.is_promo_name(x["name"]) for x in g):
            continue
        g = sorted(g, key=lambda x: x.get("created") or 0)
        prot = [x for x in g if x["fid"] in share_fids
                or x["name"] in share_titles or x["name"] in ledger]
        keep = prot[0] if prot else g[0]
        for x in g:
            if x["fid"] == keep["fid"]:
                continue
            if prot:
                prot_groups += 1
                prot_gib += size / 2 ** 30
            plan.append({"fid": x["fid"], "name": x["name"], "size": size,
                         "keep_fid": keep["fid"], "keep_name": keep["name"],
                         "base": base, "copies": len(g), "protected": bool(prot)})
    stats = {"groups": len(groups), "protected_items": int(prot_groups),
             "protected_gib": round(prot_gib, 2)}
    return plan, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真删(默认只出计划)")
    ap.add_argument("--account", default="quark", help="账号 cookie_key(默认 quark)")
    ap.add_argument("--scan-out", default=str(DEFAULT_SCAN), help="根目录扫描快照(没有就现扫)")
    ap.add_argument("--max-pages", type=int, default=110, help="扫描页数上限(接口窗口约 103 页)")
    ap.add_argument("--rescan", action="store_true", help="忽略已有快照,重新扫")
    ap.add_argument("--plan-out", default="data/quark_root_dup_plan_live.json")
    ap.add_argument("--batch", type=int, default=200, help="每批删除条数")
    args = ap.parse_args()

    db = get_session_local()()
    pool = A.refs_for(db, "quark", user_id=1)
    if not pool:
        print("池里没有夸克账号")
        db.close()
        return 1
    pair = next(((r, c) for r, c in pool if str(r.cookie_key or "quark") == args.account), None)
    if pair is None:
        print(f"池里没有 {args.account};有:{[str(r.cookie_key) for r, _ in pool]}")
        db.close()
        return 1
    ref, ck = pair
    account = str(ref.cookie_key or "quark")
    qt = QuarkTransfer(ck)

    scan_path = Path(args.scan_out)
    if args.rescan or not scan_path.exists():
        print(f"[扫描] 根目录 size:desc,每页 {PAGE_SIZE}、间隔 {GAP}s(约 5 分钟)", flush=True)
        files = scan_root(qt, scan_path, args.max_pages)
    else:
        # ★ 快照必须晚于上一次删除。`file/sort` 只返回**现存**文件 ⇒ 拿删除前的
        # 快照算计划,会把「keep 已经不在盘上」的组当成还有留存份 ⇒ **删掉独苗**。
        # 实测 2026-11-18:`_qk_root_files.json`(11-17 19:08)算出的 961 个 keep 里
        # **721 个已落在历史已删 fid 里**,而它是这个脚本的默认快照。
        age_h = (time.time() - scan_path.stat().st_mtime) / 3600
        if age_h > MAX_SNAPSHOT_AGE_H:
            print(f"⚠️ 快照 {scan_path} 已 {age_h:.1f} 小时(>{MAX_SNAPSHOT_AGE_H}h)"
                  f" —— 可能早于上一次删除,拿它算计划会删掉独苗。\n"
                  f"   加 --rescan 重扫,或改用 scripts/quark_global_dedupe.py"
                  f"(双视图 + 原件优先)。")
            db.close()
            return 3
        raw = json.loads(scan_path.read_text(encoding="utf-8"))
        files = [x for x in (raw.get("items") or []) if not x.get("dir")]
        print(f"[快照] {scan_path} -> {len(files)} 个文件条目"
              f"({sum(x['size'] for x in files) / 2 ** 30:.1f} GiB),{age_h:.1f}h 前")

    files = [x for x in files if not x.get("dir") and x.get("fid")]
    share_fids, share_titles, ledger = load_protect(db, account)
    db.close()
    print(f"[保护] 分享 first_fid {len(share_fids)} / 分享标题 {len(share_titles)}"
          f" / 台账标题 {len(ledger)}")

    plan, stats = build_plan(files, share_fids, share_titles, ledger)
    free = [p for p in plan if not p["protected"]]
    free_gib = sum(p["size"] for p in free) / 2 ** 30
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"), "account": account,
           "scanned": len(files), "scanned_gib": round(sum(x["size"] for x in files) / 2 ** 30, 2),
           "groups": stats["groups"], "n": len(plan), "gib": round(sum(p["size"] for p in plan) / 2 ** 30, 2),
           "n_free": len(free), "gib_free": round(free_gib, 2), "items": plan}
    Path(args.plan_out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n扫描 {len(files)} 份 / {out['scanned_gib']:.1f} GiB,分组 {stats['groups']} 组")
    print(f"⇒ 待删 {len(free)} 份 / **{free_gib:.2f} GiB**"
          f"(另有 {stats['protected_items']} 份因受保护拿不到 {stats['protected_gib']:.2f} GiB)")
    print(f"计划已落盘 {args.plan_out}")
    for p in sorted(free, key=lambda x: -x["size"])[:20]:
        print(f"  {p['size'] / 2 ** 30:8.2f}GiB x{p['copies']:<3d} {p['name'][:64]}")

    if not args.yes:
        print("\n(dry-run,未删除;加 --yes 才真删 —— 删除进回收站,40 天内可捞回)")
        return 0
    if not free:
        print("没有可删的")
        return 0

    before = qt.quota_usage()
    deleted = 0
    errors: list[str] = []
    for i in range(0, len(free), args.batch):
        chunk = free[i:i + args.batch]
        try:
            res = qt.delete_files([c["fid"] for c in chunk], to_recycle=True)
            n = int((res or {}).get("count") or len(chunk))
            deleted += n
            print(f"  已删 {deleted}/{len(free)}(进回收站)", flush=True)
        except Exception as exc:                        # noqa: BLE001 - 单批失败不中断
            errors.append(f"{type(exc).__name__}:{str(exc)[:70]}")
            print(f"  这批失败:{str(exc)[:90]}", flush=True)
        time.sleep(1.2)

    after = qt.quota_usage()
    print(f"\n实际删除 {deleted} 份" + (f",失败 {len(errors)} 批" if errors else ""))
    if before and after:
        print(f"配额: {before['used'] / 2 ** 40:.3f} TiB → {after['used'] / 2 ** 40:.3f} TiB"
              f"(-{(before['used'] - after['used']) / 2 ** 30:.1f} GiB)")
    for e in errors[:5]:
        print("   ", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())