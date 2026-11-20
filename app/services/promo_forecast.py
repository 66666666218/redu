# -*- coding: utf-8 -*-
"""**爆款预警**:在资源刚起量的 24 小时里,判断它「会不会爆」并提示自建链(2026-11-20)。

## 它回答的问题

用户口径:「**能否总结然后找寻规律自己主动发现爆款然后改写成自建链**」。

`push_surge` 回答的是「**刚刚**有更多人加入了」(3h 窗口、比上一窗口的增量);
这一条回答的是「**这个新出现的资源,按它头 24 小时的形状,大概率会爆**」——
判据是**绝对人数**,不是增量。两者用的是同一份台账、同一套两层身份。

## 判据与它的出处(不是拍的)

`scripts/backtest_promo_forecast.py` 用 30 天真实台账回放量出来的
(可评估 1113 桶 · 爆款≥10 人 85 个 · **基线精确率 7.6%**):

| 头 24h 的不同作者数 | 候选 | 之后真爆款 | 精确率 | 提升 |
|---|---|---|---|---|
| ≥4 人 | 128 | 53 | **41.4%** | 5.4x |
| ≥6 人 | 52 | 34 | 65.4% | 8.6x |
| ≥8 人 | 23 | 19 | 82.6% | 10.8x |

⇒ 默认 `min_hands=4`。**改这个默认值必须重跑回测**,否则卡片上的判据就没有依据了。

## 为什么是「头 24h 窗口完整之后」才报

早窗口必须是**完整的 24 小时**:一个刚出现 2 小时、已有 3 个人的桶,再等 22 小时
可能变成 8 个人。所以 `first + early_hours <= now` 是硬条件 —— 宁可晚 24 小时报,
也不要在窗口没走完时就下结论(那会把"还没到"当成"不会到")。

⚠️ 已知代价:卡片到达时,资源已经起量 24 小时。回测里 >40% 的桶在 7 天后仍在长,
所以这 24 小时的延迟不影响"值不值得搬";但**别把它当成"第一小时就报警"**。

## 只报**能动手**的

一张卡要能直接变成动作,必须同时满足:

    早窗口 ≥ min_hands 人  ∧  **当时还没有**我们的自有链  ∧  当时就有可转存的源链
    ∧ 作品名不是纯推广套话(`is_cta`)

自有链 = 夸克 `pan_owned_shares` + 迅雷 `xunlei_group_shares.our_url`(status=ok),
**两者都要认** —— 只认夸克会把已搬过的迅雷链作品误报成「可搬」(实测差 5 个桶)。

⚠️ `owned_keys` 返回的是 `{身份: **最早拿到它的时刻**}`,不是 set:只用一个 set 的话,
「早窗口**之后**才搬的链」会被算成「当时已有」⇒ 可动手量被系统性**低估**。
这个坑在回测脚本第一版上真实发生过(`scripts/backtest_promo_forecast.py` 头注)。

被这几道闸挡下的桶**不静默丢**:每个数字都进运行记录(`已有链/无源链/套话/达标`),
否则「今天没预警」与「作业看不见数据」在库里长得一样。

## 去向与冷却

**管理群**(`webhook_for(settings, "admin")`)—— 运营情报,不进客户群。
每条(盘链+作品)**发送成功后**才落冷却行(理由与 `push_surge._recently_alerted` 同:
pysqlite 上先占位再发,一次抖动就吃掉整条提醒)。冷却默认 **168h** ——
一个桶的「头 24h」只有一个,重复报没有信息量。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 可转存的源链形状。与 `scripts/pan_promo_rank.py` 同一份(那里改为 import 本模块)。
SRC_PATTERNS = (("夸克", "pan.quark.cn/s/"), ("迅雷", "pan.xunlei.com/s/"),
                ("百度", "pan.baidu.com/s/"))

#: 推广动作词。
#: ⚠️ **不能「命中一个就丢」**:`七宗罪测试`(30 人)、`犬系人格测试`(16 人)、
#: `奶娃冬季礼物获取`(8 人) 都含动作词,却**是**真资源(人格测试/资料本身就是商品)。
#: 所以只丢**剥掉动作词后什么都不剩**的名字(纯套话)。
CTA_WORDS = ("自取", "直达", "入口", "链接", "获取", "分享", "资料", "最新",
             "完整版", "合集", "整理", "推荐", "更新", "拿走", "收藏", "关注",
             "免费", "打开", "直接", "教程", "来啦", "来了", "来咯", "可")


def src_type(url: str) -> str:
    """盘链 → `夸克`/`迅雷`/`百度`;认不出返回空串(**不能当源链用**)。"""
    u = str(url or "")
    for tag, pat in SRC_PATTERNS:
        if pat in u:
            return tag
    return ""


def is_cta(name: str) -> bool:
    """剥掉动作词/标点/数字后什么都不剩 ⇒ 它是套话,不是作品名。"""
    rest = str(name or "")
    for w in CTA_WORDS:
        rest = rest.replace(w, "")
    return not re.sub(r"[\W\d_]+", "", rest)


def owned_keys(session: Session) -> dict[str, datetime]:
    """自有链身份 → **最早拿到它的时刻**。

    ⚠️ 必须带时刻(理由见模块头注的「可动手量被低估」)。
    ⚠️ 必须走 ORM 而不是 `text()`:raw 查询在 sqlite 上回的是**字符串**,
    直接和 `datetime` 比会抛 `TypeError`(`backtest_promo_forecast` 第一版就栽在这)。
    """
    from app.db.models import PanOwnedShare, XunleiGroupShare
    from app.services.push_ledger import pan_key

    out: dict[str, datetime] = {}
    for u, ts in session.execute(
            select(PanOwnedShare.url, PanOwnedShare.created_at)
            .where(PanOwnedShare.platform == "quark")).all():
        k = pan_key(str(u or ""))
        if k and ts is not None and (k not in out or ts < out[k]):
            out[k] = ts
    for u, ts in session.execute(
            select(XunleiGroupShare.our_url, XunleiGroupShare.moved_at)
            .where(XunleiGroupShare.status == "ok",
                   XunleiGroupShare.our_url.is_not(None),
                   XunleiGroupShare.our_url != "")).all():
        k = pan_key(str(u or ""))
        if k and ts is not None and (k not in out or ts < out[k]):
            out[k] = ts
    return out


def detect_forecasts(session: Session, user_id: int, *, early_hours: float = 24,
                     min_hands: int = 4, max_age_hours: float = 72,
                     history_days: float = 30, top: int = 5,
                     now: datetime | None = None,
                     stats: dict | None = None) -> list[dict]:
    """找出**头 `early_hours` 小时已攒够推手、且我们还没有链**的新资源(纯读)。

    返回 `[{pan_key, pan_url, pan_type, work_key, title, hands, sections,
             first, last, src_urls}]`,按推手数降序。

    ⚠️ `history_days` 决定「首次出现」与「链内作品名语料」的观察范围。**不能**只用
    `early_hours + max_age_hours` 那一小段:一个 5 天前就出现、今天还在推的老资源,
    在短窗口里会被当成"新出现的",然后拿一个假的头 24h 去判它。
    """
    from app.db.models import PushLedger
    from app.services.push_surge import _bracket_names, _identity, _work_key

    now = now or datetime.now()
    hist_start = now - timedelta(days=float(history_days))
    rows = session.execute(
        select(PushLedger.pan_url, PushLedger.pan_id, PushLedger.section,
               PushLedger.author, PushLedger.title, PushLedger.pan_type,
               PushLedger.pushed_at)
        .where(PushLedger.user_id == user_id,
               PushLedger.pushed_at >= hist_start,
               or_(PushLedger.pan_url != "", PushLedger.pan_id != ""))
        .order_by(PushLedger.pushed_at.asc())
    ).all()

    #: 链内作品名语料:与 `push_surge` 同一做法(否则同一条链上的不同作品会被并桶)
    names: dict[str, list[str]] = {}
    for r in rows:
        pk = _identity(r)
        if not pk:
            continue
        bucket = names.setdefault(pk, [])
        for n in _bracket_names(str(r.title or "")):
            if n not in bucket:
                bucket.append(n)

    owned = owned_keys(session)
    early = timedelta(hours=float(early_hours))
    newest, oldest = now - early, now - timedelta(hours=float(max_age_hours))

    groups: dict[tuple[str, str], dict] = {}
    n_no_author = 0
    for r in rows:
        pk = _identity(r)
        who = str(r.author or "").strip()
        if not pk:
            continue
        if not who:
            if r.pushed_at >= newest:
                n_no_author += 1
            continue
        k = (pk, _work_key(str(r.title or ""), names[pk]))
        g = groups.get(k)
        if g is None:
            groups[k] = g = {"pan_key": pk, "pan_url": str(r.pan_url or ""),
                             "pan_type": str(r.pan_type or ""), "title": "",
                             "first": r.pushed_at, "last": r.pushed_at,
                             "hands": set(), "sections": {}, "src_urls": {}}
        g["last"] = r.pushed_at
        if not (g["first"] + early >= r.pushed_at):
            #: 早窗口之外的行:既不数人也不收源链。按回测口径,「窗口之后才出现的
            #: 源链」在决策那一刻并不存在 ⇒ 收进来会让人以为当时就有得搬。
            continue
        g["hands"].add(who)
        g["sections"].setdefault(str(r.section or ""), set()).add(who)
        if not g["title"]:
            g["title"] = str(r.title or "")
        #: ⚠️ **按时刻判**「当时有没有自有链」,不能写成 `pk in owned`:
        #: 早窗口**之后**才搬的链在库里已存在,按存在判会把这条真能搬的漏掉
        #: (与 `backtest_promo_forecast._bucketize` 的 `elif` 同一口径)。
        t = src_type(r.pan_url)
        if t:
            own_ts = owned.get(pk)
            if own_ts is None or own_ts > g["first"] + early:
                g["src_urls"].setdefault(t, str(r.pan_url or ""))

    n_work = n_hit = n_own = n_src = n_cta = 0
    out: list[dict] = []
    for (pk, wk), g in groups.items():
        if not (oldest <= g["first"] <= newest):
            continue                       # 窗口没走完,或太老(已被后面的批次看过)
        n_work += 1
        if len(g["hands"]) < int(min_hands):
            continue
        n_hit += 1
        #: ⚠️ 按**时刻**判自有链:早窗口结束前拿到才算「当时已有」
        own_ts = owned.get(pk)
        if own_ts is not None and own_ts <= g["first"] + early:
            n_own += 1
            continue
        if not g["src_urls"]:
            n_src += 1
            continue
        if is_cta(wk):
            n_cta += 1
            continue
        out.append({**g, "work_key": wk, "hands": len(g["hands"]),
                    "sections": {s: len(v) for s, v in g["sections"].items()}})
    out.sort(key=lambda x: (-x["hands"], x["first"]))
    if stats is not None:
        stats.update({"rows": len(rows), "works": len(groups), "eval": n_work,
                      "hit": n_hit, "owned": n_own, "no_src": n_src,
                      "cta": n_cta, "no_author": n_no_author, "cand": len(out)})
    return out[:int(top)] if top else out


def _render(a: dict, nl: str) -> str:
    """一条预警 → 飞书纯文本。**前两行必须能让人决定要不要动手**。"""
    from app.services.board_digest import board_label

    secs = sorted(a["sections"].items(), key=lambda kv: -kv[1])
    spread = " · ".join(f"{board_label(s) or s} {n}" for s, n in secs[:6])
    lines = [f"🔭 爆款预警:{a['work_key'][:40]}",
             f"👥 头 24 小时 **{a['hands']} 人**在推 · 涉及 {len(secs)} 个板块"
             f" · 我们**还没有**自有链"]
    if spread:
        lines.append(f"🗂 {spread[:180]}")
    for t, u in sorted(a["src_urls"].items()):
        lines.append(f"📦 {t} {u}")
    lines.append(f"🕐 首次出现 {a['first'].strftime('%m-%d %H:%M')}")
    return nl.join(lines)


def _recently_alerted(session, user_id: int, key: str, cooldown_hours: float) -> bool:
    """**纯读**预检:这条(盘链+作品)在冷却期内发过没有(语义同 `push_surge`)。"""
    from app.db.models import FeishuAlert

    row = session.scalar(select(FeishuAlert).where(
        FeishuAlert.section == "promo_forecast",
        FeishuAlert.user_id == user_id,
        FeishuAlert.title == key[:200]))
    if row is None:
        return False
    return (datetime.now() - row.alerted_at) < timedelta(hours=float(cooldown_hours))


def _mark_alerted(session, user_id: int, key: str, cooldown_hours: float,
                  reason: str) -> None:
    """**发送成功之后**才落冷却行。写失败只影响去重,不抛。"""
    from app.services.alert_service import feishu_alert_gate

    try:
        feishu_alert_gate(session, user_id, "promo_forecast", key, cooldown_hours, reason)
        session.commit()
    except Exception:  # noqa: BLE001 - 冷却没落盘只影响去重
        logger.warning("爆款预警冷却门写入失败 %s", key, exc_info=True)
        session.rollback()


def promo_forecast_tick(settings=None) -> int:
    """定时入口(每小时):头 24h 攒够推手且我们没有链的新资源 → **管理群**。"""
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.db import get_session_local
    from app.db.models import User
    from app.services.feishu_client import FeishuClient, webhook_for

    hook = webhook_for(settings, "admin")
    if not hook:
        return 0
    kw = {"early_hours": float(getattr(settings, "promo_forecast_early_hours", 24) or 24),
          "min_hands": int(getattr(settings, "promo_forecast_min_hands", 4) or 4),
          "max_age_hours": float(getattr(settings, "promo_forecast_max_age_hours", 72) or 72),
          "top": int(getattr(settings, "promo_forecast_top", 5) or 5)}
    cooldown = float(getattr(settings, "promo_forecast_cooldown_hours", 168) or 168)
    client = FeishuClient(hook, getattr(settings, "feishu_secret", ""))
    session = get_session_local()()
    nl = chr(10)
    sent = 0
    try:
        for (uid,) in session.execute(select(User.id).where(User.enabled.is_(True))).all():
            stats: dict = {}
            try:
                alerts = detect_forecasts(session, uid, stats=stats, **kw)
            except Exception:  # noqa: BLE001 - 单用户失败不挡其余
                session.rollback()
                logger.exception("爆款预警检测失败 user=%s", uid)
                continue
            n_sent = 0
            for a in alerts:
                #: ⚠️ 冷却键分段截断:**别整体截** —— 整体截会把作品名切掉,
                #: 退化成"按盘链做键",同一链上第二个作品就被前一个挡掉。
                key = f"forecast:{a['pan_key'][:90]}|{a['work_key'][:80]}"
                try:
                    if _recently_alerted(session, uid, key, cooldown):
                        continue
                except Exception:  # noqa: BLE001 - 读不到冷却就照发(宁可多报)
                    logger.warning("爆款预警冷却预检失败 %s(按未发过处理)", key,
                                   exc_info=True)
                    session.rollback()
                #: ★ 先发、成功后才落冷却行(理由同 `push_surge`)
                if client.send(_render(a, nl)):
                    sent += 1
                    n_sent += 1
                    _mark_alerted(session, uid, key, cooldown, f"{a['hands']} 人/24h")
            #: 运行记录:漏斗每一级都记,否则「没预警」与「看不见数据」分不开
            try:
                from app.services.tenant_base import _record_run

                _record_run(session, uid, "promo_forecast", "success",
                            f"台账{stats.get('rows', 0)}行 "
                            f"作品桶{stats.get('works', 0)} "
                            f"窗口完整{stats.get('eval', 0)} "
                            f"达标{stats.get('hit', 0)} ⇒ "
                            f"已有链{stats.get('owned', 0)} "
                            f"无源链{stats.get('no_src', 0)} "
                            f"套话{stats.get('cta', 0)} "
                            f"发出{n_sent} "
                            f"无作者{stats.get('no_author', 0)}行")
                session.commit()
            except Exception:  # noqa: BLE001 - 运行记录失败不挡业务
                session.rollback()
                logger.warning("爆款预警运行记录失败 user=%s", uid, exc_info=True)
    finally:
        session.close()
    return sent