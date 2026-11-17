# -*- coding: utf-8 -*-
"""夸克盘**全盘**(不是根目录)大文件重复清理(2026-11-18)。

## 为什么前八轮清理都没触到主体

之前 `scripts/quark_root_dedupe.py` 扫的是 `pdir_fid="0"` 的根目录,而根目录的
`size:desc` 窗口**只有 10200 条**(`_page` 到 103 ⇒ `61004`)。实测根目录一共
**201727** 条,所以**约 19 万条从来没被看过**。

★★ **真正的入口是 `pdir_fid=""`(空字符串)** —— 它返回**全盘所有文件**:

| 请求 | `_total` | 含义 |
|---|---|---|
| `pdir_fid="0"` | 201727 | 根目录的**直接**子项 |
| `pdir_fid=""` + `file_type=1` | **1094602** | 全盘**所有文件** |
| `pdir_fid=""` + `file_type=0` | **148393** | 全盘**所有目录** |

全盘按 `size:desc` 取前 10000 个文件就是 **34.02 TiB** —— 这批文件**没有一个**
出现在根目录那 10200 条窗口里。`redian监听_*` 家 + 根目录加起来只有约 8 TiB,
所以 88 TiB 配额里有 **80 TiB 藏在这里**。

## 判据(两条,都是纯本地计算)

1. **同名 + 同字节数**(`size ≥ 1 GiB`):1501 组、多余 2704 份 ⇒ **7.42 TiB**。
   体积精确到字节相等 + 名字相同 ⇒ 同一份文件被保存到不同目录。
2. **父目录内「(文件名, 字节数) 集合」完全相同**:可回收 **1.56 TiB**。
   这是「整个课程/剧集目录被重复另存」的形状(如 170.19 GiB 那一套存了 4 份)。

## 安全边界

* **只删重复中的多余副本**,每组**留一份**:优先留**不带副本后缀的原件**,
  其次留**不在**受保护目录里的、创建更早的;
* 删除**永远进回收站**(`to_recycle=True`),配额当场释放、可捞回;
* **受保护**:`pan_owned_shares.first_fid`(我方分享的一级 fid)、整名等于分享标题、
  整名在 `push_ledger.title` 里的 —— 命中任一即整组跳过;
* `is_promo_name` 命中的不动(引流文件另有职责);
* 计划先落盘,`--yes` 才真删。

用法::

    PYTHONIOENCODING=utf-8 python scripts/quark_global_dedupe.py            # 只出计划
    PYTHONIOENCODING=utf-8 python scripts/quark_global_dedupe.py --yes      # 真删
"""
from __future__ import annotations

import argparse
import json
import re
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

SCAN = Path("data/_qk_global_top.json")
PAGE = 1000
WINDOW = 10                 # 实测 `_size=1000` 时 page 11 起 `61004`

#: 每个排序键的**翻页上限都是 offset 10000**,与 `_size` 无关(实测 `_size=50`
#: 时 page 202 能取、page 205 就 `61004`)。所以单个键只看得到 10000 条。
#:
#: ★ 但**不同键看到的 10000 条是不同的** —— 多键取并集能显著扩大覆盖。
#: 实测这 7 个键:size:desc 10000 → 加 created_at:asc 12998 → desc 22798
#: → updated:asc 32566 → file_name:asc 42310 → file_name:desc **52156**。
#: (目录那次 `_qk_alldirs.json` 就是用这个办法拿到 34455/42490 的。)
SORTS = ("size:desc", "created_at:asc", "created_at:desc",
         "updated:asc", "file_name:asc", "file_name:desc", "size:asc")


#: 全盘视图看不到的**低端**:全盘 `size:desc` 前 10000 条的下限是 **1.137 GiB**,
#: 而根目录(`pdir_fid="0"`)的 10200 条窗口能下探到 **0.583 GiB**。⇒ 0.33~1.14 GiB
#: 这一段只有根目录视图看得见。实测这一段里藏着 **2518+1245+191 个文件 / 2.16 TiB**,
#: 且 `quark_root_dup_plan_r2.json` 那 7804 份**(旧判据的 3.36 TiB)全部落在里面**。
ROOT_SORTS = ("size:desc", "created_at:desc", "file_name:asc", "file_name:desc")

#: `(标签, pdir_fid, 排序键)` —— 每一轮扫描都要把这两个视图**取并集**。
#: ⚠️ 别删任何一个:全盘视图漏低端(下限 1.137 GiB),根目录视图漏深层(窗口 10200 条)。
VIEWS = (("全盘", "", SORTS), ("根目录", "0", ROOT_SORTS))


def scan_global(qt: QuarkTransfer, out: Path) -> list[dict]:
    """全盘多键并集 + 根目录窗口并集(补 0.33~1.14 GiB 这段低端)。

    `pdir_fid=""` 才是全盘,`"0"` 只是根目录 —— 两者的窗口是**各自独立**的,
    所以取并集能同时拿到「深层的大家伙」和「根目录的中等个头」。
    """
    items: list[dict] = []
    seen: set[str] = set()
    for tag, pdir, sorts in VIEWS:
        for sort in sorts:
            page = 1
            while page <= WINDOW:
                got = None
                for _ in range(3):
                    try:
                        d = qt._request("GET", "/1/clouddrive/file/sort",
                                        api="https://drive-pc.quark.cn",
                                        params={"_page": page, "_size": PAGE, "_sort": sort,
                                                "pdir_fid": pdir, "file_type": 1,
                                                "_fetch_total": 1})
                        got = (d.get("data") or {}).get("list") or []
                        break
                    except Exception as exc:          # noqa: BLE001 - 窗口上限不是抖动
                        msg = str(exc)
                        if "61004" in msg or "66010" in msg:
                            got = "CAP"
                            break
                        print(f"    retry {msg[:56]}", flush=True)
                        time.sleep(10)
                if got is None or got == "CAP" or not got:
                    break
                added = 0
                for x in got:
                    fid = str(x.get("fid") or "")
                    if fid and fid not in seen:
                        seen.add(fid)
                        added += 1
                        items.append({"name": str(x.get("file_name") or ""), "fid": fid,
                                      "size": int(x.get("size") or 0),
                                      "pdir": str(x.get("pdir_fid") or ""),
                                      "created": int(x.get("l_created_at")
                                                     or x.get("created_at") or 0)})
                print(f"  {tag:<6} {sort:<15} p{page:<3d} +{added:<5d} 累计 {len(items):>6d}"
                      f"  {sum(i['size'] for i in items) / 2 ** 40:>6.2f} TiB", flush=True)
                if len(got) < PAGE:
                    break
                page += 1
                time.sleep(0.8)
            out.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return items


def load_protect(db) -> tuple[set[str], set[str], set[str]]:
    from sqlalchemy import text

    shr = db.execute(text(
        "select title, first_fid from pan_owned_shares "
        "where platform='quark' and status=1")).all()
    led = db.execute(text("select title from push_ledger where pan_type='夸克'")).all()
    return ({str(r[1]) for r in shr if r[1]},
            {(r[0] or "").strip() for r in shr if r[0]},
            {(r[0] or "").strip() for r in led if r[0]})


#: 重复下载时浏览器/网盘追加的副本后缀(`课件(1).mp4`、`资料（2）.zip`、`x - 副本.rar`)。
#: ⚠️ `(1)` 在**扩展名之前**,不能锚在字符串末尾(第一版就这么写的,0 组命中)。
COPY_SUFFIX = re.compile(r"\s*(\(\d{1,2}\)|（\d{1,2}）|- 副本)(?=\.[A-Za-z0-9]{1,5}$|$)")


def strip_copy_suffix(name: str) -> str:
    """去掉尾部的副本后缀;没有后缀则原样返回。"""
    return COPY_SUFFIX.sub("", name).strip()


def build_plan(files: list[dict], share_fids: set[str], titles: set[str],
               ledger: set[str]) -> tuple[list[dict], dict]:
    """同名(允许只差副本后缀)+ 同字节数的组:留一份,其余进计划。

    分组键用**去掉副本后缀**的名字,这样 `课件(1).mp4` 能和 `课件.mp4` 归到一组。
    组内名字**完全不相同**时(只差后缀),要求**至少有一份不带后缀的原件**,
    否则认不出哪份是原件,也排除不掉「名字本来就带括号、其实是两份不同资源」。
    组内名字**完全相同**则不受这条约束 —— 同名同字节数就是同一份,剥不剥不影响。
    """
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for x in files:
        groups[(strip_copy_suffix(x["name"]), x["size"])].append(x)

    plan: list[dict] = []
    prot_n = 0
    for (base, size), g in groups.items():
        if len(g) < 2:
            continue
        names = {x["name"] for x in g}
        if any(qd.is_promo_name(n) for n in names):
            continue
        # 「必须有不带后缀的原件」这道闸只管**名字真的不一样**的情形。
        # ★ 组内名字**完全相同**时(哪怕名字里自带 `（2）` 这种课程编号)剥不剥都在
        # 同一个 base 上,它到底叫「副本」还是「第 2 讲」**不影响判断** —— 同字节数
        # 的同名文件本来就是同一份。⚠️ 2026-11-18 回归:上一轮加这道闸时一刀切成
        # 「base 不在 names 就跳过」,把 `42期预测3（2）.mp4` 这类**自带编号的真资源**
        # 整组放过了 —— 实测 21 组 / 28 份 / **25.06 GiB** 白留着。而在加副本后缀
        # 判据之前,这些组本来是能正常去重的。
        exact = len(names) == 1
        if base not in names and not exact:
            continue                    # 名字只差后缀且无原件:认不出原件,不删
        kids = [x for x in g if x["fid"] in share_fids]
        if kids or base in titles or base in ledger \
                or any(n in titles or n in ledger for n in names):
            prot_n += 1
            continue                    # 受保护:整组跳过(宁可留,不可错删)
        # ★ **原件优先**:不带副本后缀的那份永远当 `keep`,其次才比创建时间。
        # 只按 created 排会在**时间戳并列**时按输入顺序选 —— 实测
        # `超凡蜘蛛侠2…2160p.mp4` 的原件与 `(1)`/`(2)` 三份 created 完全相同
        # (稳定排序 ⇒ 快照里 `(1)` 在前就被留下),旧计划 8205 份里 **6666 份**
        # 的 `keep_name` 带着后缀。删是安全的(剥完同 base),但「留最老的原件」
        # 这个目标失效了,而回收站捞回时按 keep_name 认哪一份。
        g = sorted(g, key=lambda x: (x["name"] != base, x.get("created") or 0))
        keep = g[0]
        for x in g[1:]:
            plan.append({"fid": x["fid"], "name": x["name"], "size": size,
                         "pdir": x["pdir"], "keep_fid": keep["fid"],
                         "keep_name": keep["name"],
                         "keep_pdir": keep["pdir"], "copies": len(g)})
    return plan, {"groups": len(groups), "protected_groups": prot_n}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真删(默认只出计划)")
    ap.add_argument("--account", default="quark")
    ap.add_argument("--scan", default=str(SCAN))
    ap.add_argument("--rescan", action="store_true")
    ap.add_argument("--plan-out", default="data/quark_global_dup_plan.json")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--min-gib", type=float, default=0.1)
    args = ap.parse_args()

    db = get_session_local()()
    pair = next(((r, c) for r, c in A.refs_for(db, "quark", user_id=1)
                 if str(r.cookie_key or "quark") == args.account), None)
    if pair is None:
        print(f"池里没有 {args.account}")
        db.close()
        return 1
    qt = QuarkTransfer(pair[1])

    scan_path = Path(args.scan)
    if args.rescan or not scan_path.exists():
        print("[扫描] 全盘 size:desc(pdir_fid=\"\" 才是全盘)")
        files = scan_global(qt, scan_path)
    else:
        files = json.loads(scan_path.read_text(encoding="utf-8"))
        print(f"[快照] {scan_path} -> {len(files)} 条"
              f"({sum(x['size'] for x in files) / 2 ** 40:.2f} TiB)")

    floor = int(args.min_gib * 2 ** 30)
    files = [x for x in files if x.get("fid") and x["size"] >= floor]
    share_fids, titles, ledger = load_protect(db)
    db.close()
    print(f"[保护] first_fid {len(share_fids)} / 分享标题 {len(titles)} / 台账 {len(ledger)}")

    plan, stats = build_plan(files, share_fids, titles, ledger)
    gib = sum(p["size"] for p in plan) / 2 ** 30
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "account": args.account, "scanned": len(files),
           "scanned_tib": round(sum(x["size"] for x in files) / 2 ** 40, 2),
           "min_gib": args.min_gib, "n": len(plan), "gib": round(gib, 2),
           "protected_groups": stats["protected_groups"], "items": plan}
    Path(args.plan_out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                   encoding="utf-8")

    print(f"\n≥{args.min_gib}GiB 文件 {len(files)} 个 / {out['scanned_tib']:.2f} TiB")
    print(f"⇒ 待删 **{len(plan)} 份 / {gib:.1f} GiB ({gib / 2 ** 10:.2f} TiB)**"
          f"(另有 {stats['protected_groups']} 组因受保护整组跳过)")
    print(f"计划已落盘 {args.plan_out}")
    for p in sorted(plan, key=lambda x: -x["size"])[:15]:
        print(f"  {p['size'] / 2 ** 30:8.2f}GiB x{p['copies']:<3d} {p['name'][:62]}")

    if not args.yes:
        print("\n(dry-run,未删除;加 --yes 才真删 —— 进回收站,可捞回)")
        return 0
    if not plan:
        print("没有可删的")
        return 0

    before = qt.quota_usage()
    deleted = 0
    errors: list[str] = []
    for i in range(0, len(plan), args.batch):
        chunk = plan[i:i + args.batch]
        try:
            res = qt.delete_files([c["fid"] for c in chunk], to_recycle=True)
            deleted += int((res or {}).get("count") or len(chunk))
            print(f"  已删 {deleted}/{len(plan)}(进回收站)", flush=True)
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