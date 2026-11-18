# -*- coding: utf-8 -*-
"""**跨板块重复**内部通报(2026-11-18,档2) —— 「同一份资源进了几个群」推管理群。

## 它补的是哪个盲区

`board_digest` 是**单板块**视角:每个群收到的是"我这个板块昨天有什么"。
于是「**同一份盘链在 8 个群里各推一遍**」在内部**完全看不见** —— 每个板块的日结
看起来都正常,而客户那边的体感是"到处都是同一条"。

实测(2026-11-18,近 7 天台账):
  · 带盘行 **1907** / 不同盘链 **382**;
  · 跨 **≥2 个板块**的盘链 **82 个**,涉及 **1063 行**(占带盘行的 **55.7%**);
  · 最极端的进了 **8 个板块**(`《You'…》` / 35 行,知乎 16 · 快手 5 · 小红书 5 ·
    贴吧 3 · B站 2 · 抖音 2 · 微博同行 1 · 公众号 1)。
  · ⚠️ 而 `item_key`(内容身份)跨板块重复 **0 条** —— 各平台抄的是同一份**资源**,
    标题各不相同 ⇒ 只有**盘链身份**能聚合,内容级永远看不到这件事。

## 为什么只通报,不自动合并

不同板块的群**不是一批人**(`drop_recent` 只按 section 去重的原始理由)。
跨群合并会把别的群饿死。能真去重的只有**同群的那几个板块**
(`push_ledger.group_of`:tieba/zhihu 共用同一个专属群 ⇒ 那对已改成
`drop_recent(by_group=True)` 真去重)。其余跨群重复**只能让运营看见**:
如果 8 个群确实都在要同一批资源,那是**选词/选源**的问题,不是推送层能替客户决定的。

## 去向与纪律

· **管理群**(`webhook_for(settings, "admin")`),不进客户群 —— 这是内部运营数字。
· 与 `chain_report_tick` 同一条:全绿也推(没有跨板块重复也是要知道的信息,
  而且只有每天都来,人才会注意到"今天没来")。
· 失败只留日志 + 运行记录,不抛(它是旁路,主业是推送)。

⚠️ **窗口固定 7 天**:跨板块重复是**沉淀现象**(同一份资源要经过"一个平台先火、
其他平台跟进"才会跨板块),1 天的窗口只能抓到零星一两个,看不出规模。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)


def build_report(session: Session, user_id: int, days: int = 7,
                 top: int = 10, min_sections: int = 2) -> str:
    """汇总成一段给**管理群**的文本(纯文本,飞书 `send` 那条路)。"""
    from app.services import push_ledger

    until = datetime.now()
    since = until - timedelta(days=int(days))
    s = push_ledger.cross_section_summary(session, user_id, since, until,
                                          min_sections=min_sections, top=top)
    nl = chr(10)
    lines = [f"🧩 跨板块重复(近 {days} 天)", ""]
    if not s["pan_rows"]:
        #: ⚠️ 必须说清"没有带盘的行"与"有带盘行但没跨板块"是**两件事** ——
        #: 前者可能是台账/盘链抽取坏了,后者是正常的好状态。
        lines.append(f"台账 {s['rows']} 行里**一条盘链都没有** —— "
                     "要么这周确实没推过资源,要么盘链抽取/台账写入断了,人工核一眼。")
        return nl.join(lines)
    ratio = (s["cross_rows"] * 100.0 / s["pan_rows"]) if s["pan_rows"] else 0.0
    lines.append(f"台账 **{s['rows']}** 行 · 带盘 **{s['pan_rows']}** 行 · "
                 f"不同盘链 **{s['pans']}** 个")
    lines.append(f"跨 ≥{int(min_sections)} 个板块的盘链 **{s['cross_total']}** 个"
                 f" · 涉及 **{s['cross_rows']}** 行(占带盘行 **{ratio:.1f}%**)")
    if not s["cross"]:
        lines.append("✅ 没有跨板块重复 —— 各板块的资源池是分开的。")
    else:
        from app.services.board_digest import board_label

        lines.append(nl + "【重复最多的 TOP】(资源 → 进了几个群 / 共几行)")
        for g in s["cross"]:
            secs = sorted(g["sections"].items(), key=lambda kv: -kv[1])
            spread = " · ".join(f"{board_label(sec) or sec} {n}" for sec, n in secs)
            lines.append(f"  · {str(g['title'] or g['pan_key'])[:34]}"
                         f" → **{len(g['sections'])} 个板块** / {g['total']} 行")
            lines.append(f"      {spread[:180]}")
    #: 各板块的"行数 vs 不同盘链数" —— 行数远大于盘链数就是**板块内**在重推,
    #: 那个由 `drop_recent` 管;两条数摆在一起才分得清"板块内刷"与"跨板块重复"。
    if s["by_section"]:
        from app.services.board_digest import board_label

        lines.append(nl + "【各板块】行数 / 不同盘链")
        for sec in sorted(s["by_section"], key=lambda k: -s["by_section"][k]["rows"]):
            e = s["by_section"][sec]
            lines.append(f"  · {board_label(sec) or sec}:{e['rows']} 行 / {e['pans']} 盘链")
    lines.append(nl + "(口径:只认**盘链身份**;同群跨板块的那几个已由 `drop_recent` "
                      "的 `by_group` 真去重,这里是**跨群**的规模,不自动合并。)")
    return nl.join(lines)


def cross_section_report_tick(settings=None) -> int:
    """定时入口:给每个启用用户生成一份,**推管理群**。返回发送成功数。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User
    from app.services.feishu_client import FeishuClient, webhook_for

    settings = settings or get_settings()
    hook = webhook_for(settings, "admin")
    if not hook:
        return 0
    session = get_session_local()()
    sent = 0
    try:
        for (uid,) in session.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                text = build_report(session, uid)
                if FeishuClient(hook, getattr(settings, "feishu_secret", "")).send(
                        text[:3000]):
                    sent += 1
            except Exception:  # noqa: BLE001 - 单用户失败不挡其余
                logger.exception("跨板块重复通报生成失败 user=%s", uid)
    finally:
        session.close()
    return sent
