"""夸克盘内**跨目录重复资源**的**只读检测**(2026-10-08)。**本模块不删任何东西。**

⚠️ **删除执行器故意没有写**(2026-10-08):首轮扫描**撞预算没扫完**(600 次调用、
19 个家、4587 个文件)⇒ 计划本身还没在完整数据上验过,此时写一个"删盘执行器"
就是把未验证的判据接到不可逆动作上。**先让报告跑完整,再谈删。**

## 为什么不是 `pan_dedupe`
那个模块是**迅雷**用的,判据是「**同一父目录内**、只差 `(N)` 的名字」——
它自己的注释写明「父目录不同就永远不成一组」。而本次这批重复的形状**恰好相反**:
同一个资源落在**不同的日期目录**里,名字一模一样、父目录不同。两者判据互斥,
所以不能合。⚠️ 别把两套混着跑:两套的"留哪份"规则不同,同时跑会**互相删掉对方那份**。

## 这批重复是怎么来的(已在 `quark_transfer` 修掉根因)
`_ensure_dir` 原来只创建、从不查找 ⇒ fid 缓存一丢就新建一个 `redian监听_MMDD`,
而查重**按目录**做 ⇒ 换了目录就一个都认不出来 ⇒ 同一资源又存一份。
实测盘上 19 个 `redian监听_*`,约每 3 天一个,`高性价比人生指南-HowToLiveBetter-现代-338页.pdf`
在 `redian监听_0929` 与 `redian监听_1004` 里各一份。
**根因已修**(`_adopt_existing_dir` + 合并写缓存),本模块只负责**清历史账**。

## 判据
一组 = **同名 + 同大小**,且副本分布在**≥2 个不同的「家」**(`redian监听*`)里。
⚠️ 三条收紧,每条都是对着一次误判风险加的:
  ① **必须同大小**(缺一边就不成组):"每个包里都带一份的同名文件"改名不换大小是常态,
     但大小一致才可能是同一份东西 —— 宁少删,不误删;
  ② **`_is_notice` 类不参与**(必看/说明/解压方法…):那是**引流**,归另一条链,别在这里顺手删;
  ③ **已被我方分享链指着的那份绝对不删** —— 见 `protected_fids`。

## 硬判据:不许删掉"已发出的分享链"背后的那份
`share/mypage/detail` 能列出我方全部分享(带 `first_fid`),删掉它 = 对方打开链接看到「已失效」。
这是**发出去就收不回**的损害,所以做成**一票否决**:候选里只要命中保护名单就跳过,
并在报告里说清"跳过是因为它背后有链"。`file_num>1` 的分享只保护 `first_fid` 一个,
所以那 67 条额外连名字一起保护(名字能连坐,比漏保护好)。
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

#: API 调用上限(与 `pan_dedupe.BUDGET` 同一个理由:扫描要**说得出"没扫完"**)。
BUDGET = int(os.environ.get("QUARK_DUP_BUDGET") or 600)

#: 小于这个体积的**不参与**(清理收益极小而误删代价一样大)。
MIN_BYTES = 1 * 1024 * 1024

#: 「留哪份」的说明类痕迹 —— 那是引流文件,归另一条链
_NOTICE_WORDS = ("必看", "说明", "解压", "使用帮助", "教程", "注意", "公告", "先看")


def _is_notice(name: str) -> bool:
    return any(w in str(name) for w in _NOTICE_WORDS)


def home_of(dir_name: str) -> str:
    """从目录名取"家"(`redian监听_1004` → `redian监听`);不带日期前缀的原样返回。"""
    return str(dir_name or "").split("_")[0]


def home_mmdd(dir_name: str) -> str:
    """取目录名里的 `MMDD`(没有就返回空)—— 用来做"10 月份之内"这类范围过滤。

    实测目录名形如 `redian监听_1004` / `redian监听_0928_2`;不带日期的是最早那个家。
    """
    parts = str(dir_name or "").split("_")
    for p in parts[1:]:
        if p.isdigit() and len(p) == 4:
            return p
    return ""


def list_homes(qt: Any, base: str = "redian监听") -> list[dict]:
    """列出所有「家」目录(搜索一次命中,不做分页重扫 —— 见 `QuarkTransfer.search_files`)。"""
    hits = qt.search_files(base, size=50)
    homes = [h for h in hits if h.get("dir")
             and (str(h.get("file_name") or "") == base
                  or str(h.get("file_name") or "").startswith(f"{base}_"))]
    homes.sort(key=lambda h: int(h.get("updated_at") or 0), reverse=True)
    return homes


def protected_fids(qt: Any) -> tuple[set[str], set[str], int]:
    """我方**已发出的分享链**保护的 fid 与文件名 → `(fids, names, 分享条数)`。

    ⚠️ `first_fid` 只保护每条链的第一个文件;`file_num>1` 的链其余项拿不到 fid,
    只能连**名字**一起保护(名字能连坐,比漏保护好)。
    """
    fids: set[str] = set()
    names: set[str] = set()
    n = 0
    for page in range(1, 41):                       # 分页上限 40×50 = 2000 条
        try:
            d = qt._request("GET", "/1/clouddrive/share/mypage/detail",
                            api="https://drive-pc.quark.cn",
                            params={"share_id": "", "_page": page, "_size": 50,
                                    "_order": "created_at:desc"})
        except Exception as exc:                    # noqa: BLE001 - 拿不到保护名单就**必须整单作废**
            raise RuntimeError(f"取我方分享名单失败({type(exc).__name__}: {exc}),"
                               "没有保护名单时删除是不安全的") from exc
        lst = list((d.get("data") or {}).get("list") or [])
        for a in lst:
            n += 1
            if a.get("first_fid"):
                fids.add(str(a["first_fid"]))
            if int(a.get("file_num") or 0) > 1 and a.get("title"):
                names.add(str(a["title"]).strip())
        if len(lst) < 50:
            break
    return fids, names, n


def _walk(qt: Any, fid: str, home: str, path: str, budget: dict,
          depth: int = 0) -> list[dict]:
    """递归收集一个家下的**文件**(目录只作为路径,不作为条目)。"""
    if depth > 5 or budget["n"] >= budget["cap"]:
        return []
    budget["n"] += 1
    try:
        items = qt.list_dir(fid)
    except Exception as exc:                        # noqa: BLE001 - 单个子目录挂不该拖垮整次扫描
        logger.warning("扫描 %s 时列目录失败:%s", path, str(exc)[:100])
        return []
    out: list[dict] = []
    for it in items:
        nm = str(it.get("file_name") or "")
        if it.get("dir"):
            out.extend(_walk(qt, str(it["fid"]), home, f"{path}/{nm}", budget, depth + 1))
        elif nm:
            out.append({"home": home, "path": path, "name": nm,
                        "size": int(it.get("size") or 0), "fid": str(it.get("fid") or "")})
    return out


def build_plan(qt: Any, *, home_prefix: str = "redian监听", mmdd_prefix: str = "10",
               depth: int = 5, budget_cap: int | None = None) -> dict:
    """扫描并给出"删哪些副本"的计划。**只读,不删任何东西。**

    `mmdd_prefix="10"` = 用户口径「先删 **10 月份之内**保存进来的重复文件」:
    只**动** 10 月的副本(不动更早的历史),且组里至少要有一个 10 月的副本才纳入。
    """
    budget = {"n": 0, "cap": budget_cap or BUDGET}
    homes = list_homes(qt, home_prefix)
    files: list[dict] = []
    for h in homes:
        nm = str(h.get("file_name") or "")
        files.extend(_walk(qt, str(h["fid"]), nm, nm, budget))

    prot_fids, prot_names, n_shares = protected_fids(qt)

    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for f in files:
        if f["size"] >= MIN_BYTES and not _is_notice(f["name"]):
            groups[(f["name"], f["size"])].append(f)

    plan: list[dict] = []
    skipped: list[dict] = []
    for (name, size), copies in groups.items():
        by_home = {c["home"] for c in copies}
        if len(by_home) < 2:
            continue                                # 同一个家里的同名同大小:不是本次目标
        oct_copies = [c for c in copies if home_mmdd(c["home"]).startswith(mmdd_prefix)]
        if not oct_copies:
            continue                                # 用户口径:先只清 10 月的账
        kept = [c for c in copies if c["fid"] in prot_fids or name in prot_names]
        if kept:
            keep = kept[0]
            drops = [c for c in oct_copies if c["fid"] != keep["fid"]
                     and c["fid"] not in prot_fids]
        else:
            # 没有链子指着 ⇒ 留**最近在用的那个家**里的那份(家按 updated_at 已倒序)
            order = [str(h.get("file_name")) for h in homes]
            keep = sorted(oct_copies,
                          key=lambda c: order.index(c["home"]) if c["home"] in order else 999)[0]
            drops = [c for c in oct_copies if c["fid"] != keep["fid"]]
        if not drops:
            skipped.append({"name": name, "size": size, "keep": keep, "why": "只剩一份 / 都被保护"})
            continue
        plan.append({"name": name, "size": size, "keep": keep, "drops": drops,
                     "raw_copies": len(copies)})

    plan.sort(key=lambda p: -p["size"])
    return {"plan": plan, "skipped": skipped,
            "n_drop": sum(len(p["drops"]) for p in plan),
            "groups": len(plan),
            "freed": sum(p["size"] * len(p["drops"]) for p in plan),
            "scanned_homes": len(homes), "scanned_files": len(files),
            "calls": budget["n"], "protected": len(prot_fids), "shares": n_shares,
            "complete": budget["n"] < budget["cap"]}
