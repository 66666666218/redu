"""抖音推广线索(2026-10-02,用户提供的判据)。

**要解决的问题**:抖音上的网盘推广号,标题里会多出一段**与视频内容无关**的文字——
常见形式是**书名号包裹**,而 `《…》` 里就是**迅雷的分享口令**(用户口径,2026-10-02):
进迅雷搜索框搜「白泽的梦」「三岁分享」就能搜到资源。所以它不只是"这人像推广号"的信号,
**本身就是可用的资源入口**。
实测(拿"diplay车机互联"搜 13 条):

    ★《白泽的梦》diplay软件下载教程 支持安卓苹果车机互联…     ← 命中
    ★《三岁分享》#diplay车机互联#carplay 一个软件实现车机互联,不用盒子 ← 命中
      不用加盒子,一个车机软件就可以实现无线CarPlay              ← 未命中
      Diplay如何安装#用车小常识 #Carplay #比亚迪 #iphone        ← 未命中
      …(其余 9 条同样都是直接的内容描述)

**格式差异一眼可辨**:命中那 2 条以《…》开头,其余全是直接描述内容。这是用户给的判据。

**为什么只推线索、不自动收号**:本项目装的 MediaCrawler 是作者的**教学版**,账号信息被
刻意脱敏(昵称 `睡***着`、user id 是 sha256 截断不可逆、主页链接不采集),**拿不到是谁**;
而**视频链接是完整的**——推给运营点开就能看到作者,人工补最后一步即可。
(账号层能全自动的只有知乎/B站,见 `cross_accounts.py`。)

判据:`标题以 《…》 开头`。它比"内容里有网盘链"更早命中——因为抖音的链本来就不在公开层
(见 `doc/pan-promotion-channels.md` §七)。
"""
from __future__ import annotations

import re

from sqlalchemy import select

from app.utils import get_logger
from app.services.chain_delivery import mark_newly_moved

logger = get_logger(__name__)

# 标题里所有《…》(不限位置)——口令可能出现在开头(`《白泽的梦》diplay…`),
# 也可能嵌在句中(`苹果安卓手车互联更新《玩车不求人》新版本`)。
_ANY_BRACKET_RE = re.compile(r"《([^》]{1,20})》")
# 标题**以**《…》开头 —— 最强的信号(分享者把自己的品牌名顶在最前面)。
_LEAD_RE = re.compile(r"^《([^》]{1,20})》\s*")


def _matches_nickname(mark: str, masked_name: str) -> bool:
    """《》里的名字是否就是**本账号自己**(用脱敏昵称校验)。

    MediaCrawler 的脱敏规则是"首尾各留 1 字、中间打星"(`玩车不求人` → `玩***人`),
    所以**首尾字都对得上**就足以认定《》里写的是这个号的品牌名 —— 那是口令。
    反之 `My Dearest` vs `汶***汝` 对不上,说明它只是视频内容(剧名),不该收。
    """
    if not mark or not masked_name or "*" not in masked_name:
        return False
    return mark[0] == masked_name[0] and mark[-1] == masked_name[-1]


def _lead_mark(text: str, masked_name: str = "") -> str:
    """从标题里取出**口令候选**;没有《…》则返回空串。

    ⚠️ **不要用"《》内容 = 账号名"来判定** —— 用户 2026-10-02 明确指出:
    "账号名称跟关键词并没有特殊关联,只是这个凑巧了"。也就是说 `《玩车不求人》`
    碰巧等于昵称,但**一般情况两者无关**,拿昵称做判据会漏。

    所以这里只做一件事:**把标题里的《…》摘出来当候选**。
    排序用的优先级(不影响"收不收",只影响先后):
      ① 标题**以**《…》开头 —— 分享者把品牌名顶在最前;
      ② 《》内容与**本账号脱敏昵称首尾吻合** —— 碰巧同名时的高置信信号;
      ③ 其余(《》嵌在句中的)。
    最终"是不是真口令"由人看 —— 线索本来就是推给人判断的。
    """
    m = _LEAD_RE.match(text)
    if m:
        return m.group(1)[:20]
    for cand in _ANY_BRACKET_RE.findall(text):
        return cand[:20]          # 不限位置:句中也可能藏口令
    return ""


#: 正文里的**网盘直链**(夸克/百度/迅雷;`drive.uc.cn` 也算夸克系)。
#: ★ 2026-10-29:用户口径「这些小游戏也是都需要带网盘的」—— 抖音上有一批单机游戏号
#: 直接把**百度网盘链接**写在文案里(实测搜 `游戏合集 下载` 46 条里 6 条带盘),
#: 这类帖子**没有《口令》** ⇒ 原来会被 `collect_leads` 全部丢掉。
_RE_PAN_URL = re.compile(
    r"https?://[^\s\"'<>]*?(?:pan\.quark\.cn|drive\.uc\.cn|pan\.baidu\.com|pan\.xunlei\.com)[^\s\"'<>]*")

#: 出现这些词才把《…》当成**口令**(而不是剧名)。见 `pan_of`。
_KOU_HINTS = ("口令", "咐置", "付置", "叩苓", "网盘", "提取码", "复制这段", "复制链接",
              "链接自取", "资源自取", "自取")


def pan_of(text: str) -> dict:
    """标题/正文里的**网盘凭据** → `{mark, pan_url}`;两样都没有就是两个空串。

    ⚠️ **《…》本身不算网盘凭据**(2026-10-29 当场踩到):短剧/漫剧标题里
    `《朝朝入梦》全80集` 遍地都是,拿 `_lead_mark` 一律收下会让**每条剧名都变成"带网盘"**。
    所以只有两种才算:
      · **正文里的网盘直链**(夸克/百度/迅雷);
      · **真口令**:`/~token~/` 或 `咐置…叩苓`(复用 `quark_kouling.mark_of`),
        或者**附近有"口令/网盘/提取码"这类词**时,《…》才算口令。

    ★ **单一出处**:线索卡与对标号卡都用这一份判据(对标号那边 `import` 它),
    别各写一份 —— 两处口径迟早飘。
    """
    from app.services.quark_kouling import mark_of

    t = str(text or "")
    m = _RE_PAN_URL.search(t)
    mark = mark_of(t)
    if not mark and any(h in t for h in _KOU_HINTS):
        mark = _lead_mark(t)
    return {"mark": mark[:64], "pan_url": (m.group(0) if m else "")[:500]}


def _lead_rank(text: str, masked_name: str) -> int:
    """线索排序优先级(越小越靠前):开头《》 0 / 与昵称吻合 1 / 其它 2。"""
    if _LEAD_RE.match(text):
        return 0
    for cand in _ANY_BRACKET_RE.findall(text):
        if _matches_nickname(cand, masked_name):
            return 1
    return 2


# **线索平台注册表**(2026-10-02):"按资源词搜内容 → 抓口令 → 转存"这条链的搜索源。
# 接新平台 = 加一行 + 在 .env 配 `FEISHU_WEBHOOK_<平台>` + 用 MediaCrawler 登录一次。
# ⚠️ **已被实测排除的**:B站/知乎的内容层**没有口令形态**(见 doc/pan-promotion-channels.md §九),
# 它们只适合"按行业词找账号"那条路(cross_accounts),别往这里加。
PLATFORMS = {
    "douyin": {"section": "douhot", "label": "抖音"},        # 抖音的板块名是历史遗留的 douhot
    "kuaishou": {"section": "kuaishou", "label": "快手"},
    "xiaohongshu": {"section": "xiaohongshu", "label": "小红书"},
    "weibo": {"section": "weibo", "label": "微博"},
    "bilibili": {"section": "bilibili", "label": "B站"},
    "zhihu": {"section": "zhihu", "label": "知乎"},
    "tieba": {"section": "tieba", "label": "贴吧"},
}


def platforms_of(settings) -> list[str]:
    """本轮要跑的平台(逗号分隔配置);未知名字忽略并记 warning(别让一个拼错停掉整轮)。"""
    raw = str(getattr(settings, "leads_platforms", "douyin") or "").split(",")
    out: list[str] = []
    for name in (x.strip() for x in raw):
        if not name:
            continue
        if name not in PLATFORMS:
            logger.warning("线索平台 `%s` 不认识,已忽略(可选:%s)", name, "/".join(PLATFORMS))
            continue
        if name not in out:
            out.append(name)
    return out or ["douyin"]


_AWEME_RE = re.compile(r"/(?:video|note)/(\d+)")


def _aweme_id(url: str) -> str:
    """从抖音链接里取**作品 id** —— 线索落库的去重键。

    ⚠️ 不能用 `_parse_record` 给的 `uid`:那个是 **creator_hash**(作者维度),
    同一个作者发的多条视频会全撞在一起,去重会丢线索。落库键必须是**作品维度**。
    """
    m = _AWEME_RE.search(url or "")
    return m.group(1) if m else ""


def _try_protocol(keywords: list[str], session, stats: dict | None = None,
                  user_id: int = 1, pages: int = 1) -> tuple[list[dict], Exception | None]:
    """走抖音纯协议取一批;返回 `(记录, 异常)`。**异常不在这里抛** —— 留给调用方决定回落。

    `stats` 会被 `douyin_protocol_source.search` 填进 `kw_total` / `kw_hit`
    (几个词、其中几个有结果)—— **判"是不是被限流"就靠这两个数**。

    ★ **多账号轮换**(2026-10-21):抖音的 `verify_check` 是**按账号/会话**下发的。
    库里配了 `douyin#2`(及更多)时,当前号被风控就**换下一个号**继续取,
    而不是让整条线索链停摆等 6 小时。
    ⚠️ 没配第二个号时 `account_keys` 只返回 `douyin` ⇒ 行为与改造前逐字一致。
    """
    from app.services import douyin_protocol_source as dps

    keys = dps.account_keys(session, user_id)
    last_exc: Exception | None = None
    tried = 0
    #: 这几个 kind 都是"**这个号**不可用",换号有意义;网络故障换号也一样会失败,
    #: 但换一下的代价很小(一次请求),所以一并跳过,只是不再重试同一个号。
    _switchable = {"verify", "verify_cooldown", "need_login", "restricted", "argus", "blocked"}
    for key in keys:
        if dps.in_verify_cooldown(session, user_id, cookie_key=key):
            continue
        tried += 1
        try:
            rows = dps.search(keywords, session=session, stats=stats,
                              user_id=user_id, cookie_key=key, pages=max(1, int(pages or 1)))
        except Exception as exc:  # noqa: BLE001 - 回落是设计的一部分,这里就是它的入口
            last_exc = exc
            if getattr(exc, "kind", "") in _switchable:
                continue
            return [], exc
        if rows:
            if tried > 1:
                logger.info("抖音纯协议:本轮由第 %d 个号(%s)取到 %d 条", tried, key, len(rows))
            return rows, None
        #: 这个号**没报错但一条没搜到** —— 形态与限流一致。有下一个号就换,没有就交回上层判。
        logger.info("抖音纯协议:%s 本轮 0 条,尝试下一个号(如有)", key)
    if tried == 0:
        return [], dps.DouyinProtocolError(
            "抖音**所有号都在验证冷却中**,本次不发请求(动作是等或补一个号)",
            kind="verify_cooldown")
    return [], last_exc


#: 验证冷却期内"浏览器兜底是否已经探过"的标记(每个冷却窗口只探一次)。
_PROBE_KEY = "douyin_browser_probe_until_{uid}"


def _browser_probe_allowed(session, user_id: int = 1) -> bool:
    """验证冷却期内,**这一轮还允不允许开浏览器兜底**?

    ⚠️ 为什么不是一刀切禁掉:协议被 `verify_check` 挡住时,**真人形状的浏览器那一跳
    有时还能搜到**(代码注释里 09:30 的实测案例)⇒ 完全禁掉会丢掉这条唯一的恢复路。
    ⚠️ 为什么也不能每轮都探:16:10 / 20:10 / 22:30 / 00:10 连续四轮浏览器也空,
    而代码自己的告警写着「探针也在打」⇒ 反复探只会延长风控。
    ⇒ **每个冷却窗口只探一次**:记下"这个截止时间已经探过",同一窗口再来就跳过;
    冷却被重新设置(截止时间变了)或多轮之后解冻,才会再探。
    """
    if session is None:
        return True
    try:
        from app.db.models import SystemConfig

        until_row = session.scalar(select(SystemConfig).where(
            SystemConfig.key == f"douyin_verify_until_{user_id}"))
        until = str(until_row.value or "") if until_row else ""
        if not until:
            return True
        key = _PROBE_KEY.format(uid=user_id)
        probe = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
        if probe and str(probe.value or "") == until:
            return False
        if probe:
            probe.value = until
        else:
            session.add(SystemConfig(key=key, value=until))
        session.commit()      # 先把写锁放掉 —— 后面是几分钟的浏览器慢活
        return True
    except Exception:  # noqa: BLE001 - 状态读不到就当允许(宁可多探一次,也别永远停死)
        logger.debug("读取浏览器兜底探针状态失败,按允许处理", exc_info=True)
        return True


def _release_browser_probe(session, user_id: int = 1) -> None:
    """浏览器兜底**成功**了 ⇒ 把「本窗口已探过」的标记清掉。

    ⚠️⚠️ **不清会怎样(2026-10-25 实测的 8 小时停摆)**:今天的复盘 ——
    `04:12` 浏览器兜底**成功**(运行记录带 `[浏览器兜底]`),但那次探针把
    `douyin_browser_probe_until_<uid>` 写成了当前冷却窗口的截止时间 ⇒
    同一窗口内 `08:10`/`12:10` 两轮**连浏览器都不开**,直接记"全部无结果"。
    也就是说:**唯一还通的那条路,被一次成功给关掉了**。
    这正是本仓母题「静默失败 = 假成功」的一种:链说自己失败,其实是我们自己不出手。

    冷却的本意是"**别打协议**"(协议那种机器人形状才是触发 `verify_check` 的),
    它不该顺带把**真人形状的浏览器**也禁掉 —— 何况那次浏览器刚刚证明它是通的。
    所以这里只清**探针标记**,**不动** `douyin_verify_until`(协议照旧冷却,别去撞)。
    """
    if session is None:
        return
    try:
        from app.db.models import SystemConfig

        key = _PROBE_KEY.format(uid=user_id)
        row = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
        if row is not None:
            row.value = ""
            session.commit()
            logger.info("抖音浏览器兜底成功 ⇒ 释放本窗口的探针标记(下一轮仍可用浏览器)")
    except Exception:  # noqa: BLE001 - 标记清不掉只影响"下一轮能不能再开",别影响本轮结果
        logger.debug("释放抖音浏览器探针标记失败", exc_info=True)


def _verify_until_text(session, user_id: int = 1) -> str:
    """库里记录的**上次 `verify_check` 闸门截止时间**(没有就空串)。

    ⚠️ 只**读**,不发请求 —— 反复探只会延长风控(本仓为这个栽过)。
    """
    if session is None:
        return ""
    try:
        from app.db.models import SystemConfig

        row = session.scalar(select(SystemConfig).where(
            SystemConfig.key == f"douyin_verify_until_{user_id}"))
        return str(row.value or "") if row else ""
    except Exception:  # noqa: BLE001 - 读不到就不说,别编
        logger.debug("读取抖音验证闸门失败", exc_info=True)
        return ""


def _with_douyin_verdict(msg: str, session, user_id: int = 1,
                         cdp_verdict: str = "") -> str:
    """把「**两种原因、动作相反**」收敛成**已知的那一种**。

    ★ 2026-10-25 加。起因:这条告警连续三轮都只给"① 登录过期 / ② 要求过验证,
    别先猜一个",而**判据其实就在库里** —— 上次协议探测撞到 `verify_check` 时
    顺手写了 `douyin_verify_until_<uid>`。于是每一轮都要人(或我)去手工探一次才能定性,
    而本仓的教训正是:**别让人去做判别实验**。

    ★ 2026-10-28 补:纯协议已关(`DOUYIN_LEADS_USE_PROTOCOL=false`),那句
    「协议路会把②明说成 verify_check」**已经没人能兑现**;真正还在说真话的是 **CDP 路**
    (它见到的是「验证码中间页」这一页面级证据)。所以把**本轮 CDP 的原话**接进来 ——
    有它就不用再让人去看体检报告。

    判据是**已有的证据**(本轮 CDP 的结论 / 库里的闸门记录),不是猜:
    有其一 ⇒ 这个号被要求过验证(②);都没有 ⇒ 保持原来的"两种原因"说法。
    """
    if cdp_verdict:
        return msg + (
            "\n**本次判定(据本轮 CDP 路的页面证据)**:② **平台要求过验证** —— "
            f"CDP 原话:{cdp_verdict[:160]}\n"
            "⇒ 动作 = **等 + 别再打**(重扫/重登**没用**;这也解释了同一份 cookie "
            "换个浏览器身份就能搜到)。")
    until = _verify_until_text(session, user_id)
    if not until:
        return msg
    return msg + (
        f"\n**本次判定(据库里上次协议探测留下的闸门)**:② **平台要求过验证"
        f"(`verify_check`),闸到 {until}** ⇒ 动作 = **等 + 别再打**;"
        f"重扫/重登**没用**(人工也没有滑块可过)。")


def _collect(platform: str, keywords: list[str], session=None,
             user_id: int = 1, pages: int = 1) -> tuple[list[dict], str]:
    """取搜索结果 → `(记录列表, 来源标签)`。

    **抖音走纯协议优先**(2026-10-08),其余平台照旧走 MediaCrawler。

    为什么换(见 `config/settings.py` 的 `douyin_leads_use_protocol`):
    协议约 1 秒/词、**不开浏览器**、能拿到**真实作者名**(MediaCrawler 是脱敏的 `某***`,
    推送卡片的"作者列"只能显示星号);且它只依赖我们自己的代码 + f2(Apache-2.0),
    不碰 MediaCrawler 那条 **NON-COMMERCIAL** 许可。

    **回落规则有意做成"不会比今天更贵"**:今天是**每一次都开浏览器**;
    这里只在「协议抛错」或「协议一条都没搜到」时才开 ⇒ 最坏与今天持平,通常便宜得多。

    ⚠️ **回落一定要留痕**(日志 + 来源标签进运行记录)。它本来就是"没搜到"的生产者,
    不说话的话,"协议挂了"就会被读成"抖音上没人在推资源" —— 本仓最忌讳的假阴性。
    """
    cooldown = False
    #: ★ 本轮 **CDP 路**的原话(失败时才有)。它比 MediaCrawler 那句"全部无结果"精确,
    #: 回落时把它接进最终告警 —— 见 `_with_douyin_verdict`。
    cdp_verdict = ""
    if platform == "douyin":
        from config.settings import get_settings

        if getattr(get_settings(), "douyin_leads_use_protocol", True):
            stats: dict = {}
            rows, err = _try_protocol(keywords, session, stats, user_id=user_id, pages=pages)
            total = int(stats.get("kw_total") or 0)
            hit = int(stats.get("kw_hit") or 0)
            # ★★ **"只有少数词有结果"要当成可疑,回落浏览器**(2026-10-08 实测逼出来的)。
            # 同一套配置下实测两种极端都出现过:
            #   · 8/8 个词各 14 条(共 113 条候选)—— 正常;
            #   · **只有 1/8、1/6 个词有结果,其余全 0**(共 13~14 条)—— 形态与抖音
            #     "首屏通、之后回 `status_code=0 + data:[]`"的**限流完全一致**。
            # ⚠️ **只看总条数分不出这两种**:13 条与 113 条都是"十几到上百条"的量级,
            #   而运行记录里只写"线索 N" ⇒ 少掉七分之六的量为**静默**发生。
            # ⇒ 判据用**命中词占比**(< 一半 就可疑),与"完全没搜到"一样回落 ——
            #   回落代价与今天持平(今天每一次都开浏览器),换的是**不静默丢量**。
            few = bool(rows) and total >= 4 and hit * 2 < total
            if rows and not few:
                return rows, "协议"
            if err is not None:
                logger.warning("抖音纯协议失败(kind=%s),回落浏览器:%s",
                               getattr(err, "kind", "?"), str(err)[:160])
            elif few:
                logger.warning("抖音纯协议**只有 %d/%d 个词有结果**(共 %d 条)—— 形态与限流一致"
                               "(实测 8/8 与 1/8 都出现过),本轮回落浏览器",
                               hit, total, len(rows))
            else:
                # ⚠️ **"一条都没搜到"必须与"抛错"分开报**:前者可能是限流
                #    (实测:连发会回 `status_code=0 + data:[]`,与"真没结果"形状相同),
                #    后者是硬失败。两种都回落,但日志里得看出是哪一种。
                logger.warning("抖音纯协议**一条都没搜到**(没报错)—— 可能是限流"
                               "(见 doc/抖音纯协议-链路拆解.md §5),本轮回落浏览器")
        else:
            #: ★ 2026-10-21:纯协议上线(10-08)**之前**浏览器路从没出过 verify_check;
            #: 上线后按日出现(10-17/18/19 分别 20/28/25 次),而同一账号的浏览器路
            #: 同期还能一轮拿 25~30 条 ⇒ 触发风控的是**协议那种机器人形状的请求**。
            #: 关掉它 = 回到「真人形状的本地 Edge + 已登录档案」,与小红书同一条路。
            logger.info("抖音:纯协议已关闭(DOUYIN_LEADS_USE_PROTOCOL=false)—— 走本地浏览器")
        #: 协议层刚撞过 `verify_check` 时,浏览器兜底**不能每轮都打**(探针也会加重风控)。
        from app.services import douyin_protocol_source as dps

        #: 只有**所有号都在冷却**才算"整体冷却" —— 还有一个号能用就不该放弃协议路。
        _keys = dps.account_keys(session, user_id)
        cooldown = bool(_keys) and all(
            dps.in_verify_cooldown(session, user_id, cookie_key=k) for k in _keys)
    #: ★★ **CDP 路优先**(2026-10-25):用**普通命令行启动**的浏览器 + CDP 接管,
    #: 打开搜索页读 DOM。实测同一个号同一条 cookie:MediaCrawler 路 **0 条**、
    #: CDP 路 **16 条**(`webdriver=false`)⇒ 卡住的是**自动化形态**,不是账号/IP。
    #: ⚠️ 失败**必须留痕**并回落 —— 静默回落会让"CDP 挂了"读成"抖音没人推"。
    if platform == "douyin":
        from config.settings import get_settings as _gs

        _s = _gs()
        if bool(getattr(_s, "douyin_cdp_enabled", True)):
            try:
                from app.services import douyin_cdp_source as _dcdp

                _n = max(1, int(getattr(_s, "douyin_cdp_max_keywords", 8) or 8))
                _rows = _dcdp.search(keywords[:_n], session=session,
                                     user_id=user_id, settings=_s)
                if _rows:
                    #: ⚠️ **覆盖不足要说出来**(2026-10-28):CDP 每轮只搜前 N 个词,
                    #: 而它一旦成功就**不再回落** MediaCrawler ⇒ 后面的词这一轮**没搜**。
                    #: 不说的话,下一轮看到"线索少了"只会以为"抖音上没人推"。
                    if len(keywords) > _n:
                        logger.info("抖音CDP:本轮只搜了 %d/%d 个词(上限 "
                                    "DOUYIN_CDP_MAX_KEYWORDS=%d)—— 其余 %d 个**本轮没搜**,"
                                    "不是没结果", _n, len(keywords), _n, len(keywords) - _n)
                    return _rows, "CDP"
            except Exception as exc:  # noqa: BLE001 - 回落是设计的一部分
                #: ★ **把 CDP 的原话留下来**(2026-10-28):它是回落之后唯一还说真话的地方 ——
                #: 「验证码中间页」比 MediaCrawler 那句"全部无结果"精确得多(后者自己都
                #: 承认"两种原因、别先猜一个")。见 `_with_douyin_verdict`。
                cdp_verdict = str(exc)[:200]
                logger.warning("抖音CDP 路失败(%s),回落 MediaCrawler:%s",
                               type(exc).__name__, cdp_verdict[:180])
    if cooldown and not _browser_probe_allowed(session, user_id):
        logger.warning("抖音验证冷却中且本窗口已探过浏览器 —— 本轮不再打"
                       "(动作是等;反复探针只会延长风控)")
        return [], "浏览器兜底(冷却内已探过,跳过)"
    from app.services import mediacrawler_source as mc

    #: ★ 浏览器兜底也带**协议路的登录 cookie**(2026-10-19):档案登录是第一条退路,
    #: cookie 注入是第二条 —— 档案被锁/损坏时仍然是登录态,不会空跑一圈。
    cookie = ""
    if platform == "douyin" and session is not None:
        try:
            from app.services import cookie_store

            cookie = (cookie_store.get_cookie(session, user_id, "douyin") or "").strip()
        except Exception:  # noqa: BLE001 - 读不到就交给档案登录
            logger.debug("浏览器兜底读取抖音 cookie 失败,交给档案登录", exc_info=True)
    try:
        rows = mc.crawl(platform, keywords, cookie=cookie, user_id=user_id)
    except mc.MediaCrawlerError as exc:
        #: ★ **空结果的那句话必须带上判定**(2026-10-25):见 `_with_douyin_verdict`。
        if platform != "douyin":
            raise
        raise mc.MediaCrawlerError(
            _with_douyin_verdict(str(exc), session, user_id, cdp_verdict)) from exc
    if rows and session is not None:
        #: ★ **浏览器既然通了,就别把它自己关掉**(2026-10-25):见 `_release_browser_probe`。
        if cooldown:
            _release_browser_probe(session, user_id)
        #: ★ **浏览器成功 = 真人形状的会话是通的** ⇒ 从档案重导凭据。
        #: 2026-10-21:不再只在「冷却中」才重导 —— 纯协议关闭后浏览器是**唯一**取数路,
        #: 库里的 cookie 必须跟着档案走;否则它会越来越旧,下次注入浏览器时
        #: 反而可能用旧会话盖掉档案里的新会话。
        #: 重导失败只告警,绝不把已经拿到的线索作废。
        try:
            from app.services.douyin_cookie_export import export_from_profile

            note = export_from_profile(session, user_id)
            logger.info("抖音浏览器取数成功,已从档案重导凭据:%s", note)
        except Exception as exc:  # noqa: BLE001
            logger.warning("抖音浏览器取数成功,但重导凭据失败:%s", str(exc)[:120])
    return rows, "浏览器兜底"


def collect_leads(keywords: list[str], limit: int = 30, platform: str = "douyin",
                  session=None, user_id: int = 1, pages: int = 1,
                  stats: dict | None = None) -> tuple[list[dict], str]:
    """`find_leads` 的完整版:**连来源标签一起返回**(运行记录要写它)。

    其余行为完全一致 —— `find_leads` 就是它的薄封装。
    """
    if not keywords:
        return [], ""
    # ⚠️ 取数层的硬失败(协议+浏览器**都**挂了 / MediaCrawler 未装/扫码超时)**不吞** ——
    # 让它一路冒到 `douyin_leads_tick` 记 `failed`。否则"扫码没通过"会被记成
    # `success(线索0)`,而这条链**只在每天 11:00 无人值守时跑**,失败你收不到任何信号。
    out: list[dict] = []
    seen: set[str] = set()
    # **发布时间下限**(用户口径:「2026年10月份之前的不要再保存进来了」)。
    # ★ **再加一道"最近 N 天"的新鲜度窗口**(2026-10-29,用户口径「监控的内容我们需要新鲜度,
    #   太老旧的不需要」):两者取**更严**的那个。实测游戏名 + 「下载」能搜到带口令的帖,
    #   但都是 2025-10 / 2026-07 的 —— 只有窗口挡得住它们。
    # ⚠️ **没有发布时间的也跳过** —— 判不了就宁可不收;但**逐条记账、最后打出来**,
    # 绝不静默丢(本仓最忌讳的就是"看着没产出、其实是我们在丢")。
    from config.settings import get_settings

    _s = get_settings()
    min_pub_ts = max(_min_publish_ts(_s), _max_age_ts(_s))
    n_old = n_notime = 0
    rows, source = _collect(platform, keywords, session, user_id=user_id, pages=pages)
    logger.info("线索取数:平台=%s 来源=%s 原始 %d 条", platform, source or "-", len(rows))
    for h in rows:
        text = (h.get("snippet") or "").strip()
        url = (h.get("url") or "").strip()
        if not url or url in seen:
            continue          # 拿不到视频链 / 同一个视频(多词命中)去重
        pub = int(h.get("publish_at") or 0)
        if min_pub_ts:
            if pub <= 0:
                n_notime += 1
                continue
            if pub < min_pub_ts:
                n_old += 1
                continue
        name = h.get("name") or ""
        mark = _lead_mark(text, name)
        if not mark:
            # ⚠️ **夸克口令帖没有《》**(2026-10-06):抖音上的夸克口令长
            # `咐置…叩苓` 或 `/~令牌~/`,而本函数原来只认《》 ⇒ 这类帖**一条都进不来**。
            # 实测搜「夸克口令」一次就捞到 15 条,全被这条判据挡在门外。
            from app.services.quark_kouling import mark_of
            mark = mark_of(text)
        # ★ **正文里直接贴网盘链**也算线索(2026-10-29,用户口径「这些小游戏也是都需要带网盘的」)。
        # 实测搜 `游戏合集 下载`:46 条里 6 条带盘,其中一批是**百度网盘直链**、没有《》/令牌
        # ⇒ 原来会被下面那道 `if not mark: continue` 全部丢掉。
        pan_url = "" if mark else pan_of(text)["pan_url"]
        if not mark and not pan_url:
            continue          # 既没有口令、也没有盘链 → 不是线索
        seen.add(url)
        out.append({"mark": mark, "pan_url": pan_url,
                    "title": text[:120], "url": url,
                    # 账号名。⚠️ **取决于来源**(2026-10-08):纯协议给的是**真实昵称**,
                    # MediaCrawler 那条是**脱敏**的(如「籽***」)—— 推送卡片的"作者列"
                    # 因此可能显示星号;看到星号先看运行记录的来源标签,别以为解析坏了。
                    "author": name,
                    "keyword": h.get("keyword", ""),
                    # 转发量(结算用):衡量**这个资源在抖音有多热**;⚠️ 是**别人视频**的数,
                    # 不等于我们自己发文的转化(见 DouyinLead/lead_settlement 的口径说明)。
                    "share_count": int(h.get("share_count") or 0),
                    "publish_at": int(h.get("publish_at") or 0),   # ★ 帖子发布时间(新鲜度的唯一依据)
                    "aweme_id": _aweme_id(url),         # 去重键(同一视频会被多个词命中)
                    "_rank": _lead_rank(text, name)})
    if min_pub_ts and (n_old or n_notime):
        logger.info("抖音线索:按发布时间过滤掉 %d 条(早于下限)+ %d 条(没有发布时间)",
                    n_old, n_notime)
    out.sort(key=lambda x: (-int(x.get("publish_at") or 0), x["_rank"]))
    # ↑ **越新越靠前**(发布时间倒序),同档内仍按强信号排(开头《》> 与昵称吻合 > 其它)
    for x in out:
        x.pop("_rank", None)
    #: ★ 漏斗数字留给运行记录(2026-10-31):没有它们,"产出少"到底是
    #: **原始量少**、**被时间挡光**还是**没口令**,在库里长得一样。
    if stats is not None:
        stats.update({"raw": len(rows), "old": n_old, "notime": n_notime,
                      "kept": len(out)})
    return out[:limit], source


def find_leads(keywords: list[str], limit: int = 30, platform: str = "douyin",
               session=None, user_id: int = 1) -> list[dict]:
    """搜抖音 → 挑出带**口令**的推广线索(薄封装,保留既有调用方的形状)。

    返回 `[{mark, title, url, keyword}]`;`mark` 是《…》里那段 —— **迅雷分享口令**。
    ⚠️ 抖音现在走**纯协议**(约 1 秒/词),只有协议失败或全空时才回落浏览器(几分钟)。
    想同时拿到**来源标签**,用 `collect_leads`。
    """
    return collect_leads(keywords, limit=limit, platform=platform, session=session,
                         user_id=user_id)[0]


def _max_age_ts(settings) -> int:
    """`douyin_leads_max_age_days` → 最早的允许时间戳(epoch 秒);0 = 不设窗口。

    ★ 2026-10-29 用户口径「监控的内容我们需要新鲜度,太老旧的不需要」。
    与 `_min_publish_ts`(一次性历史门槛)**取更严的那个**。
    ⚠️ 单独成函数是为了**可被测试钉死**(与 `_min_publish_ts` 同一条手法)。
    """
    from datetime import datetime, timedelta

    days = int(getattr(settings, "douyin_leads_max_age_days", 0) or 0)
    if days <= 0:
        return 0
    return int((datetime.now() - timedelta(days=days)).timestamp())


def _min_publish_ts(settings) -> int:
    """`douyin_leads_min_publish_date` → Unix 秒;留空/写坏都返回 0(= 不过滤)。

    ★ 2026-10-06:**实现搬进 `app.services.freshness`(单一事实源)**,这里只做转发。
    起因是公众号那条链**压根没实现这条规则**(审计实测 34 篇 9 月文章照旧入库)——
    同一规则在几条链上各写一遍,必然"修一条、漏一条"。
    """
    from app.services.freshness import min_publish_ts

    return min_publish_ts(settings, specific_key="douyin_leads_min_publish_date")


def _to_search_word(title: str) -> str:
    """群资源标题 → 搜索词:取**主体名**,丢掉括号里的补充说明与版本号。

    实测群里的标题长这样:「手机警报器（警笛模拟器）2.0版」—— 整句丢进抖音搜不到东西,
    「手机警报器」才对;「【全网最齐】游戏软件资源合集」这种**泛化合集名**则整个丢掉
    (搜出来全是噪音,还会把无关内容一起带进来)。

    ⚠️ 泛词表**共用** `xunlei_group.BULK_WORDS` —— 它在转存侧是"不自动搬"的闸门判据,
    在这里是"不当搜索词"的判据,同一件事(这条资源名太泛、指不到具体东西)只该有一份定义。
    """
    from app.services.xunlei_group import is_bulk_resource

    text = re.sub(r"[（(【\[][^)）】\]]*[)）】\]]", " ", title or "")
    #: ★ **把"拆字"合回去**(2026-10-31 实测):群里大量标题写成 `伪丨装丨Z丨男 🔥`、
    #: `顶丨级丨沦丨陷 🔥` —— 中间那些竖线是**防屏蔽的拆字**,拿原串去抖音搜**一条都对不上**
    #: (实测这两个词搜回来的 28 条里,最近的也只有 10-30 的 4 条,其余全是 4~9 月的老帖)。
    #: 同一条资源在抖音上写的是 `伪装Z男` / `顶级沦陷`。
    text = re.sub(r"[丨｜]", "", text)
    #: 顺带剥掉 emoji / 装饰符号(🔥 这类)—— 它们不进搜索词
    text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿️]", " ", text)
    text = re.split(r"[|｜\-—·,，、:：!！?？]", text)[0]
    # 再丢掉**版本号式的尾串**(「2.0版」「v3」「2024版」)—— 它们搜不出东西
    tokens = [t for t in text.split()
              if t and not re.match(r"^[vV]?\d", t) and not t.endswith("版")]
    word = " ".join(tokens).strip()
    if len(word) < 4 or is_bulk_resource(word):
        return ""
    return word[:12]


def promo_search_words(session, user_id: int, top: int = 2, days: int = 7) -> list[str]:
    """**正在被推广的名字** → 下一轮的搜索词(2026-10-31,用户选定「方案 B」)。

    词源 = 我们**最近自己收到的线索**里的《口令》名(`douyin_leads.mark`)。
    为什么这条路对:这些名字是**别人此刻正在抖音上发口令推广的**,拿它们去搜,
    命中推广号的概率远高于"群里存着的老资源名"(历史有效率 86% vs 20%~40%)。

    ⚠️ 必须**按找到时间倒序**:用户口径「新鲜资源优先级大于转存」——
    我们只要"最新冒出来的名字",越新越可能还在被推。
    ⚠️ 名字里的**空白/emoji/书名号**要先清掉(它们是标题语法,不是搜索词的一部分)。
    """
    from datetime import datetime, timedelta

    from app.db.models import DouyinLead

    since = datetime.now() - timedelta(days=max(1, int(days or 7)))
    rows = session.execute(
        select(DouyinLead.mark, DouyinLead.found_at)
        .where(DouyinLead.user_id == user_id, DouyinLead.found_at >= since,
               DouyinLead.mark != "")
        .order_by(DouyinLead.found_at.desc()).limit(max(4, int(top) * 10))).all()
    out: list[str] = []
    for mark, _at in rows:
        w = re.sub(r"[《》「」\s]+", "", str(mark or ""))
        w = re.sub(r"[🌀-🫿☀-➿️]", "", w).strip()
        if len(w) < 2 or w in out:
            continue
        out.append(w)
        if len(out) >= max(0, int(top)):
            break
    return out


def group_keywords(session, user_id: int, top: int = 3, days: int = 7) -> list[str]:
    """**群组里新出现的资源** → 抖音搜索词。

    用户口径(2026-10-02 起,2026-10-04 再次确认):"**搜索词应该是资源名称**" ——
    群里刚冒出来的资源 = **"最近有人在找这个"**,拿它去抖音搜,抓到的正是**正在蹭这波热度的推广号**。

    ⚠️ **自循环的解法不是"把词换成品类"**(2026-10-04 一度这么改,用户当即纠正:
    "不对,搜索词应该是资源名称")—— **品类**那层应该来自**监控里的类目→其下的具体话题**,
    而不是硬编一张词表。真正的解法是**别反复消耗同一个词**:见 `known_word_groups` 的去重。

    只取最近 `days` 天、按消息时间倒序(**新鲜冒头优先**)。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.db.models import XunleiGroupShare

    since = datetime.now() - timedelta(days=days)
    rows = session.execute(
        select(XunleiGroupShare.title, XunleiGroupShare.msg_time)
        .where(XunleiGroupShare.user_id == user_id,
               XunleiGroupShare.title != "")
        .order_by(XunleiGroupShare.msg_time.desc().nullslast(),
                  XunleiGroupShare.id.desc()).limit(top * 6)).all()
    words: list[str] = []
    for title, msg_time in rows:
        if msg_time and msg_time < since:
            continue
        word = _to_search_word(title)
        if word and word not in words:
            words.append(word)
        if len(words) >= top:
            break
    return words


# 热榜里明显不是"可搜的资源"的词:泛词/时事/情绪。**只做便宜且不会误伤的两道**
# (长度 + 这张小表),剩下的交给**搜索结果自己筛** —— 见 `hot_seed_words` 的说明。
_HOT_STOP = {"搞笑视频", "今日金价", "手势舞", "新闻", "直播", "热门", "推荐", "视频"}
_HOT_MAX_LEN = 8          # 作品/资源名一般短;事件句常更长(「张美娥为什么不早说」9 字)


def hot_seed_words(settings, limit: int | None = None) -> list[str]:
    """**外部种子**:抖音热点宝的搜索榜/话题榜 → 当搜索词。取不到就返回 `[]`(绝不抛)。

    **为什么这是"真正的自主发现"**:此前所有种子都来自**我们已知的东西**(群组里的资源名、
    资源库里的资源名)—— 本质是"在已知圈子里向外扩散",起点永远是我们已经知道的。
    热榜词**不来自我们的数据**,它来自"抖音此刻什么火",所以才可能撞见我们没听说过的东西。

    **为什么不写复杂筛选**(2026-10-03):热榜里大量是事件/时事(国足0-5巴勒斯坦、EDG发文道歉),
    想用规则分辨"作品名 vs 事件句"非常容易过拟合;而**搜索本身就是最好的筛子** ——
    搜不出《口令》的词自然沉掉,不需要我们先猜对。代价是每个废词一次搜索(约 90 秒),
    所以 `limit` 要小,且用长度 + 小黑名单挡掉最明显的那批。

    这条链**帮不到**的:它只找**有推广号在发**的资源。热榜词若没人做资源,就是白搜一次。

    ⚠️ **2026-10-05:名额从 3 降到 1,而且第一次有了数据依据**。
    实测(全历史 43 条线索的 `keyword` 归因):

    | 词源 | 线索 | 有产出(新群/新链) | **有效率** |
    |---|---|---|---|
    | 热榜种子(剧名/明星/事件) | 5 | **0** | **0%** |
    | 资源名 | 29 | 25 | **86%** |

    根因:热榜种子是"**大家在聊什么**",而这条链要的是"**谁在推资源**" —— 不是一回事。
    而它每轮占 3/7 个名额(43%)⇒ **约四成搜索预算花在 0% 有效率的词上**。
    ⚠️ 样本只有 5 条,**不代表定论**,所以是**降额 + 单独计量**(不是删掉);
    样本攒够再决定要不要彻底去掉。详见 `doc/抖音线索链-最优策略-2026-10-05.md`。

    ⚠️ `limit` 现在**真的**由 `settings.douyin_leads_hot_keywords` 控制 —— 此前那个设置
    只当开关用(真正常量是这里的默认参数 `3`),改设置不生效,**是个坑**。
    """
    raw = getattr(settings, "douyin_leads_hot_keywords", 0)
    n = int(raw if limit is None else limit)
    if n <= 0 or not raw:
        return []
    from pathlib import Path

    from app.services import douhot

    cookie_file = Path(getattr(settings, "douhot_cookie_file", "data/douhot_cookie.txt"))
    try:
        cookie = cookie_file.read_text(encoding="utf-8").strip()
    except OSError:
        logger.info("热榜种子跳过:读不到抖音热点宝 Cookie(%s)", cookie_file)
        return []
    if not cookie:
        return []
    words: list[str] = []
    for fetch, label in ((douhot.fetch_search_words, "搜索榜"), (douhot.fetch_topic_words, "话题榜")):
        try:
            rows = fetch(cookie, settings)
        except Exception:  # noqa: BLE001 - 热榜拿不到不该拖垮线索链
            logger.warning("热榜种子:%s 取词失败,跳过", label, exc_info=True)
            continue
        for r in rows or []:
            w = str(r.get("title") or r.get("key_word") or r.get("challenge_name") or "").strip()
            if not w or len(w) > _HOT_MAX_LEN or w in _HOT_STOP or w in words:
                continue
            words.append(w)
    out = words[:n]
    if out:
        logger.info("热榜种子(外部输入):%s", "、".join(out))
    return out


# ---- 词级产出评分(2026-10-05,P1) --------------------------------------------
#
# **为什么现在才做**:`douyin_leads.keyword` 从 10-03 起就**每行都填**(实测 43/43),
# 也就是说"**每个词产出了什么**"一直在库里 —— **却从来没用来选过词**。
# 排序依据一直是"新鲜度"(`order="fresh"`),那条规则定于 10-04,**当时还没有产出数据**。
# 现在让它说话。
_YIELD_PRIOR = 0.5      # 无样本时的中性值(既不高看也不低看)
_YIELD_K = 3.0          # 收缩强度:**相当于先验算 3 次**
_VARIANT_MIN_SCORE = 0.6   # 母词达到这个分才值得深挖它的变体
_VARIANT_DISCOUNT = 0.9    # 变体继承母词证据时打的折(同资源、不同写法)
_VARIANT_MAX = 2           # 一轮最多为几个母词生成变体(名额有限,别铺开)
# 推广后缀:剥掉它们得到"**同资源的核心名**"
_VARIANT_SUFFIX = re.compile(
    r"(自定义|入口|链接|直达|自取|免费下载|可保存|最新教程|获取最新教程|附链接|附最新链接|教程)+$")


def _norm_word(w: str) -> str:
    """词的**归一化形式**:小写、去掉空白与标点。

    ⚠️ **为什么查分前要先归一**:同一个资源在库里常有好几种写法,实测
    `高性价比人生指南pdf` / `高性价比人生指南PDF` 就是同一个词的两种大小写形态 ——
    **按原串查分,后面那种就白白丢掉前面攒的证据**。
    (汉字是 `\\w`,不会被 `\\W` 去掉;只吃空白与标点。)

    ⚠️ **它桥不过"词中间多几个字"**:`高性价比人生指南共338页pdf` 与
    `高性价比人生指南pdf` 归一化后仍不同 —— 那属于"同一个资源的两种叫法",
    本函数**有意不猜**(乱匹配会把 A 的分数安到 B 头上,比漏认更坏)。
    """
    return re.sub(r"[\s\W_]+", "", str(w or "").lower())


def keyword_yield(session, user_id: int) -> dict[str, tuple[int, int]]:
    """`{词: (线索数, 其中有产出的)}` —— 有产出 = `kind` 是 `group` 或 `share`。

    ⚠️ **`kind="none"` 要算作零产出**:它的意思是"搜到了抖音视频,但标题里没有《口令》"
    —— 搜了个寂寞。本仓的"线索数"把它和真产出混在一个数里,是**误导指标**
    (运行记录里已经拆开,见 `_kouling_summary`)。
    """
    from sqlalchemy import func, select

    from app.db.models import DouyinLead

    rows = session.execute(
        select(DouyinLead.keyword, func.count(DouyinLead.id),
               func.sum(func.iif(DouyinLead.kind.in_(("group", "share")), 1, 0)))
        .where(DouyinLead.user_id == user_id, DouyinLead.keyword != "")
        .group_by(DouyinLead.keyword)).all()
    return {str(k): (int(n or 0), int(ok or 0)) for k, n, ok in rows if k}


def keyword_score(n: int, ok: int) -> float:
    """**小样本收缩后**的有效率。

    ⚠️ **为什么不直接用 `ok/n`**:5 次里蒙中 1 次就是 20%,而 29 次里中 25 次是 86%
    —— 前者样本太小、不该当结论。加个"先验算 K 次、先验值 0.5"再算:
      · 没搜过 → `0.5`(中性,不偏袒也不排挤)
      · 0/2    → `0.30`(**低于**没搜过的词 ⇒ 自然"冷却",不用另写规则)
      · 0/5    → `0.19`(样本越多、越像真废词 ⇒ 降得越多)
      · 25/29  → `0.83`(与实测 86% 吻合)
    ⇒ **"零产出降权"是这条公式的自然结果,不需要单独一套规则**(P1 第 2 项)。
    """
    return (ok + _YIELD_PRIOR * _YIELD_K) / (n + _YIELD_K)


def _variants(word: str) -> list[str]:
    """高产出词的**核心名**(剥掉「入口/直达/附链接」这类推广后缀)。

    **为什么值得搜核心名**(2026-10-05,P2):推广号做同一批资源**往往一次做一批**,
    换个写法就是另一个视频/另一个《口令》。实测 `霸王茶姬杯贴自定义入口链接直达` 有效率
    **100%**、`高性价比人生指南共338页pdf` **78%** —— 它们背后的资源**还在被继续推**,
    而"核心名"通常也是**别的推广号正在用的**搜索词。

    ⚠️ **这是"探索"的正确方向**:沿着**已验证的方向**探,而不是沿着类目探。
    ⚠️ 剥完不足 4 字就放弃(太短的词搜出来全是噪声);没变化也返回空(不重复搜同一个词)。
    """
    core = _VARIANT_SUFFIX.sub("", str(word or "")).strip()
    return [core] if 4 <= len(core) < len(str(word or "")) else []


def _words_already_resolved_to_group(session, user_id: int, days: int = 21) -> set[str]:
    """近 `days` 天**已经解析出过群**的搜索词 —— 下轮把它们排到最后。

    用户口径(2026-10-04):"**而不是一直用着一个口令进群,我们是需要创新的**"。
    同一个词反复解析出群,说明它指向的就是**那一批**群;而抖音搜索**次数有限、每轮又慢**
    (一次浏览器几分钟),把名额优先给**没试过的词**,才谈得上往外扩。

    ⚠️ **只"排后"不"删掉"**:这个词将来还可能解析出**别的**群(推广号会换口令),
    硬删会把这条路堵死 —— 正如本项目反复踩过的"把能搬的链判成终态"那种反向错误。
    **数据来源不新建表**:`DouyinLead` 已经存了 `keyword` + `kind`,查得出来就别再造一张。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.db.models import DouyinLead

    since = datetime.now() - timedelta(days=days)
    rows = session.execute(
        select(DouyinLead.keyword).where(
            DouyinLead.user_id == user_id,
            DouyinLead.kind == "group",
            DouyinLead.keyword != "",
            DouyinLead.found_at >= since).distinct()).all()
    return {str(k) for (k,) in rows}


#: 资源库里**不是游戏名**的条目(文件夹占位/泛称)—— 当搜索词毫无意义。
#: ⚠️ 这类行的**父目录名才是游戏名**(如 `赛博朋克2077(豪华版全DLC)/游戏本体`),见下。
_GAME_NAME_JUNK = ("游戏本体", "更多好玩的游戏", "游戏位置", "游戏BUG", "游戏合集",
                   "QQ群", "群文件",
                   #: 游戏目录里的**非游戏**条目(实测这些出现在"X开头游戏"下面)
                   "手机想玩", "附件", "升级档", "补丁", "教程", "说明", "安装",
                   "解压", "存档", "修改器")
#: 游戏名后面常挂的**说明后缀**,搜之前要剥掉(实测带后缀的长名字搜不到资源帖)。
_NAME_SUFFIX = re.compile(
    r"(最新汉化版|汉化版|手机版|电脑端|安装包|单机版|手游|手机|端|免安装版|破解版)$")
#: 剧集形态的排除(`《猎心游戏》11` / `狩猎游戏完结`)—— 它们是**短剧**,不是游戏资源。
_DRAMA_LIKE = re.compile(r"完结|第\s*\d+\s*集|\d+\s*集|全集")


#: **不是资源名**的通用/公告/目录标签(2026-10-30 实测:它们会被当成搜索词)。
_WORD_JUNK = ("简介", "使用说明", "使用方法", "二维码", "请勿相信", "更新一次",
              "寻找资源不易", "更多好玩的游戏", "均更至最新集数",
              "资源内的联系", "防失联", "公告", "群公告", "免责声明", "注意事项",
              "切勿", "如果资源", "和谐", "同步更新", "凌晨", "卸载网盘", "教程")


def _search_word_for(cat: str, name: str, parent: str) -> str:
    """资源名 → 该**方向**的搜索词;不适合当词返回空串。

    · 小游戏 / 软件:清理后加「下载」(实测 `<游戏名> 下载` 才搜得到资源帖);
    · 影视 / 资料 / 大瓜:直接用清理后的名字(剧名/资料名本身就是推广号标题里的词)。
    ⚠️ 小游戏只认**游戏目录**(父目录含 游戏/手机和电脑/手游…):否则 `《猎心游戏》11`
      这种**剧名带"游戏"**会被当成游戏名(2026-10-29 实测踩过)。
    """
    src = str(name or "").strip()
    par = str(parent or "").strip()
    if any(j in src for j in _WORD_JUNK):
        return ""
    if cat == "小游戏":
        if any(j in src for j in _GAME_NAME_JUNK):
            #: 占位条目(`游戏本体`)⇒ 父目录才是游戏名;父目录是"X开头游戏"这种分类名就跳过。
            if not par or "开头游戏" in par or par in _GAME_NAME_JUNK:
                return ""
            src = par
        elif not any(h in par for h in ("游戏", "手机和电脑", "手游", "全系列版本", "所有版本")):
            return ""
    core = re.sub(r"[《》【】\[\]]", " ", src)                 # 去掉书名号/方括号本身
    core = re.split(r"[（(]", core)[0].strip()
    core = re.sub(r"^[A-Za-z]{1,3}(\s*[-－]\s*|\s+)", "", core).strip()   # `Z 咒语旅团` / `E-恶搞之家`
    core = _NAME_SUFFIX.sub("", core).strip()
    #: 文件名后缀 / 尾部集数(`《某剧》75` → `某剧`)
    core = re.sub(
        r"\.(mp4|mkv|avi|mov|docx?|pdf|rar|zip|7z|png|jpe?g|webp|gif|bmp|txt)$", "",
        core, flags=re.I).strip()
    core = re.sub(r"\s+\d+$", "", core).strip()
    if len(core) < 2 or len(core) > 20 or any(p in core for p in "！。！?"):
        return ""
    if cat in ("小游戏", "软件"):
        #: 版本号/整合包/本体/剧集形态 都**不是资源名**,搜了只会撞无关结果。
        if (_DRAMA_LIKE.search(core) or re.match(r"^v?\d", core)
                or any(j in core for j in ("版本", "整合包", "本体", "MOD", "mod", "重制版"))):
            return ""
        if not re.search(r"[一-鿿]", core):               # 至少含中文
            return ""
        return f"{core} 下载"
    #: 影视/资料/大瓜:名字本身就是词;纯数字/英文多半是噪音。
    if not re.search(r"[一-鿿]", core):
        return ""
    return core


#: 每个方向**可信的文件夹**(2026-10-30):只有父目录属于这个方向的目录,里面的名字才当词。
#: ⚠️ **不能只看名字分类**:短剧标题里的「系统/指南/离婚」会把剧判成软件/资料/大瓜
#: (实测取出来的是 `末世求生却绑定了dm系统 下载`、`幸福指南 完结`、`没有用的离婚协议`)。
_CAT_PARENT_HINTS: dict[str, tuple[str, ...]] = {
    "小游戏": ("游戏", "手机和电脑", "手游", "全系列版本", "所有版本"),
    #: ⚠️ **不含「模拟器」**:`手机玩电脑游戏的模拟器` 目录里装的是**游戏**,
    #: 放在软件方向会把一批游戏当软件(实测 樱花校园模拟器)。
    "软件": ("软件", "解压", "安装教程", "工具"),
    "影视": ("漫剧", "短剧", "剧集", "热门ai", "男频", "女频", "深夜档", "国漫",
             "动漫", "系列", "合集》", "摆烂"),
}


def category_search_words(session, user_id: int, cat: str, top: int = 3) -> list[str]:
    """从资源库**按方向**取资源名当搜索词(2026-10-30)。

    用户口径:「影视、软件下载、游戏下载,等别的网盘方向」都要能被监测到。
    之前每轮都用**同一批最近资源名**(几乎全是剧名)⇒ 轮到大瓜/资料/软件时,
    搜出去的词仍是影视名。现在按 `category_topics.type_map` 判出的**方向**取该方向的资源名。
    ⚠️ 方向供给天然不均(资源库:影视 1112 / 小游戏 318 / 软件 81 / 资料 10),
    不够时 `pick` 仍会用它自己的话题词兜底。
    """
    from sqlalchemy import desc, or_

    from app.db.models import XunleiResource
    from app.services import category_topics

    budget = max(0, int(top or 0))
    if budget <= 0:
        return []
    hints = _CAT_PARENT_HINTS.get(cat)
    if not hints:
        #: 资料/大瓜/问卷没有可信的"库内目录" ⇒ 不硬凑,交给 `pick` 的话题词兜底。
        return []
    #: ⚠️ **在 SQL 里按父目录过滤**:不然 limit 800 会被最近同步的剧名占满,
    #: 老的游戏目录(10-07 同步)根本查不到(实测第一版就是这么返回空的)。
    conds = [XunleiResource.parent_name.like(f"%{h}%") for h in hints]
    if cat == "小游戏":
        #: 占位条目(`游戏本体`)的父目录往往**不含"游戏"**(如 `赛博朋克2077(豪华版全DLC)`)——
        #: 它们是游戏,要一起选进来,再用父目录名当词(与旧 `game_search_words` 一致)。
        conds.append(XunleiResource.name.in_(("游戏本体", "更多好玩的游戏", "游戏位置")))
    rows = session.execute(
        select(XunleiResource.name, XunleiResource.parent_name).where(
            XunleiResource.user_id == user_id, or_(*conds))
        .order_by(desc(XunleiResource.synced_at)).limit(800)).all()
    types = category_topics.type_map(
        [(str(n or ""), str(p or "")) for n, p in rows])
    out: list[str] = []
    for name, parent in rows:
        nm = str(name or "")
        if types.get(nm) != cat:
            continue
        if not any(h in str(parent or "") for h in hints):
            #: ⚠️ 小游戏的**占位条目**(`游戏本体`)父目录往往不含"游戏" —— 交给
            #: `_search_word_for` 用父目录名当词,别在这里拦掉。
            if not (cat == "小游戏" and any(j in nm for j in _GAME_NAME_JUNK)):
                continue
        word = _search_word_for(cat, nm, str(parent or ""))
        if word and word not in out:
            out.append(word)
        if len(out) >= budget:
            break
    return out


def game_search_words(session, user_id: int, top: int = 3) -> list[str]:
    """小游戏的搜索词(`<游戏名> 下载`)—— `category_search_words` 的薄壳。"""
    return category_search_words(session, user_id, "小游戏", top=top)


def search_keywords(session, user_id: int, top: int, settings,
                    hot: list[str] | None = None) -> list[str]:
    """本轮抖音反查用的搜索词 = **资源库词**(新鲜优先) + **热榜外部种子**。

    **方向是单向的**(用户 2026-10-04 逐字口径):"**群组只有通过口令进入,而口令就是从
    搜索资源里面获得**"、"**搜索词应该是资源名称**" ——
        资源(词) → 抖音搜 → 口令 → 进群
    ⚠️ **群资源确实在当词源**(`douyin_leads_group_keywords`,实测值 **3**)。
    这里原来写着"群是目的地,不是词源;所以默认 **0**" —— **那句是假的**:
    代码默认是 3、`.env` 也没覆盖,群资源一直在参与出词。
    (2026-10-04 一度想默认关掉,用户当即纠正:"**你抖音搜索就跟着群里面的资源名字走,
    结合资源库里面的名称**";自循环的真正解法是**按类目轮换**,不是不用群里的名字。)
    方向确实是单向的:**群是目的地,词来自资源名** —— 但"资源名"里**包含**群里的那些。

    `hot` 由 `hot_seed_words()` 取好传进来,**不在这里取**:那是网络调用,而本函数是**纯 DB**
    的(`pan_discovery` 也用它),别让一个本地函数偷偷出网。

    `top` 只管**已知词**那条路的额度;热榜种子**额外附加**在末尾 —— 抖音搜一个词只要几秒
    (实测 3 个词一轮 83 秒),没必要让外部种子和已知词互相挤位置。

    ⚠️ **按类目轮换出词**(2026-10-04,见 `category_topics`):本轮只出**当前类目**的词,
    下一轮换一类 —— 广度由**轮换**保证,而不是"永远搜最热的那个"。
    **轮换游标的推进放在 `douyin_leads_tick` 里**(不在这里),免得别的调用方(如 `pan_discovery`)
    也跟着推进、把类目跳掉。

    ⚠️ **已经解析出过群的词排到最后**(`_words_already_resolved_to_group`):抖音搜索次数有限、
    每轮又慢,名额要优先给**没试过的词** —— 否则就是"一直用着一个口令进群"。

    ⚠️ **不放品牌词**(2026-10-02 用户澄清):这条链的目的是"**发现新资源**"",
    不是"看谁在提我们的牌子";品牌词是用在**推送侧**把别人的名字换成我们的(见
    `_rebrand`),不是用来搜的。
    """
    from app.services import category_topics
    from app.services.cross_accounts import _keywords_from_library

    cat = category_topics.current_category(session)
    #: ★ **小游戏轮先把资源库里的游戏名当主体词**(2026-10-29,见 `game_search_words`):
    #: 实测裸游戏名搜不到带盘帖,`<游戏名> 下载` 才有。占掉至多 3 个名额。
    #: ★ **每个方向都从资源库取自己的资源名**(2026-10-30,用户「影视/软件下载/游戏下载等方向」):
    #: 之前只有小游戏有专属词,其它方向轮到时搜的仍是同一批剧名(`类目大瓜 → 线索13 全是影视`)。
    #: ★ **库内词只占 1 个名额**(2026-10-31):这些名字多是**老资源**,搜回来的帖子自然老,
    #: 被 7 天新鲜度挡光(实测 04:11 轮 38 条挡 33 条)。名额还给"群组新资源名"。
    lib_n = int(getattr(settings, "douyin_leads_library_words", 1) or 0)
    own_words = category_search_words(session, user_id, cat,
                                      top=max(0, min(lib_n, int(top))))
    n_group = int(getattr(settings, "douyin_leads_group_keywords", 3) or 0)
    # 先取**全部**候选(群里的资源名 + 资源库名称),再按类目挑 —— 挑不够会自动补本类目话题词
    cand = group_keywords(session, user_id, top=n_group)
    for w in _keywords_from_library(session, user_id, max(1, int(top))):
        if w not in cand:
            cand.append(w)
    # ---- 词级产出评分(2026-10-05) ----
    # 数据本来就在库里(`douyin_leads.keyword` 43/43 全填),只是从没用来选过词。
    yld = keyword_yield(session, user_id)
    # ⚠️ 按**归一化**形式合并后再查:同一资源常有好几种写法(如 `…pdf` / `…PDF`),
    # 按原串查会让后一种白丢证据。见 `_norm_word`。
    by_norm: dict[str, tuple[int, int]] = {}
    for w, (n, ok) in yld.items():
        k = _norm_word(w)
        pn, pok = by_norm.get(k, (0, 0))
        by_norm[k] = (pn + n, pok + ok)
    scores = {w: keyword_score(*by_norm.get(_norm_word(w), (0, 0))) for w in cand}
    # 高产出词的**核心名**也进候选 —— 沿着已验证的方向探(见 `_variants`)
    for w in sorted([x for x in cand if scores[x] >= _VARIANT_MIN_SCORE],
                    key=lambda x: -scores[x])[:_VARIANT_MAX]:
        for v in _variants(w):
            if v not in scores:
                scores[v] = scores[w] * _VARIANT_DISCOUNT   # 同资源、不同写法 ⇒ 继承证据
                cand.append(v)
    # 同档内高分在前:`pick` 保持输入顺序,所以这一步决定了**同类里谁被选中**
    cand.sort(key=lambda w: -scores.get(w, _YIELD_PRIOR))
    # 学到的"人名"是**弱信号**(见 `category_topics.classify`);顺手从明显的瓜词里继续学
    names = category_topics.known_names(session)
    for w in cand:
        if category_topics.classify(w) == "大瓜":
            category_topics.learn_names(session, w)      # 用户口径:"大瓜**慢慢的学习**可以"
    #: ★ **同批次类型覆盖**(2026-10-29 用户「可以开始」):资源库里的裸剧名单独看判不出,
    #: 但**同目录兄弟条目的多数类型**就是答案(`最全文件` 下 83/87 是影视)。
    #: 这里把库里的名字按 `type_of(名字, 父目录, siblings=同目录)` 判一次,交给 `pick` ——
    #: 于是「影视轮」会优先出剧名、「小游戏轮」优先出游戏名,而不是把裸名当"未分类"混进去。
    _cat_of = None
    try:
        from app.db.models import XunleiResource

        #: ⚠️ 用**全库**算 `type_map`(同批次多数需要全部兄弟),只取候选词的映射。
        _rows = session.execute(select(XunleiResource.name, XunleiResource.parent_name).where(
            XunleiResource.user_id == user_id).limit(2000)).all()
        if _rows:
            _types = category_topics.type_map(
                [(str(n or ""), str(p or "")) for n, p in _rows])
            _cat_of = lambda w: _types.get(w) or category_topics.classify(w, names)
    except Exception:  # noqa: BLE001 - 覆盖失败就用旧口径,别让出词整条崩
        logger.debug("资源库类型覆盖失败,退回纯名字判据", exc_info=True)

    kws = category_topics.pick(
        cand, cat, max(0, int(top) - len(own_words)), names,
        # ★ 本类目**保底几个词**(见 `pick` 的 `min_mine`)。
        # ⚠️ **没有库内资源名的方向(资料/问卷/大瓜)保底 2 个话题词**(2026-10-30 用户
        # 「问卷和大瓜不用去掉」):只给 1 个时,这一轮实际搜的仍是别的方向的资源名。
        min_mine=max(int(getattr(settings, "douyin_leads_category_min_words", 1) or 0),
                     2 if not own_words else 0),
        cat_of=_cat_of)
    if own_words:
        #: 本方向的资源名排在前面(它们才是这一轮要找的东西),其余词补足名额。
        kws = own_words + [w for w in kws if w not in own_words]
    # 两个判据都要,**顺序不能反**:
    #   ① `seen`(已经解出过群)排后 —— 用户口径:「而不是一直用着一个口令进群」;
    #   ② 有效率(高→低) —— 同类之间的排序依据。
    seen = _words_already_resolved_to_group(session, user_id)
    kws.sort(key=lambda w: (w in seen, -scores.get(w, _YIELD_PRIOR)))
    if own_words:
        #: ⚠️ 上面那条 sort 按**历史有效率**重排,而本方向词没有历史 ⇒ 会被挤到后面。
        #: 但这一轮**就是为它们来的** ⇒ 排回最前(总词数 ≤ CDP 上限 8,不会互相挤掉)。
        kws = [w for w in own_words if w in kws] + [w for w in kws if w not in own_words]
    for w in (hot or []):
        if w and w not in kws:
            kws.append(w)
    # ⚠️ **夸克口令专搜词**(2026-10-06):上面的词都来自**我们自己的资源库**,
    # 搜出来的自然是"我们已有的"、而且**几乎全是迅雷形态**;实测补上这几个词后
    # **一次捞到 15 条夸克口令帖** —— 它们此前**一条都进不来**(`find_leads` 只认《》)。
    # ★ **2026-10-29 改:限量 + 跟着类目轮换**。原来这三个词**每轮无条件全部追加**
    #   (4 个类目词 + 1 热榜 + 3 夸克 = 8 词里的 3 个),而它们**本身不携带赛道信息**,
    #   搜回来的几乎全是短剧推广(实测 `咐置` 一个词 204 条线索)→ 用户报的
    #   「每轮推送基本都是短剧相关的」。现在每轮只取 `douyin_leads_quark_slots` 个,
    #   且**起点跟着类目游标走** ⇒ 几个词轮流出现,不是「夸克口令」永远霸着名额。
    # ★ **正在被推广的名字排最前**(2026-10-31,方案 B):它们是"此刻有人在发"的,
    #: 比群里的老资源名更可能搜到**正在推广**的内容;用户口径「新鲜优先于转存」。
    promo = promo_search_words(
        session, user_id,
        top=int(getattr(settings, "douyin_leads_promo_words", 2) or 0),
        days=int(getattr(settings, "douyin_leads_promo_days", 7) or 7))
    if promo:
        keep = max(0, int(top) - len(promo))
        kws = promo + [w for w in kws if w not in promo][:keep]
    qs = [w.strip() for w in
          str(getattr(settings, "douyin_leads_quark_keywords", "") or "").split(",")
          if w.strip()]
    slots = max(0, int(getattr(settings, "douyin_leads_quark_slots", 1) or 0))
    if qs and slots:
        start = category_topics.current_index(session) % len(qs)
        for w in (qs[start:] + qs[:start])[:slots]:
            if w not in kws:
                kws.append(w)
    #: ★ **按归一形态去重**(2026-10-31):列表里同时出现过 `D 顶级C距1` 与 `D顶级C距1`
    #: —— 同一资源的不同写法各占一个名额,等于白烧一次搜索。保留**先出现的那个**
    #: (新鲜/高产的排在前,见上面的排序)。
    _uniq: dict[str, str] = {}
    for w in kws:
        _uniq.setdefault(_norm_word(w), w)
    kws = list(_uniq.values())
    return kws


def _enqueue_later(session, user_id: int, info: dict, mark: str,
                   publish_at: int = 0) -> None:
    """**把"能搬、但现在搬不了"的链入队**,等条件好了再重试(2026-10-04 加)。

    用户口径:"**可以加上**"(指给"未搬"加待办队列)。场景正是他自己遇到的:
    抖音那轮 **5 条全卡在盘满**,而盘一清出来 —— **没有任何机制会去重搬它们**,
    只能等"同一个视频再次被搜到",纯属碰运气。

    ⚠️ **不新建表、不新增作业**:复用 `DiscoveredPanLink`(公开发现链那张)——
    它本来就有 `pending/ok/skipped` 的状态机,而 `pan_discovery.sync` **现在优先重试存量待办**。
    这就是"待办队列"该有的样子:**一处状态机,两条链共用**。
    """
    from app.db.models import DiscoveredPanLink

    url = str(info.get("share_url") or "").strip()
    if not url:
        return
    #: ★ **顺手把发帖时间带进来**(2026-10-18):这张表的行此前 **100% 没有 `publish_at`**
    #: (平台都是 `douyin`),于是「只搬新的」对它们**完全不起作用** —— 而手上这条线索本来就有时间。
    from app.utils.timeutil import to_dt

    _pub = to_dt(publish_at)
    row = session.scalar(select(DiscoveredPanLink).where(
        DiscoveredPanLink.user_id == user_id, DiscoveredPanLink.origin_url == url))
    if row is None:
        session.add(DiscoveredPanLink(
            user_id=user_id, platform="douyin", origin_url=url[:500],
            title=str(mark)[:255], status="pending", publish_at=_pub,
            message="抖音线索:转存未成(盘满/超额度),等下一轮重试"))
    elif row.status not in ("ok", "skipped"):
        row.status = "pending"
        row.message = "抖音线索:转存未成,等下一轮重试"
        if _pub and not row.publish_at:      #: 只往有值的方向写(不知道别拿空值盖掉已知的)
            row.publish_at = _pub
    session.commit()


def _moved_link_of(session, user_id: int, ld: dict) -> str:
    """**这条线索以前搬成过没有** —— 按 `aweme_id` 查本地库,返回我方分享链(没有给空串)。

    ★ 为什么必须有这一步(2026-10-14 用户口径:「**有一个解开的就用哪个解开的链接**」
    「不必说那个解开哪个没解开」):夸克那条链(`quark_kouling`)跑在**远程**(它要签名服务),
    它的成果由 `quark_link_pull` 同步回这一列;而本地的 `known_kouling_links` **只认迅雷口令**
    ⇒ 不查库的话,夸克口令那些行会一直显示「没有链」,哪怕**我们早把它搬进盘了**
    (实测:远程已搬成 236 条 vs 本地只看得到 64 条)。
    """
    from app.db.models import DouyinLead

    aid = str(ld.get("aweme_id") or "").strip()
    if not aid:
        return ""
    row = session.scalar(select(DouyinLead).where(
        DouyinLead.user_id == user_id, DouyinLead.aweme_id == aid))
    return str(getattr(row, "our_url", "") or "")


def apply_kouling(leads: list[dict], session, user_id: int, settings) -> list[dict]:
    """把线索里《…》包的口令**真的变成资源**(2026-10-02):解析 → 转存入库 / 加群。

    **为什么默认开**:此前这条链断在"口令 → 分享 id",线索只能推给人、由人去 App 里搜。
    现在 `xunlei_kouling` 把口令直接解成**网盘分享链**(可直接转存)或**群邀请**(加群后由
    群采集轮收),所以"抖音发现 → 加进网盘 → 转存"可以真的全自动。

    **两道闸门**:① 每轮最多真转存 `douyin_leads_transfer_limit` 条(转存慢且占盘),
    超出的**只解析**、把结果写进卡片由人决定;② 已经转存过的词(`xunlei_resources` 里
    `parent_name="口令解析"` 的那些)直接跳过,不重复搬。
    """
    from app.services import xunlei_kouling as kk

    if not getattr(settings, "douyin_leads_auto_transfer", True):
        for ld in leads:
            ld["kouling"] = {"kind": "off"}
        return leads
    budget = int(getattr(settings, "douyin_leads_transfer_limit", 3) or 0)
    already = kk.known_koulings(session, user_id)
    # ⚠️ 光有"搬过没"不够,还要"搬成了哪条"(见 `known_kouling_links` 的 docstring)
    already_links = kk.known_kouling_links(session, user_id)
    down: list[str] = []          # 凭据级失败(如 refresh token 失效):影响全部口令,只报一次
    for ld in leads:
        # ★ **先做一次资源库匹配**(2026-10-11 用户口径「我们资源库里面大部分应该就有这些资源」)。
        #   放在口令解析**之前**,而且**每条线索都做** —— 因为不管口令解没解出来,
        #   「库里有没有现成的」都决定卡片能不能点得动。匹配不上是 `None`(不影响原流程)。
        ld["library"] = _library_link(session, user_id, str(ld.get("title") or ""))
        # ★★ 2026-10-14:**先看这条以前搬成过没有**(按 `aweme_id` 查库)。
        #   用户口径「有一个解开的就用哪个解开的链接」⇒ 只要能拿到的链,**一律先用**:
        #   ① 本地链已落库(迅雷那条)② **远程夸克链**同步回来的(见 `quark_link_pull`)
        #   ③ 资源库里现成的那条(`_library_link`,卡片里作次选)。
        #   ⚠️ 放在"解口令"之前是有意的:能拿到链就**不再去解、不再去花转存额度**。
        _moved = _moved_link_of(session, user_id, ld)
        if _moved:
            ld["kouling"] = {"kind": "share", "status": "already",
                             "our_url": _moved, "share_url": ""}
            continue
        mark = (ld.get("mark") or "").strip()
        if not mark:
            continue
        if mark in already:
            # ⚠️ **必须带上已有的我方链**(2026-10-05 修):原来这里只写 `status=already`,
            # 于是"搬过"记下了、**"搬成了哪条链"丢了** ⇒ `douyin_leads.our_url` 恒空
            # (43 条线索、0 条有链)—— 而那个字段 2026-10-04 加出来**就是为了**回答
            # "这个口令到底搬没搬成"。**只说"做过"不说"做成了什么"的判据,等于把结果丢了。**
            ld["kouling"] = {"kind": "share", "status": "already",
                             "our_url": already_links.get(mark, ""),
                             "share_url": ""}
            continue
        try:
            info = kk.resolve(mark)
        except Exception as exc:  # noqa: BLE001 - 单条解不出不该炸掉整轮线索
            # ⚠️ **2026-10-03 实测踩到**:迅雷 refresh token 失效时 `_headers()` 直接抛,
            # 而这里原本没有兜底 → 整轮 `douyin_leads` 记 failed,**连卡片都推不出去**。
            # 可是"发现线索"和"能不能转存"是两件事:转存挂了,线索本身仍然有值(人要看的)。
            # 所以这里兜住,把原因写进条目,卡片照推(会显示"⚠️未解析")。
            ld["kouling"] = {"kind": "error", "message": str(exc)[:80]}
            down.append(str(exc)[:60])
            continue
        if info["kind"] == kk.KIND_NONE:
            ld["kouling"] = {"kind": "none"}
            continue
        if info["kind"] == kk.KIND_GROUP:
            try:
                res = kk.ingest(session, user_id, mark)
            except Exception as exc:  # noqa: BLE001 - 同上,别让一条炸掉整轮
                ld["kouling"] = {"kind": "error", "message": str(exc)[:80]}
                down.append(str(exc)[:60])
                continue
            ld["kouling"] = {"kind": "group", "status": res.get("status"),
                             "group_id": info["group_id"],
                             # 见 `xunlei_kouling.ingest`:区分"新加了个群"与"口令指向已有的群"
                             "newly_joined": bool(res.get("newly_joined"))}
            continue
        if budget <= 0:
            # ⚠️ **超额度也要入队**:不然它只是"这轮没搬",下一轮同样不会自动补
            _enqueue_later(session, user_id, info, mark,
                           publish_at=int(ld.get("publish_at") or 0))
            ld["kouling"] = {"kind": "share", "status": "over_budget",
                             "share_url": info["share_url"]}
            continue
        budget -= 1
        try:
            res = kk.ingest(session, user_id, mark)
        except Exception as exc:  # noqa: BLE001
            ld["kouling"] = {"kind": "error", "message": str(exc)[:80]}
            down.append(str(exc)[:60])
            continue
        if res.get("status") in ("disk_full", "failed"):
            # **盘满 / 单次失败** → 入队,等条件好了由 `pan_discovery` 的存量待办优先重试
            _enqueue_later(session, user_id, info, mark,
                           publish_at=int(ld.get("publish_at") or 0))
        ld["kouling"] = {"kind": "share", "status": res.get("status"),
                         "our_url": res.get("our_url") or "",
                         "share_url": info["share_url"],
                         "message": res.get("message") or ""}
    if down:
        # 同一条原因会重复 N 次 → 只报一次,并说清"是凭据挂了,不是线索没价值"
        logger.warning("口令解析/转存本轮失败 %d 条,原因:%s(线索照推,只是没自动搬)",
                       len(down), down[0])
    return leads


def _save_leads(session, user_id: int, leads: list[dict]) -> int:
    """线索**落库**(按 `aweme_id` 幂等 upsert),供结算归因。

    ⚠️ **为什么必须存**:`share_count` 只在**抓取那一次**有效 —— 不存下来,一周后就再也
    算不出"本周发现的线索总量级"了(见 `DouyinLead` 的注释)。
    没有 `aweme_id` 的跳过:去重键缺了就只能靠 URL 硬碰,不如不落(宁缺勿假)。
    """
    from datetime import date, datetime

    from app.db.models import DouyinLead

    today = date.today().isoformat()
    n = 0
    for ld in leads:
        aid = str(ld.get("aweme_id") or "").strip()
        if not aid:
            continue
        row = session.scalar(select(DouyinLead).where(
            DouyinLead.user_id == user_id, DouyinLead.aweme_id == aid))
        if row is None:
            row = DouyinLead(user_id=user_id, aweme_id=aid, found_date=today)
            session.add(row)
        _pub = int(ld.get("publish_at") or 0)
        if _pub > 0:
            try:
                row.publish_at = datetime.fromtimestamp(_pub)
            except (OverflowError, OSError, ValueError):
                pass          # 时间戳离谱就留空,别让一条脏数据炸掉整轮
        row.mark = str(ld.get("mark") or "")[:64]
        row.title = str(ld.get("title") or "")[:255]
        row.author = str(ld.get("author") or "")[:64]
        row.url = str(ld.get("url") or "")[:500]
        row.keyword = str(ld.get("keyword") or "")[:64]
        row.share_count = int(ld.get("share_count") or 0)
        row.kind = str((ld.get("kouling") or {}).get("kind") or "")[:16]
        # **这条线索搬成了哪条链**(2026-10-04 补):原来落库时丢了,事后查不出来
        had_link = bool(str(row.our_url or ""))      # ⚠️ 赋值**之前**取,见 mark_newly_moved
        row.our_url = str((ld.get("kouling") or {}).get("our_url") or "")[:500]
        # **首次搬成时刻**(2026-10-07):只覆盖"首采即搬成"的那些;后续轮次补搬的走
        # `quark_kouling`,那一处另有打戳(两处都要,否则一半样本没有时延)。
        mark_newly_moved(row, had_link)
        n += 1
    return n




#: `咐置 X 叩苓` / `/咐置 X 叩苓` —— **变体字**("复制 X 口令",`咐置`/`叩苓`
#: 是「复制」「口令」的同形替换,用来躲抖音的文本审核)。★ **X 才是资源名**。
_RE_BIANTI = re.compile(r"咐置\s*([^，。！?、\s|/]{2,20}?)\s*叩苓")
#: `《X》` —— 最常见的形态,直接就是资源名/群名。
_RE_BOOK = re.compile(r"《([^》]{2,24})》")
_RE_TAG = re.compile(r"#\S+")

#: ★★ **`#话题` 本身也是名字来源**(2026-10-15 补)。
#:
#: 以前 `#话题` 只被 `_RE_TAG` **删掉** —— 而实测**真名常常只出现在话题里**:
#:   `《冰星漫剧》朝朝入梦番外…!#朝朝入梦 #古言权谋`  ⇒ 真名 `朝朝入梦` 只在话题;
#:   `《喜乐无边》98集全…#鳄鱼缅北 #98集全`           ⇒ 真名 `鳄鱼缅北` 只在话题。
#: ⇒ 不采话题,**这两条永远抽不出候选** ⇒ 库里明明有链,卡片却显示「—」
#:   (用户报的「同一个资源有的显示库里有、有的显示没有」,假阴性那一半)。
#: ⚠️ 话题里也有通用词(`#短剧推荐`/`#漫剧`)⇒ 靠**专指度护栏** `title_claims` 在匹配端拦,
#:    不在这里做词表(词表永远不全,而护栏是结构性的)。
_RE_TAG_NAME = re.compile(r"#([^#\s，。！？、|/]{2,24})")


def _cand_names(title: str, limit: int = 7) -> list[str]:
    """从抖音标题里抽出**候选资源名**(按可信度排序、去重、最多 `limit` 个)。

    来源按可信度排序(2026-10-15 起第四类):
      · `《X》`             —— 最可信,直接就是资源/群名;
      · `咐置X叩苓`          —— 变体字,**X 才是名字**(`咐置`/`叩苓` 是「复制」「口令」的替换字);
      · **`#话题`**          —— 人**故意打的标签**,比随机正文片段可信得多;真名常常只在这里;
      · 其余正文长片段      —— 兜底,质量差但聊胜于无。

    ⚠️ `limit` 从 5 提到 7(2026-10-15):多了一类来源之后,5 个名额会把正文兜底挤掉,
    而正文那条正是 2026-10-14 救回「我的世界自然之需」的那一路。一次库查询约 25ms,
    7 个仍是"秒级、可忽略"。
    """
    t = str(title or "")
    out: list[str] = (list(_RE_BOOK.findall(t)) + _RE_BIANTI.findall(t)
                      + _RE_TAG_NAME.findall(t))
    core = _RE_TAG.sub("", t).replace("咐置", " ").replace("叩苓", " ")
    for seg in re.split(r"[，。！?、\s|/]+", core):
        seg = seg.strip()
        if len(seg) >= 3 and not seg.startswith("http"):
            out.append(seg)
    seen: set[str] = set()
    uniq: list[str] = []
    for c in out:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq[:limit]


#: 「窗口候选」的最短长度(2026-10-14)。⚠️ **别调小**:6 字以下的中文短语在资源库里
#: 天然是通用词 —— 实测 6 字的「免费下载安装」就命中了一条不相干的链。
_WINDOW_MIN = 6
#: 每条线索最多试几个窗口(长→短)。它只在**第一遍全空**时才走,每个窗口一次库查询
#: (实测约 25ms)⇒ 12 个 ≈ 0.3s/条,一轮十几条 = 秒级,可忽略。
_WINDOW_CAP = 12
#: 切片段(与 `_cand_names` 同一套分隔符,这里**额外切开 `《》`/`【】`/括号**)——
#: 因为名字常常紧贴在 `《群名》` 后面(`《来支双喜》我的世界自然之需搜 …`),
#: 不切开就永远切不出后半段那个真名字。
_WINDOW_SPLIT = re.compile(r"[，。！?、\s|/【】()（）\[\]《》]+")

#: **通用推广词**:窗口候选里**只要沾上这些字就不算名字**(2026-10-14 实测两次栽在这上面)。
#: 两个实例:`免费下载安装` 命中一条不相干的链、`免费获取教程` 命中另一条 ——
#: 它们**都能过"库里标题以它开头"那道护栏**(因为库里那条资源就叫这名),
#: 所以**必须在这里拦**。
#: ⚠️ 加词要有分寸:这是**推广行为词**,不是代词 ——
#: 别写「我的」「你们的」(会把「我的世界自然之需」一起打死,而它正是这条链要救的样本)。
_GENERIC_WORDS = (
    "免费", "获取", "教程", "下载", "安装", "在线", "观看", "领取", "分享", "链接",
    "入口", "最新", "高清", "全集", "完整版", "怎么", "什么", "可以", "来了", "来啦",
    "找到", "推荐", "关注", "点赞", "私信", "详细", "直接", "点击", "记得",
)


def _window_names(title: str) -> list[str]:
    """**第二遍**候选:把长片段按「前缀窗口」再切一遍(长→短),捞**名字被夹在长句里**的那种。

    ★ 为什么要它(2026-10-14 实测,用户报「点标题跳网盘」):
        `《来支双喜》我的世界自然之需搜 模组下载教程,…`
    而库里**明明有**「我的世界自然之需」的我方链 —— 第一遍那三类候选
    (`《X》` / `咐置X叩苓` / 整段文字)**一个都配不上**,因为名字后面还粘着一个「搜」字。
    ⇒ 把每个片段的前缀从长到短各试一次,试到第 8 个字就命中(实测命中率 41% → 48%)。

    ⚠️ **两道护栏**(见 `_library_link`):① 库里标题**必须以它开头**;② 它自己**不许沾通用词**
    (`_GENERIC_WORDS`)。⚠️ 实测缺任何一道都会配错 —— 通用短语(`免费下载安装`/`免费获取教程`)
    不但会命中不相干的链,其中一条的**库标题恰好以它开头**,连①都拦不住;
    而「**给错链比不给链更坏**」是本仓既定口径(用户点开会拿到完全不相干的东西)。
    """
    out: list[str] = []
    for seg in _WINDOW_SPLIT.split(_RE_TAG.sub("", str(title or ""))):
        seg = seg.strip()
        for length in range(len(seg), _WINDOW_MIN - 1, -1):
            win = seg[:length]
            if win in out or any(w in win for w in _GENERIC_WORDS):
                continue
            out.append(win)
            if len(out) >= _WINDOW_CAP:
                return out
    return out


def _library_link(db, user_id: int, title: str, *, days: int = 90) -> dict | None:
    """**拿线索标题去我们自己的资源库里找一条现成的我方链**。找不到返回 `None`。

    ★ 为什么需要它(2026-10-11 用户口径「**我们资源库里面大部分应该就有这些资源**」):

    抖音视频里**没有网盘直链**,只有口令;而口令能解的比例很低 —— 近 3 天 107 条实测:
    直链 15 条 / 群 29 条 / **迅雷连 X 都不认的变体字 63 条**。但**资源本身**我们库里
    常常已经有(实测 `kind=share` 那批 **15/15 全部命中**库)。
    ⇒ **不必解口令**:拿资源名去库里匹配,匹配上就直接用**我方链** ——
    卡片于是与**公众号同形:点标题就跳网盘**(那边正是"正文里抽到链 → 用它")。

    ⚠️ 匹配是**我们自己的库**(只读本地表),**不碰任何平台接口** —— 零风控风险。
    """
    from app.services.resource_library import search_resources, title_claims

    for name in _cand_names(title):
        try:
            hits = search_resources(db, user_id, name, days=days, limit=5)
        except Exception:  # noqa: BLE001 - 库里查不到不该拖垮整轮(与 `_library_evidence` 同口径)
            logger.debug("资源库匹配失败 name=%s", name, exc_info=True)
            continue
        for h in hits:
            link = str(h.get("my_link") or "").strip()
            if not link:
                continue
            # ⚠️⚠️ **必须加这道护栏 —— 实测首选命中会错**(2026-10-11):
            #   「**伪装直男**」的首选命中是一条「**高分双男主精选BL向…500部AI漫剧合集**」,
            #   而正确的专链(标题就是「《伪装直男》无雾版高清资源」)排在**第 2/3 位**。
            #   名字只在**别的标题/正文**里出现过 ⇒ 短名子串匹配**天然模糊**。
            #   **给错链比不给链更坏**:用户点开会拿到完全不相干的东西。
            #
            # ★★★ **2026-10-15 修「给错链」**:这道护栏原来写的是**裸包含**
            #   (`if name not in first`),而候选名是**抽出来的** ——
            #   `_cand_names` 只要 ≥3 字的正文片段都算一个候选 ⇒ **通用语气词也能命中**。
            #   实测(用户报「同一个资源有的显示库里有、有的显示没有」时复现):
            #     线索 id=519、标题《冰星漫剧》…!#朝朝入梦…**啊啊啊**
            #     ⇒ 返回 `name='啊啊啊'`、链是 `pan.quark.cn/s/31e155e6fe75`,
            #       而**那条链的标题是《镜外 Do or die》** —— 完全不相干。
            #   ⇒ 改用**专指度护栏** `resource_library.title_claims`
            #     (**与 `pan_discovery` 同一份实现**,不再各写一套):
            #     必须「库里的标题**以它开头**」或「标题里 **《它》/【它】点名**」。
            #   ⚠️ 对**全部** titles 判,不只 `titles[0]` —— `_rows_to_resources` 每条链最多留
            #     2 条标题,而名字可能只在另一条里(**漏判会变成新的假阴性**)。
            titles = [str(t or "") for t in (h.get("titles") or [])]
            first = titles[0] if titles else ""
            if not any(title_claims(t, name) for t in titles):
                continue
            return {"name": name, "link": link, "title": first[:80]}
        # 该候选名的命中都过不了护栏 ⇒ 换下一个候选名(不拿"次优"凑数)
    # ★★ **第二遍:前缀窗口**(2026-10-14)。只在第一遍**全空**时才走。
    #    与第一遍的差别只有护栏:**窗口候选必须让库里标题「以它开头」**——
    #    因为它比 `《X》`/变体字那两类弱得多(是机器切出来的,不是人写的),
    #    实测不加严就会配错(通用短语「免费下载安装」→ 一条不相干的链)。
    from app.services.resource_library import match_key

    for name in _window_names(title):
        try:
            hits = search_resources(db, user_id, name, days=days, limit=5)
        except Exception:  # noqa: BLE001 - 与第一遍同口径:查不动就算了
            continue
        for h in hits:
            link = str(h.get("my_link") or "").strip()
            if not link:
                continue
            first = str((h.get("titles") or [""])[0] or "")
            if not match_key(first).startswith(match_key(name)):
                continue          # 只有"库里的名字**以它开头**"才算命中(反而更严)
            return {"name": name, "link": link, "title": first[:80]}
    return None


# ⚠️ `_looks_like_kouling()`(2026-10-11 加的「看着像口令」识别)已**删除**(2026-10-14):
# 它唯一的用途是让卡片写「📋口令未解析」,而用户当天的口径是**「不必说那个解开哪个没解开,
# 有一个解开的就用哪个解开的链接」** ⇒ 拿到链就挂链(见 `push_leads` 的 `our`),拿不到就留 `—`,
# 不再在客户群里交代"我们在哪个平台解失败了"(那是内部状态)。
# ⚠️ 而"口令长什么样"的判据本身**还在用** —— 采集侧认夸克口令靠 `quark_kouling.mark_of`。


def _pan_name(url: str) -> str:
    """链接属于哪个网盘 → **中文盘名**(夸克/百度/迅雷);认不出返回空串。
    ⚠️ 用户口径(2026-10-04):"**资源不要写我方链接,那个网盘就写那个网盘名称**"。
    卡片的「资源」列原来写的是 `🔴我方链` —— "**我方**"二字**在客户群里会暴露我们是运营方**,
    换成中性的**盘名**既说清了"这条是哪个盘、能直接取",又不露身份。
    """
    u = (url or "").lower()
    if "quark.cn" in u:
        return "夸克"
    if "drive.uc.cn" in u or "uc.cn" in u:
        return "UC"
    if "baidu.com" in u:
        return "百度"
    if "xunlei.com" in u:
        return "迅雷"
    return ""


#: **只认真正的网盘域名**(2026-10-30 用户「一定要确保是网盘的链接」)。
#: ⚠️ 不要退化成"任何 http 都算" —— `library.link`/正文里可能出现推广页、公众号文章、
#: 短链,它们不是网盘,客户点开拿不到资源。
_PAN_HOSTS = ("pan.quark.cn", "drive.uc.cn", "pan.baidu.com", "pan.xunlei.com")


def _is_pan_url(url) -> bool:
    """这个 URL 是不是**网盘分享链**(夸克/UC/百度/迅雷)。"""
    u = str(url or "").strip().lower()
    return bool(u) and any(h in u for h in _PAN_HOSTS)


def _pan_link_of(ld: dict) -> tuple[str, str]:
    """取这条线索**可用的网盘链** → `(url, kind)`;`kind` = `our`(我方/库里)/`author`(作者原始)。

    优先级:我方转存链 > 资源库现成链 > **作者原始分享链** > 正文直链。
    ⚠️ 每一步都过 `_is_pan_url`;不是网盘域名的直接跳过(2026-10-30 用户口径)。
    """
    info = ld.get("kouling") or {}
    lib = (ld.get("library") or {}).get("link")
    for url, kind in ((info.get("our_url"), "our"),
                      (lib, "our"),
                      (info.get("share_url"), "author"),
                      (ld.get("pan_url"), "author")):
        if _is_pan_url(url):
            return str(url).strip(), kind
    return "", ""


def _has_usable_pan(ld: dict) -> bool:
    """这条线索有没有**可用网盘链**(只认真正的网盘域名;群口令不算)。"""
    return bool(_pan_link_of(ld)[0])


def _drop_panless(leads: list[dict]) -> list[dict]:
    """**所有赛道**:没有可用网盘链的线索一律丢掉(2026-10-30 用户口径)。

    > 「把**带网盘链的推广规则带到所有赛道**里面」

    ⚠️ **判据是"可直接取用"**(`_has_usable_pan`):我方链 / 库里现成链 / 正文直链;
    **群口令不算**(能进群,但卡片上没有可直接点的链接)。
    实测(近 14 天):小游戏线索 8 条里 3 条无链、其它赛道同样有无链项 ——
    它们在卡片上只能进「只能对标」那一张,而用户明确不要这种。
    """
    if not leads:
        return leads
    kept = [ld for ld in leads if _has_usable_pan(ld)]
    if len(kept) != len(leads):
        logger.info("抖音线索:%d 条**没有可用网盘链**,已丢弃(不监控)", len(leads) - len(kept))
    return kept


def _publish_mark(pub) -> str:
    """发布时间标记:**一小时内 = 🆕**,其余 `·MM-DD HH:MM`;拿不到就空串。

    用户口径(2026-10-16):「**一个钟内**他这个资源有他的名字」,并拍板「**标时间 + 按新排序**」——
    卡上要一眼看出"这条是刚出的"。
    ⚠️ **顺序本来就已经是新→旧**(`find_leads` 按 `-publish_at` 排,见那边的注释),
    所以这里**只补标记、不动顺序** —— 同一件事写两处迟早各飘各的。
    """
    try:
        ts = int(pub or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    from datetime import datetime

    dt = datetime.fromtimestamp(ts)
    return "🆕" if (datetime.now() - dt).total_seconds() <= 3600 else f"·{dt:%m-%d %H:%M}"


def _historical_mark_counts(session, user_id: int, marks: set[str],
                            days: int = 7) -> dict[str, int]:
    """过去 N 天**同一口令**被几条线索命中 —— 跨轮次的「资源发的多」信号。

    ★ 2026-10-29 用户口径「**可以根据之前的数据来进行策略决策**」:
    AI 单次只看得到这一轮;而"这个资源是不是很多号在推、还在被推"必须看**历史**。
    ⚠️ 只按 `mark` 统计(跨轮次唯一稳定的身份);`pan_url` 类线索没有 mark,不参与。
    """
    if session is None or not marks:
        return {}
    from datetime import datetime, timedelta

    from sqlalchemy import func

    from app.db.models import DouyinLead

    cutoff = datetime.now() - timedelta(days=max(1, int(days)))
    rows = session.execute(
        select(DouyinLead.mark, func.count()).where(
            DouyinLead.user_id == user_id,
            DouyinLead.mark.in_(sorted(m for m in marks if m)),
            DouyinLead.found_at >= cutoff,
            DouyinLead.mark != "",
        ).group_by(DouyinLead.mark)).all()
    return {str(m): int(n) for m, n in rows}


def push_leads(leads: list[dict], settings, platform: str = "douyin",
               session=None, user_id: int = 0) -> bool:
    """把线索推飞书。

    **推抖音专属群**(`FEISHU_WEBHOOK_DOUHOT`,未配则回落主群)——用户口径(2026-10-02):
    "既然是抖音的来源就推送到抖音群聊里面"。版式与公众号推送一致:**四列网格**
    (作者 / 作品 / 资源 / 链接),不靠空格对齐。
    """
    if not leads:
        return False
    from app.services.feishu_client import webhook_for

    section = (PLATFORMS.get(platform) or {}).get("section", "douhot")
    # ★ **同行作品群**(2026-10-16 用户新建):抖音监控到的**同行作品**发那个新群。
    # ⚠️ 与 `douhot` 那个 section **分开**是必须的:那个 section 同时被
    #    `alert_service` 的**采集失败告警**用着 ⇒ 直接改它会把运维告警挪进客户群。
    # 没配就回落旧行为(内容卡 → 该 section 的群)。
    peer = (getattr(settings, "feishu_webhook_douyin_peer", "") or "").strip()
    # 抖音线索也是**内容卡** → 未配专属群时落客户群是对的。
    # (末尾那个 `or feishu_webhook_admin` 是够不到的死代码:主群已配时 webhook_for 永不返回空串)
    webhook = peer or webhook_for(settings, section) or getattr(settings, "feishu_webhook_admin", "")
    if not webhook:
        logger.info("抖音线索:未配飞书 webhook,跳过推送")
        return False

    # 「条数」= **这个资源本轮被几条视频在推** —— 用户口径(2026-10-04):
    # 一眼看出"大家都在抢这个"。
    # ⚠️ **按"资源身份"分组,不是按口令**:同一个资源会被不同推广号起**不同口令**
    # (实测:《齐民要术》《人生使用说明书》其实都是《高性价比人生指南》的别名)——
    # 按口令分会把同一份资源算成好几条。**能拿到原始链的就用它当身份**(那才是资源本身),
    # 拿不到的(群口令/没解出来)才回落到口令。
    from collections import Counter

    def _res_key(ld_: dict) -> str:
        info_ = ld_.get("kouling") or {}
        #: ★ 加了 `pan_url`(2026-10-29):**没有口令、正文直接贴盘链**的帖子
        #: 原来会全部落到 `mark:` 空串上、被当成**同一个资源**(「条数」列恒等于总数)。
        return (str(info_.get("share_url") or "")
                or str(ld_.get("pan_url") or "")
                or f"mark:{ld_.get('mark') or ''}")

    _counts = Counter(_res_key(x) for x in leads)

    # ★ **推送去重**(2026-10-29 数据驱动):12 天台账里抖音 **62%** 的推送行是同一作品
    # 在 24 小时内被重推 —— 这既是"群信息杂乱"的主因,也在白烧 AI 筛选的钱。
    # ⚠️ 只在**本板块**内去重(不同板块的群不是一批人);`push_dedupe_hours=0` 关闭。
    from app.services import push_ledger

    _led_sec = "douyin_peer" if peer else section
    leads, _dup = push_ledger.drop_recent(session, user_id, _led_sec, leads, settings)
    if not leads:
        logger.info("抖音线索:%d 条全部落在去重窗口内,本轮不推", _dup)
        return False
    # ★ **新鲜度闸门**(2026-10-29):采集侧已有 7 天窗口,这里**再兜一道** ——
    # 采集窗口可配置、推送窗口也要可配置;两处口径同源(`PUSH_FRESH_DAYS`)。
    from app.services import freshness_gate

    leads, _stale, _unknown = freshness_gate.split_fresh(leads, settings)
    if not leads:
        logger.info("抖音线索:%d 条全是 %d 天前的旧内容,本轮不推", _stale,
                    int(getattr(settings, "push_fresh_days", 7) or 0))
        return False

    # ★ **AI 价值筛选**(2026-10-29,用户口径「每个板块都加上 AI 筛选」)。
    # **价值口径(用户原话)**:「有用的也就是意味着**资源发的多、转存人数多**」
    # ⇒ 把「同一资源本轮被几条视频在推」+ 转发数一起喂给模型。
    # 模型失败/没配 key ⇒ 原样全推(`source != "llm"`,卡片上不写 `AI筛x/y`)。
    # 放在这里是因为**后面所有计数/台账都该按筛后的算**。
    from app.services import ai_filter, value_prior
    from app.services import category_topics as _ct

    #: ★ **数据先验**(2026-10-29 用户口径「可以根据之前的数据来进行策略决策」):
    #: 近 30 天各赛道真实搬成率(软件 93% / 资料 87% / 影视 57% / 小游戏 54%…)
    #: 与每条线索自己的关键词历史有效率,一起喂给模型 —— 让它按数据评分。
    _rates = value_prior.niche_rates(session, user_id)
    _prior_note = value_prior.prompt_note(_rates)
    _kw_yield = keyword_yield(session, user_id) if session is not None else {}

    def _prior_of(ld_: dict) -> str:
        k = _ct.niche_of(f"{ld_.get('title') or ''} {ld_.get('mark') or ''}")
        st = _rates.get(k) or {}
        bits = []
        if st:
            bits.append(f"赛道历史{float(st.get('rate') or 0) * 100:.0f}%(n={st.get('n')})")
        ny = _kw_yield.get(str(ld_.get("keyword") or ""))
        if ny:
            n, ok = int(ny[0]), int(ny[1])
            bits.append(f"词历史{ok}/{n}")
        return " ".join(bits)

    #: ★ 历史口令计数(跨轮次「还在被推」)。
    _hist = _historical_mark_counts(
        session, user_id, {str(x.get("mark") or "") for x in leads})
    _ai = ai_filter.filter_items(
        "douyin_peer" if peer else section, leads, settings,
        extra_context=_prior_note,
        #: ★ 判定落库(供分数线校准):key 用 aweme_id,能和 `douyin_leads` 直接 join 出搬成结果。
        session=session, user_id=user_id,
        key_of=lambda ld_: str(ld_.get("aweme_id") or ld_.get("mark") or ""),
        text_of=lambda ld_: (
            f"{ld_.get('title') or ''} 作者:{ld_.get('author') or ''} "
            f"转发:{ld_.get('share_count') or 0} "
            f"同资源条数:{_counts.get(_res_key(ld_), 1)} "
            f"历史7天:{_hist.get(str(ld_.get('mark') or ''), 0)} "
            f"库内:{'有' if (ld_.get('library') or {}).get('link') else '无'} "
            f"{_prior_of(ld_)}"))
    if not _ai.items:
        logger.info("抖音线索:AI 筛选后无内容可推(原 %d 条)", _ai.total)
        return False
    leads = _ai.items

    from app.services.feishu._cards import _col_set_row, _md_safe, strip_others
    from app.services.feishu_client import FeishuClient

    label = (PLATFORMS.get(platform) or {}).get("label", platform)

    # ★ **按赛道分组**(2026-10-29,用户口径「推送的时候进行分类…每个板块都能监测到」)。
    # 之前一张卡就是一串按时间排的行,短剧占绝大多数的时候,小游戏那几条会被淹在里面
    # ——客户看到的观感就是"这轮又全是短剧"。现在:① 卡头写出**本轮赛道构成**;
    # ② 行按赛道分组、每组带小标题;③ **稀缺赛道排前面**、`其他` 垫底。
    # ⚠️ 分组**不改变逐条的展示口径**(六列、点标题跳网盘都一样),只是把顺序与标题补上。
    from app.services import category_topics

    def _niche_of(ld_: dict) -> str:
        """这条线索属于哪个赛道 —— **影视再细分**(漫剧/短剧/…),卡片分组一眼看得出类型。"""
        return category_topics.display_niche(
            f"{ld_.get('title') or ''} {ld_.get('mark') or ''}")

    def _link_of(ld_: dict) -> str:
        """这条线索**能直接取用**的网盘链(只认网盘域名;见 `_pan_link_of`)。"""
        return _pan_link_of(ld_)[0]

    def _lead_row(ld: dict) -> dict:
        """一条线索 → 六列网格行(拆出来只为让 `push_leads` 保持可读)。"""
        author = _md_safe(ld.get("author") or "—")
        title = _md_safe(strip_others(ld.get("title") or ""))
        shown = title[:22] + ("…" if len(title) > 22 else "")
        # ★ **发布时间标记**(2026-10-16 用户口径:一个钟内的 = 🆕,其余给到分钟)。
        _when = _publish_mark(ld.get("publish_at"))
        if _when:
            shown = f"{shown} {_when}"
        vurl = _md_safe(ld.get("url") or "")
        #: 这条线索**能用的那条链**(我方链 > 库里现成链 > 正文直链)。它同时决定两列:
        #: 「作品」列挂它就**可点**,「资源」列写它落在**哪个盘**。
        _raw_our, _kind = _pan_link_of(ld)
        our = _md_safe(_raw_our)
        work = f"[{shown}]({our})" if our else shown
        # 转发量**独立成列**;没有就不显示,不拿 0 冒充有数据。
        sc = int(ld.get("share_count") or 0)
        # 「资源」列 = **只写盘名**(有链 ⇒ 🔴夸克/🔴迅雷/🔴百度;没链 ⇒ —)。
        _pan = _pan_name(_raw_our)
        #: ★ **区分我方链 / 作者链**(2026-10-30 用户「可以加上,但一定要确保是网盘链接」):
        #: 🔴 = 我方转存链或资源库现成链;⏳ = 作者原始分享链(还没搬成,但可以直接取)。
        res = (("🔴" if _kind == "our" else "⏳") + _pan) if _pan else "—"
        return _col_set_row([
            (author, 3),
            (work, 5),
            (res, 2),
            (f"[▶视频]({vurl})" if vurl else "—", 2),
            (f"↗{sc}" if sc else "—", 2),
            (str(_counts.get(_res_key(ld), 1)), 1)])


    #: ★ **按"能不能直接用"分批**(2026-10-30,客户视角):可直取的先发一张、
    #: 只能对标的另发一张 —— 客户先拿货、再看行情,不必在长卡里自己挑。
    _with_link = [x for x in leads if _link_of(x)]
    _no_link = [x for x in leads if not _link_of(x)]

    def _build_card(subset: list[dict], suffix: str) -> dict:
        groups: dict[str, list[dict]] = {}
        for ld in subset:
            groups.setdefault(_niche_of(ld), []).append(ld)
        order = sorted(groups, key=lambda n: (n == category_topics.NICHE_OTHER,
                                              len(groups[n]), n))
        n_link = sum(1 for x in subset if _link_of(x))
        pan_cnt = Counter(_pan_name(_link_of(x)) for x in subset if _link_of(x))
        pan_txt = " / ".join(f"{k or '网盘'} {v}" for k, v in pan_cnt.most_common())
        intro = f"{label}上发现 **{len(subset)}** 条在推同类资源的视频。"
        if n_link:
            kinds = Counter(_pan_link_of(x)[1] for x in subset if _link_of(x))
            intro += (f"\n**可直取 {n_link} 条**"
                      f"(🔴我方 {kinds.get('our', 0)} · ⏳作者 {kinds.get('author', 0)})"
                      + (f" {pan_txt}" if pan_txt else "")
                      + " —— 点作品标题即可打开对应网盘。")
        else:
            intro += ("\n**这些条暂无可直取链,只能看对标作品**(「资源」列全为 —);"
                      "先看别人怎么推,等转存成功再取用。")
        if _ai.summary():
            intro += f"\n**{_ai.summary()}**:已挡掉纯广告 / 无具体资源 / 过时的那些。"
        if _dup:
            intro += (f"\n已跳过 {_dup} 条 "
                      f"{int(getattr(settings, 'push_dedupe_hours', 24) or 0)} 小时内推过的。")
        n_unknown = sum(1 for x in subset if not int(x.get("publish_at") or 0))
        fresh = freshness_gate.summary(len(subset), 0, n_unknown)
        if fresh:
            intro += f"\n{fresh}"
        if len(groups) > 1:
            #: ⚠️ 只有一个赛道时不写这一行 —— 那会变成"一句话把卡片标题又说一遍"
            intro += "\n**本轮赛道**:" + " · ".join(
                f"{n} {len(groups[n])}" for n in order)
        els: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": intro}},
            # **六列网格**(用户口径 2026-10-04):作者 / 作品 / 资源 / 视频 / 转发数 / 条数
            _col_set_row([("**作者**", 3), ("**作品**", 5), ("**资源**", 2), ("**视频**", 2),
                          ("**转发数**", 2), ("**条数**", 1)], grey=True)]
        for niche in order:
            if len(groups) > 1:
                #: ⚠️ 用 `div` 而不是 `column_set` —— 分组标题**不是数据行**,
                #: 混进网格会让"表头 + N 行"的判据失真(`tests/test_douyin_leads.py` 靠它)。
                els.append({"tag": "div", "text": {"tag": "lark_md",
                    "content": f"**▍{niche}** · {len(groups[niche])} 条"}})
            for ld in sorted(groups[niche],
                             key=lambda x: -(int(x.get("publish_at") or 0))):
                els.append(_lead_row(ld))
        return {
            "config": {"wide_screen_mode": True},
            # ⚠️ **不带头部品牌名**(用户口径 2026-10-04:"**不要带念飞思雪**")。
            "header": {"template": "purple", "title": {"tag": "plain_text",
                "content": f"🎯 {label}推广线索 · {len(subset)} 条{suffix}"}},
            "elements": els,
        }

    sent_any = False
    for subset, suffix in ((_with_link, " · 可直取"),
                           (_no_link, " · 只能对标(暂无盘链)")):
        if not subset:
            continue
        try:
            ok = FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(
                _build_card(subset, suffix))
        except Exception:  # noqa: BLE001 - 推送失败不该影响采集
            logger.exception("抖音线索推送失败")
            ok = False
        if ok and session is not None and user_id:
            # ★ **推送台账**(2026-10-16):板块日结/周结(`board_digest`)读它。
            #   ⚠️ 记的是**落地的那个群**;⚠️ 链在 `kouling.our_url`/`library.link` 里嵌套,
            #   台账只认顶层 `pan_url` ⇒ 必须在这里展开(2026-10-29 修,实测 1,495 行全空)。
            from app.services import push_ledger

            _led = [{**ld, "pan_url": _link_of(ld)} for ld in subset]
            push_ledger.record(session, user_id,
                               "douyin_peer" if peer else section, _led)
        sent_any = sent_any or ok
    return sent_any


def _freshness_summary(leads: list[dict]) -> str:
    """本轮线索的**新鲜度**:`最新10-29 10:12 中位10-28`(2026-10-29)。

    用户口径「监控的内容我们需要新鲜度,太老旧的不需要」—— 只设时间窗还不够,
    **每轮都得能看见产出到底有多新**,否则"窗口挡掉了多少"与"这批本来就是旧的"分不清。
    ⚠️ 没有发布时间的线索不参与(判不了就不编)。
    """
    from datetime import datetime as _dt

    ts = sorted(int(x.get("publish_at") or 0) for x in leads
                if int(x.get("publish_at") or 0) > 0)
    if not ts:
        return "无时间"
    newest = _dt.fromtimestamp(ts[-1]).strftime("%m-%d %H:%M")
    med = _dt.fromtimestamp(ts[len(ts) // 2]).strftime("%m-%d")
    return f"最新{newest} 中位{med}"


def _niche_summary(leads: list[dict]) -> str:
    """本轮线索的**赛道构成**(2026-10-29) —— 例:`小游戏1/影视3/其他2`。

    ⚠️ **为什么非要有这个数**:用户报「每轮推送基本都是短剧相关的」,而在此之前
    **没有任何一处记录**"这一轮到底出了哪些赛道" —— 只能去翻飞书卡片。
    现在运行记录里一眼能看出轮换有没有真的覆盖到别的赛道(恒为「影视N」= 没覆盖)。
    顺序与卡片一致:稀缺赛道在前、`其他` 垫底,所以两处读起来是同一份东西。
    """
    from collections import Counter

    from app.services import category_topics

    cnt = Counter(category_topics.display_niche(
        f"{ld.get('title') or ''} {ld.get('mark') or ''}") for ld in leads)
    return "/".join(f"{k}{v}" for k, v in sorted(
        cnt.items(), key=lambda kv: (kv[0] == category_topics.NICHE_OTHER, kv[1], kv[0])))


def _kouling_summary(leads: list[dict]) -> str:
    """本轮口令解析的**构成**,尤其是"**新群几个**"(2026-10-04 加)。

    **为什么必须有这个数**:这条链的搜索词有相当一部分来自**我们自己已有的群**
    (`group_keywords` 直接取群里的资源标题当词),于是很容易形成**自循环** ——
    搜出来的口令反复指向**已经加过**的群:线索数看着不少,**新群一个没有**。
    用户 2026-10-04 的原话:"而不是一直用着一个口令进群,我们是需要创新的"。

    一行就能判断这条链是在**往外扩**还是在**原地打转**:
      `新群0/群9/链3` → 解析出 9 个群口令但**全是已有的** = 自循环信号(该换词源了)。
    """
    ks = [(ld.get("kouling") or {}) for ld in leads]
    n_new = sum(1 for k in ks if k.get("kind") == "group" and k.get("newly_joined"))
    n_group = sum(1 for k in ks if k.get("kind") == "group")
    shares = [k for k in ks if k.get("kind") == "share"]
    # ⚠️ **`链N` 这个旧标签是误导的**(2026-10-05 改):它数的是"口令解出来**是**分享链的条数",
    # 而里面一大半是 `already`(早就搬过)和 `over_budget`(这轮没搬)—— **读起来像"搬成了 N 条"**,
    # 其实一条都没搬。这正是"看起来像产出、其实不是"的那类指标。
    # 现在把**真正的结果**摊开:有链(拿到我方分享链的)/ 已搬过 / 超额度。
    # ⚠️ **"有链"必须是"本轮新搬到的"**(2026-10-05 P2):`already` 那一支现在也带 `our_url`
    # (从历史捡回来的,卡片上该显示),但**它不是本轮的产出** —— 算进去就把指标注水了,
    # 而这条链要的正是"**新**群/新资源"。
    n_moved = sum(1 for k in shares
                  if k.get("our_url") and k.get("status") != "already")
    n_already = sum(1 for k in shares if k.get("status") == "already")
    n_over = sum(1 for k in shares if k.get("status") == "over_budget")
    return (f"新群{n_new}/群{n_group}/分享链{len(shares)}"
            f"(新搬{n_moved} 已搬过{n_already} 超额度{n_over})")


def run_douyin_leads(db, user_id: int, settings=None, category: str | None = None,
                     advance: bool = True) -> int:
    """**单用户**跑一轮抖音线索:定时 tick 与「失败重试」**共用这一条路径**。

    ⚠️ **为什么必须共用一个函数**(而不是给重试另写一套):本项目在 `wechat_listen` 上踩过
    —— 两条路各写各的,迟早飘出两套行为;那边连"重试要重跑同一组"都是**事后单独打补丁**
    才补上的(`_retry_runners` 里读 `cursor=` 那段)。

    ⚠️ `advance=False` 是给**重试**用的:重试必须**重跑同一个类目**、**不能推进游标** ——
    否则失败那一轮等于白跳过一个类目,而"每类都要覆盖到"正是轮换的意义
    (与 `wechat_listen` 的 `batch_index` 同一条纪律)。
    """
    from app.services import category_topics
    from app.services.tenant_base import _record_run

    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    plats = platforms_of(settings)
    # 词来自**群组新资源 + 公众号已验证资源**(见 search_keywords 的注释);各平台共用。
    # **热榜外部种子**只喂给抖音:小红书/快手没有口令,拿热榜词去搜只是白开一次浏览器。
    hot = hot_seed_words(settings) if "douyin" in plats else []
    cat = category or category_topics.current_category(db)   # 本轮搜哪个类目(轮换)
    kws = search_keywords(db, user_id,
                          int(getattr(settings, "douyin_leads_keywords", 3) or 3),
                          settings, hot=hot)
    if not kws:
        return 0
    total = 0
    for plat in plats:
        try:
            # ⚠️ **必须传 `session`** —— 纯协议要从加密库读凭据;不传就是静默不带登录态
            # (实测表现:回 `status_code=2483 请先登录`,而**看起来只是"没搜到"**)。
            funnel: dict = {}
            leads, source = collect_leads(
                kws, platform=plat, session=db, user_id=user_id,
                pages=int(getattr(settings, "douyin_leads_pages", 1) or 1),
                stats=funnel)
            total += len(leads)
            n_panless = 0
            n_grouponly = 0
            if leads:
                # 口令 → 资源(分享链直接转存入库 / 群则加群),结果一并写进卡片
                apply_kouling(leads, db, user_id, settings)
                # ★ **所有赛道都必须带网盘**(2026-10-30 用户口径):没有可用链的不入库、不推。
                before = len(leads)
                kept = _drop_panless(leads)
                n_panless = before - len(kept)
                #: ★ **群口令型:只入库、不进客户卡**(2026-10-31 用户「新鲜资源优先级大于转存,
                #: 我们不需要全部的转存」)。原来这类**直接丢掉** ⇒ 数据库里根本看不到
                #: "今天其实发现了 2 条新的",漏斗长期显示 0,像"没人发"。
                #: 现在它们进 `douyin_leads`(卡片口径不变:客户群仍然只发**可直取**的),
                #: 供观察与后续决定"要不要单独出一张『需加群』的卡"。
                kept_ids = {str(x.get("aweme_id") or "") for x in kept}
                group_only = [x for x in leads
                              if str(x.get("aweme_id") or "") not in kept_ids
                              and (x.get("kouling") or {}).get("kind") == "group"]
                n_grouponly = len(group_only)
                if group_only:
                    _save_leads(db, user_id, group_only)
                leads = kept
                if leads:
                    _save_leads(db, user_id, leads)  # 落库:转发量只在这一次有效(结算要用)
                    push_leads(leads, settings, platform=plat, session=db, user_id=user_id)
            # ⚠️ **按词源分开报**(2026-10-05):只报总数看不出"哪档在起作用" ——
            # 而实测两档差了 86 个百分点(资源名 86% / 热榜种子 0%)。
            # 不分开就永远发现不了"四成预算花在 0% 有效率的词上"这件事。
            # ⚠️ `search_keywords` **已经把热榜词并进 `kws` 了** ——
            # 所以这里报的是 `kws 共 N(其中外部 M)`,**不能写成 `词N+外部M`**(会重复计数)。
            hs = set(hot or [])
            n_hot_used = sum(1 for w in hs if w in set(kws))
            n_hot = sum(1 for x in leads if (x.get("keyword") or "") in hs)
            n_hot_ok = sum(1 for x in leads
                           if (x.get("keyword") or "") in hs
                           and (x.get("kouling") or {}).get("kind") in ("group", "share"))
            # ⚠️ **来源标签要进运行记录**(2026-10-08):抖音现在"协议优先、浏览器兜底",
            # 而两条路都会产出"0 条"。不标来源的话,看到 0 就分不清是
            # "协议挂了被回落"还是"真没人在推资源" —— 正是本仓最忌讳的那种含糊。
            _record_run(db, user_id, "douyin_leads", "success",
                        f"{plat}[{source or '-'}] 类目{cat} 词{len(kws)}(其中外部{n_hot_used}) "
                        f"线索{len(leads)} 赛道{_niche_summary(leads) or '-'} "
                        f"新鲜{_freshness_summary(leads)} "
                        f"{_kouling_summary(leads)} "
                        f"[外部命中{n_hot}/产出{n_hot_ok}] "
                        #: ★ 漏斗(2026-10-31):原始→时间挡→无时间→口令→无链,一眼看出哪道闸在掉量。
                        f"漏斗 原始{funnel.get('raw', 0)} 挡{funnel.get('old', 0)}"
                        f" 无时{funnel.get('notime', 0)} 口令{funnel.get('kept', 0)}"
                        f" 无链{n_panless} 群令{n_grouponly}")
            db.commit()
        except Exception as exc:  # noqa: BLE001 - 单平台失败不影响其余
            db.rollback()
            logger.exception("线索平台 %s 失败 user=%s", plat, user_id)
            _record_run(db, user_id, "douyin_leads", "failed", f"{plat}: {str(exc)[:160]}")
            db.commit()
    if advance:
        # ⚠️ **轮换游标在"整轮跑完之后"才推进**(2026-10-04):放在出词之后就推的话,
        # 中途失败会白白跳过一个类目 —— 而"每类都要覆盖到"正是轮换的意义。
        nxt = category_topics.advance_category(db)
        logger.info("抖音线索:类目 %s 跑完 → 下轮 %s", cat, nxt)
    return total


def douyin_leads_tick(settings=None) -> int:
    """定时:按资源库的词去各**线索平台**搜 → 解析口令 → 推推广线索。返回线索条数。

    平台列表来自 `settings.leads_platforms`(默认只有抖音)。**每个平台各开一次浏览器**
    (MediaCrawler 一次几分钟),所以别贪多 —— 实测只有抖音的内容层真带《口令》。

    单用户的活全在 `run_douyin_leads` 里(**重试走同一个函数**,理由见那边的说明)。
    """
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    if not getattr(settings, "douyin_leads_enabled", True):
        return 0
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            total += run_douyin_leads(db, uid, settings)
    finally:
        db.close()
    return total
