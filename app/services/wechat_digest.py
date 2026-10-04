"""公众号板块:**按阅读数总结** + **闭环体检**(2026-10-04)。

用户口径:
- 公众号要「**自己搜索 → 自己添加进书架 → 根据阅读数总结**」**全自动化**
- 阅读数转化率 **30%**
- **具体数字推管理群**;客户群只看"火爆程度"

为什么要"体检"这一段:这条闭环的**四段其实早就都存在**(发现/收录/监控/总结),
但它们**各自静默**——收录连续几天 0、监听拿不到阅读数,都没人知道。
2026-10-04 就踩过:WeRSS 的 `search_mp` 明明是好的,而 `listenable=0` 连着三天,
分不清是"候选名对不上(正常)"还是"链路断了(故障)"。所以**每段的产出必须被摆出来**。

⚠️ 本模块**只读**不写业务数据;推送走**管理群**(具体数字不进客户群)。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RunRecord, WechatArticle
from app.utils import get_logger

logger = get_logger(__name__)

# 闭环四段 → 对应的运行记录 kind。**顺序即链路顺序**。
#   发现 = 搜狗搜候选号 / 收录 = 号名→MP_WXS_ 加进监听 / 监控 = 监听取文与阅读数
PIPELINE_STAGES: tuple[tuple[str, str, str], ...] = (
    ("① 发现候选号", "wechat_candidates", "new"),
    ("② 收录成对标号", "wechat_candidate_import", "listenable"),
    ("③ 监听采文章", "wechat_listen", "new"),
)

_DETAIL_RE_CACHE: dict[str, re.Pattern] = {}


def _detail_int(detail: str, key: str) -> int | None:
    """从运行记录的 detail 里取 `key=数字`(形状:`... new=17 failed=0 ...`)。

    ⚠️ 取不到返回 `None` **不是 0** —— "没这个字段"与"产出确实是 0"是两回事,
    混在一起会把"格式变了"误读成"没产出"。
    """
    pat = _DETAIL_RE_CACHE.get(key)
    if pat is None:
        pat = _DETAIL_RE_CACHE[key] = re.compile(rf"(?:^|\s){re.escape(key)}=(-?\d+)")
    m = pat.search(detail or "")
    return int(m.group(1)) if m else None


def pipeline_health(session: Session, user_id: int, runs: int = 3) -> list[dict]:
    """闭环体检:每段取**最近 `runs` 次**运行,报最近时间、产出、以及"是不是连续 0"。

    `stale=True` 的判据(三条都要满足,少一条都不报):
      ① 该段**至少跑过一次**(从没跑过是另一类问题,不该混进来)
      ② 最近 `runs` 次**全都在**,且产出指标**全都解析出来了**
      ③ 这些产出**全是 0**

    ⚠️ ②里的"全都能解析出来"是关键:**只要有一次解析不出字段,就不下"连续 0"的结论**
    —— 那更可能是 detail 格式变了,不是真没产出(与 `_should_have_fired` 的保守分支同源)。
    """
    out: list[dict] = []
    for label, kind, metric in PIPELINE_STAGES:
        rows = session.scalars(
            select(RunRecord).where(RunRecord.user_id == user_id, RunRecord.kind == kind)
            .order_by(RunRecord.id.desc()).limit(runs)).all()
        if not rows:
            out.append({"stage": label, "kind": kind, "metric": metric, "last_at": None,
                        "last_status": "", "values": [], "stale": False,
                        "note": "从没跑过——检查调度是否注册"})
            continue
        values = [_detail_int(r.detail or "", metric) for r in rows]
        parsed = [v for v in values if v is not None]
        all_zero = (len(parsed) == len(rows) and bool(parsed) and all(v == 0 for v in parsed))
        note = ""
        if all_zero:
            note = f"连续 {len(rows)} 次产出为 0 —— 要么是正常(候选名对不上/本期确实没新文),要么链路断了,人工看一眼"
        elif not parsed:
            note = f"detail 里解析不出 `{metric}=` —— 别当成 0,先核对格式"
        elif len(parsed) < len(rows):
            note = "最近几次有解析不出的记录,暂不下「连续 0」的结论"
        out.append({"stage": label, "kind": kind, "metric": metric,
                    "last_at": rows[0].started_at, "last_status": rows[0].status,
                    "values": values, "stale": all_zero, "note": note})
    return out


def read_summary(session: Session, user_id: int, days: int = 7) -> dict:
    """**按阅读数总结**近 `days` 天:总量、**有阅读数的覆盖率**、每号排行、预估拉新量。

    ⚠️ **覆盖率必须报出来**:阅读数受列表额度限制(**每轮只问 25 个号、~2 天轮一圈**),
    所以"某些文章 read_num=0"是**设计取舍不是故障**。不报覆盖率,读的人会把
    0 当成"这篇没人看"——正是本仓反复出现的"静默失败=假成功"。
    """
    from app.services import conversion

    since = datetime.now() - timedelta(days=days)
    arts = session.scalars(select(WechatArticle).where(
        WechatArticle.user_id == user_id,
        WechatArticle.created_at >= since)).all()
    with_read = [a for a in arts if int(a.read_num or 0) > 0]

    by_author: dict[str, dict] = {}
    for a in with_read:
        e = by_author.setdefault(str(a.author or "未知"), {"n": 0, "reads": 0, "max": 0})
        e["n"] += 1
        e["reads"] += int(a.read_num or 0)
        e["max"] = max(e["max"], int(a.read_num or 0))
    ranked = sorted(by_author.items(), key=lambda kv: -kv[1]["reads"])

    total_reads = sum(int(a.read_num or 0) for a in with_read)
    # 预估拉新量走**单一事实源**(conversion),别在这儿再写一遍 0.3
    est = conversion.estimate("wechat", {"read_num": total_reads})
    return {"days": days, "articles": len(arts), "with_read": len(with_read),
            "total_reads": total_reads, "by_author": ranked[:8],
            "estimate": est["estimate"] if est["estimate"] is not None else None,
            "estimate_detail": conversion.describe(est) if arts else ""}


def build_digest(session: Session, user_id: int, days: int = 7) -> str:
    """汇总成一段给**管理群**的文本(具体数字只在这里出现)。"""
    nl = chr(10)
    s = read_summary(session, user_id, days)
    lines = [f"📈 公众号板块总结(近 {days} 天)", ""]
    cov = f"{s['with_read']}/{s['articles']}" if s["articles"] else "0/0"
    lines.append(f"【采集】文章 {s['articles']} 篇 · **有阅读数的 {cov}**"
                 f" · 阅读合计 {s['total_reads']}")
    if s["estimate"] is not None:
        lines.append(f"【预估拉新】{s['estimate_detail']}")
        lines.append("   ⚠️ 预估=需求侧估算(乘的是**对标号**的阅读数),不是我方实收")
    if s["articles"] and not s["with_read"]:
        lines.append("   ⚠️ **一篇都没读到阅读数** —— 检查列表额度轮转是否在跑"
                     "(窗口每轮 25 个号,~2 天一轮)")
    if s["by_author"]:
        lines.append(nl + "【号排行】(按阅读合计)")
        for name, e in s["by_author"][:5]:
            lines.append(f"  · {name[:16]} {e['n']} 篇 / 阅读 {e['reads']} / 最高 {e['max']}")

    lines.append(nl + "【闭环体检】")
    for h in pipeline_health(session, user_id):
        when = h["last_at"].strftime("%m-%d %H:%M") if h["last_at"] else "—"
        mark = "⚠️" if h["stale"] else ("·" if h["last_at"] else "❓")
        lines.append(f"  {mark} {h['stage']}:最近 {when} {h['last_status']} "
                     f"{h['metric']}={h['values']}")
        if h["note"]:
            lines.append(f"      {h['note']}")
    lines.append(nl + "(口径:阅读数来自微信读书列表接口,有会话额度;预估拉新=阅读数×30%)")
    return nl.join(lines)


def run_wechat_digest_all_users(settings=None, days: int = 7) -> int:
    """入口:给每个启用用户生成公众号总结,**推管理群**(返回成功条数)。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User
    from app.services.feishu_client import FeishuClient, webhook_for

    st = settings or get_settings()
    # ⚠️ **管理群**(不是客户内容群):这里会出现具体数字,与 `notify_incident` 同一去向
    hook = webhook_for(st, "admin")
    if not hook:
        return 0
    db = get_session_local()()
    sent = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                text = build_digest(db, uid, days=days)
                if FeishuClient(hook, st.feishu_secret).send(text[:3000]):
                    sent += 1
            except Exception:  # noqa: BLE001 - 单个用户失败不挡其余
                logger.exception("公众号总结生成失败 user=%s", uid)
    finally:
        db.close()
    return sent
