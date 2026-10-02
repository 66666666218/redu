"""独立定时作业:网盘 Cookie 保活、全量补采、关键词文章监听。"""

from app.db.models import (FeishuAlert, User, WechatBenchmark)

from app.services.early_agent import _md_safe_light

from app.services.quark_transfer import QuarkAuthError, QuarkTransfer

from app.services.weread_client import WereadError

from config.settings import Settings, get_settings

from datetime import datetime, timedelta

from sqlalchemy import select

from sqlalchemy.orm import Session

import re

import time



from app.utils import get_logger

logger = get_logger(__name__)
from app.services import wechat_monitor as _root  # 兼容 monkeypatch:可替换名经门面运行时查找

def pan_cookie_keepalive_tick(settings: Settings | None = None) -> int:
    """每日定时巡检网盘转存凭据(夸克 + 百度网盘 + 迅雷),失效即时告警。返回健康的 (用户,平台) 数。

    旧版三处不对等:① `if not settings.quark_cookie: return 0` —— 只在「Cookie 管理」按用户配了
    凭据的人**根本没被巡检过**,于是"失效"只能等到监听里转存炸了才报;② 百度盘完全没有保活,
    而它的失败(errno)长得像"对方链接失效",没人会怀疑是自己 Cookie 死了;③ 告警挂到 id 最小的
    用户、板块写成 xianyu(发到闲鱼群)。夸克保活仍是轻量列目录(顺带滚动延长 __puus)。
    同一份 Cookie 只探一次、只告警一次:多用户共用全局凭据时不重复撞接口也不刷屏。

    **迅雷**(2026-10-02 补):它的凭据是扫码出来的、**约 12 小时就废**,废了之后
    **采集还能跑但转存/分享全停** —— 不喊一声没人知道,所以纳入同一巡检。
    """
    from app.services.alert_service import notify_incident
    from app.db import get_session_local
    from app.services.baidupan_transfer import BaiduPanAuthError, BaiduPanClient
    from app.services.cookie_store import get_cookie

    settings = settings or get_settings()
    if not settings.pan_transfer_enabled:
        return 0
    _NICK = {"quark": "夸克", "baidupan": "百度网盘", "xunlei": "迅雷网盘"}
    _FIX = {"quark": "请浏览器登录 pan.quark.cn 后 F12 复制 Cookie,更新到「Cookie 管理」页的 "
                      "quark 平台(或 .env 的 QUARK_COOKIE)",
            "baidupan": "请浏览器登录 pan.baidu.com 后复制含 BDUSS 的 Cookie,更新到"
                        "「Cookie 管理」页的 baidupan 平台(百度盘没有全局默认值,只能按用户配)",
            # 迅雷：凭据是**扫码**出来的(JSON,不是 Cookie 串),约 12 小时就废;
            # 废物后采集还能跑、**转存/分享全停** —— 正是最该有人喊一声的时候
            "xunlei": "迅雷凭据约 12 小时过期,请重新扫码:在项目根目录跑 "
                      "`python tools/xl_qr_login.py`(会弹浏览器,用迅雷 App 扫一下)"}

    def _probe(platform: str, cookie: str) -> str:
        """ok / auth(凭据已死,要人工换) / error(网络或风控,不该报"Cookie 失效")。"""
        try:
            if platform == "quark":
                QuarkTransfer(cookie).keepalive()
            elif platform == "xunlei":
                # 迅雷不用传 cookie 串:`verify()` 自己从 cookie_store 读那套 JSON,
                # 且**自带 captcha 自愈**;走到失败通常是 refresh_token 已废 → 只能重扫
                from app.services import xunlei_transfer as xt

                res = xt.verify()
                if not res.get("ok"):
                    return f"auth:{res.get('message', '')}"[:200]
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
            for platform in ("quark", "baidupan", "xunlei"):
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
                    # 迅雷不是 Cookie 是**扫码凭据**,且"监听"是公众号的说法 —— 文案按平台走
                    what = "凭据" if platform == "xunlei" else "Cookie"
                    note = ("采集不受影响,仅转存/分享暂停" if platform == "xunlei"
                            else "监听不受影响,仅转存暂停")
                    notify_incident(db, uid, "wechat",
                                    f"🟠 {_NICK[platform]} {what} 已失效,转存功能停用",
                                    f"{results[key][5:]}。{_FIX[platform]};{note}",
                                    settings=settings)
    finally:
        db.close()
    return healthy
def run_full_sync_if_pending(session: Session, user_id: int,
                             settings: Settings | None = None) -> dict:
    """若 renewal 刚换出可用 Cookie(会话初期),对全部对标号跑一轮全量补采。

    背景:cover 只返回"最新一篇",部分号的书架快照冻结在关注时点,此后新文全部
    看不见;mp/articles(历史列表)仅在 Cookie 会话初期可用——正好在 renewal 成功
    (必然伴随新会话)后的窗口里把停更文章一次性补齐。
    每号独立容错,单号失败不阻断;完成后清除标记。

    `SystemConfig.value` 兼作**续采游标**:首轮是打标时间戳,遇 -2041 中止时改写成
    `cursor:<最后一个已尝试的 benchmark_id>`。补采只在四个定点各跑一次,没有游标的话
    每轮都从队头第一个号重新开始,-2041 之后的号**永远轮不到补采**(标记也永不清除)。
    """
    from app.db.models import SystemConfig

    settings = settings or get_settings()
    if not getattr(settings, "weread_fullsync_on_renewal", False):
        # 2026-09-28 实测:renewal 后新会话上打 81×2 的补采炸弹会数小时内打穿全部额度
        # (书架门轻量监听与它抢同一份会话额度),默认关停;需要补采时手动开启
        return {"status": "disabled"}
    flag_key = f"weread_fullsync_pending_{user_id}"
    flag = session.scalar(select(SystemConfig).where(SystemConfig.key == flag_key))
    if not flag:
        return {"status": "not_pending"}
    cookie = _root._weread_cookie(session, user_id, settings)
    if not cookie:
        return {"status": "skipped", "reason": "no_cookie"}
    m = re.match(r"^cursor:(\d+)$", str(flag.value or "").strip())
    start_after = int(m.group(1)) if m else 0
    rows = session.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.active.is_(True),
        WechatBenchmark.weread_book_id != "", WechatBenchmark.id > start_after
    ).order_by(WechatBenchmark.id)).all()
    if not rows:  # 游标已到尾(号后来被删完等):标记清掉,别每轮空转
        session.delete(flag)
        session.commit()
        return {"status": "done", "synced": 0, "new_articles": 0, "failed": 0}
    synced = articles_new = failed = 0

    def _abandon(last_id: int) -> dict:
        """把队尾留给下个会话窗口:推进游标后提交,标记保留。"""
        flag.value = f"cursor:{last_id}"
        session.commit()
        return {"status": "aborted", "reason": "rate_limited",
                "synced": synced, "new_articles": articles_new}

    for b in rows:
        try:
            out = _root.sync_wechat_account(session, user_id, b.id, settings=settings)
            if out.get("weread_list") == "limited":
                # 列表接口对本会话已耗尽:后面几十个号也只会各撞一次并退化成"最新一篇",
                # 白烧微信读书调用密度。标记保留,等下个新 skey 会话窗口再补。
                logger.warning("mp/articles 预算耗尽(-2041),本轮补采中止;"
                               "下次从号 %s 之后续(已补 %s 号 %s 篇)", b.id, synced, articles_new)
                return _abandon(b.id)
            if out.get("status") == "success":
                synced += 1
                articles_new += int(out.get("new") or 0)  # sync 的计数字段就叫 new
            else:
                failed += 1
        except WereadError as exc:
            # -2041 = 该 skey 的列表接口预算已耗尽:全局放弃本轮(标记保留,
            # 下个新 skey 会话再补),否则其余号每号白撞一次
            session.rollback()
            if "-2041" in str(exc):
                logger.warning("mp/articles 预算耗尽(-2041),本轮补采中止;下次从号 %s 之后续", b.id)
                return _abandon(b.id)
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
            if _root.title_hits(title):
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

    settings = settings or get_settings()
    db = get_session_local()()
    total = 0
    try:
        for uid in db.scalars(select(User.id).where(User.enabled.is_(True)).order_by(User.id)).all():
            try:
                total += _root.keyword_article_tick(db, uid, settings)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("关键词文章监控失败 user=%s", uid)
        db.commit()
    finally:
        db.close()
    return total
