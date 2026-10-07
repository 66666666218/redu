# -*- coding: utf-8 -*-
"""跨链「**谁先看到这份资源**」台账(2026-10-07)。

## 它回答什么问题
用户口径:「抖音基本上是最先开始的,然后公众号,一些大瓜从微博中可以更快获取到」
—— 这是一个**关于平台先后顺序的假设**。假设不能靠印象,要有台账。

本模块把**每条链各自记着的时间**摊到一张表上:

    ┌────────┬──────────────────────┬─────────────────────┐
    │ 链     │ 时间字段             │ 它是什么时刻        │
    ├────────┼──────────────────────┼─────────────────────┤
    │ 抖音   │ douyin_leads.publish_at   │ **帖子发布时间**(真值)│
    │ 公众号 │ wechat_articles.publish_at│ **文章发布时间**(真值)│
    │ 迅雷群 │ xunlei_group_shares.msg_time │ **群消息时间**    │
    │ 迅雷盘 │ xunlei_resources.synced_at   │ ⚠️ 扫盘时间(偏晚) │
    │ 公开平台│ discovered_pan_links.found_at│ ⚠️ 我们发现的时刻  │
    └────────┴──────────────────────┴─────────────────────┘

然后按**资源身份**归并,给出"这份资源各链分别哪天看到的、谁先谁后、差多久"。

## ⚠️⚠️ 身份判定:精确键在这里是**坏的**(实测,不是推测)
先用现成的 `core_resource_name` 试过,同一份《高性价比人生指南》被三链记成三种名字,
**精确键会把它们判成三个不同资源** —— 于是报告会输出"各链零交集",
而那是**假结论**(本仓最忌讳的静默失败)。实测原文:

    抖音   「都快点去看《高性价比人生指南》!GitHub开源20天,直接飙到近2万Star…」
    迅雷群 「🔥🔥高性价比人生指南」
    公众号 「2026《高性价比人生指南》 PDF电子版【可下载】」
    公众号 「高性价比人生指南 共638页 pdf电子版【可打印可保存】」

⇒ 身份改用**两条信号**(都实测有效):
  ① **CJK bigram 包含度** `|A∩B| / min(|A|,|B|)`:短名「高性价比人生指南」的 bigram
     整个出现在长标题里 ⇒ 1.0。(用 Dice 会漏 —— 长标题和短名的长度差会把分数拉到 0.3 以下。)
  ② **拉丁词命中**:中文名对不上、但英文名对得上的那类(实测「ForgeTax游戏下载…铸剑大师」
     vs「forgetax(铸剑纳贡)防止河蟹」—— 中文**真的不一样**,只有 `forgetax` 这个拉丁词能连上)。

⚠️ **它仍然会误配**(同名不同物/同物不同名靠字符串救不了)。所以报告**必须把每一组的原始名字
并列打出来**给人看 —— 与 `cross_platform_resonance` 同一条纪律:**先只读输出让人看,别急着接卡片**。
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 链的中文标签(报告里显示;与资源库的 `source` 口径保持一致)
CHAINS = ("抖音", "公众号", "迅雷群", "迅雷盘", "公开平台")

#: CJK/拉丁 bigram **包含度**阈值。取 0.85 而不是 0.5:
#: 归并的代价不对称 —— **误并**会把两个资源的先后顺序糊在一起(得出错结论),
#: **漏并**只是少一条样本(结论晚几天出)。所以宁可漏。
CONTAIN = 0.85

#: 短于这个长度的归一化名**不参与匹配** —— 2~3 个字的名字跟什么都像。
MIN_NAME = 4

#: 拉丁词里这些是通用尾巴,命中了也不算"同一份资源"(否则所有 pdf 资源会并成一组)。
_LATIN_STOP = frozenset({
    "pdf", "app", "apk", "ppt", "excel", "word", "zip", "rar", "doc", "docx",
    "txt", "mp4", "mkv", "com", "www", "http", "https", "html", "exe", "iso",
    "and", "the", "for", "ios", "win", "mac", "pc", "img", "src", "jpg", "png",
    "gif", "webp", "zhimg", "picx",
})

#: ⚠️⚠️ **必须含字母**,不能只是 `[a-z0-9]{3,}`(2026-10-07 实测踩到):
#: 那个会把**纯数字**当词 —— 年份 `2027` / `2026` 于是成了"身份"。
#: 实测后果:「2027公考资料合集」与「2027最新行测5000题…」被那条规则判成 sim=1.0
#: 并成一组(两者**毫无关系**,纯属都写着 2027)。所以要求**至少一个字母**,
#: 而且长度 ≥4 —— 缩写如 `ps`、`ai` 太泛,不足以当身份。
_LATIN_RE = re.compile(r"(?=[a-z0-9]*[a-z])[a-z0-9]{4,}")

#: ⚠️ **URL 必须先剥掉**(2026-10-07 实测踩到):抖音/公开平台的标题里**带着整条盘链**,
#: 归一化后 `httpspanquarkcnsa2f2c0aacbf4` 成为名字的一部分 —— 而**人人都有** `panquarkcn`
#: 这段,于是两条毫不相关的资源会因为"共享域名 bigram"而互相拉近。
_URL_RE = re.compile(r"https?://\S+|pan\.\w+\.\w+/s/\w+", re.I)


def _norm(name: str) -> str:
    """归一化:剥 URL → 只留 CJK + 拉丁字母 + 数字 → 小写。

    ⚠️ **emoji 必须剥掉**(实测踩到):迅雷群的资源名常带「🔥🔥」「📤」「🕹」,
    留着它们会让「🔥🔥高性价比人生指南」与「高性价比人生指南」**差 2 个 bigram**。
    """
    s = _URL_RE.sub(" ", str(name or ""))
    return re.sub(r"[^0-9a-z一-鿿]+", "", s.lower())


def _grams(s: str) -> set[str]:
    from app.services.events import _bigrams

    return _bigrams(s)


def _latin_tokens(s: str) -> set[str]:
    return {t for t in _LATIN_RE.findall(str(s or "").lower()) if t not in _LATIN_STOP}


def latin_similarity(a: set[str], b: set[str]) -> float:
    """两组拉丁词的 **Jaccard**。

    ⚠️ **不能用"有交集就算命中"**(2026-10-07 实测踩到):那样一个**共用词**就能把
    两条不相干的资源并到一起 —— 实测「ForgeTax游戏下载…」把
    「友情粉碎机🔥多人合作神作PEAK…」并了进来,只因两者都带 `steam`(泛词,谁都能带)。
    改成 Jaccard 后,共有 1 个词 / 并集 3 个 = 0.33 < 阈值 ⇒ 不并。
    而真正同一份资源的(`ForgeTax` vs `forgetax`)两边词集**几乎相等** ⇒ 1.0。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(ga: set[str], gb: set[str]) -> float:
    """bigram **包含度** = 交集 / **较短那个**的大小(与 Dice 不同,对长度差免疫)。

    「短名 ⊂ 长标题」时 = 1.0 —— 这正是跨链最常见的形态(抖音写一长句、
    群里只写资源名)。Dice 在这里会因分母被长标题放大而漏掉(实测 <0.3)。
    """
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


class _Row(dict):
    """一条"某链在某时刻看到某个名字"的记录(dict 子类,便于直接打日志)。"""


def _rows_of_chain(session: Session, user_id: int, chain: str,
                   days: int) -> list[_Row]:
    """读**单条链**的 (名字, 时刻)。取不到就返回空并留一条 warning —— 别让一张表缺列崩掉整份报告。

    ⚠️⚠️ **筛选和排序必须是同一个时间**(2026-10-07 实测踩到):原来是"按**入库时间**筛选、
    取**发布时间**当先后",于是**老文章今天才被同步进来**时,它带着一个几周前的发布时间
    **混进窗口** —— 实测 14 天窗口里冒出 `公众号 2026-08-05`,把"公众号最先"的计数**虚高**。
    窗口的意义是"这几条链都在线的这段",那**筛的就得是同一个量**:
    `coalesce(发布时间, 入库时间)`。
    """
    cutoff = datetime.now() - timedelta(days=days)
    out: list[_Row] = []
    try:
        if chain == "抖音":
            from app.db.models import DouyinLead

            when = func.coalesce(DouyinLead.publish_at, DouyinLead.found_at)
            for r in session.scalars(select(DouyinLead).where(
                    DouyinLead.user_id == user_id, when >= cutoff)):
                # ⚠️ **用 title 而不是 mark**:`mark` 是口令别名,`kind=none` 时它是
                #    乱码一样的「咐置铸剑大师叩苓」(实测),拿它当身份会凭空造出一堆假资源。
                out.append(_Row(chain=chain, name=str(r.title or ""),
                                ts=r.publish_at or r.found_at,
                                detail=f"pub={r.publish_at or '—'} kind={r.kind}"))
        elif chain == "公众号":
            from app.db.models import WechatArticle, WechatPanLink

            when = func.coalesce(WechatArticle.publish_at, WechatArticle.created_at)
            for t, ts in session.execute(
                    select(WechatArticle.title, when)
                    .join(WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
                    .where(WechatPanLink.user_id == user_id, when >= cutoff)):
                out.append(_Row(chain=chain, name=str(t or ""), ts=ts, detail=""))
        elif chain == "迅雷群":
            from app.db.models import XunleiGroupShare

            when = func.coalesce(XunleiGroupShare.msg_time, XunleiGroupShare.synced_at)
            for r in session.scalars(select(XunleiGroupShare).where(
                    XunleiGroupShare.user_id == user_id, when >= cutoff)):
                out.append(_Row(chain=chain, name=str(r.title or ""),
                                ts=r.msg_time or r.synced_at,
                                detail=f"msg={r.msg_time or '—'} status={r.status}"))
        elif chain == "迅雷盘":
            from app.db.models import XunleiResource

            for r in session.scalars(select(XunleiResource).where(
                    XunleiResource.user_id == user_id,
                    XunleiResource.synced_at >= cutoff)):
                out.append(_Row(chain=chain, name=str(r.name or ""), ts=r.synced_at,
                                detail="⚠️ 扫盘时间(真实到手时刻可能更早)"))
        elif chain == "公开平台":
            from app.db.models import DiscoveredPanLink

            for r in session.scalars(select(DiscoveredPanLink).where(
                    DiscoveredPanLink.user_id == user_id,
                    DiscoveredPanLink.found_at >= cutoff)):
                out.append(_Row(chain=chain, name=str(r.title or ""), ts=r.found_at,
                                detail=f"⚠️ 发现时刻 平台={r.platform}"))
    except Exception:  # noqa: BLE001 - 单链读失败不该拖垮整份报告,但必须说出来
        logger.exception("跨链台账:读 %s 失败(该链本轮缺席)", chain)
    return [r for r in out if r.get("ts")]



def first_seen_ledger(session: Session, user_id: int, days: int = 90) -> list[_Row]:
    """五条链的 (名字, 时刻) 全摊平,按时刻升序。**这是唯一的事实来源**,不做任何归并。"""
    rows: list[_Row] = []
    for ch in CHAINS:
        rows.extend(_rows_of_chain(session, user_id, ch, days))
    # 同链同名只留最早 —— 一份资源在某链被发了 20 次,不该撑出 20 条样本
    best: dict[tuple[str, str], _Row] = {}
    for r in rows:
        k = (r["chain"], _norm(r["name"]))
        if k not in best or r["ts"] < best[k]["ts"]:
            best[k] = r
    return sorted(best.values(), key=lambda r: r["ts"])


#: 一组超过这么多条 ⇒ 大概率阈值太松(链式误并),报告里要标出来给人看
MAX_GROUP = 6


def group_by_resource(rows: list[_Row], contain: float = CONTAIN) -> list[dict]:
    """按资源身份归并。**leader 聚类**:每条只跟**本组的种子**(最早那条)比一次。

    ## 为什么是固定种子,而不是"组内任意一条"或"可变代表名"
    两种更常见的做法在这里**实测都会误并**:
      · **可变代表名(取组内最长)**:组一变大代表就换人 ⇒ **传递漂移**。
        实测后果:「300张超全版国庆假期应付对象查岗照片」与「安卓系统有哪些冷门但逆天的
        手机软件?」(两者 containment **0.000**)被并进同一组 —— 中间隔着好几条链式匹配。
      · **单链(跟组内任意一条够像就进)**:A~B、B~C ⇒ A、C 同组,同样会漂。
    ⇒ 种子**取最早那条并固定不动**:同一批数据跑两次结果一样,也不会随组变大而松动。

    ⚠️ 这个做法**能成立**是因为 `containment` 是**对称**的
    (`|A∩B| / min(|A|,|B|)`,短的被长的包含时两个方向都得 1.0)——
    所以"种子是不是最长"**根本不影响判定**;那点当初是我想复杂了。
    """
    groups: list[dict] = []
    for r in rows:
        n = _norm(r["name"])
        if len(n) < MIN_NAME:
            continue                       # 太短的名字跟什么都像,不参与
        g = _grams(n)
        lat = _latin_tokens(r["name"])
        hit = None
        best_s = 0.0
        for cand in groups:
            # 两条信号取**较大**者:中文对得上用包含度,中文对不上但英文名对得上用拉丁词。
            s = max(containment(g, cand["grams"]),
                    latin_similarity(lat, cand["latin"]))
            if s >= contain and s > best_s:
                hit, best_s = cand, s
        if hit is None:
            groups.append({"seed": r["name"], "norm": n, "grams": g, "latin": lat,
                           "members": [r], "sim": 1.0})
            continue
        hit["members"].append(r)
        hit["sim"] = min(hit["sim"], best_s)
    return groups


def ordering_report(session: Session, user_id: int, days: int = 90,
                    contain: float = CONTAIN) -> dict:
    """台账 → **跨链先后**报告。返回 `{"groups", "pairs", "first_counts", "ledger_n"}`。

    `groups` 只留**跨 ≥2 条链**的(单链的资源回答不了"谁先"这个问题)。
    每组给 `order`(链的先后)、`lag_h`(最晚 - 最早)、以及**每一链里见过的一个原始名** ——
    名字必须打出来,否则误配看不出来(见模块头的说明)。
    """
    rows = first_seen_ledger(session, user_id, days=days)
    groups = group_by_resource(rows, contain=contain)

    out_groups: list[dict] = []
    pairs: dict[tuple[str, str], list[float]] = {}
    first_counts: dict[str, int] = {}
    for g in groups:
        by_chain: dict[str, _Row] = {}
        for m in g["members"]:
            cur = by_chain.get(m["chain"])
            if cur is None or m["ts"] < cur["ts"]:
                by_chain[m["chain"]] = m
        if len(by_chain) < 2:
            continue
        order = sorted(by_chain.items(), key=lambda kv: kv[1]["ts"])
        first_counts[order[0][0]] = first_counts.get(order[0][0], 0) + 1
        first_ts = order[0][1]["ts"]
        for i in range(len(order)):
            for j in range(i + 1, len(order)):
                lag = (order[j][1]["ts"] - order[i][1]["ts"]).total_seconds() / 3600
                pairs.setdefault((order[i][0], order[j][0]), []).append(lag)
        n_members = len({_norm(m["name"]) for m in g["members"]})
        out_groups.append({
            "name": g["seed"], "seed": g["seed"], "chains": len(by_chain),
            "members": n_members,
            # ⚠️ 条数超上限 = 阈值太松的信号,**必须标出来**(见 `MAX_GROUP`)
            "bloated": n_members > MAX_GROUP,
            "order": [c for c, _ in order],
            "lag_h": (order[-1][1]["ts"] - first_ts).total_seconds() / 3600,
            "sim": round(g["sim"], 3),
            "timeline": {c: {"ts": r["ts"].isoformat(sep=" ", timespec="seconds"),
                             "name": r["name"][:70], "detail": r["detail"]}
                         for c, r in order},
            # 只有跨链那几条进 timeline,组内其余名字也留下 —— 误并要从这里看
            "all_names": sorted({m["name"][:70] for m in g["members"]}),
        })
    out_groups.sort(key=lambda x: (-x["chains"], -x["members"]))

    def _agg(v: list[float]) -> dict:
        return {"n": len(v), "median_h": round(statistics.median(v), 1),
                "min_h": round(min(v), 1), "max_h": round(max(v), 1)}

    return {"groups": out_groups, "ledger_n": len(rows),
            "pairs": {f"{a}→{b}": _agg(v) for (a, b), v in pairs.items()},
            "first_counts": dict(sorted(first_counts.items(), key=lambda kv: -kv[1])),
            "span": _span_of(rows),
            "truncation_note": TRUNCATION_NOTE}


#: ⚠️⚠️ **左截断** —— 这份台账最容易得出的错结论,先写在报告里,免得自己忘。
TRUNCATION_NOTE = (
    "⚠️ **左截断**:各链**开始收的时间不一样**(公众号最早、抖音线索与公开平台是 10 月初才上的)"
    "—— 所以「谁最先」会被**我们什么时候开始收那条链**带偏:收得早的链天然更容易当'最先'。\n"
    "⇒ 看上面的**各链数据起止**:哪条链起点明显晚,它的'最先'次数就**偏低是应该的**,"
    "反过来起点早的偏高也是应该的。要比较先后,**得把窗口收窄到所有链都在线的那一段**再跑。"
)


def _span_of(rows: list[_Row]) -> dict:
    """各链的**数据起止** —— 判断左截断用(某链只有 3 天数据,它当然很少"最先")。"""
    spans: dict[str, list[datetime]] = {}
    for r in rows:
        lo_hi = spans.setdefault(r["chain"], [r["ts"], r["ts"]])
        lo_hi[0] = min(lo_hi[0], r["ts"])
        lo_hi[1] = max(lo_hi[1], r["ts"])
    return {c: {"from": v[0].isoformat(sep=" ", timespec="seconds"),
                "to": v[1].isoformat(sep=" ", timespec="seconds")}
            for c, v in sorted(spans.items())}


def summary_lines(report: dict) -> list[str]:
    """报告 → 可直接打印/推送的几行(**把误配与左截断写在脸上**)。"""
    lines = [f"台账 {report['ledger_n']} 条(同链同名只留最早),跨链资源 "
             f"{len(report['groups'])} 份"]
    span = report.get("span") or {}
    if span:
        lines.append("各链数据起止:" + " | ".join(
            f"{c} {v['from'][:10]}→{v['to'][:10]}" for c, v in span.items()))
    if report["first_counts"]:
        lines.append("**谁最先看到**(按份数):"
                     + "、".join(f"{k} {v}" for k, v in report["first_counts"].items()))
    for k, v in sorted(report["pairs"].items(), key=lambda kv: -kv[1]["n"]):
        lines.append(f"  {k}: {v['n']} 份,中位 {v['median_h']}h"
                     f"(范围 {v['min_h']}~{v['max_h']}h)")
    lines.append("⚠️ 身份靠名字模糊匹配,会有误配 —— 明细里的原始名要逐组看一眼。"
                 f"(标注 ⚠️膨胀 的组 = 并进的条数超过 {MAX_GROUP},多半阈值太松)")
    return lines


def _first_user_id(db) -> int | None:
    from app.db.models import User

    row = db.query(User).filter(User.enabled.is_(True)).first()
    return int(row.id) if row is not None else None


def chain_ordering_tick(settings=None) -> int:
    """每周推一次台账到**管理群**(2026-10-07)。返回是否推成功(0/1)。

    ## 为什么要**定时**推,而不是"留个脚本要的时候再跑"
    这份台账的用途是**验证一个假设**("抖音最先、然后公众号、微博的大瓜更快")——
    而假设要**样本攒够**才能下结论。留个没人跑的脚本 ⇒ 两周后没人想起它,
    **"该验证的事"就静默地没发生**(本仓的老毛病)。每周一张小卡片,顺手就把样本攒了。

    ⚠️ 窗口取 **14 天**而不是 90:各链历史长度不同(见 `TRUNCATION_NOTE`),
    窗口越长左截断越重。14 天是"各链都在线"和"样本尽量多"之间的折中。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.services.feishu_client import FeishuClient, webhook_for

    hook = webhook_for(settings, "admin")
    if not hook:
        return 0
    from app.db import get_session_local

    db = get_session_local()()
    try:
        uid = _first_user_id(db)
        if uid is None:
            return 0
        rep = ordering_report(db, uid, days=14)
        text = "\n".join(summary_lines(rep))
        if rep["groups"]:
            text += "\n\n**跨链资源(谁先谁后)**\n"
            for g in rep["groups"][:10]:
                flag = " ⚠️写法多" if g["bloated"] else ""
                text += (f"· {g['name'][:34]} —— {' → '.join(g['order'])}"
                         f"(跨 {g['lag_h']:.0f}h){flag}\n")
        text += "\n" + TRUNCATION_NOTE
    finally:
        db.close()
    return 1 if FeishuClient(hook, settings.feishu_secret).send(
        "🧭 跨链先后台账(近 14 天)\n" + text[:3000]) else 0
