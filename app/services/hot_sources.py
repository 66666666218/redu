# -*- coding: utf-8 -*-
"""热榜源契约与实现(2026-10-01,v2.2.0「命门自持」架构)。

**架构原则(用户要求:命门不掌握在别人手里)**:
    ① 契约层(自持):本模块定义统一热榜源接口,所有源走同一契约——换实现不动调用方;
    ② 实现层(双路线):**能直连的自研**(B站/豆瓣等公开 API,零第三方)、
       **难直连的走自部署 newsnow 容器**(127.0.0.1:4444,数据源在我们机器上,
       newsnow 停维也不影响容器运行;它只是"抓取实现"之一,不是命门);
    ③ 资产层:调用方把所有条目写入**我们自己的库**——历史沉淀与第三方无关;
    ④ 保险层:newsnow 源码快照归档(scripts/snapshot_newsnow.sh),自研抓取器永不依赖第三方。

实测(2026-10-01):B站排行 API / 豆瓣电影 JSON 直连 200;知乎热榜 401(需登录态)——归 newsnow 长尾。
"""
from __future__ import annotations
from app.utils.ua import CHROME_WINDOWS  # 统一 UA(见该模块注释)

import html
import json
import re

from curl_cffi import requests as creq

from sqlalchemy import func, select

from app.utils import get_logger
from app.services.hot_time import parse_published   # 发布时刻统一解析(见该模块:6 种时间形状)

logger = get_logger(__name__)

# ⚠️ **必须要求"看起来像标签"**(`<` 后面跟字母或 `/`)——用 `<[^>]*>` 会把
# `标题里有 < 和 > 但没标签` 中间那段**当成标签吃掉**,那就从"清洗"变成"改写内容"了
# ⚠️ 长度上限 **400**(标题本身限 500):第一版写 `{0,80}`,而金十的
# `<a href="https://cdn.jin10.com/...">` **单标签就 130 字符** —— 上线后实测
# 仍有 18 行漏网。**上限卡太小 = 长标签原样进卡片**,这个数是量出来的不是拍的。
_RE_TAG = re.compile(r"</?[a-zA-Z][^>]{0,400}>")
_RE_WS = re.compile(r"\s+")

_UA = CHROME_WINDOWS


class HotSourceError(Exception):
    """热榜源异常(网络/解析/上游变更)。"""


class HotSource:
    """热榜源契约:实现 `fetch()` 返回 [{title, url, extra}]。

    - 自研源类直接打平台公开接口(零第三方项目依赖);
    - NewsnowSource 打自部署容器(长尾平台覆盖)。
    新增源:实现 fetch 后注册进 `SOURCES`。
    """

    id: str = ""

    def fetch(self, limit: int = 30) -> list[dict]:
        raise NotImplementedError


class BilibiliSource(HotSource):
    """B站全站排行榜(官方公开 API,无需鉴权;2026-10-01 实测 200)。

    对"漫剧/影视"方向价值高:动画区/影视区热门的二创素材指向明确。

    ⚠️ **2026-10-04 补上"看业务码"**:B站被风控时返回的是 **HTTP 200 + `code:-352` + 空 list**
    (`-352` = 被风控系统拦下)—— 只 catch 异常的话,这会**悄悄变成"今天榜单是空的"**,
    还被记成 `ok`(远程实测就这么瞒了不知道多久)。本仓反复踩的「静默失败=假成功」,这里又中一次。
    """

    id = "bilibili"

    def fetch(self, limit: int = 30) -> list[dict]:
        try:
            r = creq.get("https://api.bilibili.com/x/web-interface/ranking/v2",
                         params={"rid": "0", "type": "all"},
                         impersonate="chrome", timeout=15,
                         headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/"})
            payload = r.json()
        except Exception as exc:  # noqa: BLE001
            raise HotSourceError(f"B站排行请求失败:{type(exc).__name__}") from exc
        # ⚠️ **业务码要当错误看**:HTTP 200 不等于成功
        code = payload.get("code")
        if code not in (0, None):
            raise HotSourceError(
                f"B站排行被拒:code={code} {str(payload.get('message') or '')[:60]}"
                + ("(风控,按频率触发)" if str(code) in ("-352", "-412") else ""))
        data = payload.get("data") or {}
        out = []
        for i, it in enumerate((data.get("list") or [])[:limit], 1):
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            out.append({
                "rank": i, "title": title,
                "url": str(it.get("short_link_v2") or f"https://www.bilibili.com/video/{it.get('bvid','')}"),
                "extra": f"{it.get('tname') or ''} · 播放{it.get('stat', {}).get('view', 0)}",
                # B站排行榜**自带投稿时刻**(`pubdate`)→ 发布时间有着落(2026-10-10 实测)
                "published_at": parse_published(it.get("pubdate")),
            })
        return out


def _clean_title(s: str) -> str:
    """去掉标题里的 HTML 标签与实体,并压掉多余空白。

    为什么要它:有的源把**正文片段**当标题给(实测 `wallstreetcn-quick` 回的是
    `<p>菲律宾股指日内涨幅扩大至2%。</p>`),不洗就会**原样进推送卡片**。
    ⚠️ 只做"去标签 + 解实体 + 压空白"这类**无损**清洗,不改写内容。
    """
    s = _RE_TAG.sub("", s or "")
    return _RE_WS.sub(" ", html.unescape(s)).strip()


def _pick(node, key: str):
    """按**点路径**取值:`_pick(it, "content.title")`。键为空返回 `None`。

    为什么要它:不少平台把标题**嵌一层**(掘金 `data[].content.title`)。不支持点路径
    就得为每个形状写一个类 —— 38 个平台写 38 个类,那是"静默返回空"的温床。
    """
    if not key or not isinstance(node, dict):
        return None
    cur = node
    for seg in key.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.isdigit():
            cur = cur[int(seg)] if int(seg) < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _dig_titled(payload, title_keys: tuple[str, ...], max_depth: int = 6) -> list[dict]:
    """在**深嵌套**的返回体里找"最长的那串带标题的字典"(各平台外壳形状不一,别硬编路径)。

    ⚠️ **别按记忆写路径**:上游随手加一层 `data`/`result` 就全空,而空会被读成
    "今天没热点"(本仓的老毛病)。这里用"找最长 list-of-dict 且含标题键"的启发式,
    但仍要求**至少 5 条**才算数 —— 太少说明找错了层。
    """
    best: list[dict] = []

    def walk(node, depth: int) -> None:
        nonlocal best
        if depth > max_depth:
            return
        if isinstance(node, list) and node and isinstance(node[0], dict):
            # 标题键支持点路径(`content.title`)——判断用的样本**多看几个元素**,
            # 有的接口首条是广告位/占位,没有标题字段
            if any(_pick(e, k) for e in node[:3] for k in title_keys) and len(node) > len(best):
                best = node
        if isinstance(node, dict):
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node[:5]:
                walk(v, depth + 1)

    walk(payload, 0)
    return best if len(best) >= 5 else []


class ToutiaoSource(HotSource):
    """头条热榜(公开接口,免登录;2026-10-04 实测 200 / 50 条)。

    为什么自研它:它在 newsnow 长尾里,而**热榜类平台是"命门自持"最该优先搬的**——
    公开、免登录、且对热点选题直接有用。
    """

    id = "toutiao"

    def fetch(self, limit: int = 30) -> list[dict]:
        try:
            r = creq.get("https://www.toutiao.com/hot-event/hot-board/",
                         params={"origin": "toutiao_pc"},
                         impersonate="chrome", timeout=15,
                         headers={"User-Agent": _UA, "Referer": "https://www.toutiao.com/"})
            rows = _dig_titled(r.json(), ("Title", "title"))
        except HotSourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HotSourceError(f"头条热榜请求失败:{type(exc).__name__}") from exc
        out = []
        for i, it in enumerate(rows[:limit], 1):
            title = str(it.get("Title") or it.get("title") or "").strip()
            if not title:
                continue
            hot = it.get("HotValue") or it.get("hot_value") or ""
            out.append({"rank": i, "title": title,
                        "url": str(it.get("Url") or it.get("url") or ""),
                        "extra": f"热度{hot}" if hot else "头条热榜"})
        return out


class TencentNewsSource(HotSource):
    """腾讯新闻热点榜(公开接口,免登录;2026-10-04 实测 200 / 51 条)。"""

    id = "tencent-hot"

    def fetch(self, limit: int = 30) -> list[dict]:
        try:
            r = creq.get("https://r.inews.qq.com/gw/event/hot_ranking_list",
                         params={"page_size": str(max(limit, 30))},
                         impersonate="chrome", timeout=15,
                         headers={"User-Agent": _UA, "Referer": "https://news.qq.com/"})
            rows = _dig_titled(r.json(), ("title",))
        except HotSourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HotSourceError(f"腾讯新闻热榜请求失败:{type(exc).__name__}") from exc
        out = []
        for it in rows:
            title = str(it.get("title") or it.get("Title") or "").strip()
            url = str(it.get("surl") or it.get("url") or "").strip()
            # ⚠️ 返回体**第 0 条是榜单说明**(「腾讯新闻用户最关注的热点,每10分钟更新一次」,
            # `id=TIP…` / `articletype=560` / **没有 surl**)—— 它不是热点条目。
            # 判据用"**有没有可点的链接**"而不是硬编那句标题(标题会变,结构不会)。
            if not title or not url:
                continue
            # ⚠️ **订正一条我自己的误判(2026-10-10)**:我之前记的是"它的 `time` 是不带年份的
            # 人读字符串、不可解析"—— **实测打脸**:`time` 是**完整时刻**(`"2026-10-10 11:23:21"`),
            # 旁边还有个 `timestamp`。所以它当时被截断着塞进 `extra`(一个"标签"字段),
            # 而**真正的列 `published_at` 一直空着** —— 数据在手边,却没放到该放的地方。
            # 现在归位:`extra` 回到稳定标签,时刻进 `published_at`。
            out.append({"rank": len(out) + 1, "title": title, "url": url,
                        "extra": "腾讯新闻热榜",
                        "published_at": parse_published(it.get("time") or it.get("timestamp"))})
            if len(out) >= limit:
                break
        return out


class BilibiliHotSearchSource(HotSource):
    """B站**热搜词**(公开接口,免登录;2026-10-04 实测 200 / 30 条)。

    与已有的 `BilibiliSource`(排行榜)**互补**:排行是"哪些视频火",
    热搜词是"**大家在搜什么**"—— 对选词/选题,后者常常更直接。
    """

    id = "bilibili-hotsearch"

    def fetch(self, limit: int = 30) -> list[dict]:
        try:
            r = creq.get("https://api.bilibili.com/x/web-interface/search/square",
                         params={"limit": str(max(limit, 10))},
                         impersonate="chrome", timeout=15,
                         headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/"})
            payload = r.json()
        except Exception as exc:  # noqa: BLE001
            raise HotSourceError(f"B站热搜请求失败:{type(exc).__name__}") from exc
        # ⚠️ 与 `BilibiliSource` 同一条纪律:**HTTP 200 不等于成功**,业务码要当错误看
        code = payload.get("code")
        if code not in (0, None):
            raise HotSourceError(f"B站热搜被拒:code={code} {str(payload.get('message') or '')[:60]}")
        trending = ((payload.get("data") or {}).get("trending") or {})
        rows = trending.get("list") or []
        out = []
        for i, it in enumerate(rows[:limit], 1):
            kw = str(it.get("keyword") or it.get("show_name") or "").strip()
            if not kw:
                continue
            out.append({"rank": i, "title": kw,
                        "url": f"https://search.bilibili.com/all?keyword={kw}",
                        "extra": f"热搜 · 热度{it.get('heat_score') or 0}"})
        return out


class ZhihuHotSource(HotSource):
    """知乎热榜:优先**自研**(用我们自己的 Cookie),**拿不到 Cookie 就回落 newsnow**。

    ⚠️ **为什么必须回落**(2026-10-04 远程实测踩到的回归):
    知乎热榜裸连 401,原本只能走 newsnow 容器。我给它写了自研版并**从 newsnow 名单里摘掉**,
    但**知乎 Cookie 只在本机**(家宽那条盘链搜索链在用)—— 于是**远程实例的知乎热榜
    从"能用"变成"不能用"**。两实例的库是独立的,**凭据不跟着代码走**。
    ⇒ 所以:有 Cookie 就用自研(命门自持),没有就**回落容器**(那份数据本来就有)。

    ⚠️ 两条都不通时才抛 `HotSourceError` —— **绝不返回空列表冒充"今天没热点"**。
    """

    id = "zhihu"
    _UA = CHROME_WINDOWS

    def _cookie(self) -> str:
        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import UserCookie
        from app.security import decrypt_cookie
        try:
            db = get_session_local()()
        except Exception:  # noqa: BLE001 - 拿不到库就当没有凭据(回落容器)
            return ""
        try:
            row = db.scalar(select(UserCookie).where(UserCookie.platform == "zhihu")
                            .order_by(UserCookie.id.desc()))
            return decrypt_cookie(row.cookie) if row else ""
        except Exception:  # noqa: BLE001
            logger.warning("知乎 Cookie 读取失败,回落 newsnow")
            return ""
        finally:
            db.close()

    def fetch(self, limit: int = 30) -> list[dict]:
        ck = self._cookie()
        if not ck:
            # 回落:本实例没有知乎凭据(它在家宽产生/绑出口 IP),而容器那份是现成的
            logger.info("知乎热榜:本实例没有 Cookie,回落 newsnow 容器")
            try:
                return NewsnowSource("zhihu").fetch(limit)
            except HotSourceError as exc:
                raise HotSourceError(
                    f"知乎热榜两条路都不通:本实例没有 Cookie,newsnow 也不可用({exc})") from exc
        try:
            r = creq.get("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total",
                         params={"limit": str(max(limit, 50))},
                         impersonate="chrome", timeout=15,
                         headers={"User-Agent": self._UA, "Cookie": ck,
                                  "Referer": "https://www.zhihu.com/hot",
                                  "Accept": "application/json"})
            if getattr(r, "status_code", 200) != 200:
                raise HotSourceError(f"知乎热榜 HTTP {r.status_code}(登录态失效或限流)")
            rows = r.json().get("data") or []
        except HotSourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HotSourceError(f"知乎热榜请求失败:{type(exc).__name__}") from exc
        out = []
        for i, it in enumerate(rows[:limit], 1):
            tgt = it.get("target") or {}
            title = str(tgt.get("title") or it.get("title") or "").strip()
            if not title:
                continue
            out.append({"rank": i, "title": title,
                        "url": str(tgt.get("url") or ""),
                        "extra": str(it.get("detail_text") or "知乎热榜")[:60],
                        # ⚠️ 我原本**以为**知乎不给时间,写文档前实测了一遍才没写错:
                        # `target.created` 是 **Unix 秒**(2026-10-10 实测 = 1791605094)。
                        # ⇒ 教训:**"看着没有"不等于"没有"** —— 断言前先打一遍原始返回
                        #   (本仓同类教训:`absence-of-evidence-is-not-evidence-of-absence`)。
                        "published_at": parse_published(tgt.get("created"))})
        return out


class JsonListSource(HotSource):
    """**声明式**热榜源:一个 JSON 接口 + 用"点路径"指出标题列表在哪。

    为什么做成声明式(2026-10-05):用户要「接入 38 个平台」——
    逐个平台手写类既慢又容易写出"**遇到结构变化就静默返回空**"的源(本仓的老毛病)。
    这些平台的形状高度一致(一个接口 → 一串带标题的对象),所以用配置描述,
    共性逻辑集中在一处,出问题只查一处。

    ⚠️ **取不到就抛 `HotSourceError`**,绝不返回空列表 ——
    空会被读成"今天没热点",而事实是"接口变了/被挡了"(同 `BilibiliSource` 看业务码那条纪律)。

    字段:
      `list_path`  —— 从响应里"找那个带标题的列表"的路径;
                      给 `None` 表示**自动找最长的带标题列表**(见 `_dig_titled`);
      `title_key`  —— 标题字段名(可给多个候选);
      `url_key`    —— 链接字段名(可选);
      `hot_key`    —— 热度/描述字段名(可选,只进 `extra`);
      `url_tpl`    —— 链接要拼模板时用(`{v}` 会被替换);
      `time_key`   —— **发布时刻**的字段名(可选,点路径,可给多个按序试;规则见 `hot_time`)。
    """

    def __init__(self, sid: str, url: str, *, title_keys: tuple[str, ...] = ("title",),
                 list_path: str | None = None, url_key: str = "", url_tpl: str = "",
                 hot_key: str = "", headers: dict | None = None, post_json: dict | None = None,
                 extra_label: str = "", ok_codes: tuple = (0, None, "0"),
                 strip_js: bool = False, time_key: str | tuple[str, ...] = "") -> None:
        """`title_keys` / `url_key` / `hot_key` 支持**点路径**(如 `content.title`)——
        很多平台把标题嵌一层(掘金 `data[].content.title`),不支持下就得为它单写一个类。
        `ok_codes`  —— 该平台自己的成功码;华尔街见闻用 **20000** 当 OK,不认它会被当失败。
        `strip_js`   —— 响应是 `var newest = [...]` 这种**JS 赋值**时先剥壳再解析(金十)。
        `time_key`   —— **发布时刻**字段(点路径,**可给多个按序试**)。⚠️ 留空 = 该源
                       **确实没有**时间字段,别去猜一个 —— 猜出来的时间会污染全部新鲜度判断
                       (`hot_time` 的注释)。多个的用法:澎湃的绝对时刻字段万一改名,
                       还能退回相对时间字段(`("publishTime", "pubTime")`)。
        """
        self.id = sid
        self._url = url
        self._title_keys = title_keys
        self._list_path = list_path
        self._url_key = url_key
        self._url_tpl = url_tpl
        self._hot_key = hot_key
        self._headers = headers or {}
        self._post_json = post_json
        self._label = extra_label or sid
        self._ok_codes = ok_codes
        self._strip_js = strip_js
        self._time_key = time_key

    def _rows(self, payload) -> list[dict]:
        if self._list_path is None:
            return _dig_titled(payload, self._title_keys)
        node = payload
        for seg in self._list_path.split("."):
            if isinstance(node, dict):
                node = node.get(seg)
            elif isinstance(node, list) and seg.isdigit():
                node = node[int(seg)] if int(seg) < len(node) else None
            else:
                node = None
            if node is None:
                return []
        return node if isinstance(node, list) else []

    def fetch(self, limit: int = 30) -> list[dict]:
        try:
            h = {"User-Agent": _UA, **self._headers}
            if self._post_json is not None:
                import json as _j
                r = creq.post(self._url, headers={**h, "Content-Type": "application/json"},
                              data=_j.dumps(self._post_json), impersonate="chrome", timeout=15)
            else:
                r = creq.get(self._url, headers=h, impersonate="chrome", timeout=15)
            body = r.text
            if self._strip_js:                     # `var newest = [...]` → 只留数组
                body = body[body.find("["):body.rfind("]") + 1] or "[]"
            import json as _j
            payload = _j.loads(body)
        except Exception as exc:  # noqa: BLE001
            raise HotSourceError(f"{self.id} 请求失败:{type(exc).__name__}") from exc
        # ⚠️ **有些平台失败也回 200**,业务码要当错误看(与 B站那条同源)
        if isinstance(payload, dict):
            code = payload.get("code", payload.get("errno", payload.get("err_no")))
            if code not in self._ok_codes:
                raise HotSourceError(f"{self.id} 接口报错:code={code}")
        rows = self._rows(payload)
        if not rows:
            raise HotSourceError(f"{self.id} 解析不到列表(接口结构可能变了)")
        out = []
        for it in rows:
            if not isinstance(it, dict):
                continue
            title = next((_clean_title(str(_pick(it, k) or "")) for k in self._title_keys
                          if _pick(it, k)), "")
            if not title:
                continue
            link = str(_pick(it, self._url_key) or "") if self._url_key else ""
            if link and self._url_tpl:
                link = self._url_tpl.replace("{v}", link)
            hot = str(_pick(it, self._hot_key) or "") if self._hot_key else ""
            #: **发布时刻**(源自己给的)—— 与 `captured_at`(我们抓到的时刻)是两回事:
            #: 老帖被反复扫到时 `captured_at` 照样是"刚刚",只有它才是新鲜度的真依据。
            #: 拿不到就是 `None`(="不知道"),**不编**。见 `hot_time`。
            pub = None
            for tk in ((self._time_key,) if isinstance(self._time_key, str) else self._time_key):
                if not tk:
                    continue
                pub = parse_published(_pick(it, tk))
                if pub is not None:                  # 多个候选:**第一个解析得出来的就收**
                    break
            out.append({"rank": len(out) + 1, "title": title, "url": link,
                        "extra": f"{self._label} · {hot}"[:60] if hot else self._label,
                        "published_at": pub})
            if len(out) >= limit:
                break
        if not out:
            raise HotSourceError(f"{self.id} 列表里没有可用的标题(字段名可能变了)")
        return out


class RssSource(HotSource):
    """**声明式** RSS/Atom 源(2026-10-05):很多站点仍然给 RSS —— 那是**最稳**的一类接口。

    ⚠️ 只用标准库解析(`xml.etree`),**不引新依赖**。
    ⚠️ 解析不出条目就抛错,不返回空(同上)。

    `url` 可以给**多个**(按顺序试,第一个通就用)—— 这不是过度设计:
    本仓是**跨两个网络**部署的(本机家宽 / 远程 VPS),实测 **同一个站两边可达性相反**
    (`news.ycombinator.com` 远程 0.4s、本机超时;`hnrss.org` 本机通、远程稳定 502)。
    只写一个 URL,就等于**有一侧永远是坏的**。
    """

    def __init__(self, sid: str, url: str | tuple[str, ...], label: str = "") -> None:
        self.id = sid
        self._urls: tuple[str, ...] = (url,) if isinstance(url, str) else tuple(url)
        self._label = label or sid

    def _load(self, url: str):
        import xml.etree.ElementTree as ET

        r = creq.get(url, headers={"User-Agent": _UA}, impersonate="chrome", timeout=15)
        return ET.fromstring(r.content)                           # noqa: S314 - 是我们信任的源

    def fetch(self, limit: int = 30) -> list[dict]:
        root, errs = None, []
        for url in self._urls:                                    # 多镜像:第一个通的就用
            try:
                root = self._load(url)
                break
            except Exception as exc:  # noqa: BLE001
                errs.append(f"{url.split('/')[2]}:{type(exc).__name__}")
        if root is None:
            # ⚠️ 全挂了要把**每个镜像各自的错**都带出来 —— 只报最后一个会掩盖"哪几个镜像坏了"
            raise HotSourceError(f"{self.id} RSS 全镜像失败({', '.join(errs)})")
        items = (root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry"))
        out = []
        for it in items:
            t = _clean_title(
                it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or "")
            if not t:
                continue
            link = (it.findtext("link") or it.findtext("{http://www.w3.org/2005/Atom}link") or "")
            if not link:                                          # Atom 的 link 在属性里
                el = it.find("{http://www.w3.org/2005/Atom}link")
                link = (el.get("href") if el is not None else "") or ""
            #: **发布时刻**:RSS 用 `pubDate`、Atom 用 `published`/`updated` —— 这是**标准字段**,
            #: 所以每个 RSS 源都白拿(实测 aihot/freebuf/chongbuluo/hackernews 四个都有)。
            pub = parse_published(it.findtext("pubDate")
                                  or it.findtext("{http://www.w3.org/2005/Atom}published")
                                  or it.findtext("{http://www.w3.org/2005/Atom}updated"))
            out.append({"rank": len(out) + 1, "title": t, "url": link, "extra": self._label,
                        "published_at": pub})
            if len(out) >= limit:
                break
        if not out:
            raise HotSourceError(f"{self.id} RSS 里没有可解析的条目")
        return out


class DoubanSource(HotSource):
    """豆瓣热门电影(公开 JSON;2026-10-01 实测 200)。

    对"影视/漫剧"方向的选题前瞻:豆瓣热度常领先视频平台 1~2 周。
    """

    id = "douban"

    def fetch(self, limit: int = 30) -> list[dict]:
        try:
            r = creq.get("https://movie.douban.com/j/search_subjects",
                         params={"type": "movie", "tag": "热门", "page_limit": str(min(limit, 50))},
                         impersonate="chrome", timeout=15,
                         headers={"User-Agent": _UA, "Referer": "https://movie.douban.com/"})
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise HotSourceError(f"豆瓣热门请求失败:{type(exc).__name__}") from exc
        out = []
        for i, it in enumerate((data.get("subjects") or [])[:limit], 1):
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            out.append({"rank": i, "title": title, "url": str(it.get("url") or ""),
                        "extra": f"评分{it.get('rate') or '?'}"})
        return out


class NewsnowSource(HotSource):
    """自部署 newsnow 容器源(长尾平台:知乎/微博/虎嗅/掘金 等 40+)。

    数据取自**我们自己的容器**(127.0.0.1:4444),不依赖任何外部托管服务;
    newsnow 项目若停维,容器照常运行(仅平台清单不再更新——届时以自研源补齐核心平台)。
    """

    def __init__(self, platform_id: str, base_url: str = "") -> None:
        self.id = platform_id
        if not base_url:
            from config.settings import get_settings

            base_url = getattr(get_settings(), "hot_newsnow_url", "") or "http://127.0.0.1:4444"
        self.base_url = base_url.rstrip("/")

    def fetch(self, limit: int = 30) -> list[dict]:
        try:
            r = creq.get(f"{self.base_url}/api/s", params={"id": self.id},
                         impersonate="chrome", timeout=20)
            body = r.json()
        except Exception as exc:  # noqa: BLE001
            raise HotSourceError(f"newsnow[{self.id}] 请求失败:{type(exc).__name__}") from exc
        # status: success=实时抓取;cache=容器缓存(数据同为上游真实条目,直接可用);
        # 两者之外才是真异常(2026-10-01 实测:未刷新平台回 cache,判"success only"会误杀)
        if body.get("status") not in ("success", "cache"):
            raise HotSourceError(f"newsnow[{self.id}] 返回异常:{json.dumps(body)[:120]}")
        out = []
        for i, it in enumerate((body.get("items") or [])[:limit], 1):
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            extra = it.get("extra") or {}
            #: 容器条目**可能**带时间(newsnow 常见把相对时间放 `extra.date`,如"3小时前";
            #: 也有源直接给 `timestamp`)。⚠️ **本机验证不了** —— newsnow 容器只跑在**远程那一侧**
            #: (两侧分工见 `SCHEDULER_ROLE`),本机 4444 连不上。所以这里是**尽力而为**:
            #: 给什么解析什么,读不懂就是 `None`(`hot_time` 对不认识的值一律返回 `None`,不猜)。
            pub = parse_published(
                (extra.get("date") if isinstance(extra, dict) else None)
                or it.get("timestamp") or it.get("pubDate") or it.get("date"))
            out.append({
                "rank": i, "title": title,
                "url": str(it.get("url") or it.get("mobileUrl") or ""),
                "extra": str(extra.get("info") or extra.get("hover") or "")[:80]
                if isinstance(extra, dict) else str(extra)[:80],
                "published_at": pub,
            })
        return out


# 源注册表:自研优先(零第三方),newsnow 补长尾。上层只依赖契约,换实现零改动。
SOURCES: dict[str, HotSource] = {
    # ---- 自研直连(命门自持) ----
    "bilibili": BilibiliSource(),
    "bilibili-hotsearch": BilibiliHotSearchSource(),   # 2026-10-04 新增:热搜词(与排行互补)
    "douban": DoubanSource(),
    # 2026-10-04 **从 newsnow 搬过来**(用户口径「完善那 41 个平台监控的链路」+
    # 「不再依赖别人的容器」):这三个都实测过公开/cookie 即可直连 ——
    #   · toutiao     公开免登录 50 条
    #   · tencent-hot 公开免登录 51 条
    #   · zhihu       裸连 401,但**我们本来就有知乎 Cookie**(盘链搜索在用)⇒ 复用即可
    # ⚠️ 搬走的这几个**别再注册回 NewsnowSource** —— 同一个源两条链会重复入库。
    "toutiao": ToutiaoSource(),
    "tencent-hot": TencentNewsSource(),
    "zhihu": ZhihuHotSource(),
    # ---- 声明式直连(2026-10-05,用户口径「先接入 38 个平台」) ----
    # URL 全部抠自 **newsnow 自己的源定义**(`server/sources/*.ts`),不是我猜的。
    # ⚠️ **注册进来 = 实测过**:每个都跑过真实解析,判据是"**解析出 ≥5 条**"而不是
    #    "HTTP 200"(本仓老毛病:200 但 0 条被读成"今天没热点")。
    # ⚠️ 一条源**只能有一条链**:搬过来的这几个**绝不能再留 NewsnowSource**,
    #    否则同一份数据入库两次(微博那条就是这么踩过的)。
    #
    # -- 实测通、已搬离 newsnow --
    # ⚠️ `time_key`(发布时刻字段)**只写实测确认过的** —— 2026-10-10 逐源打了一遍原始返回,
    #    结果见下表。**没写的不是忘了,是实测没有**:`nowcoder` 无任何时间字段;
    #    `juejin` 的 `content.ctime`/`mtime` **恒为 0**(占位值,收下会变成 1970-01-01)。
    #    留空 = 老老实实存 NULL;编一个会污染**全部**新鲜度判断。
    "aihot": RssSource("aihot", "https://aihot.virxact.com/feed/all.xml", "AI热点"),
    "freebuf": RssSource("freebuf", "https://www.freebuf.com/feed", "FreeBuf"),
    "chongbuluo-latest": RssSource(
        "chongbuluo-latest", "https://www.chongbuluo.com/forum.php?mod=rss&view=newthread",
        "虫部落"),
    "producthunt": RssSource("producthunt", "https://www.producthunt.com/feed", "Product Hunt"),
    "juejin": JsonListSource(
        "juejin", "https://api.juejin.cn/content_api/v1/content/article_rank"
                  "?category_id=1&type=hot&spider=0",
        title_keys=("content.title",), list_path="data", extra_label="掘金",
        headers={"Referer": "https://juejin.cn/"}),
    "tieba": JsonListSource(
        "tieba", "https://tieba.baidu.com/hottopic/browse/topicList",
        title_keys=("topic_name", "title"), url_key="topic_url", hot_key="discuss_num",
        time_key="create_time",                       # Unix 秒(实测)
        extra_label="贴吧热榜", headers={"Referer": "https://tieba.baidu.com/"}),
    "thepaper": JsonListSource(
        "thepaper", "https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar",
        title_keys=("name", "title"),
        # ⚠️ 它是 `"2026-10-10 10:38:33"` 这种**绝对**时刻;旁边那个 `pubTime` 是
        #    `"10小时前"`(相对、按抓取时刻算)—— **优先取绝对的**,相对的当兜底。
        time_key=("publishTime", "pubTime"),
        extra_label="澎湃",
        headers={"Referer": "https://www.thepaper.cn/"}),
    "dongqiudi": JsonListSource(
        "dongqiudi", "https://api.dongqiudi.com/app/tabs/web/1.json",
        title_keys=("title",), time_key="published_at",   # 字符串时刻(实测)
        extra_label="懂球帝",
        headers={"Referer": "https://www.dongqiudi.com/"}),
    "nowcoder": JsonListSource(
        "nowcoder", "https://gw-c.nowcoder.com/api/sparta/hot-search/top-hot-pc?size=20",
        title_keys=("title", "content"), hot_key="hotValue", extra_label="牛客",
        headers={"Referer": "https://www.nowcoder.com/"}),
    "jin10": JsonListSource(
        "jin10", "https://www.jin10.com/flash_newest.js",
        title_keys=("data.content", "data.title"), strip_js=True, time_key="time",
        extra_label="金十",
        headers={"Referer": "https://www.jin10.com/"}),
    "sspai": JsonListSource(
        "sspai", "https://sspai.com/api/v1/article/tag/page/get?limit=20&offset=0"
                 "&tag=%E7%83%AD%E9%97%A8%E6%96%87%E7%AB%A0&released=false",
        title_keys=("title",), time_key="released_time",  # Unix 秒(实测)
        extra_label="少数派", headers={"Referer": "https://sspai.com/"}),
    "wallstreetcn-hot": JsonListSource(
        "wallstreetcn-hot", "https://api-one.wallstcn.com/apiv1/content/articles/hot"
                            "?period=all",
        title_keys=("title",), list_path="data.day_items", url_key="uri", hot_key="pageviews",
        time_key="display_time",                      # Unix 秒(实测)
        ok_codes=(20000, 0, None), extra_label="华尔街见闻",
        headers={"Referer": "https://wallstreetcn.com/"}),
    "wallstreetcn-news": JsonListSource(
        "wallstreetcn-news", "https://api-one.wallstcn.com/apiv1/content/information-flow"
                             "?channel=global-channel&accept=article&limit=30",
        title_keys=("resource.title", "resource.content_short"), list_path="data.items",
        url_key="resource.uri", time_key="resource.display_time",   # Unix 秒(实测)
        ok_codes=(20000, 0, None), extra_label="华尔街见闻要闻",
        headers={"Referer": "https://wallstreetcn.com/"}),
    "wallstreetcn-quick": JsonListSource(
        "wallstreetcn-quick", "https://api-one.wallstcn.com/apiv1/content/lives"
                              "?channel=global-channel&limit=30",
        title_keys=("title", "content"), time_key="display_time",   # Unix 秒(实测)
        ok_codes=(20000, 0, None), extra_label="华尔街见闻快讯",
        headers={"Referer": "https://wallstreetcn.com/"}),
    # -- **新接平台**(原本连 newsnow 都没有,48 个口径里多出来的) --
    # ⚠️ **给两个镜像**:实测两边可达性**相反** —— 远程容器打官方 `news.ycombinator.com/rss`
    # 三次全 200/0.4s、打第三方 `hnrss.org` **稳定 502**;本机家宽则反过来(官方超时、
    # hnrss 通)。只写一个 URL 就等于**有一侧永远是坏的**。官方放前面(实际跑它的在远程)。
    "hackernews": RssSource("hackernews", ("https://news.ycombinator.com/rss",
                                           "https://hnrss.org/frontpage?count=30"), "HN"),
    # ---- newsnow 长尾(自部署容器;知乎 401 等无法直连的平台走这里) ----
    # 2026-10-01 扩容:9 个 → 40 个。容器实测支持 **44 个**,除下列之外全接 ——
    #   · `bilibili-*` / `douban`:我们已有**自研直连**(命门自持,不依赖 newsnow)
    #   · `baidu`:已有自研采集通道(`baidu_hot_items` 表)
    #   · `douyin` / `hackernews` / `kaopu` / `mktnews-flash` / `pcbeta-windows11` /
    #     `steam` / `zaobao`:容器当前版本不支持(实测 API 不返回 items),等它升级再说
    # 分类沿用 newsnow 的 column:china 综合热点 / tech 科技 / finance 财经 / world 国际 / sports 体育
    #
    # -- china 综合热点 --
    # ⚠️ **微博不在这里** —— 它已有专门的采集器 `app/services/collector.py`
    # (带 Cookie + 代理 + 退避重试,写 `weibo_hot_items`,而 Agent 的 `_resonance`
    #  正是读那张表)。2026-10-04 复核时发现这里还留着一份 `NewsnowSource("weibo")`
    # —— **同一个源两条链,同一份数据入库两次**,已摘掉。
    "kuaishou": NewsnowSource("kuaishou"),
    "iqiyi": NewsnowSource("iqiyi"),
    "ifeng": NewsnowSource("ifeng"),
    # thepaper / tieba / nowcoder / chongbuluo-latest / freebuf 已搬自研(见上)
    "chongbuluo-hot": NewsnowSource("chongbuluo-hot"),
    "qqvideo-tv-hotsearch": NewsnowSource("qqvideo-tv-hotsearch"),
    # -- tech 科技/资源 --
    "36kr": NewsnowSource("36kr"),
    "36kr-quick": NewsnowSource("36kr-quick"),
    "36kr-renqi": NewsnowSource("36kr-renqi"),
    "ithome": NewsnowSource("ithome"),
    "coolapk": NewsnowSource("coolapk"),
    "github-trending-today": NewsnowSource("github-trending-today"),
    "solidot": NewsnowSource("solidot"),      # 自研 RSS 实测**只剩 1 条**(站点 feed 半废),仍走容器
    # juejin / sspai / producthunt / aihot / freebuf 已搬自研(见上)
    # -- finance 财经 --
    "cls-hot": NewsnowSource("cls-hot"),
    "cls-depth": NewsnowSource("cls-depth"),
    "cls-telegraph": NewsnowSource("cls-telegraph"),   # 自研实测 code=10012(要签名),走容器
    "xueqiu-hotstock": NewsnowSource("xueqiu-hotstock"),  # 自研实测 400(要 cookie),走容器
    "gelonghui": NewsnowSource("gelonghui"),
    "fastbull-express": NewsnowSource("fastbull-express"),
    "fastbull-news": NewsnowSource("fastbull-news"),
    # -- world 国际 --
    "cankaoxiaoxi": NewsnowSource("cankaoxiaoxi"),     # 自研实测 404(端点变了),走容器
    "sputniknewscn": NewsnowSource("sputniknewscn"),
    "steam": NewsnowSource("steam"),
    # -- sports 体育 --
    "hupu": NewsnowSource("hupu"),                     # 自研端点实测是 HTML 不是 JSON,走容器
    # 抖音:newsnow 侧 id 无效(实测),我们已有 douhot 采集通道(写 DouhotWord,
    # 20 分钟一轮带趋势),**不重复注册** —— 同一个源两条链会入库两次
}


def fetch_hot(source_id: str, limit: int = 30) -> list[dict]:
    """统一入口:取某源热榜;未知源抛 HotSourceError。"""
    src = SOURCES.get(source_id)
    if src is None:
        raise HotSourceError(f"未知热榜源:{source_id}(可选:{','.join(sorted(SOURCES))})")
    return src.fetch(limit=limit)


def collect_hot_sources(session, user_id: int, sources: list[str] | None = None,
                        limit: int = 30) -> dict:
    """一轮热榜采集:遍历源取数入库(hot_source_items);单源失败不阻断其余。

    返回 {"ok": n, "failed": n, "items": n}(供 runs detail)。
    """
    from app.db.models import HotSourceItem

    ok = failed = items = 0
    #: 本轮**共享**的批次时刻 —— 所有源、所有行都盖它。
    #: ⚠️ 不共享的话每行走模型默认值 `datetime.now`,**同一批里每行微秒都不同**,
    #: "这一轮采到了什么"就**查不出来**(热榜卡因此被迫去猜,我两版都踩过)。
    from datetime import datetime as _dt

    batch_ts = _dt.now()
    for sid in (sources or list(SOURCES)):
        try:
            rows = fetch_hot(sid, limit=limit)
        except HotSourceError as exc:
            failed += 1
            logger.warning("热榜源[%s]采集失败:%s", sid, str(exc)[:100])
            continue
        for it in rows:
            session.add(HotSourceItem(user_id=user_id, source=sid, rank=int(it.get("rank") or 0),
                                      title=str(it.get("title") or "")[:500],
                                      url=str(it.get("url") or "")[:700],
                                      extra=str(it.get("extra") or "")[:200],
                                      #: **内容自身的发布时刻**(2026-10-10 起真正开始填)。
                                      #: ⚠️ 与 `captured_at` 是两个东西:老帖被反复扫到时,
                                      #: 后者照样是"刚刚",**只有它才是新鲜度的真依据**。
                                      #: 源不给时间就是 `None`(实测 44 个源里多数能给,
                                      #: 少数如 nowcoder/豆瓣 确实没有)—— **不编**。
                                      published_at=it.get("published_at"),
                                      # ⚠️⚠️ **必须传"这一轮的批次时刻"**(2026-10-07 修):
                                      # 不传的话每行走模型默认值 `datetime.now` ⇒
                                      # **同一批里每行微秒都不同**,"这一轮采到了什么"就
                                      # **查不出来**;热榜卡也因此被迫用"全表最新那一刻"
                                      # (只覆盖一批源)或"时间窗"去猜(我两版都踩过)。
                                      captured_at=batch_ts))
        ok += 1
        items += len(rows)
    return {"ok": ok, "failed": failed, "items": items}


def hot_source_tick_all_users(settings=None) -> int:
    """计划任务入口:对全部启用用户跑一轮多平台热榜采集。返回总条目数。

    调度:每小时 05 分(2026-10-01 定档)——热榜条目存活数小时级,每小时一轮
    足够覆盖新上榜;比 douhot_window 的 20 分钟低频,对 newsnow 容器与上游更温和。
    """
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                from app.services.tenant_base import _record_run
                out = collect_hot_sources(db, uid)
                # 资源级爆款检测(2026-10-01):多号突然同发某链 → 即时预警"赶紧跟"
                try:
                    from app.services.resource_library import push_viral_alerts

                    out["viral"] = push_viral_alerts(db, uid, settings)
                except Exception:  # noqa: BLE001 - 预警失败不挡采集
                    logger.exception("爆款预警失败 user=%s", uid)
                # 可选容器探活(每小时顺手):WeRSS/newsnow 挂了要有人知道
                try:
                    from app.services.health import check_optional_containers
                    from app.services.alert_service import notify_incident

                    down = check_optional_containers(settings)
                    if down:
                        notify_incident(db, uid, "wechat",
                                        "🟠 可选源容器不可用:" + "、".join(down),
                                        "列表源/热榜源已自动降级(业务不断),但该容器需人工重启:\n",
                                        "  docker start we-mp-rss   /   docker start newsnow\n",
                                        "(本机 Docker Desktop 开机自启时两种都会自动拉起)",
                                        settings=settings, push_feishu=True)
                        db.commit()
                except Exception:  # noqa: BLE001 - 探活失败不影响采集
                    logger.debug("容器探活失败", exc_info=True)
                _record_run(db, uid, "hot_source",
                            "success" if not out["failed"] else "partial",
                            f"ok={out['ok']} failed={out['failed']} items={out['items']}"
                            + (f" viral={out['viral']}" if out.get("viral") else ""))
                db.commit()
                total += out["items"]
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("热榜采集失败 user=%s", uid)
    finally:
        db.close()
    return total


_PLAT_LABEL = {
    # 自研直连
    "bilibili": "B站", "bilibili-hotsearch": "B站热搜", "douban": "豆瓣",    # china 综合热点
    "zhihu": "知乎", "weibo": "微博", "kuaishou": "快手", "iqiyi": "爱奇艺",
    "toutiao": "今日头条", "ifeng": "凤凰网", "thepaper": "澎湃新闻",
    "tencent-hot": "腾讯新闻", "tieba": "百度贴吧", "nowcoder": "牛客",
    "chongbuluo-hot": "虫部落热帖", "chongbuluo-latest": "虫部落最新",
    "freebuf": "Freebuf", "qqvideo-tv-hotsearch": "腾讯视频",
    # tech 科技/资源
    "36kr": "36氪", "36kr-quick": "36氪快讯", "36kr-renqi": "36氪人气",
    "juejin": "掘金", "ithome": "IT之家", "sspai": "少数派", "coolapk": "酷安",
    "github-trending-today": "GitHub趋势", "producthunt": "Product Hunt",
    "solidot": "Solidot", "aihot": "AI HOT",
    # finance 财经
    "cls-hot": "财联社热门", "cls-depth": "财联社深度", "cls-telegraph": "财联社电报",
    "wallstreetcn-hot": "华尔街见闻热榜", "wallstreetcn-news": "华尔街见闻要闻",
    "wallstreetcn-quick": "华尔街见闻快讯", "xueqiu-hotstock": "雪球热股",
    "jin10": "金十数据", "gelonghui": "格隆汇",
    "fastbull-express": "法布财经快讯", "fastbull-news": "法布财经",
    # world 国际 / sports 体育
    "cankaoxiaoxi": "参考消息", "sputniknewscn": "卫星通讯社", "steam": "Steam",
    "hupu": "虎扑", "dongqiudi": "懂球帝",
    # 2026-10-05 新增(声明式直连,原本连 newsnow 都没有)
    "hackernews": "Hacker News",
}


def push_hot_rank_card_all_users(settings=None, top_n: int | None = None) -> int:
    """多平台热榜速览卡 → **多平台专属群**(未配则回落总群)。

    ⚠️ 去向 **2026-10-06 变更**:原来固定推**总群**(2026-10-01 v2.2.0「新平台接入总群」),
    用户新建了「多平台监控」群并指定这张卡进那儿 ⇒ 改走 `multiplatform` 板块
    (专属群优先、未配回落总群,与其余板块同一条规矩,主群不会因此少收)。

    每日 09:30/21:30 两次(定时),每平台取最新一轮 top N 拼接文本卡;
    与 Agent 选题卡(命中新平台热点时另行推送)互补——本卡是「雷达」,选题卡是「弹药」。
    返回成功发送的群数。

    `top_n` 默认 3(2026-10-01 由 5 下调):平台数已从 11 涨到 **40**,再按每平台 5 条
    拼会撑出两千多行的卡。飞书文本消息本身能到 30KB,但**读的人不会翻那么久** ——
    雷达卡的价值在"一眼扫过各平台在聊什么",细节该去站内看。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    if top_n is None:
        top_n = int(getattr(settings, "hotrank_top_n", 3) or 3)
    from app.services.feishu_client import FeishuClient, webhook_for

    hook = webhook_for(settings, "multiplatform")
    if not hook:
        return 0
    from app.db import get_session_local
    from app.db.models import HotSourceItem, User

    db = get_session_local()()
    lines = ["🔥 多平台热榜速览"]
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            # ⚠️⚠️ **按"每个源自己的最新一轮"取,不能取"全表最新的那一刻"**(2026-10-07 修)。
            # 原来写的是 `captured_at == max(captured_at)`,而**各源是不同作业在不同时刻写的**
            # (hot_source 每小时 :05;百度/微博/抖音各自另算)⇒ 全表最新那一刻只有**那一批**
            # 源在,卡上于是只剩 13 个 —— 用户当场看出来:「不是 38 个平台吗,怎么就这几个」。
            # 实测:库里 **41 个源**,而按老写法只有 13 个进卡。
            # ⚠️⚠️ **`captured_at` 是"每行各自的插入时刻"**(采集时没显式传,走模型默认值
            # `datetime.now`)—— 所以 `captured_at == 某个时刻` **只会中 1 行**。
            # 我第一版按 `== ts` 改,结果卡上**只剩标题、内容全空**(用户当场发现)。
            # ⇒ 正确取法:**每个源取它自己最新"那一批"** —— 同一批是那个源一次循环里
            # 毫秒级连写的(实测同一源的相邻行微秒相差个位数)。
            # 窗口取 5 秒:足够容纳一批,又远小于下一批(小时级)。
            from datetime import timedelta as _td

            per_src = dict(db.execute(
                select(HotSourceItem.source, func.max(HotSourceItem.captured_at))
                .where(HotSourceItem.user_id == uid)
                .group_by(HotSourceItem.source)).all())
            by_src: dict[str, list] = {}
            for src, ts in per_src.items():
                if ts is None:
                    continue
                for _t, rank, extra in db.execute(
                        select(HotSourceItem.title, HotSourceItem.rank, HotSourceItem.extra)
                        .where(HotSourceItem.user_id == uid, HotSourceItem.source == src,
                               HotSourceItem.captured_at >= ts - _td(seconds=5),
                               HotSourceItem.rank <= top_n)
                        .order_by(HotSourceItem.rank)).all():
                    by_src.setdefault(str(src), []).append((rank, _t, extra))
            for src in sorted(by_src, key=lambda s: s not in ("bilibili", "douban")):
                label = _PLAT_LABEL.get(src, src)
                lines.append(f"\n【{label}】")
                for rank, title, extra in by_src[src]:
                    tail = f"({extra[:18]})" if extra else ""
                    lines.append(f"  {rank}. {title[:38]}{tail}")
    finally:
        db.close()
    # ⚠️ **空卡不发**(2026-10-07 实测教训):我改坏判据那次,卡上只剩标题一行 ——
    # 一张只有「🔥 多平台热榜速览」的空卡比不发更糟(读的人以为"今天没热点")。
    # 真有内容才发;没有就说清原因,别让人对着空卡猜。
    if len(lines) <= 1:
        logger.warning("多平台热榜速览:本轮没有任何源的数据,**不发空卡**"
                       "(查 hot_source 是否在采 / 库里有行没有)")
        return 0
    text = "".join(lines)[:8000]
    return 1 if FeishuClient(hook, settings.feishu_secret).send(text) else 0
