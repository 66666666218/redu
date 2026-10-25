# -*- coding: utf-8 -*-
"""板块**日结 / 周结**(2026-10-16) —— 用户口径:

> 「**有的客户反馈有点信息太多看不过来,能否加上每日总结昨天的信息然后定时发出,
>   再加一个周结每星期定时发送,每个板块都配置上**」

结论口径(用户 2026-10-16 亲定,四条):
  ① **发到各板块自己的客户群**(未配专属群的回落主群);
  ② 日结 **每天 08:30**,内容是**昨天** 00:00–24:00;周结 **周一 09:00**,内容是**过去 7 天**;
  ③ **只讲「有什么」** —— 不写失败率/覆盖率/采集状态那类**内部话**
     (内部数字的口径另有去处:管理群,见 `wechat_digest`)。

## 数据从哪来

**只读 `push_ledger`(推送台账)** —— 也就是「**客户昨天真的收到了什么**」。
为什么不读业务表:`resource_presence`(小红书/快手/贴吧/B站/知乎)**从来不落库**;
而且业务表记的是「采到了」,采到但没发出去的东西客户**从没见过**,算进日结就是虚报。
详见 `app/services/push_ledger.py` 的头注。

## 三条纪律

1. **不发空卡**:窗口内那个板块没有台账 ⇒ 那天就不发(「每个板块都配置上」指的是
   该配的都配上,不是"每天每个群都来一条")。
2. **当天/当周每个群只发一次**(`SystemConfig` 里按 `(period, section)` 记窗口标签)。
   重启/补跑不会重发;而**没发成功的那一个板块不会被打上标记**,下一次调用还能补上。
3. **【只讲有什么】** 卡片里禁止出现:失败数、覆盖率、"限流/风控/未登录"、我方内部术语
   (「我方链」三个字在客户群会暴露运营方身份 ⇒ 一律写**盘名**)。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 板块 → **客户看的名字**。键 = 台账里的 `section`(也就是"落地的那个群")
#: ⚠️ 两个 `_peer` 是 2026-10-16 新建的**同行群**(抖音/微博),与 `douhot`/`weibo` 分开 ——
#:    `douhot`/`weibo` 那两个 section 同时被**采集失败告警**用着,不能混。
BOARD_LABELS = {
    "wechat": "公众号",
    "douhot": "抖音热榜",
    "douyin_peer": "抖音",
    "weibo": "微博",
    "weibo_peer": "微博同行",
    "xiaohongshu": "小红书",
    "kuaishou": "快手",
    "tieba": "贴吧",
    "bilibili": "B站",
    "zhihu": "知乎",
    "baidu": "百度热榜",
    "multiplatform": "全平台热榜",
    "xianyu": "闲鱼",
}

#: 日结 / 周结各自的**窗口**与标题用词
PERIODS = {
    "daily": {"label": "昨天", "days": 1, "title": "昨日汇总"},
    "weekly": {"label": "过去 7 天", "days": 7, "title": "本周汇总"},
}


def board_label(section: str) -> str:
    """板块名(未登记的 section 就用原键 —— **别让一个没登记的新板块发不出去**)。"""
    return BOARD_LABELS.get(section, section)


def window_of(period: str, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """`(since, until, 窗口标签)` —— **本地零点**为界(与运行记录/周报同一口径)。

    · 日结 = **昨天**整天(今天零点往前一天);
    · 周结 = 今天零点往前 7 天(含昨天)⇒「过去 7 天」。

    ⚠️ **标签用的是"解析后的"期名**:期名拼错时窗口退回日结,标签也必须跟着退回 ——
    否则 `w33k` 与 `weekly` 会是**同一个窗口的两个标签** ⇒ 幂等判据同时失效、当天双发。
    """
    name = period if period in PERIODS else "daily"
    spec = PERIODS[name]
    today = (now or datetime.now()).date()
    until = datetime.combine(today, time.min)                       # 今天 00:00
    since = until - timedelta(days=int(spec["days"]))
    return since, until, f"{today.isoformat()}:{name}"


def build(session: Session, user_id: int, section: str, since: datetime, until: datetime,
          top: int = 0) -> dict:
    """取该板块在这个窗口里的汇总(薄壳:`push_ledger.window` + 板块名)。"""
    from app.services import push_ledger

    data = push_ledger.window(session, user_id, section, since, until, top=top)
    data["section"] = section
    data["label"] = board_label(section)
    return data


def render_card(data: dict, period: str, top: int = 8,
                part: tuple[int, int] = (1, 1), start_rank: int = 1) -> dict:
    """渲染成飞书卡片。**客户口径**:只有"有什么"、几处可点、一行怎么用。

    `top` = 最多列几行(**0 = 全部**,2026-10-25 起日结默认走这条);
    `part` = 分卡时的 `(第几, 共几)` —— 只有真的分卡了才会出现在标题上;
    `start_rank` = 这一张卡的**第一条是总榜第几名**(分卡时序号要接着上一张走,
    否则每张卡都从 1 开始,客户会以为有三份不同的榜单)。

    ⚠️ **`top=0` 不许再退化成 `items[:0]`**(那是空卡)。2026-10-25 之前
    这个切片没防 0,而新的默认值就是 0 ⇒ 不修就会把「完整版」发成一张空壳。
    """
    from app.services.feishu._cards import _col_set_row, _md_safe, metrics_md, strip_others

    spec = PERIODS.get(period) or PERIODS["daily"]
    label, when = data["label"], spec["label"]
    n_res = int(data["resources"])
    head = f"📊 **{label} · {spec['title']}** —— {when}一共 **{n_res} 个资源**"
    if int(data.get("repeats") or 0):
        head += f"(其中 {data['repeats']} 个被反复推荐)"
    if n_res >= 2:
        #: ★ 2026-10-26:排序规则**必须写在脸上**。清单按热度降序,
        #: 但没有互动数的那种源会落到最后 —— 不说的话,"排后面"会被读成"不重要"。
        head += "\n**按热度排序**(👍/▶ 高的在前;没有互动数据的按发现顺序排在后面)"
    if int(part[1]) > 1:
        head += f" · 第 {int(part[0])}/{int(part[1])} 部分"
    howto = ("资源名点开即取用(标注的是网盘);「**看作品**」点开是那条原帖,"
             "可以先看别人推成什么样。")
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"{head}\n{howto}"}},
        _col_set_row([("**资源**", 5), ("**对标作品**", 3), ("**热度**", 2)], grey=True),
    ]
    from app.services.douyin_leads import _pan_name

    _all = list(data.get("items") or [])
    items = _all if int(top or 0) <= 0 else _all[:int(top)]
    if not items:
        #: ⚠️ 调用方本来就不该为空窗口调这里(纪律 1);真到了这里也要**说人话**,别发一张空壳
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": f"{when}没有新的内容。"}})
        return _card(elements, label)
    for idx, g in enumerate(items, int(start_rank or 1)):
        name = _md_safe(strip_others(str(g.get("title") or "")))[:28] or "—"
        pan_url = str(g.get("pan_url") or "")
        pan = str(g.get("pan_type") or "") or _pan_name(pan_url)
        cell = (f"[{name}]({_md_safe(pan_url)})" + (f" · 🔴{pan}" if pan else "")
                if pan_url else f"{name} · —")
        #: ★ 2026-10-26:**带序号**,让"按热度排"这件事在卡片上看得见(否则只是一串没有次序感的行)
        cell = f"**{idx}.** {cell}"
        #: ★ 被反复推荐的那个要有**看得见的标记**:客户抱怨的就是"同一条反复出现"。
        #: 挂在**资源**那一格(它最宽),而不是挤在「对标作品」里 —— 那里本来就有链接。
        if int(g.get("pushes") or 1) > 1:
            cell += f" · 🔁{g['pushes']}次"
        url = str(g.get("url") or "")
        work = f"[▶ 看作品]({_md_safe(url)})" if url else "—"
        elements.append(_col_set_row([(cell, 5), (work, 3), (metrics_md(_metrics_of(g)), 2)]))
    #: ⚠️ 这里比的是**资源数**,不是"推送行数" —— 拿行数比会算出"还有 1 条"这种鬼话
    #: (2 个资源、其中一个推了 2 次 ⇒ 3 行),而客户要的是"还有几个资源没列出来"。
    #: ★ **分卡时不许说这句**(2026-10-25):第 1/n 部分本来就没列全,再说一句
    #: 「还有 N 个资源」既不符合事实,也会让人以为被省略了 —— 而那正是这次要修掉的东西。
    if int(part[1]) <= 1 and n_res > len(items):
        elements.append({"tag": "div", "text": {
            "tag": "lark_md", "content": f"…还有 {n_res - len(items)} 个资源,想看完整列表回一句就行。"}})
    return _card(elements, label)


def _card(elements: list[dict], label: str) -> dict:
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "turquoise",
                       "title": {"tag": "plain_text", "content": f"🗂 {label} · 汇总"}},
            "elements": elements}


def _metrics_of(g: dict) -> dict:
    """台账里的 `metrics` 是**JSON 串**(表字段就那么宽)—— 渲染前解回 dict。"""
    import json

    raw = g.get("metrics")
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _already_sent(session: Session, period: str, section: str, tag: str) -> bool:
    """这个群这一期发过没有(`SystemConfig`,键 ≤64 字符)。"""
    from app.db.models import SystemConfig

    key = f"bd_sent_{period}_{section}"
    row = session.get(SystemConfig, key)
    return bool(row and str(row.value) == tag)


def _mark_sent(session: Session, period: str, section: str, tag: str) -> None:
    """标记已发。⚠️ 只在**发送成功之后**调用 —— 失败的不打标,下次还能补上。"""
    from app.db.models import SystemConfig

    key = f"bd_sent_{period}_{section}"
    row = session.get(SystemConfig, key)
    if row is None:
        session.add(SystemConfig(key=key, value=tag))
    else:
        row.value = tag
    session.flush()


def _weekly_dow(settings) -> int | None:
    """从**周结的 cron** 里读出"星期几"(0=周一 … 6=周日)。读不出来返回 `None`。

    ⚠️ **以 cron 为准,不再抄一份"周一"常量**:两处真相迟早飘(改一处忘一处)。
    ⚠️ 只认**单值**(`1` / `mon` / `MON`)—— 配置是我们自己的,不伺候 `1-5` 这种;
    **认不出来就返回 None,并且退回「照常发日结」**(多一条消息) —— 宁可多发一条,
    也不要因为一行 cron 被改写就把日结**静默停掉**。
    """
    parts = str(getattr(settings, "board_digest_weekly_cron", "") or "").split()
    if len(parts) < 5:
        logger.warning("周结 cron 读不出星期(%r)⇒ 日结按「照常发」处理",
                       getattr(settings, "board_digest_weekly_cron", ""))
        return None
    dow = parts[4].strip().lower()
    if dow.isdigit() and 0 <= int(dow) <= 7:
        #: ⚠️⚠️ **cron 的星期号与 Python 的错开一天**:cron 里 `0/7`=周日、`1`=周一,
        #: 而 `datetime.weekday()` 是 `0`=周一、`6`=周日。照抄就会把"周一"读成周二
        #: (我第一版就是这么写的,被自己写的测试当场打红)。
        return (int(dow) % 7 + 6) % 7
    names = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    if dow in names:
        return names[dow]
    logger.warning("周结 cron 的星期字段 %r 认不出来 ⇒ 日结按「照常发」处理", dow)
    return None


def _is_weekly_day(settings, now: datetime | None = None) -> bool:
    """今天是不是"周结日"(那天日结就重复了:周结的窗口已包含昨天)。"""
    dow = _weekly_dow(settings)
    return dow is not None and (now or datetime.now()).weekday() == dow


def _split_even(items: list, per: int) -> list[list]:
    """把一列资源**均分**成若干张卡(每张不超过 `per` 行);`per<=0` = 一张卡装完。

    ⚠️ **为什么要均分,不是"切满再切余"**(2026-10-25):36 个资源按 35 切会得到
    **35 + 1** 两张卡 —— 第 2 张只孤零零一行,客户看到的就是"这条消息没内容"。
    改成按张数均分(36 ⇒ 18 + 18、58 ⇒ 29 + 29)后,张数不变、观感正常。
    """
    n = len(items)
    if n == 0:
        return [[]]
    if per <= 0 or n <= per:
        return [items]
    parts = (n + per - 1) // per                 # 需要的张数
    size = (n + parts - 1) // parts              # 每张实际拿几行(向上取整,均分)
    return [items[i:i + size] for i in range(0, n, size)]


def send_digest(session: Session, user_id: int, section: str, period: str,
                settings, top: int = 0, now: datetime | None = None) -> bool:
    """给一个板块发一条日结/周结。**发送失败不回滚、不打标**(下次补)。

    `now` 只为**可测**存在(窗口以它为准);生产不传,取真实当前时间。
    """
    from app.services.feishu_client import FeishuClient

    hook = _hook_for(settings, section)
    if not hook:
        return False
    since, until, tag = window_of(period, now)
    #: ★ **幂等唯一入口就是这里**(不只在作业循环里查):否则任何**直接**调它的人
    #: (脚本/补跑/下一个作业)都能把同一期再发一遍 —— "两处真相"迟早漏一处。
    if _already_sent(session, period, section, tag):
        logger.info("板块%s:%s 本期已发,跳过", period, section)
        return False
    data = build(session, user_id, section, since, until, top=top)
    if not data["items"]:
        return False                       #: 纪律 1:空窗口不发
    #: ★★ **完整发送,但按卡切分**(2026-10-25 用户口径「每日总结发完整的,不要再省略了」)。
    #: `top` 默认已是 0(全部);切分**不是为了省略**,而是飞书交互卡片有体积上限 ——
    #: 一个板块一天 60~80 个资源塞一张会被服务端拒,那时就变成"一条都发不出去"。
    #: 所以:内容一条不少,只是分几张卡(标题带「第 k/n 部分」)。
    #: ⚠️ **全部发成功才打标** —— 中途失败就留着重发,别让客户收到"第 1/3 部分"后再也等不到后两张。
    items = list(data["items"])
    per = int(getattr(settings, "board_digest_max_rows_per_card", 35) or 0)
    chunks = _split_even(items, per)
    client = FeishuClient(hook, getattr(settings, "feishu_secret", ""))
    rank = 1
    for k, chunk in enumerate(chunks, 1):
        card = render_card({**data, "items": chunk}, period, top=top,
                           part=(k, len(chunks)), start_rank=rank)
        if not client.send_card(card):
            logger.warning("板块%s:%s 第 %d/%d 张卡发送失败 ⇒ 不打标,下次整期重发",
                           period, section, k, len(chunks))
            return False
        rank += len(chunk)
    _mark_sent(session, period, section, tag)
    logger.info("板块%s:%s 发出(%d 行 / %d 个资源 / %d 张卡)",
                PERIODS.get(period, {}).get("title", period), section,
                data["rows"], data["resources"], len(chunks))
    return True


def run_board_digest_all_users(period: str = "daily", settings=None,
                               session: Session | None = None,
                               now: datetime | None = None) -> int:
    """作业入口:给每个启用用户、每个**有内容的板块群**发一条日结或周结。返回发出条数。

    ⚠️ 登记为 **role="both"**:两台实例的库是**分开的**(本机 wechat 侧 / 远程 hotspot 侧)
    ⇒ 各侧只对**自己这边推过内容的板块**负责(台账在本地,另一侧的板块在这边压根没有行),
    合并到一边跑会把另一侧的内容整片漏掉。

    `now` 只为**可测**存在(窗口与"今天是不是周结日"都用它);生产不传,取 `datetime.now()`。
    """
    from app.db import get_session_local
    from app.db.models import User
    from app.services import push_ledger

    from config.settings import get_settings

    settings = settings or get_settings()
    if not bool(getattr(settings, "board_digest_enabled", True)):
        return 0
    own = session is None
    session = session or get_session_local()()
    top = int(getattr(settings, "board_digest_top", 0) or 0)   #: ⚠️ 别写成 `or 8` —— 那会把「0=全部」变回 8
    allow = {x.strip() for x in
             str(getattr(settings, "board_digest_boards", "") or "").split(",") if x.strip()}
    since, until, _tag = window_of(period, now)
    #: ★ **周结日不发日结**(2026-10-16 用户口径「可以按照你的建议来进行」):
    #: 周结的窗口(过去 7 天)**已经包含昨天** ⇒ 那天再发一张日结就是把同一批资源说两遍,
    #: 而客户抱怨的恰恰是消息太多。⚠️ 判据取自**周结的 cron**(见 `_weekly_dow`),
    #: 不在这里写死"周一" —— 改 cron 就等于改这里,没有第二处真相。
    skip_daily = (period == "daily"
                  and bool(getattr(settings, "board_digest_daily_skip_on_weekly", True))
                  and _is_weekly_day(settings, now))
    sent = 0
    try:
        for (uid,) in session.execute(select(User.id).where(User.enabled.is_(True))).all():
            from app.services.tenant_base import _record_run

            try:
                if skip_daily:
                    #: ⚠️ **必须落一条运行记录**:否则"今天是周结日所以没日结"与
                    #: "日结坏了"在运行记录上长得一模一样(本仓母题)。
                    _record_run(session, uid, "board_digest_daily", "success",
                                "今天是周结日 ⇒ 日结跳过(周结的窗口已含昨天,"
                                "同一批资源不重复说两遍)")
                    session.commit()
                    continue
                have = push_ledger.boards_with_content(session, uid, since, until)
                done: list[str] = []
                skipped: list[str] = []
                failed: list[str] = []
                for section in sorted(have):
                    if allow and section not in allow:
                        continue
                    if not _hook_for(settings, section):
                        skipped.append(f"{section}(无群)")
                        continue
                    if _already_sent(session, period, section, _tag):
                        continue
                    if send_digest(session, uid, section, period, settings, top=top,
                                   now=now):
                        sent += 1
                        done.append(section)
                    else:
                        failed.append(section)
                note = f"{PERIODS.get(period, {}).get('title', period)}"
                #: ★ **台账行数要写出来**(2026-10-16):「一个群都没发」可能有两种原因 ——
                #: 昨天真没人推(正常),或者**台账根本没在记**(代码没生效/写失败)。
                #: 只写"没发"的话这两种长得一样;带上行数,后一种会立刻显形。
                note += f" 台账{sum(have.values())}行"
                if done:
                    note += f" 已发:{','.join(done)}"
                if skipped:
                    note += f" 跳过:{','.join(skipped)}"
                if failed:
                    #: ⚠️ 失败要**显形**:不然"这个群没收到日结"与"这个板块本来就没内容"同形
                    note += f" **未发:{','.join(failed)}**"
                _record_run(session, uid, f"board_digest_{period}", "success", note[:900])
                session.commit()
            except Exception as exc:  # noqa: BLE001 - 单用户失败不影响其余
                session.rollback()
                logger.exception("板块%s失败 user=%s", period, uid)
                _record_run(session, uid, f"board_digest_{period}", "failed", str(exc)[:200])
                session.commit()
    finally:
        if own:
            session.close()
    return sent


def _hook_for(settings, section: str) -> str:
    """这个板块的日结该发到哪 —— **与推送侧同一个解析**(`webhook_for`)。

    ⚠️ **必须同一套判据**,否则会出现"日结发去主群、内容却在专属群"这种去向错位:
    主群的人看不到那个板块的上下文,反而更乱。专属群没配时回落主群,
    与内容卡本身的行为**逐字一致**(用户 2026-10-16 口径:「各板块客户群,**未配回落主群**」)。
    """
    from app.services.feishu_client import webhook_for

    return webhook_for(settings, section)


def run_board_digest_daily(settings=None) -> int:
    """作业入口:每日 08:30(昨天整天)。薄壳,只为让调度表里两行各自可读。"""
    return run_board_digest_all_users("daily", settings=settings)


def run_board_digest_weekly(settings=None) -> int:
    """作业入口:每周一 09:00(过去 7 天)。"""
    return run_board_digest_all_users("weekly", settings=settings)
