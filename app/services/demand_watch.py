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

from sqlalchemy import select

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
                      pages: int = 2, count: int = 20) -> dict:
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
    return {"names": dict(found), "error": err, "no_name": no_name, "ones": ones,
            "total": total, "dup_ratio": round(dup_ratio, 3)}


# ---------------------------------------------------------------------------
# 聚合 / 入库 / 推送
# ---------------------------------------------------------------------------


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


def pick_pushable(rows, *, min_demand: int = 2, min_ones: int = 20,
                  cooldown_hours: int = 48, limit: int = 10) -> list:
    """够格且**冷却期外**的名字。按 (被求次数, 扣1数) 降序。

    ★ 两条门槛满足**任意一条**就算够格(用户口径「判断是根据作品里面的**求和1**」):
      · 有人**求**(`demand_hits ≥ min_demand`);
      · 或者**扣1跟帖够多**(`ones_hits ≥ min_ones`)—— 它说明这条被勾得很紧。

    ⚠️ **加冷却**:本作业 4 小时一轮,不加冷却的话同一个名字一天会推 6 次 ——
    群里会把它当成噪音(`push_ledger` 那套冷却的同一个道理)。
    """
    now = datetime.now()
    out = []
    for r in rows:
        demand = int(r.demand_hits or 0)
        ones = int(getattr(r, "ones_hits", 0) or 0)
        if demand < int(min_demand) and ones < int(min_ones):
            continue
        if r.last_pushed_at and (now - r.last_pushed_at) < timedelta(hours=cooldown_hours):
            continue
        out.append(r)
    out.sort(key=lambda r: (int(r.demand_hits or 0), int(getattr(r, "ones_hits", 0) or 0),
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


def source_videos(db, user_id: int = 1, settings=None) -> list[dict]:
    """挑要扫评论的视频。

    ## 优先级(2026-10-30 定)

    ① **用户给的参考作品名**(`DEMAND_SEED_WORDS`)。用户口径:「我上个对话给你的
       就是参考作品」—— 那几个名字**就是这一类**,按名字搜到的视频才是同款
       (实测 `狗爹和小桃` 搜到 13 条同款,而泛词 `吃瓜` 搜到的是亲子起名/外卖群)。
    ② 没配种子词时,用**库内素材**:`douyin_leads`(推广视频)与 `douyin_peer_works`(对标号)。
    ③ 两种都能补位 —— 谁先用完名额就停。

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

    out: list[dict] = []
    seen: set[str] = set()

    def _add(vid: str, url: str, title: str, comments: int = 0, author: str = "") -> None:
        vid = str(vid or "").strip()
        if not vid or vid in seen:
            return
        seen.add(vid)
        out.append({"aweme_id": vid, "url": url or f"https://www.douyin.com/video/{vid}",
                    "title": title or "", "comment_count": int(comments or 0),
                    "author": str(author or "")})

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
                 (v.get("metrics") or {}).get("comment_count") or 0, v.get("name"))
            #: 参考作品只占**一半名额**:它们通常没有书名号(标题是"标签流"),
            #: 名字还得靠库内推广线索那半;全被搜索占满 ⇒ 这一轮一个名字都抽不到。
            if len(out) >= max(1, need // 2):
                break

    # ★ **同款作者的新作品**也要进候选池(2026-10-30 用户:「根据这些来找方法去**发现新的大瓜**」)。
    # 参考作品 → 作者 → 他们的近期作品,这些才是"**最新的**求1型视频"的来源;
    # 只看我们自己的线索池永远发现不了新号。
    if len(out) < need:
        from datetime import timedelta

        since_ts = int((datetime.now() - timedelta(
            days=int(getattr(settings, "demand_latest_days", 3) or 3))).timestamp())
        for sec, name in list(authors.items()):
            if len(out) >= need:
                break
            res = fetch_author_posts(sec, session=db, user_id=user_id, count=20)
            if not res["ok"]:
                continue
            for it in res["items"]:
                if it["publish_at"] and it["publish_at"] < since_ts:
                    continue
                _add(it["aweme_id"], it["url"], it["desc"], 0, it["author"] or name)
                if len(out) >= need:
                    break

    # ② 我们自己的线索(推广视频,评论区天然是「求资源」的)
    leads = db.scalars(select(DouyinLead).where(
        DouyinLead.user_id == user_id,
        DouyinLead.found_at >= since).order_by(DouyinLead.found_at.desc())).all()
    for r in leads:
        _add(r.aweme_id, r.url, r.title, 0, r.author)
        if len(out) >= need:
            return out[:need]

    # ③ 对标号作品(带网盘的那些)
    works = db.scalars(select(DouyinPeerWork).where(
        DouyinPeerWork.user_id == user_id,
        DouyinPeerWork.first_seen_at >= since).order_by(
        DouyinPeerWork.first_seen_at.desc())).all()
    for w in works:
        _add(w.aweme_id, w.url, w.title, 0, w.account_name)
        if len(out) >= need:
            break

    return out[:need]


def run_user(db, user_id: int = 1, settings=None) -> dict:
    """一轮:挑视频 → 拉评论 → 抽名字 → 入库 → 推卡(只存名字,不落网盘)。"""
    from app.db.models import DemandName
    from app.services.tenant_base import _record_run

    settings = settings or _settings()
    if not bool(getattr(settings, "demand_watch_enabled", True)):
        return {"skipped": "disabled"}

    videos = source_videos(db, user_id, settings)

    agg: dict[str, dict] = {}
    errors: list[str] = []
    scanned = 0
    no_name = 0
    request_videos: list[dict] = []
    for v in videos:
        res = collect_for_video(str(v.get("aweme_id") or ""), session=db, user_id=user_id,
                                pages=int(getattr(settings, "demand_comment_pages", 2) or 2))
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
                    "name": vname,
                    "desc": v.get("title") or "",
                    "author": v.get("author") or "",
                    "url": url,
                    "why": "求/1密度" if by_density else "重复评论",
                    "demand": demand_cnt, "ones": ones, "total": total})
        # ★ **名字主要来自作品标题**(用户:「基本上作品名都有资源名」);
        # 而「扣1」的热度是**整条视频**级别的,归给这条视频标题里的名字。
        for tname in names_from_title(v.get("title") or ""):
            a = agg.setdefault(tname, {"demand": 0, "mention": 0, "video_hits": 0,
                                       "ones": 0, "title": 0, "heat": 0,
                                       "comments": [], "video": ""})
            a["title"] += 1
            a["mention"] += 1
            a["ones"] += ones
            a["heat"] += heat
            if not a["video"]:
                a["video"] = url
        for name, rec in res["names"].items():
            a = agg.setdefault(name, {"demand": 0, "mention": 0, "video_hits": 0,
                                      "ones": 0, "title": 0, "heat": 0,
                                      "comments": [], "video": ""})
            a["demand"] += rec["demand"]
            a["mention"] += rec["mention"]
            a["video_hits"] += 1
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
                    + (f" 失败:{errors[0][:60]}" if errors else ""))
        db.commit()
        return {"videos": len(videos), "names": 0, "request": len(request_videos)}

    for name, a in agg.items():
        _upsert(db, user_id, name, a["demand"], a["mention"], a["video_hits"],
                {"comments": a["comments"], "video": a["video"]},
                ones=a["ones"], title=a["title"], heat=a["heat"])
    db.commit()

    rows = db.scalars(select(DemandName).where(
        DemandName.user_id == user_id,
        DemandName.platform == PLATFORM)).all()
    chosen = pick_pushable(
        rows,
        min_demand=int(getattr(settings, "demand_min_hits", 2) or 2),
        min_ones=int(getattr(settings, "demand_min_ones", 20) or 0),
        cooldown_hours=int(getattr(settings, "demand_push_cooldown_hours", 48) or 48),
        limit=int(getattr(settings, "demand_push_limit", 10) or 10))
    pushed = 0
    if chosen and push_card(chosen, settings):
        now = datetime.now()
        for r in chosen:
            r.last_pushed_at = now
        db.commit()
        pushed = len(chosen)

    detail = (f"视频{len(videos)} 扫{scanned} 名字{len(agg)} 求{no_name}(无名) 推{pushed}"
              f" 求1型{len(request_videos)}(推{request_pushed})")
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
            if it["publish_at"] and it["publish_at"] < since:
                continue
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
    """**求1型**大瓜卡:被判为"评论区在求/扣1"的视频 + 能取到的名字。"""
    label = "小红书" if platform == "xiaohongshu" else "抖音"
    lines = []
    for it in items:
        name = it.get("name") or "(标题里没名字)"
        desc = (it.get("desc") or "")[:30]
        link = f" [{desc}]({it['url']})" if it.get("url") else ""
        lines.append(
            f"**{name}** — 求 {it['demand']} · 扣1 {it['ones']} / 共 {it['total']} 条"
            f" · {it.get('why') or '求/1密度'} · 作者 {it.get('author') or '—'}" + link)
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


def _record_scan(db, user_id: int, v: dict, *, total: int, ones: int, demand: int,
                 dup: float, flagged: bool, why: str, name: str,
                 platform: str = "douyin") -> None:
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

    seeds = [w.strip() for w in
             str(getattr(settings, "demand_seed_words", "") or "").split(",") if w.strip()]
    if not seeds:
        return {"skipped": "no_seed_words"}
    seeds = _rotate_seeds(db, seeds,
                          int(getattr(settings, "demand_xhs_seeds_per_round", 2) or 2))
    per_seed = int(getattr(settings, "demand_xhs_notes_per_seed", 2) or 2)

    items: list[dict] = []
    scanned = flagged_n = 0
    p = browser = page = None
    try:
        p, browser = X._connect()
        page = browser.contexts[0].new_page()
        for kw in seeds:
            for note in X.search_notes(page, kw, limit=per_seed):
                texts = X.note_comments(page, note["id"], note.get("token", ""))
                st = X.stats_from_texts(texts)
                if not st["total"]:
                    continue
                scanned += 1
                #: ⚠️ 小红书单篇评论常常只有几条(用户给的「小马云」那条**只有 4 条**),
                #: 样本太小时密度是噪音 ⇒ 少于 `DEMAND_XHS_MIN_COMMENTS` 只记不判。
                min_total = int(getattr(settings, "demand_xhs_min_comments", 8) or 0)
                if st["total"] < max(1, min_total):
                    _record_scan(
                        db, user_id,
                        {"aweme_id": note["id"], "title": note.get("title") or "",
                         "author": note.get("author") or "",
                         "url": f"https://www.xiaohongshu.com/explore/{note['id']}"},
                        total=st["total"], ones=st["ones"], demand=st["demand"],
                        dup=st["dup_ratio"], flagged=False, why="样本不足",
                        name="", platform="xiaohongshu")
                    continue
                ratio = (st["ones"] + st["demand"]) / st["total"]
                by_density = (ratio >= float(getattr(settings, "demand_request_density", 0.1) or 0)
                              and (st["ones"] + st["demand"]) >= 3)
                by_repeat = (st["dup_ratio"] >= float(
                    getattr(settings, "demand_repeat_ratio", 0.25) or 0)
                    and st["total"] >= 10)
                flagged = bool(by_density or by_repeat)
                name = names_from_title(note.get("title") or "")
                name = name[0] if name else (best_tag(note.get("title") or "",
                                                      exclude=note.get("author") or "")
                                             or match_seed(note.get("title") or "", seeds))
                _record_scan(
                    db, user_id,
                    {"aweme_id": note["id"], "title": note.get("title") or "",
                     "author": note.get("author") or "",
                     "url": f"https://www.xiaohongshu.com/explore/{note['id']}"},
                    total=st["total"], ones=st["ones"], demand=st["demand"],
                    dup=st["dup_ratio"], flagged=flagged,
                    why=("求/1密度" if by_density else ("重复评论" if by_repeat else "")),
                    name=name, platform="xiaohongshu")
                if flagged:
                    flagged_n += 1
                    items.append({"name": name, "desc": note.get("title") or "",
                                  "author": note.get("author") or "",
                                  "url": f"https://www.xiaohongshu.com/explore/{note['id']}",
                                  "why": "求/1密度" if by_density else "重复评论",
                                  "demand": st["demand"], "ones": st["ones"],
                                  "total": st["total"]})
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
    _record_run_quiet(db, user_id, f"小红书 扫{scanned} 命中{flagged_n} 推{pushed}")
    return {"scanned": scanned, "flagged": flagged_n, "pushed": pushed}


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
                              "flagged": sum(1 for r in rs if r.flagged)}
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
            f"{label.get(k, k)} 扫{v['scanned']}/命中{v['flagged']}" for k, v in bp.items()))
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
