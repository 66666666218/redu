"""类目 → 具体话题,以及**按类目轮换出词**(2026-10-04,用户口径)。

**用户的设计(逐字)**:
> "你脑子里另有一张表(比如"影视"下面挂"短剧/漫剧/解说","资料"下面挂"四六级/考公/模板"…),
>  **轮换类目出词**:这轮搜"资料"、下轮搜"影视",而不是永远同一类,
>  你抖音搜索就跟着群里面的资源名字走结合资源库里面的名称"

**它解决什么**:抖音那条链原来每轮都用同一批词(群里的资源名 + 资源库名),于是**反复命中
同一批群** —— 实测 27 条线索里 10 条解析成群、而群只从 9 → 10(**自循环**)。
按类目轮换就换了一批群:这轮只出「资料」类的词,下轮「影视」类…… 广度由**轮换**保证,
而不是靠"永远搜最热的那个"。

**类目词表从哪来**:⚠️ 下面 `DEFAULT_CATEGORIES` 只是**草稿种子**,**不是全集** ——
用户 2026-10-04 明确:"上面那些类目只是举例但是**并不是全部**,**不要仅仅局限这几个**"。
真正生效的表读 `.env` 的 **`LEAD_CATEGORIES`**(格式 `类目:话题1,话题2|类目2:话题3`),
**加类目/加话题不用改代码**;留空才回落到种子。

⚠️ **这张表是类目口径的唯一来源**。别的模块要判品类请 import 它,不要再各写一份 ——
本项目最忌讳"两处各有一套口径"(今天已经在"知乎选词""贴吧有没有盘链"上各栽过一次)。
"""
from __future__ import annotations

import re

from sqlalchemy import select

# ⚠️ **草稿种子,不是全集**(见模块说明)。顺序 = 轮换顺序。
# 想加类目/话题:优先改 `.env` 的 `LEAD_CATEGORIES`(不用动代码);
# 想把新的一类固化成默认,再加到这里。
DEFAULT_CATEGORIES: dict[str, tuple[str, ...]] = {
    # 「资料」= **学习资料**为主(四级/考公/小学学习资料……),但**类目宽、可以泛化** ——
    # 用户 2026-10-04:"资料基本都是学习资料,就你理解的四级、考公、小学学习资料等一些"
    # + "**并不用收窄,你因为类目很宽所以可以泛化很多**"。
    # ⚠️ 我一度把它收窄成"只收学习类",**被用户否掉** —— 宁可宽,让更多资源名有类目可归,
    # 也别让它们全落进"未分类"。真正的边界由用户给的实例校准(见 `TestUserRealExamples`)。
    "资料": ("真题", "试题", "试卷", "答案", "题库", "课件", "讲义", "笔记", "提纲",
             "知识点", "教辅", "课程", "教程", "考研", "考公", "考编", "四六级", "雅思",
             "托福", "期末", "复习", "小学", "初中", "高中", "语文", "数学", "英语",
             "物理", "化学", "模板", "表格", "PPT", "简历", "素材", "字体", "笔刷",
             "壁纸", "头像", "表情包", "图标", "报告", "手册", "指南", "学习资料", "资料",
             "文档", "PDF", "pdf", "电子书", "书单", "清单", "论文", "合集"),
    "影视": ("短剧", "漫剧", "电影", "电视剧", "剧集", "全集", "解说", "番剧", "动漫", "剧"),
    # ⚠️ **别把"入口"这类通用词放进来**(2026-10-04 实测踩到):资源名里
    # "…入口链接直达" 到处都是,会把一堆非问卷的东西误判进本类。
    "问卷": ("问卷", "调研", "测评", "量表", "测试"),
    # ⚠️ 补常见**主题词**(2026-10-04):光有"软件/安装包"判不出 `PS教程` 这类 ——
    # 主题词才是决定性的("教程"已降为通用词,见 `GENERIC_HINTS`)。
    "软件": ("软件", "安装包", "绿色版", "激活", "工具", "客户端", "补丁", "模拟器", "插件",
             "脚本", "源码", "代码", "APP", "app", "破解", "汉化", "免安装", "便携版",
             "PS", "ps", "PR", "AE", "Office", "office", "Excel", "excel", "WPS", "wps",
             "剪辑", "修图", "抠图", "投屏", "车机", "刷机", "驱动", "系统"),
    # ⚠️ 这批词是**用户给的真实例子**倒推出来的(2026-10-04):`孙宇晨小作文` → 大瓜 ——
    # 原来只有"爆料/吃瓜/热搜"这类,**认不出"小作文"**这种实际说法。后面还会继续补。
    # ⚠️ **大瓜靠"事件词"判,不靠人名**(用户 2026-10-04:"大瓜一般标题上都会有明星或者
    # 公众人物网红的名字")。**我们没有人名库** —— 明星名天天变,写死必然过期;
    # 而**事件词是稳定的**:不管主角是谁,"塌房/分手/实锤"都在。所以按事件词建表。
    # (想再准一层?可以**从热榜自动积累人名**——微博/百度热搜里大量是"XX+事件"的人名条目,
    #  那是后续可做项,见交接说明。)
    "大瓜": ("小作文", "爆料", "吃瓜", "热搜", "回应", "声明", "翻车", "道歉", "塌房",
             "内幕", "争议", "骂战", "开撕", "互撕", "内涵", "取关", "黑料", "实锤",
             "辟谣", "澄清", "退圈", "封杀", "被抓", "被捕", "判刑", "起诉", "和解",
             "分手", "离婚", "恋情", "恋爱", "官宣", "复合", "出轨", "绯闻", "撕破脸",
             "喊话", "diss", "打脸", "人设", "出圈", "翻旧账"),
}

_CONFIG_KEY = "lead_word_category"          # 轮换游标(存在 system_config,重启不丢)


def parse_categories(raw: str) -> dict[str, tuple[str, ...]]:
    """`资料:真题,考公|影视:短剧,漫剧` → `{"资料": ("真题","考公"), "影视": ("短剧","漫剧")}`。

    解析不出东西就返回空 dict(由 `categories()` 回落到种子)—— **宁可回落,也别给个半截表**。
    """
    out: dict[str, tuple[str, ...]] = {}
    for chunk in (raw or "").split("|"):
        name, _, topics = chunk.partition(":")
        name = name.strip()
        hits = tuple(t.strip() for t in topics.split(",") if t.strip())
        if name and hits:
            out[name] = hits
    return out


def categories() -> dict[str, tuple[str, ...]]:
    """**生效的**类目表:`.env` 的 `LEAD_CATEGORIES` 优先,留空/解析不出则用种子。"""
    from config.settings import get_settings

    try:
        raw = str(getattr(get_settings(), "lead_categories", "") or "")
    except Exception:  # noqa: BLE001 - 配置读不到就用种子,别让这条链整个挂掉
        raw = ""
    return parse_categories(raw) or DEFAULT_CATEGORIES


# **通用后缀词**:只描述"形态/容器",**不携带类目信息**。
#
# ⚠️ 它们必须**降权**,否则会和具体话题词**平票**,再按表序落到**错的类目**(2026-10-04 实测):
#     `问卷模板` → 资料("模板"1 票 vs "问卷"1 票 → 表序 资料 赢)← 错,该是问卷
#     `吃瓜合集` → 资料("合集"1 票 vs "吃瓜"1 票)                ← 错,该是大瓜
# 现在**具体词 2 分、通用词 1 分**:这两例都变成 2:1,判对。
GENERIC_HINTS = frozenset({
    "合集", "全套", "完整版", "大全", "资源", "资料", "文档", "素材", "模板", "表格",
    "图片", "视频", "图标", "壁纸", "字体", "笔刷", "头像", "表情包",
    # ⚠️ **"教程/课程"也是通用词**(用户 2026-10-04:"**教程需要分清楚软件还是什么教程**"):
    #   `PS教程` 是**软件**教程、`小学数学教程` 是**学习资料** —— 光看"教程"判不出来,
    #   得看**主题词**(PS / 小学·数学)。降权后由主题决定,同"入口"的处理。
    "教程", "课程",
})


# 抽"候选人名"时要挡掉的**泛称/功能词** —— 它们在瓜标题里反复出现,但不是名字。
# ⚠️ 不挡掉的话,"某明星"这种会因为太常见而攒够阈值,反而把真名字淹掉。
_NAME_STOP = frozenset({
    "某明星", "明星", "网红", "网友", "公众人物", "工作室", "官方", "本人", "当事人",
    "全网", "最新", "完整版", "合集", "事件", "回应", "道歉", "塌房", "爆料", "吃瓜",
})


def extract_candidate_names(title: str) -> list[str]:
    """从标题里抽**候选人名**:先去掉已命中的话题词,再从剩下的**纯中文片段**里取 2~4 字。

    例:`孙宇晨小作文` → 去掉"小作文" → `["孙宇晨"]`。
    """
    t = title or ""
    for hints in categories().values():
        for h in hints:
            t = t.replace(h, " ")
    out: list[str] = []
    for seg in re.split(r"[^一-鿿]+", t):
        seg = seg.strip()
        if 2 <= len(seg) <= 4 and seg not in _NAME_STOP and seg not in out:
            out.append(seg)
    return out


def learn_names(session, title: str) -> int:
    """把标题里的候选人名各记一次(用户:"大瓜**慢慢的学习**可以")。返回记了几个。

    **真的用不着写死名单**:被反复提到的名字会自然攒够次数浮上来,
    `某明星` 这类泛称被停用词挡在外面。
    """
    from app.db.models import NameLexicon

    n = 0
    for nm in extract_candidate_names(title):
        row = session.get(NameLexicon, nm)
        if row is None:
            session.add(NameLexicon(name=nm, hits=1))
        else:
            row.hits = (row.hits or 0) + 1
        n += 1
    if n:
        session.commit()
    return n


def known_names(session, min_hits: int = 2) -> set[str]:
    """已学到的名字(**攒够 `min_hits` 次**才算,避免一次性的巧合名词混进来)。"""
    from app.db.models import NameLexicon

    return {r.name for r in session.scalars(
        select(NameLexicon).where(NameLexicon.hits >= min_hits))}


def classify(text: str, names: set[str] | None = None) -> str:
    """文本属于哪个类目?认不出返回空串(**不猜** —— 猜错会把词出到错的类里去)。

    **计分规则**:
      ① **按命中次数累加**,不是"第一个命中的类目就赢" ——
         用户给的实例逼出来的: `2026性格测试｜七宗罪&七美德测试入口+完整版操作教程`
         应归**问卷**,但它同时含资料类的"教程";按顺序优先会先撞上资料而判错。
      ② **具体话题词 2 分、通用后缀词 1 分**(见 `GENERIC_HINTS`)——
         否则 `问卷模板`/`吃瓜合集` 会因为"模板/合集"与"问卷/吃瓜"平票而落到错的类目。
      ③ 仍平票时,按**类目表的顺序**。

    ⚠️ 这也解释了**为什么"入口"不能进词表**:它在软件类(超人模拟器|入口)和问卷类
    (测试入口)里都出现,**本身不携带类目信息**,放进去只会制造平票和误判。
    """
    word = text or ""
    best, best_score = "", 0
    for cat, hints in categories().items():
        score = sum(word.count(h) * (1 if h in GENERIC_HINTS else 2) for h in hints)
        if score > best_score:
            best, best_score = cat, score
    if best:
        return best
    # **弱信号**:话题词一个都没命中,但标题里带**已学到的人名** → 它是瓜的候选。
    # ⚠️ 用户原话是"如果带人名就**可去判断一下**" —— 是"去看看",**不是"直接收"**,
    # 所以它排在所有类目之后,只在"别的都判不出来"时才用。
    if names and any(n in word for n in names):
        return "大瓜"
    return ""


def current_category(session) -> str:
    """本轮该类目 —— 读轮换游标;没设过(或表已变)就从第一个开始。

    游标存 `system_config`,**跨重启不丢**(否则每次重启都从头开始 = 永远只搜第一类)。
    """
    from app.db.models import SystemConfig

    order = list(categories())
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == _CONFIG_KEY))
    cur = (row.value if row else "") or ""
    # 表改了(类目被删/改名)时游标可能失效 → 回落到第一个,别让整条链崩在 KeyError 上
    return cur if cur in order else order[0]


def advance_category(session) -> str:
    """推进到下一个类目并落库;返回**新的**当前类目。

    在**一轮出词之后**调用(而不是之前)—— 这样若本轮中途失败,下次仍然搜同一类,
    不会"跳着跳过某个类目"。
    """
    from app.db.models import SystemConfig

    order = list(categories())
    nxt = order[(order.index(current_category(session)) + 1) % len(order)]
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == _CONFIG_KEY))
    if row is None:
        session.add(SystemConfig(key=_CONFIG_KEY, value=nxt))
    else:
        row.value = nxt
    session.commit()
    return nxt


def pick(words: list[str], cat: str, need: int, names: set[str] | None = None) -> list[str]:
    """挑出本轮该搜的词。**优先顺序**(2026-10-04 定):

        ① 当前类目的**资源名**   ← 主料(用户口径:"搜索词应该是资源名称")
        ② **未分类的资源名**     ← 见下,不能丢
        ③ 当前类目的**话题词**   ← 没货时的兜底

    ⚠️ **②为什么要留着"未分类的资源名"**:类目表是**收窄**的(如「资料」只收**学习资料**),
    于是壁纸/字体/模板这类资源名**归不进任何类目**。若把它们一律丢掉,
    轮换到任何类目时都用不上它们 —— **等于把用户自己的资源名给扔了**,与
    "搜索词应该是资源名称"直接冲突。所以未分类的**排在类目内资源名之后、话题词之前**。

    ⚠️ **③为什么兜底用"本类目的话题词"而不是"别的类目的资源名"**(2026-10-04 实测发现):
    候选池小时,轮到「问卷」「软件」这类**没有现成资源名**的类目,若拿别的类目的词充数,
    **轮换就形同虚设** —— 每个类目出的其实是同一批词。而拿**本类目的话题词**去搜
    (如「问卷」→"性格测试"),才是**主动往这个方向扩**,也是用户要的"创新"。
    """
    if need <= 0:
        return []
    mine, unknown = [], []
    for w in words:
        (mine if classify(w, names) == cat else unknown).append(w)
    unknown = [w for w in unknown if classify(w, names) == ""]   # 别的类目的不要(让轮换有意义)
    out = (mine + unknown)[:need]
    if len(out) >= need:
        return out
    hints = [h for h in categories().get(cat, ()) if h not in out]
    return (out + hints)[:need]
