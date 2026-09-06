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
- 同步 `sync_wechat_account`:dajiala `history_by_ghid` 翻页(`PagingInfo.Offset`/`IsEnd`)
  拉历史文章入库,默认 `wechat_sync_max_pages` 页封顶(每页 ¥0.14);仅有微信读书源时
  只能拿最新一篇(旧列表接口已被微信读书废弃),返回 `partial`。

设计原则"免费优先":微信读书正文与原文页自抓都免费,dajiala 仅作兜底与阅读量采样。
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import WechatArticle, WechatBenchmark, WechatTrafficSample
from app.services.dajiala_client import DajialaClient, DajialaError, DajialaNoBalance
from app.services.quark_transfer import QuarkAuthError, QuarkError, QuarkTransfer, extract_quark_urls
from app.services.reader_platform_client import PlatformError, ReaderPlatformClient
from app.services.tenant_base import _base, _record_run
from app.services.weread_client import WereadAuthError, WereadClient, WereadError
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


# ---------------------------------------------------------------- 监听
def _insert_new_articles(session: Session, user_id: int, benchmark: WechatBenchmark,
                         items: list[dict], source: str, fetch_content: bool = False,
                         content_resolver=None) -> list[WechatArticle]:
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
        content = ""
        if title_hits(it["title"]):
            if content_resolver:  # 免费源注入(微信读书正文)
                content = content_resolver(it["title"]) or ""
            elif fetch_content:
                content = fetch_article_content(url)
            if content:  # 自抓成功 → 用正文的链接判定覆盖标题的盘名猜测
                types = detect_pan_types(content) or types
        pan_urls = extract_quark_urls(f'{it["title"]} {content}')  # 目前仅夸克可自动转存
        row = WechatArticle(user_id=user_id, author=(benchmark.nickname or "未命名")[:128],
                            title=it["title"][:500], url=url[:500], content=content,
                            publish_at=it.get("publish_at"), source=source,
                            benchmark_id=benchmark.id, pan_types=",".join(types)[:128],
                            pan_urls=chr(10).join(pan_urls)[:2000])
        session.add(row)
        added.append(row)
    return added


def _enrich_new_articles(session: Session, user_id: int, settings: Settings,
                         rows: list[WechatArticle],
                         client: DajialaClient | None) -> dict[int, list[tuple[str, str, str]]]:
    """新文后处理(推送前):① 即时采样阅读量(¥0.06/篇,上限 wechat_listen_sample_limit);
    ② 夸克转存盘链 → 换自己的分享链并持久化到 `my_pan_urls`。

    返回 {article_id: [(原链, 我的链, 提取码)]} 供飞书推送;失败回落原链接,绝不阻塞监听。
    """
    replacements: dict[int, list[tuple[str, str, str]]] = {}
    if not rows:
        return replacements
    if settings.wechat_listen_sample_new and settings.dajiala_key:
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
    return replacements


def run_wechat_listen(session: Session, user_id: int, settings: Settings | None = None,
                      client: DajialaClient | None = None, weread: WereadClient | None = None,
                      platform: ReaderPlatformClient | None = None, push: bool = True) -> dict:
    """监听一轮:双数据源免费优先——微信读书(cover)→ dajiala(当天发文)→ 新文入库推飞书。"""
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

    # 余额保护前置:只要有账号需要走 dajiala(无 book_id 或会话失效),先查余额(免费接口)
    needs_dajiala = use_dajiala and any(
        not (cookie and b.weread_book_id) and b.anchor_url for b in rows)
    if use_dajiala:
        client = client or DajialaClient(settings.dajiala_key)
    if needs_dajiala:
        try:
            balance = client.remain_money()
            if balance < settings.dajiala_min_balance:
                logger.warning("公众号监听跳过:余额 %.2f 低于阈值 %.2f(用户 %s)", balance,
                               settings.dajiala_min_balance, user_id)
                _record_run(session, user_id, "wechat_listen", "skipped",
                            f"low_balance={balance:.2f}")
                session.commit()
                return {"platform": "wechat", "status": "skipped",
                        "reason": "low_balance", "balance": balance}
        except DajialaError as exc:
            _record_run(session, user_id, "wechat_listen", "failed", f"{type(exc).__name__}: {exc}")
            session.commit()
            raise

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
        # ① 微信读书(免费):对标号已关联 bookId 且有 Cookie
        if not used and cookie and b.weread_book_id:
            try:
                weread = weread or WereadClient(cookie)
                item = weread.latest_article(b.weread_book_id)
                used = True
                if item and item["url"]:
                    resolver = (lambda _title, _rid=item["review_id"]: weread.mp_content(_rid))
                    got = _insert_new_articles(session, user_id, b, [item], source="listen",
                                               content_resolver=resolver)
                    if got:
                        new_rows.extend(got)
                        b.miss_count = 0
                        b.last_item_at = now
            except WereadAuthError as exc:
                logger.warning("微信读书登录态失效(用户 %s):%s;后续号降级 dajiala", user_id, exc)
                cookie = ""  # 会话失效,本轮不再试微信读书
                failed += 1
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
    replacements = _enrich_new_articles(session, user_id, settings, new_rows, client)

    session.commit()

    status = "success" if not failed or new_rows else ("failed" if failed == len(rows) else "partial")
    _record_run(session, user_id, "wechat_listen", status,
                f"accounts={len(rows)} new={len(new_rows)} failed={failed}")
    session.commit()
    if push and new_rows:
        _push_listen(session, user_id, settings, new_rows, replacements)
    return {"platform": "wechat", "status": status, "accounts": len(rows),
            "new": len(new_rows), "failed": failed}


def _push_listen(session: Session, user_id: int, settings: Settings, rows: list[WechatArticle],
                 replacements: dict[int, list[tuple[str, str, str]]] | None = None) -> None:
    """新文推公众号专属飞书群(未配则回落总群);带即时阅读量与转存后的自己的盘链。"""
    from app.services.feishu import webhook_for
    from app.services.feishu_client import FeishuClient

    wh = webhook_for(settings, "wechat")
    if not wh:
        return
    replacements = replacements or {}
    lines = [f"📡 公众号监听 · 新发文 {len(rows)} 篇"]
    for r in rows[:20]:
        tag = f"🔴{r.pan_types}" if r.pan_types else ""
        lines.append(f"{tag} {r.title}")
        for _old_url, new_url, pwd in replacements.get(r.id, []):
            extra = f" (提取码 {pwd})" if pwd else ""
            lines.append(f"📦 我的夸克链接: {new_url}{extra}")
        if r.traffic_at:
            lines.append(f"📊 阅读 {r.read_num} · 点赞 {r.zan_num} · 在看 {r.looking_num}"
                         f" · 转发 {r.share_num} · 收藏 {r.collect_num} · 评论 {r.comment_count}")
        else:
            lines.append("📊 流量未采样")
    if len(rows) > 20:
        lines.append(f"…另有 {len(rows) - 20} 篇,见平台文章列表")
    try:
        FeishuClient(wh, settings.feishu_secret).send("\n".join(lines))
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
                got = _insert_new_articles(session, user_id, b, norm, source="sync")
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
                                           content_resolver=resolver))
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
            added.extend(_insert_new_articles(session, user_id, b, items, source="sync"))
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
    """把 read_zan_pro 结果写回文章 + 追加一个采样点。"""
    r.read_num = int(data.get("read") or 0)
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

    q = select(WechatArticle).where(
        WechatArticle.user_id == user_id,
        WechatArticle.url != "",
        or_(WechatArticle.traffic_at.is_(None),
            WechatArticle.traffic_at < datetime.fromtimestamp(cutoff)))
    if benchmark_id:
        q = q.where(WechatArticle.benchmark_id == benchmark_id)
    rows = session.scalars(q.order_by(WechatArticle.created_at.desc()).limit(limit * 3)).all()
    # 排序:没采过优先,其次最近采样更久优先,再按发现时间新→旧;截到 limit
    rows = sorted(rows, key=lambda r: (r.traffic_at is not None,
                                       r.traffic_at or datetime(1970, 1, 1),
                                       -r.created_at.timestamp()))[:limit]
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
        _apply_sample(session, user_id, r, data, now)
        sampled += 1
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
