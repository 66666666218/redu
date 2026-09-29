"""新文后处理:盘链回填、夸克/百度转存换我方链(推送前)。"""

from app.db.models import (FeishuAlert, User, WechatArticle, WechatBenchmark, WechatCandidate,
                           WechatPanLink, WechatRewrite, WechatTrafficSample)

from app.db.tx import HeldSavepoint, savepoint

from app.services.content_extract import extract_account_refs as _ear

from app.services.quark_transfer import QuarkAuthError, QuarkError, QuarkTransfer, extract_quark_urls

from config.settings import Settings, get_settings

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update

from sqlalchemy.orm import Session

import re

from app.services.wechat._text import _extract_pan_urls, assess_quality, detect_pan_types, fetch_article_content, title_hits
from app.services.wechat._source import _quark_cookie
from app.services.wechat._candidates import _push_candidates


from app.utils import get_logger

logger = get_logger(__name__)
from app.services import wechat_monitor as _root  # 兼容 monkeypatch:可替换名经门面运行时查找

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
        if _root.title_hits(title):
            if content_resolver:  # 免费源注入(微信读书正文);传 url 让实现方能对上本篇 reviewId
                content = content_resolver(it["title"], url) or ""
            elif fetch_content:
                content = _root.fetch_article_content(url)
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
                         run_backfill: bool = True) -> dict[int, list[tuple[str, str, str]]]:
    """新文后处理(推送前):夸克转存盘链 → 换自己的分享链并持久化到 `my_pan_urls`。

    (2026-09-29 摘除 dajiala 后,原"① 即时采样阅读量"随付费链一并移除;阅读数相关
    字段保留在库表里兼容历史数据,新数据一律无采样。)
    `run_backfill=False`(同步收尾)跳过历史补转存队列——同步在 HTTP 请求里,自带窗口已经
    限死了本次转存篇数,再叠加队列会让一次点击多打 N 次夸克接口;队列留给定点监听。
    返回 {article_id: [(原链, 我的链, 提取码)]} 供飞书推送;失败回落原链接,绝不阻塞监听。
    """
    replacements: dict[int, list[tuple[str, str, str]]] = {}
    try:  # 先修"正文有链却抽不到"的历史行,让它们进得了下面的补转存队列
        # 回填不能挂在"本轮采到了新文"上:一轮没新文就整段早退,存量死账(本机库实测 27 篇
        # 正文里明明写着百度链、pan_urls 却空着)永远等不到修,飞书卡片上就永远是一根"—"。
        with savepoint(session):  # 失败只撤销这段回填,不能连累本轮已采的新文
            _backfill_pan_urls(session, user_id)
    except Exception:  # noqa: BLE001 - 回填是锦上添花,不能拖垮本轮监听
        logger.exception("盘链列回填失败 user=%s", user_id)
    if not rows:
        # 空轮回填完就走:补转存队列要打的外呼接口留给有新文的轮次,免得长期没新文的号
        # 也把每轮的 pan_transfer_backfill_limit 个名额平白用掉。
        return replacements
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
        # 入队还要再加一条"含夸克链":队列里只有夸克转存会消费(百度走自己的门控),
        # 只带百度/UC/迅雷链的历史行既转不动也不落 my_pan_urls,等于**永久霸占队头**,
        # 把每轮 8 个名额吃光,真正待转的夸克文再也轮不到(2026-09-26 第八轮审计 High)。
        try:
            backfill = [] if not run_backfill else session.scalars(select(WechatArticle).where(
                WechatArticle.user_id == user_id, WechatArticle.pan_urls != "",
                WechatArticle.pan_urls.like("%pan.quark.cn%"),
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
            # 只把夸克链交给夸克:UC/迅雷/百度链走识别与各自的转存门控,
            # 送进 _parse_share 只会得到"无法解析夸克分享链接"的假失败日志。
            for u in [x.strip() for x in (r.pan_urls or "").splitlines()
                      if "pan.quark.cn" in x][:3]:
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
                                # 标记与链接之间必须留空格:回落解析按"链接本体"取串,
                                # 粘着写会把 `(自分享)` 一起当成 URL 的一部分,链接点开即坏。
                                mine.append(u + " (自分享)")
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
