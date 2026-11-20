# -*- coding: utf-8 -*-
"""同资源「**推手数突增**」提醒(2026-11-20) —— 「这个资源突然有更多人在推了」。

## 它补的是哪个盲区

在此之前全仓**没有任何一条**按"推它的人变多"触发的提醒:

| 现有机制 | 它回答的问题 | 为什么不够 |
|---|---|---|
| `push_hot_rise_pct` 去重涨幅 | 同一资源**流量**涨了吗 | 只管"要不要重复推",**从不告警**;而且按点赞/播放涨 30% 判,不是按人 |
| `focus_res`(公众号共振) | 公众号里多号同发了吗 | **只覆盖公众号**一条链 |
| `push_viral_alerts`(资源库) | 24h 内 ≥3 个号同发？ | 只读 `WechatPanLink`(公众号),且近 7 天同链**最多 2 个号** ⇒ 有史以来 0 行 |
| `cross_resonance_tick` | 跨平台共振榜 | **每天**一张日报,不回答"刚刚突然变多" |

⇒ 用户 2026-11-20 问「如果哪个资源突然推的人变多是否会有提醒」,答案是**基本没有**。

## 判据

对**同一条盘链 + 同一部链内作品**(`pan_id` 源链优先,没有才用 `pan_url`;
作品名只从标题 `《…》` 取 —— 两层身份的理由见下面「身份是**两层**的」):

    当前窗口的不同作者数  n   ≥  `push_surge_min_hands`(默认 6)
    比上一窗口的增量      n-p ≥  `push_surge_min_gain`(默认 4)
    且相对倍数            n/p ≥  `push_surge_min_ratio`(默认 1.8;`p=0` 时这条不适用)

三个条件**同时**成立才算突增。只用"绝对值高"会把**长期火**的资源每小时报一次;
只用"涨了"会把 2→3 也算成突增。三条一起才对应"**突然有很多人加入**"。

## 「每个板块都加上」怎么落的

数据源是 `push_ledger`(推送台账)—— **它已经是全板块的**(公众号/抖音线索/抖音对标号/
小红书/快手/贴吧/B站/知乎/微博同行各写各的行)。所以这里**不按板块分开跑**:
把全部板块的行聚到一起按盘链数人,**再在卡片里拆出板块分布**。

⚠️ 为什么不按板块各跑一遍:全局聚合是**每个板块的超集** —— 只在贴吧起量的资源
(贴吧单板块 6 个人)在全局同样是 6 个人,照样报得出来;而分开跑会让**同一次起量
报 N 次**(每个板块一次),正撞本仓"噪音化的守卫等于没有守卫"那条。
卡片里给 `板块分布`,人一眼看得出"是全网在推还是就一个群在自嗨"。

## 为什么用**作者数**而不是行数

行数会被**我们自己**污染:同一资源被 `board_digest`/补推/重试各推一遍就是多行,
而"有多少人在推"才是资源本身的热度。作者为空的行走不进 `hands`
(数不出是谁 ⇒ 不能算一个推手),这类行**单独记账**并写进运行记录 —— 不许静默丢。

## ⚠️⚠️ 身份是**两层**的:盘链 + 链内作品名

**`pan_url` 不是可靠的作品身份 —— 一条链会被反复复用挂不同资源。** 近 7 天真实审计:

| 盘链 | 7 天行数 | 挂的不同作品 |
|---|---|---|
| `pan.quark.cn/s/d1d3c3385b95` | 129 | **31** |
| `pan.quark.cn/s/1e4c093cb2cd` | 37 | 15 |
| `pan.quark.cn/s/823f6ca17019` | 29 | 16 |

只按盘链聚时,`d1d3c3385b95` 的「7 个人在推」= 3 人推 A + 2 人推 B + 2 人推 C,
卡片却指着 A 的标题说这些人都在推它。所以 `detect_surges` 在链内**再按作品名分桶**
(见 `_work_key`)。同样的 7 天:只按盘链 8 条告警 → 两层身份 6 条,
**去掉的 2 条都是这种混桶**(`4d00f5bdd943`「超级会员V2」、`1e4c093cb2cd`「穿成短命炮灰」
其实是 3 个作品拼出来的)。这两个数都是用真实台账回放量出来的,改判据后要重跑。

## ⚠️ 链身份的已知边界(别把它当全量)

· 公众号那条链的 `pan_url` 是**我方转存链** ⇒ 多号推同一资源会收敛成一条,**数得准**;
· 小红书/快手/贴吧/B站/知乎给的是**源链** ⇒ 每个推广号自己建链 ⇒ **会低估**;
· `pan_id`(源链)只有部分推送点给(近 7 天带盘行里约 7%)⇒ 实际大多按 `pan_url` 聚;
· 作品名只从 `《》` 取(台账里 `《》` 覆盖率仅 22%):没有书名号的行退化成按整条标题,
  所以**同一作品的不同标题写法会被拆开**(实测 7 天里没有因此漏报,但别指望它全)。

实测近 7 天仍有 6 次真起量能被它抓到(跨 1~4 个板块);但**"没报"不等于"没人推"**,别反推。

## 去向

**管理群**(`webhook_for(settings, "admin")`)—— 这是运营情报,不进客户群
(客户只会看到内容卡,不该收到"这个资源我们内部觉得火")。
每条**(盘链+作品)** **24h 冷却**(`_recently_alerted` 纯读预检 +
`_mark_alerted` 发送成功后才落行);旁路失败只留日志,不抛。

## ⚠️ 冷却门的语义:**已经发出去过**,不是「跑过一次检测」

不用 `feishu_alert_gate` 的「检查并占位」语义,也不用「先占位、失败再 `rollback()`」——
pysqlite 上 SAVEPOINT 作为最外层事务时 `RELEASE` 就已落盘,`rollback()` 撤不回来
(2026-11-20 实测:换另一条连接读得到)。一次飞书抖动就会吃掉这条提醒 24h,
而这 3h 窗口一过推手数再也凑不齐 = **永久丢失**。详见 `_recently_alerted` 的注释。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)


def _identity(row) -> str:
    """台账行 → **盘链身份**。源链(`pan_id`)优先,没有才退回 `pan_url`。

    ⚠️ 两者都是 `pan_key()` 归一后的串(`pan_id` 在写入时已归一)。
    ⚠️ 这只是**第一层**身份。一条盘链上可能挂着**好几个不相干的资源**(实测见
    `_work_key` 头注),所以 `detect_surges` 还要在链内再按作品名分桶。
    """
    from app.services.push_ledger import pan_key

    pid = str(getattr(row, "pan_id", "") or "").strip()
    return pid or pan_key(str(getattr(row, "pan_url", "") or ""))


_BRACKET_RE = re.compile(r"《([^》]{2,30})》")


def _bracket_names(title: str) -> list[str]:
    """标题里 `《…》` 括起来的**候选作品名**(已 `resource_key` 归一并去掉空串)。

    ⚠️ 只认 `《》`:它是**作品名**的专用引号。`【】` 在本仓台账里是**装饰/网盘标语**
    (`【超级会员V2】` `【入口自取】` `【完整版】`),拿它当作品名会把不同资源并成一桶。
    """
    from app.services.resource_library import resource_key

    out: list[str] = []
    for m in _BRACKET_RE.findall(str(title or "")):
        k = resource_key(m)
        if k and k not in out:
            out.append(k)
    return out


def _work_key(title: str, names) -> str:
    """一行 → **链内作品身份**(在 `_identity` 之上再分一层)。

    ## 为什么必须分这一层(2026-11-20 真实台账审计)

    `pan_url` **不是可靠的作品身份** —— 一条链会被反复复用挂不同资源。实测近 7 天:

      · `pan.quark.cn/s/d1d3c3385b95` 129 行,**31 个不同作品**
        (顶级训犬大师 / 麦穗与羊 / 予上情深 / 野火 / 直男s友 …);
      · `pan.quark.cn/s/1e4c093cb2cd` 37 行 / 15 个作品;
      · `pan.quark.cn/s/823f6ca17019` 29 行 / 16 个作品。

    只按盘链聚时 `d1d3c3385b95`「7 个人在推」= **3 个人推 A + 2 个人推 B + 2 个人推 C**,
    卡片会指着 A 的标题说「B/C 的人也在推它」—— 这正是这条提醒要防的假信号。
    按链内作品名分桶后,同样的 7 天里 8 条告警降到 6 条,**去掉的 2 条全是这种混桶**。

    ## 怎么分

    ① 语料 = **本窗口 + 上一窗口**该盘链所有行的 `《…》` 名字(两个窗口必须用**同一份**
       语料,否则同一批行会被分到两套桶里,增量算出来是假的);
    ② 一行归属到**含于它标题**的最长那个名字 —— 原始标题与归一后标题**任一命中即可**
       (归一化会把 `【】` 连里面的作品名一起剥掉,只看归一后的会漏);
    ③ 标题里没有任何名字 ⇒ 用它自己的 `resource_key`(裸标题变体极少,实测无影响)。

    ⚠️ 为什么不用 `resource_library.same_resource` 两两比较:它把 `【】` 当装饰剥掉,
    `【麦穗与羊】原稿未删版` 归一后是「原稿未删版…」,**作品名被剥没了**。
    """
    from app.services.resource_library import resource_key

    raw = str(title or "").strip()
    norm = resource_key(raw)
    best = ""
    for n in names:
        #: ⚠️ **必须同时看 raw** —— `resource_key` 会把 `【】` 当装饰剥掉,
        #: `【麦穗与羊】原稿未删版` 归一成「原稿未删版…」,作品名就没了。
        if n and (n in raw or n in norm) and len(n) > len(best):
            best = n
    return best or norm


def detect_surges(session: Session, user_id: int, *, window_hours: float = 3.0,
                  min_hands: int = 6, min_gain: int = 4, min_ratio: float = 1.8,
                  top: int = 5, now: datetime | None = None,
                  stats: dict | None = None) -> list[dict]:
    """找出**这一小时**推手数突增的盘链(按增量降序,最多 `top` 条)。

    返回 `[{pan_key, pan_url, pan_type, title, hands, prev_hands,
             sections:{section: 该板块的**不同作者数**}, first, last}]`。

    ⚠️ `sections` 与 `hands` **同一口径(数人)**:各板块之和 ≤ `hands`
    (同一个人在多个板块出现会被两边各算一次),但绝不会大于它。
    数行的话卡片上「微信 7」与头部「7 人」对不上,人一眼就会怀疑整张卡。

    ⚠️ **纯读**:不开网络、不写库 —— 冷却门与发送留给 `push_surge_tick`。
    """
    from app.db.models import PushLedger

    now = now or datetime.now()
    w = timedelta(hours=float(window_hours))
    cur_start, prev_start = now - w, now - 2 * w

    rows = session.execute(
        select(PushLedger.pan_url, PushLedger.pan_id, PushLedger.section,
               PushLedger.author, PushLedger.title, PushLedger.pan_type,
               PushLedger.pushed_at)
        .where(PushLedger.user_id == user_id,
               PushLedger.pushed_at >= prev_start,
               PushLedger.pushed_at < now,
               #: 只要**有身份**就行:`pan_id` 但没 `pan_url` 的行同样能聚
               #: (实测 7 天里 6 行是这种形状;旧写法 `pan_url != ""` 会把它们漏掉)
               or_(PushLedger.pan_url != "", PushLedger.pan_id != ""))
        .order_by(PushLedger.pushed_at.asc())
    ).all()

    #: ★★ 先按盘链归集 `《…》` 候选作品名 —— **当前窗口与上一窗口共用这一份语料**。
    #: 若各用各的语料,同一批行会被分进两套桶,`prev_hands` 会算成 0(假突增)。
    names: dict[str, list[str]] = {}
    for r in rows:
        pk = _identity(r)
        if not pk:
            continue
        bucket = names.setdefault(pk, [])
        for n in _bracket_names(str(r.title or "")):
            if n not in bucket:
                bucket.append(n)

    cur: dict[tuple[str, str], dict] = {}
    prev: dict[tuple[str, str], set] = {}
    n_no_author = 0
    n_masked = 0
    for r in rows:
        pk = _identity(r)
        if not pk:
            continue
        who = str(r.author or "").strip()
        if r.pushed_at < cur_start:                    # 上一窗口:只数人
            if who:
                prev.setdefault((pk, _work_key(str(r.title or ""), names[pk])),
                                set()).add(who)
            continue
        if not who:
            #: ⚠️ 作者为空 ⇒ 数不出是谁,**不能算一个推手**;但必须记账(不许静默丢)
            n_no_author += 1
            continue
        if "*" in who:
            #: ⚠️ **打码作者名是不稳定身份**(2026-11-20 审计):快手的作者名 100% 被打码
            #: (`水***）`)、抖音对标号 19.5%,同一个号可能出现 `实***库` 与 `实测宝库`
            #: 两种写法 ⇒ 数成 2 个人;也可能两个不同的号打码后撞成一个 ⇒ 数成 1 个人。
            #: 7 天里因此错分的桶只有 1 个(实测宝库),**不改判据**;但要记账 ——
            #: 否则「这条为什么多/少报一个人」在库里查不出来。
            n_masked += 1
        k = (pk, _work_key(str(r.title or ""), names[pk]))
        g = cur.get(k)
        if g is None:
            cur[k] = g = {"pan_key": pk, "pan_url": str(r.pan_url or ""),
                          "pan_type": str(r.pan_type or ""), "title": "",
                          "hands": set(), "sections": {},
                          "first": r.pushed_at, "last": r.pushed_at}
        g["hands"].add(who)
        #: ⚠️ 板块分布也要**数人**,不能数行 —— 与卡片头那条 `hands` 同一口径。
        #: 数行的话同一个人在一个板块重推 3 次就显示"微信 7",而全卡是"7 人",
        #: 人一加就发现对不上(实测 11-19 麦穗与羊:7 人 / 板块合计 8,差的就是重推)。
        sec = str(r.section or "")
        g["sections"].setdefault(sec, set()).add(who)
        g["last"] = r.pushed_at
        #: 标题取**最早那条非空的**(与 `window()`/`cross_section_summary` 同一口径:
        #: 先出现的那份通常是"第一次发现它"时的原话)
        if not g["title"]:
            g["title"] = str(r.title or "")

    out: list[dict] = []
    for key, g in cur.items():
        n = len(g["hands"])
        p = len(prev.get(key, ()))
        if n < int(min_hands):
            continue
        if n - p < int(min_gain):
            continue
        if p and float(min_ratio or 0) > 0 and n < p * float(min_ratio):
            continue
        #: `sections` 内部是 set(按人去重),对外给计数 —— 加起来正好等于 `hands`
        out.append({**g, "sections": {s: len(v) for s, v in g["sections"].items()},
                    "work_key": key[1], "hands": n, "prev_hands": p})
    out.sort(key=lambda x: (-(x["hands"] - x["prev_hands"]), -x["hands"],
                            str(x.get("title") or "")))
    if stats is not None:
        #: `pans` = 盘链数,`works` = **链内作品桶数** —— 两个数分开记,
        #: 否则「一条链挂 31 个作品」在运行记录里看不出来
        stats.update({"rows": len(rows), "pans": len(names), "works": len(cur),
                      "no_author": n_no_author, "masked": n_masked, "cand": len(out)})
    return out[:int(top)] if top else out


def _render(a: dict, nl: str) -> str:
    """一条突增 → 飞书纯文本。"""
    from app.services.board_digest import board_label

    secs = sorted(a["sections"].items(), key=lambda kv: -kv[1])
    spread = " · ".join(f"{board_label(s) or s} {n}" for s, n in secs[:6])
    #: 第一行优先用**作品名**(链内分桶的那把钥匙),没有再退回标题/链。
    #: 同一张卡上「作品名 / 标题」并列,人一眼能看出这条链是不是又被挂到别的东西上了。
    head = a.get("work_key") or a["title"][:40] or a["pan_key"][:40]
    lines = [f"📈 资源突然有更多人在推:{head[:40]}",
             f"👥 推手 **{a['prev_hands']} → {a['hands']} 人**"
             f"(+{a['hands'] - a['prev_hands']})· 涉及 {len(secs)} 个板块"]
    if spread:
        lines.append(f"🗂 {spread[:180]}")
    if a.get("pan_type"):
        lines.append(f"🧭 {a['pan_type']}")
    if a.get("pan_url"):
        lines.append(a["pan_url"])
    return nl.join(lines)


def _recently_alerted(session, user_id: int, key: str, cooldown_hours: float) -> bool:
    """**纯读**预检:这条(盘链+作品)在冷却期内发过没有?

    ⚠️ 为什么不用 `feishu_alert_gate` 做这件事:那个门是「**检查并占位**」的语义,
    它会在返回 True 时就写冷却行。而这里要的是「**发送成功之后**才占位」——
    先占位再发的话,一次飞书抖动就把这条提醒吃掉 24h,而这 3h 窗口一过
    推手数再也凑不齐,**等于永久丢失**。

    ⚠️⚠️ 也**不能**靠「先占位、失败再 `rollback()`」:那个门内部用 SAVEPOINT 写,
    而 pysqlite 上 SAVEPOINT 的 `RELEASE` 走在最外层时就会落盘,
    `session.rollback()` 撤不回来(2026-11-20 实测:换一条连接读得到)。
    ⇒ 唯一可靠的做法是**压根不提前写**。
    """
    from app.db.models import FeishuAlert

    row = session.scalar(select(FeishuAlert).where(
        FeishuAlert.section == "push_surge",
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
        feishu_alert_gate(session, user_id, "push_surge", key, cooldown_hours, reason)
        session.commit()
    except Exception:  # noqa: BLE001 - 冷却没落盘只影响去重
        logger.warning("推手突增冷却门写入失败 %s", key, exc_info=True)
        session.rollback()


def push_surge_tick(settings=None) -> int:
    """定时入口(每小时):找出推手数突增的盘链 → **管理群**。返回发送成功数。"""
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.db import get_session_local
    from app.db.models import User
    from app.services.feishu_client import FeishuClient, webhook_for

    hook = webhook_for(settings, "admin")
    if not hook:
        return 0
    kw = {"window_hours": float(getattr(settings, "push_surge_window_hours", 3) or 3),
          "min_hands": int(getattr(settings, "push_surge_min_hands", 6) or 6),
          "min_gain": int(getattr(settings, "push_surge_min_gain", 4) or 4),
          "min_ratio": float(getattr(settings, "push_surge_min_ratio", 1.8) or 1.8),
          "top": int(getattr(settings, "push_surge_top", 5) or 5)}
    cooldown = float(getattr(settings, "push_surge_cooldown_hours", 24) or 24)
    client = FeishuClient(hook, getattr(settings, "feishu_secret", ""))
    session = get_session_local()()
    nl = chr(10)
    sent = 0
    try:
        for (uid,) in session.execute(select(User.id).where(User.enabled.is_(True))).all():
            stats: dict = {}
            try:
                alerts = detect_surges(session, uid, stats=stats, **kw)
            except Exception:  # noqa: BLE001 - 单用户失败不挡其余
                session.rollback()
                logger.exception("推手突增检测失败 user=%s", uid)
                continue
            n_sent = 0
            for a in alerts:
                #: ⚠️ 冷却键必须带**作品名**:一条盘链挂多个作品是常态(实测一条链 31 个),
                #: 只按盘链做键会让同一链上第二个真起量的作品被前一个的冷却挡掉 24 小时。
                #: 分段截断,**别整体截**:整体截会把后面的作品名切掉,又变回"按链做键"。
                key = (f"surge:{a['pan_key'][:90]}|{str(a.get('work_key') or '')[:80]}")
                try:
                    if _recently_alerted(session, uid, key, cooldown):
                        continue
                except Exception:  # noqa: BLE001 - 读不到冷却就照发(宁可多报)
                    logger.warning("推手突增冷却预检失败 %s(按未发过处理)", key, exc_info=True)
                    session.rollback()
                #: ★ **先发、成功后才落冷却行**(见 `_recently_alerted` 的两条 ⚠️):
                #: 先占位再发 ⇒ 一次网络抖动吃掉这条提醒 24h,而这 3h 窗口一过
                #: 推手数再也凑不齐 = 永久丢失。发送期间会话里**没有未提交写**。
                if client.send(_render(a, nl)):
                    sent += 1
                    n_sent += 1
                    _mark_alerted(session, uid, key, cooldown,
                                  f"{a['prev_hands']}->{a['hands']} 人")
            #: 运行记录:不写的话「今天没突增」与「作业没跑」在库里长得一样
            try:
                from app.services.tenant_base import _record_run

                _record_run(session, uid, "push_surge", "success",
                            f"窗口{kw['window_hours']:g}h 台账{stats.get('rows', 0)}行 "
                            f"盘链{stats.get('pans', 0)} 作品{stats.get('works', 0)} "
                            f"候选{stats.get('cand', 0)} 发出{n_sent} "
                            f"无作者{stats.get('no_author', 0)}行 "
                            f"打码名{stats.get('masked', 0)}行")
                session.commit()
            except Exception:  # noqa: BLE001 - 运行记录失败不挡业务
                session.rollback()
                logger.warning("推手突增运行记录失败 user=%s", uid, exc_info=True)
    finally:
        session.close()
    return sent