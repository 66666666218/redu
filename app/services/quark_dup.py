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
- **弱模式** —— 单看名字**不足以判定**(`必看`/`说明`/`教程`/`注意`/`截图`/`步骤`…)——
  资源本身也可能叫这个(实测 `字体安装方法说明.docx`、`PPT教学资料，手把手教你如何做PPT`、
  `高中英语151组最容易拼错的单词，考场上一定要注意！.docx` **都是真资源**)。
  ⇒ **一律只进「待确认」清单,永不自动删** —— 见 `classify` 里那段:
  原来的"要横跨 ≥2 个包才删"在这份盘上**逻辑失效**(同一份资源本来就横跨多个家,
  于是那条判据恒成立),而这类总共只有 269 MiB,**省不到空间却会误删资源**。

⚠️ **体积不是判据**:引流有 60 字节的 txt,也有 1.3 MB 的二维码截图;
而真资源 `地理答案.zip` 只有 7 KB。别用大小筛。

## 两条硬保护(一票否决)
1. **我方已发出的分享链指着的那份绝不删** —— `share/mypage/detail` 给出每条链的 `first_fid`,
   删掉 = 对方打开链接看到「已失效」,发出去就收不回。`file_num>1` 的链还要连**名字**一起保护。
2. **我们自己的宣传简介**(`简介.doc`)绝不删 —— 那是本轮刚放进去的。
"""
from __future__ import annotations

import os
import re
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
#
#: ⚠️⚠️ **两次真数据误判之后收紧的**(都发生在执行前的 dry-run,一个是"差一点就删"):
#:   ① **`公众号` / `推广` / 裸 `扫码` / 裸 `二维码` 必须去掉** ——
#:      实测真课件叫 `课时19485_…ppt【公众号dc008免费分享】.pptx`,那是**来源标注挂在名字尾部**,
#:      文件本身就是资源;`软件推广合作协议.docx` 也是真文档。
#:      ⇒ 换成更具体的形态(`微信扫码`/`扫码注册`/`扫码进群`/`扫码打印`/`扫码使用`)。
#:   ② **判据要"剥掉括号内容之后再比"**(见 `is_promo_name`) ——
#:      `黄一鸣曝王S聪聊天记录【先保存才能看】.rar` 是**真资源 + 广告后缀**,
#:      按整名匹配会把资源本体删掉。
STRONG_PATTERNS = (
    "切勿卸载", "不要卸载", "卸载网盘", "别卸载",
    "后续资源会同步更新", "请第二天重新打开", "重新打开网盘", "打开网盘下载",
    "下载前必看", "解压前必看", "先保存在", "先保存再", "先保存才能看", "保存后随时观看",
    "不易丢", "不要直接打开", "要下载到电脑", "下载到电脑里面",
    "点进去分开转存", "分开转存再使用",
    # ⚠️ **不要用裸 `代发` / 裸 `引流`**(第三次收紧,同一次 dry-run 里看出来的):
    #   `快递代发代收合作协议.docx` 是真文档;
    #   `啊腾去引流版.apk` 的「**去**引流版」意思可能正好相反(去广告的干净版)。
    #   换成只可能是广告的**具体形态**。
    "加群", "进群", "赚米",
    "招代发", "代发兼职", "代发助手", "代发赚", "代发日入",
    "微信扫码", "扫码注册", "扫码进群", "扫码打印", "扫码使用", "扫码即可", "扫码自助",
    "收藏不迷路", "防止丢失", "防丢失", "收藏防",
)

#: 弱模式:单看名字不足以判定 ⇒ **只报告,永不自动删**(见 `classify` 的说明)
WEAK_PATTERNS = ("必看", "说明", "教程", "使用帮助", "注意", "公告",
                 "截图", "内部", "步骤", "福利")

#: 绝不删的文件名(我们自己的东西)
PROTECT_NAMES = ("简介.doc",)

#: 名字里的「标注」部分:【…】/（…）/(…)/[…]。
#: 资源名尾部常挂来源标注(`【公众号xxx免费分享】`),而引流文件的**整名就是广告**。
_BRACKET_RE = re.compile(r"[【（(\[][^】）)\]]*[】）)\]]")


def is_promo_name(name: str) -> bool:
    """名字是否是**强模式引流**(判据:剥掉括号内容**之后**仍命中强模式)。

    ⚠️ **为什么必须剥括号**(2026-10-08 实测):
    `课时19485_…ppt【公众号dc008免费分享】.pptx` 是真课件、`黄一鸣曝王S聪聊天记录【先保存才能看】.rar`
    是真资源带广告后缀 —— 按整名匹配会把**资源本体**删掉。
    剥掉标注后:前者剩 `课时19485_…ppt`(不含强模式,安全),后者剩 `黄一鸣曝王S聪聊天记录`(安全);
    而真正的引流文件(整名即广告)剥完**一字不少**,照样命中。
    """
    body = _BRACKET_RE.sub("", str(name or ""))
    return any(p in body for p in STRONG_PATTERNS)


def home_mmdd(dir_name: str) -> str:
    """取目录名里的 `MMDD`(没有就返回空)—— 做"10 月份之内"这类范围过滤。"""
    for p in str(dir_name or "").split("_")[1:]:
        if p.isdigit() and len(p) == 4:
            return p
    return ""


def list_homes(qt: Any, base: str = "redian监听") -> list[dict]:
    """列出所有「家」目录,**按「新」在前**(搜索一次命中,不做分页重扫)。

    ⚠️⚠️ **排序用名字里的 `MMDD`,不能用 `updated_at`**(2026-10-08 实测踩到):
    `redian监听_0929` 的 `updated_at` 被改成了当天(只是被动过),
    按它排序 `_0929` 会跑到 `_1007` 前面 —— 而 `_0929` 是 **9 月 29 日**建立的家。
    这个顺序**直接决定"跨目录重复留哪份"**,排错了就会留下旧家的那份、
    删掉当前家正在用的那份(空间确实省了,但目录里从此缺一角)。
    名字里的日期是建立那天写死的,不会被别处触碰。`updated_at` 只做同月并列时的次键。
    """
    hits = qt.search_files(base, size=50)
    homes = [h for h in hits if h.get("dir")
             and (str(h.get("file_name") or "") == base
                  or str(h.get("file_name") or "").startswith(f"{base}_"))]
    homes.sort(key=lambda h: (home_mmdd(str(h.get("file_name") or "")),
                              int(h.get("updated_at") or 0)), reverse=True)
    return homes


#: 拉我方分享名单的翻页上限。⚠️ 实测分享总数**超过 2000 条**(第 45 页仍有 50 条、第 60 页空),
#: 所以原来的 40 页上限会**静默截断** —— 更早发出的链接背后的文件就没进保护名单,
#: 而"没进保护名单"在这套判据里等于"可以删"。抬到 400 页,并在**拉满仍未到底**时直接抛。
SHARE_PAGE_CAP = 400


def protected_fids(qt: Any) -> tuple[set[str], set[str], int]:
    """我方**已发出的分享链**保护的 fid 与文件名 → `(fids, names, 分享条数)`。

    ⚠️⚠️ **拉不完就必须抛,不能返回一个"看着有内容"的半份**(2026-10-08 实测踩到):
    原来的 40 页上限(2000 条)在这账号上**正好被填满**,于是保护名单**少了一大截**,
    而调用方无从分辨"保护了 2000 条"与"保护了全部" —— 那会导致删掉一条仍在生效的
    分享链背后的文件(对方打开看到「已失效」,发出去就收不回)。
    现在的判据:拉到**短页**(< 50)才算到底;否则一直翻到 `SHARE_PAGE_CAP`,仍没到底就抛。
    """
    fids: set[str] = set()
    names: set[str] = set()
    n = 0
    for page in range(1, SHARE_PAGE_CAP + 1):
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
        if len(lst) < 50:                           # 短页 = 到底了
            break
    else:
        raise RuntimeError(f"分享名单翻到 {SHARE_PAGE_CAP} 页仍未到底(已 {n} 条),"
                           "保护名单**不完整** ⇒ 拒绝用它做删除判据")
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
        strong = is_promo_name(name)      # ⚠️ 必须剥掉括号标注再比,见 is_promo_name
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
        elif weak:
            # ⚠️⚠️ **弱模式一律只报告,不删**(2026-10-08 当场改掉,原规则在真数据上是**系统性误判**)。
            #
            # 原来的规则是「弱模式 + 横跨 ≥2 个资源包 ⇒ 删」。它在**这份盘上逻辑失效**:
            # 同一份资源本来就被重复保存到多个家(这正是本次要清的东西),
            # 于是**包里几乎每个文件都"横跨多个家"** —— 那条判据退化成
            # 「名字里带 必看/教程/注意/截图/内部/步骤/福利 就删」。
            # 实测误判(计划里真实存在的名字):
            #   `高中英语151组最容易拼错的单词，考场上一定要注意！.docx`  ← 真学习资料,只因含「注意」
            #   `视频教程(1).mp4` / `编年史教程1(1).zip` / `道格测试入口以及教程.rar` ← 多半是资源本体
            # 而这一类总共只有 269 MiB —— **省不到空间,却会误删资源**,收益/风险完全不成比例。
            # ⇒ 降级成"待确认":列出来给人看,由人来判。
            review.append({"name": name, "size": size, "home": sorted(homes)[0],
                           "fid": safe[0]["fid"], "why": "名字像引流(弱模式)—— 需人工确认"})
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
