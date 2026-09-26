"""公众号文章页 HTML → 纯文本:保住超链接目标,并单独抽出「阅读原文」外链。

两处抓取(自抓 mp.weixin 与微信读书转发页)原先都用 `re.sub(r"<[^>]+>", " ", html)` 去标签,
而 `<a href="https://pan.quark.cn/s/xxx">点此保存</a>` 的 URL 就在标签里 —— 一起删掉后,
正文一个字面的盘链都不剩,监听永远识别不到资源(网盘引流文极少把链写成明文)。
同理,很多号把资源链只放在左下角「阅读原文」的跳转地址上,正文里只写"点阅读原文"。
"""
from __future__ import annotations

import html as html_mod
import re
from urllib.parse import unquote

# 微信正文容器:自抓页与微信读书转发页都是这个 id
_JS_CONTENT_RE = re.compile(r'<div[^>]*id="js_content"[^>]*>(.*?)(?:</div>\s*<script|</body>|$)', re.S)
_ANCHOR_RE = re.compile(r'<a\b[^>]*?href="(?P<href>[^"]*)"[^>]*?>(?P<text>.*?)</a>', re.I | re.S)
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.I | re.S)
# 「阅读原文」目标地址:微信写在页面 JS 变量里,旧版另有带 external="true" 的 <a>
_MSG_SOURCE_RE = re.compile(r"""msg_source_url\s*=\s*['"]?((?:https?:)?//[^'"\s;,]+)""", re.I)
_ORIG_ANCHOR_RE = re.compile(
    r'<a[^>]*(?:id="js_article_original_link"|external="true")[^>]*href="([^"]+)"', re.I)


def html_to_text(fragment: str, limit: int = 20000) -> str:
    """去标签得到正文,但把每个超链接的目标 URL 附在锚文本后面,并把脚本整块丢掉。

    limit 默认 20000 字符 ≈ 60KB utf8mb4,刻意压在 MySQL TEXT(65535 字节)列上限内,
    否则监听/同步/AI 改写写回 `WechatArticle.content` 会 DataError 1406。
    """
    if not fragment:
        return ""
    def _keep_link(m: re.Match) -> str:
        href = html_mod.unescape(m.group("href")).strip()
        text = re.sub(r"<[^>]+>", " ", m.group("text"))
        text = html_mod.unescape(re.sub(r"\s{2,}", " ", text)).strip()
        return f"{text} {href}" if href.startswith(("http", "//")) else text

    body = _ANCHOR_RE.sub(_keep_link, fragment)
    body = _SCRIPT_RE.sub(" ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html_mod.unescape(body)
    return re.sub(r"\s{2,}", " ", body).strip()[:limit]


def article_body(html: str, limit: int = 20000) -> str:
    """从整篇文章 HTML 里取正文文本(含链接 URL);找不到正文容器返回空串。

    容器缺失即"拿到的是微信的 JS 壳页"(出口 IP 被风控/需客户端渲染),
    调用方据此判失败;旧实现把整页去掉标签当正文返回,库里就存进十几 KB 的 JS,
    还把"被拦"伪装成"抓到了正文"。
    """
    m = _JS_CONTENT_RE.search(html or "")
    if not m:
        return ""
    return html_to_text(m.group(1), limit=limit)


def original_link_url(html: str) -> str:
    """文章「阅读原文」的目标 URL(取不到返回空串),外链跳转包一层时会还原真链。"""
    if not html:
        return ""
    m = _MSG_SOURCE_RE.search(html) or _ORIG_ANCHOR_RE.search(html)
    if not m:
        return ""
    url = html_mod.unescape(m.group(1).strip())
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http") and "url=" in url and "redirect" in url:
        inner = unquote(url.split("url=", 1)[1].split("&", 1)[0])
        if inner.startswith("//"):
            inner = "https:" + inner
        if inner.startswith("http"):
            url = inner
    return url[:500] if url.startswith("http") else ""
