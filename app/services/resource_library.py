# -*- coding: utf-8 -*-
"""资源库(2026-10-01):对标号全历史盘链的检索、共振榜与画像。

**为什么**:选题建议只匹配近 72h 对标文章,大量历史资源被浪费——
回灌实证"同一盘链被 13 个号同发"(花少2人格测试)这类就是金矿,却沉在历史里。
本模块把 `wechat_pan_links` + `wechat_articles` 变成可检索的资源库:

- `search_resources`   关键词(热点词/品类词)检索资源,带验证强度(多少号发过);
- `resonance_resources` 高共振资源榜(同链被 ≥N 号同发 = 需求被反复验证);
- `resource_profile`    单资源画像(多少号/时间线/我方链是否已转存可直接复用)。

全部数据来自已采集的对标信息——**不碰第三方资源聚合**(规避版权风险面)。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import WechatArticle, WechatPanLink
from app.utils import get_logger

logger = get_logger(__name__)


def _pan_kind(url: str) -> str:
    if "pan.quark.cn" in (url or ""):
        return "夸克"
    if "pan.baidu.com" in (url or ""):
        return "百度"
    if "drive.uc.cn" in (url or ""):
        return "UC"
    if "pan.xunlei.com" in (url or ""):
        return "迅雷"
    return "其他"


def _my_link_of(session: Session, user_id: int, pan_url: str) -> str:
    """该链在本租户是否已有我方转存链(可直接复用)。

    两处都查:公众号链存 `WechatArticle.my_pan_urls`,**公开平台发现的**存
    `discovered_pan_links.our_url`(2026-10-02 并入)。
    """
    blobs = session.scalars(select(WechatArticle.my_pan_urls).join(
        WechatPanLink, WechatPanLink.article_id == WechatArticle.id).where(
        WechatPanLink.pan_url == pan_url, WechatArticle.user_id == user_id,
        WechatArticle.my_pan_urls.isnot(None), WechatArticle.my_pan_urls != ""
    ).limit(3)).all()
    for blob in blobs:
        for line in (blob or "").splitlines():
            link = _clean_link(line)
            if link:
                return link
    return _discovered_my_link(session, user_id, pan_url)


def _clean_link(line: str) -> str:
    """从一行里抠出**干净的 URL**。

    ⚠️ 历史数据常带尾巴:实测 `WechatArticle.my_pan_urls` 里存过
    `https://pan.quark.cn/s/xxx (自分享)` —— 整行拿去当链接**点不开**
    (2026-10-02 跨平台热度卡片里发现)。所以在**取用端**统一抠一次。
    """
    import re

    m = re.match(r"(https?://[^\s()（）【】、,，]+)", (line or "").strip())
    return m.group(1) if m else ""


def _discovered_my_link(session: Session, user_id: int, pan_url: str) -> str:
    """公开平台(知乎)发现的那条链,我方是否已转存。"""
    from app.db.models import DiscoveredPanLink

    return str(session.scalar(select(DiscoveredPanLink.our_url).where(
        DiscoveredPanLink.user_id == user_id, DiscoveredPanLink.origin_url == pan_url,
        DiscoveredPanLink.our_url != "").limit(1)) or "")


def _discovered_resources(session: Session, user_id: int, query: str = "",
                          days: int = 90, limit: int = 20) -> list[dict]:
    """**公开平台发现的**盘链(知乎等),结构对齐 `_rows_to_resources`。

    2026-10-02 并入资源库:知乎回答里直接贴的夸克/百度链,和公众号文章里的链**是同一类东西**
    (键都是"别人的原链"),所以按 `pan_url` 天然能合在一起看。
    `accounts` 记 1(一个来源),`source` 标出来源平台 —— 别和公众号的"多少号同发"混为一谈。
    """
    from app.db.models import DiscoveredPanLink

    stmt = select(DiscoveredPanLink).where(
        DiscoveredPanLink.user_id == user_id,
        DiscoveredPanLink.found_at >= datetime.now() - timedelta(days=days))
    if query:
        stmt = stmt.where(DiscoveredPanLink.title.contains(query))
    rows = session.scalars(stmt.order_by(DiscoveredPanLink.found_at.desc()).limit(limit)).all()
    label = {"zhihu": "知乎"}
    out = []
    for r in rows:
        ts = r.found_at.isoformat(sep=" ", timespec="seconds") if r.found_at else ""
        out.append({"pan_url": r.origin_url, "pan_type": _pan_kind(r.origin_url),
                    "accounts": 1, "titles": [str(r.title or "")[:60]] if r.title else [],
                    "first_seen": ts, "last_seen": ts, "my_link": str(r.our_url or ""),
                    "source": label.get(r.platform, r.platform or "发现"),
                    "author": str(r.author or "")})
    return out


def _rows_to_resources(session: Session, user_id: int, rows, with_my: bool = True) -> list[dict]:
    """聚合行 → 资源条目(统一结构;titles 取样例)。"""
    out = []
    for pan_url, accounts, first_seen, last_seen in rows:
        sample = session.scalars(select(WechatArticle.title).join(
            WechatPanLink, WechatPanLink.article_id == WechatArticle.id).where(
            WechatPanLink.pan_url == pan_url, WechatPanLink.user_id == user_id,
        ).order_by(WechatArticle.created_at.desc()).limit(2)).all()
        out.append({
            "pan_url": pan_url, "pan_type": _pan_kind(pan_url),
            "accounts": int(accounts or 0),
            "titles": [str(t or "")[:60] for t in sample],
            "first_seen": first_seen.isoformat(sep=" ", timespec="seconds") if first_seen else "",
            "last_seen": last_seen.isoformat(sep=" ", timespec="seconds") if last_seen else "",
            "my_link": _my_link_of(session, user_id, pan_url) if with_my else "",
            "source": "公众号",
        })
    return out


def _xunlei_resources(session: Session, user_id: int, query: str = "",
                      days: int = 90, limit: int = 20) -> list[dict]:
    """**我们自己迅雷盘里**的资源(口令转存 + 扫盘),结构对齐 `_rows_to_resources`。

    ⚠️ **这条出口此前根本不存在**(2026-10-03 全项目审查发现):`xunlei_resources` 有
    **两处写入**(`xunlei_kouling` 口令转存 / `xunlei_sync` 扫盘)、**零处读取**(除健康检查
    取个时间),而 `resource_library` 只合并公众号链与公开平台发现链 —— 于是
    **抖音口令搬进来的资源、扫盘扫出来的资源,进了库谁也看不见、presence 也匹配不到**,
    等于白搬。

    ⚠️ **形状差别要说清**:公众号/知乎那两条里 `pan_url` 是"**别人的原链**"、`my_link` 是
    "我们的";而这里是**我们自己盘里的东西**,没有"别人的原链"可记 —— 所以 `pan_url` 与
    `my_link` **都是我们那条分享链**,`source` 标 `迅雷盘` 让界面能一眼区分"这条已经是我们的了"。
    没有 `share_url` 的行**跳过**(库的用途是"给出可用链",没链的放进来只会干扰)。
    """
    from app.db.models import XunleiResource

    stmt = select(XunleiResource).where(XunleiResource.user_id == user_id,
                                       XunleiResource.share_url != "")
    if days:
        stmt = stmt.where(XunleiResource.synced_at >= datetime.now() - timedelta(days=days))
    rows = session.scalars(stmt.order_by(XunleiResource.synced_at.desc())).all()
    if query:
        # ⚠️ **不能在 SQL 里 `contains`**:查询词被 `library_search_word` 剥过括号,
        # 而这里的名字是原始的 ⇒ 中间夹一处 `【…】` 就永远匹配不上(2026-10-08 修)。
        key = match_key(query)
        rows = [r for r in rows if key in match_key(r.name)]
    rows = rows[:limit]
    out: list[dict] = []
    for r in rows:
        url = str(r.share_url or "")
        ts = r.synced_at.isoformat(sep=" ", timespec="seconds") if r.synced_at else ""
        out.append({"pan_url": url, "pan_type": _pan_kind(url),
                    "accounts": 1,
                    "titles": [str(r.name or "")[:60]] if r.name else [],
                    "first_seen": ts, "last_seen": ts,
                    "my_link": url,                  # 本来就是我们自己的链
                    "source": "迅雷盘"})
    return out


def _xunlei_group_resources(session: Session, user_id: int, query: str = "",
                            days: int = 90, limit: int = 20) -> list[dict]:
    """**迅雷群组里转存进来的**资源(2026-10-05 补的出口)。结构与 `_xunlei_resources` 一致。

    ⚠️ **这条出口此前不存在,而缺口比 `XunleiResource` 那次还大**:实测本地库
    `xunlei_group_shares` **60 条里 40 条已经有 `our_url`**(群分享 → 转存 → 我方链,全链跑通),
    而 `xunlei_resources` 只有 **8** 条 —— 也就是说 **40 条"我们已经拥有的资源"在资源库里查不到**。
    后果与 `_xunlei_resources` 那次一模一样(`resource_library` 只并公众号链 + 公开发现链):
    ① 选题 Agent 的 `_library_evidence` 看到"库里没有" ⇒ 标成「待搬」而不是「已有链」;
    ② `pan_discovery.already_have` 也会判成"没有" ⇒ **去别的盘重复搬一份**(白占空间)。
    两张表都是"我方盘里的东西",只是**入库路径不同**(群分享 vs 扫盘/口令),
    所以形状照抄,只在 `source` 上区分,便于界面与排障认出来源。

    只收 **`our_url != ""`**(群分享需先转存才有我方链)—— 没链的放进来只会干扰,
    与 `_xunlei_resources` 跳过无 `share_url` 行是同一条理由。
    """
    from app.db.models import XunleiGroupShare

    stmt = select(XunleiGroupShare).where(XunleiGroupShare.user_id == user_id,
                                          XunleiGroupShare.our_url != "")
    if days:
        stmt = stmt.where(XunleiGroupShare.synced_at >= datetime.now() - timedelta(days=days))
    rows = session.scalars(stmt.order_by(XunleiGroupShare.synced_at.desc())).all()
    if query:
        # ⚠️ 同 `_xunlei_resources`:走 `match_key` 而不是 SQL `contains`(2026-10-08 修)。
        # 用户报的那个 bug 就出在这里:标题 `伪装直男【更至19】(1)等2个文件` 配上查询词
        # `伪装直男等2个文件` 在 SQL 里**匹配不上**(两段不相邻)。
        key = match_key(query)
        rows = [r for r in rows if key in match_key(r.title)]
    rows = rows[:limit]
    out: list[dict] = []
    for r in rows:
        url = str(r.our_url or "")
        ts = r.synced_at.isoformat(sep=" ", timespec="seconds") if r.synced_at else ""
        out.append({"pan_url": url, "pan_type": _pan_kind(url),
                    "accounts": 1,
                    "titles": [str(r.title or "")[:60]] if r.title else [],
                    "first_seen": ts, "last_seen": ts,
                    "my_link": url,                  # 本来就是我们自己的链
                    "source": "迅雷群"})
    return out


#: 名字里的「标注」:`【…】`/`（…）`/`(…)`/`[…]`
_MATCH_BRACKET_RE = re.compile(r"[【（(\[][^】）)\]]*[】）)\]]")
#: 匹配时一律去掉的标点与空白(中英混排)
_MATCH_PUNCT_RE = re.compile(
    r"[\s·・|｜/\\、,，。.:：;；!！?？—－\-~～+＋*＊#＃'\"“”‘’()（）\[\]【】《》<>_]")


def match_key(text: str) -> str:
    """**回库匹配用**的归一化:剥掉括号标注 + 去掉全部标点空白 + 转小写。

    ⚠️⚠️ **为什么必须有这一层**(2026-10-08 用户报的 bug):
    查询词是 `library_search_word` 造的,而它**会剥掉括号标注**;
    可回库匹配却是拿**原始标题**做子串比较。于是标题里只要**中间**夹着一处 `【…】`,
    就**永远匹配不回去**:

        原标题   `伪装直男【更至19】(1)等2个文件`
        查询词   `伪装直男等2个文件`   ← 由上面那条造出(括号被剥掉了)
        `title.contains(查询词)` → **False**(两段在原标题里不相邻)

    ⇒ 跨平台热度卡片那一行写「—(库内暂无链)」,而同一份资源的另一个名字变体
    (`伪装直男`)却能配上 —— 用户看到的就是「**一个有一个没有**」。

    ⚠️ 本模块早先那句注释写着「词就是资源库取的,所以必然能检索回去」——
    **那个假设正是被"造词时剥括号"打破的**。凡是"造词"与"回查"用两套口径的地方,
    迟早会出现这条缝。
    """
    t = _MATCH_BRACKET_RE.sub("", str(text or ""))
    return _MATCH_PUNCT_RE.sub("", t).lower()


def search_resources(session: Session, user_id: int, query: str,
                     days: int = 90, limit: int = 20) -> list[dict]:
    """关键词检索资源库:匹配文章标题,聚合到盘链级,按验证强度(号数)排序。

    命中规则:标题包含查询词(中文无分隔,直接子串;两字以下不检索防噪声)。
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    cutoff = datetime.now() - timedelta(days=days)
    # ⚠️ **匹配走 `match_key`(剥括号 + 去标点),不是 SQL 的子串比较**(2026-10-08 修)。
    # 原来这里是 `WechatArticle.title.contains(q)`,而 `q` 是 `library_search_word` 造出来的
    # —— 那个函数**会剥掉括号标注** ⇒ 只要标题**中间**夹一处 `【…】` 就永远匹配不回去
    # (实测 `伪装直男【更至19】(1)等2个文件` vs 查询词 `伪装直男等2个文件`)。
    # 聚合改到 Python 做,口径与原来的 SQL 一致(去重作者数、最早/最晚时间,按作者数倒序)。
    key = match_key(q)
    raw = session.execute(
        select(WechatPanLink.pan_url, WechatArticle.author,
               WechatArticle.created_at, WechatArticle.title)
        .join(WechatArticle, WechatArticle.id == WechatPanLink.article_id)
        .where(WechatPanLink.user_id == user_id,
               WechatArticle.created_at >= cutoff)).all()
    agg: dict[str, dict] = {}
    for pan_url, author, created_at, title in raw:
        if key not in match_key(title):
            continue
        a = agg.setdefault(str(pan_url), {"authors": set(), "first": None, "last": None})
        if author:
            a["authors"].add(author)
        if created_at is not None:
            a["first"] = created_at if a["first"] is None else min(a["first"], created_at)
            a["last"] = created_at if a["last"] is None else max(a["last"], created_at)
    rows = [(u, len(v["authors"]), v["first"], v["last"])
            for u, v in sorted(agg.items(), key=lambda kv: -len(kv[1]["authors"]))[:limit]]
    ours = _rows_to_resources(session, user_id, rows)
    # **并入公开平台发现的链**(2026-10-02):同一条链可能既被公众号发过、又被知乎贴过 ——
    # 以**公众号那条为准**(它带"多少号同发"这个更强的信号),只补公众号没有的。
    seen = {r["pan_url"] for r in ours}
    extra = [d for d in _discovered_resources(session, user_id, q, days, limit)
             if d["pan_url"] not in seen]
    # **并入我们自己迅雷盘里的资源**(2026-10-03):口令转存 + 扫盘进来的那些此前**完全没有出口**,
    # 补上以后 presence「名字型」也能匹配到它们(否则"搬进来了但用不上")。
    seen |= {d["pan_url"] for d in extra}
    mine = [x for x in _xunlei_resources(session, user_id, q, days, limit)
            if x["pan_url"] not in seen]
    # **并入迅雷群组里转存进来的那些**(2026-10-05):同样是"我方盘里的东西",只是入库路径
    # 不同(群分享转存,而非扫盘/口令)。实测 40 条已转存的群分享此前在库里查不到 ——
    # 直接后果是 `already_have` 判"没有"→ **去别的盘重复搬一份**,白占空间。
    seen |= {d["pan_url"] for d in mine}
    groups = [x for x in _xunlei_group_resources(session, user_id, q, days, limit)
              if x["pan_url"] not in seen]
    return (ours + extra + mine + groups)[:limit]


def resonance_resources(session: Session, user_id: int, days: int = 30,
                        min_accounts: int = 2, limit: int = 15,
                        order: str = "resonance") -> list[dict]:
    """高共振资源榜:同链被 ≥min_accounts 个号同发——需求被反复验证的金矿。

    `order`:
      · `"resonance"`(默认)—— 按**几个号同发**降序:被验证得越多越靠前;
      · `"fresh"` —— 按**最近一次被发**降序:谁**还在被发**谁靠前。

    ⚠️ **为什么要有 `fresh`**(2026-10-04,用户口径"**最重要的就是新鲜冒头的资源**"):
    "共振"是**沉淀过**的信号(30 天窗口里被反复发),而抖音线索那条链要的是
    "**现在正在冒头**"的词 —— 拿半年前的爆款去搜,搜到的推广号早就换话题了。
    两者都要,但**选词的链应该吃新鲜的那份**;`min_accounts` 这道"被验证过"的门槛照旧保留。
    """
    cutoff = datetime.now() - timedelta(days=days)
    stmt = (
        select(WechatPanLink.pan_url,
               func.count(func.distinct(WechatArticle.author)),
               func.min(WechatArticle.created_at), func.max(WechatArticle.created_at))
        .join(WechatArticle, WechatArticle.id == WechatPanLink.article_id)
        .where(WechatPanLink.user_id == user_id, WechatArticle.created_at >= cutoff)
        .group_by(WechatPanLink.pan_url)
        .having(func.count(func.distinct(WechatArticle.author)) >= min_accounts))
    if order == "fresh":
        stmt = stmt.order_by(func.max(WechatArticle.created_at).desc())
    else:
        stmt = stmt.order_by(func.count(func.distinct(WechatArticle.author)).desc())
    rows = session.execute(stmt.limit(limit)).all()
    return _rows_to_resources(session, user_id, rows)


#: 平台代号 → 中文(卡片上给人看;`discovered_pan_links.platform` 存的是英文代号)
_PLATFORM_CN = {"weibo": "微博", "tieba": "贴吧", "zhihu": "知乎", "douyin": "抖音",
                "douyin-kouling": "抖音口令", "xiaohongshu": "小红书", "bilibili": "B站",
                "kuaishou": "快手", "公众号": "公众号"}


def _cn(platforms) -> str:
    """平台列的中文串 —— ⚠️ 不认识的代号**原样留着**,别悄悄吞掉(那会让人以为没这个平台)。"""
    return "/".join(_PLATFORM_CN.get(str(p), str(p)) for p in (platforms or []))


#: 资源名里的**通用前后缀** —— 它们区分不出一份资源,只制造"同物异名"。
#: 归一化时剥掉,让「高性价比人生指南 pdf电子版 共338页」与「《高性价比人生指南》pdf」
#: 落到**同一个身份**上。
_CORE_PREFIX = ("今日分享的是", "今日分享", "分享一个", "亲测", "github", "GitHub",
                "值得一看", "推荐", "爆火", "最新", "2026", "2025", "2024", "【", "《")
# ⚠️ **「最新」必须放在后缀里**(2026-10-07 实测:我一开始把它当前缀,于是
# 「时代峰峻喜欢的脸top9投票最新」原样留下、和带「入口」的那条分成了两份 ——
# 同一个资源的两种写法就是这么散开的)。同理收下「共/等/附」这类**光杆尾巴**:
# 「高性价比人生指南pdf电子版共」剥掉它才继续往下剥「电子版/pdf」。
_CORE_SUFFIX = ("pdf", "PDF", "epub", "EPUB", "电子版", "完整版", "最新版", "最新", "高清",
                "免费", "可保存", "可下载", "可打印", "自取", "直达", "入口", "链接", "下载",
                "在线观看", "资源", "分享", "附教程", "附下载", "附最新", "共", "等", "附",
                "】", "》")
_CORE_TAIL_RE = re.compile(r"(共\s*\d+\s*页|\d+\s*页|第\s*\d+\s*篇|\d{2,})")
_CORE_PUNCT = " 	·・|｜/\、,，。.。:：;；!！?？—－-~～+＋*＊#＃'\"“”‘’()（）[]【】《》<>"


def core_resource_name(name: str) -> str:
    """把**清洗过的资源名**再归一化一层,得到用于"是不是同一份资源"的身份。

    ⚠️⚠️ **为什么要再来一层**(2026-10-07 实测踩到):`library_search_word` 是为
    **搜索词**设计的(必须具体才搜得到),**不是为"身份"设计的**。实测同一份
    《高性价比人生指南》在库里有 **20+ 个标题变体**,清洗后仍散成 **8 个不同的名字**:

        高性价比人生指南pdf电子版 / 高性价比人生指南pdf电子版共 / 高性价比人生指南共338页
        《高性价比人生指南》pdf/ / 2026高性价比人生指南. / github高性价比人生指南 …

    ⇒ 拿它当身份,共振榜会把**同一份资源数成好几条** —— 那正是它要解决的问题本身。
    所以这里剥掉通用前后缀与页数/日期尾巴,只留**指得到具体东西的那段核心**。

    ⚠️ 仍然**不是完美归一化**(真正的同义名「高性价比人生指南」vs「人生使用说明书」
    这种靠字符串救不了)。所以跨平台共振榜**先只读输出让人看**,别直接上卡片。
    """
    t = str(name or "").strip()
    if not t:
        return ""
    t = _CORE_TAIL_RE.sub("", t)
    t = t.strip(_CORE_PUNCT)
    # 前后缀各多剥几轮(「【值得一看】高性价比…pdf电子版【自取】」要剥三次)
    for _ in range(4):
        before = t
        low = t.lower()
        for p in _CORE_PREFIX:
            if low.startswith(p.lower()) and len(t) > len(p) + 3:
                t = t[len(p):].strip(_CORE_PUNCT)
                low = t.lower()
                break
        for suf in _CORE_SUFFIX:
            if low.endswith(suf.lower()) and len(t) > len(suf) + 3:
                t = t[: len(t) - len(suf)].strip(_CORE_PUNCT)
                low = t.lower()
                break
        if t == before:
            break
    t = t.strip(_CORE_PUNCT)
    return t if len(t) >= 4 else str(name or "").strip()


def recent_source_names(session: Session, user_id: int, days: int = 30,
                        limit: int = 40) -> list[str]:
    """**别的链**最近发现的资源名(迅雷群转存 / 我方迅雷盘 / 公开平台发现)。

    ## 为什么要单独有它(2026-10-07,用户口径)
    `resonance_resources` 的门槛是"**被 ≥2 个号同发**"(需求被反复验证)—— 那是**公众号**
    才有的信号。而迅雷群/小红书/B站/贴吧/知乎 发现的资源**没有这个信号**,
    于是它们**永远进不了抖音的搜索词池** ⇒ 抖音只会去搜"公众号上被多号发过的资源"。
    实测:用户看到「冒险岛国际服」「派出所模拟器」从**迅雷群**转存进来并推了卡,
    但抖音**永远不会去搜它们** —— 就是因为取词只看了共振榜。

    ⚠️ 这里**不做"被几个号验证"的门槛**(用户口径 a:**一视同仁当候选**)——
    它们的"验证"是**"进了别人的群 / 被人发出来了"**,与"多号同发"不是同一个信号,
    但**同样说明有人在推**。排序按**最近发生**,让新鲜的先被搜到。
    """
    cutoff = datetime.now() - timedelta(days=days)
    out: list[str] = []
    try:
        from app.db.models import DiscoveredPanLink, XunleiGroupShare

        for title in session.execute(
                select(XunleiGroupShare.title).where(
                    XunleiGroupShare.user_id == user_id,
                    XunleiGroupShare.synced_at >= cutoff).order_by(
                    XunleiGroupShare.synced_at.desc()).limit(limit)).scalars():
            if title:
                out.append(str(title))
        for title in session.execute(
                select(DiscoveredPanLink.title).where(
                    DiscoveredPanLink.user_id == user_id,
                    DiscoveredPanLink.found_at >= cutoff).order_by(
                    DiscoveredPanLink.found_at.desc()).limit(limit)).scalars():
            if title:
                out.append(str(title))
    except Exception:  # noqa: BLE001 - 取不到就退回只有共振榜,别让整条取词路崩
        logger.exception("取'其它源'资源名失败(退回只有共振榜)")
    return out


def cross_platform_resonance(session: Session, user_id: int, days: int = 90,
                             min_platforms: int = 2, min_accounts: int = 1,
                             limit: int = 20) -> list[dict]:
    """**跨平台共振榜**:同一份资源在**几个平台、被几个号**在推(2026-10-07)。

    ## 为什么不能沿用 `resonance_resources`
    那个数的是"同一**盘链**被几个**公众号**发过"。而跨平台**不能拿盘链当身份**:
    **每个推广号自己建分享链**,同一份资源在不同平台上是完全不同的 URL
    (2026-10-07 实测:两张表的盘链**零交集** ——
     `https://pan.baidu.com/s/1-0ttlsu0Zd8DWM1-QLOnFw` vs `http://pan.baidu.com/s/1AnM-_F9_5KmYQCIe7403Rg`)。

    ⇒ 身份换成**资源名**(`cross_accounts.library_search_word` 清洗出的主体名),
    于是"公众号 A + 微博 B + 贴吧 C 都在推同一个资源"这才**第一次能被数出来**。

    ⚠️⚠️ **名字归一化会误配**(同名的不同资源 / 同一资源的不同叫法),所以这个函数
    **先只读输出**,让运营者看准不准 —— **别急着接进卡片**。判据看两个:
      · `platforms` ≥2 且 `accounts` 也 ≥2:跨平台被不同人验证,是**真共振**;
      · 只有 1 个号却跨 2 个平台:多半是**同一个人多平台分发**,那是"矩阵号"不是共振。
    所以结果里两个数都给出来,让人自己看。
    """
    from app.services.cross_accounts import library_search_word

    cutoff = datetime.now() - timedelta(days=days)
    buckets: dict[str, dict] = {}

    def _add(raw_name: str, platform: str, account: str, pan_url: str) -> None:
        # ⚠️ **桶的键是核心名,不是清洗名**:同一份资源有 20+ 种标题写法(实测),
        #    拿清洗名当身份会把一份资源数成好几条 —— 见 `core_resource_name` 的实测。
        core = core_resource_name(raw_name)
        if not core:
            return                      # 洗不出名字的(太短/太泛/空标题)直接丢,别凑数
        b = buckets.setdefault(core, {"name": core, "variants": {},
                                      "platforms": set(), "accounts": set(), "pans": []})
        b["variants"][raw_name] = b["variants"].get(raw_name, 0) + 1
        b["platforms"].add(platform or "未知")
        if account:
            # ⚠️⚠️ **账号身份只按"人",不带平台前缀**(2026-10-07 实测踩到):
            # 原来存 `f"{platform}:{account}"` ⇒ **同一个人跨 3 个平台被算成 3 个号** ——
            # `min_accounts` 于是完全失效,而它挡的正是"矩阵号"(同一个人多平台分发,
            # 那不是"需求被验证过")。平台维度已经由 `platforms` 单独记着,不必混进账号里。
            b["accounts"].add(str(account))
        if pan_url:
            b["pans"].append(pan_url)

    # ① 公众号(含量最大、也是唯一带"同一链被几号同发"的那份)
    for t, a, u in session.execute(
            select(WechatArticle.title, WechatArticle.author, WechatPanLink.pan_url)
            .join(WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
            .where(WechatPanLink.user_id == user_id, WechatArticle.created_at >= cutoff)):
        _add(library_search_word(str(t or "")), "公众号", str(a or ""), str(u or ""))

    # ② 公开平台发现(微博/贴吧/知乎/抖音…)—— 这一份以前**从不参与共振计数**
    from app.db.models import DiscoveredPanLink
    try:
        for p, t, a, u in session.execute(
                select(DiscoveredPanLink.platform, DiscoveredPanLink.title,
                       DiscoveredPanLink.author, DiscoveredPanLink.origin_url)
                .where(DiscoveredPanLink.user_id == user_id)):
            _add(library_search_word(str(t or "")), str(p or ""), str(a or ""), str(u or ""))
    except Exception:                   # noqa: BLE001 - 这张表缺列/空都不该拖垮整榜
        logger.exception("跨平台共振:读公开平台发现失败(其余照常)")

    out: list[dict] = []
    for b in buckets.values():
        if len(b["platforms"]) < min_platforms:
            continue
        # ⚠️ `min_accounts` 挡的是**"矩阵号"不是共振**:同一个人在多平台发同一份资源
        # 也能凑出"平台数=2",但那说明不了"需求被验证过"。给人看的榜可以留 1(配 `platforms`
        # 一起判),给 agent 当信号时必须 ≥2,否则它会把自家人多平台分发读成市场热度。
        if len(b["accounts"]) < min_accounts:
            continue
        pans = [u for u in dict.fromkeys(b["pans"]) if u]   # 去重保序
        my = ""
        for u in pans[:5]:              # 抽前几条查"我们有没有" —— 不必全查
            try:
                my = _my_link_of(session, user_id, u) or _discovered_my_link(session, user_id, u)
            except Exception:           # noqa: BLE001
                my = ""
            if my:
                break
        shown = max(b["variants"], key=b["variants"].get)   # 最常见的那个写法当标题
        out.append({
            "name": b["name"],
            "shown": shown,
            "variant_count": len(b["variants"]),            # 合并了几种写法(归一化效果的量尺)
            "platforms": sorted(b["platforms"]),
            "platform_count": len(b["platforms"]),
            "accounts": sorted(b["accounts"]),
            "account_count": len(b["accounts"]),
            "pan_count": len(pans),
            "pans": pans[:3],
            "my_link": my,
        })
    # 排序:**平台数**优先(跨平台才是这条榜的价值),再按号数
    out.sort(key=lambda x: (-x["platform_count"], -x["account_count"], x["name"]))
    return out[:limit]


def resource_profile(session: Session, user_id: int, pan_url: str) -> dict | None:
    """单资源画像:号数/时间线/我方链。未知链返回 None。"""
    row = session.execute(
        select(func.count(func.distinct(WechatArticle.author)),
               func.min(WechatArticle.created_at), func.max(WechatArticle.created_at))
        .join(WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
        .where(WechatPanLink.user_id == user_id, WechatPanLink.pan_url == pan_url)).one()
    if not row or not row[0]:
        # 公众号里没有 → 看是不是**公开平台发现的**那条链
        found = _discovered_resources(session, user_id, limit=200)
        for d in found:
            if d["pan_url"] == pan_url:
                return d
        return None
    res = _rows_to_resources(session, user_id, [(pan_url, row[0], row[1], row[2])])
    return res[0] if res else None


def library_summary(session: Session, user_id: int, days: int = 90) -> dict:
    """资源库概览(CLI/前端展示用):总链数/多号验证数。

    `days` 是**同一个时间窗口**,两个数字必须同口径:此前 `cutoff` 算了却没用,
    `total_links` 统计的是**全历史**、而 `multi_account` 是近 days 天 —— 前端同屏
    显示两个不同尺度的数字,看着像"总链 500 条,其中多号验证只有 12 条",其实是拿
    90 天的分子比全历史的分母(2026-10-01 审查发现)。
    """
    cutoff = datetime.now() - timedelta(days=days)
    total = session.scalar(select(func.count(func.distinct(WechatPanLink.pan_url))).where(
        WechatPanLink.user_id == user_id,
        WechatPanLink.created_at >= cutoff)) or 0
    verified = len(resonance_resources(session, user_id, days=days, min_accounts=2, limit=10000))
    discovered = len(_discovered_resources(session, user_id, days=days, limit=10000))
    return {"total_links": int(total), "multi_account": int(verified),
            "discovered": discovered, "days": days}


def push_cross_platform_resonance(session: Session, user_id: int, settings=None,
                                  days: int = 30, min_platforms: int = 2,
                                  min_accounts: int = 2, limit: int = 12) -> bool:
    """跨平台共振榜 → 飞书卡(2026-10-07,用户口径「每天推一次」)。

    去向:`multiplatform` 板块(即「多平台监控」群,未配回落主群)。

    ⚠️ **门槛是 `平台≥2` **且** `号数≥2`,不是"平台≥2"就够**:
    同一个人在多平台发同一份资源也能凑出平台数 2,但那说明不了"需求被验证过"
    —— 那是**矩阵号**,不是共振。宁可少推几条。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    rows = cross_platform_resonance(session, user_id, days=days,
                                    min_platforms=min_platforms,
                                    min_accounts=min_accounts, limit=limit)
    if not rows:
        return False
    from app.services.feishu._cards import _col_set_row, _md_safe
    from app.services.feishu_client import FeishuClient, webhook_for

    hook = webhook_for(settings, "multiplatform")
    if not hook:
        return False
    brand = (getattr(settings, "brand_name", "") or "").strip()
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content":
        f"近 {days} 天里,**同一份资源在多个平台被人同时推** —— 需求被反复验证过的:"
        f"（按平台数排序,⚠️ 只看「几个平台/几个号」,不看我方有没有）"}},
        _col_set_row([("**资源**", 6), ("**平台**", 3), ("**号数**", 2), ("**我方链**", 2)],
                     grey=True)]
    for r in rows:
        my = r.get("my_link") or ""
        cell = "[▶ 打开]({})".format(_md_safe(my)) if my else "—"
        elements.append(_col_set_row([
            (_md_safe(str(r.get("name") or "")[:40]), 6),
            (_md_safe(_cn(r.get("platforms"))[:24]), 3),
            (str(r.get("account_count") or 0), 2),
            (cell, 2)]))
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "orange", "title": {"tag": "plain_text",
                                                       "content": f"🔥 {brand + ' · ' if brand else ''}"
                                                                  f"全平台共振榜 · {len(rows)} 个"}},
            "elements": elements}
    try:
        return FeishuClient(hook, getattr(settings, "feishu_secret", "")).send_card(card)
    except Exception:  # noqa: BLE001 - 推送失败不该影响采集
        logger.exception("跨平台共振榜推送失败")
        return False


def cross_resonance_tick(settings=None) -> int:
    """定时入口(每天一次):全平台共振榜推「多平台监控」群。返回发送成功数。

    ⚠️ 与 `push_viral_alerts` 那种"事件驱动"不同,这条是**固定节奏的日报** ——
    共振是**沉淀信号**(窗口期内被反复验证),不会因为晚看两小时就变。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.db import get_session_local
    from app.db.models import User

    db = get_session_local()()
    sent = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                if push_cross_platform_resonance(db, uid, settings):
                    sent += 1
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("跨平台共振榜推送失败 user=%s", uid)
    finally:
        db.close()
    return sent


def detect_viral_resources(session: Session, user_id: int, hours: int = 24,
                           min_accounts: int = 3, limit: int = 5) -> list[dict]:
    """资源级爆款检测(2026-10-01):近 N 小时被 ≥min_accounts 个号**新同发**的盘链。

    与资源库(全历史沉淀)的区别:只看**近期新增**——多号突然同发同一资源是爆款苗头
    (回灌实证:花少2人格测试被 13 个号同发),人还没反应过来时跟进去,转存扩散最强。
    返回按号数降序的资源列表(含我方链状态)。
    """
    cutoff = datetime.now() - timedelta(hours=hours)
    rows = session.execute(
        select(WechatPanLink.pan_url,
               func.count(func.distinct(WechatArticle.author)),
               func.min(WechatArticle.created_at), func.max(WechatArticle.created_at))
        .join(WechatArticle, WechatArticle.id == WechatPanLink.article_id)
        .where(WechatPanLink.user_id == user_id, WechatArticle.created_at >= cutoff)
        .group_by(WechatPanLink.pan_url)
        .having(func.count(func.distinct(WechatArticle.author)) >= min_accounts)
        .order_by(func.count(func.distinct(WechatArticle.author)).desc())
        .limit(limit)).all()
    return _rows_to_resources(session, user_id, rows)


def push_viral_alerts(session: Session, user_id: int, settings=None) -> int:
    """爆款资源预警 → 飞书(每小时 tick 调用;每链 48h 冷却;已转存/未转存分别提示)。"""
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.services.alert_service import feishu_alert_gate
    from app.services.feishu_client import FeishuClient, webhook_for

    hook = webhook_for(settings, "")
    if not hook:
        return 0
    sent = 0
    for r in detect_viral_resources(session, user_id):
        if not feishu_alert_gate(session, user_id, "viral_res",
                                 f"viral:{r['pan_url']}", 48, "爆款资源预警"):
            continue
        title = r["titles"][0] if r["titles"] else "(无标题)"
        lines = [f"🔥 爆款资源在疯传(近24h 已被 {r['accounts']} 个号同发)",
                 f"📄 {title}",
                 f"🧭 {r['pan_type']} · 最近 {r['last_seen'][:10]}"]
        if r["my_link"]:
            lines.append("📦 我方链接(已转存,点开即用):")
            lines.append(r["my_link"])
        else:
            lines.append("⏳ 尚未转存——建议尽快转存跟上,这种多号同发的需求扩散最强")
        if FeishuClient(hook, settings.feishu_secret).send(chr(10).join(lines)):
            sent += 1
    return sent
