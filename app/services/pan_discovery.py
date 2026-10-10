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
)


def _is_dead_link(message: str) -> bool:
    """这条失败是"**链本身已经死了**"吗?"""
    text = message or ""
    return any(h in text for h in _DEAD_LINK_HINTS)


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


def _alert_backlog(session, user_id: int, backlog_left: int, out: dict) -> None:
    """积压超过阈值 → 推**管理员群**。

    ⚠️ **为什么必须报**(2026-10-05 生产实测):一轮 **候选 44 条、只转存 3 条**,
    库里堆了 **41 条 pending**,理由清一色「本轮转存额度用完」——**它们是健康的、能搬的链**,
    纯粹被额度卡住;按当时 3 条/天要**两周**才清得完。而**运行记录一路 `success`**,
    没有任何地方提到"还剩 41 条在排队" ⇒ **额度不足可以无声无息地持续几周**。
    这与本仓反复踩的「静默失败 = 假成功」完全同源:系统说自己好,实际上在堆积。
    """
    try:
        from app.services.alert_service import notify_incident

        notify_incident(session, user_id, "pan_discovery",
                        f"网盘发现积压 {backlog_left} 条(转存额度跟不上发现)",
                        f"本轮候选 {out.get('found', 0)} 条、成功 {out.get('ok', 0)} 条,"
                        f"仍积压 **{backlog_left}** 条(pending/failed)。"
                        f"积压多半是「额度用完」而非链失效 —— 可调大 `PAN_DISCOVERY_TRANSFER_LIMIT`"
                        f"(积压额度)或 `PAN_DISCOVERY_FRESH_LIMIT`(新发现额度)加快清空。")
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
        from app.services.wechat._candidates import mine_title_entities
        hints += mine_title_entities([t], top=3)
    except Exception:  # noqa: BLE001 - 抽不出来不影响主流程
        pass
    # ② 连续中文片段(先去掉 #话题# 与链接,免得把推广词当资源名)
    body = re.sub(r"#\s*[^#]{1,30}\s*#", " ", t)
    body = re.sub(r"https?://\S+", " ", body)
    for seg in re.findall(r"[一-鿿]{4,12}", body):
        hints.append(seg)
    out: list[str] = []
    for h in hints:                       # 保序去重 + 去掉过短的
        h = h.strip()
        if len(h) >= 3 and h not in out:
            out.append(h)
    return out[:5]


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
            if r.get("my_link"):
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
                     snippet: str = "") -> dict:
    """**按链分发转存**:夸克走 `QuarkTransfer`、百度走 `BaiduPanClient`、迅雷走 `xunlei_transfer`。

    返回 `{"status", "our_url", "code", "message"}`;任何失败都返回结构化结果,不抛。

    ⚠️ **百度链要提取码**:知乎回答里常把码写在附近,所以把整段文字传进来一起找。
    """
    from app.services.cookie_store import get_cookie

    kind = _kind_of(pan_url)
    if not kind:
        return {"status": "skipped", "message": f"不认识的盘链:{pan_url[:60]}",
                "our_url": "", "code": ""}
    try:
        if kind == "quark":
            from app.services.quark_transfer import QuarkTransfer

            ck = (get_cookie(session, user_id, "quark")     # 按用户配的
                  or getattr(settings, "quark_cookie", "") or "").strip()   # .env 兜底
            if not ck:
                # ⚠️ **可重试**:配上 Cookie 就能搬 —— 标 pending 而不是终态,
                # 否则"发现过一次"就把这条资源永久钉死(2026-10-02 实测踩过)
                return {"status": "pending", "message": "未配夸克 Cookie(配上后会自动重试)",
                        "our_url": "", "code": ""}
            quark = QuarkTransfer(ck, fid_store=getattr(settings, "quark_fid_store", "") or None)
            res = quark.transfer_and_share(
                pan_url,
                save_dir=getattr(settings, "quark_save_dir", "") or "/来自发现",
                password=getattr(settings, "quark_share_password", "") or "",
                intro_dir=getattr(settings, "pan_intro_quark_dir", "") or "")
            if res.get("share_url"):
                return {"status": "ok", "our_url": str(res.get("share_url")),
                        "code": str(res.get("password") or ""), "message": ""}   # ⚠️ 夸克返回的键是 password
            return _fail("转存未返回我方链")

        if kind == "baidu":
            from app.services.baidupan_transfer import BaiduPanClient, extract_pwd

            ck = get_cookie(session, user_id, "baidupan")
            if not ck:
                return {"status": "pending", "message": "未配百度网盘 Cookie(配上后会自动重试)",
                        "our_url": "", "code": ""}
            pwd = extract_pwd(snippet or "", pan_url) or ""
            res = BaiduPanClient(ck).transfer_and_share(
                pan_url, password=pwd,
                intro_dir=getattr(settings, "pan_intro_baidu_dir", "") or "")
            if res.get("share_url"):
                return {"status": "ok", "our_url": str(res.get("share_url")),
                        "code": str(res.get("password") or ""), "message": ""}   # ⚠️ 百度返回的键也是 password
            return _fail("转存未返回我方链")

        from app.services import xunlei_transfer as xt

        res = xt.transfer_and_share(pan_url)
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


def sync(session, user_id: int, settings=None) -> dict:
    """找一轮 → 转存 → 入库。返回 `{"status", "found", "ok", "skipped", "failed", "items"}`。"""
    from config.settings import get_settings

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
    backlog = [{"platform": r.platform, "origin_url": r.origin_url, "title": r.title or "",
                "author": r.author or "", "source_url": r.source_url or ""}
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
    reused = 0                     # **复用了别的盘已有的链**(省下一次转存)
    items: list[dict] = []
    for c in cands:
        # 这条候选是**存量待办**还是**本轮新搜到的**?决定吃哪份额度(见上面两行注释)
        is_backlog = c["origin_url"] in backlog_urls
        src = "积压" if is_backlog else "新发现"
        # **三盘互通**(2026-10-05 用户口径):先看**别的网盘**有没有这个资源。
        # 有 ⇒ **不再转存**,直接把已有那条链推出去 —— 省空间、省额度、少一次写操作。
        # ⚠️ 这一步**不花 transfer 额度**(没调转存接口),所以放在额度判断之前。
        have = already_have(session, user_id, c["title"])
        if have:
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
            res = transfer_pan_url(session, user_id, c["origin_url"], settings, c["title"])
            status, message = res["status"], res["message"]
            our, code = res["our_url"], res["code"]
            if status == "ok":
                if is_backlog:
                    backlog_budget -= 1
                else:
                    fresh_budget -= 1
        row = exist.get(c["origin_url"])
        if row is None:
            row = DiscoveredPanLink(user_id=user_id, origin_url=c["origin_url"][:500])
            session.add(row)
            exist[c["origin_url"]] = row
        row.platform, row.title = c["platform"], c["title"][:255]
        row.author, row.source_url = c["author"], c["source_url"]
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
                          "source_url": c["source_url"], "share_url": our, "code": code})
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
                "**积压剩 %d 条**(积压额度 %d / 新发现额度 %d)",
                len(keywords), len(cands), ok, reused, backlog_left,
                int(getattr(settings, "pan_discovery_transfer_limit", 10) or 0),
                int(getattr(settings, "pan_discovery_fresh_limit", 3) or 0))
    return {"status": "ok", "found": len(cands), "ok": ok, "skipped": skipped,
            "pending": pending, "failed": failed, "reused": reused, "items": items,
            "backlog_size": len(backlog),        # 本轮开始时**排队等搬**的存量
            "backlog_left": backlog_left}        # 本轮结束时**仍**在排队的


def push_items(items: list[dict], settings) -> bool:
    """把新转存的资源推飞书(**知乎专属群**,未配回落主群)。版式与公众号一致:四列网格。"""
    if not items:
        return False
    from app.services.feishu_client import webhook_for

    # 这是**内容卡**(资源 + 可用链,给客户用)→ 未配专属群时回落**客户主群**是对的。
    # (2026-10-03 曾误改成"落管理群",用户纠正:管理群只接维护信息、内容就该进客户群。)
    webhook = webhook_for(settings, "zhihu")
    if not webhook:
        return False
    from app.services.feishu._cards import _col_set_row, _md_safe, strip_others
    from app.services.feishu_client import FeishuClient

    brand = (getattr(settings, "brand_name", "") or "").strip()
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content":
        f"公开平台(知乎)新发现 **{len(items)}** 个资源,已转存成我方分享链:"}},
        _col_set_row([("**作者**", 3), ("**资源**", 5), ("**我方分享链**", 4)], grey=True)]
    for it in items:
        elements.append(_col_set_row([
            (_md_safe(it.get("author") or "—"), 3),
            (_md_safe(strip_others(it.get("title") or "")[:40]), 5),
            (f"[▶ 打开]({_md_safe(it.get('share_url') or '')})"
             + (f" 🔑{_md_safe(it.get('code') or '')}" if it.get("code") else ""), 4)]))
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "turquoise", "title": {"tag": "plain_text",
                                                          "content": f"🔎 {brand + ' · ' if brand else ''}"
                                                                     f"新发现资源 · {len(items)} 个"}},
            "elements": elements}
    try:
        return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(card)
    except Exception:  # noqa: BLE001
        logger.exception("网盘发现推送失败")
        return False


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
                if out.get("items"):
                    push_items(out["items"], settings)
                # 运行记录里**必须带上积压数** —— 只写「候选N 转存M」的话,一个持续
                # 堆积的系统看起来和健康的系统一模一样(2026-10-05 实测踩到)。
                _record_run(db, uid, "pan_discovery", "success",
                            f"候选{out.get('found', 0)} 转存{out.get('ok', 0)} "
                            f"复用{out.get('reused', 0)} **积压{out.get('backlog_left', 0)}**")
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
