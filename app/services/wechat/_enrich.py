"""新文后处理:盘链回填、夸克/百度转存换我方链(推送前)。"""

from app.db.models import (FeishuAlert, WechatArticle, WechatBenchmark, WechatCandidate,
                           WechatPanLink)

from app.db.tx import savepoint

from app.services.content_extract import extract_account_refs as _ear

from app.services.quark_transfer import QuarkAuthError, QuarkError, QuarkTransfer

from config.settings import Settings

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update

from sqlalchemy.orm import Session

import re

from app.services.wechat._text import _extract_pan_urls, assess_quality, detect_pan_types
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
def _commit_now(session: Session, why: str) -> None:
    """★ **外部副作用一旦发生,对应的记录必须当场落盘**(2026-10-06 实测逼出来的)。

    `transfer_and_share` 不是"准备一个待写的值",它**真的动了外部世界**:
    文件已经进了我们的网盘、分享链已经建好、额度/空间已经花掉。而这一段跑在
    监听轮的**大事务**里(收尾才 commit)⇒ 只要那一轮**卡住/进程被重启**,
    未提交的事务就整段消失,变成:

      · 库里 `my_pan_urls` 是空的 → 卡片只能显示"⏳待转存"(用户看到的就是这个);
      · 下一轮又把**同一批资源重转一遍** → 多建一份分享、多占一份空间。

    **实测证据(2026-10-06)**:`14:01:49` 四条「盘链复用(免重复转存)」成功落日志,
    之后那一轮**再没有产出任何日志**(同时段调度器其他作业一切正常),
    `14:29:31` 服务重启 ⇒ 库里那几行的 `my_pan_urls` **全是空的**。
    也就是说:**盘上的转存是真花了,记录却没了。**

    ⚠️ 在 `savepoint` 块里调用它是**安全的、且是既有模式** ——
    `app/db/tx.py::_quiet` 的注释写明:"段内可能已发生外层 commit … 静默跳过即可,数据已经落库"。
    """
    try:
        session.commit()
    except Exception:  # noqa: BLE001 - 落盘失败要说出来,但不能让整轮崩掉
        # 落不下盘 = 这份转存结果白花了(下一轮会重转)。**必须显式喊一声**,
        # 不能让"转存成功"的日志成为这件事的最后一句话。
        logger.exception("转存结果落盘失败(%s)—— 这一份会丢,下一轮会重转", why)


def _insert_new_articles(session: Session, user_id: int, benchmark: WechatBenchmark,
                         items: list[dict], source: str, fetch_content: bool = False,
                         content_resolver=None, require_pan: bool = True,
                         min_ts: int | None = None) -> list[WechatArticle]:
    """按链接去重入库;网盘类型=标题 + (可选)自抓正文 的并集。

    ⚠️ **已有行也要补阅读数**(2026-10-05 修,审计抓到的"阅读数恒 0 第二根因")。
    此前这里是 `if url_norm in existing: continue` —— **已有行直接跳过**,
    而 `read_num` 只在**插入时**写。后果两条,都很要命:

      ① **最新一篇永远没有阅读数**:`_weread_collect` 把 cover 放在 `items` **最前**,
         而 cover **不带 read_num**;几秒后同一个号的文章列表才给出精确值,
         但它已经在 `existing` 里了 ⇒ 被当重复丢掉。实测每号每轮正好少 1 篇。
      ② **历史积压永远补不上**:断供期入库的那批(如 2026-09-29~10-05 的 162 篇)
         全是 `read_num=0`,之后列表再给出精确值也没人回填。

    所以改成:遇到已有行时,若**新值 > 0 而旧值是 0** ⇒ **回填**。
    只在"旧值缺、新值有"时写,**绝不覆盖已有的非零值**(那是拿一次采样去覆盖另一次,
    没有依据),也**不写 0**(0 是"不知道",不是"没有")。
    """
    # url → (库里的 id, 已有的 read_num, 本轮**还没 flush** 的对象或 None)
    # ⚠️ 第三个字段是必须的:同一轮里刚 `session.add` 的行**还没有 id**(flush 在函数末尾),
    #    那时回填只能**直接改对象**;我第一版只按 id 回填,UPDATE 打在 id=0 上、
    #    什么都没改 —— 而"cover 在前、列表在后"恰恰全发生在同一轮内,
    #    所以那个 bug 会让这条修复在最常见的场景下完全失效(是测试当场抓出来的)。
    existing: dict[str, tuple[int, int, WechatArticle | None]] = {}
    backfilled = 0
    # ⚠️⚠️ **新鲜度闸(2026-10-06 补)** —— 用户口径「**2026 年 10 月份之前的不要再保存进来了**」
    # 是**对所有内容源**说的,但此前只落在抖音线索上,**公众号这条链一处都没有**:
    # 审计实测 10-01 之后仍有 **34 篇 9 月的文章**入库。判定逻辑收在 `app.services.freshness`
    # (单一事实源)—— 免得这条规则再被"修一条、漏一条"。
    # `min_ts=None` 时**自己从配置取**:四个调用点一个都不用改,规则就不会漏挂。
    from app.services import freshness

    if min_ts is None:
        min_ts = freshness.min_publish_ts()
    stale = undated = 0
    for rid, u, rn in session.execute(
            select(WechatArticle.id, WechatArticle.url, WechatArticle.read_num)
            .where(WechatArticle.user_id == user_id, WechatArticle.url != "")).all():
        existing[u.rstrip("/").strip()] = (int(rid), int(rn or 0), None)
    added: list[WechatArticle] = []
    for it in items:
        url = it["url"]
        title = (it.get("title") or "").strip()
        url_norm = url.rstrip("/").strip()
        # 读数要在**去重之前**算出来 —— 已有行也要用它回填(见上)
        preset_read = int(it.get("read_num") or 0)
        preset_like = int(it.get("like_num") or 0)
        if url_norm in existing:
            rid, old_read, obj = existing[url_norm]
            if preset_read > 0 and old_read == 0:
                if obj is not None:               # 本轮新插入、还没 id → 直接改对象
                    obj.read_num = preset_read
                    if preset_like:
                        obj.zan_num = preset_like
                elif rid > 0:
                    session.execute(update(WechatArticle).where(WechatArticle.id == rid)
                                    .values(read_num=preset_read,
                                            zan_num=preset_like or WechatArticle.zan_num))
                existing[url_norm] = (rid, preset_read, obj)
                backfilled += 1
            continue
        if not title:
            continue  # 空标题无价值(无法展示/分析/搜索)
        # ★ **10 月之前的不要**(闸放在抓正文**之前** —— 否则白跑一次网络才发现该丢)
        if min_ts:
            if freshness.is_too_old(it.get("publish_at"), min_ts):
                stale += 1
                continue
            if freshness.parsed_publish_ts(it.get("publish_at")) is None:
                # ⚠️ **不挡,但必须计数** —— 实测这类占三成,若只是"不挡",
                # 用户的规则就在这儿**静默漏掉三成**,而日志里一点痕迹都没有。
                undated += 1
        types = detect_pan_types(title)
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
        # 记进 existing:本轮内后续若出现**同一个 URL 但有精确读数**的条目(cover 先、列表后),
        # 就能直接改这个对象(它此时还没有 id,见上方注释)
        existing[url_norm] = (0, preset_read, row)
    if backfilled:
        # ⚠️ **要说出来**:回填是"补历史",量大的时候应该看得见 ——
        # 静默回填 162 条与"本来就有"在日志里长得一样(本仓的老毛病)。
        logger.info("回填阅读数 %s 条(benchmark=%s)", backfilled, benchmark.nickname)
    if min_ts and (stale or undated):
        # ⚠️⚠️ **这道闸的"覆盖了多少"本身就是要看的数**:`undated` 那批**没被挡**,
        # 它们占实测三成 —— 只报"挡了 N 篇"会让人以为规则全生效了。
        # (计数口径:去重之后、有标题的条目;被去重/空标题丢掉的本来也不入库。)
        logger.info("新鲜度闸(benchmark=%s):挡下 %s 篇早于截止的;另有 %s 篇**判不出发布时间**",
                    benchmark.nickname, stale, undated)
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


def _relink_notify(session, user_id: int, settings, r, kind: str,
                   link: str, code: str) -> bool:
    """已推送过的文章,后续转存成功 → 补一条轻量"🔗链接已转存"消息(2026-09-30)。

    场景:文章带着盘链入库但转存失败/未配 Cookie → 推的是原文(⏳待转存);
    之后 Cookie 配好/转存恢复 → my_pan_urls 有了我方链,但**没有任何机制再告诉员工**。
    这里补一条轻量消息(标题+我方链+提取码+该文全部我方链),不重发整卡防刷屏。
    冷却:每篇×盘种一次(7 天,feishu_alert_gate)。返回是否实际发送。
    """
    if not getattr(r, "pushed_at", None):
        return False  # 还没推送过的新文,本轮整卡自然带我方链,无需补推
    from app.services.alert_service import feishu_alert_gate
    from app.services.feishu_client import FeishuClient, webhook_for

    webhook = webhook_for(settings, "wechat")
    if not webhook:
        return False
    if not feishu_alert_gate(session, user_id, "relink", f"{r.id}:{kind}", 24 * 7, "转存补链"):
        return False  # 这篇×这个盘种 7 天内已补过
    mine = [x.strip() for x in (r.my_pan_urls or "").splitlines() if x.strip()]
    lines = [f"🔗 链接已转存·补链({kind})",
             "🔴 " + (r.title or "")[:40],
             "🟠 " + link + (f"(提取码 {code})" if code else "")]
    if mine:
        lines.append("📦 该文全部我方链接:")
        lines.extend("  " + x for x in mine[:5])
    return FeishuClient(webhook, settings.feishu_secret).send("\n".join(lines))


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
    # ⚠️⚠️ **补转存队列必须算在"有没有夸克 Cookie"之外**(2026-10-06 修)。
    # 它同时喂 **夸克 / 百度 / 迅雷** 三段,而队列原来只在 `if quark_ck:` 里算 ⇒
    # "没配夸克、但配了百度"时三段全拿不到 `transfer_rows`(实测直接 `UnboundLocalError`,
    # 而且那等于**整条兜底不存在**:配了百度也永远不会补链)。
    # 门控改成 `pan_transfer_enabled` —— 任一盘可用就该补,别绑在某一个盘上。
    #
    # 入队条件 = **认得出且转得动的盘**(夸克/百度/迅雷):
    # 只带 UC 的历史行既转不动也不落 `my_pan_urls`,放进来等于**永久霸占队头**、
    # 把每轮 8 个名额吃光,真正待转的再也轮不到(2026-09-26 第八轮审计 High)。
    # 排序按 id 升序=队头优先,所以"永久失败"的必须当场出队(见各段里的出队逻辑)。
    backfill: list = []
    transfer_rows = list(rows)
    if settings.pan_transfer_enabled and run_backfill:
        try:
            backfill = session.scalars(select(WechatArticle).where(
                WechatArticle.user_id == user_id, WechatArticle.pan_urls != "",
                # ⚠️ **队列要收"能转的盘",不是"只有夸克"**(2026-10-06 扩到**百度 + 迅雷**)。
                # 原来写死 `like("%pan.quark.cn%")` ⇒ **只带百度/迅雷链的文章永远进不了队列**,
                # 于是它们从入库那轮起就永久停在"⏳待转存"—— 用户口径:「百度链和迅雷也需要
                # 设置上补链」。这三条转存路径**本来就有**(下面各一段),只是队列不收它们。
                or_(WechatArticle.pan_urls.like("%pan.quark.cn%"),
                    WechatArticle.pan_urls.like("%pan.baidu.com/s/%"),
                    WechatArticle.pan_urls.like("%pan.xunlei.com%")),
                or_(WechatArticle.my_pan_urls.is_(None), WechatArticle.my_pan_urls == "")
            ).order_by(WechatArticle.id).limit(
                max(1, int(getattr(settings, "pan_transfer_backfill_limit", 8) or 8)))).all()
        except Exception:  # noqa: BLE001 - 兜底失败不影响本轮新文
            backfill = []
        # 按对象身份(而非 `x not in rows`)排除重复:SQLAlchemy 实体的 == 会生成 SQL 表达式
        # 而不是布尔值,放进 in 的判断里语义含混。
        row_ids = {id(x) for x in rows}
        transfer_rows = list(rows) + [x for x in backfill if id(x) not in row_ids]
    if settings.pan_transfer_enabled and quark_ck:
        quark = QuarkTransfer(quark_ck, fid_store=settings.quark_fid_store)
        # 盘链级转存去重:同一资源(相同夸克分享链)只转存一次,后续文章复用首篇的
        # 我方分享链——多个对标号发同一资源时,旧逻辑每篇各存一份(浪费空间+成倍风控暴露)。
        reused: dict[str, tuple[str, str]] = {}  # pan_url -> (my_share_url, 提取码)
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
                                _commit_now(session, "夸克自分享收录")
                                logger.info("盘链为自己分享,直接采用: %s", u[:60])
                                continue
                            # 41031=对方分享被封(源永久失效,重试不可能成功)→ 落一条标记
                            # 让它离开补转存队列;推送仍回落原文,网盘列显示"源失效"。
                            # 其余为真失败(网络/风控),保留空 my_pan_urls 下轮重试。
                            if "41031" in str(exc):
                                mine = [x for x in (r.my_pan_urls or "").splitlines() if x.strip()]
                                mine.append("⚠️源分享已被封(41031),未转存")
                                r.my_pan_urls = chr(10).join(mine)[:2000]
                                # ⚠️ **出队标记也要当场落盘**:它是"这条永久失败、别再占队头"的凭据。
                                # 丢了它 ⇒ 死链每轮都排在队头、吃光 `pan_transfer_backfill_limit`
                                # 个名额,真正待转的永远轮不到(而日志里看不出任何异常)。
                                _commit_now(session, "夸克源失效出队")
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
                # ⚠️ **转存是外部副作用,结果当场落盘**(见 `_commit_now` 的实测证据):
                # 这一轮后面无论卡住还是被重启,这份已经花掉的转存都不能丢记录。
                _commit_now(session, "夸克转存")
                # 已推送过的历史行(backfill 队列救回的):补一条轻量链接消息,员工拿得到我方链
                try:
                    _relink_notify(session, user_id, settings, r, "夸克", share_url, pwd)
                except Exception:  # noqa: BLE001 - 补链失败不影响转存结果
                    logger.debug("夸克补链消息失败", exc_info=True)
            if dead:
                break
    # 百度网盘链接: 同样转存+换链(协议与夸克并列;失败回落原文推送)。
    # 独立门控:只看 pan_transfer_enabled + 用户是否配了百度 Cookie,
    # 不再借用 quark_cookie——此前复制粘贴导致"只配百度未配夸克"时百度链永不转存。
    # 2026-09-26 补提醒:缺 Cookie / Cookie 失效此前都只有一行 info 日志(或静默 break),
    # 运营者看到的是永久的"—",与夸克那套"点名告警"不对等。
    if settings.pan_transfer_enabled and any(
            "pan.baidu.com/s/" in (r.pan_urls or "") for r in transfer_rows):
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
            for r in transfer_rows:
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
                        # ⚠️ 百度这边同理:**转存已经真花了,记录不能挂在轮末那次 commit 上**。
                        _commit_now(session, "百度转存")
                        # 已推送过的历史行:补一条轻量链接消息(百度转存常晚于推送)
                        try:
                            _relink_notify(session, user_id, settings, r, "百度", share_url_b, code_b)
                        except Exception:  # noqa: BLE001 - 补链失败不影响转存结果
                            logger.debug("百度补链消息失败", exc_info=True)
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
    # 迅雷云盘链接:走**统一入口** `pan_discovery.transfer_pan_url`(三盘分发,含 captcha 自愈)。
    # ⚠️ **为什么用统一入口而不是照抄百度那段**(2026-10-06):迅雷的转存带 captcha 续期、
    # 失败语义也特殊(未配 Cookie 要标 `pending` 而不是终态),那套都封在 `xunlei_transfer`
    # 与统一入口里了 —— 这里再手写一遍就是**第二个真相源**(本仓吃过这个亏)。
    # 用户口径:「百度链和迅雷也需要设置上补链」。
    if settings.pan_transfer_enabled and any(
            "pan.xunlei.com" in (r.pan_urls or "") for r in transfer_rows):
        from app.services.pan_discovery import transfer_pan_url

        for r in transfer_rows:
            urls = [x.strip() for x in (r.pan_urls or "").splitlines()
                    if "pan.xunlei.com" in x][:2]
            if not urls:
                continue
            got = ""
            for u in urls:
                # ① 历史复用:库里已有别人转好的**同一条链**,直接用,别重复搬一份
                hist = session.execute(
                    select(WechatArticle.my_pan_urls).join(
                        WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
                    .where(WechatPanLink.pan_url == u,
                           WechatArticle.user_id == user_id,   # 只复用本租户自己转的
                           WechatArticle.my_pan_urls.like("%pan.xunlei.com%"),
                           WechatArticle.id != r.id).limit(1)).scalar()
                picked = next((x.strip() for x in (hist or "").splitlines()
                               if "pan.xunlei.com" in x), "")
                if picked:
                    got = picked.split(" (提取码")[0].strip()
                    break
                # ② 首次见到该资源:转存(统一入口**不抛**,失败也是结构化返回)
                # ⚠️ **网络活之前先放掉写锁**(与夸克/百度同一条纪律,由
                # `tests/test_slowwork_guard.py` 守着):迅雷转存要走 captcha 续期,
                # **可能几十秒**,而 SQLite 是单写者、别的作业 `busy_timeout` 只有 30 秒。
                session.commit()
                res = transfer_pan_url(session, user_id, u, settings,
                                       snippet=f"{r.title or ''} {(r.content or '')[:200]}")
                if res.get("status") == "ok" and res.get("our_url"):
                    got = str(res["our_url"])
                    break
                logger.info("迅雷链转存未成 %s(%s):%s", u[:50], res.get("status"),
                            str(res.get("message"))[:70])
            if not got:
                continue
            mine = [x for x in (r.my_pan_urls or "").splitlines() if x.strip()]
            mine.append(f"{got} [迅雷]")
            r.my_pan_urls = chr(10).join(mine)[:2000]
            replacements.setdefault(r.id, []).append((urls[0], got, ""))
            _commit_now(session, "迅雷转存")     # 外部副作用已发生 ⇒ 当场落盘
            try:
                _relink_notify(session, user_id, settings, r, "迅雷", got, "")
            except Exception:  # noqa: BLE001 - 补链失败不影响转存结果
                logger.debug("迅雷补链消息失败", exc_info=True)
    # ⚠️⚠️ **转存结果必须在这里落库,别再往下拖**(2026-10-05 生产事故)。
    #
    # 下面那段(资源共振告警 / 交叉提取)是**锦上添花**,而外层 `_listen_round` 把整个
    # `_enrich_new_articles` 关在**一个 savepoint** 里 ⇒ **它一旦抛异常,上面刚做完的
    # 全部转存与复用会被一起回滚**。
    #
    # **实测就是这么丢的**(2026-10-05 14:02:04):共振告警的冷却门 `feishu_alert_gate`
    # 里 `with savepoint(db)` 建保存点时撞上 SQLite **`database is locked`**
    # (本地 SQLite 并发写抢占,`busy_timeout=30s` 都没等到)→ 异常上抛 →
    # 整段 enrich 回滚 → 那一轮 10 篇的转存/复用**全部白干**。
    # 而外面看到的是:**轮次状态 `success`、日志里"盘链复用(免重复转存)"一条不少、
    # 卡片照常推送**,只是网盘列变成"⏳待转存" —— **典型的"看起来成功实则失败"**。
    #
    # 落库之后,即使下面再炸:`replacements` 虽然会丢(调用方置 `{}`),
    # 但推送渲染会**回落到 `r.my_pan_urls`**(已有值)⇒ 卡片照样显示 🔴 我方链。
    session.commit()
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
