"""公众号内容选题分析智能体(暂不含流量数据,见 doc/dev.md §5.x)。

输入一批公众号文章(title/content/author/publish_at),输出:
- 选题分布(主题词频,聚类出该号/对标号近期写什么)
- 标题风格(长度/含数字/emoji/疑问/悬念吸引词占比、高频词)
- 发布时间分布(按时段/小时,给出建议发文时段)
- 作者(公众号)对比(篇数/选题/标题风格差异)
- 选题建议(基于内容特征的规则建议,非流量归因)

注意:目前**无流量数据**(阅读量/点赞),故"如何更有流量"只能给**内容选题视角**的规则建议;
等接入带阅读量的第三方 API 后,可把本模块升级为"内容特征 → 流量"的归因版。

对外接口:`analyze_articles(articles: list[dict]) -> dict`。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

# 悬念/吸引词(标题里常见拉流量的句式词)
_HOOK_WORDS = (
    "揭秘", "速看", "震惊", "必看", "终于", "竟然", "居然", "太", "干货", "教程",
    "清单", "避开", "后悔", "千万别", "免费", "限时", "隐藏", "内幕", "测评", "亲测",
    "避雷", "公式", "模板", "原来", "方法", "技巧", "秘诀", "几步", "一看就会", "涨知识",
)
# 高频虚词/停用词(做主题词频时剔除)
_STOPWORDS = set(
    "的了是在我有你就都而及等与之和也还只一个不用没很这那对下上中为着过被把让从向于到"
    "就都也还会要能可以怎么什么为什么如何哪些哪个这里那里大家自己我们你们他们"
)
_TITLE_SEP = re.compile(r"[，。！？、；：（）()「」【】\[\]\/\-\s]+")
_NUM_RE = re.compile(r"\d")
_EMOJI_RE = re.compile(r"[☀-➿\U0001F300-\U0001FAFF⬀-⯿️←-⇿]")
_QUESTION_RE = re.compile(r"[?？]")
_CJK_RE = re.compile(r"[一-鿿]")
_CJK_RUN = re.compile(r"[一-鿿]+")


def _parse_time(value: object) -> datetime | None:
    """把 publish_at 解析为 datetime;取不到返回 None。"""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    if isinstance(value, str):
        v = value.strip()[:19].replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(v, fmt)
            except ValueError:
                continue
    return None


def _bigram_topics(texts: list[str], top_n: int = 10) -> list[dict]:
    """轻量主题词提取:对每段中文按 2 字窗口计频,剔除停用词后取 top。

    不做分词依赖,靠中文 2 字窗口近似出"主题短语"(如 考研、副业、退休、AI)。
    """
    counter: Counter[str] = Counter()
    for text in texts:
        for run in _CJK_RUN.findall(text):
            cleaned = "".join(ch for ch in run if ch not in _STOPWORDS)
            for i in range(len(cleaned) - 1):
                bi = cleaned[i : i + 2]
                if bi not in _STOPWORDS and bi[0] != bi[1]:
                    counter[bi] += 1
    return [{"word": w, "count": c} for w, c in counter.most_common(top_n)]


def _title_hits(title: str) -> dict:
    return {
        "num": bool(_NUM_RE.search(title)),
        "emoji": bool(_EMOJI_RE.search(title)),
        "question": bool(_QUESTION_RE.search(title)),
        "hook": any(w in title for w in _HOOK_WORDS),
        "words": [w for w in _TITLE_SEP.split(title) if _CJK_RE.search(w)],
    }


def _title_top_words(titles: list[str], top_n: int = 8) -> list[str]:
    counter: Counter[str] = Counter()
    for t in titles:
        for w in _title_hits(t)["words"]:
            for ch in _CJK_RUN.findall(w):
                for i in range(len(ch)):
                    c = ch[i]
                    if c not in _STOPWORDS:
                        counter[c] += 1
    return [w for w, _ in counter.most_common(top_n)]


def analyze_articles(articles: list[dict]) -> dict:
    """公众号内容选题分析。`articles` 为 [{title, content?, author?, publish_at?}]。"""
    n = len(articles)
    if n == 0:
        return {"count": 0, "topics": [], "title_style": {}, "publish": {}, "authors": [], "suggestions": ["暂无文章可分析"]}

    titles = [str(a.get("title", "")).strip() for a in articles if a.get("title")]
    contents = [str(a.get("content", "")) + " " + t for a, t in zip(articles, titles) if a.get("content") or t]

    # 选题分布
    topics = _bigram_topics(contents, top_n=10)

    # 标题风格
    hits = [_title_hits(t) for t in titles]
    avg_len = round(sum(len(t) for t in titles) / len(titles), 1) if titles else 0
    title_style = {
        "avg_len": avg_len,
        "num_pct": round(sum(h["num"] for h in hits) / len(hits), 2) if hits else 0,
        "emoji_pct": round(sum(h["emoji"] for h in hits) / len(hits), 2) if hits else 0,
        "question_pct": round(sum(h["question"] for h in hits) / len(hits), 2) if hits else 0,
        "hook_pct": round(sum(h["hook"] for h in hits) / len(hits), 2) if hits else 0,
        "top_words": _title_top_words(titles),
    }

    # 发布时间分布(小时)
    hours: Counter[int] = Counter()
    n_time = 0
    for a in articles:
        dt = _parse_time(a.get("publish_at"))
        if dt is None:
            continue
        hours[dt.hour] += 1
        n_time += 1
    peak_hours = [h for h, _ in hours.most_common(3)] if hours else []
    publish = {
        "count": n_time,
        "by_hour": {str(h): c for h, c in sorted(hours.items())},
        "peak_hours": sorted(peak_hours),
    }

    # 作者(公众号)对比
    by_author: dict[str, list[dict]] = {}
    for a in articles:
        au = str(a.get("author") or "未知").strip() or "未知"
        by_author.setdefault(au, []).append(a)
    authors = []
    for au, sub in by_author.items():
        sub_titles = [str(s.get("title", "")) for s in sub if s.get("title")]
        authors.append({
            "author": au, "count": len(sub),
            "top_topics": [t["word"] for t in _bigram_topics([str(s.get("content", "")) + str(s.get("title", "")) for s in sub], top_n=5)],
            "avg_title_len": round(sum(len(t) for t in sub_titles) / len(sub_titles), 1) if sub_titles else 0,
        })

    # 规则建议(内容选题视角;非流量归因)
    suggestions = []
    if title_style["hook_pct"] < 0.4:
        suggestions.append(f"标题含悬念/吸引词仅 {title_style['hook_pct'] * 100:.0f}%,可多尝试「揭秘/速看/避雷/测试」式开头拉点击")
    if title_style["num_pct"] < 0.3:
        suggestions.append(f"含数字标题仅 {title_style['num_pct'] * 100:.0f}%,数字(如「3个技巧」「第2步」)更易抓眼球")
    if title_style["question_pct"] > 0.2:
        suggestions.append("疑问式标题占比偏高,可搭配结论式(「这样做…」)降低悬而未决感")
    if topics:
        top = topics[0]["word"]
        suggestions.append(f"近期选题主线集中在「{top}」(共 {topics[0]['count']} 处词频),可围绕它深挖/做系列")
    if publish["peak_hours"]:
        suggestions.append(f"发文多集中 { '、'.join(str(h) + '点' for h in publish['peak_hours']) },可固定在此时段更新培养读者习惯")
    if len(authors) > 1:
        richest = max(authors, key=lambda x: x["count"])
        suggestions.append(f"共 {len(authors)} 个对标号,「{richest['author']}」篇数最多({richest['count']}),可重点拆解其选题与标题")
    if not suggestions:
        suggestions.append("样本偏少,建议多录入文章再观察")

    summary = (
        f"共 {n} 篇;选题主线「{topics[0]['word'] if topics else '—'}」;平均标题 {avg_len} 字,"
        f"吸引词占比 {title_style['hook_pct'] * 100:.0f}%;发布高峰 {publish['peak_hours'] or '—'}点。"
    )
    return {
        "count": n,
        "topics": topics,
        "title_style": title_style,
        "publish": publish,
        "authors": authors,
        "suggestions": suggestions,
        "summary": summary,
    }
