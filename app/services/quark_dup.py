"""夸克盘清理:**跨目录重复** + **别人的引流文件**(2026-10-08)。

## 为什么不是 `pan_dedupe`
那个模块是**迅雷**用的,判据是「**同一父目录内**、只差 `(N)` 的名字」——
它自己的注释写明「父目录不同就永远不成一组」。而本批重复的形状**恰好相反**:
同一个资源落在**不同的日期目录**里,名字一模一样、父目录不同。两者判据互斥,
所以不能合。⚠️ 别把两套混着跑:两套的"留哪份"规则不同,同时跑会**互相删掉对方那份**。

## 重复是怎么来的(根因已在 `quark_transfer` 修掉)
`_ensure_dir` 原来只创建、从不查找 ⇒ fid 缓存一丢就新建一个 `redian监听_MMDD`,
而查重**按目录**做 ⇒ 换了目录就一个都认不出来 ⇒ 同一资源又存一份。
实测盘上 19 个 `redian监听_*`(约每 3 天一个)。**根因已修**(`_adopt_existing_dir` +
合并写缓存),本模块只清历史账。

## 引流判据:分**强/弱**两档(这是本模块最需要小心的地方)
真数据采样(423 条)看下来,引流有两种形态,而**误删资源是不可逆的**,所以:

- **强模式** —— 名字本身就是**一条关于"怎么用网盘/怎么加群"的指令**
  (`切勿卸载网盘` / `下载前必看` / `先保存在观看更高清` / `点进去分开转存再使用` …)。
  这类**直接删**。
- **弱模式** —— 单看名字**不足以判定**(`必看`/`说明`/`教程`/`截图`/`第三步`…)——
  资源本身也可能叫这个(实测 `字体安装方法说明.docx`、`PPT教学资料，手把手教你如何做PPT`
  都是**真资源**)。这类**只有"同名同大小出现在 ≥2 个不同的资源包"才删**
  (每个包都带一份 = 模板化投放的特征),否则只进**待确认**清单给人看。

⚠️ **体积不是判据**:引流有 60 字节的 txt,也有 1.3 MB 的二维码截图;
而真资源 `地理答案.zip` 只有 7 KB。别用大小筛。

## 两条硬保护(一票否决)
1. **我方已发出的分享链指着的那份绝不删** —— `share/mypage/detail` 给出每条链的 `first_fid`,
   删掉 = 对方打开链接看到「已失效」,发出去就收不回。`file_num>1` 的链还要连**名字**一起保护。
2. **我们自己的宣传简介**(`简介.doc`)绝不删 —— 那是本轮刚放进去的。
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

#: API 调用上限。⚠️ 必须**说得出"没扫完"** —— 首轮 600 就把 19 个家扫漏了,
#: 而漏扫会让"这份文件只在一个家里"这种判断**变成假的**(它其实在没扫到的那个家里也有一份)。
BUDGET = int(os.environ.get("QUARK_CLEANUP_BUDGET") or 2500)

#: 重复检测的体积下限(引流不限)。删小的重复省不到空间,却一样承担误删风险。
DUP_MIN_BYTES = 1 * 1024 * 1024

#: ★ 强模式:名字本身就是"怎么用网盘 / 怎么加群"的**指令**。全部来自真数据采样。
STRONG_PATTERNS = (
    "切勿卸载", "不要卸载", "卸载网盘", "别卸载",
    "后续资源会同步更新", "请第二天重新打开", "重新打开网盘", "打开网盘下载",
    "下载前必看", "解压前必看", "先保存在", "先保存再", "先保存才能看", "保存后随时观看",
    "不易丢", "不要直接打开", "要下载到电脑", "下载到电脑里面",
    "点进去分开转存", "分开转存再使用",
    "加群", "进群", "扫码", "二维码", "公众号", "代发", "引流", "推广",
    "收藏不迷路", "防止丢失", "防丢失", "收藏防",
)

#: 弱模式:单看名字不足以判定 ⇒ 必须"同名同大小横跨 ≥2 个资源包"才删
WEAK_PATTERNS = ("必看", "说明", "教程", "使用帮助", "注意", "公告",
                 "截图", "内部", "步骤", "福利")

#: 绝不删的文件名(我们自己的东西)
PROTECT_NAMES = ("简介.doc",)


def home_mmdd(dir_name: str) -> str:
    """取目录名里的 `MMDD`(没有就返回空)—— 做"10 月份之内"这类范围过滤。"""
    for p in str(dir_name or "").split("_")[1:]:
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
    """我方**已发出的分享链**保护的 fid 与文件名 → `(fids, names, 分享条数)`。"""
    fids: set[str] = set()
    names: set[str] = set()
    n = 0
    for page in range(1, 41):                       # 分页上限 40×50 = 2000 条
        try:
            d = qt._request("GET", "/1/clouddrive/share/mypage/detail",
                            api="https://drive-pc.quark.cn",
                            params={"share_id": "", "_page": page, "_size": 50,
                                    "_order": "created_at:desc"})
        except Exception as exc:                    # noqa: BLE001 - 拿不到保护名单就必须整单作废
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
          depth: int = 0, max_depth: int = 5) -> list[dict]:
    """递归收集一个家下的**文件**(目录只作为路径,不作为条目)。

    ⚠️ `max_depth` 必须**显式传**:每深一层就多一次 `list_dir`(实测约 1.2 秒),
    而一个家有几十个子目录 ⇒ 全树扫 19 个家会跑到小时级,还会先撞预算、
    让"这份文件只在一个家里"变成假判断(没扫到的家里其实也有)。
    实测重复都出现在**两层以内**(家 → 资源目录 → 文件),所以默认的调用方传 2。
    """
    if depth > max_depth or budget["n"] >= budget["cap"]:
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
            out.extend(_walk(qt, str(it["fid"]), home, f"{path}/{nm}", budget,
                             depth + 1, max_depth))
        elif nm:
            out.append({"home": home, "path": path, "name": nm,
                        "size": int(it.get("size") or 0), "fid": str(it.get("fid") or "")})
    return out


def classify(files: list[dict], prot_fids: set[str], prot_names: set[str], *,
             mmdd_prefix: str = "10", home_order: list[str] | None = None) -> dict:
    """把扫到的文件分成三堆:**删** / **待确认** / **留**。纯函数,可离线测。

    `home_order` = 「家」的优先序(**越靠前越新**,由 `build_plan` 按 `updated_at` 倒序给出)——
    跨目录重复时**留最靠前那个家里的那份**。"留哪份"必须显式传进来:
    写死成"按名字排序"会让结果随目录名漂(而目录名带日期,等于按日期排,看着对但没依据)。
    """
    rank = {h: i for i, h in enumerate(home_order or [])}
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for f in files:
        if f["fid"] and home_mmdd(f["home"]).startswith(mmdd_prefix):
            groups[(f["name"], f["size"])].append(f)

    delete: list[dict] = []
    review: list[dict] = []
    protected_left: list[dict] = []
    for (name, size), copies in groups.items():
        homes = {c["home"] for c in copies}
        strong = any(p in name for p in STRONG_PATTERNS)
        weak = any(p in name for p in WEAK_PATTERNS)
        # 保护:名字级(我方某条链的标题)或 fid 级(某条链的 first_fid)
        if name in prot_names or name in PROTECT_NAMES:
            protected_left.append({"name": name, "size": size, "why": "名字在保护名单里"})
            continue
        safe = [c for c in copies if c["fid"] not in prot_fids]
        if len(safe) < len(copies):
            protected_left.append({"name": name, "size": size, "n": len(copies) - len(safe),
                                   "why": "有副本被已发出的分享链指着"})
        if not safe:
            continue
        if strong:
            why = "引流(强模式:名字就是网盘/加群指令)"
        elif weak and len(homes) >= 2:
            why = f"引流(弱模式 + 横跨 {len(homes)} 个资源包)"
        elif weak:
            # ⚠️ **只报告不删**:弱模式单家里出现,资源本体也可能长这样
            review.append({"name": name, "size": size, "home": sorted(homes)[0],
                           "fid": safe[0]["fid"], "why": "名字像引流,但只出现在一个包里 —— 不敢判"})
            continue
        elif len(homes) >= 2 and size >= DUP_MIN_BYTES:
            # 跨目录重复:留**最近在用的那个家**里的那份
            keep = min(safe, key=lambda c: rank.get(c["home"], 999))
            safe = [c for c in safe if c["fid"] != keep["fid"]]
            why = f"跨目录重复(留 {keep['home']})"
        else:
            continue
        for c in safe:
            delete.append({**c, "why": why, "copies": len(copies)})
    delete.sort(key=lambda d: (d["why"], -d["size"]))
    return {"delete": delete, "review": review, "protected_left": protected_left,
            "n_delete": len(delete), "freed": sum(d["size"] for d in delete)}


def build_plan(qt: Any, *, home_prefix: str = "redian监听", mmdd_prefix: str = "",
               depth: int = 2, budget_cap: int | None = None) -> dict:
    """扫描并给出清理计划。**本函数只读,不删任何东西。**

    `depth=2`(家 → 资源目录 → 文件):实测跨目录重复都出现在这一层,而每深一层
    就多一次约 1.2 秒的 `list_dir` —— 全树扫 19 个家会跑到小时级,还会先撞预算。
    `mmdd_prefix=""` = 所有家都看(默认);传 `"10"` 则只动 10 月的账。
    """
    budget = {"n": 0, "cap": budget_cap or BUDGET}
    homes = list_homes(qt, home_prefix)
    order = [str(h.get("file_name")) for h in homes]
    files: list[dict] = []
    for h in homes:
        nm = str(h.get("file_name") or "")
        files.extend(_walk(qt, str(h["fid"]), nm, nm, budget, 0, depth))
    prot_fids, prot_names, n_shares = protected_fids(qt)
    out = classify(files, prot_fids, prot_names, mmdd_prefix=mmdd_prefix, home_order=order)
    out.update({"scanned_homes": len(homes), "scanned_files": len(files),
                "calls": budget["n"], "protected": len(prot_fids), "shares": n_shares,
                "complete": budget["n"] < budget["cap"]})
    return out


def apply_plan(qt: Any, plan: dict, *, batch: int = 100) -> dict:
    """按计划**删进回收站**(可捞回)。返回 `{"deleted": n, "bytes": n, "failed": [...]}`。

    ⚠️ 只走 `delete_files(..., to_recycle=True)`:**永不彻底删**。
    清单丢了还能捞,彻底删就真没了 —— 而盘商回收站是现成的兜底,没有理由不用。
    """
    fids = [d["fid"] for d in plan.get("delete", []) if d.get("fid")]
    by_fid = {d["fid"]: d for d in plan.get("delete", []) if d.get("fid")}
    deleted = 0
    freed = 0
    failed: list[dict] = []
    for i in range(0, len(fids), batch):
        chunk = fids[i:i + batch]
        try:
            qt.delete_files(chunk, to_recycle=True)
            deleted += len(chunk)
            freed += sum(by_fid[f]["size"] for f in chunk)
        except Exception as exc:                    # noqa: BLE001 - 一批失败不该中断整轮
            failed.append({"n": len(chunk), "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
            logger.warning("删除失败(共 %d 个):%s", len(chunk), str(exc)[:160])
    return {"deleted": deleted, "bytes": freed, "failed": failed}
