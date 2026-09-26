"""公众号监听公共工具:盘链识别/正文抓取/元信息解析/质量评估/响应解析。

从 wechat_monitor.py(1300+ 行)按域拆分而来;域间共享的工具函数集中于此。
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime, timedelta

import requests
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import (FeishuAlert, User, WechatArticle, WechatBenchmark, WechatCandidate,
                           WechatPanLink, WechatRewrite, WechatTrafficSample)
from app.services.dajiala_client import DajialaClient, DajialaError, DajialaNoBalance
from app.services.quark_transfer import QuarkAuthError, QuarkError, QuarkTransfer, extract_quark_urls
from app.services.reader_platform_client import PlatformError, ReaderPlatformClient
from app.services.sogou_weixin import search_articles as sogou_search_articles
from app.services.tenant_base import _base, _record_run
from app.services.content_extract import extract_account_refs as _ear
from app.services.early_agent import _md_safe_light
from app.services.weread_client import WereadAuthError, WereadClient, WereadError, build_mp_url
from app.services.feishu_client import is_quiet_hours
from app.utils import get_logger

logger = get_logger(__name__)

# 四家网盘的分享链接特征(判定"带盘链"的唯一标准:比标题关键词可靠)

PAN_PATTERNS = {
    "夸克网盘": re.compile(r"pan\.quark\.cn/s/[0-9a-zA-Z]+"),
    "百度网盘": re.compile(r"pan\.baidu\.com/s/[0-9a-zA-Z_\-]+"),
    "UC网盘": re.compile(r"drive\.uc\.cn/s/[0-9a-zA-Z]+"),
    "迅雷云盘": re.compile(r"pan\.xunlei\.com/s/[0-9a-zA-Z]+"),
}
# 标题粗筛词:命中才值得花一次正文自抓(标题几乎必带盘商词/资源词)
# 2026-09-26 补齐"引流词":飞书卡上一片"—"的根因是这些号把链放在正文/阅读原文里,
# 而标题写的是"入口/地址/自取/模板"而不是"网盘",旧词表直接把它们挡在抓取之外。
TITLE_HINTS = ("夸克", "百度网盘", "百度云", "UC网盘", "UC盘", "迅雷", "阿里云盘",
               "网盘", "资源", "全套", "合集", "分享", "链接", "更新",
               "入口", "地址", "下载", "获取", "自取", "领取", "复制", "保存", "直达",
               "素材", "模板", "线稿", "电子版", "答案", "真题", "教程", "壁纸", "表情包",
               "pdf", "PDF")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")


def detect_pan_types(text: str) -> list[str]:
    """返回文本涉及的网盘类型:优先分享链接特征(确认级),否则退回盘名关键词(标题疑似级)。"""
    if not text:
        return []
    hits = [name for name, pat in PAN_PATTERNS.items() if pat.search(text)]
    if hits:
        return hits
    mapping = (("夸克网盘", ("夸克",)), ("百度网盘", ("百度网盘", "百度云")),
               ("UC网盘", ("UC网盘", "UC盘")), ("迅雷云盘", ("迅雷云盘", "迅雷")))
    return [name for name, kws in mapping if any(k in text for k in kws)]


def title_hits(title: str) -> bool:
    """标题粗筛:是否值得取正文确认。"""
    return any(h in (title or "") for h in TITLE_HINTS)


def _public_get(url: str, timeout: int):
    """GET 用户外链并逐跳重校验公网性,堵住重定向 SSRF。

    `assert_public_url` 只校验首个 URL;而 `requests` 默认 `allow_redirects=True`,
    公开域可用 302 把服务器引到 `http://169.254.169.254/` 等内网。故关掉自动重定向,
    手动逐跳再验。跳内网时抛 UnsafeUrlError,调用方按"拒绝外链"处理。
    """
    from urllib.parse import urljoin

    from app.utils.net import assert_public_url

    current = url
    for _ in range(6):  # 最多跟随 5 跳,防重定向环
        assert_public_url(current)
        resp = requests.get(current, timeout=timeout, headers={"User-Agent": _UA},
                            allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            current = urljoin(current, resp.headers["Location"])
            continue
        return resp
    from app.utils.net import UnsafeUrlError
    raise UnsafeUrlError("重定向次数过多")


def fetch_article_content(url: str, timeout: int = 15) -> str:
    """免费自抓微信文章正文(纯文本,含正文超链接与「阅读原文」的目标 URL)。

    命中风控("环境异常"验证页)或拿不到正文容器时返回空串——调用方应容忍空结果,
    需要兜底时才走 dajiala article_detail(¥0.01/次)。
    """
    if not url:
        return ""
    from app.utils.net import UnsafeUrlError

    try:
        resp = _public_get(url, timeout=timeout)
        text = resp.text or ""
    except (requests.RequestException, UnsafeUrlError) as exc:
        logger.warning("拒绝抓取非公网外链:%s", exc)
        return ""
    if resp.status_code != 200 or "环境异常" in text:
        return ""
    body = _article_text_with_links(text)
    if not body:
        logger.info("正文容器缺失(疑似风控壳页),不入库整页脚本:%s", url[:80])
    return body


def _article_text_with_links(page_html: str, limit: int = 20000) -> str:
    """正文文本 + 「阅读原文」外链;容器缺失返回空串。"""
    from app.utils.html_text import article_body, original_link_url

    body = article_body(page_html, limit=limit)
    if not body:
        return ""
    orig = original_link_url(page_html)
    if orig and orig not in body:
        body = (body + " " + orig).strip()[:limit]
    return body


def extract_article_meta(url: str, timeout: int = 15) -> dict:
    """免费解析文章页元信息:{biz, name, title}(与 xg 同款正则);失败/风控页返回 {}。"""
    from app.utils.net import UnsafeUrlError

    try:
        resp = _public_get(url, timeout=timeout)
        text = resp.text or ""
    except (requests.RequestException, UnsafeUrlError) as exc:
        logger.warning("拒绝解析非公网外链:%s", exc)
        return {}
    if resp.status_code != 200 or "环境异常" in text:
        return {}

    def _match(patterns: list[str]) -> str:
        for pat in patterns:
            m = re.search(pat, text, re.I | re.S)
            if m:
                return html_mod.unescape(m.group(1)).strip()
        return ""

    out = {}
    biz = _match([r'var\s+biz\s*=\s*"([^"]+)"', r'biz:\s*"([^"]+)"', r'__biz=([^&"\s]+)'])
    name = _match([r'id="js_name"[^>]*>\s*([^<]+?)\s*</a>', r'var\s+nickname\s*=\s*"([^"]+)"'])
    title = _match([r'id="activity-name"[^>]*>\s*(?:<span[^>]*>)?\s*([^<]+?)\s*(?:</span>)?\s*</h1>',
                    r'var\s+msg_title\s*=\s*"([^"]+)"'])
    if biz:
        out["biz"] = biz
    if name:
        out["name"] = name
    if title:
        out["title"] = title
    return out


def _platform_client(settings: Settings) -> ReaderPlatformClient | None:
    """读书平台客户端(wewe-rss v2 兼容,免费全量列表);URL/token 未配置返回 None。"""
    if not settings.wechat_reader_platform_url or not settings.wechat_reader_token:
        return None
    return ReaderPlatformClient(settings.wechat_reader_platform_url,
                                token=settings.wechat_reader_token, vid=settings.wechat_reader_vid)


# 虚假宣传/低质量信号:正文含这些词说明资源不是免费直链
_BAIT_PATTERNS = [
    re.compile(r"(?:加|加我|添加|私信|联系)(?:微信|微信好友|我|助手|客服)"),
    re.compile(r"(?:付费|收费|会员|VIP|开通|解锁)[后以]?[再才]?获取"),
    re.compile(r"(?:进群|入群|加群)获取"),
    re.compile(r"(?:扫码|扫描)(?:二维码|关注)[后以]?[获取领取]"),
    re.compile(r"(?:原价|限时|特价|优惠)[¥￥\d]"),
]
_QUALITY_PAN = 3       # 有实际盘链
_QUALITY_RECENT = 1    # 近3天发布
_QUALITY_MULTI = 2     # 多号同发


def assess_quality(content: str, pan_urls: list[str], read_num: int,
                   resonance_cnt: int = 0, days_old: int = 0) -> dict:
    """内容质量评估:盘链确认 / 虚假宣传 / 引流话术 检测。

    返回 {has_pan: bool, is_bait: bool, bait_signals: [...], quality_score: int}。
    quality_score: 0~10,≥6 为高质量(值得跟进),≤2 为低质量(广告/虚假)。
    """
    score = 0
    has_pan = bool(pan_urls)
    bait_signals: list[str] = []

    # ① 盘链确认(+3)
    if has_pan:
        score += _QUALITY_PAN

    # ② 时效(+1)
    if days_old <= 3:
        score += _QUALITY_RECENT

    # ③ 多号同发(+2)
    if resonance_cnt >= 2:
        score += _QUALITY_MULTI

    # ④ 虚假宣传检测:引流话术但无实际盘链
    if not has_pan:
        for pat in _BAIT_PATTERNS:
            m = pat.search(content or "")
            if m:
                bait_signals.append(m.group(0)[:20])
                score = max(0, score - 1)
    # ⑤ 阅读量加分
    if read_num >= 500:
        score += 2
    elif read_num >= 100:
        score += 1

    return {
        "has_pan": has_pan,
        "is_bait": bool(bait_signals) and not has_pan,
        "bait_signals": bait_signals,
        "quality_score": min(score, 10),
    }


def _deep_find(node: object, key: str):  # noqa: ANN201
    """递归找第一个命中键的值(响应字段层级未完全实测,统一防御式取数)。"""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            r = _deep_find(v, key)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _deep_find(v, key)
            if r is not None:
                return r
    return None


_URL_KEYS = ("content_url", "url", "link", "surl")
_TIME_KEYS = ("send_time", "timestamp", "publish_time", "datetime")


def _parse_time(value: object) -> datetime | None:
    """发文字段容错解析:epoch 秒(数字/数字串)或 ISO 字符串。"""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value)
        s = str(value).strip()
        if s.isdigit():
            return datetime.fromtimestamp(int(s))
        # 带 Z/偏移的 ISO 串是 UTC 墙钟,而全库时间戳统一为"服务器本地 naive"
        # (datetime.now());必须先 astimezone() 转本地再抹 tzinfo,否则会把 UTC
        # 当本地存,发布时段×阅读、近 N 天过滤整体偏移一个时区。naive 串 astimezone()
        # 按本地解释、值不变,安全。
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except (ValueError, OSError, TypeError):
        return None


def _extract_articles(node: object, url_keys: tuple[str, ...] = _URL_KEYS) -> list[dict]:
    """防御式抽取文章条目:递归找"带 title + 链接字段"的字典,保持原顺序。"""
    found: list[dict] = []

    def walk(n: object) -> None:
        if isinstance(n, dict):
            title = str(n.get("title") or n.get("Title") or "").strip()
            link = ""
            for k in url_keys:
                v = n.get(k) or n.get(k.capitalize())
                if v:
                    link = str(v).strip()
                    break
            if title and link:
                ts = None
                for k in _TIME_KEYS:
                    if n.get(k) is not None:
                        ts = _parse_time(n[k])
                        if ts:
                            break
                found.append({"title": title, "url": link, "publish_at": ts})
                return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return found


# ---------------------------------------------------------------- 对标号管理
def add_benchmark(session: Session, user_id: int, url: str, nickname: str = "",
                  note: str = "", settings: Settings | None = None) -> dict:
    """贴一篇文章长链即加号(不产生 API 调用);配了 key 时顺手解析昵称/ghid。"""
    settings = settings or get_settings()
    url = (url or "").strip()
    if not url.startswith("http"):
        raise ValueError("请粘贴公众号文章链接(mp.weixin.qq.com/...)")
    dup = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.anchor_url == url))
    if dup:
        raise ValueError("该文章链接对应的对标号已存在")
    ghid = ""
    biz = ""
    # 解析优先级:读书平台(免费)→ 文章页直抓(免费)→ dajiala(付费兜底);失败不挡加号
    plat = _platform_client(settings)
    if plat:
        try:
            mp = plat.resolve_mp(url)
            biz = mp["mp_id"]
            nickname = nickname or mp["name"]
        except PlatformError as exc:
            logger.info("读书平台解析公众号失败:%s", exc)
    if not biz or not nickname:
        meta = extract_article_meta(url)
        biz = biz or meta.get("biz", "")
        nickname = nickname or meta.get("name", "")
    # 付费解析昵称/ghid:走 _dajiala_key 而非全局 settings.dajiala_key。
    # 否则普通用户反复调 add_benchmark 会绕过 2026-09-14 审计确立的"全局 key 仅 admin
    # 可用"隔离,把运营者余额暴露给任意注册用户刷。
    add_key = _dajiala_key(session, user_id, settings)
    if add_key and (not ghid or not nickname):
        try:
            obj = DajialaClient(add_key).post_condition(url)
            ghid = ghid or str(obj.get("ghid") or "")
            nickname = nickname or str(obj.get("nickname") or "")
        except DajialaError as exc:  # noqa: BLE001 - 解析失败不挡加号(key 没余额也允许加)
            logger.info("加号解析昵称/ghid 失败(不影响使用):%s", exc)
    row = WechatBenchmark(user_id=user_id, nickname=(nickname or "未命名").strip()[:128],
                          ghid=ghid, biz=biz[:64], anchor_url=url[:500],
                          note=(note or "").strip()[:255])
    session.add(row)
    session.commit()
    return {"id": row.id, "nickname": row.nickname, "ghid": row.ghid,
            "biz": row.biz, "anchor_url": row.anchor_url}

def list_benchmarks(session: Session, user_id: int) -> list[dict]:
    rows = session.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id).order_by(WechatBenchmark.id.desc())).all()
    out = []
    for r in rows:
        art_count = session.scalar(
            select(WechatArticle.id).where(WechatArticle.user_id == user_id,
                                           WechatArticle.benchmark_id == r.id).limit(1))
        out.append({
            "id": r.id, "nickname": r.nickname, "ghid": r.ghid, "biz": r.biz,
            "weread_book_id": r.weread_book_id, "anchor_url": r.anchor_url,
            "note": r.note, "active": bool(r.active), "miss_count": r.miss_count,
            "last_item_at": r.last_item_at.isoformat(sep=" ", timespec="seconds") if r.last_item_at else None,
            "has_articles": art_count is not None,
        })
    return out

def remove_benchmark(session: Session, user_id: int, benchmark_id: int) -> None:
    """删除对标号并级联清理其关联数据(pan_links/采样点/文章),防止孤儿行堆积。

    用户 API 文案明确"已同步文章保留"指向的是**通用文章库**;
    这里删的是 benchmark_id 直接关联的行——对标号删除后这些数据无法再归属。
    """
    row = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.id == benchmark_id))
    if row is None:
        raise KeyError("对标账号不存在")
    arts = session.scalars(select(WechatArticle.id).where(
        WechatArticle.user_id == user_id, WechatArticle.benchmark_id == benchmark_id)).all()
    if arts:
        session.execute(delete(WechatPanLink).where(WechatPanLink.article_id.in_(arts)))
        session.execute(delete(WechatTrafficSample).where(WechatTrafficSample.article_id.in_(arts)))
        # 改写稿挂在文章下,文章删了不留行就是指向空文章的孤儿(大 Text 永久堆积)
        session.execute(delete(WechatRewrite).where(WechatRewrite.article_id.in_(arts)))
        session.execute(delete(WechatArticle).where(WechatArticle.id.in_(arts)))
    session.delete(row)
    session.commit()

def set_benchmark_active(session: Session, user_id: int, benchmark_id: int, active: bool) -> None:
    row = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.id == benchmark_id))
    if row is None:
        raise KeyError("对标账号不存在")
    row.active = bool(active)
    session.commit()


# ---------------------------------------------------------------- 微信读书(免费源)
def _dajiala_key(session: Session, user_id: int, settings: Settings) -> str:
    """dajiala key:平台内按用户配置(「dajiala」)优先,其次全局 DAJIALA_KEY。

    多租户余额隔离:每个用户用自己的 key,采样消耗各自的余额。
    全局 key 仅 admin 可用——付费接口普通用户可反复触发,回退全局 key 等于
    把运营者余额暴露给任意注册用户刷(2026-09-14 审计)。
    """
    from app.services.cookie_store import get_cookie

    own = (get_cookie(session, user_id, "dajiala") or "").strip()
    if own:
        return own
    user = session.get(User, user_id)
    if user is not None and user.role != "admin":
        return ""  # 普通用户不回退全局 key(防任意注册用户刷运营者余额)
    return (settings.dajiala_key or "").strip()

def _is_privileged(session: Session, user_id: int) -> bool:
    """是否可回退到运营者全局资源(weread Cookie 等)。与 _dajiala_key 同一门控口径。"""
    user = session.get(User, user_id)
    return user is not None and user.role == "admin"

def _quark_cookie(session: Session, user_id: int, settings: Settings) -> str:
    """夸克 Cookie:优先用户在平台内配置的「quark」,其次全局 QUARK_COOKIE。

    与 baidupan 对齐(网盘转存是"往 Cookie 主人的盘里写"的个人动作):
    只挂 .env 全局值时,运营者改一次要重启容器,多用户也没法各用自己的盘。
    """
    from app.services.cookie_store import get_cookie

    return (get_cookie(session, user_id, "quark") or settings.quark_cookie or "").strip()

def _weread_cookie(session: Session, user_id: int, settings: Settings) -> str:
    """微信读书 Cookie:优先用户在平台内配置的「weread」,其次全局 WEREAD_COOKIE。

    这是**监听取数**通道:微信读书账号可查任意公开公众号的文章列表,全局运营者
    Cookie 作为共享抓取凭据是所有租户监听正常工作的基础,故此处允许普通用户回退
    全局。暴露"运营者关注了哪些号"的书架类操作另用 _weread_cookie_for_shelf 收口。
    """
    from app.services.cookie_store import get_cookie

    return (get_cookie(session, user_id, "weread") or settings.weread_cookie or "").strip()

def _weread_cookie_for_shelf(session: Session, user_id: int, settings: Settings) -> str:
    """书架/导入/续期通道用的 Cookie:普通用户**只能用自己的**「weread」。

    与监听通道分开(2026-09-22 审计):
    - `weread_shelf`/`import_benchmarks_from_shelf` 走全局 Cookie 会把运营者账号
      "关注的全部公众号 + bookId"整份返回给任意注册用户(横向信息泄露),import 还会
      把它批量写进调用者的对标号库;
    - `refresh_weread_cookie` 走全局 Cookie 续期后,轮换出的新 skey 经 set_cookie 落到
      调用者自己的 UserCookie 行,而 .env 里的全局旧值被微信读书作废 → 一击打穿运营者
      共享凭据,连带所有依赖全局兜底的租户监听集体失效。
    故这三处不得回退全局,普通用户未自配即视为无 Cookie。
    """
    from app.services.cookie_store import get_cookie

    own = (get_cookie(session, user_id, "weread") or "").strip()
    if own:
        return own
    if not _is_privileged(session, user_id):
        return ""
    return (settings.weread_cookie or "").strip()

def weread_shelf(session: Session, user_id: int, settings: Settings | None = None) -> list[dict]:
    """列出微信读书书架上的公众号(导入预览;需先在微信读书 App 内关注目标号)。"""
    settings = _base(settings)
    cookie = _weread_cookie_for_shelf(session, user_id, settings)
    if not cookie:
        raise ValueError("未配置微信读书 Cookie(平台 Cookie「weread」或 WEREAD_COOKIE)")
    return WereadClient(cookie).shelf()

def import_benchmarks_from_shelf(session: Session, user_id: int,
                                 settings: Settings | None = None) -> dict:
    """微信读书书架一键导入:MP_WXS_* 条目 → 对标号(免费,自动关联 weread_book_id)。"""
    settings = _base(settings)
    cookie = _weread_cookie_for_shelf(session, user_id, settings)
    if not cookie:
        return {"status": "skipped", "reason": "no_cookie"}
    books = WereadClient(cookie).shelf()
    created = updated = 0
    for book in books:
        bid, name = book["book_id"], book["name"]
        row = session.scalar(select(WechatBenchmark).where(
            WechatBenchmark.user_id == user_id,
            or_(WechatBenchmark.weread_book_id == bid,
                WechatBenchmark.nickname == (name or "未命名"))))
        if row is None:
            session.add(WechatBenchmark(user_id=user_id, nickname=(name or "未命名")[:128],
                                        weread_book_id=bid[:64], note="微信读书书架导入"))
            created += 1
        elif not row.weread_book_id:
            row.weread_book_id = bid[:64]
            updated += 1
    session.commit()
    return {"status": "success", "shelf": len(books), "created": created, "updated": updated}

def _cookie_fingerprint(cookie: str) -> str:
    """Cookie 短指纹(前 6 位 md5):告警 key 携带它,换新 Cookie 后冷却自动重置——
    否则新 Cookie 的第一次死亡会被旧 Cookie 时期的同标题告警冷却拦住(2026-09-16 实测)。"""
    import hashlib

    return hashlib.md5(str(cookie or "").encode()).hexdigest()[:6]


_RENEWAL_COOLDOWN_KEY = "weread_renewal_cooldown_{uid}"
_RENEWAL_COOLDOWN_MIN = 120  # renewal 失败后的冷却:实测连续撞会触发微信读书 renewal 频控,
                             # 锁定期内换出的 skey 即刻无效(2026-09-16 凌晨连续失败 3h 的根因)


def _renewal_cooldown_until(session: Session, user_id: int) -> datetime | None:
    from app.db.models import SystemConfig

    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == _RENEWAL_COOLDOWN_KEY.format(uid=user_id)))
    if row and row.value:
        try:
            return datetime.fromisoformat(row.value)
        except ValueError:
            return None
    return None


def refresh_weread_cookie(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    """微信读书 Cookie 续期:长效 wr_rt → 新短效 wr_skey,并回写 Cookie 管理。

    wr_skey 短效且轮换(续期后旧 skey 很快 -2012),故续期成功**必须回写**;
    全局 WEREAD_COOKIE(.env)无法回写文件,统一落到平台内「weread」Cookie
    (读取优先级:平台内 > 全局,下次监听即用新值)。
    renewal 失败后进入 2h 冷却(频控风控锁定期内反复撞只会延长封锁)。
    返回 {status: success|skipped|failed, reason?, verified, cookie?}。
    """
    from app.db.models import SystemConfig
    from app.services.cookie_store import get_cookie, set_cookie

    settings = _base(settings)
    # 冷却优先于 Cookie 检查:锁定期内连解析都不做(避免每分钟监听自救反复撞频控)
    cooldown_until = _renewal_cooldown_until(session, user_id)
    if cooldown_until and datetime.now() < cooldown_until:
        return {"status": "skipped", "reason": "renewal_cooldown",
                "retry_after": cooldown_until.isoformat(sep=" ", timespec="seconds")}
    # 续期只作用于"调用者自己的 Cookie":走全局续期会把轮换出的新 skey 落到调用者
    # 自己行、作废 .env 里的全局值,一击打穿运营者共享凭据(见 _weread_cookie_for_shelf)。
    cookie = _weread_cookie_for_shelf(session, user_id, settings)
    if not cookie:
        return {"status": "skipped", "reason": "no_cookie"}
    if "wr_rt=" not in cookie:
        return {"status": "skipped", "reason": "no_rt"}
    new_cookie = WereadClient(cookie).refresh_skey()
    if not new_cookie:
        # 失败:进入冷却,停止风控锁定期内的反复撞(每分钟监听自救 × 6h tick 会加剧封锁)
        row = session.scalar(select(SystemConfig).where(
            SystemConfig.key == _RENEWAL_COOLDOWN_KEY.format(uid=user_id)))
        until = datetime.now() + timedelta(minutes=_RENEWAL_COOLDOWN_MIN)
        if row:
            row.value = until.isoformat()
        else:
            session.add(SystemConfig(key=_RENEWAL_COOLDOWN_KEY.format(uid=user_id),
                                     value=until.isoformat()))
        session.commit()
        return {"status": "failed", "reason": "renewal_failed"}
    # 先验证再回写:续期后仍 -2012/-2010 说明登录态整体过期(wr_rt 也失效),
    # 此时回写的新值同样无效,不能报"已续期"误导用户——直接失败让用户重新登录。
    try:
        WereadClient(new_cookie).shelf()
    except WereadAuthError as exc:
        # 能换出 skey 但验证即死 → renewal 接口正处于频控期(换出的即刻作废),
        # 与换不出同样需要冷却,否则每分钟监听自救继续撞,延长封锁
        logger.warning("微信读书续期后仍登录失效(用户 %s):%s;进入续期冷却", user_id, exc)
        row = session.scalar(select(SystemConfig).where(
            SystemConfig.key == _RENEWAL_COOLDOWN_KEY.format(uid=user_id)))
        until = datetime.now() + timedelta(minutes=_RENEWAL_COOLDOWN_MIN)
        if row:
            row.value = until.isoformat()
        else:
            session.add(SystemConfig(key=_RENEWAL_COOLDOWN_KEY.format(uid=user_id),
                                     value=until.isoformat()))
        session.commit()
        return {"status": "failed", "reason": "expired"}
    except WereadError as exc:  # 非登录问题(风控/接口异常):保留续期结果但标注未验证
        logger.warning("微信读书续期后书架验证异常(非登录问题,用户 %s):%s", user_id, exc)
        set_cookie(session, user_id, "weread", new_cookie)
        return {"status": "success", "verified": False, "cookie": new_cookie}
    set_cookie(session, user_id, "weread", new_cookie)
    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == _RENEWAL_COOLDOWN_KEY.format(uid=user_id)))
    if row:
        session.delete(row)
    # 打"会话初期"标记:新 Cookie 的 mp/articles 列表接口仅在会话初期可用,
    # 此时自动触发一轮全量 sync 把各对标号停更期间的历史文章补齐
    # ( cover 只出最新一篇,停更号的历史列表平时拿不到——2026-09-18 诊断)
    flag_key = f"weread_fullsync_pending_{user_id}"
    flag = session.scalar(select(SystemConfig).where(SystemConfig.key == flag_key))
    if flag:
        flag.value = datetime.now().isoformat()
    else:
        session.add(SystemConfig(key=flag_key, value=datetime.now().isoformat()))
    session.commit()
    logger.info("微信读书 Cookie 已续期并验证通过(用户 %s),已标记全量补采", user_id)
    return {"status": "success", "verified": True, "cookie": new_cookie}

_RENEWAL_FAIL_TEXT = {
    "renewal_failed": "renewal 换不出新 wr_skey(wr_rt 已失效,或正处于频控锁定期)",
    "expired": "换出了新 wr_skey 但书架验证仍报登录失效(登录态整体过期,需重新扫码)",
    "no_rt": "Cookie 里根本没有 wr_rt,自动续期无从下手(这份 Cookie 十几小时必过期)",
    "exception": "续期流程抛异常(见服务端日志)",
}


def weread_refresh_tick(settings: Settings | None = None) -> int:
    """定时续期:wr_skey 短效且轮换制,有效期内主动换新则永不过期(兜底是 wr_rt,约 30 天)。

    失败(wr_rt 整体过期/续期被拒)必须即时推飞书——否则要等监听断掉才发现,
    用户感知就是"Cookie 过期好快"。提醒自带冷却,不刷屏。
    返回续期成功的账号数;单用户失败不影响其余。
    """
    from app.db import get_session_local
    from app.db.models import User
    from app.services.alert_service import notify_incident

    settings = settings or get_settings()
    db = get_session_local()()
    total = 0
    failed: list[tuple[int, str]] = []
    try:
        users = db.scalars(select(User.id).where(User.enabled.is_(True)).order_by(User.id)).all()
        for uid in users:
            try:
                out = refresh_weread_cookie(db, uid, settings=settings)
                if out.get("status") == "success":
                    total += 1
                elif out.get("status") == "failed":
                    failed.append((uid, str(out.get("reason") or "")))
                elif out.get("reason") == "no_rt":
                    # 缺 wr_rt 是"续期根本没起跑",旧逻辑当 skipped 静默放过 → 运营者以为
                    # 自动续期在守着,实际这份 Cookie 十几小时必死(监听随后断源)。
                    failed.append((uid, "no_rt"))
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("微信读书续期失败 user=%s", uid)
                failed.append((uid, "exception"))
    finally:
        if failed:
            try:
                detail = "; ".join(f"用户{u}:{_RENEWAL_FAIL_TEXT.get(r, r) or '未知原因'}" for u, r in failed)
                from app.services.cookie_store import get_cookie

                fp = _cookie_fingerprint(get_cookie(db, failed[0][0], "weread") or "")
                notify_incident(
                    db, failed[0][0], "wechat",
                    f"🟠 微信读书 Cookie 自动续期失败,请重新复制[{fp}]",
                    f"{detail}。监听即将断源。"
                    "请在浏览器登录 weread.qq.com → F12 → 网络 复制完整 Cookie,"
                    "更新到「Cookie 管理」页 weread 平台。"
                    "粘贴前先确认串里有「wr_rt=」(约 30 天,自动续期只认它;缺它十几小时就死)。"
                    "复制后尽量不要再在该浏览器使用微信读书——浏览器会自己轮换 wr_skey,"
                    "把服务端这份顶失效(这是 Cookie『过期快』的主因)。",
                    settings=settings)
            except Exception:  # noqa: BLE001 - 提醒失败不影响续期结果
                logger.exception("微信读书续期失败提醒推送异常")
        db.close()
    if total:
        logger.info("微信读书 Cookie 定时续期完成:%d 个账号", total)
    return total


# ---------------------------------------------------------------- 监听
def _extract_pan_urls(title: str, content: str) -> list[str]:
    """标题+正文里的夸克/百度分享链(单一事实源:入库时抽一次,历史回填也用同一套)。"""
    from app.services.baidupan_transfer import extract_baidu_urls

    blob = f"{title or ''} {content or ''}"
    return extract_quark_urls(blob) + extract_baidu_urls(blob)


def _backfill_pan_urls(session: Session, user_id: int, limit: int = 100) -> int:
    """回填盘链列:正文里明明有链、`pan_urls` 却是空的文章。

    成因是"百度链提取"晚于这些文章入库(那时只抽夸克),于是它们**永远不进补转存队列**
    (`pan_urls != ""` 是入队条件),飞书卡片上就永远是一根"—"。
    只扫正文含盘链特征的行(纯 SQL 谓词,真没链的文章不会被反复载入),按租户隔离,
    重算 pan_urls/pan_types 并补归一化表;下一轮补转存队列自然接手。返回修复篇数。
    """
    cand = session.scalars(select(WechatArticle).where(
        WechatArticle.user_id == user_id,
        or_(WechatArticle.pan_urls.is_(None), WechatArticle.pan_urls == ""),
        or_(WechatArticle.content.like("%pan.quark.cn/s/%"),
            WechatArticle.content.like("%pan.baidu.com/s/%"))
    ).order_by(WechatArticle.id).limit(max(1, int(limit)))).all()
    fixed = 0
    for r in cand:
        urls = _extract_pan_urls(r.title, r.content or "")
        have = [x.strip() for x in (r.pan_urls or "").splitlines() if x.strip()]
        new = [u for u in urls if u not in have]
        if not new:
            continue
        r.pan_urls = chr(10).join(have + new)[:2000]
        merged = detect_pan_types(f"{r.title} {r.content or ''}")
        types = [t for t in (r.pan_types or "").split(",") if t.strip()]
        r.pan_types = ",".join(types + [t for t in merged if t not in types])[:128]
        for u in new:  # 归一化表按"文章×链接"建行,与 _insert_new_articles 一致
            session.add(WechatPanLink(user_id=r.user_id, article_id=r.id, pan_url=u[:500],
                                      created_at=r.created_at or datetime.now()))
        fixed += 1
    if fixed:
        session.commit()
        logger.info("回填盘链列 %d 篇(正文有链但过去没抽到)", fixed)
    return fixed


def _insert_new_articles(session: Session, user_id: int, benchmark: WechatBenchmark,
                         items: list[dict], source: str, fetch_content: bool = False,
                         content_resolver=None, require_pan: bool = True) -> list[WechatArticle]:
    """按链接去重入库;网盘类型=标题 + (可选)自抓正文 的并集。"""
    existing = {u.rstrip("/").strip() for u in session.scalars(
        select(WechatArticle.url).where(
            WechatArticle.user_id == user_id, WechatArticle.url != "")).all()}
    added: list[WechatArticle] = []
    for it in items:
        url = it["url"]
        title = (it.get("title") or "").strip()
        url_norm = url.rstrip("/").strip()
        if url_norm in existing:
            continue
        if not title:
            continue  # 空标题无价值(无法展示/分析/搜索)
        existing.add(url_norm)
        types = detect_pan_types(title)
        preset_read = int(it.get("read_num") or 0)
        preset_like = int(it.get("like_num") or 0)
        content = ""
        if title_hits(title):
            if content_resolver:  # 免费源注入(微信读书正文);传 url 让实现方能对上本篇 reviewId
                content = content_resolver(it["title"], url) or ""
            elif fetch_content:
                content = fetch_article_content(url)
            if content:  # 自抓成功 → 用正文的链接判定覆盖标题的盘名猜测
                types = detect_pan_types(content) or types
        pan_urls = _extract_pan_urls(title, content)
        if require_pan and not pan_urls and not types:
            continue  # 无盘链 → 不监控
        quality = assess_quality(content, pan_urls, preset_read)
        row = WechatArticle(user_id=user_id, author=(benchmark.nickname or "未命名")[:128],
                            title=title[:500], url=url[:500], content=content,
                            publish_at=it.get("publish_at"), source=source,
                            benchmark_id=benchmark.id, pan_types=",".join(types)[:128],
                            pan_urls=chr(10).join(pan_urls)[:2000],
                            read_num=preset_read, zan_num=preset_like,
                            quality=quality["quality_score"])
        session.add(row)
        added.append(row)
    if added:
        session.flush()  # 拿到自增 id,同步写盘链归一化表(资源共振走索引查询)
        for r in added:
            for u in [x.strip() for x in (r.pan_urls or "").splitlines() if x.strip()]:
                session.add(WechatPanLink(user_id=user_id, article_id=r.id, pan_url=u[:500],
                                          created_at=r.created_at or datetime.now()))
    return added

def _backfill_pan_links(session: Session) -> None:
    """一次性回填:归一化表建表前的旧文章,把 pan_urls 拆分写入 wechat_pan_links。

    触发条件:表为空且存在带盘链的旧文章(个人规模一次回填毫秒~秒级,之后恒跳过)。
    """
    if session.scalar(select(func.count()).select_from(WechatPanLink)) or             not session.scalar(select(func.count()).select_from(WechatArticle).where(
                WechatArticle.pan_urls != "")):
        return
    for r in session.scalars(select(WechatArticle).where(WechatArticle.pan_urls != "")).all():
        for u in [x.strip() for x in (r.pan_urls or "").splitlines() if x.strip()]:
            session.add(WechatPanLink(user_id=r.user_id, article_id=r.id, pan_url=u[:500],
                                      created_at=r.created_at or datetime.now()))
    session.commit()
    logger.info("盘链归一化表已回填历史文章")

def _enrich_new_articles(session: Session, user_id: int, settings: Settings,
                         rows: list[WechatArticle],
                         client: DajialaClient | None, allow_paid: bool = True,
                         run_backfill: bool = True) -> dict[int, list[tuple[str, str, str]]]:
    """新文后处理(推送前):① 即时采样阅读量(¥0.06/篇,上限 wechat_listen_sample_limit);
    ② 夸克转存盘链 → 换自己的分享链并持久化到 `my_pan_urls`。

    `allow_paid=False`(dajiala 余额不足)时连即时采样也跳过,只做免费的夸克转存;
    `run_backfill=False`(同步收尾)跳过历史补转存队列——同步在 HTTP 请求里,自带窗口已经
    限死了本次转存篇数,再叠加队列会让一次点击多打 N 次夸克接口;队列留给定点监听。
    返回 {article_id: [(原链, 我的链, 提取码)]} 供飞书推送;失败回落原链接,绝不阻塞监听。
    """
    replacements: dict[int, list[tuple[str, str, str]]] = {}
    if not rows:
        return replacements
    try:  # 先修"正文有链却抽不到"的历史行,让它们进得了下面的补转存队列
        _backfill_pan_urls(session, user_id)
    except Exception:  # noqa: BLE001 - 回填是锦上添花,不能拖垮本轮监听
        logger.exception("盘链列回填失败 user=%s", user_id)
        session.rollback()
    # 采样兜底:调用方(listen 主循环)通常已备好 client;若为空,必须走 _dajiala_key
    # 而非 settings.dajiala_key 直取——同 2026-09-14 审计确立的租户隔离原则,
    # 否则普通用户的监听仍会白刷运营者余额。
    own_key = "" if client else _dajiala_key(session, user_id, settings)
    if allow_paid and settings.wechat_listen_sample_new and (client or own_key):
        client = client or DajialaClient(own_key)
        session.flush()  # 新文先拿自增 id(采样点外键要用)
        sample_now = datetime.now()
        for r in rows[: max(1, settings.wechat_listen_sample_limit)]:
            try:
                data = client.read_zan_pro(r.url)
            except DajialaNoBalance:
                logger.warning("监听即时采样余额不足(用户 %s)", user_id)
                break
            except DajialaError as exc:
                logger.warning("监听即时采样失败 %s:%s", r.url, exc)
                continue
            _apply_sample(session, user_id, r, data, sample_now)
    quark_ck = _quark_cookie(session, user_id, settings) if settings.pan_transfer_enabled else ""
    if settings.pan_transfer_enabled and not quark_ck and any(
            "pan.quark.cn" in (r.pan_urls or "") for r in rows):
        # 有夸克盘链却没有任何可用 Cookie:不然是整块静默跳过,推送只会一直显示"未转存",
        # 运营者无从知道差哪一步。冷却去重后告警一次,指明配置入口。
        from app.services.alert_service import notify_incident
        notify_incident(session, user_id, "wechat", "夸克盘链未转存(缺夸克 Cookie)",
                        "本轮识别到夸克分享链但无可用 Cookie,故只推原文。转存在"
                        "「Cookie 管理」页配 quark 平台即可(或 .env 的 QUARK_COOKIE),"
                        "保存后下一轮自动生效",
                        settings=settings)
    if settings.pan_transfer_enabled and quark_ck:
        quark = QuarkTransfer(quark_ck, fid_store=settings.quark_fid_store)
        # 盘链级转存去重:同一资源(相同夸克分享链)只转存一次,后续文章复用首篇的
        # 我方分享链——多个对标号发同一资源时,旧逻辑每篇各存一份(浪费空间+成倍风控暴露)。
        reused: dict[str, tuple[str, str]] = {}  # pan_url -> (my_share_url, 提取码)
        # 补转存兜底:历史转存失败(有原链无我链)的文章每轮补 `pan_transfer_backfill_limit` 篇,
        # 让"推送带我的夸克链接"的覆盖率逐渐收敛(同步入库时走的是实时转存,这里补的是当场失败
        # 与该逻辑上线前的旧文)。
        # 排序按 id 升序=队头优先,所以**永久失败的必须当场出队**,否则几篇源已被封的死链
        # 年年霸占配额,后面真正能转的文章永远轮不到(2026-09-22 本机库实测:24 篇 09-15
        # 的文章卡在队头,一周没动过)。
        try:
            backfill = [] if not run_backfill else session.scalars(select(WechatArticle).where(
                WechatArticle.user_id == user_id, WechatArticle.pan_urls != "",
                or_(WechatArticle.my_pan_urls.is_(None), WechatArticle.my_pan_urls == "")
            ).order_by(WechatArticle.id).limit(
                max(1, int(getattr(settings, "pan_transfer_backfill_limit", 8) or 8)))).all()
        except Exception:  # noqa: BLE001 - 兜底失败不影响本轮新文
            backfill = []
        # 按对象身份(而非 `x not in rows`)排除重复:SQLAlchemy 实体的 == 会生成 SQL 表达式
        # 而不是布尔值,放进 in 的判断里语义含混。
        row_ids = {id(x) for x in rows}
        transfer_rows = list(rows) + [x for x in backfill if id(x) not in row_ids]
        for r in transfer_rows:
            dead = False
            for u in [x.strip() for x in (r.pan_urls or "").splitlines() if x.strip()][:3]:
                # ① 批内复用:本轮已转存过该盘链
                if u in reused:
                    share_url, pwd = reused[u]
                else:
                    # ② 历史复用:库中任意文章已把该盘链转存过
                    hist = session.execute(
                        select(WechatArticle.my_pan_urls).join(
                            WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
                        .where(WechatPanLink.pan_url == u,
                               WechatArticle.user_id == user_id,  # 只复用本租户自己转存的链,
                               # 否则会把他人的我网盘分享链当"我方链接"推出去(违背 7610cbe 初衷)
                               WechatArticle.my_pan_urls.isnot(None),
                               WechatArticle.my_pan_urls != "",
                               WechatArticle.id != r.id)
                        .limit(1)).scalar()
                    picked = ""
                    for line in (hist or "").splitlines():
                        line = line.strip()
                        if line.startswith("https://pan.quark.cn/"):
                            picked = line
                            break
                    if picked:
                        m = re.search(r"(?:提取码\s*([0-9A-Za-z]{4}))", picked)
                        reused[u] = (picked.split(" (提取码")[0].strip(), m.group(1) if m else "")
                        logger.info("盘链复用(免重复转存): %s", u[:60])
                        share_url, pwd = reused[u]
                    else:
                        # ③ 真正首次见到该资源:转存
                        try:
                            res = quark.transfer_and_share(u, save_dir=settings.quark_save_dir,
                                                           password=settings.quark_share_password)
                        except QuarkAuthError as exc:
                            logger.error("夸克 Cookie 失效,本轮停止转存:%s", exc)
                            dead = True
                            # 每日保活只探全局 Cookie;按用户配的「quark」死亡此前只有日志,
                            # 运营者看到的是永久的"未转存"。冷却去重后即时告警。
                            from app.services.alert_service import notify_incident
                            notify_incident(session, user_id, "wechat", "夸克 Cookie 已失效,转存停用",
                                            f"{exc}。请浏览器登录 pan.quark.cn 后 F12 复制 Cookie,"
                                            "更新到「Cookie 管理」页的 quark 平台(或 .env 的 QUARK_COOKIE);"
                                            "监听不受影响,仅转存暂停",
                                            settings=settings)
                            break
                        except QuarkError as exc:
                            if "41017" in str(exc):
                                # 资源本来就是我们分享出去的(同行搬运我们的链):
                                # 原链即我方链接,直接收录,推送可点开自己的资源;
                                # my_pan_urls 落值后补转存兜底也不再重试
                                mine = [x for x in (r.my_pan_urls or "").splitlines() if x.strip()]
                                mine.append(u + "(自分享)")
                                r.my_pan_urls = chr(10).join(mine)[:2000]
                                replacements.setdefault(r.id, []).append((u, u, ""))
                                logger.info("盘链为自己分享,直接采用: %s", u[:60])
                                continue
                            # 41031=对方分享被封(源永久失效,重试不可能成功)→ 落一条标记
                            # 让它离开补转存队列;推送仍回落原文,网盘列显示"源失效"。
                            # 其余为真失败(网络/风控),保留空 my_pan_urls 下轮重试。
                            if "41031" in str(exc):
                                mine = [x for x in (r.my_pan_urls or "").splitlines() if x.strip()]
                                mine.append("⚠️源分享已被封(41031),未转存")
                                r.my_pan_urls = chr(10).join(mine)[:2000]
                                logger.info("夸克源分享已失效,出队补转存队列 %s:%s", u, str(exc)[:80])
                                continue
                            logger.warning("夸克转存失败 %s:%s(推送保留原链接)", u, str(exc)[:80])
                            continue
                        reused[u] = (res["share_url"], res["password"])
                        share_url, pwd = res["share_url"], res["password"]
                mine = [x for x in (r.my_pan_urls or "").splitlines() if x.strip()]
                mine.append(share_url + (f" (提取码 {pwd})" if pwd else ""))
                r.my_pan_urls = chr(10).join(mine)[:2000]
                replacements.setdefault(r.id, []).append((u, share_url, pwd))
            if dead:
                break
    # 百度网盘链接: 同样转存+换链(协议与夸克并列;失败回落原文推送)。
    # 独立门控:只看 pan_transfer_enabled + 用户是否配了百度 Cookie,
    # 不再借用 quark_cookie——此前复制粘贴导致"只配百度未配夸克"时百度链永不转存。
    # 2026-09-26 补提醒:缺 Cookie / Cookie 失效此前都只有一行 info 日志(或静默 break),
    # 运营者看到的是永久的"—",与夸克那套"点名告警"不对等。
    if settings.pan_transfer_enabled and any(
            "pan.baidu.com/s/" in (r.pan_urls or "") for r in rows):
        from app.services.alert_service import notify_incident
        from app.services.baidupan_transfer import (BaiduPanAuthError, BaiduPanClient,
                                                    extract_pwd)
        from app.services.cookie_store import get_cookie

        def _baidu_dead_alert(why: str) -> None:
            notify_incident(session, user_id, "wechat", "百度网盘 Cookie 已失效,转存停用",
                            f"{why}。请浏览器登录 pan.baidu.com 后复制含 BDUSS 的 Cookie,"
                            "更新到「Cookie 管理」页的 baidupan 平台(监听不受影响,仅转存暂停)",
                            settings=settings)

        bck = get_cookie(session, user_id, "baidupan") or ""
        if not bck:
            notify_incident(session, user_id, "wechat", "百度盘链未转存(缺百度网盘 Cookie)",
                            "本轮识别到百度分享链但无可用 Cookie,故只推原文。转存在"
                            "「Cookie 管理」页配 baidupan 平台即可(需含 BDUSS),"
                            "保存后下一轮自动生效",
                            settings=settings)
        else:
            baidu_client = BaiduPanClient(bck)
            login_checked = False   # 每轮最多一次 loginStatus 定性,不额外刷接口
            baidu_dead = False
            for r in rows:
                for u in [x.strip() for x in (r.pan_urls or "").splitlines()
                          if x.strip().startswith("https://pan.baidu.com/s/")][:2]:
                    try:
                        # 历史复用: 该百度链本租户已换过则跳过转存(加 user_id 过滤,绝不用别人的链)
                        hist = session.execute(
                            select(WechatArticle.my_pan_urls).join(
                                WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
                            .where(WechatPanLink.pan_url == u,
                                   WechatArticle.user_id == user_id,
                                   WechatArticle.my_pan_urls.isnot(None),
                                   WechatArticle.my_pan_urls.like("%[百度]%"),
                                   WechatArticle.id != r.id).limit(1)).scalar()
                        picked = next((x.strip() for x in (hist or "").splitlines()
                                       if "[百度]" in x and "pan.baidu.com" in x), "")
                        if picked:
                            mine_b = picked
                            share_url_b = picked.split(" (提取码")[0].strip()
                            m = re.search(r"(?:提取码\s*([0-9A-Za-z]{4}))", picked)
                            code_b = m.group(1) if m else ""
                        else:
                            pwd_b = extract_pwd(getattr(r, "content", "") or "", u) or extract_pwd(r.title or "", u)
                            res_b = baidu_client.transfer_and_share(u, password=pwd_b)
                            share_url_b = res_b["share_url"]
                            code_b = res_b.get("password", "") or ""
                            mine_b = f"{share_url_b} (提取码 {code_b}) [百度]" if code_b else f"{share_url_b} [百度]"
                        mine = [x for x in (r.my_pan_urls or "").splitlines() if x.strip()]
                        mine.append(mine_b)
                        r.my_pan_urls = chr(10).join(mine)[:2000]
                        replacements.setdefault(r.id, []).append((u, share_url_b, code_b))
                    except BaiduPanAuthError as exc:
                        logger.error("百度网盘 Cookie 失效,本轮停止百度链转存:%s", exc)
                        _baidu_dead_alert(str(exc))
                        baidu_dead = True
                        break
                    except Exception as exc:  # noqa: BLE001 - 百度链失败不阻断,不触发夸克链路
                        # 转存失败(errno)分不清是"对方链接失效"还是"我方 Cookie 已死",
                        # 而只有 Cookie 死了才需要人介入 → 本轮第一次失败时探一次登录态定性。
                        if not login_checked:
                            login_checked = True
                            try:
                                baidu_client.keepalive()
                            except BaiduPanAuthError as exc2:
                                logger.error("百度网盘 Cookie 失效(转存失败后探出),停止百度链转存:%s", exc2)
                                _baidu_dead_alert("转存连续失败,登录态检查确认 Cookie 已失效")
                                baidu_dead = True
                        logger.info("百度链转存跳过 %s: %s", u[:50], str(exc)[:70])
                if baidu_dead:
                    break
    # 资源级共振:同一盘链在窗口期内被 ≥2 篇文章推送 → 同行网络都在发的确认级爆点资源
    from app.services.feishu import _col_set_row, _md_safe
    from app.services.feishu_client import FeishuClient, webhook_for
    from app.services.alert_service import feishu_alert_gate

    _backfill_pan_links(session)  # 一次性回填归一化表建成前的旧文章盘链
    res_window = datetime.now() - timedelta(hours=settings.wechat_resonance_hours)
    # 批量收集本轮全部盘链 → 一次 GROUP BY 查询各链接的窗口内文章数(替代循环内 N 次 COUNT)
    all_links: list[tuple[str, WechatArticle]] = []
    seen_links: set[str] = set()
    for r in rows:
        for u in [x.strip() for x in (r.pan_urls or "").splitlines() if x.strip()]:
            if u not in seen_links:
                seen_links.add(u)
                all_links.append((u, r))
    res_candidates: list[tuple[str, WechatArticle, int]] = []
    if all_links:
        url_list = [u for u, _ in all_links]
        counts = dict(session.execute(
            select(WechatPanLink.pan_url, func.count(WechatPanLink.id)).where(
                WechatPanLink.pan_url.in_(url_list),
                WechatPanLink.user_id == user_id,  # 只统计本租户监听,冷却门按 user_id 才一致
                WechatPanLink.created_at >= res_window,
            ).group_by(WechatPanLink.pan_url)).all())
        now_res = datetime.now()
        res_cooldown = timedelta(hours=settings.focus_cooldown_hours)
        for u, r in all_links:
            # 本篇的盘链在 `_insert_new_articles` 已同步写入 WechatPanLink(626-633),
            # counts 已经含本篇自身——旧实现又"+1 = 本篇自身" → 双计,每篇新盘链文
            # cnt=2 ≥ 阈值,轮轮误报共振。直接读 DB 计数即可。
            cnt = int(counts.get(u, 0))
            if cnt < 2:
                continue
            # 只读探测冷却(不烧):冷却期内跳过,否则列为候选。
            # 不能在这一步就 feishu_alert_gate——那会在 webhook 未配置(本轮不发送)
            # 或条目落在 focus_max_items 之后(不进卡片)时,把冷却白白烧掉、
            # 该资源下轮又被自身冷却排除而永无出头之日。烧冷却推迟到发送成功后、仅入选项。
            row = session.scalar(select(FeishuAlert).where(
                FeishuAlert.user_id == user_id, FeishuAlert.section == "focus_res",
                FeishuAlert.title == ("res:" + u[:120])[:200]))
            if row and (now_res - row.alerted_at) < res_cooldown:
                continue
            res_candidates.append((u, r, cnt))
    res_hits = res_candidates[: settings.focus_max_items]
    if res_hits:
        webhook = webhook_for(settings, "wechat")
        if webhook:
            # 飞书是给员工看的,推的网盘链必须是"我方转存链",绝不把同行/他人的原始盘链推出去。
            # 本轮 replacements 已含大部分(原链→我的链);缺失者回落历史复用查询,再缺则回落原文。
            my_of_raw: dict[str, tuple[str, str]] = {}
            for reps in replacements.values():
                for orig, share, code in reps:
                    if share:
                        my_of_raw.setdefault(orig, (share, code))

            def _my_pan(raw_u: str) -> tuple[str, str] | None:
                hit = my_of_raw.get(raw_u)
                if hit:
                    return hit
                blobs = session.execute(
                    select(WechatArticle.my_pan_urls).join(
                        WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
                    .where(WechatPanLink.pan_url == raw_u,
                           WechatArticle.user_id == user_id,  # 只认本租户自己转存的链
                           WechatArticle.my_pan_urls.isnot(None),
                           WechatArticle.my_pan_urls != "").limit(5)).scalars().all()
                for blob in blobs:
                    for line in blob.splitlines():
                        line = line.strip()
                        if line.startswith("https://pan.quark.cn/") or "[百度]" in line:
                            m = re.search(r"(?:提取码\s*([0-9A-Za-z]{4}))", line)
                            link = line.split(" (提取码")[0].split(" [百度]")[0].strip()
                            pair = (link, m.group(1) if m else "")
                            my_of_raw[raw_u] = pair
                            return pair
                return None

            elements = [_col_set_row([("**我的分享链**", 5), ("**同发文章数**", 2), ("**示例标题**", 5)], grey=True)]
            for u, r, cnt in res_hits:
                mine = _my_pan(u)
                if mine:
                    link, code = mine
                    kind = "百度" if "pan.baidu.com" in link else "夸克"
                    # 提取码是员工打开资源的关键,单独明文展示,不做长度截断;链接一律我方转存链
                    cell = f"🔴 [{kind}·我的转存]({_md_safe(link)})" + (f" 提取码 {code}" if code else "")
                else:
                    # 本轮没转出我方链(未开转存/转存失败):宁可指向公众号原文,也不推他人盘链
                    cell = "🔴 [未转存·看原文](" + _md_safe(r.url) + ")"
                elements.append(_col_set_row([
                    (cell, 5), (str(cnt) + " 篇", 2), (_md_safe(r.title)[:30], 5)]))
            card = {"config": {"wide_screen_mode": True},
                    "header": {"template": "red", "title": {"tag": "plain_text",
                               "content": "🔴 资源共振 · 多号同发(" + str(len(res_hits)) + " 个资源)"}},
                    "elements": elements}
            if FeishuClient(webhook, settings.feishu_secret).send_card(card):
                # 发送成功才烧冷却,且仅对入卡的 res_hits:越限项保留冷却位,下轮可轮候进入
                for u, r, cnt in res_hits:
                    feishu_alert_gate(session, user_id, "focus_res", "res:" + u[:120],
                                      settings.focus_cooldown_hours, f"{cnt} 篇同发")

    # 文章内容交叉提取:从正文提取新公众号名 → 自动入库为候选对标号
    from app.db.models import WechatCandidate

    known_names = set(session.scalars(select(WechatBenchmark.nickname).where(
        WechatBenchmark.user_id == user_id)).all())
    seen_names = set(session.scalars(select(WechatCandidate.name).where(
        WechatCandidate.user_id == user_id, WechatCandidate.status == "new")).all())
    cross_new: list[WechatCandidate] = []
    for r in rows:
        if not r.content:
            continue
        for name in _ear(r.content):
            if name in known_names or name in seen_names or name == (r.author or ""):
                continue
            seen_names.add(name)
            cross_new.append(WechatCandidate(
                user_id=user_id, name=name[:128], title=r.title[:200],
                term="content_cross", status="new"))
    if cross_new:
        session.add_all(cross_new)
        logger.info("内容交叉提取:发现 %d 个新公众号候选(用户 %s)", len(cross_new), user_id)
        _push_candidates(session, user_id, settings, cross_new)
    return replacements

def _weread_collect(user_id: int, b: WechatBenchmark, weread: WereadClient,
                    session: Session, stats: dict | None = None) -> list[WechatArticle]:
    """微信读书单号采集:**cover 最新一篇(稳定可用)→ mp/articles 列表(可选,常被限权)。

    实测(2026-09):mp/articles 仅在会话建立初期可用,数小时后被服务端限权(-2041),
    cover 始终可用——故 cover 为主路径,mp/articles 失败静默跳过不影响监听。
    **但"只有 cover"就意味着同一天发第 2、3 篇会被最新一篇顶掉、永久漏采**(两轮之间最长 8h),
    这正是"近 24h 必须全推"的唯一真实缺口;列表可用时该缺口不存在。
    `stats` 记账可枚举性(见调用方),不可枚举又采到新文时必须暴露给运维,不能假装全覆盖。
    近3天过滤;阅读/点赞以 cover/mp_articles 自带值为准(免费)。
    """
    from app.services.weread_client import WereadClient as _WC

    cutoff = datetime.now() - timedelta(days=3)
    items = []
    # 主路径:cover 最新一篇(始终可用)
    item = weread.latest_article(b.weread_book_id)
    if item and item["url"]:
        items.append({"title": item["title"], "url": item["url"],
                      "publish_at": None})
    # 备选:mp/articles 近期列表(含精确阅读/点赞;被限权时静默跳过)
    listed = False
    try:
        payload = weread.mp_articles(b.weread_book_id)
        for it in _WC.flatten_mp_articles(payload):
            ts = it.get("create_time") or 0
            pub = datetime.fromtimestamp(ts) if ts else None
            if pub and pub < cutoff:
                continue
            items.append({"title": it["title"], "url": build_mp_url(it["original_id"]),
                          "read_num": it["read_num"], "like_num": it["like_num"],
                          "publish_at": pub})
        listed = True
    except Exception as exc:  # noqa: BLE001 - 限权/废弃不影响 cover 主路径
        # -2041 是新版微信读书对该接口的永久限权,每进程只记一次,避免每账号刷屏
        if not getattr(_weread_collect, "_mp_articles_warned", False):
            _weread_collect._mp_articles_warned = True
            logger.warning("mp/articles 不可用(%s),全部账号仅用 cover 最新一篇", exc)
    # require_pan=False:不再丢弃无盘链文——"标题不含网盘词"≠"没价值",
    # 此前这道闸把 15 个对标号 10 天的新文全部静默丢弃(用户看到"停更在 9.7"的根因)
    got = _insert_new_articles(session, user_id, b, items, source="listen",
                               fetch_content=True, require_pan=False)
    if stats is not None:
        key = "weread_list_ok" if listed else ("weread_list_off_new" if got else "weread_list_off")
        stats[key] = stats.get(key, 0) + 1
    return got


def run_wechat_listen(session: Session, user_id: int, settings: Settings | None = None,
                      client: DajialaClient | None = None, weread: WereadClient | None = None,
                      platform: ReaderPlatformClient | None = None, push: bool = True,
                      batch_index: int | None = None, batch_size: int | None = None) -> dict:
    """监听一轮:双数据源免费优先——微信读书(cover)→ dajiala(当天发文)→ 新文入库推飞书。

    余额不足(dajiala)只禁用付费源与即时采样并返回 `dajiala_skipped:"low_balance"`,
    免费源(读书平台/微信读书)照常监听;全部数据源不可用才返回 `skipped`。
    """
    settings = _base(settings)
    all_rows = session.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.active.is_(True))
        .order_by(WechatBenchmark.id)).all()
    if not all_rows:
        # 跳过也记运维记录:否则后台"没有公众号情况",无从判断是没加号还是没跑
        _record_run(session, user_id, "wechat_listen", "skipped", "no_benchmarks(未添加对标号)")
        session.commit()
        return {"platform": "wechat", "status": "skipped", "reason": "no_benchmarks"}
    # 错峰分批:微信读书源按批次轮转(每轮只查 1/N 的号,降低瞬时密度防风控);
    # 批次大小默认 7(号级延迟 ≤ N×间隔,21 号 3h 全覆盖)
    rows = all_rows
    if batch_size and batch_size > 0 and len(all_rows) > batch_size:
        start = (batch_index or 0) % (len(all_rows) // batch_size + (1 if len(all_rows) % batch_size else 0))
        rows = all_rows[start * batch_size:(start + 1) * batch_size]
    cookie = _weread_cookie(session, user_id, settings)
    daj_key = _dajiala_key(session, user_id, settings)
    use_dajiala = bool(daj_key)
    if not cookie and not use_dajiala:
        _record_run(session, user_id, "wechat_listen", "skipped",
                    "no_source(无微信读书 Cookie 且无 dajiala key)")
        session.commit()
        return {"platform": "wechat", "status": "skipped", "reason": "no_source"}

    # 余额保护前置:只要有账号需要走 dajiala(无 book_id 或会话失效),先查余额(免费接口)。
    # 余额不足只禁用付费源(微信读书等免费源照常跑),不再整轮跳过——否则空余额把书架号一起饿死。
    dajiala_off = ""
    balance: float | None = None
    needs_dajiala = use_dajiala and any(
        not (cookie and b.weread_book_id) and b.anchor_url for b in rows)
    if use_dajiala:
        client = client or DajialaClient(daj_key)
    if needs_dajiala:
        try:
            balance = client.remain_money()
        except DajialaError as exc:
            _record_run(session, user_id, "wechat_listen", "failed", f"{type(exc).__name__}: {exc}")
            session.commit()
            raise
        if balance < settings.dajiala_min_balance:
            dajiala_off = f"low_balance={balance:.2f}"
            use_dajiala = False
            logger.warning("dajiala 余额 %.2f 低于阈值 %.2f,本轮仅跑免费源(用户 %s)",
                           balance, settings.dajiala_min_balance, user_id)

    plat = platform or _platform_client(settings)
    now = datetime.now()
    new_rows: list[WechatArticle] = []
    failed = 0
    # 微信读书"近期列表"可枚举性统计:决定"近24h全推"能不能兑现(见 _weread_collect)
    wr_stats: dict = {}
    for b in rows:
        used = False
        # ⓪ 读书平台(wewe-rss 兼容,免费,分页全量列表):有 biz 且平台已配置 → 首选
        if plat and b.biz:
            try:
                raw_items = plat.mp_articles(b.biz, page=1, limit=20)
                used = True
                norm = [{"title": it["title"], "url": it["url"],
                         "publish_at": _parse_time(it.get("publish_at_raw"))} for it in raw_items]
                if norm:
                    b.miss_count = 0
                    b.last_item_at = now
                    got = _insert_new_articles(session, user_id, b, norm, source="listen",
                                               fetch_content=True, require_pan=False)
                    if got:
                        new_rows.extend(got)
                else:
                    b.miss_count = (b.miss_count or 0) + 1
            except PlatformError as exc:
                logger.warning("读书平台监听 %s 失败,降级后续源:%s", b.nickname or b.biz, exc)
        # ① 微信读书(免费):对标号已关联 bookId 且有 Cookie;登录失效时自动续期重试一次
        if not used and cookie and b.weread_book_id:
            try:
                weread = weread or WereadClient(cookie)
                got = _weread_collect(user_id, b, weread, session, stats=wr_stats)
                used = True
                if got:
                    new_rows.extend(got)
                    b.miss_count = 0
                    b.last_item_at = now
            except WereadAuthError as exc:
                logger.warning("微信读书登录态失效(用户 %s):%s;尝试自动续期", user_id, exc)
                refreshed = refresh_weread_cookie(session, user_id, settings)
                if refreshed.get("status") == "success":
                    cookie = refreshed["cookie"]
                    try:
                        got = _weread_collect(user_id, b, WereadClient(cookie), session,
                                              stats=wr_stats)
                        used = True  # 微信读书源已消费本号,勿再走 dajiala 重复扣费
                        if got:
                            new_rows.extend(got)
                            b.miss_count = 0
                            b.last_item_at = now
                    except WereadError as exc2:
                        failed += 1
                        logger.warning("微信读书续期后仍失败 %s:%s", b.nickname or b.weread_book_id, exc2)
                else:
                    # 续期失败:后续号降级 dajiala;本号不置 used,继续走下方 dajiala 兜底
                    # 指纹必须在清空 cookie 前算——否则 md5("")[:6]="d41d8c" 恒定,
                    # 冷却键永远撞同一个假指纹,通知无法随用户换新 Cookie 而重置。
                    fp = _cookie_fingerprint(cookie)
                    cookie = ""
                    failed += 1
                    # 即时提醒用户更新 Cookie(6h 冷却,不刷屏)
                    from app.services.alert_service import notify_incident
                    notify_incident(
                        session, user_id, "wechat",
                        f"🟠 微信读书 Cookie 已过期,请更新[{fp}]",
                        "自动续期失败。请在浏览器登录 weread.qq.com 后 F12 复制 Cookie,"
                        "粘贴到「Cookie 管理」页 weread 平台(或发给我更新)。"
                        "粘贴前确认串里有「wr_rt=」——缺它自动续期无从下手,十几小时必过期",
                        settings=settings)
            except WereadError as exc:
                failed += 1
                logger.warning("微信读书监听 %s 失败:%s", b.nickname or b.weread_book_id, exc)
        # ② dajiala(付费兜底)
        if not used and b.anchor_url and use_dajiala:
            try:
                obj = client.post_condition(b.anchor_url)
            except DajialaNoBalance:
                logger.warning("公众号监听中途余额不足,已采 %d 篇即止(用户 %s)", len(new_rows), user_id)
                use_dajiala = False
                if not dajiala_off:
                    dajiala_off = "low_balance_midway"
                continue
            except DajialaError as exc:
                failed += 1
                logger.warning("公众号监听 %s 失败:%s", b.nickname or b.anchor_url, exc)
                continue
            used = True
            for key, val in (("nickname", obj.get("nickname")), ("ghid", obj.get("ghid"))):
                if val and not getattr(b, key):
                    setattr(b, key, str(val)[:128 if key == "nickname" else 64])
            items = _extract_articles(obj.get("data"))
            if not items:
                b.miss_count = (b.miss_count or 0) + 1  # 连续多轮"当天没有发文"→ 沉睡号
                continue
            b.miss_count = 0
            b.last_item_at = now
            new_rows.extend(_insert_new_articles(session, user_id, b, items, source="listen",
                                                 fetch_content=True, require_pan=False))
    replacements = _enrich_new_articles(session, user_id, settings, new_rows, client,
                                        allow_paid=use_dajiala)

    session.commit()

    # 判定:全部账号失败=failed;部分失败=partial(即便采到新文,故障也要暴露)。
    # 此前 `not failed or new_rows` 优先级等于 `(not failed) or new_rows`,
    # 只要采到 1 篇新文就把"全部账号挂了"也记成 success 掩盖故障
    if failed == len(rows):
        status = "failed"
    elif failed:
        status = "partial"
    else:
        status = "success"
    detail = f"accounts={len(rows)} new={len(new_rows)} failed={failed}"
    enumerable = wr_stats.get("weread_list_ok", 0)
    off_new = wr_stats.get("weread_list_off_new", 0)
    if wr_stats:
        # list_off 必须写进运维记录:"只采到 cover 最新一篇"时同日其它篇是**未知丢失**,
        # 不能让它和"该号今天真的只发了一篇"长得一样(与 -2014 假象、全败标 success 同族)。
        detail += (f" weread_list(ok={enumerable} off={wr_stats.get('weread_list_off', 0)}"
                   f" off_with_new={off_new})")
    if dajiala_off:
        detail += f" dajiala_off({dajiala_off})"
    _record_run(session, user_id, "wechat_listen", status, detail)
    session.commit()
    if push and new_rows:
        _push_listen(session, user_id, settings, new_rows, replacements)
    if off_new and push:
        # 兑现"近24h全部推送"要靠列表枚举;只要还有号列不出来又采到了新文,就必须点名而不是安静少推
        from app.services.alert_service import notify_incident
        notify_incident(
            session, user_id, "wechat",
            "⚠️ 微信读书只能拿到最新一篇,同日其它篇可能漏推",
            f"本轮列不出却采到新文的号:{off_new}(可枚举 {enumerable} / 共 {len(rows)})。"
            "微信读书 mp/articles(近期列表)对本会话不可用(-2041 限权),监听退化为"
            "cover 最新一篇:两轮之间(最长 8h)同一号发多篇时,前面的那几篇顶不掉也补不回来。"
            "要真正兑现『近24h全推』只有两条路:① 部署 wewe-rss 并给对标号回填 biz(免费全量列表);"
            "② dajiala 充值走 history_by_ghid(付费)。临时缓解:对高产号多点「同步文章」",
            settings=settings)
    out: dict = {"platform": "wechat", "status": status, "accounts": len(rows),
                 "new": len(new_rows), "failed": failed}
    if wr_stats:
        out["weread_list"] = {k: v for k, v in wr_stats.items()}
    if dajiala_off:
        out["dajiala_skipped"] = "low_balance"
        if balance is not None:
            out["balance"] = balance
    return out

def _push_listen(session: Session, user_id: int, settings: Settings, rows: list[WechatArticle],
                 replacements: dict[int, list[tuple[str, str, str]]] | None = None) -> None:
    """新文推公众号专属飞书群(column_set 网格卡片:公众号/文章/网盘/阅读 四列对齐)。

    标题超链接优先级:本轮转存链(带提取码)> 已持久化的我的转存链 > 原文;
    未配专属群则回落总群;推送失败不影响采集结果。
    不设免打扰窗口:飞书是员工查看新发文的唯一入口(平台只有运营者可见),
    任何时段采到的文章都照常全量推送。
    """
    from app.services.feishu import _col_set_row, _md_safe, webhook_for
    from app.services.feishu_client import FeishuClient

    wh = webhook_for(settings, "wechat")
    main_wh = settings.feishu_webhook
    targets = list(dict.fromkeys(filter(None, [wh, main_wh])))  # 去重保序
    if not targets:
        return
    replacements = replacements or {}
    # 免打扰过滤后可能清空(理论上上面已 return,这里再兜一层,避免推空卡)
    if not rows:
        return
    from collections import OrderedDict

    from app.db.models import WechatPanLink
    # 重复资源计数:一次 GROUP BY 批量查本轮全部文章(循环内逐篇 COUNT 是 N+1)。
    # 不再只算 rows[:20]——全量推送下每篇都要有 🔥xN 标记。
    dup_counts: dict[str, int] = {}
    pan_of = {r.id: next((x.strip() for x in (r.pan_urls or "").splitlines() if x.strip()), "")
              for r in rows if r.pan_urls}
    if pan_of:
        for pan_url, cnt in session.execute(
                select(WechatPanLink.pan_url, func.count()).where(
                    WechatPanLink.pan_url.in_(set(pan_of.values())),
                    WechatPanLink.user_id == user_id,  # 🔥xN 只数本租户监听,别把他号的重复算进来
                ).group_by(WechatPanLink.pan_url)).all():
            dup_counts[pan_url] = int(cnt)

    def _render_article(r: WechatArticle) -> dict:
        rep = replacements.get(r.id) or []
        code = ""
        my_link = False          # 标题链接是否指向"我方转存链"(False=点进去是公众号原文)
        if rep:
            link, code = rep[0][1], rep[0][2]
            my_link = True
        else:
            # 只展示我方网盘链接(本轮转存链 > 历史我方链);绝不回落到别人的盘链——
            # 未转存时点标题打开公众号原文。历史 my_pan_urls 常自带提取码,拆出来明文显示。
            cand = next((x.strip() for x in (r.my_pan_urls or "").splitlines()
                         if x.strip().startswith("https://pan.quark.cn/s/")), "")
            if cand:
                my_link = True
                m = re.search(r"(?:提取码\s*([0-9A-Za-z]{4}))", cand)
                link = cand.split(" (提取码")[0].strip()
                code = m.group(1) if m else ""
            else:
                link = r.url
        # 重复资源标记: 同盘链已被其他文章推过 → 🔥N(同行都在发的确认级资源)
        hot = ""
        first_pan = pan_of.get(r.id, "")
        if first_pan:
            dup = max(0, dup_counts.get(first_pan, 0) - (1 if first_pan in pan_of.values() else 0))
            if dup:
                hot = f"🔥x{dup + 1} "
        title = _md_safe(r.title)
        shown = title[:26] + ("…" if len(title) > 26 else "")
        q_badge = ""
        if r.read_num >= 500:
            q_badge = "🔴爆 "
        elif r.read_num >= 100:
            q_badge = "⭐热 "
        elif r.quality >= 6:
            q_badge = "⭐优 "
        elif r.quality <= 2 and r.pan_types:
            q_badge = "⚠️疑 "
        # href 只放干净链接(旧实现把" (提取码 xxxx)"拼进 URL → 链接点不开),提取码明文附后
        article_md = (f"[{hot}{q_badge}{shown}]({_md_safe(link)})" if link else shown) \
            + (f" 🔑{code}" if code else "")
        # 网盘列同时交代"点标题会去哪":🔴=我方转存链;⏳=有源链但还没转好(点进去是原文);
        # ⛔=对方分享已被封,永远转不了。员工不必点开才发现进的是公众号文章。
        types = _md_safe(r.pan_types)[:8] if r.pan_types else ""
        if my_link:
            pan = f"🔴{types}" if types else "🔴我方链"
        elif "41031" in (r.my_pan_urls or ""):
            pan = f"⛔源失效{types}"
        elif (r.pan_urls or "").strip():
            pan = f"⏳待转存{types}"
        else:
            pan = types or "—"
        read = str(r.read_num) if r.traffic_at else "—"
        return _col_set_row([
            (_md_safe(r.author)[:10] or "—", 3), (article_md, 7), (pan, 2), (read, 2),
        ])

    # 按账号分组(员工视角要一眼看清"哪个号发了哪些"),同号内资源文/高阅读优先。
    groups: "OrderedDict[str, list[WechatArticle]]" = OrderedDict()
    for r in sorted(rows, key=lambda r: (r.author or "", not r.pan_types, -(r.read_num or 0))):
        groups.setdefault(r.author or "未知账号", []).append(r)
    # 展开成 (账号, 文章) 序列,分页时按账号标题切段
    seq: list[tuple[str, WechatArticle]] = [
        (author, r) for author, arts in groups.items() for r in arts
    ]

    # LLM 叙事层:盘链文优先交给大模型解读(失败/未配 key 静默降级,不影响推送)
    ai_elements: list[dict] = []
    if settings.deepseek_api_key:
        try:
            from app.services.llm_client import narrate_articles

            top = sorted(rows, key=lambda r: (not r.pan_types, -(r.read_num or 0)))[: settings.llm_narrate_limit]
            ctx = [{"title": r.title, "summary": (r.content or "")[:200],
                    "pan_types": r.pan_types} for r in top]
            reading = narrate_articles(settings.deepseek_base_url, settings.deepseek_api_key,
                                       settings.deepseek_model, ctx)
            if reading:
                ai_elements = [
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md",
                        "content": "🤖 **AI 解读**" + chr(10) + reading[:1500]}},
                ]
        except Exception:  # noqa: BLE001 - 叙事失败不影响推送
            logger.exception("LLM 叙事失败 user=%s", user_id)

    # 全量分页:每卡最多 20 篇(飞书卡片有体积上限),超出部分继续发卡而非"见平台列表"。
    per_card = 20
    chunks = [seq[i:i + per_card] for i in range(0, len(seq), per_card)]
    total_pages = len(chunks)
    for page_idx, chunk in enumerate(chunks):
        elements: list[dict] = [
            {"tag": "note", "elements": [{"tag": "plain_text",
                "content": "点文章标题打开链接(优先你的夸克转存链) · 网盘列=识别到的盘链,"
                           "—=这篇没带网盘链(仍照常推) · 阅读未采样为 —"}]},
        ]
        if page_idx == 0:
            elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                "content": f"本轮共 {len(rows)} 篇新发文,来自 {len(groups)} 个公众号(按账号分组,全量推送)"}]})
        elements.append(_col_set_row(
            [("**公众号**", 3), ("**文章**", 7), ("**网盘**", 2), ("**阅读**", 2)], grey=True))
        last_author: str | None = None
        for author, r in chunk:
            if author != last_author:  # 换账号插入一行账号标题;账号跨卡时下一页重出标题
                elements.append({"tag": "div", "text": {"tag": "lark_md",
                    "content": f"**📢 {_md_safe(author)}**"}})
                last_author = author
            elements.append(_render_article(r))
        if page_idx == 0 and ai_elements:
            elements.extend(ai_elements)
        card_title = f"📡 公众号监听 · 新发文 {len(rows)} 篇"
        if total_pages > 1:
            card_title += f" · {page_idx + 1}/{total_pages}"
        for target in targets:
            try:
                FeishuClient(target, settings.feishu_secret).send_card({
                    "config": {"wide_screen_mode": True},
                    "header": {"template": "blue", "title": {"tag": "plain_text",
                        "content": card_title}},
                    "elements": elements,
                })
            except Exception:  # noqa: BLE001 - 推送失败不影响采集结果
                logger.exception("公众号监听飞书推送失败 user=%s page=%s", user_id, page_idx)


def _dedupe_sync_push_rows(session: Session, user_id: int,
                           rows: list[WechatArticle]) -> list[WechatArticle]:
    """同一资源(相同网盘分享链)只推一篇:本轮内保留首见者,更早入库过的盘链不再推。

    这是"相同链接不需要重复保存"的推送侧配套——转存侧由 `_enrich_new_articles` 的盘链级
    复用保证。无盘链的文章按 URL 天然唯一(`_insert_new_articles` 已按 URL 去重),原样保留。
    """
    if not rows:
        return []
    ids = [r.id for r in rows]
    first_pan = {r.id: next((x.strip() for x in (r.pan_urls or "").splitlines() if x.strip()), "")
                 for r in rows}
    pans = {p for p in first_pan.values() if p}
    already: set[str] = set()
    if pans:
        # 本轮之外的文章(更早入库、早已随监听/同步进过飞书群)带过这个资源 → 再推就是刷屏
        already = {p for (p,) in session.execute(
            select(WechatPanLink.pan_url).where(
                WechatPanLink.user_id == user_id,
                WechatPanLink.pan_url.in_(pans),
                WechatPanLink.article_id.notin_(ids)).distinct()).all()}
    seen: set[str] = set()
    kept: list[WechatArticle] = []
    for r in sorted(rows, key=lambda x: x.id):  # 入库顺序即新→旧,保留首见者
        p = first_pan.get(r.id, "")
        if p and (p in already or p in seen):
            continue
        if p:
            seen.add(p)
        kept.append(r)
    return kept


def _sync_push_after_transfer(session: Session, user_id: int, settings: Settings,
                              new_rows: list[WechatArticle]) -> dict:
    """同步收尾:按资源去重 → 先转存夸克链 → 再推飞书(文章名点进去就是"我的盘")。

    三条约束决定了这里的形状:
    ① 去重:同一盘链只留一篇(本轮内 + 更早入库过的都不再推),转存侧由
       `_enrich_new_articles` 的盘链级复用保证"相同链接不重复保存";
    ② 不采样:一次同步可入库上百篇历史文,逐篇 ¥0.06 即时采样会打穿余额,
       所以 `allow_paid=False`,历史文阅读量交给采样作业;
    ③ 封顶 `wechat_sync_push_limit` 篇(资源文优先):转存是同步网络调用,
       不设窗口会让一次 HTTP 请求跑几十分钟占死线程池。窗口外的旧文本轮不推也不转,
       留给监听的补转存队列(`run_backfill=False` 就是不再叠加这条队列),
       数量写进运行记录与返回值,不静默丢。
       **例外:近 24h 内发的文章(以及发布时间未知的刚发文)一律推,不受封顶限制**
       ——"监控号近 24h 的新发文必须全部到飞书"是硬要求,封顶只管 24h 之前的历史补采文。
    """
    if not new_rows:
        return {"pushed": 0, "transferred": 0, "deduped": 0, "truncated": 0}
    session.commit()  # 新入库的行先落库:下面转存若炸,回滚不能把这次同步的成果一起带走
    kept = _dedupe_sync_push_rows(session, user_id, new_rows)

    def _ts(r: WechatArticle) -> int:
        return int(r.publish_at.timestamp()) if r.publish_at else 0

    ordered = sorted(kept, key=lambda r: (not r.pan_types, -_ts(r)))
    ref = datetime.now() - timedelta(hours=24)
    must = [r for r in ordered if r.publish_at is None or r.publish_at >= ref]
    history = [r for r in ordered if r.publish_at is not None and r.publish_at < ref]
    cap = max(1, int(settings.wechat_sync_push_limit or 20))
    extra = max(0, cap - len(must))          # 近24h 的文章先占满窗口,剩下的名额才给历史文
    to_push = must + history[:extra]
    try:
        replacements = _enrich_new_articles(session, user_id, settings, to_push,
                                            client=None, allow_paid=False, run_backfill=False)
        session.commit()
    except Exception:  # noqa: BLE001 - 转存炸了也要推(标题回落原文),不能让同步整个报错
        logger.exception("同步后转存失败 user=%s", user_id)
        session.rollback()
        replacements = {}
    _push_listen(session, user_id, settings, to_push, replacements)
    return {"pushed": len(to_push), "transferred": len(replacements),
            "deduped": len(new_rows) - len(kept), "truncated": len(history) - extra}


# ---------------------------------------------------------------- 全量同步
def sync_wechat_account(session: Session, user_id: int, benchmark_id: int,
                        settings: Settings | None = None, client: DajialaClient | None = None,
                        max_pages: int | None = None, weread: WereadClient | None = None,
                        platform: ReaderPlatformClient | None = None) -> dict:
    """一键同步:history_by_ghid 翻页拉历史文章入库(¥0.14/页,默认 WECHAT_SYNC_MAX_PAGES 封顶)。

    入库后统一走 `_sync_push_after_transfer`:同盘链去重 → 夸克转存换成我方链 → 再推飞书,
    所以卡片里点文章名直接进"我的夸克链",不会重复保存/重复推送。
    """
    settings = _base(settings)
    b = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.id == benchmark_id))
    if b is None:
        raise KeyError("对标账号不存在")
    plat = platform or _platform_client(settings)
    # 服务端钳制:query 参数无上限时恶意调用可烧余额(¥0.14/页)
    limit = max(1, min(int(max_pages or (10 if plat and b.biz else settings.wechat_sync_max_pages)), 20))
    if plat and b.biz:
        new_rows: list[WechatArticle] = []
        pages = 0
        try:
            while pages < limit:
                raw_items = plat.mp_articles(b.biz, page=pages + 1, limit=20)
                norm = [{"title": it["title"], "url": it["url"],
                         "publish_at": _parse_time(it.get("publish_at_raw"))} for it in raw_items]
                got = _insert_new_articles(session, user_id, b, norm, source="sync", require_pan=False)
                new_rows.extend(got)
                pages += 1
                if not raw_items or len(got) < len(raw_items):
                    break  # 本页为空或全部已入库 → 更旧的页也必然已见
        except PlatformError as exc:
            logger.warning("读书平台同步失败,转 dajiala/微信读书:%s", exc)
        if pages:
            b.last_item_at = datetime.now()
            push = _sync_push_after_transfer(session, user_id, settings, new_rows)
            _record_run(session, user_id, "wechat_sync", "success",
                        f"platform account={b.nickname} pages={pages} new={len(new_rows)} "
                        f"pushed={push['pushed']} deduped={push['deduped']} truncated={push['truncated']}")
            session.commit()
            return {"platform": "wechat_sync", "status": "success", "pages": pages,
                    "new": len(new_rows), "ghid": b.ghid, "nickname": b.nickname, **push}
    # 走 _dajiala_key 而非全局 settings.dajiala_key:POST /api/wechat/benchmarks/{id}/sync
    # 是普通用户可控入口,¥0.14/页 × 无限次调用可打穿运营者余额(2026-09-14 隔离原则的漏网路径)。
    sync_key = _dajiala_key(session, user_id, settings)
    if not sync_key:
        # 无 dajiala:微信读书 cover 只给"最新一篇",mp/articles 列表可用时才谈得上补全
        cookie = _weread_cookie(session, user_id, settings)
        if not cookie or not b.weread_book_id:
            return {"platform": "wechat_sync", "status": "skipped", "reason": "no_dajiala_key"}

        def _fetch(client: WereadClient) -> tuple[list[dict], "object | None", str]:
            """(近期列表, cover 那篇, 列表状态 ok|limited|error)。"""
            from app.services.weread_client import WereadClient as _WC

            listed, out = "error", []
            try:
                for it in _WC.flatten_mp_articles(client.mp_articles(b.weread_book_id)):
                    ts = it.get("create_time") or 0
                    pub = datetime.fromtimestamp(ts) if ts else None
                    if pub and pub < datetime.now() - timedelta(days=3):
                        continue
                    out.append({"title": it["title"], "url": build_mp_url(it["original_id"]),
                                "read_num": it["read_num"], "like_num": it["like_num"],
                                "publish_at": pub, "review_id": it.get("review_id") or ""})
                listed = "ok"
            except WereadError as exc:
                # -2041 = 本会话列表预算耗尽(可预期,别当故障);其余按异常归类
                listed = "limited" if "-2041" in str(exc) else "error"
                logger.info("微信读书近期列表不可用(%s),%s 退到最新一篇", str(exc)[:60], b.nickname)
            cover = None
            try:
                cover = client.latest_article(b.weread_book_id)
            except WereadError as exc:
                if listed != "ok":
                    raise          # 两条路都没有 → 交给上层(监听/路由决定降级或续期)
                logger.warning("cover 也失败(%s),本次只同步列表内容", str(exc)[:60])
            return out, cover, listed

        wc = weread or WereadClient(cookie)
        try:
            items, cover, listed = _fetch(wc)
        except WereadAuthError:
            # wr_skey 十几小时必过期,而用户点「同步文章」走的正是这条路。
            # wr_rt 还活着时先像监听那样自救续期一次再重试;续期不成才把异常抛给上层
            # (路由翻成 502 可执行文案,此前这里是裸 500 → 前端只报"服务器开小差了")。
            # 续期会换出新会话——正是 mp/articles 可用的窗口,列表这次能补回同日漏掉的篇。
            refreshed = refresh_weread_cookie(session, user_id, settings)
            if refreshed.get("status") != "success":
                raise
            wc = WereadClient(refreshed["cookie"])
            items, cover, listed = _fetch(wc)
        if cover and cover["url"] and not any(it["url"] == cover["url"] for it in items):
            items = [cover] + items       # 列表里没这篇(刚发/翻页边界)也要带上
        # cover 的正文用它的 reviewId 走转发页;列表项各自的 reviewId 由 content_resolver 复用
        rid_of = {it["url"]: it.get("review_id") or "" for it in items}
        if cover:
            rid_of.setdefault(cover["url"], cover.get("review_id") or "")

        def _resolve(title: str, url: str = "") -> str:
            rid = rid_of.get(url) or ""
            return wc.mp_content(rid) if rid else ""

        new_rows = []
        if items:
            new_rows = _insert_new_articles(session, user_id, b, items, source="sync",
                                            content_resolver=_resolve, require_pan=False)
            b.last_item_at = datetime.now()
        push = _sync_push_after_transfer(session, user_id, settings, new_rows)
        _record_run(session, user_id, "wechat_sync",
                    "success" if listed == "ok" else "partial",
                    f"weread({listed},items={len(items)}) account={b.nickname} new={len(new_rows)} "
                    f"pushed={push['pushed']} deduped={push['deduped']} truncated={push['truncated']}")
        session.commit()
        return {"platform": "wechat_sync",
                "status": "success" if listed == "ok" else "partial",
                "reason": "" if listed == "ok" else f"weread_list_{listed}_latest_only",
                "weread_list": listed, "items": len(items),
                "pages": 1 if items else 0, "new": len(new_rows), "ghid": b.ghid,
                "nickname": b.nickname, **push}
    client = client or DajialaClient(sync_key)
    added: list[WechatArticle] = []
    offset = ""
    pages = 0
    try:
        while pages < limit:
            obj = client.history_by_ghid(ghid=b.ghid, article_url="" if b.ghid else b.anchor_url,
                                         offset=offset)
            acct = _deep_find(obj, "AccountInfo") or {}
            if not b.ghid and acct.get("UserName"):
                b.ghid = str(acct["UserName"])[:64]
            if acct.get("NickName") and (not b.nickname or b.nickname == "未命名"):
                b.nickname = str(acct["NickName"])[:128]
            items = _extract_articles(_deep_find(obj, "MsgList"),
                                      url_keys=("content_url", "ContentUrl", "url"))
            added.extend(_insert_new_articles(session, user_id, b, items, source="sync", require_pan=False))
            pages += 1
            paging = _deep_find(obj, "PagingInfo") or {}
            if str(paging.get("IsEnd")) == "1" or not paging.get("Offset"):
                break
            offset = str(paging.get("Offset"))
    except DajialaNoBalance:
        logger.warning("同步中途余额不足(用户 %s 账号 %s,已入库 %d 篇)", user_id, b.nickname, len(added))
    b.last_item_at = datetime.now()
    push = _sync_push_after_transfer(session, user_id, settings, added)
    status = "partial" if added and pages >= limit else "success"
    _record_run(session, user_id, "wechat_sync", status if added or pages else "success",
                f"pages={pages} new={len(added)} pushed={push['pushed']} "
                f"deduped={push['deduped']} truncated={push['truncated']} account={b.nickname}")
    session.commit()
    return {"platform": "wechat_sync", "status": status, "pages": pages, "new": len(added),
            "ghid": b.ghid, "nickname": b.nickname, **push}

def _apply_sample(session: Session, user_id: int, r: WechatArticle, data: dict, now: datetime) -> None:
    """把 read_zan_pro 结果写回文章 + 追加一个采样点(首采样记 first_read_num 做账号基线)。"""
    r.read_num = int(data.get("read") or 0)
    if not r.sample_count:
        r.first_read_num = r.read_num
    r.sample_count = (r.sample_count or 0) + 1
    r.zan_num = int(data.get("zan") or 0)
    r.looking_num = int(data.get("looking") or 0)
    r.share_num = int(data.get("share_num") or 0)
    r.collect_num = int(data.get("collect_num") or 0)
    r.comment_count = int(data.get("comment_count") or 0)
    r.traffic_at = now
    session.add(WechatTrafficSample(user_id=user_id, article_id=r.id,
                                    read_num=r.read_num, zan_num=r.zan_num,
                                    looking_num=r.looking_num, share_num=r.share_num,
                                    collect_num=r.collect_num,
                                    comment_count=r.comment_count, sampled_at=now))

def _notify_burst(session: Session, user_id: int, settings: Settings, r: WechatArticle,
                  growth: float | None, baseline: int | None = None) -> bool:
    """🚀 爆点苗头即时推送(公众号群,24h 冷却)。返回是否推送。"""
    from app.services.feishu_client import FeishuClient, webhook_for
    from app.services.alert_service import feishu_alert_gate

    webhook = webhook_for(settings, "wechat")
    if not webhook:
        return False
    reason = f"增长{growth:.0f}%" if growth is not None else "首采超基线"
    if not feishu_alert_gate(session, user_id, "focus_burst", str(r.id),
                             settings.focus_cooldown_hours, reason):
        return False
    growth_txt = f"+{growth:.0f}%" if growth is not None else "超基线"
    base_txt = f" · 账号基线中位 {baseline}" if baseline else ""
    mine = [x for x in (r.my_pan_urls or "").splitlines() if x.strip()]
    lines = ["🚀 爆点苗头 · 建议立即跟进改写",
             "🔴 " + r.title[:40],
             "📊 阅读 " + str(r.read_num) + "(" + growth_txt + ") · 转发 " + str(r.share_num)
             + " · 收藏 " + str(r.collect_num) + base_txt]
    if mine:
        lines.append("📦 我的链接: " + mine[0])
    lines.append(r.url)
    sent = FeishuClient(webhook, settings.feishu_secret).send(chr(10).join(lines))
    if sent:
        session.commit()  # 发送成功才落冷却门
        return True
    session.rollback()  # 发送失败不烧冷却门
    return False


# ---------------------------------------------------------------- 阅读量采样(dajiala read_zan_pro)
def sample_traffic(session: Session, user_id: int, settings: Settings | None = None,
                   client: DajialaClient | None = None, benchmark_id: int | None = None,
                   limit: int | None = None) -> dict:
    """给"最近未采样"的文章拉一次流量六指标(dajiala read_zan_pro,¥0.06/篇)。

    选样规则:有链接、距上次采样 ≥ `wechat_traffic_min_interval_hours`(没采过的优先),
    按发现时间新→旧,最多 `wechat_traffic_sample_limit` 篇(可用 limit 覆盖)。
    余额保护:先查余额(免费),按 0.06/篇 裁剪到买得起的数量。
    """
    settings = _base(settings)
    daj_key = _dajiala_key(session, user_id, settings)
    if not daj_key:
        return {"platform": "wechat_traffic", "status": "skipped", "reason": "no_key"}
    client = client or DajialaClient(daj_key)
    # 用户传入 limit 只允许调小、不允许放大:旧 `max(1, int(limit or default))`
    # 遇 POST body {"limit": 1000000} → `LIMIT 5,000,000` 把该用户全部候选 Text 文章
    # 一次性载入 Python,再逐篇串行调 dajiala 付费接口;虽被余额 affordable 截断
    # 花钱,但单请求在同步 worker 里可跑几十分钟,几个并发即占满 FastAPI 线程池拖死全站。
    cap = max(1, int(settings.wechat_traffic_sample_limit or 30))
    limit = max(1, min(int(limit or cap), cap))
    cutoff = datetime.now().timestamp() - settings.wechat_traffic_min_interval_hours * 3600

    now = datetime.now()
    young_cutoff = now - timedelta(hours=48)
    q = select(WechatArticle).where(
        WechatArticle.user_id == user_id,
        WechatArticle.url != "")
    if benchmark_id:
        q = q.where(WechatArticle.benchmark_id == benchmark_id)
    candidates = session.scalars(q.order_by(WechatArticle.created_at.desc()).limit(limit * 5)).all()
    # 逐篇按"文章年龄"决定最小采样间隔:48h 内新文 6h(密集捕捉早期增速),其余按设置(默认 24h)
    rows = []
    for r in candidates:
        fresh = bool(r.created_at and r.created_at >= young_cutoff)
        min_i = 6 * 3600 if fresh else settings.wechat_traffic_min_interval_hours * 3600
        if r.traffic_at is None or (now - r.traffic_at).total_seconds() >= min_i:
            rows.append(r)
    # 排序:48h 内新文最优先(早期增速信号最值钱),其次没采过的,再按发现时间新→旧
    rows.sort(key=lambda r: (0 if (r.created_at and r.created_at >= young_cutoff) else 1,
                             r.traffic_at is not None,
                             r.traffic_at or datetime(1970, 1, 1),
                             -r.created_at.timestamp()))
    rows = rows[:limit]
    if not rows:
        return {"platform": "wechat_traffic", "status": "skipped", "reason": "no_targets"}

    try:
        balance = client.remain_money()
    except DajialaError as exc:
        _record_run(session, user_id, "wechat_traffic", "failed", f"{type(exc).__name__}: {exc}")
        session.commit()
        raise
    affordable = int(balance / 0.06)
    if affordable <= 0:
        _record_run(session, user_id, "wechat_traffic", "skipped", f"low_balance={balance:.2f}")
        session.commit()
        return {"platform": "wechat_traffic", "status": "skipped", "reason": "low_balance",
                "balance": balance, "targets": len(rows)}
    rows = rows[:affordable]

    sampled = 0
    now = datetime.now()
    for r in rows:
        try:
            data = client.read_zan_pro(r.url)
        except DajialaNoBalance:
            logger.warning("阅读量采样中途余额不足(用户 %s),已采 %d 篇", user_id, sampled)
            break
        except DajialaError as exc:
            logger.warning("阅读量采样失败 url=%s:%s", r.url, exc)
            continue
        prev_read = r.read_num if (r.sample_count or 0) >= 1 else None
        _apply_sample(session, user_id, r, data, now)
        sampled += 1
        # 趋势判定:相邻采样增长达标 + 绝对量达标 → 爆点苗头(即时推送);大幅下滑 → 回落
        if prev_read is not None:
            growth = (r.read_num - prev_read) / max(prev_read, 1) * 100
            if growth <= -20:
                r.trend_flag = "回落"
            elif growth >= settings.wechat_resample_growth_pct and r.read_num >= settings.wechat_burst_min_reads:
                r.trend_flag = "爆点苗头"
                _notify_burst(session, user_id, settings, r, growth)
        # 账号基线:首采样阅读 ≥ 该号历史首采中位数×3 → 早期苗头(无需等趋势)
        if (r.sample_count or 0) == 1 and r.benchmark_id:
            base_vals = sorted(v for v in session.scalars(select(WechatArticle.first_read_num).where(
                WechatArticle.user_id == user_id, WechatArticle.benchmark_id == r.benchmark_id,
                WechatArticle.id != r.id, WechatArticle.first_read_num > 0)).all() if v)
            if len(base_vals) >= 3:
                median = base_vals[len(base_vals) // 2]
                if r.first_read_num >= median * 3 and r.first_read_num >= settings.wechat_burst_min_reads:
                    r.trend_flag = "爆点苗头"
                    _notify_burst(session, user_id, settings, r, None, baseline=median)
    session.commit()
    _record_run(session, user_id, "wechat_traffic", "success", f"sampled={sampled}")
    session.commit()
    return {"platform": "wechat_traffic", "status": "success", "sampled": sampled,
            "balance_after": client.remain_money() if sampled else balance}

def pan_cookie_keepalive_tick(settings: Settings | None = None) -> int:
    """每日定时巡检网盘转存 Cookie(夸克 + 百度网盘),失效即时告警。返回健康的 (用户,平台) 数。

    旧版三处不对等:① `if not settings.quark_cookie: return 0` —— 只在「Cookie 管理」按用户配了
    凭据的人**根本没被巡检过**,于是"失效"只能等到监听里转存炸了才报;② 百度盘完全没有保活,
    而它的失败(errno)长得像"对方链接失效",没人会怀疑是自己 Cookie 死了;③ 告警挂到 id 最小的
    用户、板块写成 xianyu(发到闲鱼群)。夸克保活仍是轻量列目录(顺带滚动延长 __puus)。
    同一份 Cookie 只探一次、只告警一次:多用户共用全局凭据时不重复撞接口也不刷屏。
    """
    from app.services.alert_service import notify_incident
    from app.db import get_session_local
    from app.db.models import User
    from app.services.baidupan_transfer import BaiduPanAuthError, BaiduPanClient
    from app.services.cookie_store import get_cookie
    from app.services.quark_transfer import QuarkAuthError, QuarkTransfer

    settings = settings or get_settings()
    if not settings.pan_transfer_enabled:
        return 0
    _NICK = {"quark": "夸克", "baidupan": "百度网盘"}
    _FIX = {"quark": "请浏览器登录 pan.quark.cn 后 F12 复制 Cookie,更新到「Cookie 管理」页的 "
                      "quark 平台(或 .env 的 QUARK_COOKIE)",
            "baidupan": "请浏览器登录 pan.baidu.com 后复制含 BDUSS 的 Cookie,更新到"
                        "「Cookie 管理」页的 baidupan 平台(百度盘没有全局默认值,只能按用户配)"}

    def _probe(platform: str, cookie: str) -> str:
        """ok / auth(凭据已死,要人工换) / error(网络或风控,不该报"Cookie 失效")。"""
        try:
            if platform == "quark":
                QuarkTransfer(cookie).keepalive()
            else:
                BaiduPanClient(cookie).keepalive()
            return "ok"
        except (QuarkAuthError, BaiduPanAuthError) as exc:
            logger.error("%s Cookie 已失效:%s", _NICK[platform], exc)
            return f"auth:{exc}"
        except Exception as exc:  # noqa: BLE001 - 瞬时故障留到下轮,不惊动运营者
            logger.warning("%s 保活异常:%s", _NICK[platform], exc)
            return "error"

    db = get_session_local()()
    healthy = 0
    results: dict[tuple[str, str], str] = {}   # (平台, Cookie) → 探测结论,同凭据只探一次
    warned: set[tuple[str, str]] = set()
    try:
        users = db.scalars(select(User.id).where(User.enabled.is_(True)).order_by(User.id)).all()
        for uid in users:
            for platform in ("quark", "baidupan"):
                fallback = settings.quark_cookie if platform == "quark" else ""
                ck = (get_cookie(db, uid, platform) or fallback or "").strip()
                if not ck:
                    continue
                key = (platform, ck)
                if key not in results:
                    results[key] = _probe(platform, ck)
                if results[key] == "ok":
                    healthy += 1
                elif results[key].startswith("auth:") and key not in warned:
                    warned.add(key)
                    notify_incident(db, uid, "wechat",
                                    f"🟠 {_NICK[platform]} Cookie 已失效,转存功能停用",
                                    f"{results[key][5:]}。{_FIX[platform]};监听不受影响,仅转存暂停",
                                    settings=settings)
    finally:
        db.close()
    return healthy


def traffic_tick(settings: Settings | None = None) -> int:
    """每日定时:给所有(有对标号的)用户采样一轮阅读量。返回采样总篇数。"""
    from app.db import get_session_local
    from app.db.models import User
    from sqlalchemy import func as sa_func

    settings = settings or get_settings()
    # 不再基于全局 settings.dajiala_key 早退:sample_traffic 内部按用户走 _dajiala_key
    # 隔离,普通用户在平台内配了自己的 key 但全局 key 空时,整轮采样不应被跳过。
    db = get_session_local()()
    total = 0
    try:
        users = db.scalars(select(User.id).where(User.enabled.is_(True)).order_by(User.id)).all()
        for uid in users:
            active = db.scalar(select(sa_func.count()).select_from(WechatBenchmark).where(
                WechatBenchmark.user_id == uid, WechatBenchmark.active.is_(True)))
            if not active:
                continue
            try:
                out = sample_traffic(db, uid, settings=settings)
                if out.get("sampled"):
                    total += out["sampled"]
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("阅读量采样失败 user=%s", uid)
    finally:
        db.close()
    if total:
        logger.info("每日阅读量采样完成:共 %d 篇", total)
    return total


# ---------------------------------------------------------------- 候选对标号发现
_TERM_STOPWORDS = ("链接", "入口", "获取", "教程", "分享", "合集", "全套", "更新", "最新",
                   "直达", "自取", "完整版", "白嫖", "点击", "关注", "原文", "公众号", "爆火")
_PUNCT_RE = re.compile(r"[^\w]+")     # 标点→空格(分段用,\w 含中文/字母/数字)
_HAS_CJK = re.compile(r"[一-鿿]")     # 片段需含 ≥2 个汉字,滤掉纯数字/英文碎片

def _overlap(a: str, b: str) -> int:
    """两片段最长公共子串长度(去重叠冗余用,串长 ≤6,暴力可)。"""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best

_ENTITY_RE = re.compile(r"[《【](.*?)[》】]")

def mine_title_entities(titles: list[str], top: int = 6) -> list[str]:
    """从标题的书名号《》/【】标记中提取实体名(游戏/测试/资料名),作为首选搜索词。

    引流号标题套路:实体名必然被标记突出(《乡村晋升录》《花少2》【附链接】),
    实体名即业务对象——比滑窗碎片精准得多,出现 1 次就值得搜。
    """
    from collections import Counter

    counter: Counter[str] = Counter()
    for raw in titles or []:
        for name in _ENTITY_RE.findall(str(raw or "")):
            name = name.strip(" -—|")
            if (2 <= len(name) <= 20 and len(_HAS_CJK.findall(name)) >= 1
                    and not any(stop in name for stop in _TERM_STOPWORDS)):
                counter[name] += 1
    return [name for name, _n in sorted(counter.items(), key=lambda x: -x[1])][:top]

def mine_title_terms(titles: list[str], top: int = 6) -> list[str]:
    """从已入库文章标题挖高频内容词(段内 4~6 字滑窗,剔除营销泛词),供候选发现当搜索词。

    同类引流号的标题套路高度一致("花少2人格测试""乡镇晋升录"等),滑窗频次
    天然聚出内容词。要点:① 按标点分段滑窗,不产生跨词碎片;② 片段须含
    ≥2 个汉字(滤"2026"类);③ 与已选片段重叠 ≥3 字的冗余片段剔除。
    """
    from collections import Counter

    counter: Counter[str] = Counter()
    for raw in titles or []:
        for seg in _PUNCT_RE.sub(" ", str(raw or "")).split():
            if len(seg) < 4:
                continue
            for size in (4, 5):
                for i in range(max(0, len(seg) - size + 1)):
                    frag = seg[i:i + size]
                    if any(stop in frag for stop in _TERM_STOPWORDS):
                        continue
                    if len(_HAS_CJK.findall(frag)) < 2:
                        continue
                    if frag[0].isdigit() or frag[-1].isdigit():
                        continue  # 数字粘边的窗口是跨界碎片(如"2人格测""测试20")
                    counter[frag] += 1
    ranked = [(frag, n) for frag, n in counter.items() if n >= 2]
    ranked.sort(key=lambda x: (-x[1], len(x[0])))  # 同频优先短词(4 字词粒度最稳,防跨界碎片占位)
    picked: list[str] = []
    for frag, _n in ranked:
        if any(frag in p or p in frag or _overlap(frag, p) >= 3 for p in picked):
            continue
        picked.append(frag)
        if len(picked) >= top:
            break
    return picked

def discover_candidates(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    """一轮候选对标号发现:标题画像词+配置词 → 搜狗搜文章 → 按公众号名去重入库 → 推飞书。

    与现有对标号同名、或已在候选表(new 态)的跳过;搜狗验证码连续拦截 2 词即收手。
    免费(搜狗免账号);候选需人工在微信读书内关注 + 书架导入后成为正式对标号。
    """
    settings = _base(settings)
    terms = [t.strip() for t in (settings.candidate_search_terms or "").split(",") if t.strip()]
    titles = session.scalars(select(WechatArticle.title).where(
        WechatArticle.user_id == user_id).order_by(WechatArticle.created_at.desc()).limit(200)).all()
    # 首选:标题标记实体(《游戏名》《测试名》,最精准);兜底:滑窗高频内容词
    for term in [*mine_title_entities(list(titles), top=settings.candidate_mine_terms),
                 *mine_title_terms(list(titles), top=settings.candidate_mine_terms)]:
        if term not in terms:
            terms.append(term)
    terms = terms[: settings.candidate_max_terms] or ["网盘资源"]

    known = set(session.scalars(select(WechatBenchmark.nickname).where(
        WechatBenchmark.user_id == user_id)).all())
    seen = set(session.scalars(select(WechatCandidate.name).where(
        WechatCandidate.user_id == user_id, WechatCandidate.status == "new")).all())
    new_rows: list[WechatCandidate] = []
    blocked = 0
    for term in terms:
        res = sogou_search_articles(term)
        if res["blocked"]:
            blocked += 1
            if blocked >= 2:
                logger.warning("搜狗验证码连续拦截,候选发现提前收手(用户 %s)", user_id)
                break
            continue
        for it in res["items"]:
            name = it["name"]
            if not name or name in known or name in seen:
                continue
            seen.add(name)
            new_rows.append(WechatCandidate(user_id=user_id, name=name, title=it["title"],
                                            title_ts=it.get("published_at"), term=term[:64]))
    if new_rows:
        session.add_all(new_rows)
        session.commit()
        # LLM 评级:判定"资源号/营销号/无关"+优先级,写入候选 note 供人工参考
        if settings.deepseek_api_key:
            try:
                from app.services.llm_client import rank_candidates
                payload = [{"name": c.name, "title": c.title} for c in new_rows]
                ranked = rank_candidates(settings.deepseek_base_url, settings.deepseek_api_key,
                                         settings.deepseek_model, payload[:15])
                if ranked:
                    by_name = {r["name"]: r for r in ranked}
                    for c in new_rows:
                        r = by_name.get(c.name)
                        if r:
                            c.term = (f"{c.term}|LLM:{r['verdict']}({r['priority']})")[:64]
                    session.commit()
                    kept = sum(1 for r in ranked if r.get("verdict") == "资源号")
                    # 资源号排前,并按优先级排序 → 推送时用户先看到值得关注的
                    new_rows.sort(key=lambda c: (
                        0 if "资源号" in (c.term or "") and "高" in (c.term or "") else
                        1 if "资源号" in (c.term or "") else 2, c.id))
                    logger.info("LLM 候选评级:%d/%d 判定为资源号", kept, len(ranked))
            except Exception:  # noqa: BLE001 - 评级失败不影响候选入库
                logger.exception("LLM 候选评级失败")
        _push_candidates(session, user_id, settings, new_rows)
    _record_run(session, user_id, "wechat_candidates", "success",
                f"terms={len(terms)} new={len(new_rows)} blocked={blocked}")
    session.commit()
    return {"platform": "wechat", "status": "success", "terms": terms,
            "new": len(new_rows), "blocked": blocked}

def _push_candidates(session: Session, user_id: int, settings: Settings,
                     rows: list[WechatCandidate]) -> None:
    """候选清单推公众号专属飞书群(column_set 网格卡片:公众号/代表文章/来源词 三列对齐)。"""
    from app.services.feishu import _col_set_row, _md_safe, webhook_for
    from app.services.feishu_client import FeishuClient

    wh = webhook_for(settings, "wechat")
    main_wh = settings.feishu_webhook
    targets = list(dict.fromkeys(filter(None, [wh, main_wh])))  # 去重保序
    if not targets:
        return
    # 免打扰时段(默认 23~8 点):候选号没有盘链/阅读量概念(那两字段属于文章),
    # 不存在"紧急",一律延后到白天推——此前误用了文章的 pan_types/read_num 过滤,
    # 直接 AttributeError 炸掉整轮监听的候选推送(2026-09-14 实测)。
    if is_quiet_hours(settings):
        logger.info("免打扰时段,延迟推送 %d 条候选号", len(rows))
        return
    elements: list[dict] = [
        {"tag": "note", "elements": [{"tag": "plain_text",
            "content": "手机微信读书搜索关注该号 → 监听页「从微信读书书架导入」即自动进监听"}]},
        _col_set_row([("**公众号**", 3), ("**代表文章**", 7), ("**来源词**", 2)], grey=True),
    ]
    for r in rows[:20]:
        title = _md_safe(r.title)
        ts = r.title_ts.strftime("%m-%d") if r.title_ts else ""
        shown = title[:30] + ("…" if len(title) > 30 else "")
        llm_tag = ""
        if "LLM:资源号(高)" in (r.term or ""):
            llm_tag = "🔴高优先 "
        elif "资源号" in (r.term or ""):
            llm_tag = "🟢资源号 "
        elements.append(_col_set_row([
            ((llm_tag + _md_safe(r.name))[:14] or "—", 3),
            (f"《{shown}》{f' ({ts})' if ts else ''}", 7),
            (_md_safe(r.term)[:8] or "—", 2),
        ]))
    if len(rows) > 20:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text",
            "content": f"…另有 {len(rows) - 20} 个,见平台候选列表"}]})
    for target in targets:  # 主群 + 专属群都推(此前只发 wh,主群永远收不到候选)
        try:
            FeishuClient(target, settings.feishu_secret).send_card({
                "config": {"wide_screen_mode": True},
                "header": {"template": "blue", "title": {"tag": "plain_text",
                    "content": f"🔍 候选对标号 · 新发现 {len(rows)} 个"}},
                "elements": elements,
            })
        except Exception:  # noqa: BLE001 - 推送失败不影响采集结果
            logger.exception("候选对标号飞书推送失败 user=%s", user_id)

def run_full_sync_if_pending(session: Session, user_id: int,
                             settings: Settings | None = None) -> dict:
    """若 renewal 刚换出可用 Cookie(会话初期),对全部对标号跑一轮全量补采。

    背景:cover 只返回"最新一篇",部分号的书架快照冻结在关注时点,此后新文全部
    看不见;mp/articles(历史列表)仅在 Cookie 会话初期可用——正好在 renewal 成功
    (必然伴随新会话)后的窗口里把停更文章一次性补齐。
    每号独立容错,单号失败不阻断;完成后清除标记。
    """
    from app.db.models import SystemConfig

    settings = settings or get_settings()
    flag_key = f"weread_fullsync_pending_{user_id}"
    flag = session.scalar(select(SystemConfig).where(SystemConfig.key == flag_key))
    if not flag:
        return {"status": "not_pending"}
    cookie = _weread_cookie(session, user_id, settings)
    if not cookie:
        return {"status": "skipped", "reason": "no_cookie"}
    rows = session.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.active.is_(True),
        WechatBenchmark.weread_book_id != "")).all()
    synced = articles_new = failed = 0
    for b in rows:
        try:
            out = sync_wechat_account(session, user_id, b.id, settings=settings)
            if out.get("weread_list") == "limited":
                # 列表接口对本会话已耗尽:后面 80 个号也只会各撞一次并退化成"最新一篇",
                # 白烧微信读书调用密度。标记保留,等下个新 skey 会话窗口再补。
                logger.warning("mp/articles 预算耗尽(-2041),本轮补采中止;"
                               "下次新会话窗口再补(已补 %s 号 %s 篇)", synced, articles_new)
                return {"status": "aborted", "reason": "rate_limited",
                        "synced": synced, "new_articles": articles_new}
            if out.get("status") == "success":
                synced += 1
                articles_new += int(out.get("new") or 0)  # sync 的计数字段就叫 new
            else:
                failed += 1
        except WereadError as exc:
            # -2041 = 该 skey 的列表接口预算已耗尽:全局放弃本轮(标记保留,
            # 下个新 skey 会话再补),否则其余 80 号每号白撞一次
            session.rollback()
            if "-2041" in str(exc):
                logger.warning("mp/articles 预算耗尽(-2041),本轮补采中止;下次新会话窗口再补")
                return {"status": "aborted", "reason": "rate_limited",
                        "synced": synced, "new_articles": articles_new}
            failed += 1
        except Exception:  # noqa: BLE001 - 单号失败不阻断全量
            session.rollback()
            failed += 1
        time.sleep(2.5)  # mp/articles 会话初期窗口有限,克制使用
    session.delete(flag)
    session.commit()
    logger.info("全量补采完成(用户 %s):同步 %s 号,新增 %s 篇,失败 %s",
                user_id, synced, articles_new, failed)
    return {"status": "done", "synced": synced, "new_articles": articles_new, "failed": failed}


def keyword_article_tick(session: Session, user_id: int, settings: Settings | None = None) -> int:
    """关键词文章监控:按用户配置的关键词(candidate_search_terms)搜最新文章,
    盘链文即时推飞书——补齐"对标号没发但全网已有人发"的盲区。

    数据源:搜狗微信文章搜索(免费);每词限 1 页,验证码连续 2 词即收手。
    返回推送条数。"""
    settings = settings or get_settings()
    terms = [x.strip() for x in (settings.keyword_search_terms or "").split(",") if x.strip()]
    if not terms:
        return 0
    from app.services.sogou_weixin import search_articles
    from app.services.alert_service import feishu_alert_gate
    from app.services.feishu import _col_set_row
    from app.services.feishu_client import FeishuClient, webhook_for

    webhook = webhook_for(settings, "wechat")
    if not webhook:
        return 0

    hits: list[dict] = []
    blocked = 0
    known_titles = set()
    for term in terms[:5]:  # 最多 5 个词/轮,防搜狗验证码
        res = search_articles(term)
        if res["blocked"]:
            blocked += 1
            if blocked >= 2:
                break
            continue
        for it in res["items"][:10]:
            title = (it.get("title") or "").strip()
            if not title or title in known_titles:
                continue
            known_titles.add(title)
            if title_hits(title):
                hits.append({"title": title, "name": it.get("name", ""),
                             "digest": it.get("digest", ""), "term": term})
        if len(hits) >= settings.focus_max_items:
            break
    if not hits:
        return 0

    # 冷却去重:同标题 24h 一次。先"只读探测"筛出未冷却的候选(不发冷却门),
    # 截断到 focus_max_items 后发送;发送成功仅对入卡展示的条目烧冷却。
    # 否则越限条目会在这里被 feishu_alert_gate 烧掉冷却却从未进卡片(下轮又因
    # 自身冷却被排除,永无出头之日)——与 focus_alert 同源的"门烧全部、只推前 N"缺陷。
    now = datetime.now()
    cooldown = timedelta(hours=settings.focus_cooldown_hours)
    fresh = []
    for h in hits:
        row = session.scalar(select(FeishuAlert).where(
            FeishuAlert.user_id == user_id, FeishuAlert.section == "kw_article",
            FeishuAlert.title == h["title"][:120]))
        if row and (now - row.alerted_at) < cooldown:
            continue
        fresh.append(h)
    if not fresh:
        return 0
    kept = fresh[: settings.focus_max_items]

    elements = [_col_set_row([("**标题**", 6), ("**公众号**", 3), ("**关键词**", 3)], grey=True)]
    for h in kept:
        elements.append(_col_set_row([
            (f"🔴 {_md_safe_light(h['title'])[:30]}", 6),
            (_md_safe_light(h["name"])[:12], 3),
            (h["term"][:12], 3)]))
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "orange", "title": {"tag": "plain_text",
                       "content": f"🔑 关键词文章 · {len(kept)} 篇(全网,不限对标号)"}},
            "elements": elements}
    sent = FeishuClient(webhook, settings.feishu_secret).send_card(card)
    if not sent:
        session.rollback()  # 发送失败不烧冷却门,下次还能再推(探测阶段未写库,回滚为空操作)
        return 0
    # 发送成功:仅对入卡展示的 kept 烧冷却,越限项保留冷却位下轮可轮候进入
    for h in kept:
        feishu_alert_gate(session, user_id, "kw_article", h["title"][:120],
                          settings.focus_cooldown_hours, f"关键词:{h['term']}")
    session.commit()
    return len(kept)


def keyword_article_all_users(settings: Settings | None = None) -> int:
    """调度入口(无参):keyword_article_tick 需要 per-user session,由此遍历用户。

    此前调度表直接注册了带 (session, user_id) 的函数,每次触发 TypeError 被
    _safe 吞掉——关键词文章监控从未真正运行过(2026-09-14 审计发现)。
    """
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    db = get_session_local()()
    total = 0
    try:
        for uid in db.scalars(select(User.id).where(User.enabled.is_(True)).order_by(User.id)).all():
            try:
                total += keyword_article_tick(db, uid, settings)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("关键词文章监控失败 user=%s", uid)
        db.commit()
    finally:
        db.close()
    return total


def candidate_discover_tick(settings: Settings | None = None) -> int:
    """每日定时:为所有(有对标号的)用户发现一轮同类候选号。返回新增候选数。"""
    from app.db import get_session_local
    from app.db.models import User
    from sqlalchemy import func as sa_func

    settings = settings or get_settings()
    db = get_session_local()()
    total = 0
    try:
        users = db.scalars(select(User.id).where(User.enabled.is_(True)).order_by(User.id)).all()
        for uid in users:
            has_bm = db.scalar(select(sa_func.count()).select_from(WechatBenchmark).where(
                WechatBenchmark.user_id == uid, WechatBenchmark.active.is_(True)))
            if not has_bm:
                continue
            try:
                out = discover_candidates(db, uid, settings=settings)
                total += out.get("new", 0)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("候选对标号发现失败 user=%s", uid)
    finally:
        db.close()
    if total:
        logger.info("候选对标号发现完成:新增 %d 个", total)
    return total

def list_candidates(session: Session, user_id: int) -> list[dict]:
    """候选列表(新→旧);`imported`=该名已是正式对标号(书架导入后自然闭环)。"""
    rows = session.scalars(select(WechatCandidate).where(
        WechatCandidate.user_id == user_id).order_by(WechatCandidate.id.desc()).limit(200)).all()
    known = set(session.scalars(select(WechatBenchmark.nickname).where(
        WechatBenchmark.user_id == user_id)).all())
    return [{"id": r.id, "name": r.name, "title": r.title, "term": r.term,
             "status": r.status, "imported": r.name in known,
             "title_ts": r.title_ts.isoformat(sep=" ", timespec="seconds") if r.title_ts else None,
             "discovered_at": r.discovered_at.isoformat(sep=" ", timespec="seconds")}
            for r in rows]

def set_candidate_status(session: Session, user_id: int, candidate_id: int, status: str) -> None:
    """更新候选状态(仅 new/dismissed);dismissed 后不再进入去重表,允许未来重新发现。"""
    if status not in ("new", "dismissed"):
        raise ValueError("非法状态")
    row = session.scalar(select(WechatCandidate).where(
        WechatCandidate.user_id == user_id, WechatCandidate.id == candidate_id))
    if row is None:
        raise KeyError("候选不存在")
    row.status = status
    session.commit()
