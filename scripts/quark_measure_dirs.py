# -*- coding: utf-8 -*-
"""夸克盘**目录真实体积**测量(只读,2026-11-17)。

## 为什么必须有这个脚本

夸克的目录条目 `size` 字段**恒为 0**,接口也**不给递归体积**:

| 端点 | 实测返回 |
|---|---|
| `/1/clouddrive/file/dir_size` | `null` |
| `/1/clouddrive/file/statistic` | `null` |
| `/1/clouddrive/capacity/stat` | `null` |

`include_items` 只算**直接子项数**,也不是体积。所以唯一办法是**逐层递归求和**。

## 为什么不能靠根目录快照估

根目录 `size:desc` 的窗口只有 **10200 条**(`_page` 到 103 ⇒ `61004`),
而根目录有 **201727** 条。实测:顶层只有一个 4.7 GiB 文件的家,
递归下去是 **15~144 GiB** —— 用顶层值估体积会差一个数量级。

## 用法

    PYTHONIOENCODING=utf-8 python scripts/quark_measure_dirs.py --only redian监听_1007
    PYTHONIOENCODING=utf-8 python scripts/quark_measure_dirs.py          # 全部家 + 根非空目录

结果边测边写 `data/_qk_dir_sizes.json`(可随时中断续跑)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import get_session_local                             # noqa: E402
from app.services import pan_accounts as A                      # noqa: E402
from app.services.quark_transfer import QuarkTransfer           # noqa: E402

OUT = Path("data/_qk_dir_sizes.json")
PAGE = 1000
GAP = 0.3
MAX_DEPTH = 8


def _ls(qt: QuarkTransfer, fid: str, page: int) -> tuple[list[dict], int | None]:
    """一页目录内容(带抖动重试)。返回 (rows, 成功标志);窗口触顶时返回 ([], None)。"""
    for _ in range(4):
        try:
            d = qt._request("GET", "/1/clouddrive/file/sort", api="https://drive-pc.quark.cn",
                            params={"_page": page, "_size": PAGE, "_sort": "file_name:asc",
                                    "pdir_fid": fid, "_fetch_total": 1})
            return (d.get("data") or {}).get("list") or [], 1
        except Exception as exc:                       # noqa: BLE001 - 接口抖动
            msg = str(exc)
            if "61004" in msg or "66010" in msg:       # 窗口上限,不是抖动
                return [], None
            print(f"      retry {msg[:64]}", flush=True)
            time.sleep(8)
    return [], None


def walk(qt: QuarkTransfer, fid: str, depth: int = 0) -> dict:
    """递归求和:返回 {bytes, files, dirs, truncated}。"""
    total = nfiles = ndirs = 0
    truncated = False
    page = 1
    while page <= 30:
        rows, ok = _ls(qt, fid, page)
        if ok is None:
            truncated = True
            break
        if not rows:
            break
        for x in rows:
            if x.get("dir"):
                ndirs += 1
                # ⚠️ `include_items` 是**直接子项数**(实测:根目录 42490 个目录里
                # 只有 139 个非零)。为空就直接跳过 —— 否则 1007 那种 638 个子目录
                # 的树要发上千次请求,一个家能跑一小时。
                if int(x.get("include_items") or 0) == 0:
                    continue
                if depth < MAX_DEPTH:
                    sub = walk(qt, str(x.get("fid") or ""), depth + 1)
                    total += sub["bytes"]
                    nfiles += sub["files"]
                    ndirs += sub["dirs"]
                    truncated = truncated or sub["truncated"]
                else:
                    truncated = True           # 太深,没进去 ⇒ 数字偏小,要标出来
            else:
                total += int(x.get("size") or 0)
                nfiles += 1
        if len(rows) < PAGE:
            break
        page += 1
        time.sleep(GAP)
    return {"bytes": total, "files": nfiles, "dirs": ndirs, "truncated": truncated}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="quark")
    ap.add_argument("--only", default="", help="只测名字里含该子串的项")
    args = ap.parse_args()

    db = get_session_local()()
    pair = next(((r, c) for r, c in A.refs_for(db, "quark", user_id=1)
                 if str(r.cookie_key or "quark") == args.account), None)
    if pair is None:
        print(f"池里没有 {args.account}")
        db.close()
        return 1
    qt = QuarkTransfer(pair[1])
    db.close()

    targets: list[dict] = []
    homes = json.loads(Path("data/_qk_alldirs.json").read_text(encoding="utf-8"))
    targets += [{"kind": "home", **h} for h in homes
                if str(h["name"]).startswith("redian监听_") and h["items"] > 0]
    targets += [{"kind": "rootdir", **h} for h in homes
                if not str(h["name"]).startswith("redian监听_") and h["items"] > 0]
    if args.only:
        targets = [t for t in targets if args.only in str(t["name"])]
    targets.sort(key=lambda t: -t["items"])

    done: dict[str, dict] = {}
    if OUT.exists():
        for row in json.loads(OUT.read_text(encoding="utf-8")):
            done[row["fid"]] = row

    print(f"待测 {len(targets)} 项(已完成 {len(done)})\n", flush=True)
    out = list(done.values())
    grand = 0
    for i, t in enumerate(targets, 1):
        if t["fid"] in done:
            grand += done[t["fid"]]["bytes"]
            continue
        t0 = time.time()
        r = walk(qt, t["fid"])
        grand += r["bytes"]
        row = {"name": t["name"], "fid": t["fid"], "kind": t["kind"],
               "files": r["files"], "dirs": r["dirs"],
               "bytes": r["bytes"], "gib": round(r["bytes"] / 2 ** 30, 2),
               "truncated": bool(r["truncated"]), "secs": round(time.time() - t0)}
        out.append(row)
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        flag = " ⚠️截断" if r["truncated"] else ""
        print(f"[{i}/{len(targets)}] {t['kind']:<8} {str(t['name'])[:30]:<32}"
              f" {row['gib']:>9.2f} GiB  files={r['files']:<6d} dirs={r['dirs']:<5d}"
              f" {row['secs']:>4d}s{flag}", flush=True)

    print(f"\n本次覆盖合计 {grand / 2 ** 40:.3f} TiB / {grand / 2 ** 30:.1f} GiB")
    print(f"落盘 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())