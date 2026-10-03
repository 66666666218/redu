"""跨平台同类资源号发现(2026-10-01)。

⚠️ 注意与 `cross_platform.py`(跨**板块**趋势:rising_across/run_cross_platform_alert)
   区分——那个是"微博/抖音/百度等板块之间的共振",本模块是"跨**平台站点**找同类资源号"。

**要解决的问题**:公众号监控采到的**具体网盘资源**(如"花少2人格测试"、"XX游戏安装包"),
在别的平台上也有人发——那些账号是我们的同类对标号,但现有的候选发现只搜狗搜微信生态,
看不到它们。

**做法**(用户口径):拿资源关键词 → 各平台搜 → **结果里真含网盘链**的才收录
("确认其内容,如果确认是推广网盘的就设置成对标账号添加进去")。

**各平台门槛(2026-10-01 逐平台实测)**:
- `zhihu`    ✅ 带登录 Cookie 直接搜(`api/v4/search_v3`,**无需 x-zse 签名**)
- `bilibili` ⚠️ 分区榜能拿 UP 主(无需登录),但风控按频率、连发即 `-352`,要做必须限速
- `weibo`    ❌ 完整 Cookie + XSRF 仍 `ok:-100`(比登录态更深的校验)
- `tieba`    ❌ 带 BDUSS/STOKEN 仍 403
- `baidu`    ⚠️ 网页搜索可用(www.baidu.com/s 返回 200),但结果是内容页不是账号页
- `douyin` / `xiaohongshu` ❌ 需真机请求签名,行业共识做不到

架构:`SEARCHERS` 注册表;新增平台只加一个 `(cookie, keyword, limit) -> list[dict]`。
相关:`doc/pan-promotion-channels.md`(为什么做)、`wechat/_candidates.py`(微信生态内的同类发现)。
"""

from __future__ import annotations

import re
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CrossPlatformAccount
from app.utils import get_logger

logger = get_logger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 资源库标题 → 搜索词的清洗件(见 `library_search_word` 的注释与实测数据)
_TAG_RE = re.compile(r"<[^>]+>")            # B站/知乎返回里带 `<em class="keyword">` 高亮标签
_PAREN_RE = re.compile(r"[（(【\[][^)）】\]]*[)）】\]]")
_PUNCT = "｜|·—-,，、:：!！?？~ "
_LEAD_NOISE = ("亲测", "爆火", "最新", "超火", "实测", "分享")


def _pan_of(text: str) -> str:
    """文本里的第一条网盘分享链(没有返回空串)。经门面取工具,避免子模块循环导入。"""
    from app.services.wechat_monitor import _extract_pan_urls

    urls = _extract_pan_urls("", text or "")
    return urls[0] if urls else ""


# 「这号明摆着是网盘推广号」的强特征词(2026-10-02)。用于**搜索层不给链**的平台(B站):
# 谁把自己的号叫成"网盘资源商行""夸克网盘扩容免费",谁就是在做这门生意——这比链更直接。
# 只认 **"网盘"/具体网盘品牌名**,**不认泛词"资源"**(否则会收进一大堆无关号)。
_PAN_ACCOUNT_HINTS = ("网盘", "夸克", "百度盘", "百度网盘", "阿里云盘", "迅雷",
                      "uc网盘", "115网盘", "蓝奏云", "盘搜")


class SearchSourceError(RuntimeError):
    """搜索源**硬失败**(网络异常 / 非 200 / 返回体不是搜索结构)。

    ⚠️ **为什么必须与"搜到 0 条"分开**(2026-10-03):此前 `_search_zhihu` 把所有异常都吞成
    `[]`,于是**被限流/登录态失效时与"真的没有"完全无法区分** —— 下游 `pan_discovery.sync()`
    照样记 `success(候选0)`,链路看着健康、其实早被挡住了(与闲鱼那次"假成功"同一类问题)。
    实测就踩到过:同一批词,单跑能捞到夸克链,紧接着整轮 `sync()` 却是 0 —— 静默失败。
    """


def _search_zhihu(cookie: str, keyword: str, limit: int = 20) -> list[dict]:
    """知乎搜索 → `[{uid, name, url, snippet, pan_link}]`。

    端点 `api/v4/search_v3` 实测**带登录 Cookie 即可、无需 x-zse 签名**(2026-10-01)。
    账号标识取 `author.url_token`(知乎账号的稳定 id,主页 = `/people/<token>`)。
    **硬失败抛 `SearchSourceError`**(不再返回空列表冒充"没有结果")。
    """
    import requests

    if not cookie or not (keyword or "").strip():
        return []
    try:
        resp = requests.get(
            "https://www.zhihu.com/api/v4/search_v3",
            params={"t": "general", "q": keyword.strip(), "limit": max(1, min(limit, 20))},
            headers={"Cookie": cookie, "User-Agent": _UA, "Referer": "https://www.zhihu.com/",
                     "Accept": "application/json, text/plain, */*"},
            timeout=20)
    except Exception as exc:  # noqa: BLE001 - 包成自己的异常类型,好让调用方区分
        raise SearchSourceError(f"请求异常:{exc}") from exc
    if getattr(resp, "status_code", 200) != 200:
        sc = resp.status_code
        hint = "(登录态失效或被限流)" if sc in (401, 403, 429) else ""
        raise SearchSourceError(f"HTTP {sc}{hint}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SearchSourceError(f"返回非 JSON:{str(getattr(resp, 'text', ''))[:80]}") from exc
    if not isinstance(payload, dict) or "data" not in payload:
        # 限流时知乎回 {"error": {...}} —— 那不是"没有结果",必须报错
        raise SearchSourceError(f"返回体无 data:{str(payload)[:100]}")
    out: list[dict] = []
    for item in payload.get("data") or []:
        obj = item.get("object") or {}
        author = obj.get("author") or {}
        uid = str(author.get("url_token") or "").strip()
        name = str(author.get("name") or "").strip()
        if not uid or not name:
            continue          # hot_timing/ring_box 这类非账号条目直接跳过
        snippet = f"{obj.get('title') or ''} {obj.get('content') or ''}".strip()
        out.append({"uid": uid, "name": name,
                    "url": f"https://www.zhihu.com/people/{uid}",
                    "snippet": snippet[:255],
                    "pan_link": _pan_of(snippet)})
    return out


# B站搜索的 wbi 签名(2023 起该接口要求 `w_rid`)——算法**公开、纯本地可算**,
# 不需要浏览器/真机,这是它比抖音小红书好接的根本原因。打乱表是官方固定值。
_BILI_WBI_TAB = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
                 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
                 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
                 20, 34, 44, 52)
_bili_mixin_cache: dict = {"key": "", "ts": 0.0}
_BILI_MIXIN_TTL = 3600.0     # wbi key 每天轮换;mixin 缓存 1 小时足够,还能省掉每轮的 nav 请求


def _bili_mixin() -> str:
    """取 B站 wbi 的 mixin key(带缓存)。

    **未登录也会返回** `wbi_img`——实测 nav 接口 `code=-101`(未登录)但 `data.wbi_img` 照给。
    """
    now = time.time()
    if _bili_mixin_cache["key"] and now - float(_bili_mixin_cache["ts"]) < _BILI_MIXIN_TTL:
        return str(_bili_mixin_cache["key"])
    import requests

    try:
        payload = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/"},
            timeout=15).json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("B站 wbi key 获取失败:%s", exc)
        return ""
    wbi = (payload.get("data") or {}).get("wbi_img") or {}
    ik = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sk = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not ik or not sk:
        return ""
    mixin = "".join((ik + sk)[i] for i in _BILI_WBI_TAB)[:32]
    _bili_mixin_cache.update(key=mixin, ts=now)
    return mixin


def _bili_signed_get(search_type: str, keyword: str, cookie: str = "") -> dict:
    """B站 wbi 签名 GET(签名是**公开算法、本地纯 Python 可算**,所以匿名也能搜)。

    签名规范:值里去掉 `!'()*` 四个字符,按键排序拼成 query,再 `md5(query + mixin)`。
    """
    import hashlib
    import urllib.parse

    import requests

    mixin = _bili_mixin()
    if not mixin:
        return {}
    params = {"search_type": search_type, "keyword": keyword.strip(), "page": 1,
              "wts": int(time.time())}
    clean = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in sorted(params.items())}
    query = urllib.parse.urlencode(clean)
    url = ("https://api.bilibili.com/x/web-interface/wbi/search/type?"
           f"{query}&w_rid={hashlib.md5((query + mixin).encode()).hexdigest()}")
    headers = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
    if cookie:
        headers["Cookie"] = cookie     # 配了就用(风控更宽松);没有也能跑
    return requests.get(url, headers=headers, timeout=20).json()


def search_bilibili_videos(keyword: str, limit: int = 20, cookie: str = "") -> list[dict]:
    """B站**搜视频** → `[{uid, name, url, snippet, pan_link}]`,供**名字型**用。

    **为什么搜视频**(2026-10-03):`_search_bilibili` 搜的是**用户**(那是找对标号的口径),
    而名字型要的是"**这个资源在平台上有没有人在推**" —— 那必须看内容。
    实测搜「网盘资源」20 条,标题正是「【原版】火影忍者720集网盘资源!!未删减版」这类。

    ⚠️ 视频简介(`description`)**实测基本是空的**(搜索接口不给),所以 `snippet` 里能用的
    只有**标题** + 作者 —— 但名字型**不需要更多**:判断"有没有人在推同一资源"看标题就够,
    要链的话回**资源库**匹配(那才是名字型的本意)。

    匿名即可,无需登录;风控**按频率**(与搜用户同一条口径),调用方要限速。
    """
    if not (keyword or "").strip():
        return []
    try:
        payload = _bili_signed_get("video", keyword, cookie)
    except Exception as exc:  # noqa: BLE001 - 包成自己的异常类型,好让调用方区分
        raise SearchSourceError(f"B站请求异常:{exc}") from exc
    if not payload:
        raise SearchSourceError("B站搜索不可用(wbi mixin 取不到)")
    if payload.get("code") != 0:
        # 风控 -412 / 频率 -352 都会落到这里 —— 必须报错,别跟"搜到 0 条"混为一谈
        raise SearchSourceError(f"B站返回 code={payload.get('code')} {str(payload.get('message'))[:60]}")
    out: list[dict] = []
    for item in ((payload.get("data") or {}).get("result") or [])[: max(1, min(limit, 50))]:
        bvid = str(item.get("bvid") or "").strip()
        # 标题里带 `<em class="keyword">` 高亮标签,要剥掉(与知乎 snippet 同一处理)
        title = _TAG_RE.sub("", str(item.get("title") or "")).strip()
        if not bvid or not title:
            continue
        author = str(item.get("author") or "").strip()
        out.append({"uid": bvid, "name": author or "—",
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "snippet": title[:255], "pan_link": _pan_of(title),
                    # ⚠️ **必须回填 `keyword`**:调用方(`resource_presence.probe`)按它把结果
                    # 归到"是搜哪个资源名搜出来的";少了这个字段 → 匹配不上 → **永远出 0 条**
                    # (2026-10-03 单测抓到的静默归零)。
                    "keyword": keyword.strip(),
                    "looks_like_pan": False})
    return out


def _search_bilibili(cookie: str, keyword: str, limit: int = 20) -> list[dict]:
    """B站**搜用户** → `[{uid, name, url, snippet, pan_link, looks_like_pan}]`。

    **为什么搜用户而不是搜视频**(2026-10-02 两条路都实测过):
    - 搜视频:20 条结果的 `description` **全是空的**——搜索接口不返回视频简介,拿不到链
      (与抖音同理:链不在搜索层);
    - 搜用户:一次请求返回 20 个**账号**,带 `mid`(**可拼主页**)+ `uname` + `usign`(签名),
      且实测搜"网盘资源"返回的号名本身就写着「网盘资源/网盘资源商行/夸克网盘资源/
      看简介有网盘资源」——**这比链更直接**:谁把号叫成这个,谁就在做这门生意。

    匿名即可搜(wbi 签名本地自算);风控**按频率**(连发即 `-352`),调用方必须限速。
    """
    if not (keyword or "").strip():
        return []
    try:
        payload = _bili_signed_get("bili_user", keyword, cookie)
    except Exception as exc:  # noqa: BLE001 - 包成自己的异常类型,好让调用方区分
        raise SearchSourceError(f"B站请求异常:{exc}") from exc
    if not payload:
        raise SearchSourceError("B站搜索不可用(wbi mixin 取不到)")
    if payload.get("code") != 0:
        # 风控 -412 / 频率 -352 都会落到这里 —— 必须报错,别跟"搜到 0 条"混为一谈
        # (2026-10-03 修:此前这里 `return []`,于是"被拦"在运行记录里长得像"真没新号")。
        raise SearchSourceError(f"B站返回 code={payload.get('code')} {str(payload.get('message'))[:60]}")
    out: list[dict] = []
    for item in ((payload.get("data") or {}).get("result") or [])[:max(1, min(limit, 50))]:
        mid = str(item.get("mid") or "").strip()
        name = str(item.get("uname") or "").strip()
        if not mid or not name:
            continue
        text = f"{name} {item.get('usign') or ''}".strip()
        out.append({"uid": mid, "name": name,
                    "url": f"https://space.bilibili.com/{mid}",
                    "snippet": text[:255],
                    "pan_link": _pan_of(text),
                    "looks_like_pan": any(h in text for h in _PAN_ACCOUNT_HINTS)})
    return out


# 平台注册表:新增平台只加一个 `(cookie, keyword, limit) -> list[dict]`。
# 各平台门槛与实测结论见 `doc/pan-promotion-channels.md` §七。
SEARCHERS = {"zhihu": _search_zhihu, "bilibili": _search_bilibili}

# 免 Cookie 即可跑的平台:B站的 wbi 签名是公开算法(本地自算),没配 Cookie 也能搜。
# 其余(知乎)必须有登录态——没 Cookie 的会被 `platforms` 过滤掉。
ANON_PLATFORMS = frozenset({"bilibili"})

# 账号**垂直**平台(区别于知乎那种"在内容里顺手分享"的平台):这里的网盘号是**专门做
# 这门生意**的,所以要用**行业词**而不是资源词去搜(见 discover_cross_accounts 的 docstring)。
ACCOUNT_PLATFORMS = frozenset({"bilibili"})

# MediaCrawler 覆盖的平台(2026-10-01):它们**光带 Cookie 过不去**(微博 -100/贴吧 403/
# 小红书抖音要签名),只能靠真浏览器算签名——所以走 `mediacrawler_source`,而且它的 CLI
# 一次吃一整个关键词列表,逐词调用等于反复开关浏览器,故**批量跑**。
#
# ⚠️ 2026-10-02 抖音实跑后**默认停用**(见 settings.cross_mediacrawler_enabled 的说明):
# 该工具是作者的教学版,账号信息刻意脱敏(昵称 `籽***）`、user id 为 sha256 截断、无主页
# 链接)→ 收录不了对标号;且抖音盘链不在搜索返回里(143 条实测 0 条真链)。留着代码和
# 登录态,但不再进定时轮——省得每周开一次浏览器白招风控。
MEDIACRAWLER_PLATFORMS = ("xiaohongshu", "douyin", "kuaishou", "weibo", "tieba", "bilibili")

# 限速(2026-10-01,用户要求"一次不要访问太多"):持续高频轮询是最容易被判定为爬虫的模式。
# 逐个**请求**之间留间隔——一轮 3 词 × N 平台 = 3N 次请求,而 B站风控正是按频率(连发即 -352)。
_REQ_GAP = 4.0


def pick_mediacrawler_platform(seed: int | None = None) -> str:
    """按天轮换选一个 MediaCrawler 平台——一次只碰一个,别一天把六个平台的浏览器都开一遍。"""
    from datetime import date

    s = seed if seed is not None else date.today().toordinal()
    return MEDIACRAWLER_PLATFORMS[s % len(MEDIACRAWLER_PLATFORMS)]


def discover_cross_accounts(session: Session, user_id: int, settings=None,
                            keywords: list[str] | None = None, limit: int = 0) -> dict:
    """拿搜索词去各平台搜,把**明摆着做网盘推广**的账号收录为跨平台对标号。

    **两类平台的搜索词来源不同**(2026-10-02 实测,这是本功能的关键设计):
    - **内容平台(知乎)**:用**资源词**——`resource_library.resonance_resources`(同链被
      ≥2 个对标号同发 = 需求被反复验证),问的是"谁在分享**这个具体资源**";
    - **账号垂直平台(B站)**:用**行业词**(`settings.cross_bili_keywords`),问的是
      "谁在做**这门生意**"。实测差异极大:拿资源词去 B站 搜用户返回 **0 个**;
      拿"网盘资源"搜返回 **20 个号、20 个全是网盘号**(号名就写着"网盘资源/
      夸克网盘资源/网盘资源商行")——B站 上这类号是**垂直账号**,不是"顺手分享",
      所以必须按行业找,而不是按资源找。

    返回 `{"status", "keywords", "platforms", "found", "new", "items"}`;
    `found` 统计的是**判定命中**(有盘链 或 号名/签名明写网盘)的条数。
    """
    from app.services.cookie_store import get_cookies

    if keywords is None:
        top = int(getattr(settings, "cross_discover_keywords", 5) or 5)
        keywords = _keywords_from_library(session, user_id, top)
    if not keywords:
        return {"status": "no_keywords", "new": 0}
    cookies = get_cookies(session, user_id)
    platforms = [p for p in SEARCHERS if cookies.get(p) or p in ANON_PLATFORMS]
    if not platforms:
        return {"status": "no_cookie", "new": 0}

    # 组装 (平台, 搜索词) 任务表:内容平台配资源词,账号垂直平台配行业词(见 docstring)。
    jobs: list[tuple[str, str]] = [(p, kw) for kw in keywords
                                   for p in platforms if p not in ACCOUNT_PLATFORMS]
    jobs += [(p, kw) for kw in _account_keywords(settings)
             for p in platforms if p in ACCOUNT_PLATFORMS]

    found, new, items = 0, 0, []
    tried = 0
    failed: list[str] = []
    last_err = ""
    for idx, (plat, kw) in enumerate(jobs):
        if idx:
            time.sleep(_REQ_GAP)   # 限速:每个请求之间都留间隔(用户要求"一次不要访问太多")
        tried += 1
        try:
            hits = SEARCHERS[plat](cookies.get(plat, ""), kw, limit=20)
        except Exception as exc:  # noqa: BLE001 - 单平台失败不影响其余
            # ⚠️ 单次失败不该炸整轮,但**必须计数**:全失败时"被限流/登录失效"与
            # "真的没有新号"长得一模一样(2026-10-03 修,与已修的三条发现链对齐)。
            failed.append(f"{plat}/{kw}")
            last_err = f"{plat}: {type(exc).__name__}: {str(exc)[:80]}"
            logger.exception("跨平台搜索失败 %s/%s", plat, kw)
            continue
        for h in hits:
            # 收录判据:① 内容里有真盘链(知乎口径);或 ② **号名/签名明写网盘**(B站口径,
            # 见 _PAN_ACCOUNT_HINTS——B站搜索层不给链,但号名本身就是强信号)。
            # 两者都没有 = 光聊资源的普通用户,不要。
            if not h.get("pan_link") and not h.get("looks_like_pan"):
                continue
            found += 1
            if _save(session, user_id, plat, h, kw):
                new += 1
                items.append({"platform": plat, "name": h["name"], "keyword": kw})
    # ② MediaCrawler 型平台:一次跑**全部关键词**(它的 CLI 是批量的,逐词调用等于反复开关浏览器)。
    #    工具没装/跑挂都不该拖垮上面那条直连型的发现,所以整段兜住异常。
    #    2026-10-02 默认关(实测:账号脱敏 + 拿不到盘链,见 settings 里的说明)。
    if getattr(settings, "cross_mediacrawler_enabled", False):
        try:
            from app.services import mediacrawler_source as mc

            if mc.available()[0]:
                for plat in [pick_mediacrawler_platform()]:   # 每天只碰一个平台,不一次开六个浏览器
                    for h in mc.crawl(plat, keywords):
                        if not h.get("pan_link"):
                            continue
                        found += 1
                        if _save(session, user_id, plat, h, keywords[0] if keywords else ""):
                            new += 1
                            items.append({"platform": plat, "name": h["name"], "keyword": "批量"})
        except Exception:  # noqa: BLE001 - 工具缺依赖/失效都不影响直连型平台
            logger.exception("MediaCrawler 发现失败")
    session.commit()
    if tried and len(failed) == tried:
        # **全部搜索都失败** → 这不是"没有新号",是链路被挡住(限流/登录失效/风控)。
        # 必须抛出去,否则会记成 `success(新增0)` —— 与"真没新号"无法区分(2026-10-03 修)。
        raise SearchSourceError(f"{tried} 次搜索全部失败:{last_err}")
    if failed:
        logger.warning("跨平台发现:%d/%d 次搜索失败(其余照常)", len(failed), tried)
    logger.info("跨平台发现:关键词 %d × 直连平台 %d → 命中 %d,新增 %d",
                len(keywords), len(platforms), found, new)
    return {"status": "ok", "keywords": keywords, "platforms": platforms,
            "found": found, "new": new, "items": items, "failed": len(failed)}


def _account_keywords(settings) -> list[str]:
    """账号垂直平台(B站)的搜索词:**行业词**,不是资源词(理由见 `discover_cross_accounts`)。

    默认 "网盘资源/夸克网盘/百度网盘"——实测拿它搜 B站 用户,返回的号名里就写着网盘。
    想按自己的方向收窄,配 `CROSS_BILI_KEYWORDS` 加词(如 "影视网盘资源,漫剧资源")。
    """
    raw = getattr(settings, "cross_bili_keywords", "") or "网盘资源,夸克网盘,百度网盘"
    return [k.strip() for k in raw.split(",") if k.strip()]


def library_search_word(title: str, limit: int = 16) -> str:
    """资源库标题 → 平台搜索词(抖音线索 / 知乎直链 / 跨平台热度 **三条链共用**)。

    ⚠️ **不能直接复用 `douyin_leads._to_search_word`**(2026-10-03 实测):那条是为
    **群资源标题**设计的(「手机警报器（警笛模拟器）2.0版」),做法是**按 `、！` 等分隔符
    取首段**;而资源库标题常是完整短语(「七宗罪、七美德测试免费入口直达（附最新链接）」),
    照搬会被砍成「七宗罪」→ 不足 4 字 → **整条丢掉**。实测库里 8 条资源,照搬**丢 3 条**。

    实测暴露的三个真问题与对策:
      · **截断留下分隔符尾巴**:`花少2人格测试直达入口｜最新测试` 硬切 12 → `…入口｜` → 清尾部标点;
      · **括号补充说明吃掉预算**:`霸王茶姬杯贴自定义入口链接直达（附教程）0919` 硬切 12 只到
        `…入口链` —— **先剥括号**,整名才进得来(这也是把预算从 12 放到 16 的原因);
      · **开头口水词**:`亲测！苹果ios共享id…` 的「亲测！」搜不出东西 —— 只在**紧跟标点时**才剥,
        否则会误伤 `爆火的“审批小程序”`(引号里的才是名字)。
    """
    text = _PAREN_RE.sub(" ", title or "")
    text = re.sub(r"\s+", "", text)                       # 中文标题里的空格多是排版,去掉
    for noise in _LEAD_NOISE:                             # 只在后面跟标点时才剥,避免误伤
        if text.startswith(noise) and len(text) > len(noise) and text[len(noise)] in _PUNCT:
            text = text[len(noise):]
            break
    text = re.sub(r"[vV]?\d+(?:\.\d+)+版?$", "", text)    # 版本尾串(2.0版 / v1.2)
    text = re.sub(r"\d{2,}$", "", text)                   # 尾巴上的日期串(0919 / 20241001)
    # 分隔符之后多是补充说明(「…直达入口｜最新测试」「…高清动态壁纸｜200张+8k…」),
    # 从第一个**位置 ≥6** 的分隔符处切断 —— 阈值 6 是为了别把「七宗罪、七美德…」砍成「七宗罪」。
    for i, ch in enumerate(text):
        if i >= 6 and ch in _PUNCT:
            text = text[:i]
            break
    if len(text) > limit:                                 # 仍超预算 → 硬切,但别切在 ASCII 词中间
        cut = limit
        if text[cut - 1].isascii() and text[cut].isascii() and text[cut - 1].isalnum():
            while cut > 0 and text[cut - 1].isascii() and text[cut - 1].isalnum():
                cut -= 1
        text = text[:cut]
    text = text.strip(_PUNCT)
    if len(text) < 4:
        return ""                                         # 太短搜不出东西(与群那条同口径)
    from app.services.xunlei_group import is_bulk_resource

    # 泛化大包名(最全文件/XX合集)当搜索词只会招来噪音 —— 闸门**共用**群那条路的词表,
    # "这条资源名太泛、指不到具体东西"是同一件事,只该有一份定义(见 `is_bulk_resource`)。
    return "" if is_bulk_resource(text) else text


def _keywords_from_library(session: Session, user_id: int, top: int = 5) -> list[str]:
    """从资源库挑**需求被验证过**的资源名当搜索词(同链被多号同发)。

    清洗见 `library_search_word` —— 此前这里是 `strip()[:12]` 硬切,三条下游链(抖音线索/
    知乎直链/跨平台热度)都吃这个粗词。
    """
    try:
        from app.services.resource_library import resonance_resources

        rows = resonance_resources(session, user_id, days=30, min_accounts=2, limit=top)
    except Exception:  # noqa: BLE001
        logger.exception("取资源库关键词失败")
        return []
    kws: list[str] = []
    for r in rows:
        titles = r.get("titles") or []
        if titles and str(titles[0]).strip():
            word = library_search_word(str(titles[0]))
            if word and word not in kws:
                kws.append(word)
    return kws[:top]


def _save(session: Session, user_id: int, platform: str, hit: dict, keyword: str) -> bool:
    """入库(按 user+platform+uid 去重);返回是否新增。"""
    exists = session.scalar(select(CrossPlatformAccount.id).where(
        CrossPlatformAccount.user_id == user_id,
        CrossPlatformAccount.platform == platform,
        CrossPlatformAccount.uid == hit["uid"]).limit(1))
    if exists:
        return False
    session.add(CrossPlatformAccount(
        user_id=user_id, platform=platform, uid=hit["uid"][:64], name=hit["name"][:128],
        url=str(hit.get("url") or "")[:500], hit_keyword=keyword[:128],
        snippet=str(hit.get("snippet") or "")[:255],
        pan_link=str(hit.get("pan_link") or "")[:500]))
    # 立刻 flush(不依赖 autoflush):生产的 session 是 autoflush=False,而**同一轮里多个
    # 搜索词常命中同一个号**(如"夸克网盘资源"在"网盘资源"和"夸克网盘"两次搜索里都出现),
    # 不落库的话下一次查重看不到它 → 重复 add → commit 时唯一键冲突 → **整轮白跑**
    # (2026-10-02 实跑撞到,测试用 autoflush=False 的 session 复现)。
    session.flush()
    return True


def list_cross_accounts(session: Session, user_id: int, platform: str = "") -> list[dict]:
    """跨平台对标号列表(新→旧)。"""
    stmt = select(CrossPlatformAccount).where(CrossPlatformAccount.user_id == user_id)
    if platform:
        stmt = stmt.where(CrossPlatformAccount.platform == platform)
    rows = session.scalars(stmt.order_by(CrossPlatformAccount.id.desc()).limit(200)).all()
    return [{"id": r.id, "platform": r.platform, "uid": r.uid, "name": r.name, "url": r.url,
             "hit_keyword": r.hit_keyword, "snippet": r.snippet, "pan_link": r.pan_link,
             "status": r.status,
             "discovered_at": r.discovered_at.isoformat(sep=" ", timespec="seconds")}
            for r in rows]


def cross_account_tick(settings=None) -> int:
    """每日定时:为所有配了目标平台 Cookie 的用户跑一轮跨平台发现。返回新增数。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    if not getattr(settings, "cross_discover_enabled", True):
        return 0
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            from app.services.tenant_base import _record_run

            try:
                out = discover_cross_accounts(db, uid, settings=settings)
                total += out.get("new", 0)
                note = (f"命中{out.get('found', 0)} 新增{out.get('new', 0)}"
                        + (f" 失败{out['failed']}" if out.get("failed") else ""))
                _record_run(db, uid, "cross_account_discover", "success", note)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("跨平台发现失败 user=%s", uid)
                _record_run(db, uid, "cross_account_discover", "failed", str(exc)[:200])
                db.commit()
    finally:
        db.close()
    return total
