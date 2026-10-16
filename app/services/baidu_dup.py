"""百度盘的**跨目录重复**识别 —— 只读、只报告,**一个字节都不删**。

## 为什么单独一条、而且不删
三盘里只有百度这半边没接进自动清理,原因是**百度没有回收站:删除不可逆**
(`baidupan_transfer.delete_paths` 的文档里就写着「调用方必须自己确认路径」)。
2026-10-16 用户拍板:**先做识别 + 清单,默认不删** —— 本模块就是那前半段。

## 判据(**实测订正过,别再想当然"跨目录"**)
**同一个基名 + 同一个大小**出现 ≥2 次 = 重复(基名 = 剥掉自动改名后缀:
`(1)` 与 `_<≥6位数字>`)。
⚠️⚠️ **"≥2 次"不要求跨目录**(2026-10-16 实测订正):百度重复保存同一份资源时,
是在**同一个目录里**给它加 `_<数字>` 尾巴 —— 实测 `/来自资源批量转存` 里
`137号案件.apk` 与 `137号案件_1011213845.apk` ×3 **全在同一目录**。
第一版照搬夸克那条"必须跨目录"的判据 ⇒ 扫了 9950 个文件却报「重复 0 组」,
而肉眼一看就有好几组。**夸克是"跨家"的形态,百度不是,别照搬。**
⚠️ **只同名不够**(同名不同大小是两回事);⚠️ **只同大小也不够**。
⚠️ **必须排除宣传简介**(它被设计成"塞进每个资源目录",天然同名同大小):
   不排除的话它会是这份清单里**最大的一坨**,而删它等于把功能砍了
   —— 见 `doc/三盘宣传简介.md`。夸克那边踩过同一个坑(`intro_protect_dirs`)。
⚠️ **扫描有上限**(深度 / 文件数 / 盘商翻页),**撞上限一定要如实报出来**:静默截断会让人读成
   "扫完了、没有重复",而那是本仓最恨的那类静默失败。
   ⚠️ 同理:**列不动的目录**必须与"空目录"分开报(第一版把 `errno=-9` 吞成了"扫到 0 个文件")。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 小于这个大小不参与判重(1 MiB):小文件省不出空间,却会制造一大串噪音。
DUP_MIN_BYTES = 1 << 20
#: 扫描上限(都**如实报出来**,见模块头注)。文件数放到 1.2 万是因为实测该盘的
#: `/来自资源批量转存` 一个目录里就有 **9950 个文件**(1086 GiB)—— 卡在 3000 会只扫到
#: 按名字排的前三分之一,得到一份**有偏的**清单(那是"看着扫完了"的另一种形态)。
_MAX_DEPTH = 3
_MAX_FILES = 12000
#: ⚠️ **照抄 `BaiduPanClient.list_dir` 内部的翻页上限**(`while start < 10000`):
#: 拿到正好这么多条 = 它**没列完**。这个常数放在这里是为了"两边改一处要对得上"
#: (那边真改了翻页上限,这条判断就会偏保守 —— 偏保守是安全的,漏报才不是)。
_LIST_DIR_CAP = 10000
#: 我们自己落资源的目录(`BaiduPanClient.transfer_and_share` 的默认值)。
#: ⚠️ **实测这个目录在该盘里并不存在**(errno=-9):百度侧真正的落点是
#: `/来自资源批量转存`(盘商自己的"批量转存"目录,9950 个文件),而且根目录下
#: 还散着 **51 个** 同族目录(空壳)—— 与夸克那边「保存目录会漂」是同一个形态。
#: ⇒ 默认从**根目录**扫(depth 覆盖到那些家),别写死成某一个家。
DEFAULT_ROOT = "/"


def intro_names(client: Any, intro_dir: str) -> set[str]:
    """宣传简介目录里的**文件名** —— 这些名字天然出现在每个资源目录里,不许判成重复。

    ⚠️ 取不到就回**空集**(= 不加保护),并把这件事交给调用方去说
    (与夸克那边同一条纪律:这一层只用于"少删",**取不到时绝不改判据**)。
    """
    d = (intro_dir or "").strip()
    if not d:
        return set()
    try:
        rows = client.list_dir(d)
    except Exception as exc:  # noqa: BLE001 - 简介目录取不到不该毁掉整轮识别
        logger.info("百度简介目录列不动(%s),本轮不加名字保护:%s", d, type(exc).__name__)
        return set()
    return {str(r.get("server_filename") or "") for r in rows if not r.get("isdir")}


def walk(client: Any, root: str = DEFAULT_ROOT, *, max_depth: int = _MAX_DEPTH,
         max_files: int = _MAX_FILES) -> dict:
    """广度优先列全 `root` 下的文件(带上限)。

    返回 `{"files": […], "truncated": bool, "dirs": n, "errors": [str]}`。
    ⚠️ `truncated=True` = **没列完**(撞了深度 / 文件数 / 盘商翻页上限)⇒ 调用方**必须说出来**。
    ⚠️ `errors` 非空 = **有目录压根没列成** —— 那与"目录是空的"**不是一回事**。
    """
    files: list[dict] = []
    truncated = False
    dirs_seen = 0
    errors: list[str] = []
    level = [(root or "/", 0)]
    while level:
        nxt: list[tuple[str, int]] = []
        for path, depth in level:
            dirs_seen += 1
            try:
                rows = client.list_dir(path)
            except Exception as exc:  # noqa: BLE001 - 单个目录列不动就跳过它,但**要留痕**
                # ⚠️⚠️ **列不动绝不许当成"这个目录是空的"**(2026-10-16 实测踩到):
                #   我第一版把异常吞了 ⇒ 默认根(当时写的是 `/redian百度转存`,而它在盘里
                #   **根本不存在**,`list_dir` 抛 `errno=-9`)被读成"扫完了"⇒ 报告写着
                #   「跨目录重复 0 组(扫 0 个文件)」—— **一个目录都没列成,却像个干净的结论**。
                #   那正是本仓最恨的静默失败,而它是我自己新写的代码里的。
                errors.append(f"{path}: {type(exc).__name__}")
                continue
            if len(rows) >= _LIST_DIR_CAP:
                truncated = True          # 盘商那边翻页到顶了,这个目录**没列完**
            for r in rows:
                name = str(r.get("server_filename") or "")
                sub = str(r.get("path") or "")
                if r.get("isdir"):
                    if depth + 1 < max_depth:
                        nxt.append((sub or f"{path.rstrip('/')}/{name}", depth + 1))
                    else:
                        truncated = True          # 还有更深的,但我们不下探了
                    continue
                files.append({"path": sub, "name": name,
                              "size": int(r.get("size") or 0), "dir": path})
                if len(files) >= max_files:
                    return {"files": files, "truncated": True, "dirs": dirs_seen,
                            "errors": errors}
        level = nxt
    return {"files": files, "truncated": truncated, "dirs": dirs_seen, "errors": errors}


def _base(name: str) -> str:
    """剥掉**自动改名**留下的后缀 —— 同一份文件被重复保存时盘商会给它加尾巴。

    实测(2026-10-16,`/来自资源批量转存` 里真实存在的形态):
        137号案件.apk / 137号案件_1011213845.apk / 137号案件_1011214401.apk …
        —— **同名同大小(95433288)** ×4 ⇒ 不剥这个后缀,它们会各算一组、一条都判不出重复。
    ⚠️ 两个后缀都收:`(N)`(夸克那边的形态)与 `_<≥6位数字>`(百度这边的形态)。
    ⚠️ 数字那一条**要求 ≥6 位**:短数字后缀可能是文件名本身的一部分,
    而配合"**同大小**"这条判据,长数字尾巴才有把握是自动加的。
    """
    import re

    stem, dot, ext = str(name or "").rpartition(".")
    if not dot:
        stem, ext = str(name or ""), ""
    stem = re.sub(r"(?:\(\d+\)|_\d{6,})$", "", stem).strip()
    return stem + (f".{ext}" if ext else "")


def classify(files: list[dict], *, min_bytes: int = DUP_MIN_BYTES,
             intro: set[str] | None = None) -> dict:
    """**纯函数**(可离线测):把扫到的文件分成"重复组"与"可删副本"。

    ⚠️ 判据是「**同一个名字(剥掉自动改名后缀)+ 同一个大小**出现在**不同目录**」——
    同目录同名不可能(盘里就一个)，所以"出现在 ≥2 个目录"就是重复的定义本身。
    ⚠️ 留哪份:**目录名最靠前的那份**(调用方按需排序后传进来),不按文件名排
    (目录名带日期,按名字排看着对、其实没有依据 —— 夸克那边为这条栽过)。
    """
    from collections import defaultdict

    prot = {n for n in (intro or set()) if n}
    skipped_intro = 0
    groups: "defaultdict[tuple[str, int], list[dict]]" = defaultdict(list)
    for f in files:
        name = str(f.get("name") or "")
        if name in prot:
            skipped_intro += 1
            continue
        if int(f.get("size") or 0) < min_bytes:
            continue
        groups[(_base(name), int(f.get("size") or 0))].append(f)

    out_groups: list[dict] = []
    dup_files: list[dict] = []
    for (key, size), copies in groups.items():
        if len(copies) < 2:
            continue                    # 只有一份 ⇒ 不是重复
        # ⚠️⚠️ **不要求"跨目录"**(2026-10-16 **实测订正**):百度把同一份资源重复保存时,
        #   是**在同一个目录里**给它加 `_<数字>` 尾巴(实测 `137号案件.apk` 与
        #   `137号案件_1011213845.apk` ×3 **全在 `/来自资源批量转存` 同一个目录**)
        #   ⇒ 要求"跨目录"会**一条都判不出来**:第一版就是这样,扫了 9950 个文件、
        #   报「重复 0 组」,而肉眼一看就有好几组。**夸克那边是"跨家"的形态,两边不一样**,
        #   别把夸克那条判据原样搬过来。
        #   (同目录里出现同一个"基名+同大小"≥2 次,只可能是自动改名留下的,盘里不允许多名同存。)
        where = {str(c.get("dir") or "") for c in copies}
        # 留哪份:**名字就等于基名**的那份(没被加尾巴的那个);都对不上才取第一份
        keep = next((c for c in copies if str(c.get("name") or "") == key), copies[0])
        rest = [c for c in copies if c is not keep]
        out_groups.append({"name": key, "size": size, "copies": len(copies),
                           "dirs": sorted(where), "keep": keep.get("path")})
        for c in rest:
            dup_files.append({**c, "keep": keep.get("path"), "why": "重复(同名同大小)"})
    out_groups.sort(key=lambda g: -int(g["size"]) * int(g["copies"]))
    dup_files.sort(key=lambda d: -int(d.get("size") or 0))
    return {"groups": out_groups, "dup_files": dup_files,
            "n_groups": len(out_groups), "n_dup": len(dup_files),
            "bytes": sum(int(d.get("size") or 0) for d in dup_files),
            "skipped_intro": skipped_intro}


def build_plan(client: Any, *, roots: tuple[str, ...] = (DEFAULT_ROOT,),
               intro_dir: str = "", **kw) -> dict:
    """扫 + 判 → 一份**只读计划**(调用方负责落清单/报告,**本函数永不删除**)。"""
    prot = intro_names(client, intro_dir)
    all_files: list[dict] = []
    truncated = False
    dirs = 0
    errors: list[str] = []
    for r in roots:
        got = walk(client, r, **kw)
        all_files.extend(got["files"])
        truncated = truncated or bool(got["truncated"])
        dirs += int(got["dirs"])
        errors.extend(got.get("errors") or [])
    plan = classify(all_files, intro=prot)
    plan.update({"roots": list(roots), "scanned": len(all_files),
                 "dirs": dirs, "truncated": truncated,
                 "errors": errors[:5], "n_errors": len(errors),
                 "intro_protected": len(prot)})
    return plan


def summary(plan: dict) -> str:
    """一句话人话(给 `runs.detail` 用)——**截断与列不动都要明说**,不许装成扫全了。"""
    s = (f"重复{plan.get('n_groups', 0)}组/多出{plan.get('n_dup', 0)}份/"
         f"{int(plan.get('bytes') or 0) / (1 << 30):.2f} GiB"
         f"(扫{plan.get('scanned', 0)}个文件/{plan.get('dirs', 0)}个目录")
    if plan.get("intro_protected"):
        s += f";简介护{plan['intro_protected']}"
    s += ";⚠️没列完(撞上限),数字偏小" if plan.get("truncated") else ""
    if plan.get("n_errors"):
        # ⚠️ **列不动的目录与"空目录"必须分开说**:否则"扫到 0"看起来像"很干净"
        s += f";⚠️{plan['n_errors']}个目录**没列成**({str(plan.get('errors') or [''])[:1][0][:40]}…)"
    return s + ")"
