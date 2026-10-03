"""用户预警规则(见 doc/dev.md §5.10)。

每个板块(weibo/xianyu/douhot)都支持:
- `threshold`:某指标超过阈值即预警(growth/pct/delta/score/count);
- `new`:出现"新增"的项(关键词/商品/词)即告知;
- `fixed_time`:定时发送该板块总结(由调度器读取,见 alert_digest)。
关键词过滤 + 冷却防重复 + 邮件/AlertRecord 触达。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import AlertRecord, AlertRule, RunRecord, User
from app.db.tx import HeldSavepoint, savepoint
from app.services.notifier import get_notifier, get_user_notifier
from app.utils import get_logger

logger = get_logger(__name__)

SECTIONS = ("weibo", "xianyu", "douhot", "baidu")
RULE_TYPES = ("threshold", "new", "fixed_time")


def _key(item: dict) -> str:
    # 500 与 AlertRecord.keyword String(500) 对齐:热搜标题/关键词可能越界,
    # 若不截断,一批 pairs 中任一越界会让整批 AlertRecord 提交抛 1406,
    # 该轮所有已发出去的信都回滚不落表 → 冷却失效 → 下轮重复轰炸。
    return str(item.get("key") or item.get("item_id") or item.get("keyword") or item.get("title") or "")[:500]


def _normalize_alert_time(raw: str | None) -> str:
    """校验并归一化定时时间为 "HH:MM"(零填充 24 小时制)。

    调度器用 `AlertRule.alert_time == now.strftime("%H:%M")` 精确匹配,任何偏差
    ("9:00" 缺前导零、"0900"、全角冒号、越界)都会让规则**永不触发且无任何报错**。
    因此在写入边界统一归一化;无法解析则拒绝,交由 API 返回 400。
    """
    s = (raw or "").strip().replace("：", ":")
    parts = s.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError("定时时间需为 HH:MM(24 小时制),如 09:00")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("定时时间越界:小时 0-23、分钟 0-59")
    return f"{hh:02d}:{mm:02d}"


def add_rule(
    session: Session,
    user_id: int,
    section: str,
    rule_type: str,
    metric: str | None = None,
    threshold: float | None = None,
    keyword: str | None = None,
    alert_time: str | None = None,
) -> AlertRule:
    if section not in SECTIONS:
        raise ValueError("未知板块")
    if rule_type not in RULE_TYPES:
        raise ValueError("未知规则类型")
    if rule_type == "fixed_time":
        alert_time = _normalize_alert_time(alert_time)
    else:
        alert_time = None  # 非定时规则不存时间,避免残留脏值
    rule = AlertRule(
        user_id=user_id, section=section, rule_type=rule_type,
        metric=metric, threshold=threshold, keyword=(keyword or None),
        alert_time=(alert_time or None),
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def list_rules(session: Session, user_id: int) -> list[dict]:
    rules = session.scalars(select(AlertRule).where(AlertRule.user_id == user_id).order_by(AlertRule.id)).all()
    return [_rule_dict(r) for r in rules]


def _rule_dict(r: AlertRule) -> dict:
    return {
        "id": r.id,
        "section": r.section,
        "rule_type": r.rule_type,
        "metric": r.metric,
        "threshold": r.threshold,
        "keyword": r.keyword,
        "alert_time": r.alert_time,
        "enabled": r.enabled,
        "last_alert_at": r.last_alert_at.isoformat() if r.last_alert_at else None,
    }


def delete_rule(session: Session, user_id: int, rule_id: int) -> bool:
    rule = session.scalar(select(AlertRule).where(AlertRule.id == rule_id, AlertRule.user_id == user_id))
    if rule is None:
        return False
    session.delete(rule)
    session.commit()
    return True


def evaluate(
    session: Session,
    user_id: int,
    section: str,
    latest: list[dict],
    prev_keys: set[str],
    settings: Settings | None = None,
) -> int:
    """根据用户该板块的规则,对最新快照做"阈值/新增"判定并触达;返回触发条数。"""
    settings = settings or get_settings()
    user = session.get(User, user_id)
    notifier = get_user_notifier(user, settings) if user else get_notifier(settings)
    cooldown_hours = getattr(settings, "alert_cooldown_hours", 6)
    rules = session.scalars(
        select(AlertRule).where(
            AlertRule.user_id == user_id, AlertRule.section == section, AlertRule.enabled.is_(True)
        )
    ).all()

    triggered: list[tuple[AlertRule, dict]] = []
    now = datetime.now()
    for rule in rules:
        # 冷却:已有预警且仍在冷却期则跳过
        if rule.last_alert_at and (now - rule.last_alert_at).total_seconds() < cooldown_hours * 3600:
            continue
        for item in latest:
            key = _key(item)
            if rule.keyword and rule.keyword != key:
                continue
            if rule.rule_type == "new":
                if key and key not in prev_keys:
                    triggered.append((rule, item))
                    break
            elif rule.rule_type == "threshold":
                m = (rule.metric or "growth").lower()
                val = item.get(m)
                if val is None:
                    continue
                try:
                    if float(val) > float(rule.threshold or 0):
                        triggered.append((rule, item))
                        break
                except (TypeError, ValueError):
                    continue

    if not triggered:
        return 0

    # 同一规则下的多条合并为一条消息,避免刷屏。
    # AlertRecord 仅在发送成功后落库:与 last_alert_at 同款门控——发送失败既不
    # 留"幻影"历史(否则 /api/alerts/list 显示已预警却没收到),也不会在每轮重试
    # 时把同一预警重复堆进历史(冷却未生效,下轮仍会再触发)。
    per_rule: dict[int, list[tuple[str, str]]] = {}
    for rule, item in triggered:
        key = _key(item)
        m = (rule.metric or "growth")
        val = item.get(m)
        reason = f"{key} {m}={float(val):.2f} 跨阈值 {rule.threshold}" if val is not None else f"新增 {key}"
        per_rule.setdefault(rule.id, []).append((key, reason))

    delivered = 0
    for rule_id, pairs in per_rule.items():
        subject = f"[预警] {section} · 触发 {len(pairs)} 条"
        if not notifier.send(subject, "\n".join(reason for _, reason in pairs)):
            continue  # 发送失败:不落记录、不置 last_alert_at → 冷却不生效,下轮干净重试
        for key, reason in pairs:
            session.add(
                AlertRecord(user_id=user_id, section=section, keyword=key, reason=reason, triggered_at=now)
            )
        r = session.get(AlertRule, rule_id)
        if r:
            r.last_alert_at = now
        delivered += len(pairs)
    session.commit()
    if delivered:
        logger.info("预警触发 section=%s user=%s 条数=%s", section, user_id, delivered)
    return delivered


def _build_digest(session: Session, user_id: int, section: str, settings: Settings) -> str:
    """生成某板块的定时总结文本。"""
    from app.db.models import DouhotWord, WeiboTrend, XianyuItem

    lines = [f"【{section} 定时总结】"]
    if section == "weibo":
        rows = session.scalars(
            select(WeiboTrend).where(WeiboTrend.user_id == user_id, WeiboTrend.rising.is_(True))
            .order_by(WeiboTrend.growth.desc()).limit(10)
        ).all()
        lines += [f"- {r.keyword} 增长 {(r.growth or 0)*100:.1f}%" for r in rows] or ["- 暂无上涨趋势"]
    elif section == "xianyu":
        rows = session.scalars(
            select(XianyuItem).where(XianyuItem.user_id == user_id)
            .order_by(XianyuItem.hit_keywords.desc(), XianyuItem.best_rank.asc()).limit(10)
        ).all()
        lines += [f"- {r.title[:28]} {r.price} (x{r.hit_keywords})" for r in rows] or ["- 暂无热榜"]
    else:
        rows = session.scalars(
            select(DouhotWord).where(DouhotWord.user_id == user_id)
            .order_by(DouhotWord.score.desc()).limit(10)
        ).all()
        lines += [f"- {r.title} 飙升 {(r.score or 0)/1e4:.1f}万" for r in rows] or ["- 暂无内容词"]
    return "\n".join(lines)


def run_fixed_time_digests(db: Session | None = None, settings: Settings | None = None) -> int:
    """定时派发:遍历所有 enabled 的 fixed_time 规则,当前 HH:MM 命中则发该板块总结。

    供调度器每个分钟调用;当天同一规则只发一次。返回发送条数。
    `db`/`settings` 供测试注入(缺省各自建会话/读全局配置,行为不变)。
    """
    from app.db import get_session_local

    own_db = db is None
    settings = settings or get_settings()
    db = db or get_session_local()()
    try:
        now = datetime.now()
        hhmm = now.strftime("%H:%M")
        # 排除被管理员禁用(User.enabled=False)的用户:封禁即停推,与 due_schedules /
        # 周总结 / get_current_user 的处理一致。此前该作业直接扫 AlertRule,漏掉了这层门控,
        # 导致禁用用户的定时总结仍每天发出。用 notin_(禁用 id) 而非 INNER JOIN User,
        # 保留无主孤儿规则(其 user_id 不在 User 表中,不应被误伤)。
        disabled_ids = select(User.id).where(User.enabled.is_(False))
        rules = db.scalars(
            select(AlertRule).where(
                AlertRule.enabled.is_(True), AlertRule.rule_type == "fixed_time",
                AlertRule.alert_time == hhmm, AlertRule.user_id.notin_(disabled_ids)
            )
        ).all()
        sent = 0
        for rule in rules:
            if rule.last_alert_at and rule.last_alert_at.date() == now.date():
                continue  # 当天已发
            user = db.get(User, rule.user_id)
            notifier = get_user_notifier(user, settings) if user else get_notifier(settings)
            digest = _build_digest(db, rule.user_id, rule.section, settings)
            if not notifier.send(f"[{rule.section}] 定时总结 {hhmm}", digest):
                continue  # 发送失败不置 last_alert_at、不计数:与 evaluate 同款,避免静默丢当日总结
            rule.last_alert_at = now
            sent += 1
        db.commit()
        return sent
    finally:
        if own_db:
            db.close()


def feishu_alert_gate(db: Session, user_id: int, section: str, title: str,
                      cooldown_hours: float, reason: str = "") -> bool:
    """飞书告警冷却门(全项目共用):冷却期内返回 False;否则写入/刷新冷却记录并返回 True。

    调用方 gate 通过后再自行推送。title 建议含足够区分度(板块/链接/文章ID)。
    """
    from app.db.models import FeishuAlert

    now = datetime.now()
    existing = db.scalar(select(FeishuAlert).where(
        FeishuAlert.section == section, FeishuAlert.user_id == user_id, FeishuAlert.title == title[:200]))
    if existing and (now - existing.alerted_at) < timedelta(hours=cooldown_hours):
        return False
    # 冷却行写进 SAVEPOINT:并发撞唯一约束时只撤销这一行。原来是整段 db.rollback(),
    # 会把调用方尚未提交的业务数据一起抹掉(监听轮的中途告警正踩这条)。
    try:
        with savepoint(db):
            if existing:
                existing.reason, existing.alerted_at = (reason or existing.reason)[:255], now
            else:
                db.add(FeishuAlert(section=section, user_id=user_id, title=title[:200],
                                   reason=reason[:255], alerted_at=now))
            db.flush()  # 立即落库试探:并发双门同时 INSERT 会在此撞唯一约束
    except IntegrityError:
        row = db.scalar(select(FeishuAlert).where(
            FeishuAlert.section == section, FeishuAlert.user_id == user_id, FeishuAlert.title == title[:200]))
        # 并发方已抢到门(其 alerted_at 即为准):按冷却逻辑判定
        return not (row and (now - row.alerted_at) < timedelta(hours=cooldown_hours))
    return True


def notify_incident(db: Session, user_id: int, kind: str, title: str, detail: str,
                    settings: Settings | None = None, push_feishu: bool = True) -> bool:
    """事件级即时告警:默认推该板块飞书群,复用 feishu_alert_cooldown_hours 冷却去重。

    与 check_collect_failures 的"聚合计数"互补——滑块这类需要人工立刻介入的事件,
    不该等 24h 内凑满 3 次失败才响。返回是否实际推送。

    `push_feishu=False` 给的是**不需要人当场动手的运维诊断**(例:"微信读书只能拿到最新一篇"
    的漏推风险、"补推仍未送达"这种飞书自身故障):飞书群是员工看文章和 Cookie 提醒的入口,
    这类"要不要自建 WeRSS / 要不要充值"的长期决策项丢进来只是噪音。它们改落**站内告警**
    (`alerts` 表 → 预警页 `/api/alerts/list`、管理端用户详情、告警导出 CSV 都读它),
    冷却门照旧走,所以每 `FEISHU_ALERT_COOLDOWN_HOURS` 最多记一条,不会堆垃圾。
    """
    settings = settings or get_settings()
    from app.services.feishu_client import FeishuClient, webhook_for

    # 告警是运维信息,推**管理员群**(未配则回落原板块群)——
    # 客户内容群里不该混进"哪个采集挂了/哪个 Cookie 失效"(2026-10-01 受众分流)
    webhook = webhook_for(settings, "admin") if push_feishu else ""
    if push_feishu and not webhook:
        return False
    section, key = f"incident_{kind}", title[:80]
    # 冷却门写在 SAVEPOINT 里:发送失败要"不烧冷却期"时只撤销这一行。
    # 旧写法是整段 db.rollback()——监听轮第一条 commit 在收尾,一次飞书抖动
    # 就能把本轮已采到的新文章全部抹掉(2026-09-26 第八轮审计 High)。
    gate = HeldSavepoint(db)
    try:
        passed = feishu_alert_gate(db, user_id, section, key,
                                   settings.feishu_alert_cooldown_hours, detail)
    except Exception as exc:  # noqa: BLE001 - 冷却门写不进也不能伤及调用方数据
        logger.warning("告警冷却门写入失败,跳过本次告警 %s:%s", key, exc)
        gate.close(keep=False)
        return False
    if not passed:
        gate.close(keep=False)
        return False
    if not push_feishu:
        db.add(AlertRecord(user_id=user_id, section=f"incident_{kind}"[:32],
                           keyword=title[:500], reason=detail))
        gate.close(keep=True)
        db.commit()
        return False
    message = f"🔴 {title}" + chr(10) + detail
    # 送达日志+一次重试(关键事件告警不应因瞬时抖动丢失;送达结果可审计)
    sent, attempts, last_err = False, 0, ""
    for attempt in range(2):
        attempts = attempt + 1
        try:
            sent = FeishuClient(webhook, settings.feishu_secret).send(message)
            last_err = "" if sent else "send 返回 False"
        except Exception as exc:  # noqa: BLE001 - FeishuClient 常规不抛,防御性兜住
            last_err = f"{type(exc).__name__}: {exc}"[:250]
            sent = False
        if sent:
            break
        time.sleep(1.5)
    if not sent:
        # 失败不烧冷却期(送达日志一并不写,与旧行为一致),但**原因必须留痕**:
        # 此前 last_err 算完即丢,告警发不出去时运维只看得见"失败"、看不见"为什么"
        # —— 而这恰恰是最需要知道原因的时刻(2026-10-01 审查发现)。
        logger.warning("飞书推送失败(试了 %d 次):%s | %s", attempts, last_err or "未知", title[:60])
        gate.close(keep=False)
        return False
    from app.db.models import NotificationLog
    gate.close(keep=True)
    db.add(NotificationLog(user_id=user_id, channel="feishu", section=kind,
                           title=title[:255], ok=True, attempts=attempts, error=""))
    db.commit()  # 发送成功才落冷却门
    return True


def _last_success_days(db: Session, uid: int, kind: str) -> int | None:
    """该 (用户,平台) 距最近一次**成功**采集的天数;从未成功返回 None。用于判断"长期坏"升级。"""
    last_ok = db.scalar(select(func.max(RunRecord.started_at)).where(
        RunRecord.user_id == uid, RunRecord.kind == kind, RunRecord.status == "success"))
    if last_ok is None:
        return None
    return int((datetime.now() - last_ok).total_seconds() // 86400)


def check_collect_failures(settings: Settings | None = None, db: Session | None = None) -> int:
    """采集持续失败告警:某用户某板块近 24h 失败 >= 阈值,推送飞书并去重。

    解决"Cookie 过期/接口异常,只有日志没有主动提醒"的运维盲区。
    返回本次告警条数;未配置飞书 webhook 时跳过。`db` 供测试注入。
    """
    settings = settings or get_settings()
    from app.services.feishu_client import FeishuClient, platform_webhook, webhooks_for
    if not settings.feishu_webhook and not any(
            platform_webhook(settings, s) for s in ("weibo", "xianyu", "douhot", "baidu", "wechat")):
        return 0
    from sqlalchemy import func

    from app.db import get_session_local
    from app.db.models import FeishuAlert, RunRecord

    threshold = settings.fail_alert_threshold
    # 长期坏升级阈值(与 check_health_stalls 同源):超过 N 天未成功 → 标注【长期】
    escalate_days = getattr(settings, "health_escalate_days", 3) or 3
    since = datetime.now() - timedelta(hours=24)
    own_session = db is None
    db = db or get_session_local()()
    sent = 0
    try:
        # 仅当某 (用户,板块) **最近一次运行仍失败**(当前仍断)才告警;
        # 否则中途重试成功过,不算"持续失败"(否则会误报)。
        latest = select(RunRecord.user_id, RunRecord.kind, func.max(RunRecord.id).label("mid")).group_by(
            RunRecord.user_id, RunRecord.kind
        ).subquery()
        latest_rows = db.execute(
            select(latest.c.user_id, latest.c.kind, RunRecord.status, RunRecord.detail).join(
                RunRecord, RunRecord.id == latest.c.mid)
        ).all()

        def _broken(status: str, detail: str) -> bool:
            # failed 之外,verify 痕迹的 skipped/verify_cooldown 与 partial 也算"当前仍断":
            # 闲鱼滑块后 30 分钟内的轮次记 skipped,若只认 failed 会漏掉正在发生的风控
            if status == "failed":
                return True
            d = detail or ""
            return status in ("skipped", "partial") and ("XianyuVerify" in d or "verify" in d.lower())

        currently_broken = {(uid, kind) for uid, kind, status, detail in latest_rows if _broken(status, detail)}

        rows = db.execute(
            select(RunRecord.user_id, RunRecord.kind, func.count(RunRecord.id))
            .where(RunRecord.status == "failed", RunRecord.started_at >= since)
            .group_by(RunRecord.user_id, RunRecord.kind)
        ).all()
        hits = []
        for uid, kind, cnt in rows:
            if (uid, kind) not in currently_broken or cnt < threshold:
                continue
            existing = db.scalar(
                select(FeishuAlert).where(
                    FeishuAlert.section == "collect_fail", FeishuAlert.user_id == uid, FeishuAlert.title == kind
                )
            )
            if existing and (datetime.now() - existing.alerted_at).total_seconds() < settings.feishu_alert_cooldown_hours * 3600:
                continue
            hits.append((uid, kind, cnt))
            if existing:
                existing.reason, existing.alerted_at = f"近24h失败{cnt}次", datetime.now()
            else:
                db.add(FeishuAlert(section="collect_fail", user_id=uid, title=kind, reason=f"近24h失败{cnt}次"))
        # 恢复确认(2026-09-30,对齐闲鱼"✅已恢复"惯例):之前告过警、现在已不在
        # "仍断"名单的 (用户,板块) → 发 ✅ 并清冷却。否则"失败 N 次"的旧告警
        # 挂在群里成孤魂,恢复后用户看到旧告警+新数据并存会误判还在坏(实测困惑×2)。
        recovered = []
        for (uid, kind) in sorted(currently_broken ^ {(u, k) for u, k, _ in rows}):
            row = db.scalar(select(FeishuAlert).where(
                FeishuAlert.section == "collect_fail", FeishuAlert.user_id == uid,
                FeishuAlert.title == kind))
            if row:
                recovered.append((uid, kind))
        if recovered and not hits:
            _ok = 0
            for uid, kind in recovered:
                for wh in webhooks_for(settings, kind):
                    if FeishuClient(wh, settings.feishu_secret).send(
                            f"✅ 采集已恢复 · 用户#{uid} 板块[{kind}]——此前「持续失败」告警作废,以本条为准"):
                        _ok += 1
                # ⚠️ **必须按这对 (uid, kind) 重新查,不能沿用上面扫描循环遗留的 `row`**
                # (2026-10-04 修,由作业心跳抓出):
                # 那个 `row` 最后一次赋值可能是 **None**(最后一对恰好没有告警行),
                # 于是 `db.delete(None)` 抛 `UnmappedInstanceError: Class 'builtins.NoneType'
                # is not mapped` —— **整个 `check_collect_failures` 当场崩掉**,
                # 也就是"采集失败告警"这条链**根本没在工作**(而崩溃发生在最常见的分支:
                # 有恢复、当前没坏)。多对恢复时它还会反复删同一行。历史遗留变量是祸根。
                stale = db.scalar(select(FeishuAlert).where(
                    FeishuAlert.section == "collect_fail", FeishuAlert.user_id == uid,
                    FeishuAlert.title == kind))
                if stale is not None:
                    db.delete(stale)
            if _ok:
                db.commit()
                logger.info("采集恢复确认推送,条数=%s", _ok)

        if hits:
            sent = 0
            for uid, kind, cnt in hits:
                # 带上最近一次失败的具体原因(如"XianyuVerify: 闲鱼人机验证(滑块),需人工处理"),
                # 让运维一眼知道该做什么(人工过滑块/换出口 IP),而非只看到"失败 N 次"。
                detail = db.scalar(
                    select(RunRecord.detail)
                    .where(RunRecord.user_id == uid, RunRecord.kind == kind, RunRecord.status == "failed")
                    .order_by(RunRecord.id.desc())
                    .limit(1)
                ) or ""
                extra = f"  {detail}" if detail else ""
                # 长期坏升级:距最近一次成功超过 N 天 → 标注,区分偶发与长期
                days = _last_success_days(db, uid, kind)
                long = days is not None and days >= escalate_days
                long_txt = f"  【长期】已 {days} 天未成功,建议人工处理" if long else ""
                head = "🔴 " if long else "⚠️ "
                # 带评估时间戳(2026-09-30):历史消息与新消息并存时,一眼分清新旧——
                # 用户三次把凌晨/上午的旧告警当成新告警发回来问(误判恢复状态)
                msg = (f"{head}采集持续失败(近24h) · 评估于 {datetime.now():%m-%d %H:%M} · "
                       f"用户#{uid} 板块[{kind}] 失败 {cnt} 次{extra}{long_txt}")
                # 按板块路由:主群 + 该平台专属群 都发(互不替代)
                for wh in webhooks_for(settings, kind):
                    if FeishuClient(wh, settings.feishu_secret).send(msg):
                        sent += 1
            # 全部发送失败 → 丢弃冷却门(否则飞书故障期"持续失败"告警被静默一个冷却窗)
            if sent:
                db.commit()
            else:
                db.rollback()
            logger.info("采集失败告警推送,条数=%s", sent)
        return sent
    finally:
        if own_session:
            db.close()


def check_health_stalls(settings: Settings | None = None, db: Session | None = None) -> int:
    """采集停摆告警:已配 Cookie(在用)的平台超过 `health_stall_hours` 无新数据写入 → 推飞书。

    补 `check_collect_failures` 盲区——它只看"失败运行",漏掉**静默停摆**(后端宕机/调度停/
    被风控全挡却未记为失败,如磁盘满导致整站 502)。返回告警条数;未配 webhook 返回 0。`db` 供测试注入。
    """
    settings = settings or get_settings()
    from app.services.feishu_client import FeishuClient, platform_webhook, webhooks_for
    if not settings.feishu_webhook and not any(
            platform_webhook(settings, s) for s in ("weibo", "xianyu", "douhot", "baidu", "wechat")):
        return 0
    from sqlalchemy import func

    from app.db import get_session_local
    from app.db.models import BaiduHotItem, DouhotWord, FeishuAlert, UserCookie, WeiboHotItem, XianyuItem

    own_session = db is None
    db = db or get_session_local()()
    stall_hours = getattr(settings, "health_stall_hours", 24) or 24
    since = datetime.now() - timedelta(hours=stall_hours)
    labels = {"weibo": "微博", "xianyu": "闲鱼", "douhot": "抖音", "baidu": "百度"}
    data_tables = {
        "weibo": (WeiboHotItem, "captured_at"),
        "baidu": (BaiduHotItem, "captured_at"),
        "xianyu": (XianyuItem, "created_at"),
        "douhot": (DouhotWord, "created_at"),
    }
    # 在用平台 = 有启用用户配了该平台 Cookie **且该板块有启用中的调度**(否则不告警)。
    # Cookie 平台名与数据平台名不同(goofish→xianyu,douyin→douhot),需映射。
    # 2026-09-30 补调度开关:双部署分工后本机关闭的板块(如 douhot 归远程)没有新数据
    # 是**预期行为**——只按"配了 Cookie"判定,归远程的板块会被误报停摆(实测)。
    from app.db.models import UserSchedule

    cookie_to_data = {"weibo": "weibo", "baidu": "baidu", "douyin": "douhot", "goofish": "xianyu"}
    data_to_cookie = {v: k for k, v in cookie_to_data.items()}
    raw = set(db.execute(
        select(UserCookie.platform).join(User, User.id == UserCookie.user_id).where(User.enabled.is_(True)).distinct()
    ).scalars().all())
    sched_on = set(db.execute(
        select(UserSchedule.section).join(User, User.id == UserSchedule.user_id)
        .where(User.enabled.is_(True), UserSchedule.enabled.is_(True)).distinct()
    ).scalars().all())
    in_use = {cookie_to_data.get(c, c) for c in raw} & sched_on
    stalled = []
    for p, (model, col) in data_tables.items():
        if p not in in_use:
            continue
        latest = db.execute(select(func.max(getattr(model, col)))).scalar()
        if latest is None or latest < since:
            # 记一个"在用该平台的首个启用用户"作为告警归属,便于去重。
            # 必须 order_by:该 uid 同时是 FeishuAlert 去重键,无确定序时 limit(1)
            # 在不同作业间可能返回不同行 → uid 漂移,同一停摆事件重复推给操作员。
            uid = db.scalar(select(User.id).join(UserCookie, UserCookie.user_id == User.id).where(
                User.enabled.is_(True), UserCookie.platform == data_to_cookie[p]).order_by(User.id).limit(1))
            stalled.append((p, latest, uid))
    stalled = [s for s in stalled if s[2] is not None]
    if not stalled:
        if own_session:
            db.close()
        return 0

    cooldown = settings.feishu_alert_cooldown_hours * 3600
    now = datetime.now()
    items = []
    for p, latest, uid in stalled:
        existing = db.scalar(select(FeishuAlert).where(
            FeishuAlert.section == "health_stall", FeishuAlert.title == p, FeishuAlert.user_id == uid))
        if existing and (now - existing.alerted_at).total_seconds() < cooldown:
            continue
        items.append((p, latest))
        if existing:
            existing.reason, existing.alerted_at = f"无数据流入({stall_hours}h)", now
        else:
            db.add(FeishuAlert(user_id=uid, section="health_stall", title=p, reason=f"无数据流入({stall_hours}h)"))
    sent = 0
    if items:
        escalate_days = getattr(settings, "health_escalate_days", 3) or 3
        ok = True
        for p, latest in items:
            when = latest.strftime("%m-%d %H:%M") if latest else "从未进数据"
            long_txt = ""
            long = False
            if latest:
                days = int((now - latest).total_seconds() // 86400)
                if days >= escalate_days:
                    long = True
                    long_txt = f"  【长期】已 {days} 天,建议人工排查"
            head = (f"🔴 采集停摆(> {stall_hours}h 无新数据,已长期)" if long
                    else f"⚠️ 采集停摆(> {stall_hours}h 无新数据)")
            lines = [head, f"  · {labels.get(p, p)}:最近数据 {when}{long_txt}",
                     "可能:后端宕机/调度停止/Cookie失效/被风控全挡(闲鱼常见滑块/限流)"]
            # 按板块路由:主群 + 该平台专属群 都发(互不替代),任一送达即算成功
            whs = webhooks_for(settings, p)
            msg = "\n".join(lines)
            delivered = False
            for wh in whs:
                if FeishuClient(wh, settings.feishu_secret).send(msg):
                    delivered = True
            if delivered:
                sent += 1
            else:
                ok = False
        if ok:
            db.commit()
            logger.info("采集停摆告警推送,条数=%s", sent)
        else:
            db.rollback()  # 有发送失败则全部回滚,下次再试
    if own_session:
        db.close()
    return sent
