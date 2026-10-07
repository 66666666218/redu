# -*- coding: utf-8 -*-
"""小红书:**纯协议**(2026-10-07,替换 MediaCrawler 与页面渲染)。

链路拆解、算法、以及"**停更了自己怎么修**"都在
`doc/小红书纯协议-链路拆解.md` —— 读它比读这个文件重要。

## 一句话
`xhshow` 算出 `x-s` / `x-s-common`,我们把它连同 **Cookie 头**发出去直连
`edith.xiaohongshu.com`。实测 4 词 × 2 页全部 HTTP 200、**~0.85 秒/请求**
(浏览器那条路 ~15 秒/词)。

## ⚠️ 三条纪律
1. **"没登录" 与 "没搜到" 必须分开**(本仓反复栽的假阴性):
   `code:-101 无登录信息` / `300011 账号异常` ⇒ **抛错**,绝不返回空列表。
2. **一次搜索的所有页共用一个 `search_id`** —— 2026-10-07 控制变量实测:
   同 id 翻两页交集 **0**(真在翻);每页各新生成交集 **5**(静默重复)。
3. **凭据来自加密 cookie 库**(`tools/xhs_export_cookie.py` 从浏览器档案解密导出)。
   手抄那份实测**丢了 `id_token`**,站点不认 ⇒ **纯协议的是"采集",不是"登录"**。

## 与页面渲染那条路的关系
`app/services/xhs_page_source.py` **保留作降级兜底** ——
`xhshow` 是第三方签名库(且 PyPI 上 `license: None`),它哪天跟不上小红书改版,
页面渲染还能顶上(慢、脆,但不需要签名)。
"""
from __future__ import annotations

import json
import time

from app.utils import get_logger

logger = get_logger(__name__)

API_BASE = "https://edith.xiaohongshu.com"
URI_SEARCH = "/api/sns/web/v1/search/notes"
PLATFORM = "xiaohongshu"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
#: 逐词之间的间隔。实测连发 8 次没问题,但没必要贴着上限跑。
_GAP = 1.2
#: 单页条数(接口实测稳定给 20~22)
PAGE_SIZE = 20


class XhsProtocolError(Exception):
    """纯协议硬失败。`kind` 说清是哪一种,**`needs_human` 表示要人动手**。"""

    def __init__(self, msg: str, kind: str = "api", needs_human: bool = False) -> None:
        super().__init__(msg)
        self.kind = kind
        self.needs_human = needs_human


def _cookie(session=None, user_id: int = 1, settings=None) -> str:
    """登录态:优先**加密 cookie 库**,回落 `.env` 的 `xhs_web_session`。

    ⚠️ 只有 `web_session` 是**不够**的 —— 实测手抄那份(只有 web_session/a1/webId)
    站点回 `-101 无登录信息`;从浏览器档案 `ctx.cookies()` **解密**读出来的全套才行
    (关键是 `id_token`)。所以缺它时要**明确报错 + 给修法**,别让它表现成"没热搜"。
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
            logger.debug("读小红书 cookie 失败(回落 .env)", exc_info=True)
    if not blob:
        blob = (getattr(settings, "xhs_web_session", "") or "").strip()
    return blob


def _cookie_dict(cookie: str) -> dict:
    """`a=1; b=2` → dict(签名与 `Cookie` 头都要用)。"""
    out: dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def needs_login(text: str) -> bool:
    """响应/页面里是不是"**没登录/被踢**"(而不是"没搜到")。

    ⚠️ 这几句都要认:`-101 无登录信息`、`300011 检测到账号异常`、
    `电脑设备登录超限,请重新登录`(被踢)。
    """
    t = str(text or "")
    return ("无登录信息" in t or "登录信息为空" in t or "-101" in t
            or "300011" in t or "检测到账号异常" in t or "请重新登录" in t
            or "登录超限" in t)


def search(keywords: list[str], settings=None, session=None, user_id: int = 1,
           pages: int = 1) -> list[dict]:
    """按关键词搜小红书。返回 `{uid, name, url, snippet, pan_link, keyword}`。

    形状与 `mediacrawler_source.crawl` 一致,上层(`resource_presence.probe`)不用改。
    """
    import requests
    from xhshow import Xhshow

    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return []
    ck_str = _cookie(session, user_id, settings)
    if not ck_str:
        raise XhsProtocolError(
            "没有小红书凭据 —— 跑 `python tools/xhs_export_cookie.py` 从浏览器档案导出"
            "(只导一次,之后纯协议)", kind="need_login", needs_human=True)
    ck = _cookie_dict(ck_str)
    if "a1" not in ck or "web_session" not in ck:
        # ⚠️ 硬失败**只认这两项**:`a1` 是设备标识(`x-s-common` 的 x5 就是它),
        # `web_session` 是登录态本体 —— 缺哪个都必然不行。
        raise XhsProtocolError(
            f"小红书凭据不全(缺 {'a1' if 'a1' not in ck else 'web_session'})—— "
            f"必须从浏览器档案导出全套,别手抄", kind="need_login", needs_human=True)
    if "id_token" not in ck:
        # ⚠️ **只是告警,不当硬失败**:手抄那份缺 `id_token` 且回 `-101`,
        # 但那份**同时可能已经过期** ⇒ 把 -101 单独归因给 `id_token` 证据不够硬。
        # 记一条日志 + 导出工具会提示,让它自己去失败、报真实原因。
        logger.warning("小红书凭据里没有 `id_token`(手抄的常见缺它)—— "
                       "先用着,若回 -101 就用 tools/xhs_export_cookie.py 重新导出")

    x = Xhshow()
    rows: list[dict] = []
    for i, kw in enumerate(kws):
        if i:
            time.sleep(_GAP)
        # ★ **一次搜索共用一个 search_id**(控制变量实测:每次新生成会让翻页混进重复)
        sid = x.get_search_id()
        for page in range(1, max(1, pages) + 1):
            payload = {"keyword": kw, "page": page, "page_size": PAGE_SIZE,
                       "search_id": sid, "sort": "general", "note_type": 0,
                       "ext_flags": [], "image_formats": ["jpg", "webp", "avif"]}
            try:
                headers = x.sign_headers_post(URI_SEARCH, ck, payload=payload)
            except Exception as exc:  # noqa: BLE001 - 签名库自己炸了要说清是哪一层
                raise XhsProtocolError(f"算签名失败(xhshow):{type(exc).__name__}: "
                                       f"{str(exc)[:90]}", kind="sign") from exc
            # ⚠️ **`xhshow` 不会替你设 `Cookie` 头** —— 少了它签名照样被接受(HTTP 200、
            # 没有 461),但接口回 `-101 无登录信息`。这个坑极有欺骗性,写在这里。
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in ck.items())
            headers["content-type"] = "application/json;charset=UTF-8"
            headers["user-agent"] = _UA
            try:
                r = requests.post(API_BASE + URI_SEARCH, data=json.dumps(payload),
                                  headers=headers, timeout=25)
            except Exception as exc:  # noqa: BLE001
                raise XhsProtocolError(f"请求失败:{type(exc).__name__}: {str(exc)[:90]}",
                                       kind="network") from exc
            if r.status_code == 461:
                raise XhsProtocolError(
                    "**HTTP 461** —— 签名/指纹没通过(不是「没搜到」)。"
                    "照 `doc/小红书纯协议-链路拆解.md` §5 先比对签名常量", kind="sign")
            if r.status_code == 406:
                raise XhsProtocolError(
                    "**HTTP 406** —— 这个接口要 `XYW_` 格式(不是 `XYS_`)。"
                    "同一个文档 §2 写了第二种格式", kind="sign")
            if r.status_code != 200:
                raise XhsProtocolError(f"HTTP {r.status_code}:{r.text[:120]}", kind="api")
            try:
                d = r.json()
            except ValueError as exc:
                raise XhsProtocolError(f"响应不是 JSON(多半被挡):{r.text[:120]}",
                                       kind="api") from exc
            if needs_login(r.text[:400] + str(d.get("msg") or "")):
                raise XhsProtocolError(
                    f"小红书登录态失效/被踢({d.get('msg') or d.get('code')})—— "
                    f"重新导一次凭据:`python tools/xhs_export_cookie.py`",
                    kind="need_login", needs_human=True)
            if not d.get("success"):
                raise XhsProtocolError(f"接口不成功:code={d.get('code')} "
                                       f"msg={str(d.get('msg'))[:80]}", kind="api")
            items = ((d.get("data") or {}).get("items") or [])
            for it in items:
                nc = it.get("note_card") or {}
                title = str(nc.get("display_title") or "").strip()
                if not title:
                    continue
                user = nc.get("user") or {}
                rows.append({"uid": str(user.get("user_id") or ""),
                             "name": str(user.get("nickname") or "")[:60],
                             "url": f"https://www.xiaohongshu.com/explore/{it.get('id')}",
                             "snippet": title[:255], "pan_link": "", "keyword": kw})
            logger.info("小红书纯协议:「%s」第 %d 页 +%d 条", kw[:24], page, len(items))
            if not items:
                break
    return rows
