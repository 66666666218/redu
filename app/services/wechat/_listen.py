"""监听轮:书架粗筛、额度熔断、微信读书采集、批次轮换、主循环。"""

from app.db.models import (FeishuAlert, User, WechatArticle, WechatBenchmark, WechatCandidate,
                           WechatPanLink, WechatRewrite, WechatTrafficSample)

from app.db.tx import HeldSavepoint, savepoint

from app.services.reader_platform_client import PlatformError, ReaderPlatformClient

from app.services.tenant_base import _base, _record_run

from app.services.weread_client import WereadAuthError, WereadClient, WereadError, build_mp_url

from app.services.werss_client import WerssClient

from config.settings import Settings, get_settings

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update

from sqlalchemy.exc import IntegrityError

from sqlalchemy.orm import Session

import json

import re

import uuid

import zlib

from app.services.wechat._text import _MY_LINK_RE, _parse_time, fetch_article_content
from app.services.wechat._source import _cookie_fingerprint, _platform_client, _weread_cookie, add_benchmark, feed_biz, refresh_weread_cookie, weread_shelf
from app.services.wechat._enrich import _enrich_new_articles, _insert_new_articles


_LISTEN_CURSOR_KEY = "wechat_listen_cursor_{uid}"
from app.utils import get_logger

logger = get_logger(__name__)
from app.services import wechat_monitor as _root  # 兼容 monkeypatch:可替换名经门面运行时查找

def _advance_listen_cursor(session: Session, user_id: int) -> int:
    """读并推进监听批次游标(存 system_config,所有入口共享同一轮转序列)。

    返回本次应使用的 batch_index;副作用是游标 +1(先取后增,从 0 起)。
    并发场景:监听轮自身有"同一用户不并发"的时长锁(见 run_wechat_listen),
    游标读写实际串行,无需额外加锁。
    """
    from app.db.models import SystemConfig

    key = _LISTEN_CURSOR_KEY.format(uid=user_id)
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
    current = int(row.value) if row and str(row.value).isdigit() else 0
    if row is None:
        session.add(SystemConfig(key=key, value=str(current + 1)))
    else:
        row.value = str(current + 1)
    session.commit()
    return current
_WEREAD_QUOTA_MARKS = ("-2014", "-2041")
_COVER_QUOTA_TRIP = 3
_DORMANT_MISS = 7      # 连续 N 轮确认未发文 → 沉睡降频(与前端"沉睡"口径一致)
_DORMANT_SKIP = 3      # 沉睡号每 3 轮参与 1 轮
_BATCH_MAX = 75        # 单轮额度安全线(75 号 ≈150 请求;微信读书会话预算实测 160~200)
_BATCH_MIN = 8


def _select_listen_batch(session: Session, user_id: int, all_rows: list,
                         settings, batch_index: int | None = None,
                         batch_size: int | None = None) -> tuple[list, str]:
    """自适应分批 + 权重分层(2026-10-01,为号池扩建准备)。返回 (本轮号列表, batch_pos)。

    - **批大小自适应**:clamp(ceil(有效池/定点数), 8, 75)——池子涨了自动调,每天尽量
      全覆盖;超 75 时天然"多日轮转"(每天 4×75=300 号,池 400 两天轮完一遍)。
    - **权重分层**:沉睡号(miss_count≥7)每 3 轮只参与 1 轮——额度花在"有产出的号"上。
    - 显式 batch_size/batch_index(测试/特殊用途)保持旧语义,不叠分层。
    - settings.wechat_listen_batch_size: 0=自适应;非 0=固定批(旧);负数=全量。
    """
    N = len(all_rows)
    if batch_size is not None:
        eff = batch_size
        if eff <= 0 or N <= abs(eff):
            return all_rows, ""
        n_groups = N // abs(eff) + (1 if N % abs(eff) else 0)
        idx = batch_index or 0
        start = idx % n_groups
        return (all_rows[start * abs(eff):(start + 1) * abs(eff)],
                f" batch={start + 1}/{n_groups}(size={abs(eff)},cursor={idx})")

    cfg = getattr(settings, "wechat_listen_batch_size", 0)
    if cfg < 0:                       # 逃生门:负数 = 全量
        return all_rows, ""
    if batch_index is None:
        batch_index = _advance_listen_cursor(session, user_id)

    active = [b for b in all_rows if (b.miss_count or 0) < _DORMANT_MISS]
    dormant = [b for b in all_rows if (b.miss_count or 0) >= _DORMANT_MISS]
    dormant_pick = dormant[batch_index % _DORMANT_SKIP::_DORMANT_SKIP] if dormant else []
    pool = active + dormant_pick

    K = 4  # 每日定点数(WECHAT_LISTEN_HOURS);每日覆盖预期 = K × B
    if cfg and cfg > 0:               # 固定批(旧行为):在全量池上轮转
        B = cfg
        pool = all_rows
        dormant_pick = []
    else:                             # 自适应:按有效池定批
        B = max(_BATCH_MIN, min(_BATCH_MAX, -(-len(pool) // K)))
    if B <= 0 or len(pool) <= B:
        return pool, (f" all(size={len(pool)})" if len(pool) < N else "")

    n_groups = len(pool) // B + (1 if len(pool) % B else 0)
    start = batch_index % n_groups
    rows = pool[start * B:(start + 1) * B]
    extra = f",active={len(active)},dormant={len(dormant)}/{_DORMANT_SKIP}轮" if dormant else ""
    return rows, f" batch={start + 1}/{n_groups}(size={B},cursor={batch_index}{extra})"


def _is_weread_quota_error(exc: BaseException) -> bool:
    """-2014(频率额度)/ -2041(会话列表预算耗尽):同属"再问也不给,还会把风控加深"。"""
    text = str(exc)
    return any(mark in text for mark in _WEREAD_QUOTA_MARKS)
_BAN_MARKERS = (("此账号已被屏蔽", "账号封禁"), ("该内容已被发布者删除", "作者删除"),
                ("此内容因违规无法查看", "违规处理"))
def _detect_ban_reason(page_text: str) -> str:
    """识别微信封文/封号页并提取违规类别;正常页面返回 ''。

    微信公开页**不区分投诉人**——只给「由用户投诉并经平台审核」+ 违规类别;
    但类别含「版权/商标/专利」即走了知识产权通道,普通用户很少走,批量出现
    就是版权方清扫的典型特征(2026-09-28 霸王茶姬杯贴文实测:同类资源多号
    接连被封,盘商分享链同步被批量投诉 41031)。
    """
    if not page_text:
        return ""
    for marker, note in _BAN_MARKERS:
        if marker in page_text:
            ip = ("版权" in page_text or "商标" in page_text or "专利" in page_text)
            return f"{note}·{'侵权投诉(版权/商标/专利)' if ip else '平台规范'}"
    return ""
def _weread_collect(user_id: int, b: WechatBenchmark, weread: WereadClient,
                    session: Session, stats: dict | None = None,
                    breaker: dict | None = None, shelf_ts: str | int | None = None,
                    banned_out: dict[str, str] | None = None
                    ) -> tuple[list[WechatArticle], bool]:
    """微信读书单号采集:**cover 最新一篇(稳定可用)→ mp/articles 列表(可选,常被限权)。

    实测(2026-09):mp/articles 仅在会话建立初期可用,数小时后被服务端限权(-2041),
    cover 始终可用——故 cover 为主路径,mp/articles 失败静默跳过不影响监听。
    **但"只有 cover"就意味着同一天发第 2、3 篇会被最新一篇顶掉、永久漏采**(两轮之间最长 8h),
    这正是"近 24h 必须全推"的唯一真实缺口;列表可用时该缺口不存在。
    `stats` 记账可枚举性(见调用方),不可枚举又采到新文时必须暴露给运维,不能假装全覆盖。
    近3天过滤;阅读/点赞以 cover/mp_articles 自带值为准(免费)。
    `shelf_ts` = 书架粗筛拿到的 lastChapterCreateTime:cover 文章就是该号最新一篇,
    书架时间戳即它的发布时间——此前 cover 路径 publish_at 恒空,卡片时效/补推窗口/
    采样窗口全都吃不到,现在每轮监听顺手补上(2026-09-27)。

    第二个返回值 = **源这一轮有没有正面回答过这个号**(cover 给出一篇可看的文 / 列表枚举成功)。
    调用方据此决定 `miss_count` 加不加:+1 的含义是"确认当天没发文",
    cover 空响应且列表又挂了的"什么都不知道"不能算进去——否则一次风控就把正常号
    刷成"连续 N 轮未发文"的沉睡号,运营会去删本来在发的号。
    """
    from app.services.weread_client import WereadClient as _WC

    cutoff = datetime.now() - timedelta(days=3)
    items = []
    # 主路径:cover 最新一篇(始终可用);发布时间用书架时间戳盖(封面文=最新一篇)
    item = weread.latest_article(b.weread_book_id)
    cover_ok = bool(item and item.get("url"))
    if cover_ok:
        items.append({"title": item["title"], "url": item["url"],
                      "publish_at": _shelf_ts_to_dt(shelf_ts),
                      "review_id": str(item.get("review_id") or "")})
    # 备选:mp/articles 近期列表(含精确阅读/点赞;被限权时静默跳过)
    # ⚠️ 这个接口是**稀缺额度**:一轮 81 个号各问一次就是 81 次,微信读书按会话/IP 记账,
    # 队头那十几个号把额度吃光后,后面的号必然连 cover 一起被 -2014 挡掉(2026-09-27 实测:
    # 同 IP 连打 7 次 cover 即回 -2014)。所以一旦本轮被额度类错误挡下,`breaker` 就合闸,
    # 剩余号不再问列表——省下来的额度留给"每个号至少问得动 cover"。
    listed = False
    list_skipped = bool(breaker is not None and breaker.get("list_off"))
    if not list_skipped:
        try:
            payload = weread.mp_articles(b.weread_book_id)
            for it in _WC.flatten_mp_articles(payload):
                ts = it.get("create_time") or 0
                pub = datetime.fromtimestamp(ts) if ts else None
                if pub and pub < cutoff:
                    continue
                items.append({"title": it["title"], "url": build_mp_url(it["original_id"]),
                              "read_num": it["read_num"], "like_num": it["like_num"],
                              "publish_at": pub, "review_id": str(it.get("review_id") or "")})
            listed = True
        except Exception as exc:  # noqa: BLE001 - 限权/废弃不影响 cover 主路径
            # -2041 是新版微信读书对该接口的永久限权,每进程只记一次,避免每账号刷屏
            if not getattr(_weread_collect, "_mp_articles_warned", False):
                _weread_collect._mp_articles_warned = True
                logger.warning("mp/articles 不可用(%s),全部账号仅用 cover 最新一篇", exc)
            if breaker is not None and _is_weread_quota_error(exc):
                breaker["list_off"] = True
    # 正文:先直抓 mp.weixin.qq.com(不占微信读书配额),**抓空了再用这篇的 reviewId
    # 走微信读书转发页**。此前这里只传 fetch_content=True,把 cover/列表白拿的 reviewId 丢了,
    # 于是直抓被风控的那 26% 正文永远为空 → 盘链认不出 → 飞书卡片整片"—"而员工以为号没发资源
    # (本机库实测 369 篇里 97 篇正文空、97 篇全部无盘链;「同步文章」走 mp_content 就没这问题)。
    rid_of = {it["url"]: it["review_id"] for it in items if it.get("review_id")}

    def _resolve(title: str, url: str = "") -> str:
        body = _root.fetch_article_content(url)
        reason = _detect_ban_reason(body)
        if reason:
            # 封文/封号页不当正文入库(污染检索与分析),状态带出去给卡片标 ⛔
            if banned_out is not None and url:
                banned_out[url] = f"{title[:24]}·{reason}"
        elif body:
            return body
        rid = rid_of.get(url) or ""
        if not rid:
            return ""
        alt = weread.mp_content(rid)   # 惰性回退:直抓成功就绝不追打转发页(2s 节流=风控暴露)
        reason = _detect_ban_reason(alt)
        if reason:
            if banned_out is not None and url:
                banned_out[url] = f"{title[:24]}·{reason}"
            return ""
        return alt

    # require_pan=False:不再丢弃无盘链文——"标题不含网盘词"≠"没价值",
    # 此前这道闸把 15 个对标号 10 天的新文全部静默丢弃(用户看到"停更在 9.7"的根因)
    got = _insert_new_articles(session, user_id, b, items, source="listen",
                               content_resolver=_resolve, require_pan=False)
    if banned_out and got:
        for r in got:
            if r.url in banned_out:
                # 沿用 41031 的标记位语义:卡片网盘列认「原文失效」显示 ⛔,免得员工白点尸体链
                r.my_pan_urls = f"⚠️原文失效({banned_out[r.url]}),未转存"
    if stats is not None:
        if listed:
            key = "weread_list_ok"
        elif got:
            # 列不出却采到新文 = 同日其它篇**未知丢失**,必须进铁律告警,不能因为
            # "本轮被熔断没问列表"就把它记成无害的 off
            key = "weread_list_off_new"
        elif list_skipped:
            key = "weread_list_skipped"
        else:
            key = "weread_list_off"
        stats[key] = stats.get(key, 0) + 1
    return got, (cover_ok or listed)
def _listen_lock_key(user_id: int) -> str:
    return f"wechat_listen_running_{user_id}"
_SHELF_REVIEW_KEYS = ("reviewId", "review_id", "lastReviewId", "latestReviewId", "mpReviewId")
_SHELF_TS_KEYS = ("lastChapterCreateTime", "updateTime")
def _shelf_slot(book_id: str, every: int) -> int:
    """每号在"强制问询"轮转里的固定槽位(bookId 散列:与排序无关,增删号不影响别人的节奏)。"""
    return zlib.crc32(str(book_id).encode("utf-8")) % max(1, every)
def _bump_shelf_round(session: Session, user_id: int) -> int:
    """粗筛轮计数器(system_config):强制问询按"轮数+号槽位"轮转,返回自增前的轮数。"""
    from app.db.models import SystemConfig

    key = f"weread_shelf_round_{user_id}"
    row = session.get(SystemConfig, key)
    try:
        cur = int(str(row.value)) if row and row.value else 0
    except ValueError:
        cur = 0
    val = str(cur + 1)
    if row is None:
        session.add(SystemConfig(key=key, value=val, updated_at=datetime.now()))
    else:
        row.value = val
        row.updated_at = datetime.now()
    return cur
def _load_shelf_marks(session: Session, user_id: int) -> dict[str, str]:
    """书架水位(JSON 落 system_config):每号上次「问过且答上」时看到的信号值。"""
    from app.db.models import SystemConfig

    row = session.get(SystemConfig, f"weread_shelf_marks_{user_id}")
    try:
        data = json.loads(row.value) if row and row.value else {}
    except ValueError:
        data = {}
    return {str(k): str(v) for k, v in data.items()}
def _save_shelf_marks(session: Session, user_id: int, updates: dict[str, str]) -> int:
    """水位前移(写入 system_config JSON),返回前移的号数。

    只收「问过且答上了」的号是水位机制的安全底线:问都没问成(额度熔断/请求失败)
    就前移,等于把没采到的那篇永久记成"见过"——下一轮书架值不变就跳过,漏推再也补不回来。
    """
    from app.db.models import SystemConfig

    if not updates:
        return 0
    key = f"weread_shelf_marks_{user_id}"
    marks = _load_shelf_marks(session, user_id)
    marks.update(updates)
    val = json.dumps(marks, ensure_ascii=False)
    row = session.get(SystemConfig, key)
    if row is None:
        session.add(SystemConfig(key=key, value=val, updated_at=datetime.now()))
    else:
        row.value = val
        row.updated_at = datetime.now()
    return len(updates)
def _shelf_ts_to_dt(value) -> datetime | None:
    """书架 lastChapterCreateTime(unix 秒)→ 发布时间;离谱值一律 None(宁缺勿错)。"""
    try:
        n = int(str(value))
    except (TypeError, ValueError):
        return None
    if not 1_500_000_000 <= n <= 4_000_000_000:
        return None
    return datetime.fromtimestamp(n)
def _shelf_gate_plan(session: Session, user_id: int, rows: list[WechatBenchmark],
                     weread: WereadClient | None, settings: Settings) -> dict:
    """书架粗筛(降频主刀):1 次书架请求回答「哪些号自上次问到之后没有更新」。

    判据(2026-09-27 线上实勘后定稿):书架条目的 lastChapterCreateTime == 该号水位
    (上次「问过且答上」时看到的同一字段值,JSON 落 system_config)→ 服务端没更新过
    这个号的最新章节,cover 只会吐我们已入库的那篇旧文,问它纯属白问。量化收益
    (本机 9.22 快照):每轮 81 次 cover 只有 4~20 个号真吐新文,其余 60~77 次白问
    正是烧光会话额度、让真有文的号被 -2014 挡掉的元凶。

    与首版(reviewId 比对文章 URL)的差异:线上实勘证明书架**不带 reviewId**,
    时间戳没有"可从库里反解"的对应物,水位必须自己持久化;由此多出一条铁律——
    **水位只对问过且答上的号前移**(_save_shelf_marks),问都没问成就前移等于把
    没采到的那篇永久记成"见过",那是本机制唯一会造永久漏推的错。

    失效保护,任何一环不确定都整门停用(代价只是白付 1 次书架请求):
    - 配置关停 / 客户端没有 shelf_entries(测试假件)/ 书架请求失败 → 逐号照旧;
    - 条目里认不出信号字段(名单见 _SHELF_REVIEW_KEYS/_SHELF_TS_KEYS)→ 跳过集为空;
    - 水位匹配不搞永久豁免:每号每 `weread_shelf_gate_every` 轮强制真问一次 cover,
      "书架字段滞后"这种最坏错判的盲区被压到 ≤K 轮(K=4 → 每号每天至少被真问一次)。

    返回:{ok, skip(本轮可跳过的 bookId 集), force(到期必须真问的), tier(排序权重:
    0=有更新/无水位优先问,1=强制问询,2=证实没更新), signals(每号本轮信号值), reason}。
    """
    plan: dict = {"ok": False, "skip": set(), "force": set(), "tier": {},
                  "signals": {}, "reason": ""}
    if not getattr(settings, "weread_shelf_gate", True) or weread is None:
        plan["reason"] = "disabled"
        return plan
    shelf_entries = getattr(weread, "shelf_entries", None)
    if not callable(shelf_entries):
        plan["reason"] = "no_shelf_entries"
        return plan
    try:
        entries = shelf_entries()
    except Exception as exc:  # noqa: BLE001 - 书架挂了不能连累监听:退化为逐号问
        plan["reason"] = type(exc).__name__
        logger.warning("书架粗筛请求失败,本轮退化为逐号问(%s)", exc)
        return plan
    signals: dict[str, str] = {}
    for it in entries or []:
        bid = str(it.get("bookId") or "")
        if not bid:
            continue
        sig = ""
        for k in _SHELF_REVIEW_KEYS:      # reviewId 优先:文章身份,判据最强
            v = str(it.get(k) or "").strip()
            if v:
                sig = v
                break
        if not sig:
            for k in _SHELF_TS_KEYS:      # 实勘:线上只有时间戳可用(已自证)
                if it.get(k) not in (None, ""):
                    sig = str(it[k])
                    break
        if sig:
            signals[bid] = sig
    plan["signals"] = signals
    if not signals:
        plan["reason"] = "no_signal_field"
        return plan
    marks = _load_shelf_marks(session, user_id)
    every = max(1, int(getattr(settings, "weread_shelf_gate_every", 4) or 4))
    rnd = _bump_shelf_round(session, user_id)
    for b in rows:
        sig = signals.get(b.weread_book_id)
        mark = marks.get(b.weread_book_id)
        if sig and mark and (sig == mark or sig.endswith("_" + mark)):
            if (rnd + _root._shelf_slot(b.weread_book_id, every)) % every == 0:
                plan["force"].add(b.weread_book_id)
                plan["tier"][b.id] = 1     # 保险单:排在"可能有更新"的号后面问
            else:
                plan["skip"].add(b.weread_book_id)
                plan["tier"][b.id] = 2
        else:
            plan["tier"][b.id] = 0         # 有更新/首轮无水位/书架没这号:优先问
    plan["ok"] = True
    return plan
def _acquire_listen_slot(session: Session, user_id: int, settings: Settings) -> str | None:
    """抢占"这一用户的一轮监听"执行权(跨进程/跨 worker 的时长锁)。抢到给令牌,抢不到给 None。

    `claim_schedule` 的乐观锁只保护**抢占那一刻**(比较 last_run_at),而一轮监听要跑几分钟:
    期间用户再点一次「立即监听」、或后台 `retry_failed_runs` 撞上在跑的定时轮,两轮就会并行
    扫同一批号——同一篇新文发两张卡、微信读书密度翻倍招风控
    (2026-09-26 第九轮审计)。标记落在 `system_config`(整库可见),超过 TTL 视为持有进程已死,
    允许后来者接管:宁可重跑一轮,也不能因没解锁而把该用户的监听永久锁死。
    """
    from app.db.models import SystemConfig

    key = _listen_lock_key(user_id)
    now = datetime.now()
    stale_before = now - timedelta(minutes=max(1, int(settings.wechat_listen_lock_ttl_minutes or 20)))
    token = uuid.uuid4().hex
    if session.get(SystemConfig, key) is None:
        try:
            with savepoint(session):     # 插入冲突=别人刚抢到,只撤销这一段
                session.add(SystemConfig(key=key, value=token, updated_at=now))
                session.flush()
        except IntegrityError:
            return None
        session.commit()
        return token
    res = session.execute(
        update(SystemConfig).where(
            SystemConfig.key == key,
            or_(SystemConfig.value == "", SystemConfig.updated_at.is_(None),
                SystemConfig.updated_at < stale_before),
        ).values(value=token, updated_at=now))
    session.commit()
    return token if res.rowcount else None
def _release_listen_slot(session: Session, user_id: int, token: str) -> None:
    """解锁:只删自己那把令牌(接管过的后来者令牌不同,不会被误删)。"""
    from app.db.models import SystemConfig

    try:
        with savepoint(session):
            session.execute(delete(SystemConfig).where(
                SystemConfig.key == _listen_lock_key(user_id), SystemConfig.value == token))
            session.flush()
        session.commit()
    except Exception:  # noqa: BLE001 - 解锁失败只能等 TTL 过期,不能盖掉本轮结果
        logger.exception("释放监听在跑标记失败 user=%s(该用户在标记过期前不能再起一轮)", user_id)
def run_wechat_listen(session: Session, user_id: int, settings: Settings | None = None,
                      weread: WereadClient | None = None,
                      platform: ReaderPlatformClient | WerssClient | None = None, push: bool = True,
                      batch_index: int | None = None, batch_size: int | None = None) -> dict:
    """监听一轮(外层是"同一用户不并发"的时长锁,内层 `_listen_round` 才是采集本体)。

    所有入口——手动「立即监听」、调度器定时轮、管理端失败重试——都走这里,所以防重只需要
    在这一处做实。被挡住的一轮返回 `skipped/running`,不产生采集副作用。
    """
    st = _base(settings)
    token = _acquire_listen_slot(session, user_id, st)
    if token is None:
        _record_run(session, user_id, "wechat_listen", "skipped",
                    "running(上一轮监听尚未结束)")
        session.commit()
        logger.warning("公众号监听跳过 user=%s:上一轮仍在执行", user_id)
        return {"platform": "wechat", "status": "skipped", "reason": "running"}
    try:
        return _root._listen_round(session, user_id, settings=settings, weread=weread,
                             platform=platform, push=push, batch_index=batch_index,
                             batch_size=batch_size)
    finally:
        _release_listen_slot(session, user_id, token)
def _listen_round(session: Session, user_id: int, settings: Settings | None = None,
                  weread: WereadClient | None = None,
                  platform: ReaderPlatformClient | WerssClient | None = None, push: bool = True,
                  batch_index: int | None = None, batch_size: int | None = None) -> dict:
    """监听一轮:纯免费源——免费列表(WeRSS/读书平台)→ 微信读书(cover/列表)→ 新文入库推飞书。

    (2026-09-29 用户决策:dajiala 收费链整体摘除,免费源不可用即如实记 failed/partial。)
    全部数据源不可用才返回 `skipped`。
    """
    settings = _base(settings)
    # 开工先补上一轮的欠推:飞书抖动/超时/进程被杀留下的"已入库未推送"文章在这一轮补上。
    # 关在保存点里——补推本身炸了不能伤到本轮采集(与后处理同一条原则)。
    repushed = 0
    try:
        with savepoint(session):
            repushed = repush_unpushed(session, user_id, settings)
    except Exception:  # noqa: BLE001 - 补推失败不影响本轮监听
        logger.exception("补推上轮欠推失败 user=%s", user_id)
    all_rows = session.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.active.is_(True))
        .order_by(WechatBenchmark.id)).all()
    if not all_rows:
        # 跳过也记运维记录:否则后台"没有公众号情况",无从判断是没加号还是没跑
        _record_run(session, user_id, "wechat_listen", "skipped", "no_benchmarks(未添加对标号)")
        session.commit()
        return {"platform": "wechat", "status": "skipped", "reason": "no_benchmarks"}
    # 自适应分批 + 权重分层(2026-10-01,随号池扩建自动适配;见 _select_listen_batch):
    # 批大小按池子规模自适应、沉睡号降频;cursor=原始游标值(重试路径据此重跑同一组)。
    rows, batch_pos = _select_listen_batch(session, user_id, all_rows, settings,
                                           batch_index=batch_index, batch_size=batch_size)
    cookie = _root._weread_cookie(session, user_id, settings)
    if not cookie:
        _record_run(session, user_id, "wechat_listen", "skipped",
                    "no_source(无微信读书 Cookie,免费列表源未配置或不可用)")
        session.commit()
        return {"platform": "wechat", "status": "skipped", "reason": "no_source"}

    plat = platform or _root._platform_client(settings, session=session, user_id=user_id)
    # 书架粗筛(1 次书架请求换"谁没更新"的答案):把每轮 60~77 次白问省下来。
    # 任何不确定(字段认不出/书架挂了/配置关停)都整门停用,退化为逐号问
    # (判据与三重失效保护见 _shelf_gate_plan;量化依据见 doc/operations.md §9.2)。
    weread = weread or (_root.WereadClient(cookie) if cookie else None)
    gate = _shelf_gate_plan(session, user_id, rows, weread, settings)
    if gate["ok"]:
        # 稳定排序把"可能有更新/无水位"的号排到队头:额度/熔断真触发时,稀缺的请求
        # 先花在最可能吐新文的号上(书架说没更新、仅到强制问询期的号殿后)
        rows = sorted(rows, key=lambda b: gate["tier"].get(b.id, 0))
    now = datetime.now()
    new_rows: list[WechatArticle] = []
    failed = 0
    # 微信读书"近期列表"可枚举性统计:决定"近24h全推"能不能兑现(见 _weread_collect)
    wr_stats: dict = {}
    # 额度熔断:微信读书按会话/IP 给请求记账,一轮 162 次(81 号 × cover+列表)远超它的容忍线。
    # 被额度类错误(-2014/-2041)挡下后继续让剩余号逐个去撞,只会把风控加深、让队尾整轮挨饿,
    # 所以这里合闸:列表先停,连续 _COVER_QUOTA_TRIP 个号 cover 也挡不下就整源停。
    breaker: dict = {"list_off": False, "cover_quota_fails": 0, "off": False}
    quota_skipped = 0
    no_free_source = 0
    marks_advance: dict[str, str] = {}   # 本轮「问过且答上」的号 → 书架信号值(轮末前移水位)
    banned: dict[str, str] = {}          # 本轮被封/被删的文章 url → 标题·违规类别(清扫预警用)
    for b in rows:
        used = False
        # ⓪ 免费全量列表(自建 WeRSS 或 wewe-rss 兼容的读书平台):biz 是源认识的形态才首选
        feed_id = feed_biz(b)
        if plat and feed_id:
            try:
                raw_items = plat.mp_articles(feed_id, page=1, limit=20)
                norm = [{"title": it["title"], "url": it["url"],
                         "publish_at": _parse_time(it.get("publish_at_raw"))} for it in raw_items]
                if norm:
                    used = True
                    b.miss_count = 0
                    b.last_item_at = now
                    got = _insert_new_articles(session, user_id, b, norm, source="listen",
                                               fetch_content=True, require_pan=False)
                    if got:
                        new_rows.extend(got)
                else:
                    # 首页就空 ≠ 这个号今天没发文:更可能是这个订阅在源里不存在/还没抓到
                    # (WeRSS 里没加、或它的 weread 模式被限权)。不能当采集成功——否则 ① 的 cover
                    # 也被挤掉,该号整轮失明还表现为"连续 N 轮未发文"。留给后续源回答。
                    logger.warning("免费列表 %s 返回空(biz=%s),本轮交由后续源", b.nickname, feed_id)
            except PlatformError as exc:
                logger.warning("读书平台监听 %s 失败,降级后续源:%s", b.nickname or b.biz, exc)
        # ① 微信读书(免费):对标号已关联 bookId 且有 Cookie;登录失效时自动续期重试一次
        wr_eligible = (not used) and bool(cookie) and bool(b.weread_book_id)
        if not used and not wr_eligible and not plat:
            no_free_source += 1  # 免费列表源缺失+无 bookId 的手动号:本轮结构性失明,计入运维记录
        if wr_eligible and breaker["off"]:
            # 整源已停:本号这一轮"什么都不知道",不能算 answered(故不动 miss_count),
            # 但必须计数暴露——否则运维记录会长得跟"81 个号都问过了、只是没新文"一样。
            quota_skipped += 1
        elif wr_eligible and b.weread_book_id in gate["skip"]:
            # 书架水位未变:cover 只会吐库里已有的那篇旧文,这一跳省的是纯白问、不是盲区。
            # 语义与"cover 答了但没新文"对齐(miss_count+1、置 used 视为本号已有答案);
            # 书架字段若滞后,强制问询轮(每号每 K 轮)会把错判纠回来。
            wr_stats["weread_cover_shelf_skipped"] = wr_stats.get("weread_cover_shelf_skipped", 0) + 1
            used = True
            b.miss_count = (b.miss_count or 0) + 1
        elif wr_eligible:
            try:
                weread = weread or _root.WereadClient(cookie)
                got, answered = _weread_collect(user_id, b, weread, session,
                                                stats=wr_stats, breaker=breaker,
                                                shelf_ts=gate["signals"].get(b.weread_book_id),
                                                banned_out=banned)
                # 只有免费源真答了才算"本号已被消费":答不上按 failed 计,如实暴露。
                used = answered
                if answered and gate["signals"].get(b.weread_book_id):
                    # 答上了才准前移水位(问都没问成就前移=把没采到的篇永久记成"见过")
                    marks_advance[b.weread_book_id] = gate["signals"][b.weread_book_id]
                if got:
                    new_rows.extend(got)
                    b.miss_count = 0
                    b.last_item_at = now
                elif answered:
                    # cover 报得出"最新一篇是哪篇",库里也确有这篇 → 当天确实没发文。
                    # 此前微信读书路径从不 +1(只有平台分支加),而它是 81 个号
                    # 唯一的源 → 前端"连续 N 轮未发文"永远空着,号停更和源挂了分不清。
                    b.miss_count = (b.miss_count or 0) + 1
                else:
                    # 免费源没答上(cover 空 + 列表挂):dajiala 摘除后没有第二双腿,
                    # 必须计 failed 如实暴露——旧版此处静默留给付费兜底,兜底没了就是纯盲区。
                    failed += 1
            except WereadAuthError as exc:
                logger.warning("微信读书登录态失效(用户 %s):%s;尝试自动续期", user_id, exc)
                refreshed = _root.refresh_weread_cookie(session, user_id, settings)
                if refreshed.get("status") == "success":
                    cookie = refreshed["cookie"]
                    # 续期=换了一把新会话,列表额度按新会话重新计(见 weread_client 头注释:
                    # mp/articles 仅在会话建立/续期后初期可用)→ 熔断重新合上再试
                    breaker.update(list_off=False, cover_quota_fails=0, off=False)
                    try:
                        got, answered = _weread_collect(user_id, b, _root.WereadClient(cookie), session,
                                                        stats=wr_stats, breaker=breaker,
                                                        shelf_ts=gate["signals"].get(b.weread_book_id),
                                                        banned_out=banned)
                        used = answered  # 答上了就消费掉本号(答不上按 failed 计,如实暴露)
                        if answered and gate["signals"].get(b.weread_book_id):
                            marks_advance[b.weread_book_id] = gate["signals"][b.weread_book_id]
                        if got:
                            new_rows.extend(got)
                            b.miss_count = 0
                            b.last_item_at = now
                        elif answered:
                            b.miss_count = (b.miss_count or 0) + 1
                        else:
                            failed += 1  # 续期后仍没答上:无兜底,如实计失败
                    except WereadError as exc2:
                        failed += 1
                        logger.warning("微信读书续期后仍失败 %s:%s", b.nickname or b.weread_book_id, exc2)
                else:
                    # 续期失败:本号本轮记失败,如实暴露(无付费兜底可降级)
                    # 指纹必须在清空 cookie 前算——否则 md5("")[:6]="d41d8c" 恒定,
                    # 冷却键永远撞同一个假指纹,通知无法随用户换新 Cookie 而重置。
                    fp = _cookie_fingerprint(cookie)
                    cookie = ""
                    failed += 1
                    # 即时提醒用户更新 Cookie(6h 冷却,不刷屏)
                    from app.services.alert_service import notify_incident
                    notify_incident(
                        session, user_id, "wechat",
                        "🟠 微信读书 Cookie 已过期,请更新[" + fp + "]",
                        "自动续期失败。请在浏览器登录 weread.qq.com 后 F12 复制 Cookie,"
                        "粘贴到「Cookie 管理」页 weread 平台(或发给我更新)。"
                        "粘贴前确认串里有「wr_rt=」——缺它自动续期无从下手,十几小时必过期。"
                        "⚠️ 若服务器出口 IP 刚变过(容器重建/加了代理/换网络),同一把 Cookie 也会立刻被判"
                        "登录超时(会话与出口 IP 绑定,2026-09-27 实测)——那就先固定出口再重扫,"
                        "否则新 Cookie 一样当场死",
                        settings=settings)
            except WereadError as exc:
                failed += 1
                logger.warning("微信读书监听 %s 失败:%s", b.nickname or b.weread_book_id, exc)
                if _is_weread_quota_error(exc):
                    breaker["cover_quota_fails"] += 1
                    if breaker["cover_quota_fails"] >= _COVER_QUOTA_TRIP:
                        breaker["off"] = True
                        logger.warning("微信读书连续 %d 个号回额度类错误,本轮剩余号停采(别再加深风控)",
                                       breaker["cover_quota_fails"])
                else:
                    breaker["cover_quota_fails"] = 0
    # 水位前移(只收问过且答上的号,铁律见 _save_shelf_marks):写丢=下轮重问一遍,无害
    try:
        advanced = _save_shelf_marks(session, user_id, marks_advance)
    except Exception:  # noqa: BLE001 - 水位回写失败不能连累本轮采集与推送
        logger.exception("书架水位回写失败 user=%s", user_id)
        advanced = 0
    if banned:
        # 版权清扫预警(2026-09-28 霸王茶姬杯贴文实测引出):同轮 ≥2 篇被投诉下架 = 批量
        # 维权特征,值得让群里知道(同类资源盘链可能连带失效);单篇只落站内不刷群。
        # 微信封禁页不区分投诉人,但类别「版权/商标/专利」即知识产权通道——普通用户
        # 很少走,批量出现即为版权方清扫的典型特征,文案里如实写「疑似」。
        from app.services.alert_service import notify_incident

        cats = "、".join(sorted({v.split("·")[-1] for v in banned.values()}))
        if len(banned) >= 2:
            notify_incident(
                session, user_id, "wechat",
                f"⚠️ 疑似版权清扫:本轮 {len(banned)} 篇文章被投诉下架",
                f"违规类别:{cats}。微信封禁页只标『由用户投诉并经平台审核』,不区分投诉人,"
                "但知识产权类别的批量投诉基本是版权方维权。同类资源的盘链可能被连带投诉、"
                "陆续失效——群里看到可用的 🔴 转存链尽快保存,别等卡变 ⛔。"
                "命中:" + "; ".join(list(banned.values())[:5]),
                settings=settings, push_feishu=True)
        else:
            notify_incident(
                session, user_id, "wechat",
                "⚠️ 1 篇文章被投诉下架(仅站内记录)",
                f"违规类别:{cats}。命中:{list(banned.values())[0]}",
                settings=settings, push_feishu=False)
    # 后处理(回填/采样/转存/共振)整段关在保存点里:它炸了只撤销自己那半截写,
    # 本轮已采到的新文照样 commit + 推飞书(回落原文)。此前它是裸调用,
    # 一次转存异常会连带 `_record_run`/`_push_listen` 全部跳过(第八轮审计)。
    replacements: dict[int, list[tuple[str, str, str]]] = {}
    try:
        with savepoint(session):
            replacements = _root._enrich_new_articles(session, user_id, settings, new_rows)
    except Exception:  # noqa: BLE001 - 转存炸了也要推(标题回落原文)
        logger.exception("监听后处理失败 user=%s(本轮推送回落原文)", user_id)
        replacements = {}

    session.commit()

    # 判定:全部账号失败=failed;部分失败=partial(即便采到新文,故障也要暴露)。
    # 此前 `not failed or new_rows` 优先级等于 `(not failed) or new_rows`,
    # 只要采到 1 篇新文就把"全部账号挂了"也记成 success 掩盖故障
    # quota_skipped(整源熔断后根本没问过的号)同样是"没监控到",不能记 success:
    # 否则 81 号只问了 12 个的一轮会长得跟"81 号都问过了、只是没新文"一模一样。
    if failed == len(rows):
        status = "failed"
    elif failed or quota_skipped:
        status = "partial"
    else:
        status = "success"
    detail = f"accounts={len(rows)} new={len(new_rows)} failed={failed}{batch_pos}"
    if quota_skipped:
        detail += f" quota_skipped={quota_skipped}"
    if no_free_source:
        detail += f" no_free_source={no_free_source}"
    # biz 里躺着源认不出的形态(历史上 add_benchmark 写过 base64 __biz):⓪ 分支按"没配"处理
    # 所以号不会失明,但免费全量列表也就没接上。不点名出来,运维只会以为"配了 WeRSS 就该全推"。
    miskeyed = [str(b.nickname or b.id) for b in rows if (b.biz or "").strip() and not feed_biz(b)]
    if miskeyed:
        detail += f" biz_bad_shape({len(miskeyed)})"
    if repushed:
        detail += f" repushed={repushed}"   # 补推的上一轮欠推数,写进运维记录而非只进日志
    enumerable = wr_stats.get("weread_list_ok", 0)
    off_new = wr_stats.get("weread_list_off_new", 0)
    if wr_stats:
        # list_off 必须写进运维记录:"只采到 cover 最新一篇"时同日其它篇是**未知丢失**,
        # 不能让它和"该号今天真的只发了一篇"长得一样(与 -2014 假象、全败标 success 同族)。
        # list_skipped = 本轮列表已被额度熔断挡下、这些号根本没被问过(见 breaker)。
        detail += (f" weread_list(ok={enumerable} off={wr_stats.get('weread_list_off', 0)}"
                   f" off_with_new={off_new} skipped={wr_stats.get('weread_list_skipped', 0)})")
    if gate["ok"]:
        detail += (f" shelf(signals={len(gate['signals'])} skip={len(gate['skip'])}"
                   f" force={len(gate['force'])} adv={advanced})")
    elif gate["reason"] not in ("", "disabled", "no_shelf_entries"):
        # 试过但没用上:no_signal_field=条目认不出信号字段(拿 probe 的字段表回来对名单),
        # 异常类名=书架请求本身挂了(本轮已自动退化为逐号问)
        detail += f" shelf(off={gate['reason']})"
    if banned:
        detail += f" banned={len(banned)}"
    _record_run(session, user_id, "wechat_listen", status, detail)
    session.commit()
    if push and new_rows:
        _push_listen(session, user_id, settings, new_rows, replacements)
        try:
            with savepoint(session):
                _burst_scan(session, user_id, settings, new_rows)
        except Exception:  # noqa: BLE001 - 爆点扫描失败不伤主流程
            session.rollback()
            logger.exception("爆点扫描失败 user=%s", user_id)
    if off_new and push:
        # 兑现"近24h全部推送"要靠列表枚举;只要还有号列不出来又采到了新文,就必须点名而不是安静少推。
        # 但点名落在**站内告警**:这是"要不要自建 WeRSS/要不要充值"的长期决策,不是员工群里
        # 该刷的东西——用户 2026-09-26 定的口径:飞书只推文章与 Cookie 提醒。
        from app.services.alert_service import notify_incident
        notify_incident(
            session, user_id, "wechat",
            "⚠️ 微信读书只能拿到最新一篇,同日其它篇可能漏推",
            f"本轮列不出却采到新文的号:{off_new}(可枚举 {enumerable} / 共 {len(rows)})。"
            "微信读书 mp/articles 是**按会话/IP 记额度**的稀缺接口:一把刚建立的会话能列"
            "(2026-09-27 实测:刚续期的会话回 -2014『额度』而非会话老化后的 -2041『不下发』),"
            "被 81 个号 × 每轮 2 次的密度耗尽后就整轮列不出。监听因此退化为 cover 最新一篇:"
            "两轮之间(最长 8h)同一号发多篇时,前面的那几篇顶不掉也补不回来。"
            "要真正兑现『近24h全推』:① 把列表额度花在少数号上(错峰分批/按号轮转枚举,"
            "见 §9.2 密度账);② 自建 WeRSS 的 web/app 模式(要求你有一个自己的公众号后台身份,没有就不能用——"
            "它的 weread_mp 模式会原样继承本接口的额度限制)。详见 doc/operations.md §4g/§9.2"
            + (f";另有 {len(miskeyed)} 个号的 biz 不是源认识的形态:{('、'.join(miskeyed[:5]))}"
               if miskeyed else ""),
            settings=settings, push_feishu=False)
    if quota_skipped:
        # "本轮根本没问到"是密度问题,不是"号没发文"——必须单独点名(按口径落站内,不刷飞书)。
        # 不点名的话,运营看到的只有"今天怎么又只有几个号推",而事实是"其余号被源挡在门外"。
        from app.services.alert_service import notify_incident
        notify_incident(
            session, user_id, "wechat",
            f"🟠 微信读书额度耗尽,本轮 {quota_skipped} 个号未采到",
            f"连续 {breaker['cover_quota_fails']} 个号被 -2014/-2041 挡回后本轮熔断,"
            f"剩余 {quota_skipped} 个号(共 {len(rows)})这一轮没有数据,下一轮会重新尝试。"
            f"根因是单轮请求密度({len(rows)} 号 × cover+列表 ≈ {len(rows) * 2} 次/轮、"
            "4 轮/天)超出该会话的容忍线。可选缓解:调度器启用错峰分批"
            "(2026-09-29 起已默认启用:每批 wechat_listen_batch_size=36)、"
            "拉长 WereadClient.min_gap(现 2s)。详见 doc/operations.md §9.2",
            settings=settings, push_feishu=False)
    out: dict = {"platform": "wechat", "status": status, "accounts": len(rows),
                 "new": len(new_rows), "failed": failed}
    if quota_skipped:
        out["weread_quota_skipped"] = quota_skipped
    if repushed:
        out["repushed"] = repushed
    if wr_stats:
        out["weread_list"] = {k: v for k, v in wr_stats.items()}
    if gate["ok"]:
        out["weread_shelf"] = {"signals": len(gate["signals"]), "skip": len(gate["skip"]),
                               "force": len(gate["force"]), "advanced": advanced}
    if banned:
        out["banned"] = len(banned)
    if miskeyed:
        out["biz_bad_shape"] = miskeyed[:10]
    return out
def _burst_scan(session: Session, user_id: int, settings: Settings,
                rows: list[WechatArticle]) -> int:
    """免费爆点检测(2026-09-30,dajiala 付费采样摘除后的轻量替代)。

    数据源:微信读书 cover/列表自带的**站内阅读数**(免费,随监听一并入库,零额外请求)。
    判定:新文阅读数 ≥ 同号近 14 天文章阅读中位数 × `wechat_burst_median_mult`
    且 ≥ `wechat_burst_min_reads` → 🔥 爆点卡推公众号群(建议员工立即跟进)。
    每篇只报一次(冷却键=article_id,7 天);基线不足 3 篇宁缺毋滥。返回发送篇数。
    """
    if not rows or not settings.wechat_burst_min_reads:
        return 0
    from app.services.alert_service import feishu_alert_gate
    from app.services.feishu_client import FeishuClient
    from app.services.feishu import webhook_for

    webhook = webhook_for(settings, "wechat")
    if not webhook:
        return 0
    now = datetime.now()
    client = FeishuClient(webhook, settings.feishu_secret)
    sent = 0
    for r in rows:
        if not r.benchmark_id or (r.read_num or 0) < settings.wechat_burst_min_reads:
            continue
        if not feishu_alert_gate(session, user_id, "burst_free", f"burst:{r.id}",
                                 24 * 7, f"站内阅读{r.read_num}"):
                continue  # 这篇 7 天内已报过
        vals = sorted(v for v in session.scalars(select(WechatArticle.read_num).where(
            WechatArticle.user_id == user_id, WechatArticle.benchmark_id == r.benchmark_id,
            WechatArticle.id != r.id, WechatArticle.read_num > 0,
            WechatArticle.created_at >= now - timedelta(days=14))).all() if v)
        if len(vals) < 3:
                continue  # 基线不足,宁缺毋滥
        median = vals[len(vals) // 2]
        if r.read_num < median * settings.wechat_burst_median_mult:
                continue
        mine = [x for x in (r.my_pan_urls or "").splitlines() if x.strip()]
        lines = ["🔥 爆点苗头 · 建议立即跟进改写",
                 "🔴 " + r.title[:40],
                 f"📊 站内阅读 {r.read_num}(同号中位数 {median} 的 "
                 f"{r.read_num / max(median, 1):.0f} 倍)"]
        if mine:
            lines.append("📦 我的链接: " + mine[0])
        lines.append(r.url)
        if client.send(chr(10).join(lines)):
            sent += 1
    return sent


def repush_unpushed(session: Session, user_id: int, settings: Settings | None = None) -> int:
    """补推:近 N 小时入库、却从未成功推上飞书的文章(铁律的最后一道兜底)。

    "监控号近 24h 发的文章必须全部到飞书"是产品铁律,但推送从来没有事实记录:
    `run_wechat_listen` 先 commit 新文、再发卡,卡片发送失败只留一行 exception 日志,
    那批文章就永久停在库里,而且和"根本没发文"长得一模一样(第八轮审计遗留的最后一格)。
    现在 `_push_listen` 只在**真的送达**时才落 `pushed_at`,这里把 `pushed_at IS NULL`
    的窗口内文章重新走一次发卡(标题带 ⏰补推,员工能分辨这是迟到的卡)。

    每轮监听开头先补上一轮的欠账,所以不需要额外的定时作业;窗口外的文章不再补
    (与铁律同界,免得翻旧账刷屏)。
    """
    settings = _base(settings)
    cutoff = datetime.now() - timedelta(hours=max(1, int(settings.wechat_repush_window_hours or 24)))
    limit = max(1, int(settings.wechat_repush_limit or 100))
    rows = list(session.scalars(select(WechatArticle).where(
        WechatArticle.user_id == user_id,
        WechatArticle.pushed_at.is_(None),
        WechatArticle.source.in_(("listen", "sync")),
        WechatArticle.created_at >= cutoff,
        # 发布时间也要在窗口内:「同步文章」被封顶窗口砍掉的 24h 之前的历史补采文
        # 按设计是"留给补转存队列、不再补推"的,只按入库时间筛会把它们翻出来刷屏。
        or_(WechatArticle.publish_at.is_(None), WechatArticle.publish_at >= cutoff),
    ).order_by(WechatArticle.id).limit(limit)).all())
    if not rows:
        return 0
    pushed = _push_listen(session, user_id, settings, rows, None, repush=True)
    if pushed:
        logger.warning("补推完成 user=%s:窗口内欠推 %d 篇,本次送达 %d 篇", user_id, len(rows), pushed)
    if pushed < len(rows):
        # 补推又没推完 = 飞书侧持续故障或积压超过单轮上限——必须点名,不能安静少推。
        # 而这种"飞书本身坏了"的告警发飞书更是发不出去(或被同一故障吞掉),只落站内。
        from app.services.alert_service import notify_incident

        notify_incident(
            session, user_id, "wechat",
            "⚠️ 公众号文章补推仍未送达,飞书推送可能持续故障",
            f"近 {settings.wechat_repush_window_hours} 小时内有 {len(rows)} 篇从未成功推送,"
            f"本轮补推只送达 {pushed} 篇(单轮上限 {limit} 篇)。"
            "请检查飞书机器人 webhook 是否被移除/关键词/IP 白名单拦截,或被封禁群。"
            "剩余欠推会在下一轮监听继续补,超过窗口后不再补。",
            settings=settings, push_feishu=False)
    return pushed
def _my_pan_link_from_history(my_pan_urls: str) -> tuple[str, str]:
    """从已持久化的 `my_pan_urls` 里取第一条可用我方链 → (干净 URL, 提取码),没有给 ("", "")。

    过去这里写死 `startswith("https://pan.quark.cn/s/")`,只认夸克;而百度转存成功落库的是
    `https://pan.baidu.com/s/xxx (提取码 abcd) [百度]` —— 于是**非本轮的卡片**(⏰补推、
    同步重推)上百度文一律退回公众号原文,网盘列却还标着盘商名,员工点开才发现是文章。
    用盘链正则取 URL 本体,顺带把 `(自分享)`/`[百度]`/提取码这些附属标记留在外面。
    """
    for line in (my_pan_urls or "").splitlines():
        m = _MY_LINK_RE.search(line)
        if m:
            c = re.search(r"提取码\s*([0-9A-Za-z]{4})", line)
            return m.group(0), (c.group(1) if c else "")
    return "", ""
def _push_listen(session: Session, user_id: int, settings: Settings, rows: list[WechatArticle],
                 replacements: dict[int, list[tuple[str, str, str]]] | None = None,
                 repush: bool = False) -> int:
    """新文推公众号专属飞书群(column_set 网格卡片:公众号/文章/网盘/阅读 四列对齐)。

    标题超链接优先级:本轮转存链(带提取码)> 已持久化的我的转存链 > 原文;
    未配专属群则回落总群;推送失败不影响采集结果。
    不设免打扰窗口:飞书是员工查看新发文的唯一入口(平台只有运营者可见),
    任何时段采到的文章都照常全量推送。

    返回值=成功送达的篇数。**送达的卡片里那些文章会盖上 `pushed_at`**,没盖上的
    (整卡发送失败/异常)就是"采到了却从没到过飞书",由 `repush_unpushed` 下一轮补推
    ——此前这类文章只留下一行 logger.exception,静默永久丢失(2026-09-26 第八轮审计)。
    `repush=True` 时卡标题带 ⏰补推,让员工/运营分得清这是迟到的那批。
    """
    from app.services.feishu import _col_set_row, _md_safe, webhook_for
    from app.services.feishu_client import FeishuClient

    wh = webhook_for(settings, "wechat")
    main_wh = settings.feishu_webhook
    targets = list(dict.fromkeys(filter(None, [wh, main_wh])))  # 去重保序
    if not targets:
        return 0
    replacements = replacements or {}
    # 免打扰过滤后可能清空(理论上上面已 return,这里再兜一层,避免推空卡)
    if not rows:
        return 0
    from collections import OrderedDict

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
            link, code = _my_pan_link_from_history(r.my_pan_urls)
            if link:
                my_link = True
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
        # 时效标注:发布日=今天不标(监听主打就是刚发的);带发布时间的旧文(补采/同步/
        # 迟到补推)标上日期——盘链时效短,员工该对"点开可能已失效"有预期
        if r.publish_at and r.publish_at.date() < datetime.now().date():
            shown += f" ·{r.publish_at:%m-%d}"
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
        elif "原文失效" in (r.my_pan_urls or ""):
            pan = "⛔原文失效"   # 原文被投诉下架(账号封禁/作者删除),链接是尸体别点
        elif (r.pan_urls or "").strip():
            pan = f"⏳待转存{types}"
        else:
            pan = types or "—"
        # 阅读数来源:微信读书 cover/列表自带站内阅读(免费,2026-09-30 起);
        # dajiala 采样摘除后 traffic_at 恒空,不能再当显示条件(否则永远显示"—")
        read = str(r.read_num) if r.read_num else "—"
        if r.traffic_at:
            read += "*"  # 有采样点的历史文章:标注为采样口径
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
                # LLM 输出同样是"外部可控文本":员工群卡片里不能原样塞 markdown 链接/
                # `<at>`(提示注入 → 钓鱼链、@所有人)。逐行走 _md_safe,保留换行排版。
                safe_reading = chr(10).join(_md_safe(line) for line in reading.splitlines())
                ai_elements = [
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md",
                        "content": "🤖 **AI 解读**" + chr(10) + safe_reading[:1500]}},
                ]
        except Exception:  # noqa: BLE001 - 叙事失败不影响推送
            logger.exception("LLM 叙事失败 user=%s", user_id)

    # 全量分页:每卡最多 20 篇(飞书卡片有体积上限),超出部分继续发卡而非"见平台列表"。
    per_card = 20
    chunks = [seq[i:i + per_card] for i in range(0, len(seq), per_card)]
    total_pages = len(chunks)
    delivered: list[int] = []   # 成功送达那一页所盖的 article id → 收尾统一落 pushed_at
    for page_idx, chunk in enumerate(chunks):
        elements: list[dict] = [
            {"tag": "note", "elements": [{"tag": "plain_text",
                "content": "点文章标题打开链接(优先你的夸克转存链) · 网盘列=识别到的盘链,"
                           "—=这篇没带网盘链(仍照常推) · 阅读未采样为 —"
                           " · 标题后 ·MM-DD=那天发的(旧文/补采,盘链可能已失效)"
                           " · ⛔=原文被投诉下架,别点"}]},
        ]
        if page_idx == 0:
            n_pan = sum(1 for r in rows if (r.pan_urls or "").strip())
            elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                "content": f"本轮共 {len(rows)} 篇新发文,来自 {len(groups)} 个公众号"
                           f"(按账号分组,全量推送),其中 {n_pan} 篇带网盘资源"}]})
        elements.append(_col_set_row(
            [("**公众号**", 3), ("**文章**", 7), ("**网盘**", 2), ("**阅读**", 2)], grey=True))
        last_author: str | None = None
        for author, r in chunk:
            if author != last_author:  # 换账号插入一行账号标题;账号跨卡时下一页重出标题
                elements.append({"tag": "div", "text": {"tag": "lark_md",
                    "content": f"**📢 {_md_safe(author)} · {len(groups[author])} 篇**"}})
                last_author = author
            elements.append(_render_article(r))
        if page_idx == 0 and ai_elements:
            elements.extend(ai_elements)
        card_title = f"📡 公众号监听 · 新发文 {len(rows)} 篇"
        if repush:
            # 补推卡必须自报身份:员工看到的是一张迟到的卡,不是"这轮又发了新的"
            card_title = "⏰ 补推 · " + card_title
        if total_pages > 1:
            card_title += f" · {page_idx + 1}/{total_pages}"
        sent_any = False
        for target in targets:
            try:
                if FeishuClient(target, settings.feishu_secret).send_card({
                    "config": {"wide_screen_mode": True},
                    "header": {"template": "blue", "title": {"tag": "plain_text",
                        "content": card_title}},
                    "elements": elements,
                }):
                    sent_any = True
            except Exception:  # noqa: BLE001 - 推送失败不影响采集结果
                logger.exception("公众号监听飞书推送失败 user=%s page=%s", user_id, page_idx)
        # 任一目标群收下即算这一页送达(专属群+总群双推是设计如此;补推时会整批重发,
        # 好的那个群可能再收一次——重复一张卡远好于员工永远没看到这篇)。
        if sent_any:
            delivered.extend(r.id for _, r in chunk)
    if delivered:
        session.execute(update(WechatArticle).where(WechatArticle.id.in_(delivered))
                        .values(pushed_at=datetime.now()))
        session.commit()
    return len(delivered)
