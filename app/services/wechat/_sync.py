"""一键同步:免费列表翻页 + 微信读书兜底,同盘链去重推卡。"""

from app.db.models import (WechatArticle, WechatBenchmark, WechatPanLink)

from app.services.reader_platform_client import PlatformError, ReaderPlatformClient

from app.services.tenant_base import _base, _record_run

from app.services.weread_client import WereadAuthError, WereadClient, WereadError, build_mp_url

from app.services.werss_client import WerssClient

from config.settings import Settings

from datetime import datetime, timedelta

from sqlalchemy import select, update

from sqlalchemy.orm import Session

from app.services.wechat._text import _parse_time
from app.services.wechat._source import feed_biz
from app.services.wechat._enrich import _insert_new_articles
from app.services.wechat._listen import _push_listen


from app.utils import get_logger

logger = get_logger(__name__)
from app.services import wechat_monitor as _root  # 兼容 monkeypatch:可替换名经门面运行时查找

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
    dropped: list[int] = []
    for r in sorted(rows, key=lambda x: x.id):  # 入库顺序即新→旧,保留首见者
        p = first_pan.get(r.id, "")
        if p and (p in already or p in seen):
            dropped.append(r.id)
            continue
        if p:
            seen.add(p)
        kept.append(r)
    # 被有意跳过的重复资源也要盖 pushed_at:否则 `repush_unpushed` 下一轮把它们当成
    # "从没推过"重新发卡,把"同链只推一篇"的铁律反过来破坏了。
    if dropped:
        session.execute(update(WechatArticle).where(WechatArticle.id.in_(dropped))
                        .values(pushed_at=datetime.now()))
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
        replacements = _root._enrich_new_articles(session, user_id, settings, to_push,
                                            run_backfill=False)
        session.commit()
    except Exception:  # noqa: BLE001 - 转存炸了也要推(标题回落原文),不能让同步整个报错
        logger.exception("同步后转存失败 user=%s", user_id)
        session.rollback()
        replacements = {}
    _push_listen(session, user_id, settings, to_push, replacements)
    return {"pushed": len(to_push), "transferred": len(replacements),
            "deduped": len(new_rows) - len(kept),
            # 截断数=历史文里没进窗口的那些。直接 `len(history)-extra` 在
            # 本轮入库不足封顶数时会算出**负数**(extra 可大于 history 长度),
            # 前端 toast 就成了"剩余 -17 篇"。
            "truncated": len(history) - len(history[:extra])}
def sync_wechat_account(session: Session, user_id: int, benchmark_id: int,
                        settings: Settings | None = None,
                        max_pages: int | None = None, weread: WereadClient | None = None,
                        platform: ReaderPlatformClient | WerssClient | None = None) -> dict:
    """一键同步:免费列表(WeRSS/读书平台)翻页拉历史文章入库,微信读书兜底。

    入库后统一走 `_sync_push_after_transfer`:同盘链去重 → 夸克转存换成我方链 → 再推飞书,
    所以卡片里点文章名直接进"我的夸克链",不会重复保存/重复推送。
    """
    settings = _base(settings)
    b = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.id == benchmark_id))
    if b is None:
        raise KeyError("对标账号不存在")
    plat = platform or _root._platform_client(settings, session=session, user_id=user_id)
    feed_id = feed_biz(b)
    # 服务端钳制:query 页数无上限会被恶意调用当免费 API 刷(拖死同步 worker)
    limit = max(1, min(int(max_pages or (10 if plat and feed_id else settings.wechat_sync_max_pages)), 20))
    if plat and feed_id:
        new_rows: list[WechatArticle] = []
        pages = 0
        try:
            while pages < limit:
                raw_items = plat.mp_articles(feed_id, page=pages + 1, limit=20)
                if pages == 0 and not raw_items:
                    # 首页就空 = 这个订阅在源里不存在/没抓到,不能算同步成功(否则下面直接 return,
                    # 微信读书兜底路也不会走,用户点「同步文章」永远 0 篇还显示成功)
                    logger.warning("免费列表 %s 首页为空(biz=%s),同步交由后续源", b.nickname, feed_id)
                    break
                norm = [{"title": it["title"], "url": it["url"],
                         "publish_at": _parse_time(it.get("publish_at_raw"))} for it in raw_items]
                got = _insert_new_articles(session, user_id, b, norm, source="sync", require_pan=False)
                new_rows.extend(got)
                pages += 1
                if not raw_items or len(got) < len(raw_items):
                    break  # 本页为空或全部已入库 → 更旧的页也必然已见
        except PlatformError as exc:
            logger.warning("读书平台同步失败,转微信读书兜底:%s", exc)
        if pages:
            b.last_item_at = datetime.now()
            push = _sync_push_after_transfer(session, user_id, settings, new_rows)
            # 翻满 limit 页且最后一页还是"整页新文" = 历史被页数上限截断,
            # 和微信读书路一样不能记 success:用户以为"这个号就这些文章",实际是没翻完
            status = "partial" if pages >= limit else "success"
            _record_run(session, user_id, "wechat_sync", status,
                        f"platform account={b.nickname} pages={pages} new={len(new_rows)} "
                        f"pushed={push['pushed']} deduped={push['deduped']} truncated={push['truncated']}"
                        + (" history_limit_hit" if status == "partial" else ""))
            session.commit()
            return {"platform": "wechat_sync", "status": status, "pages": pages,
                    "new": len(new_rows), "ghid": b.ghid, "nickname": b.nickname, **push}
    # 微信读书兜底(2026-09-29 dajiala 摘除后为唯一兜底):cover 只给"最新一篇",
    # mp/articles 列表可用时才谈得上补全同日漏掉的篇
    cookie = _root._weread_cookie(session, user_id, settings)
    if not cookie or not b.weread_book_id:
        return {"platform": "wechat_sync", "status": "skipped",
                "reason": "no_source(无微信读书 Cookie 且无免费列表源)"}

    if True:  # noqa: SIM108 - 原付费分支已摘除;保留块结构使下方缩进/补丁最小化
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
            except WereadAuthError:
                raise  # Cookie 失效必须往上抛(上层据此续期);吞成 error 会让 80 个号白撞
            except Exception as exc:  # noqa: BLE001 - 列表是"锦上添花",任何形状问题都不该打断同步
                # -2041 = 本会话列表预算耗尽(可预期,别当故障);其余按异常归类
                listed = "limited" if "-2041" in str(exc) else "error"
                logger.warning("微信读书近期列表不可用(%s),%s 退到最新一篇", str(exc)[:60], b.nickname)
            cover = None
            try:
                cover = client.latest_article(b.weread_book_id)
            except WereadAuthError:
                raise  # 登录态刚死:必须让上层续期重试,不能当成"这次没 cover"继续
            except WereadError as exc:
                if listed != "ok":
                    raise          # 两条路都没有 → 交给上层(监听/路由决定降级或续期)
                logger.warning("cover 也失败(%s),本次只同步列表内容", str(exc)[:60])
            return out, cover, listed

        wc = weread or _root.WereadClient(cookie)
        try:
            items, cover, listed = _fetch(wc)
        except WereadAuthError:
            # wr_skey 十几小时必过期,而用户点「同步文章」走的正是这条路。
            # wr_rt 还活着时先像监听那样自救续期一次再重试;续期不成才把异常抛给上层
            # (路由翻成 502 可执行文案,此前这里是裸 500 → 前端只报"服务器开小差了")。
            # 续期会换出新会话——正是 mp/articles 可用的窗口,列表这次能补回同日漏掉的篇。
            refreshed = _root.refresh_weread_cookie(session, user_id, settings)
            if refreshed.get("status") != "success":
                raise
            wc = _root.WereadClient(refreshed["cookie"])
            items, cover, listed = _fetch(wc)
        if cover and cover["url"] and not any(it["url"] == cover["url"] for it in items):
            items = [cover] + items       # 列表里没这篇(刚发/翻页边界)也要带上
        # cover 的正文用它的 reviewId 走转发页;列表项各自的 reviewId 由 content_resolver 复用。
        # 空 reviewId 不能占键:`{it["url"]: it.get("review_id") or ""}` 会让 cover 那条
        # 有效的 reviewId 被 setdefault 跳过(同篇既在列表里又缺 reviewId 时正文直接为空)。
        rid_of = {it["url"]: it["review_id"] for it in items if it.get("review_id")}
        if cover and cover.get("url") and cover.get("review_id"):
            rid_of.setdefault(cover["url"], cover["review_id"])

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
