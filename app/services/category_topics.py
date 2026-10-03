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

from sqlalchemy import select

# ⚠️ **草稿种子,不是全集**(见模块说明)。顺序 = 轮换顺序。
# 想加类目/话题:优先改 `.env` 的 `LEAD_CATEGORIES`(不用动代码);
# 想把新的一类固化成默认,再加到这里。
DEFAULT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "资料": ("真题", "试题", "答案", "课件", "讲义", "笔记", "教程", "课程", "考研", "考公",
             "考编", "四六级", "雅思", "托福", "期末", "复习", "模板", "表格", "PPT", "简历",
             "素材", "字体", "笔刷", "壁纸", "头像", "表情包", "图标", "报告", "手册", "指南",
             "资料", "文档", "PDF", "pdf", "电子书", "书单", "清单", "论文", "合集"),
    "影视": ("短剧", "漫剧", "电影", "电视剧", "剧集", "全集", "解说", "番剧", "动漫", "剧"),
    # ⚠️ **别把"入口"这类通用词放进来**(2026-10-04 实测踩到):资源名里
    # "…入口链接直达" 到处都是,会把一堆非问卷的东西误判进本类。
    "问卷": ("问卷", "调研", "测评", "量表", "测试"),
    "软件": ("软件", "安装包", "绿色版", "激活", "工具", "客户端", "补丁", "模拟器", "插件",
             "脚本", "源码", "代码", "APP", "app"),
    "大瓜": ("爆料", "吃瓜", "热搜", "回应", "声明", "翻车"),
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


def classify(text: str) -> str:
    """文本属于哪个类目?认不出返回空串(**不猜** —— 猜错会把词出到错的类里去)。"""
    word = text or ""
    for cat, hints in categories().items():
        if any(h in word for h in hints):
            return cat
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


def pick(words: list[str], cat: str, need: int) -> list[str]:
    """挑出属于 `cat` 的前 `need` 个词;**该类目词不够时,用该类目的"话题词"兜底**。

    ⚠️ **为什么兜底用话题词、而不是别的类目的词**(2026-10-04 实测发现):
    候选池小的时候(群里 + 资源库就那几个资源名),轮到「问卷」「软件」这类**没有现成资源名**
    的类目时,若拿别的类目的词充数,**轮换就形同虚设** —— 每个类目出的其实是同一批词。
    那"轮换类目"就白做了。

    而**这正是类目表"下面具体话题"的用处**:这个类目下暂时没有资源名,就拿**它自己的话题词**
    去搜(如「问卷」→ 搜"问卷模板"),**主动往这个方向扩** —— 用户要的"创新"正在这里。

    ⚠️ 但**优先永远是"资源名称"**(用户口径:"搜索词应该是资源名称");话题词只是**没货时的兜底**。
    """
    if need <= 0:
        return []
    mine = [w for w in words if classify(w) == cat]
    if len(mine) >= need:
        return mine[:need]
    hints = [h for h in categories().get(cat, ()) if h not in mine]
    return (mine + hints)[:need]
