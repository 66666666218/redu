# -*- coding: utf-8 -*-
"""夸克盘清理:按用户口径「**重复 + 很老旧 + 没人推**」出计划 / 执行(2026-11-17)。

## 口径与判据(每一条都是实测得出的,不是想当然)

用户原话:「你开始清理夸克吧,把一些**重复**和**很老旧没人推**的删了」。

* **重复** —— 复用 `quark_dup.classify`(跨家同名同大小)。实测全盘只有 **17 组 / 1.29 GiB**,
  这是"聊胜于无"的那部分;**盘满的主因不在这里**。
* **很老旧** —— 顶层目录的 `updated_at`/`created_at` 距今天数(`--min-age-days`,默认 30)。
* **没人推** —— 三重证据**同时**不成立才判"没推过":
  1. 目录名不在我方分享标题里(`pan_owned_shares.title`);
  2. 目录内**任一文件** fid 不是某条链的 `first_fid`(递归比对);
  3. 目录名不在推送台账里(`push_ledger.title`)。
  ⚠️ 为什么不直接查台账:`push_ledger` **只从 2026-10-18 起有记录**,之前的链是台账盲区;
  而本流水线**推送必有分享**(转存 → 建分享 → 推送)⇒「没被我方分享」是比台账更硬的判据。
* **一票否决**:能匹配到**监控源**(`xunlei_cleanup._source_latest`)的一律不删 ——
  说明它还在被重发,是活的。

## 为什么不用现成的两条链

* `pan_auto_clean`(去重):只扫 `redian监听_*` **两层**,且判据只有"重复" ⇒ 实测每天 `计划0`;
* `pan_retention`(保留期):判据是"监控源 N 天没人重发",而盘上 88% 的目录名
  **匹配不到任何监控源** ⇒ 也是 `计划 0`。
⇒ 用户这个口径这两条都表达不了,所以单列一个脚本,**不重写任何已有判据**
(保护/匹配全部从 `quark_dup` / `pan_retention` / `xunlei_cleanup` 引)。

## 安全边界

* 删除**永远进回收站**(`to_recycle=True` 硬编码),SVIP 40 天内可捞回;
* 只删**家下面的资源目录**,家本身、简介目录、我方分享的目录都不碰;
* 计划**先落盘**(`data/quark_prune_plan.json`),`--yes` 才真删;
* 删完把受影响的家作废快照。

用法::

    PYTHONIOENCODING=utf-8 python scripts/quark_prune_old.py            # 只出计划
    PYTHONIOENCODING=utf-8 python scripts/quark_prune_old.py --yes      # 真删
    ... --min-age-days 30 --homes redian监听_0913_2,redian监听_0915
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import get_session_local                             # noqa: E402
from app.services import pan_accounts as A                       # noqa: E402
from app.services import pan_retention as _R                     # noqa: E402
from app.services import quark_dup                               # noqa: E402
from app.services import xunlei_cleanup as xc                    # noqa: E402
from app.services.quark_transfer import QuarkTransfer            # noqa: E402


def _load_share_index(db, account_key: str) -> tuple[set[str], set[str]]:
    """该账号的 `(分享标题, 分享 first_fid)`。**只算这个号的链** —— 两个号的家不通用。"""
    from sqlalchemy import text

    rows = db.execute(text(
        "select title, first_fid from pan_owned_shares "
        "where platform='quark' and status=1 and account_key=:k"), {"k": account_key}).all()
    titles = {(r[0] or "").strip() for r in rows if r[0]}
    fids = {str(r[1]) for r in rows if r[1]}
    return titles, fids


def _load_ledger_titles(db, account_key: str) -> set[str]:
    """推送台账里出现过的标题。⚠️ 不按账号分(台账没有 account_key 列)。"""
    from sqlalchemy import text

    rows = db.execute(text(
        "select title from push_ledger where pan_type='夸克'")).all()
    return {(r[0] or "").strip() for r in rows if r[0]}


def _top_dirs_of_home(qt, home_fid: str, max_items: int = 5000) -> list[dict]:
    """一个家**顶层**的条目(资源目录/文件)。"""
    try:
        items = qt.list_dir(home_fid)
    except Exception as exc:                        # noqa: BLE001 - 单个家列不动不影响其余
        print(f"    列目录失败:{type(exc).__name__}: {str(exc)[:60]}", flush=True)
        return []
    out = []
    for it in items[:max_items]:
        ts = int(it.get("updated_at") or 0) or int(it.get("created_at") or 0)
        out.append({
            "name": str(it.get("file_name") or ""), "fid": str(it.get("fid") or ""),
            "dir": bool(it.get("dir")), "ts": ts,
            "ts_h": datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else "",
        })
    return out


def _walk_fids(qt, fid: str, cap: int = 3000) -> set[str]:
    """递归收集一个目录下**所有文件**的 fid(判「有没有被我方分享链指着」)。"""
    budget = {"n": 0, "cap": cap}
    try:
        fs = quark_dup._walk(qt, fid, "x", "x", budget, 0, 4)
    except Exception:                               # noqa: BLE001 - 列不动就当没有
        return set()
    return {str(f.get("fid") or "") for f in fs if f.get("fid")}


def _fids_from_snapshot(home: str, top: str, snap: dict) -> set[str] | None:
    """从 `quark_dup_cache` 快照里取「某个家下某个顶层目录」里的**全部文件 fid**。

    ⚠️ **为什么要有这条**:`_walk_fids` 每判一个目录就一次 `list_dir`(实测 1.2 秒),
    非现用家也还有 250 多个候选 ⇒ **一跑就是十几分钟**,而且中途撞到限流
    (`query fail [61004]`)。快照里已经有每个家的递归文件清单(depth=2),
    ⇒ 这一步**零 API 调用**。判的是"这份资源有没有被我方链指着",快照够用。

    ★★ **`None` = 这个家根本不在快照里(没扫过),不是"扫了但里面没有"**(2026-11-24)。
    快照是**增量**的(`quark_dup_cache` 的 `remaining_homes`,一轮只扫一部分家)——
    实测 2 号账号的快照里只有 `redian监听` 一个家。拿"没扫过"当"没有已分享文件",
    就会把那个家里**全部**目录列进删除计划。这两种情况必须分开,见调用方。
    """
    h = (snap.get("homes") or {}).get(home)
    if not h or "files" not in h:
        return None
    out: set[str] = set()
    for f in h.get("files") or []:
        parts = [p for p in str(f.get("path") or "").split("/") if p]
        if len(parts) >= 2 and parts[1] == top and f.get("fid"):
            out.add(str(f["fid"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真删(默认只出计划)")
    ap.add_argument("--min-age-days", type=int, default=30)
    ap.add_argument("--homes", default="", help="只看这些家(逗号分隔;空=全部)")
    ap.add_argument("--plan-out", default="data/quark_prune_plan.json")
    ap.add_argument("--include-current-home", action="store_true",
                    help="连**现用家**(名字里最新的那个)一起清;默认跳过")
    ap.add_argument("--offline", action="store_true",
                    help="用 quark_dup_cache 快照判「内含已分享文件」,零额外 API")
    args = ap.parse_args()

    db = get_session_local()()
    pool = A.refs_for(db, "quark", user_id=1)
    if not pool:
        print("池里没有夸克账号")
        return 1

    now = datetime.now()
    sources = xc._source_latest(db, 1)
    print(f"监控源条目 {len(sources)}")
    want_homes = {h.strip() for h in args.homes.split(",") if h.strip()}
    plan_all: list[dict] = []

    for ref, ck in pool:
        key = str(ref.cookie_key or "quark")
        titles, share_fids = _load_share_index(db, key)
        ledger = _load_ledger_titles(db, key)
        print(f"\n=== {key} / {ref.label}:分享标题 {len(titles)} / first_fid {len(share_fids)}"
              f" / 台账标题 {len(ledger)}", flush=True)
        #: ★ **快照必须按账号分开读**(2026-11-24 修):文件名与 `pan_auto_clean` 同一套
        #: (`pan_accounts.scoped_path`)。固定读 `quark_dup_cache.json` 的话,2 号账号会拿
        #: **1 号的家名+fid** 去比对 —— 同名不同 fid,`_fids_from_snapshot` 一条都取不到
        #: ⇒ 每个目录都被判成「内含已分享文件 0 个」⇒ **把还在用的资源列进删除计划**。
        snap: dict = {}
        if args.offline:
            sp = Path(A.scoped_path("data/quark_dup_cache.json", key, platform="quark"))
            if not sp.exists():
                print(f"  [{key}] 没有快照({sp}),先跑一次 pan_auto_clean 或去掉 --offline")
                continue
            snap = json.loads(sp.read_text(encoding="utf-8"))
            print(f"  离线快照 {len(snap.get('homes') or {})} 个家 ← {sp.name}")
        qt = QuarkTransfer(ck)
        homes = quark_dup.list_homes(qt, "redian监听")
        if not homes:
            print("  没有家目录")
            continue
        newest = str(homes[0].get("file_name") or "")
        for h in homes:
            nm = str(h.get("file_name") or "")
            if want_homes and nm not in want_homes:
                continue
            if not want_homes and not args.include_current_home and nm == newest:
                print(f"  [{nm}] 现用家,跳过(--include-current-home 才清)")
                continue
            items = _top_dirs_of_home(qt, str(h.get("fid") or ""))
            kept = drop = 0
            unscanned = 0
            reasons: dict[str, int] = {}
            for it in items:
                if not it["dir"] or not it["fid"] or not it["name"]:
                    continue
                age = (now - datetime.fromtimestamp(it["ts"] / 1000)).days if it["ts"] else 0
                why_stop = ""
                if age < args.min_age_days:
                    why_stop = f"不够老({age}天)"
                elif it["name"] in titles:
                    why_stop = "目录名是我方分享标题"
                elif it["name"] in ledger:
                    why_stop = "目录名在推送台账里"
                else:
                    ok, _last = _R._match(it["name"], sources)
                    if ok:
                        why_stop = "仍在被监控源重发"
                if not why_stop and share_fids:
                    inner = (_fids_from_snapshot(nm, it["name"], snap) if snap
                             else _walk_fids(qt, it["fid"]))
                    #: ★ `None` = 这个家**没扫过快照**(增量扫描,一轮只扫一部分家)。
                    #: 拿它当"没有已分享文件"会把这个家的全部目录列进删除计划 ——
                    #: 离线模式下遇到没扫过的家,**整家保留**,并把原因打出来。
                    if inner is None:
                        unscanned += 1
                        kept += 1
                        reasons["没扫过快照"] = reasons.get("没扫过快照", 0) + 1
                        continue
                    if inner & share_fids:
                        why_stop = f"内含 {len(inner & share_fids)} 个已分享文件"
                if why_stop:
                    kept += 1
                    #: 按**主因**归类(去掉括号里的数字,避免同一原因被拆成多行)
                    k = why_stop.split("(")[0]
                    reasons[k] = reasons.get(k, 0) + 1
                    continue
                drop += 1
                plan_all.append({"account": key, "home": nm, "name": it["name"],
                                 "fid": it["fid"], "age_days": age, "date": it["ts_h"]})
            tail = (f" (其中 {unscanned} 个**没扫过快照**、整家保留)" if unscanned else "")
            print(f"  [{nm}] 顶层 {len(items)} 项 ⇒ 保留 {kept} / **待删 {drop}**{tail}",
                  flush=True)
            #: ★ 保留原因必须打出来。只报「待删 0」回答不了用户的「为什么没得清」 ——
            #: 而「待删 0」最常见的真因恰恰是**保护面覆盖了全部目录**(见 §清单口径)。
            for k, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
                print(f"     保留原因:{k} × {n}", flush=True)

    out = {"generated_at": now.isoformat(timespec="seconds"),
           "min_age_days": args.min_age_days,
           "accounts_homes_scope": args.homes or "(除现用家以外的全部家)",
           "n": len(plan_all), "items": plan_all}
    Path(args.plan_out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n计划已落盘 {args.plan_out}:{len(plan_all)} 个目录")
    for p in sorted(plan_all, key=lambda x: -x["age_days"])[:40]:
        print(f"  {p['age_days']:4d}天 {p['date']} [{p['home']:16s}] {p['name'][:50]}")

    if not args.yes:
        print("\n(dry-run,未删除;加 --yes 才真删 —— 删除进回收站,40 天内可捞回)")
        db.close()
        return 0
    if not plan_all:
        print("没有可删的")
        db.close()
        return 0

    deleted = 0
    errors: list[str] = []
    for ref, ck in pool:
        key = str(ref.cookie_key or "quark")
        mine = [p for p in plan_all if p["account"] == key]
        if not mine:
            continue
        qt = QuarkTransfer(ck)
        for i in range(0, len(mine), 50):
            chunk = mine[i:i + 50]
            try:
                res = qt.delete_files([c["fid"] for c in chunk], to_recycle=True)
                n = int((res or {}).get("count") or len(chunk))
                deleted += n
                print(f"  {key} 已删 {n} 个目录(进回收站)", flush=True)
            except Exception as exc:                # noqa: BLE001 - 单批失败不中断
                errors.append(f"{key}:{type(exc).__name__}:{str(exc)[:70]}")
                print(f"  {key} 这批失败:{str(exc)[:90]}", flush=True)
    print(f"\n实际删除 {deleted} 个目录" + (f",失败 {len(errors)} 批" if errors else ""))
    for e in errors[:5]:
        print("   ", e)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
