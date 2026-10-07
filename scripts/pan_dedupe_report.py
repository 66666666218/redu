"""盘内**重复资源**只读报告(2026-10-07)。**不删任何东西**。

## 判据从哪来(以及为什么换了口径)
用户口径原本是「重复 = 同一资源多份;老 = 早于 2026-10-01」。实测后发现"老"**算不出来**:
迅雷盘上所有文件的 `created_time` / `original_create_time` **全是入库那一天**,
没有区分度 —— 那是"什么时候搬进来的",不是"这资源有多老"。
所以本脚本**只做重复**(它现在算得出来),老的判据留给你重新定。

## 重复怎么判
用 `resource_library.core_resource_name`(**今天做跨平台共振时建的那层归一化**)——
盘里同一份资源常有一堆写法:「xxx.pdf」/「xxx(1).pdf」/「xxx 完整版.pdf」,
按原始名比对会把它们当成三份,按归一化名才认得出是一份。

用法:`python scripts/pan_dedupe_report.py [深度]`
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import xunlei_transfer as xt  # noqa: E402
from app.services.resource_library import core_resource_name  # noqa: E402

MAX_DEPTH = int(sys.argv[1]) if len(sys.argv) > 1 else 3
BUDGET = 400          # API 调用上限:扫不完就说扫不完,别跑飞


def _strip_copy_suffix(name: str) -> str:
    """去掉「(1)」「(2)」这类**复制副本**后缀 —— 那是"同一份存了两次"最典型的样子。

    ⚠️ 只在**末尾**剥,且要求括号里是纯数字:「（先保存再下载）」这种说明性括号**不动**。
    """
    import re as _re

    return _re.sub(r"\s*[（(]\s*\d+\s*[)）]\s*$", "", str(name or "")).strip()


#: 说明类**目录名**的痕迹 —— 每个资源包里都带一份,不是重复
_NOTICE_WORDS = ("必看", "解压方法", "使用帮助", "说明", "教程", "注意", "公告")


def _is_notice(name: str) -> bool:
    return any(w in str(name) for w in _NOTICE_WORDS)


def main() -> int:
    calls = {"n": 0}
    files: list[dict] = []
    dirs: list[dict] = []          # 资源包(文件夹)—— 去重的判据落在这一层,见下面注释

    def ls(pid: str) -> list[dict]:
        if calls["n"] >= BUDGET:
            return []
        calls["n"] += 1
        try:
            return xt.list_all_files(pid, page_size=200, max_pages=20)
        except Exception as exc:  # noqa: BLE001 - 单目录失败不拖垮整轮
            print(f"   ⚠️ 列 {pid[:10]} 失败: {str(exc)[:60]}")
            return []

    def walk(pid: str, depth: int, path: str) -> None:
        if depth > MAX_DEPTH or calls["n"] >= BUDGET:
            return
        for x in ls(pid):
            name = str(x.get("name") or "").strip()
            if not name:
                continue
            if "folder" in str(x.get("kind")):
                kids = ls(str(x.get("id")))
                dirs.append({"id": str(x.get("id")), "name": name, "n": len(kids),
                             "parent": path, "path": f"{path}/{name}"})
                walk(str(x.get("id")), depth + 1, f"{path}/{name}")
            else:
                files.append({"id": str(x.get("id")), "name": name,
                              "size": int(x.get("size") or 0), "path": f"{path}/{name}"})

    print(f"扫盘(深度上限 {MAX_DEPTH},API 预算 {BUDGET} 次)…")
    walk("", 1, "")
    print(f"扫到 {len(files)} 个文件,用了 {calls['n']} 次调用"
          f"{'(**撞预算,没扫完**)' if calls['n'] >= BUDGET else ''}\n")
    if not files:
        return 0

    # ⚠️⚠️ **判据:同级 + 归一化名相同** —— 关键是「**同级**」这三个字。
    #
    # 前两版都栽在同一类误判上,只是从文件挪到了文件夹:
    #   ① 按**文件**名去重 → 报出【必看.jpg】×5、【解压方法看这里.JPG】×3、
    #      【使用帮助及新版软件.jpeg】×8 —— 每个资源包各自带的说明书,删了会弄坏每个包;
    #   ② 改成按**文件夹**名去重 → 报出【公众号:小林的家】×14 —— 同样是每个包里的
    #      **品牌文件夹**(实测 14 份分布在 14 个**不同**资源包里)。
    #
    # **真正的重复长什么样**:`/J开头游戏/捷德升华模拟器` 与 `/J开头游戏/捷德升华模拟器(1)`
    #   —— **同一层并排的两个**。
    # **包内固定件长什么样**:`/A开头游戏/avvy/公众号:小林的家` 与
    #   `/A开头游戏/安洁拉世界/公众号:小林的家` —— 名字一样,但**父目录各不相同**。
    # ⇒ 键必须是 `(父目录, 归一化名)`:父目录不同就永远不成一组。
    groups: dict[tuple, list[dict]] = collections.defaultdict(list)
    for d in dirs:
        base = core_resource_name(_strip_copy_suffix(str(d["name"])))
        if not base or _is_notice(base):
            continue
        groups[(d["parent"], base)].append(d)
    dups = {k: v for k, v in groups.items() if len(v) > 1}

    print(f"=== 重复的**资源包(同级同名)**:{len(dups)} 组 ===")
    plan: list[dict] = []
    for k, v in sorted(dups.items(), key=lambda kv: -len(kv[1])):
        stats = []
        for x in v:
            n, sz = _dir_stats(ls, x["id"], budget=calls)
            stats.append({**x, "items": n, "size": sz})
        # ⚠️ **留"内容多"的那份**:先比**递归条目数**,再比**递归体积**,最后才比"名字带不带 (N)"。
        #    只比子项数是不够的(6 个小文件 ≠ 3 个大文件),所以体积是第二判据。
        #    (用户口径 2026-10-07:一律留内容多的那份。)
        keep = max(stats, key=lambda s: (s["items"], s["size"], 0 if _has_copy_suffix(s["name"]) else 1))
        drop = [s for s in stats if s["id"] != keep["id"]]
        plan.append({"name": k[1], "parent": k[0], "keep": keep, "drop": drop})
        print(f"\n  【{k[1][:42]}】×{len(v)}   同处 {str(k[0])[:40] or '/'}")
        for s in sorted(stats, key=lambda y: -(y["items"] * 10**12 + y["size"])):
            tag = "✔留" if s["id"] == keep["id"] else "✂删"
            print(f"     {tag} {s['items']:>4} 项 / {s['size'] / 2**20:>9.1f} MiB  {s['path'][:68]}")
    freed = sum(s["size"] for p in plan for s in p["drop"])
    n_drop = sum(len(p["drop"]) for p in plan)
    print(f"\n⇒ 计划:**删 {n_drop} 个整包**,可省 **{freed / 2**30:.2f} GiB**")
    print("   ⚠️ 本脚本**只读**。要真删请跑 `scripts/pan_dedupe_apply.py`(默认 dry-run)。")
    return 0


def _has_copy_suffix(name: str) -> bool:
    return _strip_copy_suffix(name) != str(name or "").strip()


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
    print("⚠️ **文件层不去重**:每个资源包里都有一份「必看 / 解压方法 / 使用帮助」——")
    print("   它们文件名相同,但**各自属于那个包**,删掉等于把每个包都弄坏。")
    print("   上面报的是**整包重复**(同一份资源存了两次),删掉多余那份才安全。")
    print()
    print("   ⚠️ 本脚本**只读**。要删请另行确认 —— 删之前必须看清每组**留哪一份**,")
    print("      以及「名字像但其实是两份不同资源」的误判(归一化不是万能的)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
