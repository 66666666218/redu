"""**评论区「在求什么」监控**(2026-10-30,用户口径)。

> 「帮我监控一些**评论区都在求的大瓜名称**…只需要**监控名称**就行,不用保存到网盘」

## 它回答的问题

需求侧:**观众在评论区反复点名的那个东西是什么**。
与 `douyin_leads`(供给侧:谁在推、带了什么盘链)**口径不同**,所以单独一张表
(`demand_names`)、单独一张卡 —— 混在一起会把「有人求」读成「有人在卖」。

## 链路(抖音已实测,2026-10-30)

1. **找素材视频**:用种子词(`吃瓜`/`大瓜`/`求资源`…)走 `douyin_protocol_source.search`
   —— 复用它已有的 cookie、签名、限流判定,**不另起一套请求**;
2. **拉评论**:`/aweme/v1/web/comment/list/`(必须带 `a_bogus`;实测**不带签名回空体**,
   带签名 200 + 20 条 —— 这是当天做的对照实验);
3. **抽名字**:书名号 + 「求/想要/谁有/在哪看…」后面的那段中文;
4. **聚合**:按名字累计 `demand_hits`(带"求"语气的评论条数)与 `mention_hits`;
5. **推送**:只推**够格**的(默认 ≥2 条求 + 至少出现在 1 个视频)且 **48 小时没推过的**,
   避免同一批名字每 4 小时刷一次屏。

## 纪律

- **只存名字、不落网盘**(用户明确):本模块**从不**调用转存/建链;
- **名字要能回溯**:`samples` 里留着抽到它的评论原文与视频链接,抽歪了一眼能看出;
- **拿不到就不写**:拉评论失败(被限/无登录)只记 `runs` 并按失败计数,**不写空行**。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func, select

from app.utils import get_logger

logger = get_logger(__name__)

PLATFORM = "douyin"

#: 拉评论的接口(实测:必须带 `a_bogus`,不带回**空体**)
COMMENT_API = "https://www.douyin.com/aweme/v1/web/comment/list/"

#: 「扣 1」类跟帖:整条评论就是一个/一串 `1`(或 `扣1`)。这是推广号的**互动口令**
#: (「扣1看完整版」),评论区的 `1` 越多 = 这条越火 —— 用户口径:
#: 「判断是根据作品里面的**求和1**来判断火爆程度」(2026-10-30)。
#: ⚠️ 只认**整条就是 1**,不做子串匹配:正常句子里的「1」("第1集")不能算。
_ONES_RE = re.compile(r"^扣?[1一]{1,8}$")
#: 表情占位符(`[捂脸]`/`[宕机]`)—— 比对前要剥掉,否则 `111[尴尬流汗]` 认不出是「1」。
_EMOJI_RE = re.compile(r"\[[^\]]{1,10}\]")


def _norm_comment(text: str) -> str:
    """评论的**归一形态**:去表情占位符、去空白与标点。

    用途有两个,都是用户样本逼出来的(2026-10-30 第二条样本):
      · `111[尴尬流汗]` 要能被认成「1」;
      · `刚看完` / `刚看完[宕机]` / `刚看完。` 要能被当成**同一条重复评论** ——
        按原文比对时它们的重复率只有 22%,归一后才是真实的"满屏一样"。
    """
    s = _EMOJI_RE.sub("", str(text or ""))
    return re.sub(r"[\s\W_]+", "", s)


#: 连续中文片段 —— 高频名抽取只在中文串里切 n-gram(数字/拉丁/标点在评论区太杂)
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

#: **高频名**的最短/最长长度。2 字(`甜甜`/`蕉妹`)到 6 字(`香香公主与爱犬`)。
_ENTITY_MIN_LEN = 2
_ENTITY_MAX_LEN = 6

#: 「边界出现」的最低占比(见 `Entity.edge`)。0.3 是实测出来的:
#: 真名字 `水子哥`(0.56)/`面具`(0.33) 在线上,句中切出来的 `子哥`(0.11)/`是水`(≈0) 在线下。
_ENTITY_EDGE_MIN = 0.3

#: **不能出现在名字首/尾的虚字**(2026-11-06 实测残留的噪音全是这种形状):
#: `潘甜甜是`/`是谁`/`不烧心`/`都是`/`是水` —— 它们恰好落在中文串边界上,
#: 边界率过关,但**起止落在虚词上**,说明它是从句子里切出来的半句,不是名字。
#: 真名字(`林予曦`/`潘甜甜`/`水子哥`/`蕉妹`)首尾都是实词。
#:
#: ⚠️ 故意**不含**「上/下/前/后/里/外/看/说」这类能起头的实词(《上海滩》《下雨天》);
#: 也不追求覆盖全部 —— `你/我/他/她/这/那` 早在 `_NOT_NAME_PARTS` 里被 `is_name_like` 挡了。
_ENTITY_EDGE_BAD = frozenset(
    "是 的 了 也 都 就 还 在 和 与 有 没 不 要 会 能 想 个 些 很 太 最 又 再 才 被 把 给 对 "
    "从 到 但 而 且 因 所 如 若 虽 却 只 更 挺 好 真 可 谁 什 怎 哪 为 以 于 之 其 此 该 每 各".split())


class Entity(NamedTuple):
    """一个候选片段:文本、文档频率(出现在多少条不同评论里)、边界率。"""
    text: str
    df: int
    edge: float


def frequent_entities(texts: list[str], *, min_docs: int = 3) -> list[Entity]:
    """★ **评论区里反复出现的实体名**(2026-11-06)。

    ## 为什么需要它
    原有的抽取只认两种形状:**书名号 `《…》`** 与 **「求 X / X 叫什么」句式**。
    实测(2026-11-06,生产评论原文)这两种**全都抓不到真实场景**:

    | 视频 | 评论区原文 | 原抽取器 |
    |---|---|---|
    | 「两位巅峰王者」 | `林予曦就两部 甜甜三部` / `蕉妹第二部` | **0 个** |
    | 「水逆塌方」 | `水子哥略显青涩` / `我有水子哥了` / `声音像水子哥`… | **0 个** |

    —— 观众**不会**写书名号,也**不会**说「求 XX」;他们直接**说这个名字**
    (`水子哥`×9 条、`甜甜`×8 条)。所以判据要换成 **"在很多条不同评论里重复出现的短中文串"**。

    ## 怎么算
    对每条评论的连续中文串切 2~6 字 n-gram,统计 **DF(出现在多少条不同评论里)** ——
    注意是文档频率不是总次数:同一句刷 100 遍只算 1,防刷屏。

    ## 另算一个「边界率」
    光看重复次数会把**从句中间切出来的碎片**也捞上来(实测 `个面具`/`是水`/`人都`/`都是`
    都进了榜)。所以同时统计每个片段的**边界出现占比**:
    该片段落在中文串**首或尾**的次数 ÷ 出现总次数。

    ⚠️ 「边界」包含**整条中文串就是它**的情形:实测 `水子哥` 有相当一部分评论
    就是这两个字本身,那是**最强**的边界证据。早先版本要求 `len(run) > len(gram)`
    才计边界,把这批全漏了(edge 从 0.56 掉到 0.25) —— 已去掉。
    短 run 会给碎片刷出 1.0 的假高值(`是男的` 切出 `是男`/`男的`),
    那类由 `_ENTITY_EDGE_BAD`(虚字起止)与 `_ENTITY_STOP` 兜住,不靠这条。

    ## 这一步**必定**混进泛词
    实测 `电影`(4 个视频)、`什么`(7 个)、`名字`(3 个) 都会上榜 ——
    它们本来就是评论区的高频词。所以本函数**只负责"重复"这一个信号**,
    "是不是名字"由调用方用 `is_name_like` + **跨视频分布** + 库内交叉验证来收口
    (见 `run_user` 里 `_ENTITY_*` 那段)。

    返回 `[Entity(text, df, edge)]`,已按 DF 降序。
    """
    from collections import Counter

    df: Counter[str] = Counter()
    edge: Counter[str] = Counter()
    #: ⚠️ **先去重再统计**(2026-11-06):文档频率的含义是"多少条**不同**评论提到它"。
    #: 同一句话被刷 100 遍(`水子哥` 复制粘贴)在这条路上只能算 1 —— 否则刷屏就能
    #: 把一个短语顶成"高频实体"。
    for t in dict.fromkeys(_norm_comment(x) for x in texts):
        seen: set[str] = set()
        #: 边界命中**也按评论去重**:一条评论里同一片段在开头和结尾各出现一次,
        #: 只能说明"这条评论说了两次",不能把边界率推到 >1。
        edged: set[str] = set()
        for run in _CJK_RUN_RE.findall(_norm_comment(t)):
            for n in range(_ENTITY_MIN_LEN, min(_ENTITY_MAX_LEN, len(run)) + 1):
                for i in range(len(run) - n + 1):
                    g = run[i:i + n]
                    seen.add(g)
                    if i == 0 or i + n == len(run):
                        edged.add(g)
        df.update(seen)
        edge.update(edged)
    out = [Entity(g, c, round(edge[g] / c, 3))
           for g, c in df.items() if c >= int(min_docs)]
    return sorted(out, key=lambda e: (-e.df, -len(e.text)))


def _drop_subsumed(cands: list[Entity]) -> list[Entity]:
    """去掉**被更长的近似同频片段包含**的候选。

    实测(2026-11-06,真实评论):
      · `林予`(6) 与 `林予曦`(5)、`豆包什`(3) 与 `豆包什么电影`(3) 会同时上榜 ——
        短的那个是被句子**切断的半截**,留着就是噪音;
      · 而 `甜甜`(7) 与 `潘甜甜`(3) **都要留**:前者是观众对这个名字的简称,DF 更高。

    判据用 **`kc >= c * 0.7`**(长的那个出现得差不多多)而不是 `kc >= c`:
    后者太松(半截 vs 全名常常差 1 条评论),前者仍能把 `甜甜`/`潘甜甜` 这一对区分开。

    ⚠️ 只认**前缀/后缀**包含,不认"出现在中间"(2026-11-06 实测):
    被切成半截的碎片**总是**全名的前缀或后缀(`林予`/`予曦`→`林予曦`,`来早`→`来早了`);
    而中间包含常常是另一个词(`潘甜甜` 里含 `甜甜`,那是简称不是碎片)。
    用中间包含会误伤,而且 `_entity_names` 的边界率已经能挡住它在句中的形态。
    """
    out: list[Entity] = []
    for e in cands:
        if any(e.text != k.text and k.df >= e.df * 0.7
               and (k.text.startswith(e.text) or k.text.endswith(e.text))
               for k in cands):
            continue
        out.append(e)
    return out


#: **评论区高频但不可能是资源名**的词(2026-11-06 从真实评论里逐条看过来的)。
#:
#: ⚠️ 这是"高频实体"这条路**必然**带来的代价:评论区的填充词(`电影`/`什么`/`名字`)DF 天然很高。
#: 词表以**实测噪音**为准,不靠想象补 —— 加得太宽会把真名字挡掉(如 `面具` 在某条视频里
#: 确实是观众在说的东西,但它不是资源名,所以仍在表内)。
_ENTITY_STOP = frozenset({
    "电影", "电影名", "电影名字", "片名", "剧名", "名字", "什么", "怎么", "多少", "知道",
    "一部", "两部", "第一", "第二", "第三", "第四", "一个", "两个", "三个", "这部",
    "这部片", "那个", "这个", "片子", "视频", "评论", "抖音", "快手", "表情", "声音",
    "男的", "女的", "男生", "女生", "厕所", "面具", "前面", "后面", "上面", "下面",
    "真的", "可以", "没有", "不是", "我们", "你们", "他们", "自己", "时候", "东西",
    "感觉", "觉得", "好看", "一般", "正常", "衣服", "裙子", "黑色", "白色", "今天",
    "昨天", "明天", "好可爱", "来早了", "豆包", "还有", "这是", "就是", "然后", "所以",
    #: 2026-11-06 第二轮实测补的**虚词/量词碎片**(逐个看过原文才加):
    #: `辨识度`(4)/`但是`(4)/`确实`(3) 来自「水子哥」那条;
    #: `已经`(3) 来自「古见同学」;`有个`(4)/`同学`(3)/`部作品`(3) 来自「兽人」与「两位巅峰王者」。
    #: 它们在**任何**话题下都会高频出现,不带任何资源语义。
    "辨识度", "但是", "确实", "已经", "有个", "同学", "部作品", "听说", "为什",
})


def _entity_names(cands: list[Entity], *, title: str = "") -> list[Entity]:
    """把 `frequent_entities` 的原始候选收成**能当名字的**:先并碎片,再过泛词/噪音词。

    ⚠️ **顺序很关键**(2026-11-06 实测):必须先 `_drop_subsumed` 再 `is_name_like`。
    反过来会留下**碎片**:`什么电影` 含 `什么`(在 `_NOT_NAME_PARTS` 里)⇒ 被 `is_name_like`
    挡掉,而它的碎片 `么电影`(DF 一样高)**不**含 `什么` ⇒ 反而活下来。
    实测那轮结果里排第一的就是 `么电影`(8 条)。

    ⚠️ **边界率**是必要不充分条件(见 `_ENTITY_EDGE_MIN`):
    实测 `个面具`/`是水`/`人都`/`都是`/`下海` 这类从句中切出的碎片边界率≈0,被它挡掉;
    而泛词(`电影`/`什么电影`)边界率高达 1.0 —— 所以泛词仍必须靠 `_ENTITY_STOP`
    与 `run_user` 的**跨视频**判据来收口,这条闸门替代不了它们。

    ⚠️ `title`:出现在**视频标题**里的片段一律跳过。实测 `古见`(标题「猫系古见同学」)、
    `兽人`(标题里就有)都上了榜 —— 那不是"评论区在议论谁",而是**视频本身在讲什么**;
    而且标题里的书名号名字走的是 `names_from_title` 那条路,不需要在这里重复发现。
    """
    kept = _drop_subsumed(list(cands))
    title = _norm_comment(title)
    out: list[Entity] = []
    for e in kept:
        g = e.text
        if title and g in title:
            continue
        if e.edge < _ENTITY_EDGE_MIN:
            continue
        if g[0] in _ENTITY_EDGE_BAD or g[-1] in _ENTITY_EDGE_BAD:
            continue
        if g in _ENTITY_STOP or len(set(g)) <= 1 or not is_name_like(g, quoted=True):
            continue
        out.append(e)
    return out



def names_from_title(title: str) -> list[str]:
    """**作品名里的资源名** —— 用户口径:「基本上作品名都有资源名」。

    ⚠️ **只认书名号 `《…》`,不认标签 `#…`**(2026-10-30 实跑后改回来的):
    试过把标签也当名字,结果抽出来的是 `青筋`/`小桃`/`锐评`/`交流`/`vlog日常`/
    `vlog十亿流量扶持计划` —— 标签是**描述词**,不是资源名,放进榜里比没名字更糟。
    推广号的标题用的是书名号(`《反差直男》`),那才是名字。
    """
    out: list[str] = []
    for m in _QUOTED_RE.finditer(str(title or "")):
        n = _clean(m.group(1))
        #: 标题里的书名号经常写全名(如 `《sunny水水水/水子哥下海vlog》`),所以放宽到 24 字
        if is_name_like(n, quoted=True) and n not in out:
            out.append(n)
    return out


#: 「这是在要东西」的语气词。命中任意一个,这条评论的提及就算一次 **demand**。
_DEMAND_WORDS = ("求", "想要", "想看", "谁有", "有没有", "在哪看", "哪里看", "叫什么",
                 "求出处", "出处", "有吗", "求资源", "求个", "求一个", "求名字")

#: 「求 X」/「谁有 X」后面的那段就是候选名字(最多 20 个字符,遇到标点就断)。
_AFTER_RE = re.compile(
    r"(?:求个?一?个?|想要|想看|谁有|有没有)\s*[《「\"']?"
    r"([一-鿿A-Za-z0-9][一-鿿A-Za-z0-9· ]{1,19})")
#: `《…》` —— 推广号/观众都爱用书名号包名字,这是**最强**的信号
_QUOTED_RE = re.compile(r"《([^》]{2,24})》")
#: 「X 在哪看」/「X 叫什么」—— 名字在前
_BEFORE_RE = re.compile(
    r"([一-鿿A-Za-z0-9· ]{2,20}?)\s*(?:在哪[看里]|哪里看|叫什么|有吗|谁有|全集在哪)")

#: 名字的**尾巴**:跟着一起被正则吃进来的泛词,剥掉它们
_TAIL_WORDS = ("的资源", "资源", "的链接", "链接", "网盘", "全集", "完整版", "完整", "版",
               "在线观看", "在线", "免费", "高清", "下载", "谢谢", "大佬", "博主", "作者",
               "名字", "叫啥", "是什么", "的视频", "视频", "在哪", "哪里", "有吗", "啊",
               "呀", "呢", "吧", "的")

#: **不是名字**的泛词(整词命中就丢)。少而准 —— 宁可漏,也别把「资源」当名字推给客户。
_GENERIC = frozenset({
    "资源", "链接", "网盘", "全集", "完整版", "视频", "名字", "出处", "分享", "下载",
    "免费", "在线", "高清", "最新", "这个", "那个", "什么", "怎么", "哪里", "在哪",
    "大佬", "博主", "作者", "老师", "兄弟们", "姐妹们", "好人", "一生平安", "求求了",
    "吃瓜", "大瓜", "瓜", "合集", "系列", "更新", "有没有", "谁有", "想要", "想看",
})


def _clean(raw: str, *, strip_tails: bool = True) -> str:
    """把抽出来的那段**剥成名字**:去掉首尾空白/标点,以及(可选)尾巴泛词。

    ⚠️ `strip_tails=False` 给**标签**用:标签本身就是一个整体,
    剥尾巴会把 `新名字` 剥成 `新`、`小小绘` 不受影响 —— 实测因此丢掉过候选名。
    """
    s = str(raw or "").strip().strip("《》「」\"'“”‘’,,。.!！?？:：;；、")
    if not strip_tails:
        return s.strip()
    changed = True
    while changed and s:            # 尾巴可能叠着两三个(`的资源完整版`),剥到不动为止
        changed = False
        for w in _TAIL_WORDS:
            if s.endswith(w) and len(s) > len(w):
                s = s[: -len(w)].rstrip()
                changed = True
    return s.strip()


#: **不可能出现在资源名里**的片段。命中就不当名字。
#: ⚠️ 这些是从**真实的误抽**里长出来的,不是拍脑袋:2026-10-30 实测
#: `求个真实有效的资源` → 抽出「真实有效」、`有没有大佬分享一下` → 抽出「大佬分享一下」。
_NOT_NAME_PARTS = ("一下", "分享", "有效", "真实", "可以", "帮忙", "什么", "怎么",
                   "谢谢", "大佬", "有没有", "求求", "一个", "这个", "那个",
                   #: ★ 2026-10-30 第二次实测补的:**代词**。
                   #: `现在父母起名字…别人问你孙子你爷爷叫什么` 被抽成了名字
                   #: 「别人问你孙子你爷爷」—— 「X叫什么」这个句式在**聊起名**的评论里
                   #: 到处都是,而资源名里几乎不会出现「你/我/他/她」。
                   "你", "我", "他", "她", "咱", "们", "啥",
                   #: ★ 第三次:`这女的叫什么` 会被抽成「这女」——
                   #: 指示代词开头的片段不是名字(真名字极少以「这/那」开头)。
                   "这", "那", "知道",
                   #: ★ 第四次实跑:`…还有一部叫什么` 抽出「还有一部叫」——
                   #: 承接词开头的片段不是名字。
                   "还有", "一部", "另外", "这是", "就是")


#: ★ **动词短语的开头/结尾**——它们是从评论句子里被"求/叫什么"句式**截出来的半句话**
#: (2026-11-02 数据:16 个名字里 11 个是这种噪音,如「谈过恋爱」「直接买」「可换」
#: 「偷偷跑去女厕所过」「愿意花米」「主包用的柠胶」)。真正的资源名很少以动词开头/结尾。
_BAD_START = ("直接", "愿意", "偷偷", "主包", "可", "谈过", "跑去", "咱们", "所以", "然后")
_BAD_END = ("过", "买", "换", "时", "掉", "来", "去")


def is_name_like(name: str, *, quoted: bool = False) -> bool:
    """像不像一个**资源名**:2~24 字、不是泛词、不以语气词结尾、含汉字或拉丁字母。

    ⚠️ 结尾过滤不是吹毛求疵:`求个XX的资源` 剥掉尾巴后常剩下一个**半句话**
    (`真实有效的`/`有没有的`),把它当名字推给客户就是噪音 —— 宁可漏。
    """
    n = str(name or "").strip()
    #: 书名号里的放宽到 30 字(小红书笔记标题常是一整句);靠句式猜出来的限 12 字 ——
    #: 实测(`叫什么`/`求…`)长句子会整段被吃进来,而真名字通常不长。
    if not (2 <= len(n) <= (30 if quoted else 12)):
        return False
    if n in _GENERIC:
        return False
    if n[-1] in "的了呢吧啊呀吗嘛哦":
        return False
    if n.startswith(_BAD_START) or n.endswith(_BAD_END):
        return False
    if any(p in n for p in _NOT_NAME_PARTS):
        return False
    return bool(re.search(r"[一-鿿A-Za-z]", n))


def extract_names(text: str) -> list[tuple[str, bool]]:
    """从一条评论里抽 `[(名字, 是不是在求)]`。

    ★ **两种信号分开记**:书名号 ⇒ 一定在提这个名字(但未必在求);
    带了「求/谁有/在哪看…」⇒ 这条提及算一次 **demand**。
    只看书名号会把「这个《X》真好看」也算成需求,把榜单灌成"热门"而不是"被求"。
    """
    t = str(text or "")
    demand = any(w in t for w in _DEMAND_WORDS)
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for m in _QUOTED_RE.finditer(t):
        n = _clean(m.group(1))
        if is_name_like(n, quoted=True) and n not in seen:
            seen.add(n)
            out.append((n, demand))
    if demand:
        for rx in (_AFTER_RE, _BEFORE_RE):
            for m in rx.finditer(t):
                n = _clean(m.group(1))
                if is_name_like(n) and n not in seen:
                    seen.add(n)
                    out.append((n, True))
    return out


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


def fetch_comments(aweme_id: str, cursor: int = 0, count: int = 20,
                   session=None, user_id: int = 1) -> dict:
    """拉一页评论。返回 `{ok, comments:[{text, like, user, cid}], has_more, error}`。

    ⚠️ **必须带 `a_bogus`**:2026-10-30 对照实验 —— 不带签名两次都回**空体**,
    带签名 200 + 20 条。签名用仓里 f2 移植的那份(`douyin_sign`),UA 与它保持一致。
    """
    import requests

    from app.services import douyin_protocol_source as dp
    from app.services.douyin_sign import ABogus, BrowserFingerprintGenerator

    blob = dp._cookie(session, user_id)
    if not blob:
        return {"ok": False, "comments": [], "has_more": False,
                "error": "没有抖音登录态"}
    ck = dp._cookie_dict(blob)
    params = {"device_platform": "webapp", "aweme_id": str(aweme_id),
              "cursor": str(int(cursor)), "count": str(int(count)),
              "item_type": "0", "aid": "6383"}
    query = "&".join(f"{k}={v}" for k, v in params.items())
    fp = BrowserFingerprintGenerator.generate_fingerprint("Chrome")
    signed = ABogus(fp=fp, user_agent=dp._UA).generate_abogus(params=query)[0]
    headers = dp.build_headers(blob, ck, "")
    headers["Referer"] = "https://www.douyin.com/"
    try:
        r = requests.get(f"{COMMENT_API}?{signed}", headers=headers, timeout=20)
    except Exception as exc:  # noqa: BLE001 - 网络错误按"这一页没拿到"处理
        return {"ok": False, "comments": [], "has_more": False,
                "error": f"{type(exc).__name__}: {str(exc)[:80]}"}
    if r.status_code != 200:
        return {"ok": False, "comments": [], "has_more": False,
                "error": f"HTTP {r.status_code}"}
    try:
        d = r.json()
    except ValueError:
        return {"ok": False, "comments": [], "has_more": False,
                "error": "响应不是 JSON(被挡?)"}
    if d.get("status_code") not in (0, None):
        return {"ok": False, "comments": [], "has_more": False,
                "error": f"status_code={d.get('status_code')}"}
    rows = []
    for c in (d.get("comments") or []):
        if not isinstance(c, dict):
            continue
        txt = str(c.get("text") or "").strip()
        if not txt:
            continue
        user = c.get("user") if isinstance(c.get("user"), dict) else {}
        #: ★ 接口顺带给了**回复预览**(`reply_comment`)—— 这很重要:
        #: 「这女的叫什么」这类提问,**答案往往就在回复里**,而不在正文。
        replies = [str(r.get("text") or "").strip()
                   for r in (c.get("reply_comment") or []) if isinstance(r, dict)]
        rows.append({"text": txt,
                     "like": int(c.get("digg_count") or 0),
                     "user": str(user.get("nickname") or ""),
                     "cid": str(c.get("cid") or ""),
                     "replies": [x for x in replies if x][:3]})
    return {"ok": True, "comments": rows, "has_more": bool(d.get("has_more")),
            "error": ""}


def names_from_replies(replies) -> list[str]:
    """从**回复预览**里找名字 —— 「这女的叫什么」的答案通常在这里。

    规则:回复里有书名号就取书名号里的;没有就把**整条短回复**当一个名字
    (评论区的回答往往就是光秃秃一个名字: `Alli Skye` / `小小绘`)。
    ⚠️ 只对**问了但没说出名字**的评论调用,避免把普通回复当成名字。
    """
    out: list[str] = []
    for r in (replies or []):
        quoted = [n for n in (_clean(m.group(1)) for m in _QUOTED_RE.finditer(str(r)))
                  if is_name_like(n, quoted=True)]
        cands = quoted
        if not cands:
            t = _clean(str(r))
            if is_name_like(t) and len(t) <= 12:
                cands = [t]
        for n in cands:
            if n not in out:
                out.append(n)
    return out


def best_tag(title: str, exclude: str = "") -> str:
    """**求1型视频**的标题里挑一个最像名字的标签(只在已判定为该类型时用)。

    这一类的标题常常没有书名号,只有一串标签(用户给的参考作品就是这种)。
    挑法:**最长**的、不在通用词表里的拉丁/中文标签 —— 通用词(`#反差`/`#覆面`/`#fyp`)
    一律丢掉;挑不出来就返回空串(宁可没有名字,也别把描述词当名字)。

    ⚠️ `exclude` = **作者名**:实测用户样本的标签里最长的是 `#阿玛特拉斯`,那正是**作者自己**
    (2026-10-30),把它当"大瓜名字"推出去毫无意义。
    """
    cands = [n for n in _tags_of(title)
             if not (exclude and n == str(exclude).strip())]
    if cands:
        return max(cands, key=len)
    #: 兜底:**标签前的那句正文**往往就是作品名(`智慧蛊回来了# 范小勤 …`)。
    #: ⚠️ 上限 **30** 字(不是 16):小红书笔记标题常是一整句
    #: (`大耳朵图图 & 小美睡前情头|晚安太治愈啦`),16 字门槛会把它整条丢掉(2026-10-30 实测)。
    head = str(title or "").split("#")[0].strip()
    head = _clean(head)
    return head if 2 <= len(head) <= 30 and is_name_like(head, quoted=True) else ""


def match_seed(title: str, seeds: list[str]) -> str:
    """标题里如果出现**参考作品名**,就用那个名字(容忍语序不同)。

    第三条样本实测(2026-10-30):《我的天呐!!! 小桃和狗爹简直是仙品啊啊啊》——
    标题**没有书名号、没有标签**,名字是句子里的「小桃和狗爹」。
    按二元组覆盖率匹配(`狗爹和小桃` 的 4 个二元组命中 3 个)就能认出来。
    """
    t = str(title or "")
    if not t or not seeds:
        return ""
    best, best_score = "", 0.0
    for seed in seeds:
        s = str(seed or "").strip()
        if len(s) < 2:
            continue
        grams = {s[i:i + 2] for i in range(len(s) - 1)}
        if not grams:
            continue
        hit = sum(1 for g in grams if g in t)
        score = hit / len(grams)
        if score > best_score:
            best, best_score = s, score
    #: 阈值 0.5:实测「小桃和狗爹」这种**语序反过来**的写法只命中 2/4 个二元组,
    #: 0.6 就漏了(第三条样本);而完全不相关的标题覆盖率是 0,不会误伤。
    return best if best_score >= 0.5 else ""


#: 标签里的**通用词**,不是名字(平台标签、形态词)
_TAG_JUNK = frozenset({
    "fyp", "ootd", "vlog", "日常", "反差", "覆面", "覆面系", "覆面肌肉男", "手控",
    "手控党", "甜妹", "穿搭", "女大学生", "推荐", "热门", "上热门", "涨粉", "流量",
    "情感", "故事", "短剧", "古风", "汉服", "直播", "直播日常分享", "直播回放",
    "合作", "互关", "点赞", "高壮男", "宽肩窄腰", "薄肌", "青筋", "青筋暴起",
    "卡点舞", "肌肉男", "胸肌", "手臂", "摆胯舞", "男妈妈", "纯欲感", "暴力美学",
    "动漫", "二次元", "搞笑", "颜值", "身材", "腹肌", "健身", "跳舞", "舞蹈",
    # ★ 2026-10-30 第一次跑 `--expand` 实际冒出来的**风格词**(不是名字,
    #   但同一个作者会在几十条作品里反复打,靠"出现次数"筛不掉):
    "禁欲系", "锐评", "交流", "手势舞", "慢摇", "卡点", "变装", "氛围感", "御姐",
    "纯欲", "反差感", "甜妹", "高颜值", "美女", "帅哥", "颜值担当", "表情包",
    "火影忍者", "火影忍者手游", "游戏", "手游", "王者荣耀", "和平精英",
})


def collect_for_video(aweme_id: str, *, session=None, user_id: int = 1,
                      pages: int = 2, count: int = 20, min_docs: int = 3,
                      title: str = "") -> dict:
    """把一个视频的评论扫一遍。返回 `{name: {"demand":n,"mention":n,"samples":[…]}}`。"""
    found: dict[str, dict] = defaultdict(lambda: {"demand": 0, "mention": 0, "samples": []})
    cursor, err, no_name, ones, total = 0, "", 0, 0, 0
    texts: list[str] = []
    for page in range(max(1, int(pages))):
        res = fetch_comments(aweme_id, cursor=cursor, count=count,
                             session=session, user_id=user_id)
        if not res["ok"]:
            err = res["error"] or "拉评论失败"
            break
        for c in res["comments"]:
            total += 1
            norm = _norm_comment(c["text"])
            texts.append(norm or c["text"].strip())
            if _ONES_RE.match(norm):             # 「扣1」跟帖 = 热度信号,不是名字
                ones += 1
                continue
            hits = extract_names(c["text"])
            #: 问了但没说名字(`这女的叫什么` / `求个链接`) ⇒ 去**回复**里找答案
            if not hits and any(w in c["text"] for w in _DEMAND_WORDS):
                hits = [(n, True) for n in names_from_replies(c.get("replies"))]
            if not hits:
                #: ★ **有"求"但没抽到名字**也要计数:它是"这批素材到底有没有需求"的唯一证据。
                #: 实测(2026-10-30)短剧类线索 80 条评论里名字型需求 0 —— 这条计数
                #: 能让人一眼区分"链路坏了"与"素材不对/评论没人求"。
                if any(w in c["text"] for w in _DEMAND_WORDS):
                    no_name += 1
                continue
            for name, is_demand in hits:
                rec = found[name]
                rec["mention"] += 1
                if is_demand:
                    rec["demand"] += 1
                if len(rec["samples"]) < 3:
                    rec["samples"].append(c["text"][:60])
        if not res["has_more"]:
            break
        cursor += count
    #: ★ **重复评论占比**(2026-10-30 用户给的样本算出来的第二个信号):
    #: 用户给的那条「范小勤」图文,59 条评论里 `刚看完`×8、`冰露`×5、`1`×6 ——
    #: 满屏重复短句是**引流/刷**的形状,比"求"更常见。
    dup_ratio = 0.0
    if total >= 5:
        dup_ratio = 1 - (len(set(texts)) / total)
    #: ★ **评论区高频实体**(2026-11-06):观众直接说名字(`水子哥`×9 / `甜甜`×8),
    #: 既不写书名号也不写「求 XX」,原抽取器一个都抓不到。详见 `frequent_entities`。
    #: ⚠️ 这些**只算提及、不算"求"** —— 重复 ≠ 有人在要;它是不是名字由调用方收口。
    entities = _entity_names(frequent_entities(texts, min_docs=int(min_docs or 3)),
                             title=title)
    for e in entities:
        rec = found[e.text]
        rec["mention"] += e.df
        #: ★ 打标:`entity=True` 告诉调用方"这个名字是**靠重复**来的,不是靠句式" ——
        #: 两者在**跨视频**上的含义正好相反(见 `run_user` 的收口判据)。
        rec["entity"] = True
        if len(rec["samples"]) < 3:
            rec["samples"].append(f"(评论区出现 {e.df} 条)")
    return {"names": dict(found), "error": err, "no_name": no_name, "ones": ones,
            "total": total, "dup_ratio": round(dup_ratio, 3),
            "entities": [(e.text, e.df) for e in entities], "texts": texts}


# ---------------------------------------------------------------------------
# 聚合 / 入库 / 推送
# ---------------------------------------------------------------------------


def unfed_demand_names(db, user_id: int = 1, top: int = 2, days: int = 7,
                       settings=None,
                       platforms: tuple[str, ...] | None = None) -> list[str]:
    """★ **「评论区在求的名字」→ 抖音搜索词**(2026-11-13)。返回还没喂过搜索的那批。

    ## 为什么需要这一层

    这条链一直是**单向死路**:`demand_watch` 把名字推给客户,然后就结束了 ——
    那些名字**从没进过 `douyin_leads` 的搜索词表**。实测近 7 天推过的 10 个名字
    (`水子哥`/`林予曦`/`潘甜甜`/`蕉妹`/`香蕉`/`三水`/`男生宿舍`/`片名`/`豆包 女主`/
    `听图图的话`)**0 个**搜过,转化成真资源 1/10。

    而库里真正搬成功的名字(`恶女得宠`/`嫁金钗`/`走马上任`)**全部来自搜索词** ——
    推广号的标题写《口令》,只有搜索能命中。所以"有人在求"的名字必须回灌搜索。

    ## 判据

    - **够格推送的那些**才进词表(与 `pick_pushable` **同一组门槛**:被求 / 扣1 /
      被反复提及,满足任意一条)—— 不是所有抽出来的碎片。否则 `第二部`/`一样`/`片名`
      这种半句话会占掉名额,搜了只是白烧一次额度;
    - `fed_search_at IS NULL` —— **同一个名字只搜一次**(与探针词同一纪律)。
      喂过没产出的不再喂,否则每个名字每轮都占名额;
    - `last_seen_at` 近 `days` 天 —— 凉掉的事件搜不到东西;
    - `is_name_like(quoted=True)` —— 再过一遍名字判据(表里可能有历史脏数据);
    - 按 (被求次数, 提及次数) 降序。

    ⚠️ **只读,不写**。打戳由调用方在**搜索完成后**做(见 `mark_demand_names_fed`)。

    ★ `platforms` 默认只取 `douyin`(评论区在求的)。大瓜轮传
    `("douyin", WEVENT_PLATFORM)` 把**公众号事件名**一起取出来 —— 那批的
    `demand_hits` 存的是**作者数**,量级比评论数小得多,所以那些"够不够格"的
    门槛(`demand_min_hits` 等)**只对 douyin 那批生效**,公众号那批按
    "≥2 作者 + 有吃瓜标记"在写入时就已筛过(`wechat_event_names`)。
    """
    from app.db.models import DemandName

    if db is None or top <= 0:
        return []
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    min_demand = int(getattr(settings, "demand_min_hits", 2) or 0)
    min_ones = int(getattr(settings, "demand_min_ones", 20) or 0)
    min_mention = int(getattr(settings, "demand_min_mention", 6) or 0)
    since = datetime.now() - timedelta(days=max(1, int(days or 7)))
    plats = tuple(platforms or (PLATFORM,))
    rows = db.scalars(select(DemandName).where(
        DemandName.user_id == user_id,
        DemandName.platform.in_(plats),
        DemandName.fed_search_at.is_(None),
        DemandName.last_seen_at >= since).order_by(
        DemandName.demand_hits.desc(), DemandName.mention_hits.desc()).limit(top * 5)).all()
    out: list[str] = []
    for r in rows:
        n = str(r.name or "").strip()
        if not n or n in out or not is_name_like(n, quoted=True):
            continue
        #: ★ 「够格」的三条门槛**只对本平台(douyin)那批生效**:它们比的是评论数,
        #: 而公众号事件名的 `demand_hits` 是**作者数**(2~3 的量级),
        #: 拿 `demand_min_mention=6` 去卡会把 48h 窗口里唯一过闸的瓜整个挡掉。
        #: 那一批在写入时已按「≥2 作者 + 有吃瓜标记」筛过(`wechat_event_names`)。
        if (r.platform == PLATFORM
                and int(r.demand_hits or 0) < min_demand
                and int(getattr(r, "ones_hits", 0) or 0) < min_ones
                and int(r.mention_hits or 0) < min_mention):
            continue
        out.append(n)
        if len(out) >= top:
            break
    return out


def mark_demand_names_fed(db, user_id: int, names: list[str],
                          platforms: tuple[str, ...] | None = None) -> int:
    """给这批名字打上「已喂过搜索」的戳。返回打戳条数。

    ⚠️ **由调用方在搜索完成后打**:搜索挂了名字没消耗,下轮还能再喂。

    `platforms` 默认只打本平台(`douyin`)。大瓜轮会把**公众号事件名**
    (`platform="wechat"`,见 `wechat_event_names`)一并喂搜索,那批也要一起打戳
    —— 否则同一个事件每轮都会被重新抽出来、重新搜一遍。
    """
    from app.db.models import DemandName

    if db is None or not names:
        return 0
    now = datetime.now()
    ids = [str(w).strip() for w in names if str(w).strip()]
    rows = db.scalars(select(DemandName).where(
        DemandName.user_id == user_id,
        DemandName.platform.in_(tuple(platforms or (PLATFORM,))),
        DemandName.name.in_(ids))).all()
    for r in rows:
        r.fed_search_at = now
    db.flush()
    return len(rows)


#: 公众号事件名在 `demand_names` 里的 `platform` 取值 —— 与「评论区在求的名字」
#: (`douyin`)分开放:**一个是"有人在写",一个是"有人在求"**,口径不同,不能混。
WEVENT_PLATFORM = "wechat"

#: `X与Y` / `X和Y` 的**实体对** —— 八卦标题写两个人就是它(`大美与高迪`/`黄仁勋和小三`)。
#: ⚠️ 2~4 字是**两端都限**:太短会把「与」前后的半个词粘进来,太长会把整句吃掉。
_WEVENT_PAIR_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{2,4})(与|和)([\u4e00-\u9fffA-Za-z0-9]{2,4})")

#: **载体词**:它出现的位置就是"事件名到此为止"。
#: 实测标题形态:「大美与高迪**视频**」「大美与高迪**出轨**对象摄影师**聊天记录**曝光」。
_WEVENT_CARRIER = ("视频", "完整版", "聊天记录", "聊天", "记录", "原稿", "流出", "曝光",
                   "出轨", "事件", "后续", "内容", "全集", "大瓜", "资源", "照片",
                   "图片", "截图", "真相", "回应", "发声")

#: **吃瓜语义标记** —— 至少一篇标题里要有,否则不算大瓜。
#: ⚠️ 这一道是**必需**的,不是保险:实测「论答题卡和答题纸」有 2 作者 2 篇,
#: 完美通过跨作者闸,而它只是公考资料。**跨作者只能证明"多人在写同一件事",
#: 证明不了"这件事是瓜"** —— 少了这道,每轮都会拿资料模板名去烧搜索额度。
_WEVENT_GOSSIP = ("大瓜", "吃瓜", "曝光", "出轨", "聊天记录", "流出", "不雅",
                  "劈腿", "小三", "塌房", "内幕", "揭秘", "实锤", "爆料",
                  "回应", "翻车", "官宣", "恋情", "绯闻", "骂战", "开撕", "互撕")


#: X 前面常会粘上的**连接词/标签词**。⚠️ 与 `_WEVENT_GOSSIP` 分开列:
#: 这些**不表示"是瓜"**,只表示"X 被多吃了一个字"(`…练习题含答案与解析` → X=`题含答案`)。
_WEVENT_GLUE = ("以及", "包括", "含", "附", "及")


def _strip_label_prefix(x: str) -> str:
    """剥掉 X 前面**粘上来的标签词**:`深夜大瓜大美` 里的 X 会被正则吃成 `大瓜大美`,
    真名其实只有 `大美`。

    ⚠️ **只剥已知的标签/载体/连接词前缀,而且剥完必须还剩 ≥2 字** —— 不能用
    "取最短 X" 这种规则:`黄仁勋和小三…` 的最短切分会把真名截成 `仁勋`(实测真标题)。
    """
    changed = True
    while changed and len(x) > 2:
        changed = False
        for w in _WEVENT_GOSSIP + _WEVENT_CARRIER + _WEVENT_GLUE:
            if len(x) - len(w) >= 2 and x.startswith(w):
                x = x[len(w):]
                changed = True
                break
    return x


def _wevent_name(title: str) -> str:
    """从一条**公众号标题**里抽出 `X与Y` / `X和Y` 形态的**事件名**;抽不到给空串。

    ⚠️ 截断后必须**仍是一个纯实体对**(`fullmatch`):`相处模式与性格契合点是什么`
    前半截像实体对、后半截是整句 —— 不 fullmatch 就会把半句话当成事件名。

    ⚠️ X 要先剥标签前缀:实测 `深夜大瓜!大美与高迪视频【完整版】` 会抽出
    `大瓜大美与高迪`(不是 `大美与高迪`)—— 因为正则贪心往前吃了 4 个字。
    """
    t = _norm_comment(str(title or ""))
    for m in _WEVENT_PAIR_RE.finditer(t):
        x = _strip_label_prefix(m.group(1))
        x_end = m.start(1) + len(m.group(1))
        tail = len(t)
        for w in _WEVENT_CARRIER:
            i = t.find(w, x_end)
            if i != -1:
                tail = min(tail, i)
        cand = x + m.group(2) + t[m.end(2):tail]
        if _WEVENT_PAIR_RE.fullmatch(cand):
            return cand
    return ""


def wechat_event_names(db, user_id: int = 1, top: int = 3, hours: int = 48,
                       settings=None) -> int:
    """★★ **从公众号标题抽「大瓜事件名」**(`X与Y` 形态)并落进 `demand_names`。

    ## 为什么需要这一层(2026-11-17,用户问「高迪和大美最近就是大瓜是否监控到了」)

    实测答案:那次大瓜**只有公众号一路看得见**(8 篇文章 / 2 个作者),
    而抖音线索、对标号、热榜表、`demand_names`、`hot_word_seen` **全部 0 条** ——
    这两个名字**从没进过任何搜索词表**,所以抖音那边连"去搜一下"都没发生过。

    根因在标题形态:`names_from_title` 只认书名号 `《…》`(推广号的写法),
    而资讯号的标题是「深夜大瓜!大美与高迪视频【完整版】」—— **没有书名号**。
    拿 `names_from_title` 跑近 7 天 1733 条公众号标题,**全部抽出空**。

    ## 判据(全部用 7 天真实标题验过,不是拍脑袋)

    1. `X与Y` / `X和Y` 的实体对(`_WEVENT_PAIR_RE`);
    2. 从 X 之后**第一处载体词**截断(`_WEVENT_CARRIER`)⇒「大美与高迪出轨对象…」
       收敛回「大美与高迪」;
    3. 截断后必须**仍是纯实体对**;
    4. **≥2 个不同作者**写过 —— 单一来源的事件名是"某一篇的标题",不是"在传的瓜";
    5. 标题里至少一篇带**吃瓜语义标记**(`_WEVENT_GOSSIP`)。

    ⇒ 24h 窗口过闸 **0** 个、48h 窗口过闸 **1** 个且正是「大美与高迪」。
    窗口取 48h 而不是 24h 是**实测出来的**:这个事件在 11-14 20:02 首次落地、
    11-16 04:02 才有第二个作者跟写,24h 窗口会正好错过跨作者证据。

    ## 写库纪律

    - `platform=WEVENT_PLATFORM`(`wechat`),与「评论区在求的名字」分开 ——
      `known_name_set` 会把它们当**跨平台交叉验证**用,这正是想要的;
    - **覆盖式更新**(不是累加):这里记的是"这个窗口内几个作者在写",
      累加会让老事件的分数无限膨胀,把新事件挤掉;
    - `demand_hits` 存**作者数**、`mention_hits` 存**篇数**(同名字段的既有语义:
      "多少人在点名");
    - `samples` 留标题样例,抽歪了一眼能看出。

    ⚠️ **只写不搜**。喂搜索由调用方用 `unfed_demand_names(platforms=(...))` 取、
       **搜索完成后**用 `mark_demand_names_fed(platforms=(...))` 打戳。
    """
    from app.db.models import DemandName, WechatArticle

    if db is None or top <= 0:
        return 0
    since = datetime.now() - timedelta(hours=max(1, int(hours)))
    try:
        rows = db.execute(select(WechatArticle.title, WechatArticle.author).where(
            WechatArticle.user_id == user_id,
            WechatArticle.created_at >= since,
            WechatArticle.title != "").limit(4000)).all()
    except Exception:  # noqa: BLE001 - 取不到就返回 0,不影响大瓜轮其余部分
        logger.debug("公众号事件名取标题失败", exc_info=True)
        return 0
    by: dict[str, dict] = {}
    for title, author in rows:
        name = _wevent_name(title)
        if not name:
            continue
        g = by.setdefault(name, {"authors": set(), "titles": 0, "gossip": 0,
                                 "sample": []})
        if author:
            g["authors"].add(str(author))
        g["titles"] += 1
        if any(w in str(title or "") for w in _WEVENT_GOSSIP):
            g["gossip"] += 1
        if len(g["sample"]) < 3:
            g["sample"].append(str(title or "")[:120])
    #: ★ 跨作者是**下限 2**,配置只能**调高**不能调成 1 —— 单作者的事件名就是一篇标题。
    min_authors = max(2, int(getattr(settings, "wechat_event_min_authors", 2) or 0))
    picked = [(n, g) for n, g in by.items()
              if len(g["authors"]) >= min_authors and g["gossip"] >= 1]
    picked.sort(key=lambda kv: (-len(kv[1]["authors"]), -kv[1]["titles"], kv[0]))
    now = datetime.now()
    n = 0
    for name, g in picked[:top]:
        row = db.scalar(select(DemandName).where(
            DemandName.user_id == user_id, DemandName.platform == WEVENT_PLATFORM,
            DemandName.name == name))
        if row is None:
            row = DemandName(user_id=user_id, platform=WEVENT_PLATFORM, name=name,
                             first_seen_at=now, samples="{}")
            db.add(row)
        row.demand_hits = len(g["authors"])      #: 「几个作者在写」←→「多少人在点名」
        row.mention_hits = int(g["titles"])
        row.last_seen_at = now
        row.samples = json.dumps({"titles": g["sample"]}, ensure_ascii=False)[:4000]
        n += 1
    db.flush()
    if n:
        logger.info("公众号事件名:窗口 %.0fh 抽到 %d 个(候选 %d)→ %s",
                    float(hours), n, len(by), "、".join(x for x, _ in picked[:top]))
    return n


def guagua_candidates(db, user_id: int = 1, top: int = 3, days: int = 14) -> list[str]:
    """**大瓜候选名**:从小红书"挂了群聊 + 主题相关"的笔记里取到的名字(2026-11-04 用户:
    「串起来」)。

    为什么要它:抖音上搜 `吃瓜群/反差` 这类**泛词**只会搜到大众八卦(实测两轮口令 0);
    而小红书那边**挂群笔记的标题**本身就是"这一类"的坐标。拿这些**具体名字**回抖音搜,
    才是抖音能命中的形态(推广号标题会写《名字》)。
    """
    from datetime import timedelta

    from app.db.models import DemandName, DemandScan

    since = datetime.now() - timedelta(days=max(1, int(days)))
    out: list[str] = []

    #: ⚠️ **群名不是作品名**(2026-11-04 实测):小红书挂群笔记的标题多是
    #: `完全女儿国来的…急招男生`、`欢迎所有爱吃瓜的小伙伴加入`、`互助` ——
    #: 拿这些去抖音搜一条都对不上。只收**像作品名**的:≤12 字、不含群/加入/招/互助…
    _GROUP_WORDS = ("群", "裙", "加入", "欢迎", "互助", "招", "女生", "男生", "扩列", "聊天", "拉我",
                    "吃瓜", "闲聊", "诚信", "互", "阁", "交友", "搭子")

    def _add(x: str) -> None:
        n = str(x or "").strip()
        if not n or n in out:
            return
        if any(w in n for w in _GROUP_WORDS):
            return
        if is_name_like(n):          # 未加书名号 ⇒ 限 12 字,且过动词短语黑名单
            out.append(n)

    try:
        for r in db.scalars(select(DemandScan).where(
                DemandScan.user_id == user_id, DemandScan.platform == "xiaohongshu",
                DemandScan.has_group.is_(True), DemandScan.name != "",
                DemandScan.scanned_at >= since).order_by(
                DemandScan.scanned_at.desc()).limit(80)):
            _add(r.name)
            if len(out) >= top:
                return out
        for r in db.scalars(select(DemandName).where(
                DemandName.user_id == user_id, DemandName.platform == "xiaohongshu",
                DemandName.last_seen_at >= since).order_by(
                DemandName.last_seen_at.desc()).limit(40)):
            _add(r.name)
            if len(out) >= top:
                break
    except Exception:  # noqa: BLE001 - 取不到就返回空,不影响主流程
        logger.debug("取大瓜候选名失败", exc_info=True)
    return out[:max(0, int(top))]


def xunlei_group_hits(db, name: str, days: int = 30) -> int:
    """这个名字在**迅雷群消息**里出现过几次(2026-11-04 用户:「迅雷群里也会发这些资源」)。

    只读计数,用来告诉运营"这条大瓜在我们自己的群里也有人发"。
    """
    from datetime import timedelta

    from app.db.models import XunleiGroupShare

    n = str(name or "").strip()
    if len(n) < 2:
        return 0
    try:
        return int(db.scalar(select(func.count()).select_from(XunleiGroupShare).where(
            XunleiGroupShare.title.like(f"%{n}%"),
            XunleiGroupShare.msg_time >= datetime.now() - timedelta(days=max(1, int(days))),
        )) or 0)
    except Exception:  # noqa: BLE001 - 查不到就当 0
        logger.debug("迅雷群计数失败", exc_info=True)
        return 0


def known_name_set(db, user_id: int = 1, days: int = 30) -> set[str]:
    """**已知的资源名/作品名** —— 用来给评论里抽出的名字做**交叉验证**(2026-11-04 用户:
    「可以结合多个平台和网盘资源来判断」)。

    来源:① 网盘资源库的条目名(`xunlei_resources`);② 我们自己收到的线索名(`douyin_leads.mark`);
    ③ **另一个平台**上已经确认过的需求名(`demand_names` 里 platform 不是本平台的)。
    都做归一(去空格/标点),长度 ≥2 才纳入。
    """
    from datetime import timedelta

    from app.db.models import DemandName, DouyinLead, XunleiResource

    out: set[str] = set()

    def _add(x: str) -> None:
        n = _norm_comment(x)
        if len(n) >= 2:
            out.add(n)

    try:
        for n in db.scalars(select(XunleiResource.name).where(
                XunleiResource.user_id == user_id).limit(4000)):
            _add(str(n or ""))
        for m in db.scalars(select(DouyinLead.mark).where(
                DouyinLead.user_id == user_id, DouyinLead.mark != "",
                DouyinLead.found_at >= datetime.now() - timedelta(days=days)).limit(2000)):
            _add(str(m or ""))
        for n in db.scalars(select(DemandName.name).where(
                DemandName.user_id == user_id, DemandName.platform != PLATFORM,
                DemandName.last_seen_at >= datetime.now() - timedelta(days=days)).limit(500)):
            _add(str(n or ""))
    except Exception:  # noqa: BLE001 - 拿不到就不做交叉验证,别挡住主流程
        logger.debug("构建已知名字集合失败", exc_info=True)
    return out


def corroborated(name: str, known: set[str]) -> bool:
    """这个名字**在别的来源里也出现过**吗?(完全一致,或一方是另一方的子串,且长度≥3)"""
    n = _norm_comment(name)
    if len(n) < 2 or not known:
        return False
    if n in known:
        return True
    for k in known:
        if len(k) >= 3 and (k in n or (len(n) >= 3 and n in k)):
            return True
    return False


def _upsert(db, user_id: int, name: str, demand: int, mention: int, video_hits: int,
            samples: dict, ones: int = 0, title: int = 0, heat: int = 0) -> object:
    """把一个名字的**本轮观测**并进表里(累计值 + 最近样例)。"""
    from app.db.models import DemandName

    row = db.scalar(select(DemandName).where(
        DemandName.user_id == user_id, DemandName.platform == PLATFORM,
        DemandName.name == name))
    now = datetime.now()
    if row is None:
        row = DemandName(user_id=user_id, platform=PLATFORM, name=name,
                         first_seen_at=now, samples="{}")
        db.add(row)
    row.demand_hits = int(row.demand_hits or 0) + int(demand)
    row.mention_hits = int(row.mention_hits or 0) + int(mention)
    row.video_hits = int(row.video_hits or 0) + int(video_hits)
    row.ones_hits = int(getattr(row, "ones_hits", 0) or 0) + int(ones)
    row.title_hits = int(getattr(row, "title_hits", 0) or 0) + int(title)
    row.heat_score = int(getattr(row, "heat_score", 0) or 0) + int(heat)
    row.last_seen_at = now
    row.samples = json.dumps(samples, ensure_ascii=False)[:4000]
    return row


#: 作品 URL → 事件 id。抖音是 `/video/<aweme>`、小红书是 `/explore/<note>`
#: (`/discovery/item/` 是小红书的另一种老形状,一并认)。
_EVENT_URL_RE = re.compile(r"/(?:video|explore|discovery/item)/([0-9A-Za-z]+)")


def _event_key(row) -> str:
    """这个名字是在**哪条作品**上被抽到的 —— 同一条作品上的多个名字是**同一个瓜**。

    ★★ 2026-11-25 实测:`狗爹`(提74)/ `小桃`(提37)/ `狗爹小桃`(11)/ `换狗爹`(10)/
    `狗爹身材`(9)/ `困困狗`(10) —— **六个名字全部来自同一条视频**
    (`7710851964850384805`)。它们不是六个瓜,是**同一个瓜的六种叫法**;
    按名字各自推卡,客户看到的是满屏重复的同一件事。
    取不到出处时退回按名字分组(逐个独立),不把不同的瓜误并。

    ⚠️ `samples` 是**最近一轮**的快照(`_upsert` 覆盖写),不是历史全量 ——
    所以这里回答的是"它最近一次出现在哪条作品上",对"最近在说哪个瓜"够用。
    """
    try:
        meta = json.loads(row.samples or "{}")
    except (ValueError, TypeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    for u in (meta.get("video"), *(meta.get("videos") or ())):
        m = _EVENT_URL_RE.search(str(u or ""))
        if m:
            return m.group(1)
    return f"name:{row.name}"


def _is_fragment(row, group) -> bool:
    """`row` 是不是组内某个**更短名字**的碎片?

    ★★ **书名号优先**(2026-11-25):谁的名字**出现在作品标题的书名号里**(`title_hits`),
    谁就是正式作品名 —— 这一条压过下面的提及数判据。
    实测依据(用户举的「麦穗与羊」):`麦穗与羊`(书名号出处 1 次,提及 5)
    vs `完了麦穗与羊`(书名号 0 次,提及 8)—— 纯看提及数会把**正式名**留下、
    把**加料的版本**当成真名;而 `完了` 显然是评论里那句「完了麦穗与羊」的开头。

    书名号相同(通常两边都没有,评论里大家只写名字)时,退回提及数判据:
    **短名是长名的子串,且短名出现得不少于长名**。
    实测:`狗爹小桃`(11) 含 `狗爹`(74)、`换狗爹`(10) 含 `狗爹`(74)、
    `狗爹身材`(9) 含 `狗爹`(74)、`喜欢小黄`(6) 含 `喜欢`(21) 与 `小黄`(15) ——
    这四个都是"在一个真名字上加了个动词/名词",不是独立的名字。

    ⚠️ 与抽取阶段的 `_drop_subsumed` **方向相反**,不要合并成一个函数:
    那边丢的是"被句子切断的**短**碎片"(`林予` ⊂ `林予曦`),靠的是长名出现得一样多;
    这里丢的是"在真名字上**加料**的**长**碎片"(`换狗爹` ⊃ `狗爹`),靠的是短名出现得更多。
    同名之间的包含(`小桃` ⊂ `小桃身材`)两边都成立,结果一致,不冲突。
    """
    n = str(row.name or "")
    m = int(row.mention_hits or 0)
    t = int(getattr(row, "title_hits", 0) or 0)
    for o in group:
        on = str(o.name or "")
        if o is row or len(on) < 2 or on == n or on not in n:
            continue
        ot = int(getattr(o, "title_hits", 0) or 0)
        #: ① 短名有书名号出处、我没有 ⇒ 我是在正式作品名上加料的碎片,**不看提及数**。
        if ot > t:
            return True
        #: ② 反过来:我有书名号、短名没有 ⇒ 我是正式名,短名只是简称,谁都不剔。
        if t > ot:
            continue
        #: ③ 两边书名号情况相同 ⇒ 退回提及数判据。
        if int(o.mention_hits or 0) >= m:
            return True
    return False


def pick_pushable(rows, *, min_demand: int = 2, min_ones: int = 20,
                  cooldown_hours: int = 48, limit: int = 10,
                  min_mention: int = 6, max_age_days: int = 3,
                  per_event: int = 2,
                  dropped: dict | None = None) -> list:
    """够格、**冷却期外**,且**按事件合并**后最该推的名字。**新鲜度优先**。

    ★ 四条门槛满足**任意一条**就算够格(用户口径「判断是根据作品里面的**求和1**」):
      · 有人**求**(`demand_hits ≥ min_demand`);
      · 或者**扣1跟帖够多**(`ones_hits ≥ min_ones`);
      · 或者**评论区把它念了很多遍**(`mention_hits ≥ min_mention`,2026-11-06 新增)。
        这一类(`水子哥`×9 / `甜甜`×8)观众**根本没在"求"** —— 他们是在**议论这个人**,
        所以 `demand`/`ones` 都是 0,前两条永远不放行。实测这类视频恰恰是**大瓜**本身
        (某人塌方,评论区满屏叫他的名字),漏掉它等于漏掉整条链最想要的东西。
        ⚠️ 门槛设 6 而不是 3:重复 3 次的常常还是短语(`好可爱`),6 次以上才像真名。
      · 或者**名字出现在作品标题的书名号里**(`title_hits ≥ 1`,2026-11-25 新增)。
        用户口径「基本上作品名都有资源名」—— 书名号里的是**正式作品名**,比"评论区念了
        6 遍"更硬。实测 `麦穗与羊`:书名号出处 1 次,但评论里大家写的是加料的
        `完了麦穗与羊`(提及 8) ⇒ 纯按提及数,正式名连准入门槛都过不了
        (`mention=5 < 6`、`demand=0`、`ones=0`),根本走不到 `_is_fragment` 那一步。
        ⚠️ 只放 `title_hits≥1` 不会灌进噪音:实测近 3 天 `title_hits>0` 的只有 3 个名字。

    ★★ **大瓜不能靠"名字已知"来准入**(2026-11-25 用户:「大瓜还是不能单纯的依赖名字,
    因为大瓜要追求新鲜度肯定就是未知的名字」)。实测印证:37 个够格且新鲜的名字里,
    只有 **1 个**(`第二季`)能在资源库/线索名里被印证;而真名字 `狗爹`/`小桃`/`困困狗`
    **全部零印证** —— 它们正因为是**新的**,才没被库收录。所以那道"交叉验证"只能当
    **加分项**,绝不能当准入闸。这里的准入靠**结构信号**:
      ① `_event_key` **按作品合并** —— 同一条视频上的六个变体只占一个事件的名额;
      ② `_is_fragment` 剔除"真名字加料"的长碎片;
      ③ 每个事件最多 `per_event` 个名字(默认 2:一条视频里通常就一两个主角)。

    ★★ **排序改成"首次出现新→旧"**(原来按累计量降序):累计量的排序下,
    `后续`(首见 11-19)/`女主`(11-15)/`红果`(11-18) 这类**已经凉了的老名字**
    永远压在新瓜前面 —— 实测 top10 里 7 个是首见 4~7 天前的。而大瓜的价值就在"新"。

    ⚠️ **加冷却**:本作业 4 小时一轮,不加冷却的话同一个名字一天会推 6 次 ——
    群里会把它当成噪音(`push_ledger` 那套冷却的同一个道理)。

    ★★ **还要看这个名字有多久没被提起**(2026-11-24,`max_age_days`):原来只判"够不够格"
    + 冷却,于是**凉透的名字**照样反复推 —— 实测 `七夕` 最后一次被见到是 11-04,
    却在 **11-24 12:50** 还在推;`有人`(11-03)11-24 08:51、`有偿`(11-03)11-23 04:51。
    `last_seen_at` 由 `_upsert` 每次扫到就刷新,所以"三天没再被提起"= 这个名字已经凉了。
    ⚠️ `last_seen_at` 为 `None`(**从没记录过**,理论上不会有)⇒ **放行**,不拿"不知道"当"凉了"。
    """
    now = datetime.now()
    qualified = []
    too_old = 0
    for r in rows:
        demand = int(r.demand_hits or 0)
        ones = int(getattr(r, "ones_hits", 0) or 0)
        mention = int(r.mention_hits or 0)
        titled = int(getattr(r, "title_hits", 0) or 0)
        if (demand < int(min_demand) and ones < int(min_ones)
                and mention < int(min_mention) and titled < 1):
            continue
        if r.last_pushed_at and (now - r.last_pushed_at) < timedelta(hours=cooldown_hours):
            continue
        #: 名字凉了就**不再推** —— 它与素材视频的发布时间闸是同一族问题的第二半。
        seen = getattr(r, "last_seen_at", None)
        if (int(max_age_days or 0) > 0 and seen is not None
                and (now - seen) > timedelta(days=int(max_age_days))):
            too_old += 1
            continue
        qualified.append(r)

    #: ① 按**作品(事件)**分组;② 组内按提及数降序,先剔碎片再限名额。
    groups: dict[str, list] = {}
    for r in qualified:
        groups.setdefault(_event_key(r), []).append(r)
    cap = max(1, int(per_event))
    out = []
    fragments = overflow = 0
    for _ev, group in groups.items():
        group = sorted(group, key=lambda r: int(r.mention_hits or 0), reverse=True)
        live = []
        for r in group:
            if _is_fragment(r, group):
                fragments += 1
            else:
                live.append(r)
        out.extend(live[:cap])
        overflow += max(0, len(live) - cap)

    if dropped is not None:
        #: ⚠️ 拦了多少要显形 —— 否则「名字全凉了没得推」与「名字表空了」长得一样。
        dropped["name_stale"] = too_old
        #: ★ 事件合并/碎片剔除也是闸门,同样必须显形(2026-11-25):
        #: 不写出来的话,"这一轮只推了 3 个"分不清是**没有新瓜**还是**全被合并了**。
        dropped["name_fragment"] = fragments
        dropped["name_event_cap"] = overflow

    #: ③ 新鲜度优先:首见越新越靠前;同一批里按提及数。`first_seen_at` 缺失 ⇒ 垫底。
    out.sort(key=lambda r: (getattr(r, "first_seen_at", None) or datetime.min,
                            int(r.mention_hits or 0)), reverse=True)
    return out[:max(0, int(limit))]


def build_card(rows) -> dict:
    """飞书卡片:**名字 + 被求次数 + 一条样例**。`url` 是抽到它的那个视频。"""
    lines = []
    for i, r in enumerate(rows, 1):
        try:
            meta = json.loads(r.samples or "{}")
        except ValueError:
            meta = {}
        sample = ((meta.get("comments") or [""])[0] or "").replace("\n", " ")
        url = str(meta.get("video") or "")
        head = (f"**{i}. {r.name}** — 求 {int(r.demand_hits or 0)} 次 · "
                f"扣1 {int(getattr(r, 'ones_hits', 0) or 0)} 条 · "
                f"提及 {int(r.mention_hits or 0)} 次 · "
                f"评论热度 {int(getattr(r, 'heat_score', 0) or 0)}")
        tail = f"「{sample[:40]}」" if sample else ""
        lines.append(f"{head}\n{tail}" + (f" [原视频]({url})" if url else ""))
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text",
                             "content": f"🔍 评论区在求的名字 · 抖音({len(rows)})"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(lines)}},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "只监控名称,不保存资源;名字来自公开评论,点「原视频」可回溯"}]},
        ],
    }


def push_card(rows, settings) -> bool:
    from app.services.feishu_client import FeishuClient, webhook_for

    #: ★ 2026-10-30 用户口径「先收集数据**发到管理群**,再根据数据改策略」——
    #: 这一整套(需求名/最新/求1型/新名字)都先发**管理群**,不打扰客户群。
    webhook = webhook_for(settings, "admin")
    if not webhook:
        logger.info("没配抖音板块 webhook,需求名卡片不推")
        return False
    return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(build_card(rows))


def _rotate_seeds(db, seeds: list[str], n: int) -> list[str]:
    """每轮只搜 `n` 个参考作品名,**轮着来**(2026-10-30)。

    ⚠️ 为什么不一次搜完 9 个:抖音**连发会进「空成功」限流**(实测 ~35 分钟自愈),
    而且这套作业 4 小时一轮 —— 一轮 3 个名字 × 4 小时 = 一天把 9 个名字各过 2~3 遍,
    比"一次打 9 个搜索再被限"划算。游标记在 `system_config`,重启不丢
    (与类目轮换 `lead_word_category` 同一套做法)。
    """
    from app.db.models import SystemConfig

    if not seeds:
        return []
    n = max(1, int(n or 1))
    if len(seeds) <= n:
        return list(seeds)
    key = "demand_seed_cursor"
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == key))
    cur = int(row.value) if row and str(row.value).isdigit() else 0
    cur %= len(seeds)
    picked = [seeds[(cur + i) % len(seeds)] for i in range(n)]
    nxt = str((cur + n) % len(seeds))
    if row is None:
        db.add(SystemConfig(key=key, value=nxt))
    else:
        row.value = nxt
    db.commit()
    return picked


def publish_ts_of(aweme_id, declared=None) -> int:
    """这条素材的**发布时间**(Unix 秒);两个来源都判不了 ⇒ `0`。

    ## 为什么需要它(2026-11-24,用户「有的大瓜已经是很久之前的了」)

    这条链一直是拿 `found_at`/`first_seen_at`(**我们抓到的时刻**)当素材新鲜度 ——
    于是**三年前的老视频**今天第一次被扫到,照样算"新素材"喂进评论扫描。实测近 7 天:
    `douyin_peer_works` 831 条里 **707 条发布超过 30 天**;`demand_scans` 里被判求1型的
    抖音视频 26 条中 **20 条发布超过 7 天**(`金智秀flower` 是 2023-04-10,被推了 9 次)。
    这与记忆 `published-at-not-captured-at` 记的是**同一个坑**。

    ## 取值优先级

    ① `declared` —— 调用方手上**权威**的 `create_time`(协议搜索 / 作者主页作品接口);
    ② **抖音雪花 id 高位** `int(aweme_id) >> 32` —— `aweme_id` 是雪花 ID,高 32 位就是
       发布时的 Unix 秒。实测(400 条有 `publish_at` 的样本)**中位误差 1 秒、p90 = 3 秒、
       397/400 在 60 秒内** ⇒ 可直接当发布时刻用。这一档是**兜底**,让 `douyin_leads`/
       `douyin_peer_works` 里那批没记 `publish_at` 的存量行也能判新旧(`douyin_peer_scan`
       早就用同一个性质做「雪花锚点」,见 `douyin_peer_scan` 1051~1083 行的注释)。

    ⚠️ 雪花高位只在 **19 位、且落在 1e9~2e9**(2001~2033 年)时采信 ——
    位数不对/越界一律当"判不了",**不许**把一个离谱值当时间用(那是假新鲜度)。
    """
    if isinstance(declared, datetime):
        return int(declared.timestamp())
    try:
        if declared is not None and int(declared) > 0:
            return int(declared)
    except (TypeError, ValueError):
        pass
    s = str(aweme_id or "").strip()
    if len(s) == 19 and s.isdigit():
        ts = int(s) >> 32
        if 1_000_000_000 <= ts <= 2_000_000_000:
            return ts
    return 0


def _age_days(ts: int, now: datetime | None = None) -> float:
    """发布时间戳距今天数;`ts<=0`(判不了)返回 `-1`。"""
    if int(ts or 0) <= 0:
        return -1.0
    try:
        return ((now or datetime.now()) - datetime.fromtimestamp(int(ts))).total_seconds() / 86400.0
    except (OSError, OverflowError, ValueError):
        return -1.0


def _age_note(stats: dict | None) -> str:
    """素材侧的三段显形:`拦:…` / `已扫N 回填N` / `源:搜N/作N/线N/标N/热N`。

    三段都是同一个母题:没有它,"闸门把素材全挡了"/"每轮都在扫同一批"/"素材全来自
    搜索、库内一条没用"这三种失灵,在 `runs` 里与"今天正常"长得一模一样。

    ⚠️ 全部为 0 时返回空串 —— 运行记录不该被常量填满;要看全貌去 `doc`。
    """
    s = stats or {}
    bits: list[str] = []
    st, un = int(s.get("stale") or 0), int(s.get("unknown") or 0)
    if st or un:
        cut = []
        if st:
            cut.append(f"老{st}")
        if un:
            cut.append(f"无时间{un}")
        bits.append("拦:" + "/".join(cut))
    skip, fill = int(s.get("rescanned") or 0), int(s.get("refilled") or 0)
    if skip or fill:
        bits.append(f"已扫{skip} 回填{fill}")
    groups, brand = int(s.get("lead_groups") or 0), int(s.get("brand") or 0)
    if groups or brand:
        bits.append(f"事件{groups}组" + (f"(品牌{brand})" if brand else ""))
    hotnames = int(s.get("hotname") or 0)
    if hotnames:
        bits.append(f"热榜名{hotnames}")
    srcs = [(k, int(s.get(f"src_{k}") or 0))
            for k in ("search", "author", "lead", "peer", "hot")]
    if any(n for _k, n in srcs):
        #: 搜索/作者合并成「搜」与「作」—— 它们是同一条路(抖音搜索出来的),
        #: 分开写只会让运行记录变长而看不出差别。
        abbr = {"search": "搜", "author": "作", "lead": "线", "peer": "标", "hot": "热"}
        bits.append("源:" + "/".join(f"{abbr[k]}{n}" for k, n in srcs))
    return (" " + " ".join(bits)) if bits else ""


#: 「**获取类**动词」—— 标题里有它,说明这条视频在**卖怎么拿到东西**,不是作品本身。
#: ★ 2026-11-25 在生产库上标定(近 7 天 236 条线索 / 75 组):
#: 真事件组(`麦穗与羊` 0.00 / `下一个天亮` 0.05 / `斗破苍穹剧场版` 0.00 / `GTA6` 0.00)
#: 与资源号品牌组(`红日宝库` / `二哈部落` / `百战百胜` / `东洋宝库` 全 1.00)
#: 被这一把尺子**完整分开**。⚠️ 词表只放「获取资源」义的字,不放大词
#: (`推荐/合集/最新` 在真事件标题里也常见,放进去就把事件判成品牌了)。
_ACQ_RE = re.compile(
    r"(教程|获取|下载|安装|美化|入口|代码|提取码|链接|自取|领取|安装包|修改器|破解|内购|卡密|资源包)")

#: 一组素材里「获取类」占比达到它 ⇒ 判定为**资源号品牌**(不是作品/事件)。
_ACQ_BRAND_RATIO = 0.5

#: 事件分组取名的上限:书名号里的名字再长就不是作品名了(与 `is_name_like` 同口径)。
_EVENT_NAME_MAX = 30


def _event_name(title: str, mark: str = "") -> str:
    """一条素材**在说哪个作品/事件** —— 取标题里**最后一个**书名号,没有就退回口令/标题键。

    ★ 为什么取最后一个:`hot_breakout._primary_name` 已经在一手数据上定过这条 ——
    中文标题里主作品在后面(`国漫《斗破苍穹》剧场版《双帝之战》定档` ⇒ 主 = 双帝之战),
    而前面那些书名号常是"原班人马/前作"。两处**必须同一把尺子**,否则素材池与热榜
    对同一个作品会分成两个组(本仓母题:两处归一不同 ⇒ 计数恒 0)。

    ⚠️ `mark`(口令)是次选:它常常就是资源名本身;最后才退回 `resource_key(标题)`。
    """
    from app.services.resource_library import resource_key

    names = _QUOTED_RE.findall(str(title or ""))
    for raw in reversed(names):
        n = _clean(raw)
        if n and len(n) <= _EVENT_NAME_MAX:
            return resource_key(n)
    m = _clean(mark) if mark else ""
    if m:
        return resource_key(m)
    return resource_key(title)


def _is_brand(titles: list[str], marks: list[str] | None = None) -> bool:
    """这组素材是不是**资源号品牌**(而不是一个作品/事件)?

    ★★ 2026-11-25 实测(近 7 天线索按事件分组,75 组):光看"有没有书名号"分不出
    作品与品牌 —— 两边都带书名号。真正的分野是**标题在说什么**:
      · `红日宝库`/`二哈部落`/`百战百胜`/`东洋宝库`(`教程/获取/链接…` 占比 **1.00**)
        —— 这是**一个号的名字**,它下面每条视频讲的是不同的软件/资料,不是一个瓜;
      · `麦穗与羊`/`下一个天亮`/`斗破苍穹剧场版`/`GTA6`(占比 **0.00~0.05**)
        —— 这是**作品名**,跨作者同时被推,才是要追的事件。
    资源号品牌的素材**降级**(排到真事件之后),不是丢弃:它们的评论区同样是
    "求资源"现场,只是不该抢真事件的位子。
    """
    ts = [str(t or "") for t in titles if str(t or "").strip()]
    if not ts:
        return False
    ms = [str(m or "") for m in (marks or ()) if str(m or "").strip()]
    hit = sum(1 for t in ts if _ACQ_RE.search(t))
    if ms:
        hit += sum(1 for m in ms if _ACQ_RE.search(m))
        return hit / float(len(ts) + len(ms)) >= _ACQ_BRAND_RATIO
    return hit / float(len(ts)) >= _ACQ_BRAND_RATIO


class _EventGroup(NamedTuple):
    """素材池里的**一个事件分组**(见 `_event_groups`)。"""
    key: str
    rows: list
    authors: int
    brand: bool
    newest: int


def _event_groups(rows, *, title_of, author_of, ts_of, mark_of=None) -> list[_EventGroup]:
    """把素材候选**按事件分组**,并给每组算好排序用的三项:跨作者数 / 品牌 / 最新发布。

    ## 为什么素材也要按事件轮换(2026-11-25 实测)

    原来的取法是 `ORDER BY found_at DESC` 每轮取前 N 条 —— 在生产库上回放近 7 天的
    线索:三个真事件(`麦穗与羊` 23 人 / `下一个天亮` 18 人 / `斗破苍穹剧场版` 7 人)
    要到**第 70 轮**才轮得到,因为每天都有新的单条素材压在它们前面。
    改成按组轮换后,**第 1 轮三个事件全部覆盖**(每轮每组 1 条,组间真事件优先)。

    ## 组间排序(元组,升序)

    `(品牌, -跨作者数, -条数, -最新发布)` —— 真事件在前;同一档里**跨作者多的先**
    (跨作者分散度是"这是个真事件"最硬的信号:`缅北温柔陷阱` 14 条但只有 1 个作者,
    那是**一个号的连载**,不是大家在推的瓜),再比条数,最后比最新发布。

    ## 组内

    由调用方按**发布时间新→旧**取(最新的那一条最可能还在发酵)。

    ⚠️ `rows` 里的行对象形状不统一(`DouyinLead`/`DouyinPeerWork`),所以三个取值器
    由调用方传进来 —— 这里**不猜属性名**。
    """
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(_event_name(title_of(r), mark_of(r) if mark_of else ""), []).append(r)
    out: list[_EventGroup] = []
    for key, grp in groups.items():
        authors = {str(author_of(r) or "") for r in grp} - {""}
        out.append(_EventGroup(
            key=key, rows=grp, authors=len(authors),
            brand=_is_brand([title_of(r) for r in grp],
                            [mark_of(r) for r in grp] if mark_of else None),
            newest=max((int(ts_of(r) or 0) for r in grp), default=0)))
    out.sort(key=lambda g: (1 if g.brand else 0, -g.authors, -len(g.rows), -g.newest))
    return out


def _round_robin(groups: list[_EventGroup], room: int, *, pick):
    """按**组轮换**取 `room` 条:每轮每组最多 1 条,组内先取 `pick` 认为最该取的那条。

    ⚠️ 只影响**顺序**,不在这里判重/判新鲜:那些闸门在 `_add` 里,被挡掉的名额
    由下一组顺延(所以这里会遍历 `len(groups)` 轮,直到取满或候选耗尽)。
    """
    if room <= 0 or not groups:
        return []
    ordered = [sorted(g.rows, key=pick) for g in groups]
    out: list = []
    depth = max(len(rows) for rows in ordered)
    for i in range(depth):
        for rows in ordered:
            if room <= 0:
                return out
            if i < len(rows):
                out.append(rows[i])
                room -= 1
    return out


def _recently_scanned(db, user_id: int, days: int) -> set[str]:
    """近 `days` 天已经扫过评论的 `aweme_id` 集合(含今天已扫的)。

    ★ 为什么需要它(2026-11-24 实测):近 7 天 114 行扫描只覆盖 **82 个视频**,
    **28% 是重复**,单个视频最多**跨 7 天被扫 7 次** —— `source_videos` 按
    "评论数从多到少"取素材,只要不排除已扫的,每轮都会选中同一批高评论的老视频。
    `demand_scans` 就是现成的台账(每轮扫完都会落一行),不需要新表。
    """
    from app.db.models import DemandScan

    days = int(days or 0)
    if days <= 0:
        return set()
    since = datetime.now() - timedelta(days=days)
    rows = db.scalars(select(DemandScan.aweme_id).where(
        DemandScan.user_id == user_id,
        DemandScan.platform == PLATFORM,
        DemandScan.scanned_at >= since)).all()
    return {str(r) for r in rows if r}


def source_videos(db, user_id: int = 1, settings=None,
                  stats: dict | None = None) -> list[dict]:
    """挑要扫评论的视频。

    ## 优先级(2026-10-30 定)

    ① **用户给的参考作品名**(`DEMAND_SEED_WORDS`)。用户口径:「我上个对话给你的
       就是参考作品」—— 那几个名字**就是这一类**,按名字搜到的视频才是同款
       (实测 `狗爹和小桃` 搜到 13 条同款,而泛词 `吃瓜` 搜到的是亲子起名/外卖群)。
    ② 没配种子词时,用**库内素材**:`douyin_leads`(推广视频)与 `douyin_peer_works`(对标号)。
    ③ 两种都能补位 —— 谁先用完名额就停。
    ④ 补 **今天才冒出来的热点**(`hot_cycle`,形态词探针)。

    ⚠️ **④ 的名额是「预留」的,不是「剩下多少用多少」**(2026-11-13 修):
    库内素材永远是满的(近 7 天 332 条线索),原来的写法让 ② 一填满就 `return`,
    **④ 一次都轮不到** —— 实测 40 轮运行记录全是 `视频4 扫4`,`hot_word_seen`
    停在 `2026-11-11 09:49`、日志里「热点差分」**零次出现**;而探针词的另一个
    消费者 `douyin_leads.unfed_search_words` 读的就是那张表 ⇒ **两个消费者同时断供**,
    运行记录里的 `[探针0]` 不是"没搜到"而是"根本没取过词"。
    现在先扣掉 `demand_hot_videos`,库内素材只能用剩下的 `need - hot_budget`。
    顺序上的原意(**库内优先于热点**)不变:热点仍排在最后,只是名额不再被吃光。

    ⚠️ **不再按"标题里有没有书名号"筛搜索结果**:用户给的参考作品是"标题 + 一堆标签"的
    形状(没有书名号),那条筛选会把它们全挡掉。名字改用 `names_from_title` 从
    **书名号 + 标签**两处取。
    """
    from datetime import timedelta

    from app.db.models import DouyinLead, DouyinPeerWork

    settings = settings or _settings()
    days = int(getattr(settings, "demand_source_days", 7) or 7)
    since = datetime.now() - timedelta(days=max(1, days))
    need = max(1, int(getattr(settings, "demand_max_videos", 6) or 6))
    #: ★ **热点先扣名额**(2026-11-13):见 docstring 里那段实测。留 1 个给库内素材
    #: (库内是已知能出货的那批,不能被热点全挤掉)。
    hot_budget = min(int(getattr(settings, "demand_hot_videos", 2) or 0), need - 1)
    hot_budget = max(0, hot_budget)
    lib_need = need - hot_budget

    out: list[dict] = []
    seen: set[str] = set()
    #: 已经**进了池**的 id。与 `seen` 分开:被"已扫"跳过的那批也在 `seen` 里
    #: (防同一条在四段来源里被反复计数),但回填时它们才是**唯一可用的候选** ——
    #: 拿 `seen` 判重会让回填永远填不进去。
    out_ids: set[str] = set()

    #: ★★ **库内保底名额**(2026-11-24 实测):`①b 同款作者` 会把 `lib_need` 填满 ⇒
    #: `②线索`/`③对标号` **永远拿 0 个名额**(历史 263 行扫描里 260 行来自搜索/热点)。
    #: 但保底**只在库内真有候选时才算** —— 否则会把搜索/作者这两步也饿死
    #: (`test_同款作者的新作品也进候选池` 那种"库里什么都没有"的场景)。
    leads = db.scalars(select(DouyinLead).where(
        DouyinLead.user_id == user_id,
        DouyinLead.found_at >= since).order_by(DouyinLead.found_at.desc())).all()
    works = db.scalars(select(DouyinPeerWork).where(
        DouyinPeerWork.user_id == user_id,
        DouyinPeerWork.first_seen_at >= since).order_by(
        DouyinPeerWork.first_seen_at.desc())).all()
    reserve = max(0, min(int(getattr(settings, "demand_lib_reserve", 2) or 0), lib_need - 1))
    if not leads and not works:
        reserve = 0                    #: 库里没货 ⇒ 名额全给搜索/作者(保底无处可用)
    #: `search_cap` = ①+①b 合起来的上限(给库内让出的那部分是 `reserve`)。
    search_cap = max(1, lib_need - reserve)
    #: ② ②③ 共享剩下的名额,但 ③ 有候选时要留 1 个(否则 ② 一填满 ③ 又是 0)。
    peer_reserve = 1 if (works and reserve >= 1) else 0
    lead_stop = max(1, lib_need - peer_reserve)

    #: ★★ **内容发布时间闸**(2026-11-24,用户「有的大瓜已经是很久之前的了」)。
    #: 闸门**在进池前**判(不是扫完评论再筛):一条三年前的老视频,评论扫描额度花了
    #: 才发现"太老",那是纯浪费 —— 抖音连发还会进「空成功」限流。
    #: 两条窗口:`search_days`(搜来的,本来就是当下的)与 `lib_days`(存量线索/对标号,
    #: 里面老货极多 —— 实测 831 条对标作品 707 条超 30 天)。
    #: ⚠️ 拦下的条数**必须显形**(`stats` 由调用方写进运行记录):否则「闸门把素材全挡了」
    #: 与「今天本来就没素材」在 `runs` 里长得一模一样(本仓母题)。
    age_days = int(getattr(settings, "demand_max_age_days", 7) or 0)
    lib_days = int(getattr(settings, "demand_source_max_age_days", 0) or 0) or age_days
    allow_unknown = bool(getattr(settings, "demand_age_allow_unknown", False))
    counters = stats if stats is not None else {}
    for _k in ("stale", "unknown", "rescanned", "refilled"):
        counters.setdefault(_k, 0)

    #: ★ **近 N 天扫过的不再重复扫**(2026-11-24 实测:28% 的扫描行是重复,单个视频
    #: 最多跨 7 天被扫 7 次)。两段式:`_skip()` 判,`_backfill()` 在候选**全被跳过**时
    #: 按发布新→旧回填 —— 去重不该把素材池饿死(实测近 7 天池里有 240 条新鲜素材,
    #: 3 天轮换绰绰有余,但线索池更新慢时会不够)。
    skip_days = int(getattr(settings, "demand_skip_scanned_days", 3) or 0)
    scanned_recently = _recently_scanned(db, user_id, skip_days)
    skipped_cand: list[dict] = []

    def _skip(vid: str, cand: dict) -> bool:
        """近 N 天扫过 ⇒ 跳过,但**记下来**给回填用(不能静默丢)。"""
        if skip_days <= 0 or vid not in scanned_recently:
            return False
        skipped_cand.append(cand)
        counters["rescanned"] += 1
        return True

    def _backfill(room: int) -> None:
        """候选全被"已扫"跳过时,按发布时间新→旧回填 `room` 条。

        ⚠️ 回填**必须显形**(`refilled`)—— 否则"回填的老货"与"今天真没新素材"
        在运行记录里长得一样(本仓母题)。
        """
        if room <= 0 or not skipped_cand:
            return
        pool = sorted(skipped_cand, key=lambda c: int(c.get("publish_at") or 0), reverse=True)
        for cand in pool:
            if room <= 0 or len(out) >= lib_need + hot_budget:
                break
            #: ⚠️ 判重看 `out_ids` 而不是 `seen` —— 被跳过的那批在 `seen` 里,
            #: 拿 `seen` 判会让回填**一条都填不进去**(本函数存在的意义就没了)。
            if cand["aweme_id"] in out_ids:
                continue
            out.append(cand)
            out_ids.add(cand["aweme_id"])
            counters["refilled"] += 1
            room -= 1

    def _fresh(vid: str, declared=None, lib: bool = False) -> bool:
        """这条能不能进池。判不了时间 ⇒ 按 `demand_age_allow_unknown` 处理并计数。"""
        ts = publish_ts_of(vid, declared)
        win = lib_days if lib else age_days
        if win <= 0:
            return True                  #: 窗口 0 = **整道闸关掉**(连"无时间"也不拦)
        if ts <= 0:
            if allow_unknown:
                return True
            counters["unknown"] += 1
            return False
        if _age_days(ts) > float(win):
            counters["stale"] += 1
            return False
        return True

    def _add(vid: str, url: str, title: str, comments: int = 0, author: str = "",
             src: str = "", declared=None, lib: bool = False) -> None:
        """进池(被判重/太老/已扫跳过则静默返回,条数各自记在 `counters` 里)。

        ⚠️ 回填**不**走这里:回填本身就是"明知已扫过也要用",而这里会再 `_skip` 一次
        ⇒ 永远填不进去。见 `_backfill`。
        """
        vid = str(vid or "").strip()
        if not vid or vid in seen:
            return
        #: ⚠️ **先记 `seen` 再判新不新鲜**:同一条可能同时出现在线索表和对标号表里,
        #: 判新鲜之前不加 `seen` 的话,一条老货会被**计两次**,运行记录里的 `老N` 就虚高。
        seen.add(vid)
        if not _fresh(vid, declared, lib=lib):
            return
        cand = {"aweme_id": vid, "url": url or f"https://www.douyin.com/video/{vid}",
                "title": title or "", "comment_count": int(comments or 0),
                "author": str(author or ""), "src": src,
                "publish_at": publish_ts_of(vid, declared)}
        if _skip(vid, cand):
            return
        out.append(cand)
        out_ids.add(vid)

    # ① 参考作品名(有就先用) —— 每轮轮着搜 3 个,避免连发触发抖音限流
    seeds = [w.strip() for w in
             str(getattr(settings, "demand_seed_words", "") or "").split(",") if w.strip()]
    authors: dict[str, str] = {}
    if seeds:
        from app.services import douyin_protocol_source as dps

        seeds = _rotate_seeds(db, seeds,
                              int(getattr(settings, "demand_seeds_per_round", 3) or 3))
        min_c = int(getattr(settings, "demand_min_comments", 20) or 0)
        vids = dps.search(seeds, settings=settings, session=db, user_id=user_id, pages=1)
        for v in vids:                                  # 顺手记下同款作者,下一步用
            if v.get("uid"):
                authors.setdefault(str(v["uid"]), str(v.get("name") or ""))
        #: 评论数太少就没有统计意义;按评论数从多到少取
        vids = [v for v in vids
                if int((v.get("metrics") or {}).get("comment_count") or 0) >= min_c]
        vids.sort(key=lambda v: int((v.get("metrics") or {}).get("comment_count") or 0),
                  reverse=True)
        for v in vids:
            _add(v.get("aweme_id"), v.get("url"), v.get("snippet"),
                 (v.get("metrics") or {}).get("comment_count") or 0, v.get("name"),
                 declared=v.get("publish_at"), src="search")
            #: 参考作品只占**一半名额**:它们通常没有书名号(标题是"标签流"),
            #: 名字还得靠库内推广线索那半;全被搜索占满 ⇒ 这一轮一个名字都抽不到。
            #: ★ 2026-11-24:`search_cap` 还会给库内素材再让出 `reserve` 个名额。
            if len(out) >= min(max(1, lib_need // 2), search_cap):
                break

    # ★ **同款作者的新作品**也要进候选池(2026-10-30 用户:「根据这些来找方法去**发现新的大瓜**」)。
    # 参考作品 → 作者 → 他们的近期作品,这些才是"**最新的**求1型视频"的来源;
    # 只看我们自己的线索池永远发现不了新号。
    if len(out) < search_cap:
        from datetime import timedelta

        since_ts = int((datetime.now() - timedelta(
            days=int(getattr(settings, "demand_latest_days", 3) or 3))).timestamp())
        for sec, name in list(authors.items()):
            if len(out) >= search_cap:
                break
            res = fetch_author_posts(sec, session=db, user_id=user_id, count=20)
            if not res["ok"]:
                continue
            for it in res["items"]:
                if it["publish_at"] and it["publish_at"] < since_ts:
                    continue
                _add(it["aweme_id"], it["url"], it["desc"], 0, it["author"] or name,
                     declared=it["publish_at"], src="author")
                if len(out) >= search_cap:
                    break

    # ② 我们自己的线索(推广视频,评论区天然是「求资源」的)
    #: ★★ **按事件轮换,不按 `found_at`**(2026-11-25 实测):原来的 `found_at desc`
    #: 让三个真事件排到**第 70 轮**才轮到(见 `_event_groups` 的注释)。现在:
    #: 组间「真事件优先 → 跨作者多 → 条数多 → 最新发布」,组内按**发布时间新→旧**,
    #: 每轮每组只出 1 条 ⇒ 3 个真事件在**第 1 轮**就被覆盖。
    _lead_groups = _event_groups(
        leads, title_of=lambda r: r.title, author_of=lambda r: r.author,
        mark_of=lambda r: r.mark,
        ts_of=lambda r: publish_ts_of(r.aweme_id, r.publish_at))
    #: ★ **两个数必须显形**(`事件N组`/`品牌降级N`):不写的话,"这一轮又只扫到那几条"
    #: 分不清是**素材池里只有一个事件**还是**轮换没生效**(本仓母题)。
    counters["lead_groups"] = len(_lead_groups)
    counters["brand"] = sum(1 for g in _lead_groups if g.brand)
    _lib_cand: list[dict] = []          #: 先收集两个来源,再统一按事件轮换(见下)
    #: ⚠️ `room` 给**候选总数**而不是名额数:轮换只决定**尝试顺序**,真正把口的是
    #: 下面消费循环里的按来源名额。给名额数的话,被"已扫"跳过的那条会**吃掉一次尝试**,
    #: 后面的候选永远轮不到(实测:3 条候选只试 2 条,第 3 条白白丢掉)。
    for r in _round_robin(_lead_groups, len(leads),
                          pick=lambda r: -publish_ts_of(r.aweme_id, r.publish_at)):
        _lib_cand.append({"aweme_id": r.aweme_id, "url": r.url, "title": r.title,
                          "author": r.author, "declared": r.publish_at, "src": "lead"})

    # ③ 对标号作品(带网盘的那些)
    for w in _round_robin(
            _event_groups(
                works, title_of=lambda w: w.title, author_of=lambda w: w.account_name,
                mark_of=lambda w: w.mark,
                ts_of=lambda w: publish_ts_of(w.aweme_id, w.publish_at)),
            len(works),                  #: 同上:只定顺序,名额在消费循环里把
            pick=lambda w: -publish_ts_of(w.aweme_id, w.publish_at)):
        _lib_cand.append({"aweme_id": w.aweme_id, "url": w.url, "title": w.title,
                          "author": w.account_name, "declared": w.publish_at,
                          "src": "peer"})

    for c in _lib_cand:
        #: ⚠️ **先判再 `_add`,不是加完再判**(2026-11-17 修):原写法 `_add` 之后才
        #: `break` ⇒ 进入循环时 `len(out)` 已经等于 `lib_need` 时仍会**多塞一条**,
        #: 于是 ② 填满 3 条后 ③ 又加第 4 条 ⇒ `need - len(out) == 0` ⇒
        #: **④ 热点段的名额被吃掉,一次都轮不到**(实测:`demand_hot_videos=1` 而
        #: `hot_budget` 恒被算成 0,`hot_word_seen` 停在 11-11、日志里「热点差分」零次)。
        #: 名额按**来源**各自把口:线索最多 `lead_stop`、对标号最多 `lib_need`,
        #: 与轮换前的口径完全一致(轮换只改"取谁",不改"各来源几个名额")。
        if c["src"] == "lead":
            if sum(1 for v in out if v.get("src") == "lead") >= lead_stop:
                continue
        elif len(out) >= lib_need:
            continue
        _add(c["aweme_id"], c["url"], c["title"], 0, c["author"],
             declared=c["declared"], lib=True, src=c["src"])

    #: ★ **候选全被"近 N 天已扫"跳过时回填**(见 `_backfill` 的注释):
    #: 去重是为了别每轮扫同一批,**不是**为了把素材池扫空。
    _backfill(lib_need - len(out))

    # ④ ★ **今天才冒出来的热点**(2026-11-08 用户:「每次都会有新的热点出来,
    # 你需要总结这些规律来完成监控和预测」)。
    #: 用户 2026-11-08 纠正:「**热点事件名称是没有关联性的,每一个都是独立的事件**」
    #: ⇒ 取词**不能靠已知名字扩展**(那只出同一事件的不同说法)。
    #: 主通路现在是**形态词探针**(`hot_cycle.carrier_words`,2026-11-10):
    #: 用「后续/回应/通报/事件…」这类事件语法壳子查热点宝**全量词库**,能捞到榜单
    #: 盲区里的新事件(实测「只忍五次」16 万量,榜第 50 名是 217 万,差 13 倍进不了榜)。
    #: 探针不给力时自动退回「1 小时搜索榜全榜」(`board_words`)→ 种子族扩展(`emerging`)。
    #: 判据 = 序列点数 <23(**刚起量**,满 24 点是常青词)且峰值 >=15 万,
    #: 见 `doc/热点规律与监控-2026-11-09.md` 与 `hot_cycle.CARRIER_*` 的注释。
    #:
    #: ⚠️ **排在所有库内素材之后**:库内线索/对标号是**已知能出货**的那批,热点是
    #: **赌新事件**,不许把前者挤掉(单测 `test_素材优先取我们自己的线索_而不是搜吃瓜`
    #: 就是守这一条的)。
    #: ⚠️ 这些词**只拿来扫评论找名字**,不拿去搜网盘 —— 实测 7 个爆发词共 93 条搜索结果
    #: 里**带网盘链 0 条**。
    #: ⚠️ **2026-11-12 起这条注释的前半句已松动**:探针词**同时**会进 `douyin_leads`
    #: 的搜索词表(`unfed_search_words`,找的是推广号《口令》标题,不是网盘直链,
    #: 与上面说的「搜网盘 0%」是两条不同的路);这条评论扫描路照旧保留。
    #: 名额已在函数开头**预留**给热点(`lib_need = need - hot_budget`),这里直接用。
    #: 仍取 `min(need - len(out), ...)` 兜底:种子/作者那两步可能没填满 `lib_need`。
    hot_budget = max(0, min(need - len(out), hot_budget))
    if hot_budget > 0:
        from app.services import hot_cycle

        #: ⚠️ 这里用**带差分**的 `new_hot_words`(2026-11-10),不是 `hot_video_seeds`:
        #: 形态词探针每轮都会捞到同一批还在发酵的事件,不差分就是一天推 6 次同一件事。
        #: 它内部**自己开短会话**记账(写完立刻提交关闭),**不借这里的 `db`** ——
        #: 这个 `db` 后面还要跨搜索/评论那些慢活,拿它记账会把写锁抱到慢活结束
        #: (本仓在 `new-write-point-holds-lock-over-slowwork` 上栽过两次)。
        hot_words = [h.word for h in hot_cycle.new_hot_words(
            settings, seeds, user_id=user_id, limit=hot_budget)]
        #: ★★ **热榜里的真作品名也算热点素材**(2026-11-25,用户口径「结合热点榜加热搜」)。
        #: 探针词是"事件句"(搜回来多是围观/解说),作品名才是评论区真在求的那种素材。
        #: 判据见 `hot_cycle.hot_work_names`:`hot_source_items` 内容型源、48h
        #: **跨 ≥2 个源**都出现的 `《作品名》` —— 实测 48h 只有 3 个,不会挤占探针词。
        #: ⚠️ **只读库、不写库**(不记账、不打戳):这里借的是现成快照,零网络成本;
        #: 它**不**消费 `douyin_leads` 的搜索额度(那边自己另取一份)。
        try:
            _hn = hot_cycle.hot_work_names(db, user_id)
        except Exception:  # noqa: BLE001 - 读不到就只用探针词
            logger.warning("热榜作品名读取失败", exc_info=True)
            _hn = []
        hot_words = hot_words + [n for n, _ in _hn if n not in hot_words]
        counters["hotname"] = len(_hn)
        if hot_words:
            from app.services import douyin_protocol_source as dps

            min_c = int(getattr(settings, "demand_min_comments", 20) or 0)
            try:
                hv = dps.search(hot_words, settings=settings, session=db,
                                user_id=user_id, pages=1)
            except Exception:  # noqa: BLE001 - 热点视频搜不到就退回原有素材
                logger.warning("新热点视频搜索失败:%s", "、".join(hot_words), exc_info=True)
                hv = []
            hv = [v for v in hv
                  if int((v.get("metrics") or {}).get("comment_count") or 0) >= min_c]
            hv.sort(key=lambda v: int((v.get("metrics") or {}).get("comment_count") or 0),
                    reverse=True)
            for i, v in enumerate(hv):
                if i >= hot_budget:
                    break
                _add(v.get("aweme_id"), v.get("url"), v.get("snippet"),
                     (v.get("metrics") or {}).get("comment_count") or 0, v.get("name"),
                     src="hot", declared=v.get("publish_at"))

    #: ★ **来源分布必须显形**(2026-11-24):历史 263 行扫描里 260 行来自搜索/热点、
    #: 库内线索只 3 行 —— 而这个失衡**在运行记录里一个字都没有**,只能靠事后 join
    #: `douyin_leads`/`douyin_peer_works` 才反推得出来(所以它存在了一个月没人发现)。
    #: 现在每轮把四个来源的条数直接写进 `stats`,由 `run_user` 落进 `runs.detail`。
    for _s in ("search", "author", "lead", "peer", "hot"):
        counters.setdefault(f"src_{_s}", 0)
        counters[f"src_{_s}"] = sum(1 for v in out if v.get("src") == _s)

    return out[:need]


def run_user(db, user_id: int = 1, settings=None) -> dict:
    """一轮:挑视频 → 拉评论 → 抽名字 → 入库 → 推卡(只存名字,不落网盘)。"""
    from app.db.models import DemandName
    from app.services.tenant_base import _record_run

    settings = settings or _settings()
    if not bool(getattr(settings, "demand_watch_enabled", True)):
        return {"skipped": "disabled"}

    # ★★ **没有抖音登录态是「跳过」,不是「故障」**(2026-11-17)。实测:user=2 的加密库里
    # **没有任何 `douyin` 凭据**(`user_cookies` 里只有 user=1 有),于是每轮 4 小时
    # 都抛一次 `DouyinProtocolError(kind="need_login")`,栈里写着「要求先登录」——
    # 而 user=2 这一侧**永远不可能登进来**(那个租户没有采集账号)。
    # 这既刷 ERROR 噪音(近 7 天 33 次),又让 `runs` 里分不清「链路坏了」与「这个号本来就没号」。
    # ⚠️ 判据必须落在**响应自己说的原因**上(`kind="need_login"`),不能靠预先检查凭据:
    # 预先检查会把"凭据刚好过期"也一起吞掉,那才是真的要人动手的那种。

    #: ★ 新鲜度闸的计数(2026-11-24):`{"stale": 太老被拦, "unknown": 判不了时间被拦}`。
    #: 由 `source_videos` 原地累加,这里**写进运行记录** —— 拦下的条数不显形的话,
    #: 「闸门把素材全挡了」与「今天本来就没素材」在 `runs` 里长得一模一样。
    age_stats: dict[str, int] = {}
    try:
        videos = source_videos(db, user_id, settings, stats=age_stats)
    except Exception as exc:  # noqa: BLE001 - 只拦"这个号没登录",其余照旧往上抛
        from app.services.douyin_protocol_source import DouyinProtocolError

        if not (isinstance(exc, DouyinProtocolError)
                and getattr(exc, "kind", "") == "need_login"):
            raise
        logger.info("需求名监控:user=%s 没有抖音登录态(%s),本轮跳过 —— 不是链路故障",
                    user_id, str(exc)[:60])
        db.rollback()
        return {"skipped": "no_douyin_credential"}

    agg: dict[str, dict] = {}
    errors: list[str] = []
    scanned = 0
    no_name = 0
    request_videos: list[dict] = []
    for v in videos:
        res = collect_for_video(str(v.get("aweme_id") or ""), session=db, user_id=user_id,
                                pages=int(getattr(settings, "demand_comment_pages", 2) or 2),
                                title=str(v.get("title") or ""))
        if res["error"]:
            errors.append(res["error"])
            continue
        scanned += 1
        no_name += int(res.get("no_name") or 0)
        ones = int(res.get("ones") or 0)
        heat = int(v.get("comment_count") or 0)
        url = str(v.get("url") or "")
        # ★★ **求1型识别**(用户「特点就是评论区都在发求和1」):
        # 判据 = 评论区里(整条 `1` + 带求语气的评论)占已拉评论的比例。
        total = int(res.get("total") or 0)
        demand_cnt = int(res.get("no_name") or 0) + sum(
            int(rec.get("demand") or 0) for rec in res["names"].values())
        if total >= 5:
            ratio = (ones + demand_cnt) / total
            dup = float(res.get("dup_ratio") or 0)
            by_density = (ratio >= float(getattr(settings, "demand_request_density", 0.1) or 0)
                          and (ones + demand_cnt) >= 3)
            #: 第二个信号:**满屏重复短句**(用户给的样本:59 条里 `刚看完`×8、`冰露`×5、`1`×6)。
            #: 实算那条的「求/1 密度」只有 **12%**(6 个 1 + 1 个求),按 20% 的门槛会被漏掉 ——
            #: 所以补这条:重复率 ≥ 30% 且评论 ≥ 10 也算这一类。
            by_repeat = (dup >= float(getattr(settings, "demand_repeat_ratio", 0.3) or 0)
                         and total >= 10)
            flagged = bool(by_density or by_repeat)
            tnames = names_from_title(v.get("title") or "")
            all_seeds = [w.strip() for w in str(
                getattr(settings, "demand_seed_words", "") or "").split(",") if w.strip()]
            vname = (tnames[0] if tnames else
                     (best_tag(v.get("title") or "", exclude=v.get("author") or "")
                      or match_seed(v.get("title") or "", all_seeds)))
            #: ★ **不管命不命中都落一行** —— 用户要「先收集数据,再根据数据改策略」;
            #: 只记命中的话,分母(扫了多少条)就没了,阈值调起来全靠猜。
            _record_scan(db, user_id, v, total=total, ones=ones, demand=demand_cnt,
                         dup=dup, flagged=flagged,
                         why=("求/1密度" if by_density else ("重复评论" if by_repeat else "")),
                         name=vname)
            if flagged:
                request_videos.append({
                    #: 迅雷群里有没有人也发过这个名字(用户:「这种也都会发网盘」)
                    "xunlei": xunlei_group_hits(db, vname) if vname else 0,
                    "name": vname,
                    "desc": v.get("title") or "",
                    "author": v.get("author") or "",
                    "url": url,
                    #: ★ **发布时间**带进卡片(2026-11-24):客户投诉的就是"很久之前的",
                    #: 卡片上必须能一眼看出这条有多老,而不是只看到「求 12 · 扣1 30」。
                    "publish_at": int(v.get("publish_at") or 0),
                    "why": "求/1密度" if by_density else "重复评论",
                    "demand": demand_cnt, "ones": ones, "total": total})
        # ★ **名字主要来自作品标题**(用户:「基本上作品名都有资源名」);
        # 而「扣1」的热度是**整条视频**级别的,归给这条视频里抽到的**所有**名字。
        #: ★★ 2026-11-25 修:`a["ones"] += ones` 原来**只在书名号循环里**累加,而近期
        #: 扫描的视频标题几乎不带《》 ⇒ 生产库 `demand_names` 328 行里 **326 行
        #: `ones_hits` 恒为 0**(其余两行最大才 3),卡片上永远是「扣1 0 条」。
        #: 而 `demand_min_ones=20` 是三条准入之一 —— 恒为 0 等于**这条准入从来没生效过**。
        #: 现在两条分支都记,用 `credited` 去重(同一个名字既在标题又在评论里时只算一次)。
        credited: set[str] = set()
        for tname in names_from_title(v.get("title") or ""):
            a = agg.setdefault(tname, {"demand": 0, "mention": 0, "video_hits": 0,
                                       "ones": 0, "title": 0, "heat": 0,
                                       "comments": [], "video": "",
                                       "entity_only": False})
            a["title"] += 1
            a["entity_only"] = False          #: 标题里有书名号 ⇒ 它本来就是个作品名
            a["mention"] += 1
            a["ones"] += ones
            a["heat"] += heat
            credited.add(tname)
            if not a["video"]:
                a["video"] = url
        for name, rec in res["names"].items():
            a = agg.setdefault(name, {"demand": 0, "mention": 0, "video_hits": 0,
                                      "ones": 0, "title": 0, "heat": 0,
                                      "comments": [], "video": "",
                                      "entity_only": True})
            a["demand"] += rec["demand"]
            a["mention"] += rec["mention"]
            a["video_hits"] += 1
            if name not in credited:
                a["ones"] += ones
                a["heat"] += heat
                credited.add(name)
            #: 只要有一条视频里它是靠**句式**(书名号/「求 X」)抽到的,就不再是"纯重复词"
            if not rec.get("entity"):
                a["entity_only"] = False
            for s in rec["samples"][:2]:
                if len(a["comments"]) < 3 and s not in a["comments"]:
                    a["comments"].append(s)
            if not a["video"]:
                a["video"] = url

    # ★ 「求1型」单独一张卡(判据见上),**即使一个名字都没抽到也要推** ——
    #: 用户要的就是"这类视频"本身;名字取不到时卡上写「(标题里没名字)」。
    request_pushed = 0
    if request_videos:
        from app.services.push_ledger import drop_recent, record

        kept, _dropped = drop_recent(db, user_id, "douyin", request_videos, settings)
        if kept and push_request(kept, settings):
            record(db, user_id, "douyin", kept)
            request_pushed = len(kept)

    if not agg:
        #: ⚠️ **扫了却一个名字都没抽到,要标 partial 而不是 success** ——
        #: 否则「链路坏了」和「这轮评论没人求」在 `runs` 里长得一模一样(本仓最忌的假成功)。
        #: `求N(无名)` 是**素材对不对**的唯一证据:它 >0 说明有人求、只是没说出名字;
        #: 它 =0 说明这批素材的评论区压根不是"求资源"的场景。
        _record_run(db, user_id, "demand_watch", "partial" if videos else "success",
                    f"视频{len(videos)} 扫{scanned} 名字0 求{no_name}(无名)"
                    f" 求1型{len(request_videos)}(推{request_pushed})"
                    + _age_note(age_stats)
                    + (f" 失败:{errors[0][:60]}" if errors else ""))
        db.commit()
        return {"videos": len(videos), "names": 0, "request": len(request_videos)}

    kept_names = dropped_names = dropped_common = 0
    #: ★ **交叉验证用**(2026-11-04 用户「结合多个平台和网盘资源来判断」):
    #: 网盘资源库 + 我们收到的线索名 + **另一个平台**上确认过的需求名。
    known = known_name_set(db, user_id)
    for name, a in agg.items():
        #: ★★ **只留"像名字"的**(2026-11-04 数据):名字表 20 条里大半是**评论片段**
        #: (`抖音号`/`过人`/`有偿`/`认识女主`/`最后用`/`去汉中路`/`壁纸`),
        #: 共同点是**只在一条视频的评论里出现过一次**。规则:
        #:   ① 出自**标题**(作品名)⇒ 留;② 出现在 **≥2 个视频** ⇒ 留;
        #:   ③ 被求 **≥2 次** ⇒ 留。其余都是单条评论里的偶发片段,丢掉。
        #: ④ **交叉验证**(2026-11-04):名字在网盘资源库 / 线索名 / 别平台的需求名里出现过
        #: ⇒ 不是评论里的偶发片段,留。
        #: ⑤ ⚠️ **纯重复词的方向与 ② 相反**(2026-11-06):只靠"评论区重复出现"拿到的名字
        #: (`entity_only`),**跨视频反而是泛词的证据** —— 实测 `电影` 出现在 4 个视频、
        #: `什么` 7 个,而真名字(`水子哥`/`甜甜`/`林予曦`)**只在一个视频**里刷屏。
        #: 所以这一类要求 `video_hits == 1`,并靠 ③④ 兜底(重复够多、或被库里别处印证)。
        # 先取快照:下面的准入条件在**几轮之间会改**,两条分支的含义不一样,别写成一行。
        entity_only = bool(a.get("entity_only"))
        video_hits = int(a["video_hits"])
        is_signal = (a["title"] >= 1 or video_hits >= 2 or a["demand"] >= 2
                     or corroborated(name, known))
        #: 纯重复词的准入:**只在一个视频里刷屏** + 重复 ≥3 条(它没有"被求"语义,
        #: `demand>=2` 对它永远不成立) —— 见 ⑤。
        entity_ok = entity_only and video_hits == 1 and int(a["mention"]) >= 3
        if entity_only and video_hits > 1:
            dropped_common += 1
            continue
        if not (is_signal or entity_ok):
            dropped_names += 1
            continue
        kept_names += 1
        _upsert(db, user_id, name, a["demand"], a["mention"], a["video_hits"],
                {"comments": a["comments"], "video": a["video"]},
                ones=a["ones"], title=a["title"], heat=a["heat"])
    db.commit()

    rows = db.scalars(select(DemandName).where(
        DemandName.user_id == user_id,
        DemandName.platform == PLATFORM)).all()
    name_stats: dict[str, int] = {}
    chosen = pick_pushable(
        rows,
        min_demand=int(getattr(settings, "demand_min_hits", 2) or 2),
        min_ones=int(getattr(settings, "demand_min_ones", 20) or 0),
        cooldown_hours=int(getattr(settings, "demand_push_cooldown_hours", 48) or 48),
        limit=int(getattr(settings, "demand_push_limit", 10) or 10),
        min_mention=int(getattr(settings, "demand_min_mention", 6) or 6),
        max_age_days=int(getattr(settings, "demand_name_max_age_days", 3) or 0),
        per_event=int(getattr(settings, "demand_name_per_event", 2) or 2),
        dropped=name_stats)
    pushed = 0
    if chosen and push_card(chosen, settings):
        now = datetime.now()
        for r in chosen:
            r.last_pushed_at = now
        db.commit()
        pushed = len(chosen)

    detail = (f"视频{len(videos)} 扫{scanned} 名字{kept_names}(丢{dropped_names}"
              f"/泛词{dropped_common}) 求{no_name}(无名) 推{pushed}"
              f" 求1型{len(request_videos)}(推{request_pushed})"
              + _age_note(age_stats))
    #: ★ **热点素材的条数必须显形**(2026-11-13):40 轮 `视频4 扫4` 看着一切正常,
    #: 而热点那条路**一次都没跑过** —— 只报总数的话,"4 条都来自库内"和
    #: "3 条库内 + 1 条热点"在记录里长得一模一样。长期恒为 0 ⇒ 名额又被吃光了。
    n_hot = sum(1 for v in videos if v.get("src") == "hot")
    if n_hot:
        detail += f" 其中热点{n_hot}"
    #: ★ **凉透的名字也要显形**(2026-11-24):`推0` 有两种可能 —— 名字表里没够格的,
    #: 或者够格的**全都三天没再被人提起了**。不写出来就分不清闸门在拦还是在空转。
    if name_stats.get("name_stale"):
        detail += f" 名字凉{name_stats['name_stale']}"
    #: ★ **事件合并/碎片剔除的条数也要显形**(2026-11-25):不加这两项的话,
    #: 「这一轮只推了 3 个」分不清是**没有新瓜**、还是**同一个瓜的变体占满了名额**。
    if name_stats.get("name_fragment"):
        detail += f" 碎片{name_stats['name_fragment']}"
    if name_stats.get("name_event_cap"):
        detail += f" 同瓜超员{name_stats['name_event_cap']}"
    if errors:
        detail += f" 失败:{errors[0][:60]}"
    _record_run(db, user_id, "demand_watch",
                "success" if not errors else "partial", detail)
    db.commit()
    return {"videos": len(videos), "names": len(agg), "pushed": pushed,
            "no_name": no_name, "request": len(request_videos)}


# ---------------------------------------------------------------------------
# 「最新的大瓜」:顺着参考作品找同款账号,再拿他们**最近发布**的作品
# ---------------------------------------------------------------------------

#: 作者主页作品接口(与评论接口一样**必须带 `a_bogus`**;实测 200 + 17 条,
#: 每条都带 `create_time` 与 `desc`)
POSTS_API = "https://www.douyin.com/aweme/v1/web/aweme/post/"


def fetch_author_posts(sec_uid: str, count: int = 20, cursor: int = 0,
                       session=None, user_id: int = 1) -> dict:
    """拉一个作者主页的作品(最近的在前由调用方排序)。返回 `{ok, items, error}`。"""
    import requests

    from app.services import douyin_protocol_source as dp
    from app.services.douyin_sign import ABogus, BrowserFingerprintGenerator

    blob = dp._cookie(session, user_id)
    if not blob or not sec_uid:
        return {"ok": False, "items": [], "error": "缺少登录态或 sec_uid"}
    ck = dp._cookie_dict(blob)
    params = {"device_platform": "webapp", "sec_user_id": str(sec_uid),
              "max_cursor": str(int(cursor)), "count": str(int(count)), "aid": "6383"}
    query = "&".join(f"{k}={v}" for k, v in params.items())
    fp = BrowserFingerprintGenerator.generate_fingerprint("Chrome")
    signed = ABogus(fp=fp, user_agent=dp._UA).generate_abogus(params=query)[0]
    headers = dp.build_headers(blob, ck, "")
    headers["Referer"] = "https://www.douyin.com/"
    try:
        r = requests.get(f"{POSTS_API}?{signed}", headers=headers, timeout=20)
        d = r.json()
    except Exception as exc:  # noqa: BLE001 - 网络/解析失败按"这号没拿到"处理
        return {"ok": False, "items": [], "error": f"{type(exc).__name__}: {str(exc)[:60]}"}
    if r.status_code != 200 or d.get("status_code") not in (0, None):
        return {"ok": False, "items": [],
                "error": f"HTTP {r.status_code} status={d.get('status_code')}"}
    out = []
    for a in (d.get("aweme_list") or []):
        vid = str(a.get("aweme_id") or "")
        if not vid:
            continue
        author = a.get("author") if isinstance(a.get("author"), dict) else {}
        out.append({"aweme_id": vid,
                    "desc": str(a.get("desc") or "").strip(),
                    "publish_at": int(a.get("create_time") or 0),
                    "author": str(author.get("nickname") or ""),
                    "url": f"https://www.douyin.com/video/{vid}"})
    return {"ok": True, "items": out, "error": ""}


def discover_latest(db, user_id: int = 1, settings=None) -> list[dict]:
    """顺着**参考作品**找同款账号,再拿他们最近 `demand_latest_days` 天的新作品。

    用户口径:「**根据这些去找最新的大瓜**」—— 参考作品给出"这一类"的坐标,
    顺着它们找到在发同一类的号,这些号的**新作品**就是最新的大瓜。
    """
    from datetime import timedelta

    settings = settings or _settings()
    seeds = [w.strip() for w in
             str(getattr(settings, "demand_seed_words", "") or "").split(",") if w.strip()]
    if not seeds:
        return []
    days = int(getattr(settings, "demand_latest_days", 3) or 3)
    since = int((datetime.now() - timedelta(days=max(1, days))).timestamp())
    max_authors = int(getattr(settings, "demand_latest_authors", 3) or 3)
    limit = int(getattr(settings, "demand_latest_limit", 15) or 15)

    from app.services import douyin_protocol_source as dps

    seeds = _rotate_seeds(db, seeds, int(getattr(settings, "demand_seeds_per_round", 3) or 3))
    authors: dict[str, str] = {}
    author_seed: dict[str, str] = {}
    for kw in seeds:
        for v in dps.search([kw], settings=settings, session=db, user_id=user_id, pages=1):
            sec = str(v.get("uid") or "")
            if sec and sec not in authors:
                authors[sec] = str(v.get("name") or "")
                author_seed[sec] = kw
            if len(authors) >= max_authors * len(seeds):
                break

    items: dict[str, dict] = {}
    for sec, name in list(authors.items())[: max_authors * max(1, len(seeds))]:
        res = fetch_author_posts(sec, session=db, user_id=user_id)
        if not res["ok"]:
            logger.debug("拉作者作品失败 %s: %s", name, res["error"])
            continue
        #: ★ **相关性闸门**(2026-10-30 实跑教训):抖音搜索很"松",
        #: 种子 `兔兔图图` 会返回儿童动画号 —— 跟着这些号拉主页,拿到的是
        #: 电影解说/美食之类,与"大瓜"毫无关系。所以要求该号的作品里**至少有一条
        #: 提到这个种子名**,才认为它属于这一类。
        kw = author_seed.get(sec, "")
        related = any(kw and kw in (it.get("desc") or "") for it in res["items"])
        if kw and not related:
            logger.debug("作者 %s 的作品里没有 %s,跳过", name, kw)
            continue
        for it in res["items"]:
            #: ★ **发布时间闸,判不了也不放行**(2026-11-24,与 `source_videos` 同一口径)。
            #: 原写法 `if it["publish_at"] and ...` 是"判不了就放行" —— 主页接口若少给
            #: `create_time`,几个月前的老作品就会被当成"最新的大瓜"推出去(用户投诉的
            #: 就是这张卡)。这里用 `publish_ts_of`(雪花 id 兜底),判不了直接丢。
            ts = publish_ts_of(it["aweme_id"], it["publish_at"])
            if ts <= 0 or ts < since:
                continue
            it["publish_at"] = ts
            it["author"] = it["author"] or name
            items.setdefault(it["aweme_id"], it)
    out = sorted(items.values(), key=lambda x: x["publish_at"], reverse=True)
    return out[:limit]


def build_latest_card(items: list[dict]) -> dict:
    """「最新大瓜」卡:时间 + 作者 + 标题(点标题看原视频)。"""
    lines = []
    for it in items:
        ts = datetime.fromtimestamp(it["publish_at"]) if it.get("publish_at") else None
        when = ts.strftime("%m-%d %H:%M") if ts else "—"
        desc = (it.get("desc") or "").replace("\n", " ")[:52] or "(无描述)"
        lines.append(f"`{when}` **{it.get('author') or '—'}**  [{desc}]({it.get('url')})")
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red",
                   "title": {"tag": "plain_text", "content": f"⏰ 最新大瓜 · 抖音({len(items)})"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "顺着参考作品找到的同款账号的新作品;只列名称与链接,不保存资源"}]},
        ],
    }


def push_latest(items: list[dict], settings) -> bool:
    from app.services.feishu_client import FeishuClient, webhook_for

    #: ★ 2026-10-30 用户口径「先收集数据**发到管理群**,再根据数据改策略」——
    #: 这一整套(需求名/最新/求1型/新名字)都先发**管理群**,不打扰客户群。
    webhook = webhook_for(settings, "admin")
    if not webhook or not items:
        return False
    return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(
        build_latest_card(items))


def build_request_card(items: list[dict], platform: str = "douyin") -> dict:
    """**求1型**大瓜卡:被判为"评论区在求/扣1"的视频 + 能取到的名字。

    ★ **每行带发布时间**(2026-11-24):用户投诉「有的大瓜已经是很久之前的了」——
    卡片上不写时间,客户只能看到「求 12 · 扣1 30」,看不出这条其实是三年前的。
    标记统一走 `hot_time.publish_mark`(与别的板块同一套:`🆕` / `·11-20 14:30`),
    拿不到时间就**不写**,别拿"刚刚"或扫描时刻顶上。
    """
    from app.services.hot_time import publish_mark

    label = "小红书" if platform == "xiaohongshu" else "抖音"
    lines = []
    for it in items:
        raw = (it.get("desc") or "").replace("\n", " ").strip()
        #: ★ **取不到名字时用视频标题顶上**(2026-11-18):原来一律写「(标题里没名字)」,
        #: 一张卡上好几行都是一模一样的占位符 ⇒ 管理群里分不出是哪条视频,
        #: 而**视频标题本来就在 `desc` 里**(实测 `section='douyin'` 的台账 30 行里
        #: 24 行 title 为空,全是因为这条链没把 `desc` 当名字)。
        name = it.get("name") or raw[:28] or "(标题里没名字)"
        #: 只有名字来自 `name` 时才把标题当"补充描述";名字就是标题时别重复写一遍。
        desc = raw[:30] if it.get("name") else ""
        link = f" [{desc}]({it['url']})" if (desc and it.get("url")) else (
            f" [原视频]({it['url']})" if it.get("url") else "")
        #: 迅雷群侧证据(用户「迅雷群里面是否有人发这些资源」):群消息里搜得到就标出来
        xh = int(it.get("xunlei") or 0)
        #: ★ 发布时间标记(2026-11-24):`🆕` = 一小时内,其余 `·MM-DD HH:MM`;
        #: 拿不到时间就是空串(**不写占位符** —— 一列「—」会被读成"这条没有时间"之外的意思)。
        when = publish_mark(it.get("publish_at"))
        lines.append(
            f"**{name}** — 求 {it['demand']} · 扣1 {it['ones']} / 共 {it['total']} 条"
            f" · {it.get('why') or '求/1密度'} · 作者 {it.get('author') or '—'}"
            + (f" · {when}" if when else "")
            + (f" · 🧲群内 {xh} 条" if xh else "") + link)
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red",
                   "title": {"tag": "plain_text",
                             "content": f"🔥 求1型大瓜 · {label}({len(items)})"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "判据:评论区里「求…」与整条「1/扣1」的占比;只记录名称,不保存资源"}]},
        ],
    }


def push_request(items: list[dict], settings, platform: str = "douyin") -> bool:
    from app.services.feishu_client import FeishuClient, webhook_for

    #: ★ 2026-10-30 用户口径「先收集数据**发到管理群**,再根据数据改策略」——
    #: 这一整套(需求名/最新/求1型/新名字)都先发**管理群**,不打扰客户群。
    webhook = webhook_for(settings, "admin")
    if not webhook or not items:
        return False
    return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(
        build_request_card(items, platform))


def _tags_of(title: str) -> list[str]:
    """标题里的标签(按出现顺序,已去通用词)。`best_tag` 与扩展开销都用它。"""
    out: list[str] = []
    for m in re.finditer(r"#\s*([^\s#]{2,16})", str(title or "")):
        n = _clean(m.group(1), strip_tails=False)
        if not is_name_like(n, quoted=True):
            continue
        if n.lower() in _TAG_JUNK or n in _TAG_JUNK:
            continue
        out.append(n)
    return out


def discover_names(db, user_id: int = 1, settings=None) -> list[dict]:
    """**顺着参考作品扩展名字**(2026-10-30 用户:「根据这些来找方法去**发现新的大瓜**」)。

    方法:参考作品 → 搜到的**同款作者** → 拉他们最近的 30 条作品 → 从标题的**标签**里
    统计哪些名字**反复出现**(≥ `demand_expand_min` 条不同作品) → 这些就是"这一类"的
    名字候选;排除:参考作品本身、作者名、通用词。

    ⚠️ 为什么用"**跨作品反复出现**"而不是"出现一次就算":实测标签里绝大多数是描述词
    (`#覆面系`/`#高壮男`),而**真正的名字**(如 `#小小绘`/`#范小勤`)会在他**多条**作品里重复。
    """
    settings = settings or _settings()
    seeds = [w.strip() for w in
             str(getattr(settings, "demand_seed_words", "") or "").split(",") if w.strip()]
    if not seeds:
        return []
    from app.services import douyin_protocol_source as dps

    min_hits = int(getattr(settings, "demand_expand_min", 2) or 2)
    max_authors = int(getattr(settings, "demand_latest_authors", 3) or 3)

    authors: dict[str, str] = {}
    for kw in _rotate_seeds(db, seeds, int(getattr(settings, "demand_seeds_per_round", 3) or 3)):
        for v in dps.search([kw], settings=settings, session=db, user_id=user_id, pages=1):
            sec = str(v.get("uid") or "")
            if sec:
                authors.setdefault(sec, str(v.get("name") or ""))
    stats: dict[str, dict] = {}
    for sec, author in list(authors.items())[: max_authors * max(1, len(seeds))]:
        res = fetch_author_posts(sec, session=db, user_id=user_id, count=30)
        if not res["ok"]:
            continue
        for it in res["items"]:
            names = _tags_of(it["desc"]) + names_from_title(it["desc"])
            for n in names:
                if n in seeds or n == author:
                    continue
                rec = stats.setdefault(n, {"hits": 0, "author": author, "url": it["url"]})
                rec["hits"] += 1
    out = [{"name": n, **rec} for n, rec in stats.items() if rec["hits"] >= min_hits]
    out.sort(key=lambda x: x["hits"], reverse=True)
    return out[: int(getattr(settings, "demand_expand_limit", 10) or 10)]


def build_expand_card(items: list[dict]) -> dict:
    lines = [f"**{i}. {it['name']}** — 出现在 {it['hits']} 条作品里(作者 {it['author']})"
             for i, it in enumerate(items, 1)]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "turquoise",
                   "title": {"tag": "plain_text",
                             "content": f"🆕 顺着参考作品发现的新名字({len(items)})"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "判据:同款作者的多条作品标题里反复出现;只记名称,不保存资源"}]},
        ],
    }


def push_expand(items: list[dict], settings) -> bool:
    from app.services.feishu_client import FeishuClient, webhook_for

    #: ★ 2026-10-30 用户口径「先收集数据**发到管理群**,再根据数据改策略」——
    #: 这一整套(需求名/最新/求1型/新名字)都先发**管理群**,不打扰客户群。
    webhook = webhook_for(settings, "admin")
    if not webhook or not items:
        return False
    return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(
        build_expand_card(items))


#: 纯社交群/招募帖的标志词。**命中就判非主题,哪怕标题里带「瓜」「八卦」**
#: (2026-11-25 实测:原来的"主题词优先"放过了 25/59 条群招募帖)。
#: ⚠️ `裙` 必须单列(2026-11-08 实测):小红书为了避审核把「群」写成「裙」,
#: `完全女儿国来的,裙里女生超多` 因此**漏过了社交词判定**、被判成主题相关。
#: `搭子/扩列/互赞` 同理,是"找搭子"帖的高频词,与大瓜/资源无关。
#: ★ 2026-11-25 补的**招募语**(逐条看过实测标题才加,不是拍脑袋):
#: `互助`(互帮互助啦/正能量互帮互助/车队互助)、`招募`(招募：互帮互助,一起成长)、
#: `仅限`(仅限姐妹!)、`兼职`/`找工作`(大学副业兼职,找工作)、`大队`(又想进吃🍉大队的么
#: —— 它用「大队」回避了「群」)。
#: ⚠️ **故意不放 `加入`**:实测真瓜标题也会写「欢迎所有爱吃瓜的小伙伴加入✨」,
#: 加了会误伤;判据宁可漏一条,别把真内容挡掉。
_SOCIAL_WORDS = ("群", "裙", "扩列", "互赞", "涨粉", "聊天", "交友", "招聘", "学习", "红包",
                 "搭子", "互关", "粉丝", "直播间",
                 "互助", "招募", "仅限", "兼职", "找工作", "大队")


def topic_hit(title: str) -> bool:
    """这条是不是**主题相关**(大瓜/资源),而不是纯社交群。

    ★★ **社交词优先**(2026-11-25)。原写法是"主题词优先" —— 标题里只要有「瓜」
    就直接返回 True,**社交词根本轮不到判**。实测近 30 天 `topic_ok=1` 且 flagged
    且有名字的 59 条里,**25 条是群招募帖**:`八卦群🍉群公告`/`谁懂有个女生八卦群的
    快乐`/`像这种的吃瓜群谁要？`/`所有人,瓜主亲友还是吃瓜人,来进群吧` ——
    它们全都含「瓜」或「八卦」,**全都漏过了这道闸**。

    顺序就是判据:「想拉人进群」比「在聊瓜」更能说明这条帖的性质 ——
    一个大瓜帖不会写「群公告」「来进群吧」。反过来,社交词命中时即便有「瓜」也挡掉。
    """
    t = str(title or "")
    if any(w in t for w in _SOCIAL_WORDS):
        return False
    #: 没有社交词 ⇒ **算相关**(宁可放过,别把 `护肤教程（毛孔篇）纯享版教程`
    #: 这种既不带瓜也不带群的真内容挡掉)。
    #: ⚠️ 原「主题词」那张表(`_TOPIC_WORDS`)已删:社交词优先之后它**不再是判据**,
    #: 只用来「有瓜就放行」,而那正是漏掉 25/59 条招募帖的那条支路。
    #: 真正兜底的是下游的「求/1 密度」和「重复率」,不是标题里有没有「瓜」。
    return True


def _record_scan(db, user_id: int, v: dict, *, total: int, ones: int, demand: int,
                 dup: float, flagged: bool, why: str, name: str,
                 platform: str = "douyin", has_group: bool = False) -> None:
    """把这一条视频的扫描结果落库(同一天同一条只留一行,以最新一次为准)。"""
    from app.db.models import DemandScan

    today = datetime.now().strftime("%Y-%m-%d")
    vid = str(v.get("aweme_id") or "")
    if not vid:
        return
    row = db.scalar(select(DemandScan).where(
        DemandScan.user_id == user_id, DemandScan.aweme_id == vid,
        DemandScan.scanned_date == today))
    if row is None:
        row = DemandScan(user_id=user_id, aweme_id=vid, scanned_date=today)
        db.add(row)
    row.platform = platform
    row.author = str(v.get("author") or "")[:64]
    row.title = str(v.get("title") or "")[:255]
    row.url = str(v.get("url") or "")[:500]
    row.total, row.ones, row.demand = int(total), int(ones), int(demand)
    row.dup_ratio = float(dup or 0)
    row.flagged, row.why, row.name = bool(flagged), str(why or "")[:32], str(name or "")[:128]
    row.has_group = bool(has_group)
    row.topic_ok = topic_hit(v.get("title") or "")
    #: ★ **这条素材的发布时间**(2026-11-24):让"被扫的视频有多老"在库里可查、可回放标定。
    #: 取不到就留 `None`(**不拿 `scanned_at` 顶** —— 那是假新鲜度,见字段注释)。
    _ts = publish_ts_of(vid, v.get("publish_at"))
    row.publish_at = datetime.fromtimestamp(_ts) if _ts > 0 else None
    row.scanned_at = datetime.now()


def scan_xhs(db, user_id: int = 1, settings=None) -> dict:
    """**小红书**求1型扫描(2026-10-30,用户「小红书上的热点大瓜应该更好监控」)。

    种子词仍用抖音那套参考作品名;判据与抖音**同一套**(`xhs_demand.stats_from_texts`
    直接复用本模块的归一化与匹配)。频率刻意压得很低:每轮 ≤2 个词、每词 ≤2 篇笔记。
    """
    from app.services import xhs_demand as X

    settings = settings or _settings()
    if not bool(getattr(settings, "demand_xhs_enabled", True)):
        return {"skipped": "disabled"}
    ok, why = X.available(settings)
    if not ok:
        #: CDP 不可达 = 浏览器没开/没登录。**不自己 launch** —— 脚本拉起的浏览器
        #: `navigator.webdriver=True`,那条路就废了(见 `tools/xhs_cdp.py` 头注)。
        return {"skipped": why}

    #: ★ 小红书**用自己的种子词**(2026-10-30 实测):抖音那套参考作品名在小红书
    #: 命中率极低(评论 26~35 条、求只有 0~1);而 `吃瓜群` 一轮就命中 2/2 篇
    #: (两篇都挂着群聊入口)。留空则回落到通用 `DEMAND_SEED_WORDS`。
    xhs_seeds = [w.strip() for w in
                 str(getattr(settings, "demand_xhs_seed_words", "") or "").split(",")
                 if w.strip()]
    seeds = xhs_seeds or [w.strip() for w in
                          str(getattr(settings, "demand_seed_words", "") or "").split(",")
                          if w.strip()]
    if not seeds:
        return {"skipped": "no_seed_words"}
    seeds = _rotate_seeds(db, seeds,
                          int(getattr(settings, "demand_xhs_seeds_per_round", 2) or 2))
    per_seed = int(getattr(settings, "demand_xhs_notes_per_seed", 2) or 2)

    items: list[dict] = []
    scanned = flagged_n = skipped_n = 0
    #: ★ 发布时间闸的计数(2026-11-24):`stale` = 太老被拦,`unknown` = 判不了时间。
    #: ⚠️ 小红书这条链**此前连发布时间都没取**,取不到的比例可能很高 —— 拦下的条数
    #: 必须进运行记录,否则「全被闸门挡了」和「没有命中」在库里长得一样。
    age_stale = age_unknown = 0
    max_age = int(getattr(settings, "demand_xhs_max_age_days", 3) or 0)
    min_total = int(getattr(settings, "demand_xhs_min_comments", 8) or 0)
    p = browser = page = None
    try:
        p, browser = X._connect()
        page = browser.contexts[0].new_page()
        for kw in seeds:
            for idx, note in enumerate(X.search_notes(page, kw, limit=per_seed)):
                #: ★★ **发布时间闸**(2026-11-24,用户「有的大瓜已经是很久之前的了」)。
                #: 位置在**开页之前**:打开笔记要滚 3 次、发 3 次懒加载请求,是最贵的一步 ——
                #: 三年前的老帖没必要为它花掉风控额度(这条链被 461/300011 罚过,见记忆
                #: `xhs-461-solved-by-page-render`)。取不到时间按"判不了"处理并计数:
                #: 这一步**不拦**,只记 —— 拦了会把整条链堵死(搜索结果字段是猜的,
                #: 得先看到「无时间N」的真实比例再决定要不要拦)。
                _ts = int(note.get("published") or 0)
                if max_age > 0 and _ts > 0 and _age_days(_ts) > float(max_age):
                    age_stale += 1
                    continue
                if _ts <= 0:
                    age_unknown += 1
                #: 搜索结果自带 `commentCount` ⇒ **评论太少就别开页**(省额度也省风控)。
                #: ⚠️ 但每个词**第一篇照开**:群聊型的帖子常常评论很少(用户:「小红书大多都会
                #: 有加入群聊」),而"挂没挂群"只有打开才知道。
                if idx > 0 and int(note.get("comments") or 0) < max(1, min_total):
                    continue
                texts = X.note_comments(page, note["id"], note.get("token", ""))
                has_group = X.page_has_group(page)
                st = X.stats_from_texts(texts)
                if not st["total"] and not has_group:
                    continue
                scanned += 1
                tnames = names_from_title(note.get("title") or "")
                note_name = (tnames[0] if tnames else
                             (best_tag(note.get("title") or "",
                                       exclude=note.get("author") or "")
                              or match_seed(note.get("title") or "", seeds)))
                #: ⚠️ 小红书单篇评论常常只有几条(用户给的「小马云」那条**只有 4 条**),
                #: 样本太小时密度是噪音 ⇒ 少于 `DEMAND_XHS_MIN_COMMENTS` 只记不判。
                if st["total"] < max(1, min_total):
                    _record_scan(
                        db, user_id,
                        {"aweme_id": note["id"], "title": note.get("title") or "",
                         "author": note.get("author") or "",
                         "url": f"https://www.xiaohongshu.com/explore/{note['id']}",
                         "publish_at": _ts},
                        total=st["total"], ones=st["ones"], demand=st["demand"],
                        dup=st["dup_ratio"], flagged=False,
                        why=("群聊入口" if has_group else "样本不足"),
                        name=note_name, platform="xiaohongshu", has_group=has_group)
                    continue
                ratio = (st["ones"] + st["demand"]) / st["total"]
                by_density = (ratio >= float(getattr(settings, "demand_request_density", 0.1) or 0)
                              and (st["ones"] + st["demand"]) >= 3)
                by_repeat = (st["dup_ratio"] >= float(
                    getattr(settings, "demand_repeat_ratio", 0.25) or 0)
                    and st["total"] >= 10)
                flagged = bool(by_density or by_repeat)
                name = note_name
                #: ★★ **主题闸门**(2026-11-08):`topic_ok=0` 的 19 条里,
                #: `互帮互助群`/`摸鱼群招人啦`/`90.00后05后宝妈孕妈群`/`正常人聊天群` 全是
                #: **社交招募帖** —— 求/1 密度一样高(拉群也让人扣1),但**不是大瓜**。
                #: 此前只"记录不拦",结果每轮都在推这些,真大瓜被淹掉。
                #: 现在**记录照旧**(密度数据要留着调阈值),**但不再推送**。
                topic_ok = topic_hit(note.get("title") or "")
                _record_scan(
                    db, user_id,
                    {"aweme_id": note["id"], "title": note.get("title") or "",
                     "author": note.get("author") or "",
                     "url": f"https://www.xiaohongshu.com/explore/{note['id']}",
                     "publish_at": _ts},
                    total=st["total"], ones=st["ones"], demand=st["demand"],
                    dup=st["dup_ratio"], flagged=flagged,
                    why=("求/1密度" if by_density else ("重复评论" if by_repeat else "")),
                    name=name, platform="xiaohongshu", has_group=has_group)
                if flagged:
                    flagged_n += 1
                    if not topic_ok or not name:
                        #: 非主题(社交群)或**连名字都取不到** ⇒ 只入表不推。
                        #: 抖音那条链取不到名字**仍然推**(用户要的是"这类视频"本身),
                        #: 小红书不同:它的评论区信号本来就弱一个量级(`doc` 9.5),
                        #: 再推"没名字"的卡片就纯是噪音。
                        skipped_n += 1
                        continue
                    items.append({"name": name, "desc": note.get("title") or "",
                                  "author": note.get("author") or "",
                                  "url": f"https://www.xiaohongshu.com/explore/{note['id']}",
                                  "why": "求/1密度" if by_density else "重复评论",
                                  "demand": st["demand"], "ones": st["ones"],
                                  "total": st["total"], "publish_at": _ts})
    except Exception as exc:  # noqa: BLE001 - 浏览器/页面异常不该把整轮带崩
        logger.exception("小红书扫描失败")
        return {"error": f"{type(exc).__name__}: {str(exc)[:80]}", "scanned": scanned}
    finally:
        try:
            if page is not None:
                page.close()
        finally:
            try:
                if browser is not None:
                    browser.close()
            finally:
                if p is not None:
                    p.stop()

    db.commit()
    pushed = len(items) if items and push_request(items, settings, "xiaohongshu") else 0
    #: ⚠️ 明细里必须带上 `拦N` —— 否则"扫10 命中3 推0"和"扫10 命中0 推0"
    #: 在库里长得一样,看不出是**闸门拦掉了**还是**根本没命中**。
    age_note = ""
    if age_stale:
        age_note += f" 老{age_stale}"
    if age_unknown:
        age_note += f" 无时间{age_unknown}"
    _record_run_quiet(db, user_id,
                      f"小红书 扫{scanned} 命中{flagged_n} 拦{skipped_n} 推{pushed}{age_note}")
    return {"scanned": scanned, "flagged": flagged_n, "pushed": pushed,
            "skipped": skipped_n, "age_stale": age_stale, "age_unknown": age_unknown}


def _record_run_quiet(db, user_id: int, detail: str) -> None:
    """写一行运行记录(小红书那条链自己的可观测性)。"""
    from app.services.tenant_base import _record_run

    try:
        _record_run(db, user_id, "demand_watch_xhs", "success", detail)
        db.commit()
    except Exception:  # noqa: BLE001 - 记录失败不影响结果
        db.rollback()


def digest_stats(db, user_id: int = 1, hours: int = 24) -> dict:
    """近 N 小时的**分母数据**:扫了多少、命中多少、平均密度、TOP 名字/作者。"""
    from collections import Counter
    from datetime import timedelta

    from app.db.models import DemandName, DemandScan

    since = datetime.now() - timedelta(hours=max(1, int(hours)))
    rows = db.scalars(select(DemandScan).where(
        DemandScan.user_id == user_id, DemandScan.scanned_at >= since)).all()
    n = len(rows)
    flagged = [r for r in rows if r.flagged]
    authors = Counter(r.author for r in flagged if r.author)
    #: 分平台看:抖音/小红的量级差很多,混成一个命中率就看不出来了。
    by_platform: dict[str, dict] = {}
    for p in ("douyin", "xiaohongshu"):
        rs = [r for r in rows if (getattr(r, "platform", "") or "douyin") == p]
        if rs:
            by_platform[p] = {"scanned": len(rs),
                              "flagged": sum(1 for r in rs if r.flagged),
                              "group": sum(1 for r in rs if getattr(r, "has_group", False)),
                              #: 命中里**主题相关**的条数 —— 用来回答"命中率高是不是因为
                              #: 混进了一堆交流群/扩列群"(2026-10-30 数据里 6/14 就是这类)。
                              "topic": sum(1 for r in rs if r.flagged
                                           and getattr(r, "topic_ok", True))}
    names = db.scalars(select(DemandName).where(
        DemandName.user_id == user_id,
        DemandName.last_seen_at >= since).order_by(
        (DemandName.demand_hits + DemandName.ones_hits).desc()).limit(5)).all()
    return {
        "hours": int(hours), "scanned": n, "flagged": len(flagged),
        "rate": (len(flagged) / n) if n else 0.0,
        "avg_density": (sum((r.ones + r.demand) / r.total for r in rows if r.total) / n)
        if n else 0.0,
        "avg_dup": (sum(float(r.dup_ratio or 0) for r in rows) / n) if n else 0.0,
        "authors": authors.most_common(3),
        "by_platform": by_platform,
        "names": [(r.name, int(r.demand_hits or 0), int(getattr(r, "ones_hits", 0) or 0))
                  for r in names],
    }


def build_digest_card(st: dict) -> dict:
    lines = [
        f"扫描 **{st['scanned']}** 条 · 判为求1型 **{st['flagged']}** 条"
        f"({st['rate']:.0%})",
        f"平均求/1密度 **{st['avg_density']:.0%}** · 平均重复率 **{st['avg_dup']:.0%}**",
    ]
    if st["authors"]:
        lines.append("命中最多作者:" + " · ".join(f"{a} {n}" for a, n in st["authors"]))
    bp = st.get("by_platform") or {}
    if bp:
        label = {"douyin": "抖音", "xiaohongshu": "小红书"}
        lines.append("分平台:" + " · ".join(
            f"{label.get(k, k)} 扫{v['scanned']}/命中{v['flagged']}"
            + (f"(主题{v['topic']})" if v.get("topic") is not None else "")
            + (f"/有群{v['group']}" if v.get("group") else "")
            for k, v in bp.items()))
    if st["names"]:
        lines.append("名字 TOP:" + " · ".join(f"{n}(求{d}/1{o})" for n, d, o in st["names"]))
    else:
        lines.append("近 {h} 小时没有新增名字".format(h=st["hours"]))
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text",
                             "content": f"📊 求1型监控数据 · 近{st['hours']}小时"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "这是**分母**:扫描条数、命中条数、平均密度;策略就按这张表调"}]},
        ],
    }


def push_digest(st: dict, settings) -> bool:
    from app.services.feishu_client import FeishuClient, webhook_for

    webhook = webhook_for(settings, "admin")
    if not webhook:
        return False
    return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(
        build_digest_card(st))


def demand_digest_tick(settings=None) -> int:
    """每天一次:把近 24 小时的数据发管理群。返回推了几张。"""
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or _settings()
    if not bool(getattr(settings, "demand_digest_enabled", True)):
        return 0
    db = get_session_local()()
    n = 0
    try:
        for uid in db.scalars(select(User.id)).all():
            try:
                st = digest_stats(db, int(uid),
                                  int(getattr(settings, "demand_digest_hours", 24) or 24))
                if push_digest(st, settings):
                    n += 1
            except Exception:  # noqa: BLE001 - 一个用户失败不影响其他
                logger.exception("求1型数据汇总失败 user=%s", uid)
                db.rollback()
        return n
    finally:
        db.close()


def _settings():
    from config.settings import get_settings

    return get_settings()


def demand_watch_tick(settings=None) -> int:
    """计划任务入口:每个用户跑一轮。返回推出去的卡片数。"""
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or _settings()
    db = get_session_local()()
    n = 0
    try:
        for uid in db.scalars(select(User.id)).all():
            try:
                out = run_user(db, int(uid), settings)
                n += 1 if out.get("pushed") else 0
                # ★ 「最新的大瓜」:顺参考作品找同款账号 → 他们最近的作品。
                # ⚠️ 过 `push_ledger` 去重:4 小时一轮,同一批新作品不该每轮都推一遍。
                if bool(getattr(settings, "demand_latest_enabled", True)):
                    from app.services.push_ledger import drop_recent, record

                    items = discover_latest(db, int(uid), settings)
                    kept, _dropped = drop_recent(db, int(uid), "douyin", items, settings)
                    if kept and push_latest(kept, settings):
                        record(db, int(uid), "douyin", kept)
                        n += 1
                # ★ 顺着参考作品**发现新名字**(用户「根据这些来找方法去发现新的大瓜」)。
                # 判据是"同款作者的多条作品里反复出现",一次出现不算。
                if bool(getattr(settings, "demand_expand_enabled", True)):
                    names = discover_names(db, int(uid), settings)
                    if names and push_expand(names, settings):
                        n += 1
                # ★ **小红书**那条链(CDP 页面路,需要人开着那个浏览器)。
                # 不可达时 `scan_xhs` 自己返回 skip,不会把整轮带崩。
                if bool(getattr(settings, "demand_xhs_enabled", True)):
                    scan_xhs(db, int(uid), settings)
            except Exception:  # noqa: BLE001 - 一个用户失败不影响其他用户
                logger.exception("需求名监控失败 user=%s", uid)
                db.rollback()
        return n
    finally:
        db.close()
