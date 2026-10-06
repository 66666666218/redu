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


def find_leads(keywords: list[str], limit: int = 30, platform: str = "douyin") -> list[dict]:
    """搜抖音 → 挑出标题带 `《…》` 前缀的推广线索。

    返回 `[{mark, title, url, keyword}]`;`mark` 是《》里那段 —— **迅雷分享口令**(可直接去迅雷搜)。
    ⚠️ 会**开浏览器**(MediaCrawler),一次几分钟 —— 只该低频跑。

    关键词**一次性全给** MediaCrawler(它的 CLI 吃整个列表,逐词调用等于反复开关浏览器)。
    """
    from app.services import mediacrawler_source as mc

    if not keywords:
        return []
    # ⚠️ `mc.crawl` 的硬失败(未装/扫码超时/非零退出)会抛 `MediaCrawlerError`,这里**不吞** ——
    # 让它一路冒到 `douyin_leads_tick` 记 `failed`。否则"扫码没通过"会被记成
    # `success(线索0)`,而这条链**只在每天 11:00 无人值守时跑**,失败你收不到任何信号。
    out: list[dict] = []
    seen: set[str] = set()
    for h in mc.crawl(platform, keywords):
        text = (h.get("snippet") or "").strip()
        url = (h.get("url") or "").strip()
        if not url or url in seen:
            continue          # 拿不到视频链 / 同一个视频(多词命中)去重
        name = h.get("name") or ""
        mark = _lead_mark(text, name)
        if not mark:
            continue          # 标题里没有《…》 → 不是线索
        seen.add(url)
        out.append({"mark": mark, "title": text[:120], "url": url,
                    "author": name,                     # 账号名(**被工具脱敏**,如「籽***」)
                    "keyword": h.get("keyword", ""),
                    # 转发量(结算用):衡量**这个资源在抖音有多热**;⚠️ 是**别人视频**的数,
                    # 不等于我们自己发文的转化(见 DouyinLead/lead_settlement 的口径说明)。
                    "share_count": int(h.get("share_count") or 0),
                    "aweme_id": _aweme_id(url),         # 去重键(同一视频会被多个词命中)
                    "_rank": _lead_rank(text, name)})
    out.sort(key=lambda x: x["_rank"])      # 强信号排前面(开头《》> 与昵称吻合 > 其它)
    for x in out:
        x.pop("_rank", None)
    return out[:limit]


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
    text = re.split(r"[|｜\-—·,，、:：!！?？]", text)[0]
    # 再丢掉**版本号式的尾串**(「2.0版」「v3」「2024版」)—— 它们搜不出东西
    tokens = [t for t in text.split()
              if t and not re.match(r"^[vV]?\d", t) and not t.endswith("版")]
    word = " ".join(tokens).strip()
    if len(word) < 4 or is_bulk_resource(word):
        return ""
    return word[:12]


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
    kws = category_topics.pick(cand, cat, int(top), names)
    # 两个判据都要,**顺序不能反**:
    #   ① `seen`(已经解出过群)排后 —— 用户口径:「而不是一直用着一个口令进群」;
    #   ② 有效率(高→低) —— 同类之间的排序依据。
    seen = _words_already_resolved_to_group(session, user_id)
    kws.sort(key=lambda w: (w in seen, -scores.get(w, _YIELD_PRIOR)))
    for w in (hot or []):
        if w and w not in kws:
            kws.append(w)
    return kws


def _enqueue_later(session, user_id: int, info: dict, mark: str) -> None:
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
    row = session.scalar(select(DiscoveredPanLink).where(
        DiscoveredPanLink.user_id == user_id, DiscoveredPanLink.origin_url == url))
    if row is None:
        session.add(DiscoveredPanLink(
            user_id=user_id, platform="douyin", origin_url=url[:500],
            title=str(mark)[:255], status="pending",
            message="抖音线索:转存未成(盘满/超额度),等下一轮重试"))
    elif row.status not in ("ok", "skipped"):
        row.status = "pending"
        row.message = "抖音线索:转存未成,等下一轮重试"
    session.commit()


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
            _enqueue_later(session, user_id, info, mark)
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
            _enqueue_later(session, user_id, info, mark)
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
    from datetime import date

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
        row.mark = str(ld.get("mark") or "")[:64]
        row.title = str(ld.get("title") or "")[:255]
        row.author = str(ld.get("author") or "")[:64]
        row.url = str(ld.get("url") or "")[:500]
        row.keyword = str(ld.get("keyword") or "")[:64]
        row.share_count = int(ld.get("share_count") or 0)
        row.kind = str((ld.get("kouling") or {}).get("kind") or "")[:16]
        # **这条线索搬成了哪条链**(2026-10-04 补):原来落库时丢了,事后查不出来
        row.our_url = str((ld.get("kouling") or {}).get("our_url") or "")[:500]
        n += 1
    return n


def _kouling_line(ld: dict) -> str:
    """把解析/转存结果渲染成卡片上的一行(让运营一眼看出这条线索值不值钱)。"""
    info = ld.get("kouling") or {}
    kind, status = info.get("kind"), info.get("status")
    if kind == "share" and status == "ok":
        return f"✅ **已自动转存进你的盘**,我方分享链:[▶ 点这里打开]({info.get('our_url') or ''})"
    if kind == "share" and status == "already":
        return "✅ 之前已转存过(库里已有)"
    if kind == "share" and status == "over_budget":
        return f"⏸ 本轮转存额度用完,未搬(原链 {info.get('share_url') or ''})"
    if kind == "share" and status == "disk_full":
        return f"⏸ 盘满未搬({info.get('message') or ''}) —— 清出空间后会再试。原链:{info.get('share_url') or ''}"
    if kind == "share" and status == "skipped":
        return f"⏸ 未搬({info.get('message') or '被闸门挡下'}) —— 原链:{info.get('share_url') or ''}"
    if kind == "share" and status == "failed":
        return f"⚠️ 转存失败:{info.get('message') or ''}"
    if kind == "group":
        return "👥 指向**群组**,已加群(群里的资源由群采集自动收)"
    if kind == "none":
        return "· 未解析出资源(可能只是剧名/普通词)"
    if kind == "error":
        return f"⚠️ **没能解析/转存**(通常是我方迅雷登录态失效,不是你网络问题):`{info.get('message') or ''}`"
    if kind == "off":
        return "· 自动转存已关闭,仅作线索"
    return ""


def _pan_name(url: str) -> str:
    """链接属于哪个网盘 → **中文盘名**(夸克/百度/迅雷);认不出返回空串。

    ⚠️ 用户口径(2026-10-04):"**资源不要写我方链接,那个网盘就写那个网盘名称**"。
    卡片的「资源」列原来写的是 `🔴我方链` —— "**我方**"二字**在客户群里会暴露我们是运营方**,
    换成中性的**盘名**既说清了"这条是哪个盘、能直接取",又不露身份。
    """
    u = (url or "").lower()
    if "quark.cn" in u:
        return "夸克"
    if "baidu.com" in u:
        return "百度"
    if "xunlei.com" in u:
        return "迅雷"
    return ""


def push_leads(leads: list[dict], settings, platform: str = "douyin") -> bool:
    """把线索推飞书。

    **推抖音专属群**(`FEISHU_WEBHOOK_DOUHOT`,未配则回落主群)——用户口径(2026-10-02):
    "既然是抖音的来源就推送到抖音群聊里面"。版式与公众号推送一致:**四列网格**
    (作者 / 作品 / 资源 / 链接),不靠空格对齐。
    """
    if not leads:
        return False
    from app.services.feishu_client import webhook_for

    section = (PLATFORMS.get(platform) or {}).get("section", "douhot")
    # 抖音线索也是**内容卡** → 未配专属群时落客户群是对的。
    # (末尾那个 `or feishu_webhook_admin` 是够不到的死代码:主群已配时 webhook_for 永不返回空串)
    webhook = webhook_for(settings, section) or getattr(settings, "feishu_webhook_admin", "")
    if not webhook:
        logger.info("抖音线索:未配飞书 webhook,跳过推送")
        return False

    from app.services.feishu._cards import _col_set_row, _md_safe, strip_others
    from app.services.feishu_client import FeishuClient

    label = (PLATFORMS.get(platform) or {}).get("label", platform)
    # 「条数」= **这个资源本轮被几条视频在推** —— 用户口径(2026-10-04):
    # 一眼看出"大家都在抢这个"。
    # ⚠️ **按"资源身份"分组,不是按口令**:同一个资源会被不同推广号起**不同口令**
    # (实测:《齐民要术》《人生使用说明书》其实都是《高性价比人生指南》的别名)——
    # 按口令分会把同一份资源算成好几条。**能拿到原始链的就用它当身份**(那才是资源本身),
    # 拿不到的(群口令/没解出来)才回落到口令。
    from collections import Counter

    def _res_key(ld_: dict) -> str:
        info_ = ld_.get("kouling") or {}
        return str(info_.get("share_url") or "") or f"mark:{ld_.get('mark') or ''}"

    _counts = Counter(_res_key(x) for x in leads)

    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content":
        f"{label}上发现 **{len(leads)}** 条在推同类资源的视频。"
        "已自动解析并把能拿到的**转存进我方网盘**,可直接取用。"}},
        # **六列网格**(用户口径 2026-10-04):作者 / 作品 / 资源 / 视频 / 转发数 / 条数
        # ⚠️ 两处可点(**照公众号那套**):「作品」点开是**抖音视频**、「资源」点开是**我方链**;
        #    「资源」写的是**网盘名**(夸克/百度/迅雷)而**不是链接文本** —— 见 `_pan_name`。
        _col_set_row([("**作者**", 3), ("**作品**", 5), ("**资源**", 2), ("**视频**", 2),
                      ("**转发数**", 2), ("**条数**", 1)], grey=True)]
    for ld in leads:
        info = ld.get("kouling") or {}
        author = _md_safe(ld.get("author") or "—")
        title = _md_safe(strip_others(ld.get("title") or ""))
        shown = title[:22] + ("…" if len(title) > 22 else "")
        vurl = _md_safe(ld.get("url") or "")
        # 「作品」列:**标题本体可点** → 抖音视频(公众号卡片的「文章」列就是这个做法)
        work = f"[{shown}]({vurl})" if vurl else shown
        # 转发量**独立成列**(原来并进作品列,六列版式下挪出来)。没有就不显示,
        # 不拿 0 冒充有数据 —— 口径见 `DouyinLead` 的注释(它是**别人视频**的转发量)。
        sc = int(ld.get("share_count") or 0)
        # 「资源」列:**网盘名 + 可点(→ 我方链)**;没搬的写明**为什么**
        pan = _pan_name(info.get("our_url") or info.get("share_url") or "")
        if info.get("kind") == "share" and info.get("status") == "ok":
            res = f"[🔴{pan or '已转存'}]({_md_safe(info.get('our_url') or '')})"
        elif info.get("kind") == "group":
            res = "👥已加群"
        elif info.get("status") == "already":
            res = f"🔴{pan or '已转存'}(转过)"
        elif info.get("status") == "disk_full":
            res = f"⏸{pan or '盘'}(网盘已满)"
        elif info.get("status") == "over_budget":
            res = f"⏸{pan or '盘'}(本轮额度)"
        elif info.get("status") == "skipped":
            res = f"⏸{pan or '盘'}(被挡下)"
        else:
            res = "—"
        elements.append(_col_set_row([
            (author, 3),
            (work, 5),
            (res, 2),
            (f"[▶视频]({vurl})" if vurl else "—", 2),
            (f"↗{sc}" if sc else "—", 2),
            (str(_counts.get(_res_key(ld), 1)), 1)]))
    card = {
        "config": {"wide_screen_mode": True},
        # ⚠️ **不带头部品牌名**(用户口径 2026-10-04:"**不要带念飞思雪**")。
        # 这是对 2026-10-02 那条"品牌词只进标题"口径的**变更** —— 当时想让品牌露个脸,
        # 但实操下来:① 卡片是发到**客户群/抖音群**的,头部挂运营方品牌**没有给客户的价值**;
        # ② 与「资源」列**同一动机** —— 客户群里不该暴露"这是谁在运营"。
        # (真正需要品牌的地方是**作品标题里的替换**(`_rebrand`),那套没动。)
        "header": {"template": "purple", "title": {"tag": "plain_text",
                                                   "content": f"🎯 {label}推广线索 · {len(leads)} 条"}},
        "elements": elements,
    }
    try:
        return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(card)
    except Exception:  # noqa: BLE001 - 推送失败不该影响采集
        logger.exception("抖音线索推送失败")
        return False


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
            leads = find_leads(kws, platform=plat)
            total += len(leads)
            if leads:
                # 口令 → 资源(分享链直接转存入库 / 群则加群),结果一并写进卡片
                apply_kouling(leads, db, user_id, settings)
                _save_leads(db, user_id, leads)      # 落库:转发量只在这一次有效(结算要用)
                push_leads(leads, settings, platform=plat)
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
            _record_run(db, user_id, "douyin_leads", "success",
                        f"{plat} 类目{cat} 词{len(kws)}(其中外部{n_hot_used}) "
                        f"线索{len(leads)} {_kouling_summary(leads)} "
                        f"[外部命中{n_hot}/产出{n_hot_ok}]")
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
