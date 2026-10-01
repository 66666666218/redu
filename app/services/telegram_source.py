"""Telegram 频道源(2026-10-01):把公开频道里的分享盘链汇入资源库。

**为什么值得做**:公众号文章是我们**唯一**的资源来源,一个号停了就断一条线。TG 上有一批
专职发资源的公开频道,与公众号是两个独立生态(重合度低),而且抓公开预览页**不需要账号/Cookie**。

**借鉴 CloudSaver 的只有这一处**:它(MIT,9.3k★)的 `Searcher` 抓 `t.me/s/<频道>` 公开页,
用 `.tgme_widget_message_wrap` 遍历消息、`data-post` 取消息号、`js-message_text` 取正文,
而**盘链挂在正文的 `<a href>` 上**——所以链接必须从 href 里捞,只扫纯文本会漏。
转存侧一概不学:它的开源仓停在 V0.2.5,批量/重试/去重都不如我们现有的 quark/baidu 实现。

⚠️ **启用前提**:本机直连 `t.me` 超时,且无本地代理端口(r.jina.ai、RSSHub 公共实例同样不通)。
所以留了 `TG_PROXY` 配置位:能出网的机器(如境外 VPS)把 `TG_ENABLED` 打开即可。
解析逻辑全是纯函数,不联网也能测(见 tests/test_telegram_source.py)。
"""

from __future__ import annotations

import html as html_mod
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User, WechatArticle, WechatPanLink
from app.utils import get_logger

logger = get_logger(__name__)

_BASE = "https://t.me/s/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
# 正文所在 div 的右边界:优先切到 footer(t.me 的固定骨架),切不到再退回第一个 </div>
_TEXT_UNTIL_FOOTER = re.compile(
    r'class="[^"]*js-message_text[^"]*"[^>]*>(.*?)<div class="tgme_widget_message_footer', re.S)
_TEXT_UNTIL_DIV = re.compile(r'class="[^"]*js-message_text[^"]*"[^>]*>(.*?)</div>', re.S)


def _clean_html(fragment: str) -> str:
    """HTML 片段 → 纯文本(`<br>` 当换行,其余标签剥掉,实体反转义)。"""
    text = re.sub(r"<br\s*/?>", "\n", fragment or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[ \t]{2,}", " ", html_mod.unescape(text)).strip()


def _message_text_html(block: str) -> str:
    m = _TEXT_UNTIL_FOOTER.search(block) or _TEXT_UNTIL_DIV.search(block)
    return m.group(1) if m else ""


def _parse_dt(value: str) -> datetime | None:
    """t.me 的 `<time datetime="...">` 是 ISO 8601(带时区),失败就当没有时间。"""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def parse_messages(html: str, limit: int = 30) -> list[dict]:
    """解析 `t.me/s/<频道>` 预览页 → `[{msg_id, text, links, published_at}]`(页面序=时间序)。

    页面结构:t.me 用 `tgme_widget_message_wrap` 包每条消息,消息号在
    `data-post="频道/序号"`,正文在 `js-message_text`,时间是 `<time datetime>`。
    """
    out: list[dict] = []
    for block in re.split(r'class="tgme_widget_message_wrap', html or "")[1:]:
        post = re.search(r'data-post="([^"]+)"', block)
        if not post:
            continue
        ts = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        out.append({
            "msg_id": post.group(1).strip(),
            "text": _clean_html(_message_text_html(block)),
            # 盘链主要在 <a href> 上(CloudSaver 同款做法),纯文本里常只剩个"点击获取"
            "links": [html_mod.unescape(h) for h in re.findall(r'href="(https?://[^"]+)"', block)],
            "published_at": _parse_dt(ts.group(1) if ts else ""),
        })
    return out[-limit:] if limit and len(out) > limit else out


def fetch_channel(channel: str, proxy: str = "", timeout: int = 20, limit: int = 30) -> list[dict]:
    """抓一个频道的公开预览页。`channel` 形如 `xxx` 或 `@xxx`(去掉 @ 后用)。"""
    import requests

    name = (channel or "").strip().lstrip("@")
    if not name:
        return []
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.get(_BASE + name, timeout=timeout, proxies=proxies,
                            headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    except requests.RequestException as exc:
        # 直连不通是本机常态(见模块头注),按可重试的普通失败处理,不炸整轮
        raise RuntimeError(f"Telegram 抓取失败({name}):{exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram 返回 HTTP {resp.status_code}({name})")
    return parse_messages(resp.text or "", limit=limit)


def _pan_urls(msg: dict) -> list[str]:
    """该消息里的网盘分享链(链接与正文合起来提一次,去重保序)。

    经**门面**取这两个工具:直接 `import app.services.wechat._text` 会成环
    (该子模块头部依赖门面,门面又反向重导出它)。
    """
    from app.services.wechat_monitor import _extract_pan_urls

    blob = " ".join([msg.get("text", ""), *msg.get("links", [])])
    return list(dict.fromkeys(_extract_pan_urls("", blob)))


def _store(session: Session, user_id: int, items: list[tuple[str, dict]]) -> int:
    """落库:只留**带盘链**的消息(闲聊/广告进库只会稀释资源库的共振榜)。

    去重键是消息永久链接(`https://t.me/<频道>/<序号>`)——频道页每次抓的都是最近 N 条,
    不做这一步的话每轮都会把同一批消息重写一遍。
    """
    from app.services.wechat_monitor import detect_pan_types

    new = 0
    for channel, msg in items:
        urls = _pan_urls(msg)
        if not urls:
            continue
        url = f"https://t.me/{msg['msg_id']}"
        exists = session.scalar(select(WechatArticle.id).where(
            WechatArticle.user_id == user_id, WechatArticle.url == url).limit(1))
        if exists:
            continue
        text = msg.get("text") or ""
        art = WechatArticle(user_id=user_id, author=f"@{channel}"[:128],
                            title=(text[:200] or url), content=text, url=url,
                            source="telegram", publish_at=msg.get("published_at"),
                            pan_types=",".join(detect_pan_types(" ".join([text, *urls]))),
                            pan_urls="\n".join(urls))
        session.add(art)
        session.flush()   # 需要 art.id 建盘链行
        for u in urls:
            session.add(WechatPanLink(user_id=user_id, article_id=art.id, pan_url=u[:500]))
        new += 1
    session.commit()
    return new


def collect_tick(settings=None, db: Session | None = None) -> dict:
    """抓所有配置频道 → 提盘链 → 入库(每用户一份,与其它采集的口径一致)。"""
    from app.db import get_session_local
    from config.settings import get_settings

    settings = settings or get_settings()
    if not getattr(settings, "tg_enabled", False):
        return {"status": "disabled"}
    channels = [c.strip().lstrip("@") for c in (getattr(settings, "tg_channels", "") or "").split(",")
                if c.strip()]
    if not channels:
        return {"status": "no_channels"}

    own = db is None
    session = db or get_session_local()()
    fetched: list[tuple[str, dict]] = []
    failed: list[str] = []
    try:
        for ch in channels:
            try:
                fetched.extend((ch, m) for m in fetch_channel(
                    ch, proxy=getattr(settings, "tg_proxy", ""),
                    limit=int(getattr(settings, "tg_limit", 30))))
            except Exception:  # noqa: BLE001 - 单个频道失败不影响其余
                failed.append(ch)
                logger.warning("TG 频道抓取失败:%s", ch)
        new = 0
        if fetched:
            users = session.scalars(select(User.id).where(User.enabled.is_(True))).all()
            for uid in users:
                new += _store(session, uid, fetched)
        logger.info("TG 采集完成:频道 %d(失败 %d),消息 %d,入库 %d",
                    len(channels), len(failed), len(fetched), new)
        return {"status": "ok", "channels": len(channels), "failed": failed,
                "messages": len(fetched), "new": new}
    finally:
        if own:
            session.close()
