# -*- coding: utf-8 -*-
"""「**看到 → 搬成 → 推出**」的交付看板(2026-10-07)。

## 它补的是 `chain_ordering` 答不了的那半
`chain_ordering` 只回答"**谁先看到**"。但先看到不等于及时交付:
抖音比公众号早 80 小时看到,如果搬成又要三天,那 80 小时的提前量**一点没落到客户手里**。
所以这一层量的是**转化 + 时延**,并且**逐段拆开**,好定位到底卡在哪一段。

    发现 ──── 搬成 ──── 推出
     │         │          │
     │         │          └─ 公众号有 `pushed_at`(入库→推送实测中位 0.5h,不是瓶颈)
     │         └─ 2026-10-07 才开始记(`moved_at`),存量行 NULL
     └─ 各链自带(`publish_at` / `msg_time` / `found_at`)

## ⚠️⚠️ 这个模块**必须自己区分「当前」与「窗口内历史」**(2026-10-07 差点栽在这)
做这一层时的第一个版本是"近 7 天失败率 35%",看着像链路一直在坏 ——
**实际是窗口里含了 10-03~10-05 那段(迅雷 refresh_token `invalid_grant`,61 次失败),
而 10-06 起已归零**(10-07:76 成功 / 0 失败)。差一点就照着"一直在坏"去做优化了。
⇒ 报告里**永远同时给两个数**:`current_24h`(现在什么状态)与窗口聚合。
这和 `chain_ordering.TRUNCATION_NOTE` 是同一类错误:**窗口聚合会把"曾经坏过"读成"一直坏"**。
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 至少要有这么多"跨段样本"才给调度建议(样本少的时候给建议 = 假信号)。
MIN_SAMPLE = 20


def mark_newly_moved(row: Any, had_link: bool, now: datetime | None = None) -> None:
    """**首次**搬成时打一个时刻戳。`had_link` **必须由调用方显式给出**,没有默认值。

    ## ⚠️ 为什么强制传 `had_link`(而不是"没戳就打")
    这些落库点**都是 upsert**(同一个 `aweme_id` / `share_id` 每轮都可能再走一遍)。
    如果只判"`moved_at` 为空就打",那么**加这一列之前的存量行**
    (有链、没戳)会在**下一轮被重新处理时打上"现在"的戳** ——
    于是"搬成耗时"算出来是"从发布到现在",**那是编出来的数据**,
    而且它长得很有说服力(样本变多、数字合理),比缺样本危险得多。
    ⇒ 只有**"此前没有链、这一次才拿到"**才是真的首次搬成;存量行**永远保持 NULL**,
      由读取端当"没记"处理(见 `delivery_report` 的 `no_ts`)。
    """
    if had_link:
        return
    # ⚠️⚠️ **还得"真的有链"才算搬成**(2026-10-07,被单测当场抓到):
    # 起初只判了 `had_link`,于是 `our_url` **还是空**的时候也会被打上戳 ——
    # 后果是"没搬成"的线索被记成"搬成时刻 = 采集时刻",那条时延口径**整片是假的**
    # (而且它长得特别合理:每条的时延都≈0,像是"搬得飞快")。
    # 五个调用点里有三个传进来的链**可能是空串**(`share_url or ""` 之类),
    # 所以这条检查放在**这里**——一处兜住,不靠调用方记得。
    if not str(getattr(row, "our_url", "") or ""):
        return
    if getattr(row, "moved_at", None) is None:
        row.moved_at = now or datetime.now()


def _discover_ts_douyin(r) -> datetime | None:
    return r.publish_at or r.found_at


def _discover_ts_group(r) -> datetime | None:
    return r.msg_time or r.synced_at


def fmt_h(h: float | None, none_text: str = "还没样本") -> str:
    """小时数 → **按量级换单位**。

    ⚠️ 不能用 `f"{h:.1f}h"`:实测"入库→推出"中位 **0.043h(2.6 分钟)**,那样会打成 `0.0h`
    —— 一句"0 小时"读起来像"瞬间完成/没测到",把"2.6 分钟"这个真数抹掉了。
    时延这种量的**有效位数**随量级变,格式也得跟着变。
    """
    if h is None:
        return none_text
    if h < 1:
        return f"{h * 60:.0f} 分钟"
    if h < 48:
        return f"{h:.1f}h"
    return f"{h / 24:.1f} 天"


def _stats(rows: list[dict]) -> dict:
    """一组 `{"seen","moved","move_h","no_ts"}` → 汇总(中位/成功率/缺时间戳条数)。"""
    moved = [r for r in rows if r["moved"]]
    with_ts = [r for r in moved if r["move_h"] is not None]
    return {
        "seen": len(rows),
        "moved": len(moved),
        "move_rate": round(len(moved) / len(rows), 3) if rows else 0.0,
        # ⚠️ 保留 3 位:四舍五入到 1 位会把 0.043h(2.6 分钟)抹成 0.0,
        # 那是**精度在数据层被丢掉**,后面怎么格式化都救不回来。格式化只在渲染层做。
        "move_h": (round(statistics.median([r["move_h"] for r in with_ts]), 3)
                   if with_ts else None),
        "with_ts": len(with_ts),
        # ⚠️ 有链但没时间戳 = **当时没记**,不是"没搬成" —— 必须单独列出来,
        # 否则 `move_h` 会看起来"样本很多"而其实只覆盖了一小部分(静默失真)。
        "no_ts": len(moved) - len(with_ts),
    }


def _douyin(db: Session, uid: int, lo: datetime) -> list[dict]:
    from app.db.models import DouyinLead

    out = []
    for r in db.scalars(select(DouyinLead).where(
            DouyinLead.user_id == uid,
            func.coalesce(DouyinLead.publish_at, DouyinLead.found_at) >= lo)):
        d = _discover_ts_douyin(r)
        m = r.moved_at
        out.append({"moved": bool(str(r.our_url or "")),
                    "move_h": ((m - d).total_seconds() / 3600
                               if (m and d and m >= d) else None)})
    return out


def _xunlei_group(db: Session, uid: int, lo: datetime) -> list[dict]:
    from app.db.models import XunleiGroupShare

    out = []
    for r in db.scalars(select(XunleiGroupShare).where(
            XunleiGroupShare.user_id == uid,
            func.coalesce(XunleiGroupShare.msg_time, XunleiGroupShare.synced_at) >= lo)):
        d = _discover_ts_group(r)
        m = r.moved_at
        out.append({"moved": bool(str(r.our_url or "")),
                    "status": str(r.status or ""), "title": str(r.title or ""),
                    "message": str(r.message or ""),
                    "move_h": ((m - d).total_seconds() / 3600
                               if (m and d and m >= d) else None)})
    return out


def _reasons(rows: list[dict], top: int = 5) -> list[dict]:
    """失败原因分布。**只数终态失败**(`status == failed`)。

    ⚠️ `skipped` **不算失败** —— 那是我们**主动不搬**(盘满暂停/自己的分享/太泛的大包名),
    把它算进失败率会把"我们自己决定不搬"读成"搬不动"。
    """
    counts: dict[str, int] = {}
    for r in rows:
        if str(r.get("status") or "") != "failed":
            continue
        key = (r.get("message") or "未记原因")[:60]
        counts[key] = counts.get(key, 0) + 1
    return [{"reason": k, "n": v} for k, v in
            sorted(counts.items(), key=lambda kv: -kv[1])[:top]]


def _wechat(db: Session, uid: int, lo: datetime) -> dict:
    """公众号这条:交付的终点是**推送**(`pushed_at`),不是"搬成"。

    ⚠️⚠️ **两个坑都在这里,不处理就会得出假数**(2026-10-07 实测):
      ① **一对多**:一篇文章平均挂 1 条盘链,但最长的那篇挂了 **14 条** ——
         直接 join 会把同一篇数 14 次(实测算出"推出 308 > 采到 260"= 118%,荒谬)。
         ⇒ 一律**按文章 id 去重**。
      ② **回填的 `pushed_at` 不是真时刻**:加这一列时为了防止历史文章被补推刷屏,
         迁移把**所有存量行**的 `pushed_at` 直接写成了 `created_at`(实测 866 篇里 **369 篇**
         是两个时间相等)。拿它算"入库→推出"必然得 **0.0h**,而那个 0 长得像"极快",
         极容易被当成好消息。⇒ 这类行**只计入"推过"、不计入时延**,并单独报个数。
    另外"发布→推出"和"入库→推出"要分开说:前者是真正的端到端(含我们发现得晚),
    后者只衡量我们自己的手脚快慢。
    """
    from app.db.models import WechatArticle, WechatPanLink

    rows = db.execute(
        select(WechatArticle.id, WechatArticle.publish_at, WechatArticle.created_at,
               WechatArticle.pushed_at)
        .join(WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
        .where(WechatPanLink.user_id == uid,
               func.coalesce(WechatArticle.publish_at,
                             WechatArticle.created_at) >= lo)).all()
    arts = {r[0]: r for r in rows}            # ← 按文章去重(见 ①)
    pub_h: list[float] = []
    in_h: list[float] = []
    pushed = backfilled = 0
    for _aid, pub, created, pu in arts.values():
        if pu is None:
            continue
        pushed += 1
        if created and pu == created:         # ← 回填行(见 ②):只算"推过",不算时延
            backfilled += 1
            continue
        base = pub or created
        if base and pu >= base:
            pub_h.append((pu - base).total_seconds() / 3600)
        if created and pu >= created:
            in_h.append((pu - created).total_seconds() / 3600)
    seen = len(arts)
    return {"seen": seen, "pushed": pushed, "backfilled": backfilled,
            "push_rate": round(pushed / seen, 3) if seen else 0.0,
            "publish_to_push_h": round(statistics.median(pub_h), 3) if pub_h else None,
            "ingest_to_push_h": round(statistics.median(in_h), 3) if in_h else None,
            "latency_n": len(in_h)}


def delivery_report(session: Session, user_id: int, days: int = 14) -> dict:
    """交付看板:**当前 24h** + 窗口聚合 + 逐段时延 + 瓶颈判定。"""
    now = datetime.now()
    lo, lo24 = now - timedelta(days=days), now - timedelta(hours=24)

    dy = _douyin(session, user_id, lo)
    dy24 = _douyin(session, user_id, lo24)
    xg = _xunlei_group(session, user_id, lo)
    xg24 = _xunlei_group(session, user_id, lo24)

    cur_xg = _stats(xg24)
    rep: dict[str, Any] = {
        "days": days,
        "chains": {
            "抖音": {**_stats(dy), "note": "失败原因记在作业记录里(douyin_leads/quark_kouling)"},
            "迅雷群": {**_stats(xg), "fail_reasons": _reasons(xg),
                     "skipped": sum(1 for r in xg if r.get("status") == "skipped")},
            "公众号": _wechat(session, user_id, lo),
        },
        # ⚠️ **反对"窗口聚合"的那一半** —— 见模块头的 10-03~05 那段。
        "current_24h": {
            "迅雷群": {**cur_xg, "fail_reasons": _reasons(xg24)},
            "抖音": _stats(dy24),
        },
    }

    # 瓶颈判定:只有**当前**还在坏才算瓶颈(窗口里的历史不算)。
    # ⚠️ **只看当前 24h** —— 窗口里有历史失败不算瓶颈(见模块头那段)。
    bad = [r for r in xg24 if str(r.get("status") or "") == "failed"]
    if bad and len(xg24) >= 5:
        top = _reasons(xg24, 1)
        why = f"(主因「{top[0]['reason']}」)" if top else ""
        rep["bottleneck"] = f"迅雷群**当前仍在失败**:近 24h {len(bad)}/{len(xg24)} 条{why}"
    elif any(bool(v.get("moved")) and v.get("move_h") is None
             for v in rep["chains"].values() if isinstance(v, dict)):
        rep["bottleneck"] = ("搬成时延**样本还不够**(`moved_at` 是 2026-10-07 才开始记的)"
                             " —— 先别下结论,等它攒几天")
    else:
        rep["bottleneck"] = "近 24h 没有在坏的段落(窗口里的历史失败已修好)"
    return rep


def render_lines(rep: dict) -> list[str]:
    """看板 → 可读几行。**必须同时打"当前 24h"**,否则会被旧窗口骗。"""
    c = rep["chains"]
    lines = [f"=== 交付看板(近 {rep['days']} 天)==="]
    for name, v in c.items():
        if name == "公众号":
            if not v["seen"]:
                continue
            bf = (f";另有 {v['backfilled']} 篇的 `pushed_at` 是迁移回填的,"
                  f"只算「推过」不算时延" if v.get("backfilled") else "")
            lines.append(
                f"  {name}: 采到 {v['seen']} 篇 / 推出 {v['pushed']}"
                f"({v['push_rate'] * 100:.0f}%);发布→推出中位 "
                f"**{fmt_h(v['publish_to_push_h'])}**、入库→推出 "
                f"{fmt_h(v['ingest_to_push_h'])};时延样本 {v['latency_n']} 篇{bf}")
            continue
        rate = f"{v['move_rate'] * 100:.0f}%"
        mv = f"**{fmt_h(v['move_h'])}**" if v["move_h"] is not None else "**还没样本**"
        lines.append(f"  {name}: 看到 {v['seen']} / 搬成 {v['moved']}({rate});"
                     f"看到→搬成中位 {mv}(有戳 {v['with_ts']}、无戳 {v['no_ts']})")
        for fr in (v.get("fail_reasons") or [])[:3]:
            lines.append(f"      ✗ {fr['n']}× {fr['reason']}")
    cur = rep["current_24h"]
    x24 = cur["迅雷群"]
    lines.append(f"  **当前 24h**(用来区分「现在」与「窗口里曾经」):"
                 f"迅雷群 看到 {x24['seen']} / 搬成 {x24['moved']};"
                 f"抖音 看到 {cur['抖音']['seen']} / 搬成 {cur['抖音']['moved']}")
    for fr in (x24.get("fail_reasons") or [])[:3]:
        lines.append(f"      ✗ {fr['n']}× {fr['reason']}")
    lines.append(f"  **瓶颈**:{rep['bottleneck']}")
    return lines


#: 每条链的**探测周期**取自哪一项配置(单一事实源:不要在这里抄一份时间)。
#: ⚠️ 用配置**字符串**而不是数字:改了 cron 之后这里自动跟着变,不会飘。
_CHAIN_CRON = {
    "抖音": "douyin_leads_cron",        # 线索抓取(口令解析也在这一步)
    "迅雷群": "xunlei_group_cron",       # 群消息流采集 + 限量转存
    "迅雷盘": "xunlei_sync_cron",        # 扫盘
    "公开平台": "presence_cron",          # 小红书/快手/贴吧/微博 按资源名搜
}


def cron_interval_h(expr: str) -> float | None:
    """从 cron 表达式估**一轮要多久**(小时)。估不出来返回 `None`(**别猜**)。

    支持调度器里实际出现过的几种形状;其余一律 `None` —— 宁可"不知道周期"
    也不要编一个数出来,那会让后面的判据看起来有依据而其实是错的。
    """
    p = str(expr or "").split()
    if len(p) != 5:
        return None
    mi, hh, _dom, _mon, dow = p
    try:
        if hh.startswith("*/") and mi.isdigit():
            return float(hh[2:])
        if hh == "*" and mi.startswith("*/"):
            return float(mi[2:]) / 60.0                    # 分钟内步进(如 */30)
        if hh.isdigit() and dow == "*":
            return 24.0
        if hh.isdigit() and dow != "*":
            return 168.0                                   # 每周一次
        if all(x.isdigit() for x in hh.split(",")):
            return 24.0 / len(hh.split(","))               # 每天 N 个定点
        if hh == "*" and all(x.isdigit() for x in mi.split(",")) and len(mi.split(",")) > 1:
            return 1.0 / len(mi.split(",")) * 1.0          # 每小时的 N 个定点
        if hh == "*" and mi.isdigit():
            return 1.0
    except (ValueError, ZeroDivisionError):
        return None
    return None


def _intervals_h(settings) -> dict[str, float]:
    """各链当前的探测周期(小时)。取不到的链**就不出现在结果里**(不编数)。"""
    from app.services.schedule_service import WECHAT_LISTEN_HOURS

    out: dict[str, float] = {}
    for chain, attr in _CHAIN_CRON.items():
        v = cron_interval_h(str(getattr(settings, attr, "") or ""))
        if v:
            out[chain] = v
    n = len(WECHAT_LISTEN_HOURS or [])
    if n:
        out["公众号"] = 24.0 / n
    return out


def recommend_cadence(ordering: dict, settings=None) -> list[str]:
    """由「**提前量**」反推「**还要不要提速**」——**带样本门槛,宁可不给建议**。

    ## 判据(为什么不是"越快越好")
    探测周期只决定"**我们什么时候看到它**";提前量来自"**它在别的链上还没出现**"。
    压缩周期能多拿到的提前量 **最多 = 当前周期**。所以:
      · 某链的中位提前量 **远大于** 它的周期(抖音:提前几十小时,周期 4 小时)
        ⇒ 优势来自**平台本身** —— 4→2 小时只多抢 2 小时,而那几十小时的领先
          早在上一轮就拿到了 ⇒ **不建议动**,压周期是纯浪费。
      · 提前量 **接近或小于** 周期 ⇒ 它多半是靠"跑得勤"才赢的,这时压周期才真有用。
    ⇒ 结论常常是"**谁的周期都不必动**" —— 那也是个有用的结论(省下一次无谓的加密,
      也就是省下风控暴露与机器时间)。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    groups = (ordering or {}).get("groups") or []
    if len(groups) < MIN_SAMPLE:
        return [f"⚠️ 跨链样本只有 {len(groups)} 份(门槛 {MIN_SAMPLE}),"
                f"**现在给建议就是假信号** —— 等台账攒够再看。"]
    pairs = (ordering or {}).get("pairs") or {}
    leads: dict[str, list[float]] = {}
    for k, v in pairs.items():
        a = str(k).split("→")[0]
        leads.setdefault(a, []).append(float(v.get("median_h") or 0))
    if not leads:
        return ["样本里没有可比的先后关系,先不下建议。"]
    ivs = _intervals_h(settings)
    out: list[str] = []
    for chain, vals in sorted(leads.items(),
                              key=lambda kv: -statistics.median(kv[1])):
        lead = statistics.median(vals)
        iv = ivs.get(chain)
        if iv is None:
            out.append(f"· {chain}: 中位提前 **{lead:.0f}h**(周期未知,不下判断)")
        elif lead > iv * 3:
            out.append(f"· {chain}: 提前 {lead:.0f}h ≫ 周期 {iv:.1f}h ⇒ 优势来自**平台本身**,"
                       f"压周期最多再抢 {iv:.1f}h,**不必动**")
        elif lead > iv:
            out.append(f"· {chain}: 提前 {lead:.0f}h 略大于周期 {iv:.1f}h ⇒ 提速收益有限")
        else:
            out.append(f"· {chain}: 提前 {lead:.0f}h ≤ 周期 {iv:.1f}h ⇒ "
                       f"**靠跑得勤赢的,压周期真有用**")
    out.append("(仅建议,**不自动改配置** —— 改 cron 要重启才生效,由人决定)")
    return out
