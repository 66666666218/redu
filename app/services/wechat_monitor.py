"""公众号监听/同步(见 doc/dajiala-api.md 与 doc/dev.md §5.12)。

三个用户入口:
- 对标号管理:`add_benchmark`(贴任意文章链即加号,免费)/`list_benchmarks`/`remove_benchmark`;
  也可从微信读书书架一键导入(`import_benchmarks_from_shelf`,免费);
- 监听 `run_wechat_listen`:**双数据源,免费优先**——
  ① 对标号有 `weread_book_id` 且配了微信读书 Cookie → `WereadClient.latest_article`
  (`/api/mp/cover`,免费)拿最新一篇,新文按链接去重入库;
  ② 否则(或微信读书失效)→ dajiala `post_condition`(¥0.14/号)拿**当天全部发文**;
  标题命中网盘关键词的文,优先用微信读书正文(免费)、其次自抓原文页,做盘链确认
  (pan.quark.cn 等四家正则),新文推公众号专属飞书群;
  dajiala 余额不足时仅禁用付费源(post_condition/即时采样),免费源照常监听。
- 同步 `sync_wechat_account`:dajiala `history_by_ghid` 翻页(`PagingInfo.Offset`/`IsEnd`)
  拉历史文章入库,默认 `wechat_sync_max_pages` 页封顶(每页 ¥0.14);仅有微信读书源时
  只能拿最新一篇(旧列表接口已被微信读书废弃),返回 `partial`。

设计原则"免费优先":微信读书正文与原文页自抓都免费,dajiala 仅作兜底与阅读量采样。
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime, timedelta

import requests
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import (FeishuAlert, WechatArticle, WechatBenchmark, WechatCandidate,
                           WechatPanLink, WechatTrafficSample)
from app.services.dajiala_client import DajialaClient, DajialaError, DajialaNoBalance
from app.services.quark_transfer import QuarkAuthError, QuarkError, QuarkTransfer, extract_quark_urls
from app.services.reader_platform_client import PlatformError, ReaderPlatformClient
from app.services.sogou_weixin import search_articles as sogou_search_articles
from app.services.tenant_base import _base, _record_run
from app.services.content_extract import extract_account_refs
from app.services.weread_client import WereadAuthError, WereadClient, WereadError, build_mp_url
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
TITLE_HINTS = ("夸克", "百度网盘", "百度云", "UC网盘", "UC盘", "迅雷", "阿里云盘",
               "网盘", "资源", "全套", "合集", "分享", "链接", "更新")
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


def fetch_article_content(url: str, timeout: int = 15) -> str:
    """免费自抓微信文章正文(纯文本)。命中风控("环境异常"验证页)返回空串。

    文章页是公开网页;数据中心 IP 可能被"环境异常"拦截——调用方应容忍空结果,
    需要兜底时才走 dajiala article_detail(¥0.01/次)。
    """
    if not url:
        return ""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": _UA})
        text = resp.text or ""
    except requests.RequestException:
        return ""
    if resp.status_code != 200 or "环境异常" in text:
        return ""
    m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*<script', text, re.S)
    body = m.group(1) if m else text
    body = re.sub(r"<[^>]+>", " ", body)
    body = html_mod.unescape(body)
    return re.sub(r"\s{2,}", " ", body).strip()[:100000]


def extract_article_meta(url: str, timeout: int = 15) -> dict:
    """免费解析文章页元信息:{biz, name, title}(与 xg 同款正则);失败/风控页返回 {}。"""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": _UA})
        text = resp.text or ""
    except requests.RequestException:
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
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
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
    if settings.dajiala_key and (not ghid or not nickname):
        try:
            obj = DajialaClient(settings.dajiala_key).post_condition(url)
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
    row = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.id == benchmark_id))
    if row is None:
        raise KeyError("对标账号不存在")
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
def _weread_cookie(session: Session, user_id: int, settings: Settings) -> str:
    """微信读书 Cookie:优先用户在平台内配置的「weread」,其次全局 WEREAD_COOKIE。"""
    from app.services.cookie_store import get_cookie

    return (get_cookie(session, user_id, "weread") or settings.weread_cookie or "").strip()


def weread_shelf(session: Session, user_id: int, settings: Settings | None = None) -> list[dict]:
    """列出微信读书书架上的公众号(导入预览;需先在微信读书 App 内关注目标号)。"""
    settings = _base(settings)
    cookie = _weread_cookie(session, user_id, settings)
    if not cookie:
        raise ValueError("未配置微信读书 Cookie(平台 Cookie「weread」或 WEREAD_COOKIE)")
    return WereadClient(cookie).shelf()


def import_benchmarks_from_shelf(session: Session, user_id: int,
                                 settings: Settings | None = None) -> dict:
    """微信读书书架一键导入:MP_WXS_* 条目 → 对标号(免费,自动关联 weread_book_id)。"""
    settings = _base(settings)
    cookie = _weread_cookie(session, user_id, settings)
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


def refresh_weread_cookie(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    """微信读书 Cookie 续期:长效 wr_rt → 新短效 wr_skey,并回写 Cookie 管理。

    wr_skey 短效且轮换(续期后旧 skey 很快 -2012),故续期成功**必须回写**;
    全局 WEREAD_COOKIE(.env)无法回写文件,统一落到平台内「weread」Cookie
    (读取优先级:平台内 > 全局,下次监听即用新值)。
    返回 {status: success|skipped|failed, reason?, verified, cookie?}。
    """
    from app.services.cookie_store import get_cookie, set_cookie

    settings = _base(settings)
    cookie = (get_cookie(session, user_id, "weread") or settings.weread_cookie or "").strip()
    if not cookie:
        return {"status": "skipped", "reason": "no_cookie"}
    if "wr_rt=" not in cookie:
        return {"status": "skipped", "reason": "no_rt"}
    new_cookie = WereadClient(cookie).refresh_skey()
    if not new_cookie:
        return {"status": "failed", "reason": "renewal_failed"}
    set_cookie(session, user_id, "weread", new_cookie)
    verified = False
    try:
        WereadClient(new_cookie).shelf()
        verified = True
    except WereadError as exc:
        logger.warning("微信读书续期后书架验证未通过(用户 %s):%s", user_id, exc)
    logger.info("微信读书 Cookie 已续期(用户 %s,验证%s)", user_id, "通过" if verified else "未通过")
    return {"status": "success", "verified": verified, "cookie": new_cookie}


def weread_refresh_tick(settings: Settings | None = None) -> int:
    """每日定时:为所有配置了微信读书 Cookie 的用户续期(防 wr_skey 过期断免费源)。

    返回续期成功的账号数;单用户失败不影响其余。
    """
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    db = get_session_local()()
    total = 0
    try:
        users = db.scalars(select(User.id).order_by(User.id)).all()
        for uid in users:
            try:
                out = refresh_weread_cookie(db, uid, settings=settings)
                if out.get("status") == "success":
                    total += 1
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("微信读书续期失败 user=%s", uid)
    finally:
        db.close()
    if total:
        logger.info("微信读书 Cookie 每日续期完成:%d 个账号", total)
    return total


# ---------------------------------------------------------------- 监听
def _insert_new_articles(session: Session, user_id: int, benchmark: WechatBenchmark,
                         items: list[dict], source: str, fetch_content: bool = False,
                         content_resolver=None, require_pan: bool = True) -> list[WechatArticle]:
    """按链接去重入库;网盘类型=标题 + (可选)自抓正文 的并集。"""
    existing = set(session.scalars(select(WechatArticle.url).where(
        WechatArticle.user_id == user_id, WechatArticle.url != "")).all())
    added: list[WechatArticle] = []
    for it in items:
        url = it["url"]
        if url in existing:
            continue
        existing.add(url)
        types = detect_pan_types(it["title"])
        preset_read = int(it.get("read_num") or 0)
        preset_like = int(it.get("like_num") or 0)
        content = ""
        if title_hits(it["title"]):
            if content_resolver:  # 免费源注入(微信读书正文)
                content = content_resolver(it["title"]) or ""
            elif fetch_content:
                content = fetch_article_content(url)
            if content:  # 自抓成功 → 用正文的链接判定覆盖标题的盘名猜测
                types = detect_pan_types(content) or types
        pan_urls = extract_quark_urls(f'{it["title"]} {content}')
        if require_pan and not pan_urls and not types:
            continue  # 无盘链 → 不监控
        quality = assess_quality(content, pan_urls, preset_read)
        row = WechatArticle(user_id=user_id, author=(benchmark.nickname or "未命名")[:128],
                            title=it["title"][:500], url=url[:500], content=content,
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
                         client: DajialaClient | None, allow_paid: bool = True) -> dict[int, list[tuple[str, str, str]]]:
    """新文后处理(推送前):① 即时采样阅读量(¥0.06/篇,上限 wechat_listen_sample_limit);
    ② 夸克转存盘链 → 换自己的分享链并持久化到 `my_pan_urls`。

    `allow_paid=False`(dajiala 余额不足)时连即时采样也跳过,只做免费的夸克转存;
    返回 {article_id: [(原链, 我的链, 提取码)]} 供飞书推送;失败回落原链接,绝不阻塞监听。
    """
    replacements: dict[int, list[tuple[str, str, str]]] = {}
    if not rows:
        return replacements
    if allow_paid and settings.wechat_listen_sample_new and settings.dajiala_key:
        client = client or DajialaClient(settings.dajiala_key)
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
    if settings.pan_transfer_enabled and settings.quark_cookie:
        quark = QuarkTransfer(settings.quark_cookie)
        for r in rows:
            dead = False
            for u in [x.strip() for x in (r.pan_urls or "").splitlines() if x.strip()][:3]:
                try:
                    res = quark.transfer_and_share(u, save_dir=settings.quark_save_dir,
                                                   password=settings.quark_share_password)
                except QuarkAuthError as exc:
                    logger.error("夸克 Cookie 失效,本轮停止转存:%s", exc)
                    dead = True
                    break
                except QuarkError as exc:
                    logger.warning("夸克转存失败 {}:{}(推送保留原链接)", u, exc)
                    continue
                mine = [x for x in (r.my_pan_urls or "").splitlines() if x.strip()]
                mine.append(res["share_url"] + (f" (提取码 {res['password']})" if res["password"] else ""))
                r.my_pan_urls = chr(10).join(mine)[:2000]
                replacements.setdefault(r.id, []).append((u, res["share_url"], res["password"]))
            if dead:
                break
    # 资源级共振:同一盘链在窗口期内被 ≥2 篇文章推送 → 同行网络都在发的确认级爆点资源
    from app.services.feishu import _col_set_row
    from app.services.feishu_client import FeishuClient, webhook_for
    from app.services.alert_service import feishu_alert_gate

    _backfill_pan_links(session)  # 一次性回填归一化表建成前的旧文章盘链
    checked: set[str] = set()
    res_hits: list[tuple[str, WechatArticle, int]] = []
    res_window = datetime.now() - timedelta(hours=settings.wechat_resonance_hours)
    for r in rows:
        for u in [x.strip() for x in (r.pan_urls or "").splitlines() if x.strip()]:
            if u in checked:
                continue
            checked.add(u)
            cnt = session.scalar(select(func.count()).select_from(WechatPanLink).where(
                WechatPanLink.pan_url == u,
                WechatPanLink.created_at >= res_window,
                WechatPanLink.article_id != r.id)) + 1  # +1 = 本篇自身
            if (cnt or 0) >= 2 and feishu_alert_gate(
                    session, user_id, "focus_res", "res:" + u[:120],
                    settings.focus_cooldown_hours, f"{cnt} 篇同发"):
                res_hits.append((u, r, cnt))
    if res_hits:
        webhook = webhook_for(settings, "wechat")
        if webhook:
            elements = [_col_set_row([("**分享链**", 5), ("**同发文章数**", 2), ("**示例标题**", 5)], grey=True)]
            for u, r, cnt in res_hits[: settings.focus_max_items]:
                elements.append(_col_set_row([
                    ("🔴 [" + u[:40] + "](" + u + ")", 5), (str(cnt) + " 篇", 2), (r.title[:30], 5)]))
            card = {"config": {"wide_screen_mode": True},
                    "header": {"template": "red", "title": {"tag": "plain_text",
                               "content": "🔴 资源共振 · 多号同发(" + str(len(res_hits)) + " 个资源)"}},
                    "elements": elements}
            FeishuClient(webhook, settings.feishu_secret).send_card(card)

    # 文章内容交叉提取:从正文提取新公众号名 → 自动入库为候选对标号
    from app.services.content_extract import extract_account_refs as _ear
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
                    session: Session) -> list[WechatArticle]:
    """微信读书单号采集:**cover 最新一篇(稳定可用)→ mp/articles 列表(可选,常被限权)。

    实测(2026-09):mp/articles 仅在会话建立初期可用,数小时后被服务端限权(-2041),
    cover 始终可用——故 cover 为主路径,mp/articles 失败静默跳过不影响监听。
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
    except Exception as exc:  # noqa: BLE001 - 限权/废弃不影响 cover 主路径
        logger.debug("mp/articles 不可用(%s),仅用 cover 最新一篇", exc)
    return _insert_new_articles(session, user_id, b, items, source="listen",
                                fetch_content=True)


def run_wechat_listen(session: Session, user_id: int, settings: Settings | None = None,
                      client: DajialaClient | None = None, weread: WereadClient | None = None,
                      platform: ReaderPlatformClient | None = None, push: bool = True) -> dict:
    """监听一轮:双数据源免费优先——微信读书(cover)→ dajiala(当天发文)→ 新文入库推飞书。

    余额不足(dajiala)只禁用付费源与即时采样并返回 `dajiala_skipped:"low_balance"`,
    免费源(读书平台/微信读书)照常监听;全部数据源不可用才返回 `skipped`。
    """
    settings = _base(settings)
    rows = session.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.active.is_(True))
        .order_by(WechatBenchmark.id)).all()
    if not rows:
        return {"platform": "wechat", "status": "skipped", "reason": "no_benchmarks"}
    cookie = _weread_cookie(session, user_id, settings)
    use_dajiala = bool(settings.dajiala_key)
    if not cookie and not use_dajiala:
        return {"platform": "wechat", "status": "skipped", "reason": "no_source"}

    # 余额保护前置:只要有账号需要走 dajiala(无 book_id 或会话失效),先查余额(免费接口)。
    # 余额不足只禁用付费源(微信读书等免费源照常跑),不再整轮跳过——否则空余额把书架号一起饿死。
    dajiala_off = ""
    balance: float | None = None
    needs_dajiala = use_dajiala and any(
        not (cookie and b.weread_book_id) and b.anchor_url for b in rows)
    if use_dajiala:
        client = client or DajialaClient(settings.dajiala_key)
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
                                               fetch_content=True)
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
                got = _weread_collect(user_id, b, weread, session)
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
                        got = _weread_collect(user_id, b, WereadClient(cookie), session)
                        used = True  # 微信读书源已消费本号,勿再走 dajiala 重复扣费
                        if got:
                            new_rows.extend(got)
                            b.miss_count = 0
                            b.last_item_at = now
                    except WereadError as exc2:
                        failed += 1
                        logger.warning("微信读书续期后仍失败 %s:%s", b.nickname or b.weread_book_id, exc2)
                else:
                    cookie = ""  # 续期失败:后续号降级 dajiala;本号不置 used,继续走下方 dajiala 兜底
                    failed += 1
                    # 即时提醒用户更新 Cookie(6h 冷却,不刷屏)
                    from app.services.alert_service import notify_incident
                    notify_incident(
                        session, user_id, "wechat",
                        "🟠 微信读书 Cookie 已过期,请更新",
                        "自动续期失败。请在浏览器登录 weread.qq.com 后 F12 复制 Cookie,"
                        "粘贴到「Cookie 管理」页 weread 平台(或发给我更新)",
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
                                                 fetch_content=True))
    replacements = _enrich_new_articles(session, user_id, settings, new_rows, client,
                                        allow_paid=use_dajiala)

    session.commit()

    status = "success" if not failed or new_rows else ("failed" if failed == len(rows) else "partial")
    detail = f"accounts={len(rows)} new={len(new_rows)} failed={failed}"
    if dajiala_off:
        detail += f" dajiala_off({dajiala_off})"
    _record_run(session, user_id, "wechat_listen", status, detail)
    session.commit()
    if push and new_rows:
        _push_listen(session, user_id, settings, new_rows, replacements)
    out: dict = {"platform": "wechat", "status": status, "accounts": len(rows),
                 "new": len(new_rows), "failed": failed}
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
    """
    from app.services.feishu import _col_set_row, _md_safe, webhook_for
    from app.services.feishu_client import FeishuClient

    wh = webhook_for(settings, "wechat")
    main_wh = settings.feishu_webhook
    targets = list(dict.fromkeys(filter(None, [wh, main_wh])))  # 去重保序
    if not targets:
        return
    replacements = replacements or {}
    elements: list[dict] = [
        {"tag": "note", "elements": [{"tag": "plain_text",
            "content": "点文章标题打开链接(优先你的夸克转存链) · 网盘列=识别到的盘链 · 阅读未采样为 —"}]},
        _col_set_row([("**公众号**", 3), ("**文章**", 7), ("**网盘**", 2), ("**阅读**", 2)], grey=True),
    ]
    for r in rows[:20]:
        rep = replacements.get(r.id) or []
        if rep:
            link = rep[0][1] + (f" (提取码 {rep[0][2]})" if rep[0][2] else "")
        else:
            link = next((x.strip() for x in (r.my_pan_urls or "").splitlines() if x.strip()),
                        "") or r.url
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
        article_md = f"[{q_badge}{shown}]({_md_safe(link)})" if link else shown
        pan = f"🔴{_md_safe(r.pan_types)[:8]}" if r.pan_types else "—"
        read = str(r.read_num) if r.traffic_at else "—"
        elements.append(_col_set_row([
            (_md_safe(r.author)[:10] or "—", 3), (article_md, 7), (pan, 2), (read, 2),
        ]))
    if len(rows) > 20:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text",
            "content": f"…另有 {len(rows) - 20} 篇,见平台文章列表"}]})
    # LLM 叙事层:盘链文优先交给大模型解读(失败/未配 key 静默降级,不影响推送)
    if settings.deepseek_api_key:
        try:
            from app.services.llm_client import narrate_articles

            top = sorted(rows, key=lambda r: (not r.pan_types, -(r.read_num or 0)))[: settings.llm_narrate_limit]
            ctx = [{"title": r.title, "summary": (r.content or "")[:200],
                    "pan_types": r.pan_types} for r in top]
            reading = narrate_articles(settings.deepseek_base_url, settings.deepseek_api_key,
                                       settings.deepseek_model, ctx)
            if reading:
                elements.append({"tag": "hr"})
                elements.append({"tag": "div", "text": {"tag": "lark_md",
                    "content": "🤖 **AI 解读**" + chr(10) + reading[:1500]}})
        except Exception:  # noqa: BLE001 - 叙事失败不影响推送
            logger.exception("LLM 叙事失败 user=%s", user_id)
    for target in targets:
        try:
            FeishuClient(target, settings.feishu_secret).send_card({
                "config": {"wide_screen_mode": True},
                "header": {"template": "blue", "title": {"tag": "plain_text",
                    "content": f"📡 公众号监听 · 新发文 {len(rows)} 篇"}},
                "elements": elements,
            })
        except Exception:  # noqa: BLE001 - 推送失败不影响采集结果
            logger.exception("公众号监听飞书推送失败 user=%s", user_id)


# ---------------------------------------------------------------- 全量同步
def sync_wechat_account(session: Session, user_id: int, benchmark_id: int,
                        settings: Settings | None = None, client: DajialaClient | None = None,
                        max_pages: int | None = None, weread: WereadClient | None = None,
                        platform: ReaderPlatformClient | None = None) -> dict:
    """一键同步:history_by_ghid 翻页拉历史文章入库(¥0.14/页,默认 WECHAT_SYNC_MAX_PAGES 封顶)。"""
    settings = _base(settings)
    b = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.id == benchmark_id))
    if b is None:
        raise KeyError("对标账号不存在")
    plat = platform or _platform_client(settings)
    limit = max(1, int(max_pages or (10 if plat and b.biz else settings.wechat_sync_max_pages)))
    if plat and b.biz:
        added = 0
        pages = 0
        try:
            while pages < limit:
                raw_items = plat.mp_articles(b.biz, page=pages + 1, limit=20)
                norm = [{"title": it["title"], "url": it["url"],
                         "publish_at": _parse_time(it.get("publish_at_raw"))} for it in raw_items]
                got = _insert_new_articles(session, user_id, b, norm, source="sync", require_pan=False)
                added += len(got)
                pages += 1
                if not raw_items or len(got) < len(raw_items):
                    break  # 本页为空或全部已入库 → 更旧的页也必然已见
        except PlatformError as exc:
            logger.warning("读书平台同步失败,转 dajiala/微信读书:%s", exc)
        if pages:
            b.last_item_at = datetime.now()
            _record_run(session, user_id, "wechat_sync", "success",
                        f"platform account={b.nickname} pages={pages} new={added}")
            session.commit()
            return {"platform": "wechat_sync", "status": "success", "pages": pages,
                    "new": added, "ghid": b.ghid, "nickname": b.nickname}
    if not settings.dajiala_key:
        # 无 dajiala:微信读书源只能拿"最新一篇"(列表接口已被微信读书废弃)
        cookie = _weread_cookie(session, user_id, settings)
        if not cookie or not b.weread_book_id:
            return {"platform": "wechat_sync", "status": "skipped", "reason": "no_dajiala_key"}
        wc = weread or WereadClient(cookie)
        item = wc.latest_article(b.weread_book_id)
        new = 0
        if item and item["url"]:
            resolver = (lambda _title, _rid=item["review_id"]: wc.mp_content(_rid))
            new = len(_insert_new_articles(session, user_id, b, [item], source="sync",
                                           content_resolver=resolver, require_pan=False))
            b.last_item_at = datetime.now()
        _record_run(session, user_id, "wechat_sync", "partial",
                    f"weread_latest_only account={b.nickname} new={new}")
        session.commit()
        return {"platform": "wechat_sync", "status": "partial", "reason": "weread_latest_only",
                "pages": 1 if item else 0, "new": new, "ghid": b.ghid, "nickname": b.nickname}
    client = client or DajialaClient(settings.dajiala_key)
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
    status = "partial" if added and pages >= limit else "success"
    _record_run(session, user_id, "wechat_sync", status if added or pages else "success",
                f"pages={pages} new={len(added)} account={b.nickname}")
    session.commit()
    return {"platform": "wechat_sync", "status": status, "pages": pages, "new": len(added),
            "ghid": b.ghid, "nickname": b.nickname}


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
    session.commit()
    return bool(sent)


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
    if not settings.dajiala_key:
        return {"platform": "wechat_traffic", "status": "skipped", "reason": "no_key"}
    client = client or DajialaClient(settings.dajiala_key)
    limit = max(1, int(limit or settings.wechat_traffic_sample_limit))
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


def traffic_tick(settings: Settings | None = None) -> int:
    """每日定时:给所有(有对标号的)用户采样一轮阅读量。返回采样总篇数。"""
    from app.db import get_session_local
    from app.db.models import User
    from sqlalchemy import func as sa_func

    settings = settings or get_settings()
    if not settings.dajiala_key:
        return 0
    db = get_session_local()()
    total = 0
    try:
        users = db.scalars(select(User.id).order_by(User.id)).all()
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
    elements: list[dict] = [
        {"tag": "note", "elements": [{"tag": "plain_text",
            "content": "手机微信读书搜索关注该号 → 监听页「从微信读书书架导入」即自动进监听"}]},
        _col_set_row([("**公众号**", 3), ("**代表文章**", 7), ("**来源词**", 2)], grey=True),
    ]
    for r in rows[:20]:
        title = _md_safe(r.title)
        ts = r.title_ts.strftime("%m-%d") if r.title_ts else ""
        shown = title[:30] + ("…" if len(title) > 30 else "")
        elements.append(_col_set_row([
            (_md_safe(r.name)[:12] or "—", 3),
            (f"《{shown}》{f' ({ts})' if ts else ''}", 7),
            (_md_safe(r.term)[:8] or "—", 2),
        ]))
    if len(rows) > 20:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text",
            "content": f"…另有 {len(rows) - 20} 个,见平台候选列表"}]})
    try:
        FeishuClient(wh, settings.feishu_secret).send_card({
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text",
                "content": f"🔍 候选对标号 · 新发现 {len(rows)} 个"}},
            "elements": elements,
        })
    except Exception:  # noqa: BLE001 - 推送失败不影响采集结果
        logger.exception("候选对标号飞书推送失败 user=%s", user_id)


def candidate_discover_tick(settings: Settings | None = None) -> int:
    """每日定时:为所有(有对标号的)用户发现一轮同类候选号。返回新增候选数。"""
    from app.db import get_session_local
    from app.db.models import User
    from sqlalchemy import func as sa_func

    settings = settings or get_settings()
    db = get_session_local()()
    total = 0
    try:
        users = db.scalars(select(User.id).order_by(User.id)).all()
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
