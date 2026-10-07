# -*- coding: utf-8 -*-
"""抖音:**纯协议**关键词搜索(2026-10-07)。

链路拆解、算法、以及"**停更了自己怎么修**"在 `doc/抖音纯协议-链路拆解.md` ——
读它比读这个文件重要。

## 一句话
按抖音 web 搜索接口的合同拼好 query + 请求头,直连 `www.douyin.com`,拿回
`data[].aweme_info`。签名由 `app/services/douyin_sign/`(f2 移植)提供。

## ⚠️ 与小红书那条路**最大的不同**:搜索可能**根本不需要签名**
MediaCrawler(市面上真在跑的实现)对 `/v1/web/general/search/` 这条接口
**刻意不加 `a_bogus`**(它 `__process_req_params` 里那句是 `if "/v1/web/general/search"
not in uri:` —— **反条件**)。所以:①`use_abogus` 默认 **False**,与那条实测配置一致;
②真正卡人的不是签名,而是请求头 `x-tt-argus` 与 `uifid`(缺了网关直接 403
`Blocked by ArgusSecurityPlugin`)——**这才是用户说的"埋点"**。
⇒ 这一点**尚未由我们实跑证实**(见下),`use_abogus` 做成开关就是留给实跑判定的。

## ★ 2026-10-07 匿名实跑结论(推翻了一个假设,值得单独记)
在**未登录、不带任何账号凭据**下跑的对照实验(`tools/douyin_probe.py`,4 次请求):

| 组 | HTTP | status_code | 响应 |
|---|---|---|---|
| A **不带** `a_bogus` | 200 | 2483 | `请先登录,再继续搜索吧` |
| B **带** `a_bogus` | 200 | 2483 | **一字不差** |

三个结论:
1. **卡点是登录态,不是签名** —— `status_code=2483`。这条接口**匿名搜不了**
   (所以 `_cookie` 的匿名告警是**真的会发生**的,不是理论担忧;错误归类也按它单列
   成 `kind="need_login"`)。
2. **`x-tt-argus` 的 403 没出现** —— 值传常量 `"1"` 就过了网关,**连 `uifid` 都不需要**。
   所以 MediaCrawler 注释里那条"缺 uifid → 403"**当前不成立**(可能只对某些接口/时期)。
   `x-tt-argus` 与 `uifid` 照旧带着(免费且无害),但**它们不是当前的瓶颈**。
3. **`a_bogus` 在这两组上没有产生任何差异。** ⚠️ 但**这不足以断定"搜索不需要签名"**:
   登录闸门在签名之前就拦下了,签名**根本没轮到被校验**。真正的判定要等有了登录态、
   在**登录态下**再跑一次同一对照(见 doc 里的"下一步")。

⇒ 所以 `use_abogus` 默认仍是 `False`(与在跑的 MediaCrawler 一致),但**标注为待复验**。

## ⚠️ 合同的来源
参数表与请求头取自两条**互相独立**的链路:
- **MediaCrawler**(`tools/MediaCrawler`,`--type search` 真在跑)⇒ 接口的**参数与头**;
- **f2**(Apache-2.0,2683⭐)⇒ **签名算法**(见 `app/services/douyin_sign/`)。
`status_code` / `data is None` 那些分支**已由上面那次实跑校准过一部分**
(`_parse` 的字段映射仍只是照响应形状写的,**未做端到端验证** —— 因为匿名拿不到 `data`)。

## ⚠️ 许可红线:参数能抄,**代码不能抄**
MediaCrawler 是 *NON-COMMERCIAL LEARNING LICENSE*,而本项目是商用。
但**接口的端点、参数名、取值是事实**,不是受版权保护的表达 ⇒ 参数表照用没问题;
**它的代码不许复制**。所以 `_webid` 这类小工具是我**按行为重写**的,不是搬过来的。
"""
from __future__ import annotations

import json
import random
import time
import urllib.parse

from app.utils import get_logger

logger = get_logger(__name__)

API_BASE = "https://www.douyin.com"
URI_SEARCH = "/aweme/v1/web/general/search/single/"
PLATFORM = "douyin"

#: 与 `douyin_sign` 里签名用的 UA **必须是同一个** —— 签名把 UA 算进去了,
#: 换了 UA 就等于换了签名,站点那边对不上。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

#: 逐词之间的间隔(秒)。抖音对**未登录**的搜索比小红书宽松,但我们不贴着上限跑 ——
#: 上一次贴着上限跑,代价是把小红书账号打进了限制期(见 memory stress-test-costs-the-account)。
_GAP = 1.5
#: 单页条数。接口实测稳定给 15(MediaCrawler 的 `count` 就是 15)。
PAGE_SIZE = 15
#: MediaCrawler 里写死的分组 id。**它是个会过期的魔数** —— 失效时的表现是
#: 接口照回 200 但 `data` 空,而不是报错,所以排障时要想到它。
_FROM_GROUP_ID = "7378810571505847586"

#: msToken 的字符集(base64url 风格)。
_MS_CHARS = ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-")


class DouyinProtocolError(Exception):
    """纯协议硬失败。`kind` 说清是哪一种,**`needs_human` 表示要人动手**。"""

    def __init__(self, msg: str, kind: str = "api", needs_human: bool = False) -> None:
        super().__init__(msg)
        self.kind = kind
        self.needs_human = needs_human


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------


def _cookie(session=None, user_id: int = 1, settings=None) -> str:
    """登录态:优先**加密 cookie 库**,回落 `.env` 的 `douyin_cookie`。

    ⚠️ **必须有登录态** —— 2026-10-07 匿名实跑实测:不带登录凭据时抖音搜索
    **一律回 `status_code=2483`「请先登录,再继续搜索吧」**(带不带签名都一样),
    见模块 docstring 的对照表。所以这里**不因缺 cookie 就抛错**只是为了让请求发出去、
    由站点自己回答(报出准确的 2483),而不是被我们的提前检查挡成一句含糊的"没配置"。
    """
    if settings is None:
        from config.settings import get_settings
        settings = get_settings()
    blob = ""
    if session is not None:
        try:
            from app.services.cookie_store import get_cookie

            blob = (get_cookie(session, user_id, PLATFORM) or "").strip()
        except Exception:  # noqa: BLE001
            logger.debug("读抖音 cookie 失败(回落 .env)", exc_info=True)
    if not blob:
        blob = (getattr(settings, "douyin_cookie", "") or "").strip()
    return blob


def _cookie_dict(blob: str) -> dict:
    """`a=1; b=2` → dict(取 `uifid`、`msToken` 要用)。"""
    out: dict[str, str] = {}
    for part in str(blob or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def _uifid(ck: dict) -> str:
    """`uifid`(网关要它;缺了 → 403 `Uifid Not Found`)。

    ⚠️ 两个名字都要认:`UIFID` 是正式项,`UIFID_TEMP` 是没登录时的临时项。
    """
    return str(ck.get("UIFID") or ck.get("UIFID_TEMP") or "").strip()


def _webid() -> str:
    """19 位数字的 `webid`(客户端自造的随机设备号)。

    ⚠️ **按行为重写,不是搬 MediaCrawler 的代码** —— 它是 *NON-COMMERCIAL* 许可,
    本项目商用,不能复制其代码。这里只复刻**可观察行为**:19 位十进制。
    抖音不校验它的取值(它本来就是随机的),所以这样足够。
    """
    return "".join(random.choice("0123456789") for _ in range(19))


def _ms_token(ck: dict) -> str:
    """`msToken`:cookie 里有就**用真的**,没有才**造一个假的**。

    假 token 的形状按 f2 的写法:182 个 base64url 字符 + `==`。
    ⚠️ 真有 `msToken` 时**绝不能覆盖成假的** —— 站点会把它和登录态对账。
    """
    real = str(ck.get("msToken") or "").strip()
    if real:
        return real
    return "".join(random.choice(_MS_CHARS) for _ in range(182)) + "=="


# ---------------------------------------------------------------------------
# 请求构造(纯函数,可离线测)
# ---------------------------------------------------------------------------

#: 每次请求都带的"设备指纹"式参数。名字与取值照接口合同,顺序固定
#: —— 顺序一乱,签名(若启用)会跟着变,排查时也难对账。
_COMMON_PARAMS: tuple[tuple[str, str], ...] = (
    ("device_platform", "webapp"),
    ("aid", "6383"),
    ("channel", "channel_pc_web"),
    ("version_code", "190600"),
    ("version_name", "19.6.0"),
    ("update_version_code", "170400"),
    ("pc_client_type", "1"),
    ("cookie_enabled", "true"),
    ("browser_language", "zh-CN"),
    ("browser_platform", "Win32"),
    ("browser_name", "Chrome"),
    ("browser_version", "125.0.0.0"),
    ("browser_online", "true"),
    ("engine_name", "Blink"),
    ("engine_version", "125.0.0.0"),
    ("os_name", "Windows"),
    ("os_version", "10"),
    ("cpu_core_num", "8"),
    ("device_memory", "8"),
    ("platform", "PC"),
    ("screen_width", "2560"),
    ("screen_height", "1440"),
    ("effective_type", "4g"),
    ("round_trip_time", "50"),
)


def build_params(keyword: str, offset: int = 0, search_id: str = "",
                 ms_token: str = "", webid: str = "") -> dict:
    """拼搜索接口的 query(业务参数在前,设备参数在后 —— 与合同一致)。

    `search_id`:首屏传空串;**翻页要用上一屏响应里的 `extra.logid`**
    (MediaCrawler 就是这么翻的)。传错的表现是**每页都拿到同一批**,
    与小红书那条(共用一个 `search_id` 才不重复)正好**相反**,别记混。
    """
    params: dict[str, str] = {
        "search_channel": "aweme_general",
        "enable_history": "1",
        "keyword": keyword,
        "search_source": "tab_search",
        "query_correct_type": "1",
        "is_filter_search": "0",
        "from_group_id": _FROM_GROUP_ID,
        "offset": str(offset),
        "count": str(PAGE_SIZE),
        "need_filter_settings": "1",
        "list_type": "multi",
        "search_id": search_id,
    }
    params.update(_COMMON_PARAMS)
    params["webid"] = webid or _webid()
    params["msToken"] = ms_token
    return params


def build_headers(cookie_blob: str, ck: dict, keyword: str) -> dict:
    """拼请求头。

    ⚠️ **`x-tt-argus` 与 `uifid` 这两个是抖音特有的闸门**(MediaCrawler 的注释:
    缺 `uifid` → 403 `Blocked by ArgusSecurityPlugin Uifid Not Found`;
    补了 `uifid` 但没这个头 → `... Signature Not Found`)。当前网关**不校验取值**,
    传常量即可;哪天开始真校验,就得回到浏览器里让页面自带的 SDK 去补。
    """
    referer = (f"https://www.douyin.com/search/{urllib.parse.quote(keyword)}"
               f"?aid=f594bbd9-a0e2-4651-9319-ebe3cb6298c1&type=general")
    headers = {
        "User-Agent": _UA,
        "Cookie": cookie_blob,
        "Host": "www.douyin.com",
        "Origin": "https://www.douyin.com/",
        "Referer": urllib.parse.quote(referer, safe=":/"),
        "Content-Type": "application/json;charset=UTF-8",
        "x-tt-argus": "1",
    }
    uifid = _uifid(ck)
    if uifid:
        headers["uifid"] = uifid
    return headers


def build_url(params: dict, use_abogus: bool = False) -> str:
    """把 query 拼成完整 url;`use_abogus=True` 时把 `a_bogus` 也算上。

    ⚠️ **默认不加 `a_bogus`** —— 见模块开头:真在跑的 MediaCrawler 对这条接口就是不加。
    要判定到底需不需要,只能实跑两边对比(controlled experiment),**不能靠读代码下结论**。
    """
    query = "&".join(f"{k}={v}" for k, v in params.items())
    if not use_abogus:
        return f"{API_BASE}{URI_SEARCH}?{query}"
    from app.services.douyin_sign import ABogus, BrowserFingerprintGenerator

    fp = BrowserFingerprintGenerator.generate_fingerprint("Chrome")
    signed = ABogus(fp=fp, user_agent=_UA).generate_abogus(params=query)[0]
    return f"{API_BASE}{URI_SEARCH}?{signed}"


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


def _parse(d: dict, keyword: str) -> tuple[list[dict], str]:
    """抖音响应 → 统一记录形状(与 `mediacrawler_source._parse_record` 对齐)。

    返回 `(记录列表, 本页的 logid)`;`logid` 供**翻页**当 `search_id` 用。

    ★ **比 MediaCrawler 那条多拿到的东西**:真实 `author.nickname`。
    教学版把昵称脱敏成了 `籽***`,推送卡片的"作者列"只能显示星号;
    走协议拿到的是**真名**。
    """
    rows: list[dict] = []
    logid = str(((d.get("extra") or {}).get("logid") or ""))
    for item in (d.get("data") or []):
        info = item.get("aweme_info") if isinstance(item, dict) else None
        if not info:
            # 搜索结果里混着 user / live / mix,没有 `aweme_info` 的直接跳过 ——
            # 这不是错误,是我们只要作品那一类。
            continue
        aweme_id = str(info.get("aweme_id") or "")
        desc = str(info.get("desc") or "").strip()
        author = info.get("author") if isinstance(info.get("author"), dict) else {}
        name = str(author.get("nickname") or "").strip()
        sec_uid = str(author.get("sec_uid") or author.get("uid") or "").strip()
        if not aweme_id:
            continue
        stats = info.get("statistics") if isinstance(info.get("statistics"), dict) else {}
        # ⚠️ **拿不到就不放进 metrics**(而不是填 0):填 0 会被读成"没人看",
        # 而"平台没给这个字段"与"确实是 0"是两回事(见 mediacrawler_source._metric_int)。
        # ⚠️ 归一后的键名**必须与 `mediacrawler_source._METRIC_KEYS` 对齐** ——
        # 它们是喂给 `conversion.ALIASES` 的,名字对不上就等于没采到。
        metrics = {}
        for src, dst in (("play_count", "play_count"),
                         ("digg_count", "liked_count"),
                         ("collect_count", "collected_count"),
                         ("comment_count", "comment_count"),
                         ("share_count", "share_count")):
            v = stats.get(src)
            if isinstance(v, int) and v >= 0:
                metrics[dst] = v
        rows.append({
            "uid": sec_uid or aweme_id,
            "name": name,
            # 用**规范链接**(不是接口给的 share_url):`douyin_leads._aweme_id` 的正则
            # 认 `/(video|note)/<id>`,这个形状一定能匹配上;share_url 换域名就断了。
            "url": f"https://www.douyin.com/video/{aweme_id}",
            "snippet": desc[:255],
            "pan_link": "",
            # 兼容字段:下游 `douyin_leads` / 数据库列本来就把它定义成 int,缺了给 0。
            # (与上面 `metrics` 的口径**不同是有意的** —— 那是"平台的原始指标",
            #  这个是"我们表里的老列",它没有 None 这个取值。)
            "share_count": metrics.get("share_count", 0),
            "metrics": metrics,                      # 交给 conversion 算曝光
            "keyword": keyword,
            "publish_at": int(info.get("create_time") or 0),
            "aweme_id": aweme_id,
        })
    return rows, logid


def _is_argus_block(text: str) -> bool:
    """是不是被网关的 ArgusSecurityPlugin 挡了(**这跟"没搜到"完全两回事**)。"""
    return "ArgusSecurityPlugin" in text or "Uifid Not Found" in text


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def search(keywords: list[str], settings=None, session=None, user_id: int = 1,
           pages: int = 1, use_abogus: bool = False) -> list[dict]:
    """按关键词搜抖音。返回 `{uid, name, url, snippet, pan_link, ...}`。

    形状与 `mediacrawler_source.crawl` 一致,上层(`douyin_leads.find_leads`)不用改。
    """
    import requests

    # ⚠️ 不能写成 `[str(k).strip() for k in keywords if str(k).strip()]` ——
    # `str(None)` 是字符串 `"None"`,**非空** ⇒ 一个 `None` 会变成关键词「None」
    # 打出去一次真请求,白烧一次风控输入(这条是测试抓出来的)。
    kws: list[str] = []
    for k in (keywords or []):
        if k is None:
            continue
        s = str(k).strip()
        if s:
            kws.append(s)
    if not kws:
        return []
    blob = _cookie(session, user_id, settings)
    ck = _cookie_dict(blob)
    if not blob or not _uifid(ck):
        # ⚠️ **不抛错,但要留痕**:缺凭据时站点回的是 `status_code=2483`「请先登录」
        #   (2026-10-07 实测**不是** 403)—— 而那是**我们造成的**,不该被读成
        #   "抖音上没人在推资源"。日志留着,排障时一眼能看到根因。
        logger.warning("抖音纯协议:凭据缺失或不含 uifid(cookie 长度 %d)—— "
                       "搜索结果会是 status_code=2483「请先登录」(实测口径),"
                       "别把空结果当成「没搜到」", len(blob))
    ms_token = _ms_token(ck)
    webid = _webid()
    rows: list[dict] = []

    for i, kw in enumerate(kws):
        if i:
            time.sleep(_GAP)
        search_id = ""            # 首屏传空;翻页用上一屏的 logid
        for page in range(1, max(1, pages) + 1):
            params = build_params(kw, offset=(page - 1) * PAGE_SIZE,
                                  search_id=search_id, ms_token=ms_token, webid=webid)
            url = build_url(params, use_abogus=use_abogus)
            headers = build_headers(blob, ck, kw)
            try:
                r = requests.get(url, headers=headers, timeout=25)
            except Exception as exc:  # noqa: BLE001
                raise DouyinProtocolError(
                    f"请求失败:{type(exc).__name__}: {str(exc)[:90]}",
                    kind="network") from exc

            if r.status_code == 403 and _is_argus_block(r.text):
                raise DouyinProtocolError(
                    "**被网关 ArgusSecurityPlugin 挡了**(403)—— 缺 `x-tt-argus` 或 `uifid`。"
                    "凭据要从浏览器导出(`UIFID`),见 doc/抖音纯协议-链路拆解.md §4",
                    kind="argus", needs_human=True)
            if r.status_code != 200:
                raise DouyinProtocolError(
                    f"HTTP {r.status_code}:{r.text[:120]}", kind="api")
            if not r.text or r.text.strip() == "blocked":
                raise DouyinProtocolError(
                    f"响应为空/blocked(通常是参数或凭据不对):{r.text[:120]!r}",
                    kind="blocked", needs_human=True)
            try:
                d = r.json()
            except ValueError as exc:
                raise DouyinProtocolError(
                    f"响应不是 JSON(多半被挡):{r.text[:120]}", kind="api") from exc

            # ★ 2026-10-07 **匿名实跑实测**:抖音搜索对未登录**一律回这个**
            #   (HTTP 200 + status_code=2483 + 「请先登录,再继续搜索吧」,且**不带 `data` 键**)。
            #   ⇒ 必须**先判它**,否则会被下面那条"没有 data 字段"吞掉、报成含糊的"风控"。
            #   诊断错 → 修法就错:这条的修法是**补登录态**,不是改签名、也不是等解封。
            code = d.get("status_code")
            msg = str(d.get("status_msg") or "")
            if code == 2483 or "请先登录" in msg or "先登录" in msg:
                raise DouyinProtocolError(
                    "抖音要求**先登录**(status_code=2483「请先登录,再继续搜索吧」)——"
                    "这条接口**匿名搜不了**,与签名无关(实测:带不带 `a_bogus` 的响应一字不差)。"
                    "修法:准备一个**登录态 cookie**导进加密库,见 doc/抖音纯协议-链路拆解.md §4",
                    kind="need_login", needs_human=True)
            if "data" not in d:
                raise DouyinProtocolError(
                    f"响应里没有 `data` 字段(status_code={code})—— 原文:{str(d)[:160]}",
                    kind="restricted", needs_human=True)
            if code not in (0, None):
                raise DouyinProtocolError(
                    f"接口不成功:status_code={code} msg={str(d.get('status_msg'))[:80]}",
                    kind="api")

            page_rows, logid = _parse(d, kw)
            rows.extend(page_rows)
            logger.info("抖音纯协议:「%s」第 %d 页 +%d 条(logid=%s)",
                        kw[:24], page, len(page_rows), logid[:16] or "-")
            if not page_rows:
                break
            search_id = logid
    return rows
