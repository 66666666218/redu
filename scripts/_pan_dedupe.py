"""盘内**重复资源包**的检测逻辑(报告与执行器共用一份,免得两处各写一遍又漂)。

判据经过**两轮收紧**(2026-10-07,见 `pan_dedupe_report.py` 的注释):
  · 键 = `(父目录, 归一化名)` —— **父目录不同就永远不成一组**(否则会把"每个包里都带的
    说明书/品牌文件夹"当成重复,那类误判我连栽两次);
  · **判据只认「剥掉 (N) 副本后缀后的原名」,不用 `core_resource_name`** ——
    后者会剥版本号/日期,那是为"跨平台找同一份资源"设计的;而**删除判据要的正好相反**,
    被剥掉的恰恰是区分不同资源的信息(干跑时它把三个不同的比亚迪固件版本并成了一组)。

"留哪一份"按用户口径:**留内容多的** —— 先比递归条目数,再比递归体积,
最后才比"名字带不带 (N)"。⚠️ **不能只留"不带 (N) 的那个"**:实测多组里 `(1)` 反而更多。
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import xunlei_transfer as xt  # noqa: E402

import os

#: API 调用上限。默认 800;要一次扫完跑得久的树可以 `PAN_DEDUPE_BUDGET=5000` 抬上去。
#: ⚠️ 试过 14 种"服务端排除回收站"的参数名,接口**一个都不认** —— 原始列表里约 197/200
#: 是 trashed 条目,只能在客户端过滤,所以**每个目录都要多翻十几页**。这是扫描慢的根因。
BUDGET = int(os.environ.get("PAN_DEDUPE_BUDGET") or 800)
#: 扫描**最多用掉**预算的这个比例,剩下的**留给"数每组内容量"** ——
#: 不留的话统计会撞上限,**每组都数出 0 项 0 字节**,于是"留内容多的那份"
#: 这条规则**静默失效**(实测踩到:4 组的 ✔留/✂删 全是 0.0 MiB,等于没判)。
WALK_RATIO = 0.7

#: 「要删的那份比留的大这么多倍」⇒ 认定"条目多≠内容全",**自动删跳过**。见 build_plan 里的说明。
RISKY_RATIO = 1.5
#: 而且差值要够大才算 risky(小文件之间的正常波动不惊动人工)。
RISKY_MIN_MIB = 100


def strip_copy_suffix(name: str) -> str:
    """剥掉末尾的「(1)」「(2)」副本后缀(**只在末尾、括号里是纯数字时才剥**)。"""
    import re

    return re.sub(r"\s*[（(]\s*\d+\s*[)）]\s*$", "", str(name or "")).strip()


def has_copy_suffix(name: str) -> bool:
    return strip_copy_suffix(name) != str(name or "").strip()


#: 说明类**目录名**的痕迹 —— 每个资源包里都带一份,不是重复
_NOTICE_WORDS = ("必看", "解压方法", "使用帮助", "说明", "教程", "注意", "公告")


def _is_notice(name: str) -> bool:
    return any(w in str(name) for w in _NOTICE_WORDS)


def _dir_stats(ls, fid: str, budget: dict, depth: int = 0) -> tuple[int, int]:
    """递归数一个资源包的 (条目数, 总字节)。预算共用,**撞上限就返回已数到的部分**。"""
    if depth > 4 or budget["n"] >= BUDGET:
        return 0, 0
    n = sz = 0
    for x in ls(fid):
        if "folder" in str(x.get("kind")):
            sn, ss = _dir_stats(ls, str(x.get("id")), budget, depth + 1)
            n += sn
            sz += ss
        else:
            n += 1
            sz += int(x.get("size") or 0)
    return n, sz


def build_plan(max_depth: int = 3, budget: int = BUDGET) -> dict:
    """扫盘 → 找同级同名重复 → 定"留谁删谁"。返回 `{"plan", "freed", "n_drop", ...}`。

    ⚠️ **撞 API 预算时 `complete=False`** —— 调用方必须把这件事说出来:
    "没扫完"和"扫完了没重复"长得一样,正是本仓最忌讳的那种失败。
    """
    calls = {"n": 0}
    dirs: list[dict] = []

    def ls(pid: str) -> list[dict]:
        if calls["n"] >= budget:
            return []
        calls["n"] += 1
        try:
            return xt.list_all_files(pid, page_size=200, max_pages=20)
        except Exception:  # noqa: BLE001 - 单目录失败不拖垮整轮
            return []

    walk_budget = max(1, int(budget * WALK_RATIO))     # 给统计留 30%

    def walk(pid: str, depth: int, path: str) -> None:
        if depth > max_depth or calls["n"] >= walk_budget:
            return
        for x in ls(pid):
            name = str(x.get("name") or "").strip()
            if not name:
                continue
            if "folder" in str(x.get("kind")):
                dirs.append({"id": str(x.get("id")), "name": name, "parent": path,
                             "path": f"{path}/{name}"})
                walk(str(x.get("id")), depth + 1, f"{path}/{name}")

    walk("", 1, "")

    groups: dict[tuple, list[dict]] = collections.defaultdict(list)
    for d in dirs:
        # ⚠️⚠️ **键只认「剥掉 (N) 副本后缀后的原名」,不用 `core_resource_name`** ——
        # 这条是干跑救回来的(2026-10-07,第三轮):
        #   `core_resource_name` 会剥**版本号/日期/页码**,那是**为"跨平台找同一份资源"
        #   设计的**(不剥就认不出是同名异写)。但**删除判据要的正好相反**:被剥掉的
        #   恰恰是**区分不同资源**的信息。实测它把这三个并成了一组:
        #       26年6月18号-比亚迪23.1.63控制器OTA升级固件
        #       26年6月27号-比亚迪23.1.61控制器OTA升级固件
        #       26年6月30号-比亚迪23.1.83控制器OTA升级固件
        #   —— **三个不同的固件版本**,删下去就是删掉两个真资源。
        # ⇒ 这里改成"**只有 (N) 之差才算重复**",最保守也最安全:
        #   真重复本来就是"同一份被存了两次",名字必然只差那个 (N)。
        base = strip_copy_suffix(str(d["name"]))
        if not base or _is_notice(base):
            continue
        groups[(d["parent"], base)].append(d)
    dups = {k: v for k, v in groups.items() if len(v) > 1}

    plan: list[dict] = []
    for k, v in sorted(dups.items(), key=lambda kv: -len(kv[1])):
        stats = []
        for x in v:
            n, sz = _dir_stats(ls, x["id"], calls)
            stats.append({**x, "items": n, "size": sz})
        keep = max(stats, key=lambda s: (s["items"], s["size"],
                                         0 if has_copy_suffix(s["name"]) else 1))
        drop = [s for s in stats if s["id"] != keep["id"]]
        # ⚠️⚠️ **「删的那份比留的还大」→ 不许自动删**(2026-10-07,干跑逮到)。
        # "留内容多的"我们是按**先条目数、再体积**判的,但**条目多 ≠ 内容全**:
        # 实测「宝可梦朱紫」留的是 36 项 / **2.55 GiB**,而要删的是 24 项 / **27.76 GiB**
        # —— 那 24 项几乎肯定是视频/镜像大文件,而这两份**可能是不同的东西**(本体 vs 本体+DLC),
        # 根本就不是"同一份存了两次"。**自动删下去就是删掉一份真资源。**
        # ⇒ 这类组标成 risky,**执行器默认跳过**,交人工看。
        big = [s for s in drop if s["size"] > keep["size"] * RISKY_RATIO
               and s["size"] - keep["size"] > RISKY_MIN_MIB * 2 ** 20]
        risky = ""
        if big:
            worst = max(big, key=lambda s: s["size"])
            risky = (f"要删的那份比留的大 {worst['size'] / max(keep['size'], 1):.1f} 倍"
                     f"({worst['size'] / 2**30:.1f} GiB vs {keep['size'] / 2**30:.2f} GiB)"
                     f" —— 条目多≠内容全,可能根本不是同一份")
        plan.append({"name": k[1], "parent": k[0], "keep": keep, "drop": drop,
                     "risky": bool(risky), "risky_reason": risky})
    return {"plan": plan, "scanned_dirs": len(dirs), "calls": calls["n"],
            "complete": calls["n"] < budget,
            "n_drop": sum(len(p["drop"]) for p in plan if not p["risky"]),
            "n_risky": sum(1 for p in plan if p["risky"]),
            "freed": sum(s["size"] for p in plan if not p["risky"] for s in p["drop"]),
            "risky_freed": sum(s["size"] for p in plan if p["risky"] for s in p["drop"])}
