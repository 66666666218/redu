"""网盘资源发现(2026-10-02):按资源词去**公开平台**搜,把别人贴出来的盘链转成我方分享链。

**与抖音那条链的分工 —— 两种内容形态**:
  · **口令型**(抖音):推广号把《群名》写进**标题** → 得先**口令解析**成分享链才能用;
  · **直链型**(知乎):回答里**直接把夸克/百度盘链贴出来** → 拿到就能转存,**连解析都省了**。

**实测**(2026-10-02,拿资源库的 5 个资源词搜知乎):87 条结果里 **3 条带直接盘链**
(`pan.quark.cn/s/…`、`pan.baidu.com/s/…`)。命中率不高,但**成本接近零** ——
不用登录新平台、不用开浏览器,走的就是 `cross_accounts` 里那条已经在跑的知乎搜索。

链路:资源词 → 搜知乎 → 抽盘链 → **按链接平台分发转存**(夸克/百度/迅雷)→ 入库 → 推飞书。

⚠️ **限速**:知乎搜索有日限且按频率风控,所以**逐词之间要间隔**(`_REQ_GAP`)。
"""
from __future__ import annotations

import re
import time

from sqlalchemy import func, select

from app.db.models import DiscoveredPanLink
from app.utils import get_logger

logger = get_logger(__name__)

_REQ_GAP = 4.0          # 逐词间隔(秒)——与 cross_accounts 同一套限速口径
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """清知乎返回里的杂质:`<em>` 高亮标签 + **结构化残留**。

    实测 snippet 常长这样:
    `最近爆火的花少2人格测试 [{'content': '这个太好玩了，<em>花少2` ——
    后面那截是接口把评论/正文结构直接 dump 出来了,展示时要在 `[{` 处截断。
    """
    t = _TAG_RE.sub("", text or "")
    t = re.split(r"\[\{|\{'|\{\"", t)[0]          # 结构化残留 → 到此为止
    return " ".join(t.split()).strip()


def _kind_of(url: str) -> str:
    """按域名判这条链属于哪个盘(决定用哪个客户端转存)。"""
    u = (url or "").lower()
    if "quark.cn" in u:
        return "quark"
    if "baidu.com" in u:
        return "baidu"
    if "xunlei.com" in u:
        return "xunlei"
    return ""


# **终态**失败特征:分享已死(被取消 / 删号 / 违规 / 过期)—— 重试一万次也不会变好。
# ⚠️ 只收**措辞明确**的;含糊的(如 `errno=-6` 却没有 `show_msg`)宁可留 `failed` 再试一次,
# 也别误判成终态,把一条其实能搬的链永久钉死(这个反向的坑 2026-10-02 踩过)。
_DEAD_LINK_HINTS = (
    "分享地址已失效", "链接失效", "分享不存在", "分享已取消", "分享已被删除",
    "分享文件已被删除", "文件不存在", "已被和谐", "违规内容", "含违规",
    "41011",                      # 夸克:分享地址已失效
    "get_share_user_banned",      # 迅雷:分享者被封
    #: ★ 2026-10-21:迅雷分享详情里的**终态状态**(`share_status != OK`)。
    #: 积压里 21 条 `PASS_CODE_EMPTY`(分享要求提取码而我们没有) + 1 条
    #: `SENSITIVE_RESOURCE`(敏感资源)一直当 `failed` 每轮重试 —— 重试一万次也不会变。
    #: 只收**状态码原文**,不把整句「分享状态异常」当判据(那会把可重试的也判死)。
    "PASS_CODE_EMPTY", "SENSITIVE_RESOURCE",
)


def _is_dead_link(message: str) -> bool:
    """这条失败是"**链本身已经死了**"吗?"""
    text = message or ""
    return any(h in text for h in _DEAD_LINK_HINTS)


#: 「**盘没空间**」的措辞。三盘各写各的,但都落在这几个词上(见各盘的返回构造):
#:   · 夸克 —— `QuarkError("夸克网盘容量不足,请清理空间或更换账号")` → `"夸克盘空间不足(...)"`;
#:   · 百度 —— `"百度盘空间不足(已用 ... / 配额 ...)"`;
#:   · 迅雷 —— `"空间不足:这个包需要 ..."`(`xunlei_transfer.is_space_error` 自己也是这么认的)。
#: ⚠️ **只认这几个词,不要放宽成"任何失败都算"** —— 那会把死链、验证码、限流
#: 全当成"盘满",于是把一条**打不开的链**当资源推给客户(内容事故)。
_SPACE_HINTS = ("空间不足", "容量不足", "file_space_not_enough", "space_insufficient")

#: 「**未转存,已按源链推送**」这句前缀是**库内的状态印记**,两处共用:
#: ① 直推时写进去;② 下一轮重试时**不许把它擦掉**(见 `sync` 里"保留历史标记"那段)。
#: ⚠️ 别在别处手写这个字面量 —— 改了前缀而漏改判据,等于把印记判据静默废掉。
_SRC_ONLY_PREFIX = "**未转存,已按源链推送**"


def _is_space_blocked(message: str) -> bool:
    """这条失败是「**盘没空间**」吗?

    ★★ **为什么单列一个判据**(2026-11-23,用户口径「内存不足时不转存直接推送」):

    盘满时 `sync` 会把候选埋进积压(`pending`),而积压**不推卡** —— 实测 2026-11-19
    起连续 5 天「转存 0」、积压 145→327,其中 **326 条**是空间不足。这些链**本身是好的、
    客户点开就能拿**,却被"必须转存成我方链才推"这条前提整批扣住了。
    ⇒ 盘满时**跳过转存、直接推源链**:客户拿到的东西不变(还是那份资源),
    我们只是少了"换成自己的链"这一步,而这一步此刻**物理上做不到**。

    ⚠️ 判据必须窄:只有**盘的空间问题**算这一类。「验码失败」「分享已失效」
    仍然是 `skipped`/`failed`,绝不走这条路(那些链客户也打不开)。
    """
    text = message or ""
    return any(h in text for h in _SPACE_HINTS)


def _source_pass_code(c: dict) -> str:
    """给**源链**(别人贴出来的分享)找提取码 —— 盘满直推时**必须**尽力补上。

    ★ **为什么单列一个函数**(2026-11-23):盘满直推这条路**没有经过转存**,
    所以拿不到 `res["code"]`(那是我们自己新创建分享的码)。而别人的分享**很多带密码**:
    实测积压里 326 条空间不足的候选中,**55 条 URL 自带 `?pwd=`、233 条标题里有提取码字样**
    —— 不带码推给客户 = 推出一堆**点开要密码却不说密码**的链。

    判据与转存时**同一套**(直接复用 `baidupan_transfer.extract_pwd`:先认 URL 里的
    `?pwd=xxxx`,再认正文里的「提取码/密码:xxxx」)。

    ⚠️ **取不到就返回空串,不猜**:卡片上就不会显示 🔑,客户得回原帖找码。
    这是**已知的降级**,不隐藏 —— 取码率要能从 `runs.detail` 和卡片上直接看出来。
    """
    from app.services.baidupan_transfer import extract_pwd

    return extract_pwd(c.get("title") or "", c.get("origin_url") or "")


def _recent_source_pushed(session, user_id: int, settings) -> set[str]:
    """窗口内**已经推过**的盘链身份(归一化,源链与已有链两档并起来)。

    ★ **为什么必须查台账**(2026-11-23,盘满直推上线当天就发现):
    直推出去的行**仍然是 `pending`**(空间出来要补搬)⇒ 下一轮它们还在队首、
    仍被判盘满、**又吃满直推名额**。不查的话每轮推的都是同样那 20 条
    (推送侧 `drop_recent` 会把它们挡掉,卡片变空),后面三百多条**永远轮不到** ——
    与本仓「排序 + 截断把某一类源整批埋掉」是同一个病(见 memory「静默失败=假成功」)。
    查过就跳过,**不消耗名额、也不改消息**(保持原文的空间不足措辞)。

    ⚠️ 读台账失败**只影响"可能少推几条"**(推送侧还有一道 24h 去重兜底),
    所以这里 warn 后返回空集,不让它挡住整轮发现。
    """
    from app.services import push_ledger

    hours = float(getattr(settings, "push_pan_dedupe_hours", 72) or 72)
    try:
        srcs, my = push_ledger.recent_pan_map(session, user_id, "zhihu", hours=hours)
    except Exception:  # noqa: BLE001
        logger.warning("盘满直推:读推送台账失败,本轮可能重复直推", exc_info=True)
        return set()
    return set(srcs) | set(my)


def _fail(message: str) -> dict:
    """失败结果:`死链` → `skipped`(**终态,不再重试**),其余 → `failed`(**下轮还会重试**)。

    ⚠️ **为什么必须分开**(2026-10-04):贴吧/知乎发现的影视盘链**大批是失效的**
    (实测一轮 17 条里 15 条死链),而旧实现一律记 `failed` —— 于是这些死链**每天被重试一遍、
    永远重试不完**,`failed` 越堆越多,运行记录看着还像"链路故障"。
    现在能看到百度的 `show_msg` 了,就该把"**已失效**"和"**限流该等**"分开 ——
    前者是终态,后者才值得重试。
    """
    return {"status": "skipped" if _is_dead_link(message) else "failed",
            "message": message, "our_url": "", "code": ""}


def _alert_auth_expired(session, user_id: int, kind: str, message: str) -> None:
    """登录态失效 → 推**管理员群**(运维信息)。

    ⚠️ **为什么必须报**(2026-10-04 实测):公开发现链一轮 15 条失败里,**10 条**都是
    `转存失败(errno=-6 账户已过期,重新登陆)` —— 也就是**一批本来能搬的资源全卡在
    "没人去重粘百度 Cookie"** 上,而旧实现只记一条泛泛的 `failed`:
    **运行记录看不出该干什么,也没有任何告警**(很容易被当成"这些链又失效了"而忽略)。
    公众号链早就有这个告警(`_enrich.py` 的「百度网盘 Cookie 已失效,转存停用」),这条链漏了。
    """
    try:
        from app.services.alert_service import notify_incident

        notify_incident(session, user_id, "pan_discovery",
                        f"{kind} 登录态失效(公开发现链转存停用)",
                        f"{message[:120]} —— 去「Cookie 管理」重粘 {kind} Cookie 即可;"
                        f"已发现的盘链会留 pending,恢复后自动重试。")
    except Exception:  # noqa: BLE001 - 告警自身失败不该影响转存结果
        logger.exception("登录态失效告警推送失败")


def _backlog_breakdown(session, user_id: int) -> dict:
    """积压的**构成**:排队等搬 vs 已失败,以及失败原因的前几名。

    ⚠️ **为什么必须拆开**(2026-10-17 实测):`_alert_backlog` 原来把 139 条一律说成
    「多半是**额度用完**」并建议调大 `PAN_DISCOVERY_TRANSFER_LIMIT` —— 而真去查库,
    **失败 138 / 排队 4**,前两类原因清一色是
      · `转存失败(errno=2 转存路径不存在)` 58 条(百度盘上那个目标目录**不在了**)
      · `提取码验证失败(errno=105)` 50 条(分享本身的问题)
    ⇒ **一条都不是额度问题**。那条告警把人指向"调额度"这个**没用的旋钮**,
    而真正该做的是**在百度盘上把目录建回来**(代码里没有建目录的能力)。
    这正是本仓那条纪律:**给错动作比不给动作更坏** —— 照着做完没用,下次就没人看告警了。
    """
    from app.db.models import DiscoveredPanLink

    counts = {s: int(c or 0) for s, c in session.execute(
        select(DiscoveredPanLink.status, func.count()).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.status.in_(("pending", "failed")),
        ).group_by(DiscoveredPanLink.status)).all()}
    top = [(str(m or "")[:44], int(c or 0)) for m, c in session.execute(
        select(func.substr(DiscoveredPanLink.message, 1, 44), func.count()).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.status == "failed",
        ).group_by(func.substr(DiscoveredPanLink.message, 1, 44))
        .order_by(func.count().desc()).limit(3)).all()]
    #: ★ **盘满要单独数,不能只看 `failed`**(2026-10-25 加)。盘满的候选会被记成
    #: `pending`(链是好的,清了盘就能搬),于是它们**从 `failed` 里消失了** ⇒
    #: 告警会掉进「排队 ≥ 失败 ⇒ 多半是额度用完」那一支,又给一次**错动作**。
    space_blocked = int(session.scalar(
        select(func.count()).select_from(DiscoveredPanLink).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.status.in_(("pending", "failed")),
            DiscoveredPanLink.message.like("%空间不足%"),
        )) or 0)
    return {"pending": counts.get("pending", 0), "failed": counts.get("failed", 0),
            "top": top, "space": space_blocked}


def _alert_backlog(session, user_id: int, backlog_left: int, out: dict) -> None:
    """积压超过阈值 → 推**管理员群**,并且**按证据给出该做的那件事**。

    ⚠️ **为什么必须报**(2026-10-05 生产实测):一轮 **候选 44 条、只转存 3 条**,
    库里堆了 **41 条 pending**,理由清一色「本轮转存额度用完」——**它们是健康的、能搬的链**,
    纯粹被额度卡住;按当时 3 条/天要**两周**才清得完。而**运行记录一路 `success`**,
    没有任何地方提到"还剩 41 条在排队" ⇒ **额度不足可以无声无息地持续几周**。
    这与本仓反复踩的「静默失败 = 假成功」完全同源:系统说自己好,实际上在堆积。
    """
    try:
        from app.services.alert_service import notify_incident

        bk = _backlog_breakdown(session, user_id)
        why = " · ".join(f"{m}({c} 条)" for m, c in bk["top"] if m)
        if not why and bk.get("space"):
            #: 盘满的候选都挂在 `pending` 里 ⇒ `failed` 可能一条都没有,
            #: 别让告警在此处退化成「(失败原因未记)」。
            why = f"百度盘空间不足({bk['space']} 条,已挂起等空间)"
        why = why or "(失败原因未记)"
        #: ★ **动作按证据给** —— 三种构成对应三件完全不同的事,别一律甩"调额度"
        if bk.get("space") or "空间不足" in why or "超额" in why or "-32" in why:
            #: ★ 2026-10-25 加。原来这两类**落到最后的"链本身的问题"**那一支 ——
            #: 「给错动作比不给动作更坏」:让人去查链,而真相是**盘超额 43 倍**。
            _n = f"(其中 {bk['space']} 条已按「盘满」挂起等空间)" if bk.get("space") else ""
            act = ("⇒ **落点是盘满,不是链坏了**:这批候选本身是好的,只是百度盘"
                   "**已用远超配额**(2026-10-25 实测 5.07 TiB / 120 GiB)⇒ 清理百度盘,"
                   "或在「Cookie 管理」加一个还有空间的号;**空间一出来它们会自动重试**"
                   f"{_n}。"
                   "⚠️ 代码已改为**先探配额再决定要不要逐条转存**,所以不会再拿同一批去空撞。")
        elif "转存路径不存在" in why or "建不出来" in why:
            act = ("⇒ **百度盘上那个转存目录不在**(`/redian百度转存`)。"
                   "⚠️ 2026-10-17 起**代码会自己补建**(转存撞到这个错就建一次再重试);"
                   "**若连续两轮仍报同一句**,说明这个账号**建不了目录**(权限/只读)⇒ "
                   "请人工在网页版新建。这批候选本身是好的,只是没有落点。")
        elif bk["pending"] >= bk["failed"]:
            act = ("⇒ 多半是「**额度用完**」而非链失效:可调大 `PAN_DISCOVERY_TRANSFER_LIMIT`"
                   "(积压额度)或 `PAN_DISCOVERY_FRESH_LIMIT`(新发现额度)加快清空。")
        else:
            act = ("⇒ **额度不是瓶颈**(失败比排队多):这些是**链本身**的问题,"
                   "调额度只会让同一批死链多跑几遍 —— 先看上面的失败原因。")
        #: ⚠️ `ok` **含复用** ⇒ 必须剔掉才配叫「成功」(见 `pan_discovery_tick` 同处);
        #:   复用为 0 时**不写**那半句(免得每次多一坨 "另复用 0 条")。
        _real_ok = max(0, int(out.get("ok", 0) or 0) - int(out.get("reused", 0) or 0))
        _reuse = int(out.get("reused", 0) or 0)
        _ok_txt = f"**成功 {_real_ok} 条**" + (f"(另复用 {_reuse} 条)" if _reuse else "")
        #: ★★ **标题里不许出现数字**(2026-11-23 修)。`notify_incident` 的冷却键是
        #: `title[:80]`,而这里原先把积压条数写进标题 ⇒ **数字一变就是新键**,
        #: 冷却门 100% 失效 —— 实测 `incident_pan_discovery` **106 条告警 / 106 个不同标题**,
        #: 每 4 小时推一次、连续 8 天,内容只是 327→319→302 这种抖动。
        #: 同仓 `xunlei_group` / `disk_guard` 的标题**故意不含数字**(注释写着同一条纪律),
        #: 这里漏了。**具体条数全部挪到 detail** —— 人要看数就在正文里看。
        notify_incident(session, user_id, "pan_discovery",
                        "网盘发现积压(超过阈值)",
                        #: ⚠️ 「失败 X / 排队 Y」这个写法被既有用例与历史告警沿用,
                        #: 别为了好看改成「·」分隔(改过一次,`test_目录不在就叫人去建目录` 红)
                        f"仍积压 **{backlog_left}** 条(失败 {bk['failed']} / 排队 {bk['pending']})。"
                        f"本轮候选 {out.get('found', 0)} 条、{_ok_txt}。"
                        f"**失败原因**:{why}。{act}")
    except Exception:  # noqa: BLE001 - 告警自身失败不该影响本轮结果
        logger.exception("积压告警推送失败")


def _resource_name_hints(title: str) -> list[str]:
    """从一条候选的标题里挖出**可能的资源名**(用来回查我们库里有没有)。

    三路取词,按精度从高到低:
      ① 《…》/【…】 实体(引流号把资源名标出来,最准);
      ② 去掉话题标签后的**连续中文片段**(4~12 字);
      ③ 兜底:整条标题(短的时候)。
    """
    t = str(title or "")
    hints: list[str] = []
    try:
        #: ⚠️⚠️ **必须走门面**(2026-10-17 实测):直接 `from app.services.wechat._candidates import …`
        #: 在"**先 import 了 pan_discovery**"的进程里会**循环导入失败**,而下面那个
        #: `except` 一吞 ⇒ **最准的《…》实体名静默消失**,只剩碎片档。
        #: 实测后果:候选标题 `《JOJO的奇妙冒险：飙马野郎》…` 只提出「的奇妙冒险」「飙马野郎」,
        #: 而库里明明有同一资源 ⇒ `already_have` 判「没有」⇒ **又搬了一份**(用户报的"很多重复")。
        from app.services import wechat_monitor as _wm

        hints += _wm.mine_title_entities([t], top=3)
    except Exception:  # noqa: BLE001 - 抽不出来不影响主流程,但**不许静默**
        logger.warning("资源名提取:书名号实体那一档取不到(本轮只剩碎片档)", exc_info=True)
    # ② 连续中文片段(先去掉 #话题# 与链接,免得把推广词当资源名)
    body = re.sub(r"#\s*[^#]{1,30}\s*#", " ", t)
    body = re.sub(r"https?://\S+", " ", body)
    #: ★ **先把"很短的括号组"剥掉再切**:`伪装[心] 直男` 会被切成「伪装」「直男」两个
    #: 2 字段(都过不了 3 字下限)⇒ 那个资源**一个候选名都提不出来**(2026-10-17 实测)。
    #: 只剥**内容 ≤2 字**的组(`[心]` / `[1]`);长组留给实体提取那一档(《…》/【…】)。
    body = _SHORT_BRACKET_RE.sub("", body)
    for seg in re.findall(r"[一-鿿]{4,12}", body):
        if seg[0] in _PARTICLE_HEADS:
            #: ★ **残片**(2026-10-17):正则在**拉丁字母/数字/标点**处会切断,
            #: 于是 `JOJO的奇妙冒险` 被切成 `的奇妙冒险`。这类以虚词开头的片段
            #: **不可能是资源名**(没有哪个资源叫「的奇妙冒险」),留着只会
            #: ① 白查询;② 万一同上名就张冠李戴。⇒ 直接丢。
            continue
        if any(p in seg for p in _SHARE_PHRASES):
            continue          #: 分享话术(「我用夸克网盘给你分享了」)—— 不是资源名
        hints.append(seg)
    out: list[str] = []
    for h in hints:                       # 保序去重 + 去掉过短的
        h = h.strip()
        if len(h) >= 3 and h not in out:
            out.append(h)
    return out[:5]


#: **很短的括号组**(内容 ≤2 字):`[心]` / `(1)` / `【更】` —— 切词前先剥掉,
#: 否则 `伪装[心]直男` 会被切成两个 2 字段,那个资源**一个候选名都提不出来**。
_SHORT_BRACKET_RE = re.compile(r"[（(\[【《「{][^）)\]】》」}]{1,2}[）)\]】》」}]")

#: 以这些字开头的 CJK 片段**不可能是资源名**(虚词/连词/副词 —— 它们是正则从
#: 拉丁字母/标点处切断后留下的**残片**)。
#: ⚠️ **故意不收 `我/你/他/不/有/这/那`**:`我的人间烟火`、`不完美的她`、`有翡`
#: 都是**真资源名** ⇒ 宁可漏挡,也不要把真名字一起挡掉("给错拒绝比不拒绝更坏")。
_PARTICLE_HEADS = ("的", "了", "是", "在", "和", "与", "及", "把", "被", "让", "就",
                   "都", "也", "而", "但", "又", "还", "再", "很", "太", "更", "最")

#: 分享**话术**片段:整段是"我怎么把链接给你"的句子,不是资源名。
_SHARE_PHRASES = ("我给你分享", "给你分享", "网盘给你", "我用夸克", "我用百度",
                  "我用迅雷", "分享的文件", "复制这段", "复制链接")


#: **通用短语**:这类词在标题里满地都是,拿它回库匹配**必然张冠李戴**。
#:
#: ★ 这条是 **2026-10-14 一场真实事故**换来的:一条《人生大事》的贴吧帖被配上
#:   「高性价比人生指南」的 PDF 链,而它不过是用了「无删减完整版」「百度网盘资源免费下载安装」
#:   这种词去回查库 —— **而库里恰恰也存着这些贴吧帖本身**(发现链会入库),
#:   于是把它们那条**已经写错的** `our_url` 又抄了一遍:**越滚越错**。
#:   实测:`discovered_pan_links` 里 428 条复用行,**201 条最终指向同一份 PDF**。
#: ⚠️ 与 `douyin_leads._GENERIC_WORDS` 是同一族的护栏(那边也有一次"通用短语配出不相干链")。
_GENERIC_HINTS = (
    "网盘", "百度", "夸克", "迅雷", "UC", "蓝奏", "完整版", "无删减", "全集", "免费",
    "下载", "链接", "资源", "分享", "观看", "高清", "4K", "电影", "短剧", "电视剧",
    "动漫", "漫剧", "教程", "安装", "在线", "手机", "电脑", "入口", "获取", "自取",
    "整理", "合集", "最新", "更新", "速存", "保存", "收藏", "推荐", "免费看", "抢先",
)


def _specific_hint(hint: str) -> bool:
    """这个候选名**够不够专指**?—— 沾通用词的一律不算(见 `_GENERIC_HINTS`)。"""
    h = str(hint or "").strip()
    return len(h) >= 3 and not any(w in h for w in _GENERIC_HINTS)


def _title_claims(first: str, name: str) -> bool:
    """库里的那条标题**到底说的是不是这个资源**(判据见 `resource_library.title_claims`)。

    ★★ **2026-10-15 收敛成单一出处**:实现搬去了 `resource_library.title_claims`,
    这里只转发。原因:当天抖音卡片那个 bug,根子就是**这道护栏只加在了 `pan_discovery`、
    没同步给 `douyin_leads`** —— 于是抖音那边一直是**裸包含**,
    实测线索标题里的语气词 `啊啊啊` 命中了《镜外 Do or die》。
    ⇒ **同一套纪律写两处,迟早一处先修、另一处照旧**(本仓母题)。

    ⚠️ 这道护栏**只能防新错配**,防不住"已经写错的 `our_url` 被抄下去" ——
    那部分要清(见 `scripts/cleanup_reuse_pollution.py`)。
    """
    from app.services.resource_library import title_claims

    return title_claims(first, name)


def already_have(session, user_id: int, title: str) -> dict | None:
    """这个资源我们**已经有我方链**了吗(在**任意一个网盘**上)。

    用户口径(2026-10-05):「**三个网盘之间能否做到互通来缓解单个网盘内存的压力** ——
    如果同一资源单个网盘已经有了,就不需要多个网盘进行转存了,只需要**从已有的里面推这个资源**就行」。

    ⚠️ **匹配只能按名字,不能按链接**:同一份资源在夸克和百度上是**不同的 share_id**,
    按链接去重挡不住"同一资源被搬到第二个盘"——而那正是要避免的浪费。

    ⚠️ **宁可漏判不要误判**:匹配不上就返回 `None`(照常转存)。
    误判的代价是"把 A 资源的链当成 B 资源推出去",那是**内容事故**;
    漏判的代价只是"多搬了一份",是**空间浪费**。两者不是一个量级。
    """
    try:
        from app.services.resource_library import search_resources
    except Exception:  # noqa: BLE001
        return None
    for name in _resource_name_hints(title):
        try:
            rows = search_resources(session, user_id, name, limit=3)
        except Exception:  # noqa: BLE001 - 查库失败就当没有,别挡转存
            continue
        for r in rows:
            if not r.get("my_link"):
                continue
            first = str((r.get("titles") or [""])[0] or "")
            if not _title_claims(first, name):
                continue
            # ★ **护栏只有这一道,而且它够用**(2026-10-14 实测):
            #   通用短语(「无删减完整版」)之所以能张冠李戴,是因为原来只做**包含**判断;
            #   而 `_title_claims` 要求"以它开头 或 标题里《它》点名" ⇒ 那句话在
            #   《冥界司机》的标题里既不在开头、也没被点名 ⇒ 直接落空。
            # ⚠️ **不要再叠加"通用词黑名单"**:我试过,它会把 `完整版资源合集` 这种
            #   **正当匹配**一起挡掉(`match_key` 会把 `【…】` 连内容剥掉,那类名字只能靠包含匹配)
            #   ⇒ 实测让两条既有用例变红。判据越少越不容易误伤。
            return r
    #: ★★ **近似名兜底**(2026-10-17 用户口径:「有些资源是同一个资源,但名字可能有繁体字
    #: 或者多音字写的;很多可能是标题字数上就个别不一样但是是同一个资源」)。
    #: 上面按**精确命中**找完了还没有 ⇒ 再用宽松判据(`same_resource`)扫一遍库 ——
    #: 没有这一层,同一份资源换个写法就会被**再搬一份**。
    #: ⚠️ 它只用于"**要不要再搬**";给客户出链仍走 `_title_claims` 那套严格判据。
    #: ⚠️ 兜底本身失败就当没有(照常转存)—— 它是省一次转存的旁路,不许变成新故障源。
    try:
        from app.services.resource_library import fuzzy_have

        #: ⚠️ **只把"专指名"交给近似名兜底**(`_specific_hint`):宽松判据没有
        #: `_title_claims` 那道护栏,拿通用短语(「无删减完整版」)去比会把
        #: **完全不相干**的资源认成已有 —— 实测让 `test_通用短语不再把不相干的库链认成已有` 打红。
        _names = [n for n in _resource_name_hints(title) if _specific_hint(n)]
        return fuzzy_have(session, user_id, _names) if _names else None
    except Exception:  # noqa: BLE001
        logger.info("资源库近似名兜底失败(忽略,照常转存)", exc_info=True)
        return None


def strict_have(session, user_id: int, title: str) -> dict | None:
    """**只要精确命中**的「库内已有我方链」——给**客户出链**用,不含近似名兜底。

    ## 为什么单列,不复用 `already_have`
    `already_have` 末尾有一段**近似名兜底**(`fuzzy_have`),它的用途是"**要不要再搬一份**";
    那段自己的注释就写着:「它只用于'要不要再搬';给客户出链仍走 `_title_claims` 那套严格判据」。
    卡片上的标题链接是**给客户点开拿资源**的 ⇒ 必须走严格判据,
    否则近似名可能把 A 资源的链挂到 B 资源上(内容事故,见 2026-10-16 那次错链扩散)。

    ## 它解决什么(2026-10-25 用户报)
    推文卡片上大量 `⏳待转存`,但那些资源**我们库里早就有了**(`pan_owned_shares` 5367 条)。
    旧渲染只看文章自己的 `my_pan_urls`,不从库里按名称反查 ⇒ 客户以为还得自己搬,
    实际点开原文拿到的还是别人的链。
    """
    try:
        from app.services.resource_library import search_resources
    except Exception:  # noqa: BLE001
        return None
    for name in _resource_name_hints(title):
        try:
            rows = search_resources(session, user_id, name, limit=3)
        except Exception:  # noqa: BLE001 - 查库失败就当没有,别挡渲染
            continue
        for r in rows:
            if not r.get("my_link"):
                continue
            first = str((r.get("titles") or [""])[0] or "")
            if not _title_claims(first, name):
                continue
            return r
    return None


def reuse_if_have(session, user_id: int, title: str) -> dict | None:
    """★ **三盘互通**的统一入口:**这个资源我们盘里已经有了吗?有就把那条链给你,别再搬一份。**

    用户口径(2026-10-05):「**三个网盘之间能否做到互通来缓解单个网盘内存的压力** ——
    如果同一资源单个网盘已经有了,就不需要多个网盘进行转存,只需要从已有的里面推这个资源」。

    ⚠️ **为什么要有这个门面**(2026-10-06):`already_have` 原来**只有 `pan_discovery.sync`
    一条链在用**,而别的转存点(夸克口令 / 迅雷群 / 抖音口令 / 选题 Agent 的自动转存)
    **一律盲转** —— 同一个资源在三个盘里各存一份。用户口径是「**所有资源都走三盘互通**」,
    所以给它一个**统一入口**:新链路接进来只需一行,不必各自复刻匹配逻辑。

    返回 `{"my_link","pan_url","titles","source",...}`(命中,链可直接复用)或 `None`(没有,照常搬)。
    """
    try:
        return already_have(session, user_id, title)
    except Exception:  # noqa: BLE001 - 查库失败就当没有,别挡转存
        logger.exception("三盘互通查询失败 title=%s", str(title)[:40])
        return None


def transfer_pan_url(session, user_id: int, pan_url: str, settings=None,
                     snippet: str = "", title: str = "") -> dict:
    """**按链分发转存** + ★ **转存后立刻核对"里面装的是不是这个资源"**。

    `title` 给了就核对(多一次只读请求);**对不上 ⇒ 不写链**(返回 `status="mismatch"`)。

    ⚠️⚠️ **为什么加这道核对**(2026-10-16 实测的一次真事故):别人的**原链内容是对的**
    (`pan.quark.cn/s/602ff5fc5865` 里就是「三千年等一口将军气（更至77集）」),
    而**我们转存后新建的分享里却是另一个资源(中医古籍)**。那条链被当 `our_url` 用了 **69 次**,
    还经「回库复用」扩散到 **68 个不同标题** ⇒ **客户点开拿到完全不相干的东西**。
    ⇒ 不管上游是哪条子路径,这道核对都让它到不了库、更到不了卡。

    ⚠️ **判不了就放行**(限流/超时)并只在日志留痕 —— 别拿"我们探不动"当"资源有问题"。
    """
    out = _transfer_pan_url_raw(session, user_id, pan_url, settings=settings, snippet=snippet)
    if out.get("status") == "ok" and str(title or "").strip() and out.get("our_url"):
        from app.services import link_audit as _la

        ok, why = _la.check_share_matches(str(out["our_url"]), title, settings)
        if ok is False:
            logger.warning("网盘发现:转存出来的内容**与标题对不上** ⇒ 不写链(title=%s / %s)",
                           str(title)[:40], why[:80])
            return {"status": "mismatch", "our_url": "", "code": "",
                    "message": (f"**转存出来的内容与标题对不上**(实际:{why[:60]})⇒ 不写链"
                                f"(见 doc/链接体检.md 里 2026-10-16 那次错链扩散)")}
        if ok is None:
            logger.info("网盘发现:转存后核对**判不了**(%s),按原样放行", why[:80])
    return out


#: 百度盘的**配额探针缓存**。`/api/quota` 一次就够 —— 但转存是**逐条**调用的,
#: 40 条百度候选各探一次 = 40 次多余请求(与迅雷那边「配额探针必须限流」同一条纪律)。
#: ★ **按 Cookie 分键**(本仓 2026-10-16 在迅雷那条链上吃过"探 1 号的结果被当成 2 号"的亏)。
_BAIDU_QUOTA_TTL = 60.0
_baidu_quota_cache: dict[str, dict] = {}

#: 夸克配额缓存(2026-11-11)。比百度长得多 —— 夸克这边**每条链**都要先判一次满不满,
#: 不缓存等于每条候选多打两个请求;而 5 分钟内的变化(盘从 99% 到 100%)由
#: 转存失败的第二层兜底(`容量不足` ⇒ 换号)接住。
_QUARK_QUOTA_TTL = 300.0
_quark_quota_cache: dict[str, dict] = {}


def _size_text(n: int | float | None) -> str:
    """字节数 → 人能读的两档单位(只用于告警文案,不需要精确)。"""
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024:
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.2f} {unit}"
        v /= 1024
    return f"{v:.2f} TB"


def _fid_store_for(settings, ref, platform: str = "quark") -> str | None:
    """**按账号分文件**的目录 fid 缓存路径(`quark_fid_store` 的每账号副本)。

    ⚠️⚠️ **不能两个号共用一份**(2026-11-11 加多账号轮换时发现):fid 是**账号内**的标识,
    A 号的 `/redian监听` fid 拿到 B 号去用必然报错 ⇒ `_ensure_dir` 会 `invalidate_dir()`,
    而那个动作会往**共享的** fid_store 里写**墓碑**(`_dropped`),把 A 号正确的映射一起删掉。
    结果是两边互相作废、每轮都新建 `_MMDD` 目录 —— 正是"同一资源重复占空间"的根因。

    默认号(`cookie_key == 平台名`)沿用原路径,所以**单账号部署的行为逐字不变**。

    ★ 2026-11-13:具体拼法搬进 `pan_accounts.scoped_path` —— 夸克扫描快照
    (`quark_dup_cache`)踩的是同一个坑(两个号家名相同、fid 不同),两处共用一份实现。
    """
    from app.services import pan_accounts as _A

    base = str(getattr(settings, "quark_fid_store", "") or "")
    if not base:
        return None
    return _A.scoped_path(base, str(getattr(ref, "cookie_key", "") or ""), platform=platform)


def _baidu_quota(cookie: str) -> dict | None:
    """这份 Cookie 对应账号的 `{used,total,ratio}`(60 秒内复用);拿不到回 `None`。

    `ratio` **可能远大于 1** —— 2026-10-25 实测那个百度号 `used≈5.07 TiB / total≈120 GiB`
    (超额 43 倍)。**判"满"用 `used >= total`,不要用百分比直觉。**
    """
    import hashlib
    import time as _t

    key = hashlib.sha256((cookie or "").encode()).hexdigest()[:16]
    hit = _baidu_quota_cache.get(key)
    now = _t.time()
    if hit and now - float(hit.get("at") or 0) < _BAIDU_QUOTA_TTL:
        return hit.get("usage")
    from app.services.baidupan_transfer import BaiduPanClient

    try:
        usage = BaiduPanClient(cookie).quota_usage()
    except Exception:  # noqa: BLE001 - **探不到 ≠ 盘满了**,照旧去试那一条
        logger.debug("百度配额探针失败", exc_info=True)
        usage = None
    _baidu_quota_cache[key] = {"at": now, "usage": usage}
    return usage


def _quark_quota(cookie: str) -> dict | None:
    """这份 Cookie 对应夸克账号的 `{used,total,ratio}`(5 分钟内复用);拿不到回 `None`。

    ⚠️ 与 `_baidu_quota` 同一取舍:**探不到 ≠ 盘满了** ⇒ 返回 `None`,调用方照旧去试。
    """
    import hashlib
    import time as _t

    key = hashlib.sha256((cookie or "").encode()).hexdigest()[:16]
    hit = _quark_quota_cache.get(key)
    now = _t.time()
    if hit and now - float(hit.get("at") or 0) < _QUARK_QUOTA_TTL:
        return hit.get("usage")
    from app.services.quark_transfer import QuarkTransfer

    try:
        usage = QuarkTransfer(cookie).quota_usage()
    except Exception:  # noqa: BLE001 - 探针失败只说"不知道",照旧去试那一条
        logger.debug("夸克配额探针失败", exc_info=True)
        usage = None
    _quark_quota_cache[key] = {"at": now, "usage": usage}
    return usage


def _transfer_quark(session, user_id: int, pan_url: str, settings=None) -> dict:
    """夸克转存:**按池择一,满了换下一个**(2026-11-11)。

    ## 为什么要有这一段

    实测两个号:

    | 号 | 等级 | 已用 / 配额 | 比例 |
    |---|---|---|---|
    | `quark`(1 号) | SUPER_VIP | 106.01 / 86.00 TiB | **123%** |
    | `quark#2`(2 号) | NORMAL | 0.00 / 21.01 TiB | **0.02%** |

    但 `pan_discovery` 一直用的是 `cookie_for()` —— **只取第一个,不轮换**
    ⇒ 每条链都撞 1 号 ⇒ 近 7 天 **67 条**「夸克网盘容量不足」失败,积压 235 → 269。
    ⇒ 与迅雷群侧(`xunlei_group.py` 的择一循环)对齐:**先判满,满了换号,全满才停**。

    ## 单账号行为不变

    池里没配 ⇒ `cookies_for()` 只返回默认 key 那一枚 ⇒ 循环跑一次,与旧代码逐字等价
    (包括 `.env` 的 `quark_cookie` 兜底 与 缺凭据返回 `pending` 的措辞)。

    ## 两层判满

    1. **开撞前**:`_quark_quota()` 探一次(5 分钟缓存),`used >= total` 就换号 ——
       不消耗一次转存请求;
    2. **撞上后**:`transfer_and_share` 抛「容量不足」时也换号继续试(探针可能滞后)。
       这正是"空着的 2 号永远轮不到"的兜底。
    """
    from app.services import pan_accounts as _A
    from app.services.quark_transfer import QuarkAuthError, QuarkError, QuarkTransfer

    quota_limit = float(getattr(settings, "quark_quota_limit", 0.98) or 0.98)
    pool = _A.cookies_for(session, "quark", user_id=user_id)
    #: ⚠️ `cookies_for()` 返回的是 `(label, cookie)`,而 `report()` 要的是 `AccountRef`
    #: ⇒ 按 label 反查(池里 label 唯一;不是 `cookie_key` —— 那个是 `quark`/`quark#2`)。
    by_label = {}
    for r in _A.accounts(session, "quark", user_id=user_id):
        by_label.setdefault(r.label or r.cookie_key, r)
    if not pool:
        #: `.env` 兜底:池里一个凭据都取不到时,旧实现读的那一枚。
        ck = str(getattr(settings, "quark_cookie", "") or "").strip()
        if not ck:
            # ⚠️ **可重试**:配上 Cookie 就能搬 —— 标 pending 而不是终态,
            # 否则"发现过一次"就把这条资源永久钉死(2026-10-02 实测踩过)
            return {"status": "pending", "message": "未配夸克 Cookie(配上后会自动重试)",
                    "our_url": "", "code": ""}
        pool = [("quark(.env)", ck)]

    tried: list[str] = []
    full: list[str] = []
    auth_err: str = ""
    save_dir = getattr(settings, "quark_save_dir", "") or "/来自发现"
    password = getattr(settings, "quark_share_password", "") or ""
    intro_dir = getattr(settings, "pan_intro_quark_dir", "") or ""
    fid_base = getattr(settings, "quark_fid_store", "") or None

    for label, ck in pool:
        ref = by_label.get(label)
        usage = _quark_quota(ck)
        #: ⚠️ 判满用 `ratio >= limit`,**不用 `used >= total`** —— 转存是先落文件再判,
        #: 98% 的盘常常正好装不下一个几 GiB 的包(实测失败文案就是 capacity limit)。
        if usage and float(usage["ratio"]) >= quota_limit:
            full.append(f"{label} 已用 {usage['ratio'] * 100:.1f}%")
            if ref is not None:
                try:
                    _A.report(session, ref, ratio=float(usage["ratio"]), full=True,
                              why=f"已用 {usage['ratio'] * 100:.0f}%")
                except Exception:  # noqa: BLE001 - 写不回池只影响下次择号
                    logger.debug("夸克配额写回池失败", exc_info=True)
            continue
        if usage and ref is not None:
            try:
                _A.report(session, ref, ratio=float(usage["ratio"]), full=False)
            except Exception:  # noqa: BLE001
                logger.debug("夸克配额写回池失败", exc_info=True)
        #: ★ **fid 是账号内标识**:共享一个 fid_store 会让 A 号的「目录不存在」墓碑
        #: 把 B 号的正确映射一起删掉(`invalidate_dir`),两边互相作废、每轮新建 `_MMDD` 目录。
        #: ⇒ 非默认号用**自己的**缓存文件。
        store = _fid_store_for(settings, ref, "quark") if ref is not None else fid_base
        quark = QuarkTransfer(ck, fid_store=store)
        try:
            res = quark.transfer_and_share(
                pan_url, save_dir=save_dir, password=password, intro_dir=intro_dir)
        except QuarkAuthError as exc:
            #: 登录态失效是**账号级**的:换下一个号试,全都不行由调用方判 pending + 告警。
            auth_err = str(exc)[:200]
            tried.append(f"{label} 登录态失效")
            continue
        except QuarkError as exc:
            if "容量不足" in str(exc):
                #: 探针滞后(5 分钟缓存)或这一包本身太大废掉了余量 ⇒ 第二层兜底。
                full.append(f"{label}: {str(exc)[:60]}")
                if ref is not None:
                    try:
                        _A.report(session, ref, full=True, why=str(exc)[:60])
                    except Exception:  # noqa: BLE001
                        logger.debug("夸克配额写回池失败", exc_info=True)
                continue
            raise
        if res.get("share_url"):
            return {"status": "ok", "our_url": str(res.get("share_url")),
                    "code": str(res.get("password") or ""), "message": ""}   # ⚠️ 夸克返回的键是 password
        return _fail("转存未返回我方链")

    if auth_err and not full:
        #: 池里每个号都是登录态失效 ⇒ 交给调用方统一判 pending + 推管理员群。
        raise QuarkAuthError(auth_err)
    if full:
        #: 记 `pending`(不是终态):清了盘/加了号就会自动重试。
        #: ⚠️ `code` 留空 —— 它会原样写进 `pass_code` 那一列(见百度那段同样的注释)。
        return {"status": "pending", "our_url": "", "code": "",
                "message": ("夸克盘空间不足(" + ";".join(full) + ")"
                            "—— 本轮**未尝试**转存;清理或加号后会自动重试")}
    if tried:
        return {"status": "pending", "our_url": "", "code": "",
                "message": "夸克账号全部不可用(" + ";".join(tried) + ")"}
    return _fail("夸克池里没有可用账号")


def _transfer_pan_url_raw(session, user_id: int, pan_url: str, settings=None,
                          snippet: str = "") -> dict:
    """**按链分发转存**:夸克走 `QuarkTransfer`、百度走 `BaiduPanClient`、迅雷走 `xunlei_transfer`。

    返回 `{"status", "our_url", "code", "message"}`;任何失败都返回结构化结果,不抛。

    ⚠️ **百度链要提取码**:知乎回答里常把码写在附近,所以把整段文字传进来一起找。
    """
    kind = _kind_of(pan_url)
    if not kind:
        return {"status": "skipped", "message": f"不认识的盘链:{pan_url[:60]}",
                "our_url": "", "code": ""}
    try:
        if kind == "quark":
            return _transfer_quark(session, user_id, pan_url, settings)

        if kind == "baidu":
            from app.services.baidupan_transfer import BaiduPanClient, extract_pwd

            from app.services import pan_accounts as _A

            #: ★★ **百度转存按配置停用**(2026-11-24,用户拍板「暂时先放弃百度转存,等有
            #: 空间了再加回来」)。放在**取凭据之前**:连 cookie 都不必读,更不打配额探针
            #: —— 唯一那个百度号超额 43 倍,每轮几十条候选各探一次纯属白烧。
            #: ⚠️ 记 `pending`(不是 `skipped`):链是好的,配置一改回来就能搬。
            #: ⚠️ 措辞里**必须出现「空间不足」那组词之一** —— 上游靠 `_is_space_blocked`
            #: 判「跳过转存、直推源链」,这样客户**照样拿到这条链**(见 `sync` 那段)。
            #: 同时明写「按配置停用」,否则运维会去查一个根本没满的盘。
            if not bool(getattr(settings, "pan_transfer_baidu", True)):
                return {"status": "pending", "our_url": "", "code": "",
                        "message": ("百度转存已按配置停用(`PAN_TRANSFER_BAIDU=false`;"
                                    "该号空间不足)—— 本轮**未尝试**转存,"
                                    "源链照推;配置改回 true 后会自动重试")}

            ck = _A.cookie_for(session, "baidupan", user_id=user_id)
            if not ck:
                return {"status": "pending", "message": "未配百度网盘 Cookie(配上后会自动重试)",
                        "our_url": "", "code": ""}
            # ★★ **盘已经超额就别再一条条去撞**(2026-10-25 加)。
            # 实测:唯一那个百度号 `used≈5.07 TiB / total≈120 GiB`(超额 **43 倍**),
            # 积压里 40 条候选每轮都走完「验码 → 解析 → 转存」三次请求再吃一个
            # `errno=-32 剩余空间不足`。探一次配额就能省掉这 40 轮空转,
            # 而且能让失败原因**说出真正该做的事**(清理 / 换号),而不是"链本身的问题"。
            # ⚠️ 记 `pending` 而不是 `skipped`:链是**好的**,清了盘就能搬。
            _q = _baidu_quota(ck)
            if _q and _q["total"] > 0 and _q["used"] >= _q["total"]:
                #: ⚠️ `code` 留空 —— 它会**原样写进 `pass_code`**(那一列放的是分享提取码),
                #: 塞个 `space_exhausted` 进去会污染卡片上的"提取码"。判据只用 message 措辞。
                return {"status": "pending", "our_url": "", "code": "",
                        "message": (f"百度盘空间不足(已用 {_size_text(_q['used'])} / "
                                    f"配额 {_size_text(_q['total'])},超额 "
                                    f"{_q['ratio']:.0f} 倍)—— 本轮**未尝试**转存;"
                                    f"清理百度盘或换一个号后会自动重试")}
            pwd = extract_pwd(snippet or "", pan_url) or ""
            res = BaiduPanClient(ck).transfer_and_share(
                pan_url, password=pwd,
                intro_dir=getattr(settings, "pan_intro_baidu_dir", "") or "")
            if res.get("share_url"):
                return {"status": "ok", "our_url": str(res.get("share_url")),
                        "code": str(res.get("password") or ""), "message": ""}   # ⚠️ 百度返回的键也是 password
            return _fail("转存未返回我方链")

        from app.services import xunlei_transfer as xt

        res = xt.transfer_and_share(
            pan_url, settings=settings,
            intro_dir=getattr(settings, "pan_intro_xunlei_dir", "") or "")
        if res.get("status") == "ok":
            return {"status": "ok", "our_url": str(res.get("share_url") or ""),
                    "code": str(res.get("code") or ""), "message": ""}
        msg = str(res.get("message") or "")
        # 迅雷另有两类**终态**:分享者被封(`is_dead_share_error`)、或那是**我们自己发的**(`is_own_share_error`)
        if xt.is_dead_share_error(msg) or xt.is_own_share_error(msg) or _is_dead_link(msg):
            return {"status": "skipped", "message": msg, "our_url": "", "code": ""}
        return {"status": "failed", "message": msg, "our_url": "", "code": ""}
    except Exception as exc:  # noqa: BLE001 - 单链失败不该炸整轮
        from app.services.baidupan_transfer import BaiduPanAuthError
        from app.services.quark_transfer import QuarkAuthError

        if isinstance(exc, (BaiduPanAuthError, QuarkAuthError)):
            # **登录态失效 ≠ 链死了**:重粘 Cookie 就能搬,所以记 `pending`(可重试)而不是终态,
            # 并推一次管理员群 —— 否则会像 2026-10-04 那样,一批资源卡着却没人知道要去重新登录。
            kind = "百度网盘" if isinstance(exc, BaiduPanAuthError) else "夸克"
            logger.warning("盘链转存:%s 登录态失效(%s)", kind, str(exc)[:80])
            _alert_auth_expired(session, user_id, kind, str(exc))
            return {"status": "pending", "message": str(exc)[:200], "our_url": "", "code": ""}
        logger.warning("盘链转存失败 %s:%s", pan_url[:50], exc)
        return _fail(str(exc)[:200])


def _candidates_from_zhihu(ck: str, keywords: list[str], limit: int) -> list[dict]:
    """知乎:逐词搜(**有频控**,词间必须隔开)。全部词失败则抛 `SearchSourceError`。"""
    from app.services.cross_accounts import SearchSourceError, _search_zhihu

    out: list[dict] = []
    failed, last_err = 0, ""
    for i, kw in enumerate(keywords):
        if i:
            time.sleep(_REQ_GAP)
        try:
            rows = _search_zhihu(ck, kw, limit)
        except SearchSourceError as exc:
            # 单个词失败(限流/网络)不该丢掉其余词 —— 但**必须记下来**:全失败要报错,
            # 不能像以前那样返回空列表被下游当成"真的没有"(见 SearchSourceError 的说明)。
            failed += 1
            last_err = str(exc)
            logger.warning("网盘发现:知乎词「%s」失败(%s)", kw, exc)
            continue
        for r in rows:
            url = str(r.get("pan_link") or "").strip()
            if not url:
                continue
            out.append({"platform": "zhihu", "origin_url": url,
                        "title": _clean(r.get("snippet") or "")[:255] or kw[:60],
                        "author": str(r.get("name") or "")[:64],
                        "source_url": str(r.get("url") or "")[:500],
                        # ★ **互动数一路带到卡上**(2026-10-16 用户口径:5 要素里要有「对标数据」)
                        #   —— 知乎的搜索命中本来就有 `metrics`,只是**候选这里没接**。
                        "metrics": dict(r.get("metrics") or {}),
                        "publish_at": int(r.get("publish_at") or 0)})
    if keywords and failed == len(keywords):
        raise SearchSourceError(f"{len(keywords)} 个词全部搜索失败:{last_err}")
    return out


def _candidates_from_weibo(ck: str, keywords: list[str], limit: int) -> list[dict]:
    """微博:逐词搜(**有频控**,词间隔开)。全部词失败则抛 `SearchSourceError`。

    ⚠️ **它是目前质量最好的盘链源**(2026-10-05 实测):微博有一批"资源铺"账号
    (#小说资源铺# 等超话),帖子里直接挂盘链。实测「资源 合集」19 条里 5 条带链、
    去重 18 条(百度 14 / 夸克 3 / 迅雷 1),而且**是真正在分享资源** ——
    不像知乎/掘金那样只是"技术文章里顺手一提"。
    """
    from app.services.cross_accounts import SearchSourceError, _search_weibo

    out: list[dict] = []
    failed, last_err = 0, ""
    for i, kw in enumerate(keywords):
        if i:
            time.sleep(_REQ_GAP)
        try:
            rows = _search_weibo(ck, kw, limit)
        except SearchSourceError as exc:
            failed += 1
            last_err = str(exc)
            logger.warning("网盘发现:微博词「%s」失败(%s)", kw, exc)
            continue
        for r in rows:
            # ⚠️ **一条微博的所有链都要收**(2026-10-05):实测有的帖子同时挂百度 + 夸克,
            # 只取 `pan_link`(第一条)会把后面的漏掉。`pan_links` 缺失时回落到单值(兼容旧形状)。
            urls = r.get("pan_links") or ([r["pan_link"]] if r.get("pan_link") else [])
            for url in urls:
                url = str(url or "").strip()
                if not url:
                    continue
                out.append({"platform": "weibo", "origin_url": url,
                            "title": _clean(r.get("snippet") or "")[:255] or kw[:60],
                            "author": str(r.get("name") or "")[:64],
                            "source_url": str(r.get("url") or "")[:500],
                            "publish_at": int(r.get("publish_at") or 0),
                            "metrics": r.get("metrics") or {}})
    if keywords and failed == len(keywords):
        raise SearchSourceError(f"{len(keywords)} 个词全部搜索失败:{last_err}")
    return out


def _tieba_rows(keywords: list[str]) -> tuple[list[dict], str]:
    """取贴吧搜索结果 → `(记录, 来源标签)`。**纯协议优先**,失败回落浏览器。

    ⚠️ **回落不会比之前更差**:2026-10-08 之前**每一次**都开浏览器。
    ⚠️ 回落要留痕:两条路都可能产出 0 条链,不标来源就分不清"协议挂了"和"真没货"。
    """
    from app.services import tieba_search
    from app.services.mediacrawler_source import crawl

    from config.settings import get_settings

    if not getattr(get_settings(), "tieba_prefer_protocol", True):
        return crawl("tieba", keywords), "浏览器(开关关掉)"
    try:
        return tieba_search.search(keywords), "协议"
    except tieba_search.TiebaSearchError as exc:
        logger.warning("贴吧纯协议失败(%s),回落浏览器:%s", exc.kind, str(exc)[:140])
    return crawl("tieba", keywords), "浏览器"


def _candidates_from_tieba(keywords: list[str]) -> list[dict]:
    """贴吧:盘链发现的**主源**(实测产出是知乎的 3 倍)。

    ⚠️ **实测产出**(2026-10-03 同口径对照):贴吧 16 条 → **9 条带盘链(56%)**,
    且**全是百度网盘**、内容集中在影视剧集(对口赛道);而知乎加了限定词后也才 17.8%。

    ★ **2026-10-08 改走纯协议**(A/B 实跑,同一个词「网盘资源」):

    | | 纯协议(aiotieba) | 浏览器(MediaCrawler) |
    |---|---|---|
    | **带盘链** | **19 条** | 16 条 |
    | 耗时 | **12 秒** | 60 秒 |

    ⇒ 盘链更多、快 5 倍,而且**不再需要登录档案**(匿名即可)。
    ⚠️ **纯协议那条的盘链不在搜索返回里** —— 搜索给的是**截断的摘要**,
    必须再取一次**首楼全文**才抽得出链(见 `tieba_search` 的 docstring)。
    """
    from app.services.tieba_metrics import agree_to_metrics, fetch_agree

    rows, source = _tieba_rows(keywords)
    # **补点赞数**:**只有浏览器那条需要补** ——
    # 协议路在"取首楼全文"那一次请求里已经把 `agree` 顺带拿回来了(见 tieba_metrics),
    # 再调一次 `fetch_agree` 等于把每个帖子又问一遍。
    agrees = (fetch_agree([t for t in (_tid_of(r.get("url")) for r in rows) if t])
              if source == "浏览器" else {})

    out: list[dict] = []
    for r in rows:
        url = str(r.get("pan_link") or "").strip()
        if not url:
            continue
        tid = _tid_of(r.get("url"))
        metrics = dict(r.get("metrics") or {})
        metrics.update(agree_to_metrics(agrees.get(tid)))
        out.append({"platform": "tieba", "origin_url": url,
                    "title": _clean(r.get("snippet") or "")[:255],
                    "author": str(r.get("name") or "")[:64],
                    "source_url": str(r.get("url") or "")[:500],
                    # ★ 协议路多给的:发帖时间(浏览器那条的贴吧记录里没有)
                    "publish_at": int(r.get("publish_at") or 0),
                    # 曝光/互动指标 —— 交给 `conversion` 算预估拉新
                    "metrics": metrics})
    logger.info("贴吧候选:%s → %d 条,带盘链 %d 条", source or "-", len(rows), len(out))
    return out


_TID_RE = re.compile(r"/p/(\d+)")


def _tid_of(url: object) -> str:
    """从贴吧帖子 URL 抽 tid(`https://tieba.baidu.com/p/11071928513`)。抽不到返回空串。"""
    m = _TID_RE.search(str(url or ""))
    return m.group(1) if m else ""


def find_candidates(session, user_id: int, keywords: list[str], limit: int = 20,
                    settings=None) -> list[dict]:
    """按资源词搜**公开平台** → 挑出**带直接盘链**的内容,按 `origin_url` 去重。

    两个源(谁没配谁跳过),**一个源失败不影响另一个**:
      · **贴吧**(MediaCrawler,主源,实测命中 56%);
      · **知乎**(带 Cookie 直搜,命中 17.8%)。
    只有**全部都失败**才抛 `SearchSourceError` —— 否则"全挂了"会被下游当成"今天真没资源"。

    返回 `[{platform, origin_url, title, author, source_url}]`。
    """
    from config.settings import get_settings

    from app.services.cookie_store import get_cookie
    from app.services.cross_accounts import SearchSourceError
    from app.services.mediacrawler_source import MediaCrawlerError

    settings = settings or get_settings()
    found: dict[str, dict] = {}
    attempted = failures = 0
    last_err = ""

    ck = (get_cookie(session, user_id, "zhihu") or "").strip()
    if ck:
        attempted += 1
        try:
            for c in _candidates_from_zhihu(ck, keywords, limit):
                found.setdefault(c["origin_url"], c)
        except SearchSourceError as exc:
            failures += 1
            last_err = str(exc)
    else:
        logger.info("网盘发现:未配知乎 Cookie,跳过知乎源")

    # **微博**(2026-10-05 新增):实测是目前质量最好的盘链源 —— 微博有"资源铺"账号
    # (#小说资源铺# 等超话),帖子里直接挂盘链。放在**贴吧之后**(贴吧 56% 命中、它是主源),
    # 用它补贴吧没覆盖到的品类(小说/资料类尤其多)。
    wb_ck = (get_cookie(session, user_id, "weibo") or "").strip()
    if wb_ck and getattr(settings, "pan_discovery_weibo", True):
        attempted += 1
        try:
            for c in _candidates_from_weibo(wb_ck, keywords, limit):
                found.setdefault(c["origin_url"], c)
        except SearchSourceError as exc:
            failures += 1
            last_err = str(exc)
    elif not wb_ck:
        logger.info("网盘发现:未配微博 Cookie,跳过微博源")

    if getattr(settings, "pan_discovery_tieba", True):
        attempted += 1
        try:
            for c in _candidates_from_tieba(keywords):
                found.setdefault(c["origin_url"], c)
        except MediaCrawlerError as exc:
            failures += 1
            last_err = str(exc)
            logger.warning("网盘发现:贴吧源失败(%s)", str(exc)[:120])

    if attempted and failures == attempted:
        raise SearchSourceError(f"{attempted} 个源全部失败:{last_err}")
    if not attempted:
        logger.info("网盘发现跳过:两个源都没启用/都没配")
    cands = list(found.values())
    # **按发布时间过滤**(2026-10-06,与抖音/B站**同一条口径**)。
    # ⚠️ 拿不到时间的也跳过,但**必须报数** —— 某个源若永远给不出时间,
    # 它会这样**静默归零**(整条链 0 产出),那正是本仓最忌讳的,所以要吵。
    from app.services.douyin_leads import _min_publish_ts

    min_ts = _min_publish_ts(settings or get_settings())
    if min_ts:
        kept, n_old, n_notime = [], 0, 0
        for c in cands:
            pv = int(c.get("publish_at") or 0)
            if pv <= 0:
                n_notime += 1
            elif pv < min_ts:
                n_old += 1
            else:
                kept.append(c)
        if n_old or n_notime:
            logger.info("公开平台发现:按发布时间过滤掉 %d 条(早于下限)+ %d 条(没有发布时间)",
                        n_old, n_notime)
        cands = kept
    return cands


def _search_words(session, user_id: int, top: int, settings) -> list[str]:
    """知乎搜索词 = **资料词表**(优先) + 「动态词 + 限定后缀」。

    ⚠️ **为什么不能直接拿 `douyin_leads.search_keywords` 的结果去搜**(2026-10-03 实测):
    那套词的用途是"在抖音热点里找《口令》",给的是**剧名/热点名**;拿到知乎上,
    48 条结果里**一条盘链都没有**(鞠婧祎/苏超/兰香如故 全 0)。原因是知乎的盘链回答
    集中在**资料/合集**类问题,裸剧名搜到的全是剧情讨论。

    同一批词**补上限定词**后立刻有产出(同账号同时段对照):
    `兰香如故 全集 网盘`→1、`教程 资料 网盘`→2、`四级真题 网盘`→4、`PS教程 全套 网盘`→6 ——
    命中率 **0% → 17.8%**。所以这里统一补后缀;`pan_discovery_terms` 里还能再放
    自己赛道的资料词(**优先于**动态词)。

    `top` 是**上限不是目标**:逐词要隔 `_REQ_GAP` 秒,且知乎有频控(实测连打 20+ 次会 403)。
    """
    from app.services.douyin_leads import search_keywords

    suffix = str(getattr(settings, "pan_discovery_suffix", "") or "")
    terms = [t.strip() for t in str(getattr(settings, "pan_discovery_terms", "") or "").split(",")
             if t.strip()]
    out = list(terms)
    for w in search_keywords(session, user_id, top, settings):
        q = f"{w}{suffix}".strip()
        if q and q not in out:
            out.append(q)
    return out[:top]


def _too_old(published, settings=None) -> str:
    """这条候选**太老了吗**?太老返回一句原因(给 `runs` 看),否则返回空串。

    ★ 用户口径(2026-10-16):「**资源不在多,在新在精**」。
    判据用**原帖发布时间**(不是 `found_at` —— 那只是"我们发现的时刻",老帖永远显示"刚刚";
    见 memory「热榜要区分抓到的时刻与内容发布的时刻」)。

    ⚠️ **判不了就放行**:不少源不给时间 ⇒ 拿"不知道"当"老"会把整条链堵死。
    ⚠️ 期限 `0` = **关掉这条闸门**(默认如此 —— 换算法前先看清数据)。
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    days = int(getattr(settings, "pan_discovery_max_age_days", 0) or 0)
    if days <= 0:
        return ""
    from app.services.hot_time import parse_published

    #: ⚠️⚠️ **`datetime` 要单独认**(2026-10-16 被自己的测试当场抓到):
    #: `parse_published` 只认 **int/float/str**,对 `datetime` 返回 **None** ——
    #: 而从库里读出来的 `publish_at` **就是 datetime** ⇒ 闸门会**对它永远放行**,
    #: 也就是"这道闸门只挡当场抓到的候选、挡不住存量"。**静默失效**,最坏的那种。
    from datetime import datetime as _dt

    dt = published if isinstance(published, _dt) else parse_published(published)
    if dt is None:
        return ""                      #: **不知道 ⇒ 放行**(见上)
    from datetime import datetime, timedelta

    if dt < datetime.now() - timedelta(days=days):
        return f"太老(发布于 {dt:%Y-%m-%d},超过 {days} 天)"
    return ""


def sync(session, user_id: int, settings=None) -> dict:
    """找一轮 → 转存 → 入库。返回 `{"status", "found", "ok", "skipped", "failed", "items"}`。"""
    from config.settings import get_settings

    #: 时间归一化(**单一来源**):各平台给的"发布时间"有 **epoch 与多种文本**两种形状,
    #: 别在这里另写一份解析(见 `hot_time.parse_published`)。
    from app.services.hot_time import parse_published

    settings = settings or get_settings()
    top = int(getattr(settings, "pan_discovery_keywords", 5) or 5)
    keywords = _search_words(session, user_id, top, settings)
    if not keywords:
        return {"status": "no_keywords", "found": 0, "ok": 0, "skipped": 0,
                "pending": 0, "failed": 0, "items": []}

    # ⚠️ **只把"已成功 / 终态不搬"的算作已知** —— 把 pending/failed 也算进去的话,
    # 那条链就被永久钉死、再也不会重试(2026-10-02 实测踩过:缺 Cookie 跳过的那条
    # 明明配上 Cookie 就能搬,却因为"发现过了"再也不动)。
    known = set(session.scalars(select(DiscoveredPanLink.origin_url).where(
        DiscoveredPanLink.user_id == user_id,
        DiscoveredPanLink.status.in_(("ok", "skipped"))).distinct()).all())
    # 已存在的行(含 pending/failed)→ **更新而不是再插一条**:否则重试撞唯一键
    exist = {r.origin_url: r for r in session.scalars(select(DiscoveredPanLink).where(
        DiscoveredPanLink.user_id == user_id)).all()}
    # ⚠️ **存量待办必须优先重试**(2026-10-04 加):库里 `pending`/`failed` 的行
    # (缺 Cookie / **盘满** / 超额度)**不能只等"再次被搜到"才动** ——
    # 而**盘满恰恰是最常见的停摆原因**(用户当天亲身遇到:抖音那轮 5 条全卡在盘满,
    # 盘一清出来却没有任何机制会去重搬它们)。
    # 复用本表当**待办队列**:**不新建表、不新增作业**,抖音那条链也把搬不动的往这里入队。
    #: ★★ **`publish_at` 必须带进来**(2026-11-13 修)。此前这里只搬 5 个字段,
    #: 而下面 `_too_old(c.get("publish_at"))` 读的正是它 ⇒ 对**每一条存量待办**都拿到
    #: `None` ⇒ `_too_old` 按"不知道就放行"处理 ⇒ **「只搬新的」对存量完全失效**。
    #: 实测(2026-11-13):221 条待办里 **45 条**(38 百度 + 7 夸克)发布时间已超过 14 天,
    #: 每轮都排在 fresh 前面空转,吃的是同一份额度。
    #: ⚠️ 这正是 `_too_old` 的 docstring 里写着"闸门要挡得住存量"的那个场景 ——
    #: 那边把 `datetime` 认出来了,这边却**根本没把值传下去**。
    backlog = [{"platform": r.platform, "origin_url": r.origin_url, "title": r.title or "",
                "author": r.author or "", "source_url": r.source_url or "",
                "publish_at": r.publish_at}
               for r in exist.values() if r.status in ("pending", "failed")]
    fresh = [c for c in find_candidates(session, user_id, keywords, settings=settings)
             if c["origin_url"] not in known]
    cands, _seen = [], set()
    for c in backlog + fresh:            # 待办在前(它们等得最久),再是新发现的
        if c["origin_url"] in _seen:
            continue
        _seen.add(c["origin_url"])
        cands.append(c)
    # 两份额度(2026-10-05,理由见 settings 里 `pan_discovery_transfer_limit` 的注释):
    # **积压**吃大额慢慢清,**新发现**保底推进 —— 否则积压排在最前且吃满额度,fresh 永远轮不到。
    backlog_budget = int(getattr(settings, "pan_discovery_transfer_limit", 10) or 0)
    fresh_budget = int(getattr(settings, "pan_discovery_fresh_limit", 3) or 0)
    backlog_urls = {c["origin_url"] for c in backlog}
    ok = skipped = failed = pending = 0
    skipped_old = 0                #: ★ 「**太老没搬**」单独计数(见 `_too_old`)——不能混进 `skipped`
    reused = 0                     # **复用了别的盘已有的链**(省下一次转存)
    #: ★★ **盘满直推**的条数(2026-11-23)。它**不花 transfer 额度**(根本没转存),
    #: 所以与 `reused` 一样单独计数 —— 混进 `ok` 会让「转存 N 条」这个数再次骗人
    #: (同 `pan_discovery_tick` 里「转存必须剔掉复用」那条注释)。
    source_only = 0
    #: ★★ **「太老、直推了客户也收不到」的条数**(2026-11-24)。转存闸门
    #: (`pan_discovery_max_age_days`,默认 14 天)比**推送**闸门(`push_fresh_days`,默认 7 天)
    #: 宽 ⇒ 7~14 天这段夹在中间:转存它放行、推送它丢弃。老实现照样给这些行打
    #: 「已按源链推送」并**扣掉直推名额**,然后在 `push_items` 里被新鲜度闸门整批丢掉 ——
    #: 实测 20 条打了印记的行里 **16 条**超过 7 天,客户一条都没收到,而运行记录
    #: 写的是「盘满直推源链20」(**标了已推、实际没推**,静默失败最难查的一种)。
    #: 现在这一档**不占名额、不打印记**,只计数,让名额留给真能到客户手里的那批。
    source_only_old = 0
    #: ★ 直推也**拆两份额度**(理由与上面的转存额度**同源**,2026-11-23 当天用真实积压跑出来):
    #: 积压 327 条排在最前,按 20/轮要 **17 天**才清完 —— 这期间**当天新搜到的资源
    #: 排在队尾,一天都轮不到**。而新资源恰恰最有时效性(用户口径「在新在精」)。
    #: 所以积压直推留给 `pan_discovery_source_push_limit`,新发现保底
    #: `pan_discovery_source_push_fresh`(默认 5)。`0` 仍表示**整条直推路关掉**。
    source_only_total = int(getattr(settings, "pan_discovery_source_push_limit", 20) or 0)
    #: ⚠️ 保底**不许超过总额的一半**:否则总额调小(比如 2)时新发现会把整份吃光、
    #: 积压一条都推不了 —— 那样「积压慢慢清」这件事就彻底停了(算术上等于把假功能写进来)。
    source_only_fresh = min(int(getattr(settings, "pan_discovery_source_push_fresh", 5) or 0),
                            source_only_total // 2)
    source_only_left = source_only_total - source_only_fresh   # 积压那份
    source_only_fresh_left = source_only_fresh                 # 新发现那份
    #: 窗口内**已经直推过**的盘链身份(见 `_recent_source_pushed`)。这批行仍是 `pending`,
    #: 不排除的话它们每轮都排在队首、吃满名额,后面几百条永远轮不到。
    _already_pushed = (_recent_source_pushed(session, user_id, settings)
                       if source_only_total > 0 else set())
    from app.services.push_ledger import pan_key as _pan_key

    def _source_quota_left(is_backlog_c: bool) -> bool:
        """这条候选还能不能吃到直推名额(**积压 / 新发现各一份**,理由见上面)。"""
        return (source_only_left if is_backlog_c else source_only_fresh_left) > 0

    def _pushable(published) -> bool:
        """直推出去**客户真的会收到**吗?

        ★ **为什么直推要单独看一眼**(2026-11-24):推送侧 `push_items` 里的
        `freshness_gate.split_fresh` 按 `push_fresh_days`(默认 **7 天**)裁,
        而转存闸门 `pan_discovery_max_age_days` 默认 **14 天** —— 7~14 天这段
        **能转存、能打印记、能扣名额,却在最后一刻被丢掉**。
        实测:20 条带「已按源链推送」印记的行里 **16 条**落在这段,客户一条没收到,
        而 `runs` 写的是「直推源链20」。⇒ 直推前先用**推送侧的尺子**量一次,
        量不过的老行**不占名额、不打印记**(只进 `source_only_old` 计数)。

        ⚠️ 用 `freshness_gate.is_stale` 而**不是自己再写一遍天数比较** ——
        两把尺子各写一份,改了一处另一处必然漂(本仓母题:同一个事实只能有一个来源)。
        ⚠️ 判不了时间(`None`)**照样放行**,与 `split_fresh` 的 `allow_unknown` 一致 ——
        也正因为两边口径一致,这里判"能推"的,那边一定不会丢。
        """
        from app.services import freshness_gate

        return not freshness_gate.is_stale(published, settings)

    #: 因为「本窗口内已经推过」而被跳过的条数(单独计数 —— 它与"额度用完"不是一回事)
    source_only_done = 0
    items: list[dict] = []
    for c in cands:
        # 这条候选是**存量待办**还是**本轮新搜到的**?决定吃哪份额度(见上面两行注释)
        is_backlog = c["origin_url"] in backlog_urls
        src = "积压" if is_backlog else "新发现"
        # **三盘互通**(2026-10-05 用户口径):先看**别的网盘**有没有这个资源。
        # 有 ⇒ **不再转存**,直接把已有那条链推出去 —— 省空间、省额度、少一次写操作。
        # ⚠️ 这一步**不花 transfer 额度**(没调转存接口),所以放在额度判断之前。
        #: ★★ **只搬新的**(2026-10-16,用户口径「资源不在多,**在新在精**」)。
        #: 发布时间比 `pan_discovery_max_age_days` 还老 ⇒ **不搬**(标 `skipped` 并写明"太老",
        #: 与"库里已有"**分开记** —— 两者处置不一样)。⚠️ **判不了就放行**:不少源不给时间,
        #: 拿"不知道"当"老"会把整条链堵死。
        _too = _too_old(c.get("publish_at"), settings)
        have = None if _too else already_have(session, user_id, c["title"])
        #: 这一条是不是「**盘满 ⇒ 只推源链、没转存**」?**必须用显式标记** ——
        #: 别在后面拿 `message.startswith(...)` 之类反解状态(本仓栽过:子串判据一旦
        #: 被文案改动或解释性注释命中,就会静默判错,见 `_fail` 那边同样的取舍)。
        pushed_source_only = False
        if _too:
            status, message, our, code = "skipped", _too, "", ""
            skipped_old += 1
        elif have:
            status = "ok"
            message = f"库里已有(跳过转存,直接复用该盘):{str(have.get('pan_url') or '')[:60]}"
            our, code = str(have.get("my_link") or ""), ""
            reused += 1
        elif (backlog_budget if is_backlog else fresh_budget) <= 0:
            # 消息里**写清是哪份额度**用完 —— 否则看日志分不出"积压没清完"和"新发现被挡"
            status, message, our, code = "pending", f"本轮{src}额度用完", "", ""
        else:
            # ⚠️⚠️ **转存前先放掉写锁**(2026-10-06 审计逮到,与夸克口令那条**同源**):
            # `transfer_pan_url` 是**网络慢活**(每条 1–3 秒 × 最多 13 条),而 SQLite 是**单写者**,
            # 别的作业 `busy_timeout` 只有 30 秒 ⇒ **它们的心跳写入被饿死**。
            # 实测证据:`11:34:30`(正好是本作业跑的时段)**`alert_fixed_time` / `collect_tick`
            # 的「作业心跳写入失败」成批出现**;`14:19–14:23` 我手动跑 drain 时又来一批。
            # 纪律:**慢活(网络/浏览器/模拟器)一律不要在事务里做**。
            session.commit()
            #: ⚠️ 第 5 个位置参数是 **`snippet`**(百度要从这段文字里找提取码)——
            #: 所以 `title` **必须再用关键字传一次**,否则"转存后核对"拿不到标题 = 形同没接。
            res = transfer_pan_url(session, user_id, c["origin_url"], settings, c["title"],
                                   title=c["title"])
            status, message = res["status"], res["message"]
            our, code = res["our_url"], res["code"]
            if status == "ok":
                if is_backlog:
                    backlog_budget -= 1
                else:
                    fresh_budget -= 1
            elif _is_space_blocked(message) and (_pan_key(c["origin_url"]) in _already_pushed
                                                 or not _source_quota_left(is_backlog)):
                #: ★ **这一条本窗口内已经直推过**(它仍是 `pending`,所以还在队首)——
                #: **不占名额、不改消息、不再进 items**。但转存**照样试过了**(上面那步):
                #: 空间一旦释放,这里会走 `status == "ok"` 正常搬,不会被这道判断挡住。
                #: ⚠️ **额度为 0 也走这里**(而不是落到下面的"未转存已推送"分支):
                #: 那会把没推过的行标成「已推送」——消息与事实不符,下一轮还会被当成
                #: 已推过的样本(见 `_source_pass_code` 那边同一条纪律:不装作做过)。
                _ex = exist.get(c["origin_url"])
                #: ⚠️ **不要把库里已有的提取码擦掉**:上一轮直推时补到的码还在那一列里,
                #: 这轮转存失败拿回的是空串 ⇒ 直接覆盖等于把码丢了(下轮还得重新找,
                #: 而标题里的码可能早被上游改掉了)。
                status, message, our = "pending", message, ""
                code = str(getattr(_ex, "pass_code", "") or "") or _source_pass_code(c)
                if _pan_key(c["origin_url"]) in _already_pushed:
                    source_only_done += 1
            elif _is_space_blocked(message) and not _pushable(c.get("publish_at")):
                # ★★★ **推了也到不了客户手里 ⇒ 不占名额、不打印记**(2026-11-24)。
                # 转存闸门 14 天、推送闸门 7 天 ⇒ 7~14 天这段能转存、能打印记、能扣名额,
                # 却在 `push_items` 的新鲜度闸门被整批丢掉。实测 20 条印记行里 **16 条**
                # 落在这段:客户一条没收到,`runs` 却写「盘满直推源链20」。
                # ⇒ 这一档**只保留原文的空间不足措辞**(库里仍是 `pending`,空间出来会补搬),
                #    把名额让给能真正送达的那批。**提取码照旧保住**(不擦库里的值)。
                _ex = exist.get(c["origin_url"])
                status, message, our = "pending", message, ""
                code = str(getattr(_ex, "pass_code", "") or "") or _source_pass_code(c)
                source_only_old += 1
            elif _is_space_blocked(message) and _source_quota_left(is_backlog):
                # ★★★ **盘满 ⇒ 跳过转存,直接推源链**(2026-11-23,用户口径
                # 「内存不足时不转存直接推送」)。见 `_is_space_blocked` 的说明:
                # 这些链**本身是好的**,被"必须换成我方链"这条前提整批扣在积压里 ——
                # 实测 2026-11-19 起连续 5 天转存 0、积压 327(其中 326 条空间不足)。
                # ⚠️ **库里仍记 `pending`**(不是新状态):空间一出来它就还得被搬一次,
                #    记成终态等于把 326 条永远丢掉;
                # ⚠️ 消息头**必须不一样** —— 否则运维看到「空间不足」会以为还是没推。
                status = "pending"
                message = (f"{_SRC_ONLY_PREFIX}:{message[:120]}"
                           f"(空间出来后会自动补搬)")
                #: ⚠️ **提取码必须在这里补**:源链是**别人的**分享,很多带密码;
                #: 转存那条路拿到的是**我们自己**新链的码,这个分支没有它。
                #: 判据与转存时同一套(`extract_pwd`,认 `?pwd=` 与「提取码:xxxx」)。
                #: ⚠️⚠️ **`our_url` 必须留空,不能塞源链**(2026-11-23):那一列的定义是
                #: 「**我方**分享链」,下游 `ai_calibration` 用 `our_url != ''` 判「搬成」、
                #: `chain_delivery` 用非空判 `moved` —— 塞进去等于把「没搬」记成「搬了」,
                #: 搬运统计和交付状态一起骗人。卡片要的源链走 `items["share_url"]`(见下)。
                our, code = "", _source_pass_code(c)
                pushed_source_only = True
                source_only += 1
                if is_backlog:
                    source_only_left -= 1
                else:
                    source_only_fresh_left -= 1
        row = exist.get(c["origin_url"])
        if row is None:
            row = DiscoveredPanLink(user_id=user_id, origin_url=c["origin_url"][:500])
            session.add(row)
            exist[c["origin_url"]] = row
        row.platform, row.title = c["platform"], c["title"][:255]
        row.author, row.source_url = c["author"], c["source_url"]
        #: ★ **原帖发布时间**(2026-10-16):**只往有值的方向写** —— 与"读数只许往高走"同一条纪律
        #: (不知道就别拿空值盖掉已知的)。`parse_published` 认 **epoch 与各平台文本**两种形状,
        #: 认不出**返回 None 是合法的**(不少源就是不给时间),别当异常。
        _pub = parse_published(c.get("publish_at"))
        if _pub and not row.publish_at:
            row.publish_at = _pub
        #: ★ **「已按源链推送」这个印记不许被下一轮洗掉**(2026-11-24)。
        #: 直推过的行仍是 `pending` ⇒ 下一轮还会被重新判一次盘满,而这里拿到的是
        #: **本轮刚生成的原始失败原因**(「百度盘空间不足…」)⇒ 直接覆盖等于把
        #: 「这条已经推给客户了」这条事实抹掉。实测 11-23 直推的 37 条,到 11-24
        #: 只有 20 条还带印记,其余全被洗回原文 —— 于是「推过没有」只能靠台账回溯,
        #: 而台账只留 72 小时(见 `push_pan_dedupe_hours`)。
        #: ⚠️ 只**保留前缀**,后面那段原因仍用本轮的原文(它是最新的盘况)。
        if row.message and str(row.message).startswith(_SRC_ONLY_PREFIX) \
                and not message.startswith(_SRC_ONLY_PREFIX):
            message = f"{_SRC_ONLY_PREFIX}(此前已推过;本轮仍盘满,未能补搬)"
        row.status, row.message = status, message[:200]
        row.our_url, row.pass_code = our[:500], code[:32]
        # ★★ **必须是 `commit()`,不能是 `flush()`**(2026-10-08)。
        #
        # **实测证据**(拿生产库副本真跑这个循环,数 SQLAlchemy 的事务事件):
        # `flush()` 版跑 **9 条候选只产生 2 次 COMMIT** ⇒ **事务横跨了多次迭代**;
        # 改成 `commit()` 后是**每条一次**。
        # 而每次迭代里都要调 `already_have()`(查库),所以事务横跨几轮 = 写锁被多抱几轮 ——
        # SQLite 是**单写者**,别人 `busy_timeout` 30 秒等不到就 `database is locked`。
        # 看门狗(见 `app/db/database.py`)点名撞锁时"开着的写事务"**正是这一行**。
        #
        # ⚠️ **不要把"横跨几轮"读成"几秒"**:看门狗早期报过一个 6.3s 的数字,
        # 但那份仪器**没能复现**、后来还被发现可能虚报(它只在事务**结束**时报,
        # 且没有可靠的清零信号)。**站得住的只有上面那条计数证据。**
        #
        # 语义不变:同轮去重靠内存里的 `exist` 字典;而"这一条先落盘"本来就是这个循环想要的
        # (上面 608 行已经为同一个理由手工 commit 过一次)。
        session.commit()
        if status == "ok":
            ok += 1
            items.append({"title": c["title"], "author": c["author"],
                          "source_url": c["source_url"], "share_url": our, "code": code,
                          # ★ **原帖发布时间**(2026-10-29):推送侧新鲜度闸门要用它。
                          "publish_at": c.get("publish_at"),
                          # ★ 互动数带进卡(2026-10-16):三个候选源(知乎/微博/贴吧)都给了,
                          #   只是这一步没接 ⇒ 卡上「对标数据」恒空。
                          "metrics": dict(c.get("metrics") or {})})
        elif pushed_source_only:
            #: ★★ **盘满直推**(2026-11-23):没转存成我方链,但**这条源链客户能直接用**
            #: ⇒ 照样进 `items` 推出去。卡片那边靠 `source_only` 这个标记区分
            #: (列名从「我方分享链」换成「源链(未转存)」,别让客户以为我们搬过)。
            pending += 1
            items.append({"title": c["title"], "author": c["author"],
                          "source_url": c["source_url"],
                          #: ⚠️ `share_url` 放的是**别人的源链**。`push_ledger` 会把它
                          #: 当成 `pan_url` 记台账(日结/复盘按盘链聚合),这是**有意的**:
                          #: 那条链确实是客户点开拿到的那一份。
                          "share_url": c["origin_url"], "code": code,
                          "source_only": True,
                          "publish_at": c.get("publish_at"),
                          "metrics": dict(c.get("metrics") or {})})
        elif status == "skipped":
            skipped += 1
        elif status == "pending":
            pending += 1
        elif status == "failed":
            failed += 1
    session.commit()
    # ⚠️ **积压必须被算出来并说出来**(2026-10-05)。原来这里只记「候选 N 转存 M」——
    # 而**积压恰恰是看不见的那一半**:实测一轮候选 44、转存 3,库里静静躺着 41 条
    # `pending`,运行记录一路 `success`。没人会去查"还剩多少没搬",于是额度不足
    # 可以无声无息地持续几周(与「静默失败 = 假成功」同源)。
    backlog_left = int(session.scalar(
        select(func.count()).select_from(DiscoveredPanLink).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.status.in_(("pending", "failed")))) or 0)
    logger.info("网盘发现:词 %d 个 → 候选 %d 条 → 转存成功 %d(其中复用已有 %d);"
                "**盘满未转存、直推源链 %d 条**;"
                "**积压剩 %d 条**(积压额度 %d / 新发现额度 %d)",
                len(keywords), len(cands), ok, reused, source_only, backlog_left,
                int(getattr(settings, "pan_discovery_transfer_limit", 10) or 0),
                int(getattr(settings, "pan_discovery_fresh_limit", 3) or 0))
    return {"status": "ok", "found": len(cands), "ok": ok, "skipped": skipped,
            "pending": pending, "failed": failed, "reused": reused, "items": items,
            #: ★★ **盘满直推**单独报(2026-11-23):它**不花转存额度**、也没转存,
            #: 和 `reused` 一样必须与 `ok` 分开 —— 否则「转存 N 条」的口径又骗人。
            "source_only": source_only,
            #: ★ **「太老,推了客户也收不到」的条数**(2026-11-24)。它**没推**、**没占名额** ——
            #: 单独报出来,否则这些行会让人以为「积压里还有很多能推的,怎么不推」。
            #: 它的存在也说明**两道闸门不同宽**(转存 14 天 / 推送 7 天)这件事是真的。
            "source_only_old": source_only_old,
            #: 因为「本窗口内已经直推过」而跳过的条数。单独报 —— 它与"额度用完"不是一回事:
            #: 看到 source_only=0 但这里很大,说明**当前窗口已经推过一批**,不是坏了。
            "source_only_done": source_only_done,
            "backlog_size": len(backlog),        # 本轮开始时**排队等搬**的存量
            "backlog_left": backlog_left,       # 本轮结束时**仍**在排队的
            #: ★ 「太老没搬」进运行记录 —— 否则开了闸门之后"候选一大堆、搬的很少"
            #: 会被读成"链子坏了"(与"积压必须说出来"同一条纪律)。
            "skipped_old": skipped_old}


def push_items(items: list[dict], settings, session=None, user_id: int = 0) -> bool:
    """把新转存的资源推飞书(**知乎专属群**,未配回落主群)。版式与公众号一致:四列网格。"""
    if not items:
        return False
    from app.services.feishu_client import webhook_for

    # 这是**内容卡**(资源 + 可用链,给客户用)→ 未配专属群时回落**客户主群**是对的。
    # (2026-10-03 曾误改成"落管理群",用户纠正:管理群只接维护信息、内容就该进客户群。)
    webhook = webhook_for(settings, "zhihu")
    if not webhook:
        return False
    # ★ **推送去重**(2026-10-29):同一板块内 24 小时内推过的不再推(数据:知乎重推率
    # 只有 3%,但统一口径;真正的价值在 B站 53% / 抖音 62% 那几个板块)。
    from app.services import push_ledger

    items, _dup = push_ledger.drop_recent(
        session, user_id, "zhihu", items, settings,
        hot_of=lambda it: (it.get("metrics") or {}).get("liked_count")
        or (it.get("metrics") or {}).get("view_count"),
        hot_min=float(getattr(settings, "push_hot_bypass_likes", 1000) or 0))
    if not items:
        return False
    # ★ **新鲜度闸门**(2026-10-29 用户口径「所有板块都要新鲜度检查」)。
    # ⚠️ 它**只决定推什么**,不改上面的转存闸门(`pan_discovery_max_age_days`)。
    from app.services import freshness_gate

    items, _stale, _unknown = freshness_gate.split_fresh(items, settings)
    if not items:
        logger.info("公开平台:%d 条全是 %d 天前的旧内容,本轮不推", _stale,
                    int(getattr(settings, "push_fresh_days", 7) or 0))
        return False
    # ★ **AI 价值筛选**(2026-10-29,用户口径「每个板块都加上 AI 筛选」)。
    from app.services import ai_filter

    _ai = ai_filter.filter_items(
        "zhihu", items, settings,
        text_of=lambda it: (
            f"{it.get('title') or ''} 作者:{it.get('author') or ''} "
            #: 价值口径(用户 2026-10-29):「资源发的多、转存人数多」——带热度一起判。
            + " ".join(f"{k}={v}" for k, v in (it.get("metrics") or {}).items())))
    if not _ai.items:
        return False
    items = _ai.items
    #: ★ **客户第一眼要的答案**(2026-10-29):这一批发出去的全部**可直接取用**,还分落在哪个盘。
    from collections import Counter as _Counter

    from app.services.douyin_leads import _pan_name as _pn

    _pan_cnt = _Counter(_pn(it.get("share_url") or "") or "网盘" for it in items)
    _pan_txt = " / ".join(f"{k} {v}" for k, v in _pan_cnt.most_common())
    from app.services.feishu._cards import _col_set_row, _md_safe, metrics_md, strip_others
    from app.services.feishu_client import FeishuClient

    brand = (getattr(settings, "brand_name", "") or "").strip()
    #: ★★ **盘满直推**的说明必须分开写(2026-11-23)。原来这句「已转存成我方分享链」是
    #: **硬编码的假话** —— 一旦有 `source_only` 的行混进来,客户会以为我们搬过,
    #: 而实际上那条链是**别人的分享**、我们没动过。两件事不能混成一句话。
    _n_src = sum(1 for it in items if it.get("source_only"))
    if not _n_src:
        _head = (f"公开平台(知乎/贴吧)新发现 **{len(items)}** 个资源,已转存成我方分享链:"
                 f"「**资源**」点开是我方链、可直接取用;")
    elif _n_src == len(items):
        _head = (f"公开平台(知乎/贴吧)新发现 **{len(items)}** 个资源,"
                 f"**网盘空间不足、本轮未转存**,直接给源链(点开同样能取):")
    else:
        _head = (f"公开平台(知乎/贴吧)新发现 **{len(items)}** 个资源:"
                 f"**{len(items) - _n_src}** 条已转存成我方分享链、"
                 f"**{_n_src}** 条因**网盘空间不足未转存**(给的是源链,点开同样能取);")
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content":
        _head
        + "「**原帖**」点开是对标作品的出处;"
        + "「**对标数据**」= 那条原帖的热度。"
        + f"\n**可直取 {len(items)} 条**({_pan_txt})"
        + (f"\n**{_ai.summary()}**:已挡掉纯广告 / 无具体资源 / 过时的那些。"
           if _ai.summary() else "")
        + (f"\n已跳过 {_dup} 条 {int(getattr(settings, 'push_dedupe_hours', 24) or 0)} 小时内推过的。"
           if _dup else "")
        + (f"\n{freshness_gate.summary(len(items), _stale, _unknown)}"
           if freshness_gate.summary(len(items), _stale, _unknown) else "")}},
        # ★ **「原帖」独立成列**(2026-10-16 用户口径「别的平台也应该保持这个模式:
        #   **有原文,有网盘**」)。`source_url` **本来就在 items 里**(见上面 append),
        #   只是从来没渲染出来 ⇒ 员工看不到"这条资源是从哪个帖子里发现的",无法核对对标作品。
        # ★ **「对标数据」也是 2026-10-16 补的**(用户那 5 要素:对标账号/对标作品/对标数据/资源/可用链)。
        _col_set_row([("**作者**", 3), ("**资源**", 5), ("**原帖**", 2),
                      #: 有盘满直推的行时,列名必须中性 —— 那一列里可能放的是**别人**的源链,
                      #: 叫「我方分享链」就是撒谎(逐行再标「源链(未转存)」)。
                      (("**可用链**" if _n_src else "**我方分享链**"), 3)], grey=True)]
    from app.services.hot_time import publish_mark as _pub_mark

    for it in items:
        _src = _md_safe(it.get("source_url") or "")
        #: ★ 每条都带发布时间(2026-10-31 用户:「没看到资源时间的新鲜度」)
        _pm = _pub_mark(it.get("publish_at"))
        elements.append(_col_set_row([
            (_md_safe(it.get("author") or "—"), 3),
            (_md_safe(strip_others(it.get("title") or "")[:40])
             + (f" {_pm}" if _pm else ""), 5),
            (f"[▶ 原帖]({_src})" if _src else "—", 2),
            (metrics_md(it.get("metrics") or {}), 2),
            (f"[▶ 打开]({_md_safe(it.get('share_url') or '')})"
             #: ⚠️ **逐行标「源链未转存」**(2026-11-23):表头为了这一档改成了中性
             #: 「可用链」,但客户仍要能一眼分出手里这条是**我们的**还是**别人**的 ——
             #: 别人的链可能哪天被撤掉,我们的不会。码取不到时**不显示 🔑**(不猜、不装)。
             + (" (源链,未转存)" if it.get("source_only") else "")
             + (f" 🔑{_md_safe(it.get('code') or '')}" if it.get("code") else ""), 3)]))
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "turquoise", "title": {"tag": "plain_text",
                                                          "content": f"🔎 {brand + ' · ' if brand else ''}"
                                                                     f"新发现资源 · {len(items)} 个"}},
            "elements": elements}
    try:
        ok = FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(card)
    except Exception:  # noqa: BLE001
        logger.exception("网盘发现推送失败")
        return False
    if ok and session is not None and user_id:
        # ★ **推送台账**(2026-10-16):板块日结/周结读它。这条卡落在**知乎群**,
        #   所以 section 记 `zhihu`(台账记的是"落地那个群",不是"从哪个平台抓的")。
        from app.services import push_ledger

        push_ledger.record(session, user_id, "zhihu", items)
    return ok


def pan_discovery_tick(settings=None) -> int:
    """定时:按资源词搜公开平台 → 转存入库 → 推飞书。返回本轮成功数。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    if not getattr(settings, "pan_discovery_enabled", True):
        return 0
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            from app.services.tenant_base import _record_run

            try:
                out = sync(db, uid, settings=settings)
                total += out.get("ok", 0)
                # ⚠️ **卡片推没推出去必须留痕**(2026-10-10,同 `resource_presence` 那处):
                # `push_items` 在「没配 webhook」时**静默 return False**,返回值又被丢掉
                # ⇒ 「这轮卡片到底推出去没有」事后查不出来。
                pushed = (push_items(out["items"], settings, session=db, user_id=uid)
                          if out.get("items") else None)
                # 运行记录里**必须带上积压数** —— 只写「候选N 转存M」的话,一个持续
                # 堆积的系统看起来和健康的系统一模一样(2026-10-05 实测踩到)。
                #: ★★ **「转存」必须剔掉「复用」(2026-10-17)**:`out["ok"]` 里**含复用**
                #: (复用那条也走 `status="ok"` 分支)⇒ 原来写「转存24 复用11」,读起来像
                #: 24+11=35,而**真实只搬了 13 条**(24−11)—— 正好等于额度上限(10+3),
                #: 于是「**额度满了**」这件事被数字藏住了(一读就是「搬了 24 条还不错」)。
                #: 本仓母题:两种事混成一个数 ⇒ 结论必错。改成「转存13(复用11)」。
                _real = max(0, int(out.get("ok", 0) or 0) - int(out.get("reused", 0) or 0))
                _record_run(db, uid, "pan_discovery", "success",
                            f"候选{out.get('found', 0)} 转存{_real} "
                            f"(复用{out.get('reused', 0)}) **积压{out.get('backlog_left', 0)}**"
                            #: ★★ **盘满直推必须报出来**(2026-11-23):这一档**没转存**,
                            #: 不写的话「转存 0」会看起来像整轮什么都没发生 ——
                            #: 而实际上客户已经收到 N 条源链(与「积压必须说出来」同一纪律)。
                            + (f" **盘满直推源链{out['source_only']}**"
                               if out.get("source_only") else "")
                            #: ★ **「太老,推了客户也收不到」单独报**(2026-11-24):它们
                            #: **没推、没占名额**。不写的话,看到「直推 0」会以为
                            #: 积压里没有能推的,而实际是**这批连推送闸门都过不了**
                            #: (两道闸门不同宽:转存 14 天 / 推送 7 天)。
                            + (f" 太老推不到{out['source_only_old']}"
                               if out.get("source_only_old") else "")
                            #: 窗口内已推过而跳过的条数:没有它,「直推 0」会被读成"没在推"
                            #: (而实际是**本窗口已经推过一批、还没到重推时间**)。
                            + (f" (本窗口已推{out['source_only_done']})"
                               if out.get("source_only_done") else "")
                            #: ★ 「**太老没搬**」必须看得见(2026-10-16):否则开了"只搬新的"
                            #: 这道闸门之后,"候选一大堆、搬的很少"会被读成"链子坏了"。
                            + (f" **太老跳过{out['skipped_old']}**"
                               if out.get("skipped_old") else "")
                            + (f" 卡片{'已推' if pushed else '未推'}" if pushed is not None else ""))
                threshold = int(getattr(settings, "pan_discovery_backlog_alert", 60) or 0)
                left = int(out.get("backlog_left") or 0)
                if threshold and left >= threshold:
                    _alert_backlog(db, uid, left, out)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("网盘发现失败 user=%s", uid)
                _record_run(db, uid, "pan_discovery", "failed", str(exc)[:200])
                db.commit()
    finally:
        db.close()
    return total
