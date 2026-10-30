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


def _clean(raw: str) -> str:
    """把抽出来的那段**剥成名字**:去掉首尾空白/标点与尾巴泛词。"""
    s = str(raw or "").strip().strip("《》「」\"'“”‘’,,。.!！?？:：;；、")
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
                   "这", "那", "知道")


def is_name_like(name: str, *, quoted: bool = False) -> bool:
    """像不像一个**资源名**:2~24 字、不是泛词、不以语气词结尾、含汉字或拉丁字母。

    ⚠️ 结尾过滤不是吹毛求疵:`求个XX的资源` 剥掉尾巴后常剩下一个**半句话**
    (`真实有效的`/`有没有的`),把它当名字推给客户就是噪音 —— 宁可漏。
    """
    n = str(name or "").strip()
    #: 书名号里的放宽到 24 字;靠句式猜出来的限 12 字 ——
    #: 实测(`叫什么`/`求…`)长句子会整段被吃进来,而真名字通常不长。
    if not (2 <= len(n) <= (24 if quoted else 12)):
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


def collect_for_video(aweme_id: str, *, session=None, user_id: int = 1,
                      pages: int = 2, count: int = 20) -> dict:
    """把一个视频的评论扫一遍。返回 `{name: {"demand":n,"mention":n,"samples":[…]}}`。"""
    found: dict[str, dict] = defaultdict(lambda: {"demand": 0, "mention": 0, "samples": []})
    cursor, err, no_name = 0, "", 0
    for page in range(max(1, int(pages))):
        res = fetch_comments(aweme_id, cursor=cursor, count=count,
                             session=session, user_id=user_id)
        if not res["ok"]:
            err = res["error"] or "拉评论失败"
            break
        for c in res["comments"]:
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
    return {"names": dict(found), "error": err, "no_name": no_name}


# ---------------------------------------------------------------------------
# 聚合 / 入库 / 推送
# ---------------------------------------------------------------------------


def _upsert(db, user_id: int, name: str, demand: int, mention: int, video_hits: int,
            samples: dict) -> object:
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
    row.last_seen_at = now
    row.samples = json.dumps(samples, ensure_ascii=False)[:4000]
    return row


def pick_pushable(rows, *, min_demand: int = 2, cooldown_hours: int = 48,
                  limit: int = 10) -> list:
    """够格且**冷却期外**的名字。按 (求的次数, 提及数) 降序。

    ⚠️ **加冷却**:本作业 4 小时一轮,不加冷却的话同一个名字一天会推 6 次 ——
    群里会把它当成噪音(`push_ledger` 那套冷却的同一个道理)。
    """
    now = datetime.now()
    out = []
    for r in rows:
        if int(r.demand_hits or 0) < int(min_demand):
            continue
        if r.last_pushed_at and (now - r.last_pushed_at) < timedelta(hours=cooldown_hours):
            continue
        out.append(r)
    out.sort(key=lambda r: (int(r.demand_hits or 0), int(r.mention_hits or 0)), reverse=True)
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
        head = f"**{i}. {r.name}** — 求 {int(r.demand_hits or 0)} 次 / 提及 {int(r.mention_hits or 0)} 次"
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

    webhook = webhook_for(settings, "douyin")
    if not webhook:
        logger.info("没配抖音板块 webhook,需求名卡片不推")
        return False
    return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(build_card(rows))


def source_videos(db, user_id: int = 1, settings=None) -> list[dict]:
    """挑要扫评论的视频 —— **优先我们自己收的推广视频**,搜索只作补充。

    ## 为什么不再"搜 `吃瓜`/`大瓜` 就开扫"(2026-10-30 第一版实跑的教训)

    按种子词搜出来的头几条是**亲子起名**和**外卖折扣群**的视频,评论区里抽出来的是
    「姓龙比较适合」「8折外卖」—— 全是真需求,但**不是我们要的那一类**。
    「吃瓜/大瓜」在抖音上首先命中"八卦八卦"的大众内容,不是网盘推广那一挂。

    而 `douyin_leads`(标题带《口令》的推广视频)与 `douyin_peer_works`(对标号带盘作品)
    **本身就是"这一类"** —— 它们的评论区里才会有人问「求《X》全集」。
    搜索结果只在**库里素材不够**时补位,而且只留**标题带书名号**的(那是推广贴的形状)。
    """
    from datetime import timedelta

    from app.db.models import DouyinLead, DouyinPeerWork

    settings = settings or _settings()
    days = int(getattr(settings, "demand_source_days", 7) or 7)
    since = datetime.now() - timedelta(days=max(1, days))
    need = max(1, int(getattr(settings, "demand_max_videos", 6) or 6))

    out: list[dict] = []
    seen: set[str] = set()

    def _add(vid: str, url: str, title: str) -> None:
        vid = str(vid or "").strip()
        if not vid or vid in seen:
            return
        seen.add(vid)
        out.append({"aweme_id": vid, "url": url or f"https://www.douyin.com/video/{vid}",
                    "title": title or ""})

    # ① 我们自己的线索(推广视频,评论区天然是「求资源」的)
    leads = db.scalars(select(DouyinLead).where(
        DouyinLead.user_id == user_id,
        DouyinLead.found_at >= since).order_by(DouyinLead.found_at.desc())).all()
    for r in leads:
        _add(r.aweme_id, r.url, r.title)
        if len(out) >= need * 2:
            break

    # ② 对标号作品(带网盘的那些)
    if len(out) < need:
        works = db.scalars(select(DouyinPeerWork).where(
            DouyinPeerWork.user_id == user_id,
            DouyinPeerWork.first_seen_at >= since).order_by(
            DouyinPeerWork.first_seen_at.desc())).all()
        for w in works:
            _add(w.aweme_id, w.url, w.title)
            if len(out) >= need:
                break

    # ③ 搜索补位:只留**标题带《》**的(推广贴的形状),按评论数挑
    if len(out) < need:
        seeds = [w.strip() for w in
                 str(getattr(settings, "demand_seed_words", "") or "").split(",") if w.strip()]
        if seeds:
            from app.services import douyin_protocol_source as dps

            vids = dps.search(seeds, settings=settings, session=db, user_id=user_id, pages=1)
            min_c = int(getattr(settings, "demand_min_comments", 20) or 0)
            vids = [v for v in vids if "《" in str(v.get("snippet") or "")
                    and int((v.get("metrics") or {}).get("comment_count") or 0) >= min_c]
            vids.sort(key=lambda v: int((v.get("metrics") or {}).get("comment_count") or 0),
                      reverse=True)
            for v in vids:
                _add(v.get("aweme_id"), v.get("url"), v.get("snippet"))
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
    for v in videos:
        res = collect_for_video(str(v.get("aweme_id") or ""), session=db, user_id=user_id,
                                pages=int(getattr(settings, "demand_comment_pages", 2) or 2))
        if res["error"]:
            errors.append(res["error"])
            continue
        scanned += 1
        no_name += int(res.get("no_name") or 0)
        for name, rec in res["names"].items():
            a = agg.setdefault(name, {"demand": 0, "mention": 0, "video_hits": 0,
                                      "comments": [], "video": ""})
            a["demand"] += rec["demand"]
            a["mention"] += rec["mention"]
            a["video_hits"] += 1
            for s in rec["samples"][:2]:
                if len(a["comments"]) < 3 and s not in a["comments"]:
                    a["comments"].append(s)
            if not a["video"]:
                a["video"] = str(v.get("url") or "")

    if not agg:
        #: ⚠️ **扫了却一个名字都没抽到,要标 partial 而不是 success** ——
        #: 否则「链路坏了」和「这轮评论没人求」在 `runs` 里长得一模一样(本仓最忌的假成功)。
        #: `求N(无名)` 是**素材对不对**的唯一证据:它 >0 说明有人求、只是没说出名字;
        #: 它 =0 说明这批素材的评论区压根不是"求资源"的场景。
        _record_run(db, user_id, "demand_watch", "partial" if videos else "success",
                    f"视频{len(videos)} 扫{scanned} 名字0 求{no_name}(无名)"
                    + (f" 失败:{errors[0][:60]}" if errors else ""))
        db.commit()
        return {"videos": len(videos), "names": 0}

    for name, a in agg.items():
        _upsert(db, user_id, name, a["demand"], a["mention"], a["video_hits"],
                {"comments": a["comments"], "video": a["video"]})
    db.commit()

    rows = db.scalars(select(DemandName).where(
        DemandName.user_id == user_id,
        DemandName.platform == PLATFORM)).all()
    chosen = pick_pushable(
        rows,
        min_demand=int(getattr(settings, "demand_min_hits", 2) or 2),
        cooldown_hours=int(getattr(settings, "demand_push_cooldown_hours", 48) or 48),
        limit=int(getattr(settings, "demand_push_limit", 10) or 10))
    pushed = 0
    if chosen and push_card(chosen, settings):
        now = datetime.now()
        for r in chosen:
            r.last_pushed_at = now
        db.commit()
        pushed = len(chosen)

    detail = (f"视频{len(videos)} 扫{scanned} 名字{len(agg)} 求{no_name}(无名) 推{pushed}")
    if errors:
        detail += f" 失败:{errors[0][:60]}"
    _record_run(db, user_id, "demand_watch",
                "success" if not errors else "partial", detail)
    db.commit()
    return {"videos": len(videos), "names": len(agg), "pushed": pushed,
            "no_name": no_name}


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
            except Exception:  # noqa: BLE001 - 一个用户失败不影响其他用户
                logger.exception("需求名监控失败 user=%s", uid)
                db.rollback()
        return n
    finally:
        db.close()
