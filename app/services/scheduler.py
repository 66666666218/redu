"""后台调度器(见 doc/dev.md §6)。

与旧的"每板块一个全局 Cron"不同,这里**每分钟 tick 一次**,从
`user_schedules` 表取出到期的 (用户, 板块) 逐个执行——因为频率是每个用户
自己设的,不再有统一周期。用户改频率下一分钟即生效。

随 FastAPI 应用一起启动(`app/platform.py` 的 lifespan),因此单容器部署即可,
不需要额外的调度容器。`python -m app.main` 的独立调度模式复用同一套 tick 逻辑。
"""
from __future__ import annotations

import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import Settings, get_settings
from app.services import schedule_service
from app.utils import get_logger

logger = get_logger(__name__)

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def _runners() -> dict:
    """延迟导入,避免 `tenant` ←→ 调度器的循环导入。"""
    from app.services.tenant import run_baidu, run_douhot, run_weibo, run_xianyu
    from app.services.wechat_monitor import run_wechat_listen

    return {"weibo": run_weibo, "xianyu": run_xianyu, "douhot": run_douhot, "baidu": run_baidu,
            "wechat": run_wechat_listen}


# **板块归哪一侧跑**(与 `doc/operations.md` §4j 的分工一致):
#   wechat 侧 = 公众号 + 闲鱼   /   hotspot 侧 = 微博/抖音/百度
#
# ⚠️ 为什么光有 `SCHEDULER_ROLE` 还不够:`collect_tick` 本身是 **`both` 角色(两端都跑)**,
# 它的防重复靠 `claim_schedule` 原子抢占 —— 而**两台实例的数据库是独立的**,各自抢占都会成功
# → 同一个上游账号会被两端同时采集。闲鱼实测"哎哟喂,被挤爆啦"(**账号级**限流:换出口
# 也没用、请求量只有 6 次/小时)高度疑似与此有关。所以这里**再按板块分一次侧**:
# 本实例不分管的板块直接跳过(不打上游、不消耗配额)。
_SECTION_ROLE = {"wechat": "wechat", "xianyu": "wechat",
                 "weibo": "hotspot", "douhot": "hotspot", "baidu": "hotspot"}


def _section_allowed(section: str, settings: Settings) -> bool:
    """本实例该不该采这个板块(未登记的板块不拦,向后兼容)。"""
    want = _SECTION_ROLE.get(section)
    if not want:
        return True
    cur = (getattr(settings, "scheduler_role", "all") or "all").strip().lower()
    return cur in ("all", "both") or cur == want


def collect_tick(settings: Settings | None = None, now: datetime | None = None) -> dict:
    """每分钟执行一次:把到期的 (用户, 板块) 采集跑掉。

    单条失败不影响其余;失败也会记为"已执行"(见 `schedule_service.mark_ran`),
    失败重试由 `retry_failed_runs` 负责。
    """
    from app.db import get_session_local

    settings = settings or get_settings()
    runners = _runners()
    now = now or datetime.now()
    db = get_session_local()()
    ok = failed = skipped = 0
    try:
        schedule_service.ensure_all_users(db)
        due = schedule_service.due_schedules(db, now)
        for row in due:
            if row.section == "wechat":
                continue  # 公众号监听由 wechat_collect_tick 独立作业处理(长任务,勿阻塞其它板块)
            if not _section_allowed(row.section, settings):
                skipped += 1
                logger.debug("跳过不属于本实例的板块:%s(角色=%s)", row.section,
                             getattr(settings, "scheduler_role", "all"))
                continue
            runner = runners.get(row.section)
            if runner is None:
                continue
            # 缺 Cookie 就跳过且**不标记**:用户配好 Cookie 后下一分钟即可开跑,
            # 也不会每个周期刷一条"未配置 Cookie"的失败记录。
            if schedule_service.missing_cookie(db, row.user_id, row.section):
                skipped += 1
                continue
            # 原子抢占(执行前置标记):多进程/双模式部署时防止同一到期任务被重复执行
            if not schedule_service.claim_schedule(db, row, now):
                skipped += 1
                continue
            try:
                runner(db, row.user_id, settings)
                ok += 1
                # 采集成功后触发飞书实时提醒(新增/飙升话题立即推送到群里)。
                # 异步、失败不影响本轮采集结果。
                if row.section != "wechat":  # 公众号监听的新文推送在 run_wechat_listen 内部完成
                    try:
                        from app.services.cross_platform import run_cross_platform_alert
                        from app.services.feishu import run_feishu_keyword_alerts, run_feishu_keyword_realtime, run_feishu_realtime

                        run_feishu_realtime(row.section, row.user_id, settings)
                        if row.section == "douhot":
                            run_feishu_keyword_alerts(row.user_id, settings)
                            run_feishu_keyword_realtime(row.user_id, settings)  # 话题词新进/上升/爆发实时提醒
                        run_cross_platform_alert(row.user_id, settings)  # ≥2板块上升的关键词
                        from app.services.focus_alert import run_focus_alert
                        run_focus_alert(db, row.user_id, settings)  # 跨板块共振/板块内反复 → 🔴重点
                        from app.services.early_agent import agent_tick
                        agent_tick(db, row.user_id, settings)  # 早期苗头评分(增速/新上榜/共振融合)
                    except Exception:  # noqa: BLE001
                        db.rollback()  # 半提交的告警冷却/Agent状态行一并丢弃,勿带脏session进 mark_ran
                        logger.exception("飞书实时提醒失败 section=%s user=%s", row.section, row.user_id)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                db.rollback()
                logger.warning("定时采集失败 用户=%s 板块=%s:%s", row.user_id, row.section, exc)
    finally:
        db.close()
    if ok or failed:
        logger.info("采集 tick 完成:执行=%s 成功=%s 失败=%s 跳过=%s", ok + failed, ok, failed, skipped)
    return {"due": ok + failed, "ok": ok, "failed": failed, "skipped": skipped}


def _beat(job_id: str, ok: bool, err: str = "") -> None:
    """写一条**作业心跳** —— 让"这个作业到底跑没跑"有据可查(见 `JobHeartbeat`)。

    ⚠️ **心跳失败绝不能影响作业本身**:它只是"顺手记一笔",DB 被锁/表缺失都得咽下去。
    """
    try:
        from sqlalchemy import select

        from app.db.database import get_session_local
        from app.db.models import JobHeartbeat

        now = datetime.now()
        with get_session_local()() as db:
            row = db.scalar(select(JobHeartbeat).where(JobHeartbeat.job_id == job_id))
            if row is None:
                row = JobHeartbeat(job_id=job_id)
                db.add(row)
            row.last_run_at = now
            row.run_count = (row.run_count or 0) + 1
            if ok:
                row.last_ok_at = now
            else:
                row.error_count = (row.error_count or 0) + 1
                row.last_error = (err or "")[:255]
            db.commit()
    except Exception:  # noqa: BLE001 - 见上:记不上就算了,不能连带作业
        logger.warning("作业心跳写入失败:%s", job_id)


def _safe(func, job_id: str = ""):  # type: ignore[no-untyped-def]
    """包裹调度作业:异常只记日志,不杀死调度器;**并落一条心跳**。"""

    def wrapper() -> None:
        ok, err = True, ""
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            ok = False
            err = f"{type(exc).__name__}: {exc}"
            logger.exception("调度作业执行失败:%s", getattr(func, "__name__", func))
        if job_id:
            _beat(job_id, ok, err)

    wrapper.__name__ = getattr(func, "__name__", "job")
    return wrapper


def _cron_trigger(expr: str, default: dict) -> CronTrigger:
    """解析 5 段 Cron 为 APScheduler 触发;非法时回退到 default(避免死作业)。

    ⚠️ 星期几字段必须做 **POSIX → APScheduler** 换算:标准 cron 里 0=周日,而
    APScheduler 的 `day_of_week` 是 **0=周一**(0 周一,1 周二, …, 6 周日)。
    不换算会把"0 9 * * 1"(想周一)跑成周二、"0 20 * * 0"(想周日)跑成周一。
    """
    try:
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"需 5 段,收到 {len(parts)} 段")
        # 星期几:数字按 POSIX(0=Sun..6=Sat)→ APScheduler(0=Mon..6=Sun) 换算。
        # 单字符之外的 `1,5` / `1-5` / `1-5/2` 也要换算——否则整体错位一天却无报错
        # (POSIX 1=Mon,APScheduler 1=Tue;`0 20 * * 1,5` 想周一/周五,实跑周二/周六)。
        # 只处理纯数字端点(名称 mon/tue 等保持原样);步进 /N 与步长段保留。
        def _conv_dow_field(token: str) -> str:
            def _conv_num(s: str) -> str | None:
                if s.isdigit() and len(s) == 1:
                    return str((int(s) + 6) % 7)
                return None

            out_parts = []
            for piece in token.split(","):
                step = ""
                if "/" in piece:
                    piece, _, step_str = piece.partition("/")
                    step = "/" + step_str
                if "-" in piece:
                    lo, _, hi = piece.partition("-")
                    nlo, nhi = _conv_num(lo), _conv_num(hi)
                    if nlo is not None and nhi is not None:
                        out_parts.append(f"{nlo}-{nhi}{step}")
                    else:
                        out_parts.append(piece + step)
                else:
                    n = _conv_num(piece)
                    out_parts.append((n if n is not None else piece) + step)
            return ",".join(out_parts)

        parts[4] = _conv_dow_field(parts[4])
        cron_kw = dict(zip(("minute", "hour", "day", "month", "day_of_week"), parts))
        return CronTrigger(**cron_kw)
    except Exception:  # noqa: BLE001
        return CronTrigger(**default)


def douhot_window_tick(settings: Settings | None = None) -> dict:
    """抖音关键词多窗口对比专用 tick(每 DOUHOT_WINDOW_CRON 一次)。

    独立于 collect_tick:遍历全部用户 + 逐词打热点宝接口(每词 ≥2 次查询),
    与榜单采集错峰,避免抢占;命中爆发/新起/回落 → 推飞书。
    """
    from app.db import get_session_local
    from app.db.models import User
    from app.services.douhot_window import analytics, collect_windows, run_feishu
    from sqlalchemy import select

    settings = settings or get_settings()
    db = get_session_local()()
    users_ok = pushed = 0
    try:
        for uid in db.scalars(
            select(User.id).where(User.enabled.is_(True)).order_by(User.id)
        ).all():
            try:
                out = collect_windows(db, uid, settings=settings)
                if out.get("status") == "success" and out.get("ok"):
                    users_ok += 1
                    pushed += run_feishu(db, uid, settings)
                    # 爆发 → 即时联动拉新方案(不必等 09:10/14:10/20:40 的定时 Agent,见 push_timeline):
                    # 多窗口对比检出 burst 的话题,立刻给「发什么货/标题/人群/转存钩子」,
                    # 站内推送(爆发卡片已在飞书,方案落站内,口径 2026-09-27)
                    burst_rows = [r for r in analytics(db, uid, settings=settings)
                      if r.get("signal") == "burst"]
                    topics = [(r.get("entry_title") or r.get("keyword") or "").strip()
                              for r in burst_rows]
                    topics = [t for t in topics if t][:3]
                    if topics:
                        from app.services import alert_service
                        from app.services.hotspot_agent import burst_plan
                        plan = burst_plan(db, uid, topics, settings)
                        if plan:
                            alert_service.notify_incident(
                                db=db, user_id=uid, kind="agent",
                                title=f"⚡ 爆发话题拉新方案:{topics[0][:24]}"
                                      + (f" 等{len(topics)}个" if len(topics) > 1 else ""),
                                detail=plan, settings=settings, push_feishu=False)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("抖音多窗口对比失败 user=%s", uid)
    finally:
        db.close()
    if users_ok or pushed:
        logger.info("抖音多窗口对比完成:用户=%s 推送=%s", users_ok, pushed)
    return {"users": users_ok, "pushed": pushed}


def wechat_collect_tick(settings: Settings | None = None) -> dict:
    """公众号监听专用 tick(每分钟,独立于 collect_tick)。

    为什么独立:一轮监听(平台分页+正文抓取+采样+转存)可能持续数分钟,
    与四个板块共用串行循环会互相阻塞。独立作业 + max_instances=1,
    监听再慢也不拖累微博/闲鱼/抖音/百度,反之亦然。
    """
    from app.db import get_session_local
    from app.services import schedule_service
    from app.services.wechat_monitor import run_wechat_listen

    settings = settings or get_settings()
    db = get_session_local()()
    ok = failed = skipped = 0
    try:
        schedule_service.ensure_all_users(db)
        now = datetime.now()
        # 四定点作业:候选集必须无视用户间隔直接取所有启用的 wechat 设置。
        # 早期用 due_schedules(按 interval_minutes 过滤)导致间隔 ≥360min 的用户
        # 在 18:00 等"距上次不足间隔"的定点静默丢轮(2026-09-22 审计)。
        due = schedule_service.enabled_section_schedules(db, "wechat")
        for row in due:
            # 原子抢占:API 内嵌调度器与独立调度进程双跑时防重复监听
            # force=True: 四定点作业自身即调度,不受用户间隔约束
            if not schedule_service.claim_schedule(db, row, now, force=True):
                skipped += 1
                continue
            try:
                # renewal 换出可用 Cookie 后的会话初期窗口:全量补采停更文章
                from app.services.wechat_monitor import run_full_sync_if_pending
                run_full_sync_if_pending(db, row.user_id, settings)
                # 不限数量:每轮监控全部对标号(用户决策:发现时效优先;
                # 微信读书客户端内置 2s 限速,32 号约 100s/轮,由 max_instances=1 串行防重叠)
                run_wechat_listen(db, row.user_id, settings=settings,
                                  batch_index=None, batch_size=None)
                from app.services.focus_alert import run_focus_alert
                run_focus_alert(db, row.user_id, settings)  # 公众号新文参与共振/反复检测
                ok += 1
            except Exception as exc:  # noqa: BLE001 - 单用户失败不影响其余
                failed += 1
                db.rollback()
                logger.warning("公众号监听失败 用户=%s:%s", row.user_id, exc)
    finally:
        db.close()
    if ok or failed:
        logger.info("公众号监听 tick 完成:成功=%s 失败=%s", ok, failed)
    return {"ok": ok, "failed": failed, "skipped": skipped}


def _event_assign() -> None:
    """事件归属(全用户):Hotspot → Event 聚类。"""
    from app.services import events as events_svc

    events_svc.event_assign_all_users()


def _member_renewal() -> None:
    """会员续费检查(全用户):到期该收/超期该踢 → 飞书提醒运营者。"""
    from app.db import get_session_local
    from app.services import members as members_svc

    db = get_session_local()()
    try:
        members_svc.renewal_tick(db=db)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("会员续费检查失败")
    finally:
        db.close()


def _agent_learn_all() -> None:
    """苗头回测+权重自适应(全用户)。"""
    from sqlalchemy import select

    from app.db import get_session_local
    from app.services.agent_learning import backtest_and_learn
    from config.settings import get_settings as _gs

    settings = _gs()
    db = get_session_local()()
    try:
        from app.db.models import User
        for uid in db.scalars(select(User.id).where(User.enabled.is_(True))).all():
            try:
                backtest_and_learn(db, uid, settings)
            except Exception:  # noqa: BLE001
                db.rollback()
    finally:
        db.close()


def _role_allows(job_role: str) -> bool:
    """本实例该不该跑这一类作业(见 `settings.scheduler_role`)。

    分体部署时:本机设 `wechat`(公众号 + 闲鱼侧)、远程设 `hotspot`(热点侧),各自只跑
    自己那一侧 —— 否则两边都推飞书、都打采集,既重复又互抢额度;本机的热点数据源早已
    停用,跑热点作业纯属拿旧数据空转。

    `both` 标记的是**中性作业**:它们各按自己库里的数据行事(采集调度、告警、清理),
    两边跑不会互相干扰,反而是各自实例该做的事。
    """
    cur = (getattr(get_settings(), "scheduler_role", "all") or "all").strip().lower()
    return cur == "all" or job_role == "both" or cur == job_role


# 错过触发后的宽限期(秒)。APScheduler 默认为 **1 秒** —— 差一点点就直接**丢弃**这次执行。
# 而本机是**笔记本/台式常驻**、看门狗还会重启(实测 6 天重启 125 次):只要触发那一刻
# 机器在休眠唤醒、进程在重启、或调度线程被占住,那 1 秒就过去了,作业**静默不跑**。
# 项目里 `wechat_collect_tick` 早就因此显式设了 3600(注释写着"防止休眠/重启错过窗口"),
# 但**其余定点作业没设** —— 实测 `resource_presence`(每天 09:00)因此**六天一次没跑**,
# 而它隔壁的 `wechat_collect_tick`(08:00,有 3600)天天正常,证据链正指向这里。
# 所以改成**统一兜底**:调用方显式传的仍然优先(`setdefault`)。
_DEFAULT_MISFIRE_GRACE = 3600


def _add_job(scheduler: BackgroundScheduler, func, trigger, job_id: str,
             role: str = "both", **kw) -> bool:
    """按角色登记作业;被角色挡下的**不登记也不报错**(这是预期行为,不是失败)。

    返回是否真的登记了,方便调用方/测试断言。
    """
    if not _role_allows(role):
        logger.info("实例角色 %s 跳过作业 %s(%s 侧)",
                    getattr(get_settings(), "scheduler_role", "all"), job_id, role)
        return False
    kw.setdefault("misfire_grace_time", _DEFAULT_MISFIRE_GRACE)   # 见上:别用 1 秒默认值
    scheduler.add_job(_safe(func, job_id), trigger, id=job_id, max_instances=1, coalesce=True, **kw)
    return True


def build_jobs(scheduler: BackgroundScheduler) -> None:
    """注册后台作业:按用户频率采集、定时告警摘要、失败自动重试、飞书日报/周报、邮件周报。"""
    from app.admin import retry_failed_runs
    from app.services.alert_service import check_collect_failures, check_health_stalls, run_fixed_time_digests
    from config.settings import get_settings as _get_settings

    _add_job(scheduler, collect_tick, CronTrigger(minute="*"), "collect_tick", "both")
    # 公众号监听四定点(用户决策 2026-09-22 调整):4:00/8:00/14:00/20:00——
    # 白天工作时段与夜间各覆盖一次,相邻间隔 4~6h 更均匀。
    # 原"每分钟检查+用户频率调度"改为纯定点——每轮全量 81 号约 3 分钟,
    # 每天仅 4 次主动请求,最大限度降低微信读书风控压力(Cookie 生命周期优先)。
    # misfire_grace_time=3600:定点错过后 1 小时内仍补跑(防止休眠/重启错过窗口)。
    from app.services.schedule_service import WECHAT_LISTEN_HOURS
    _add_job(scheduler, wechat_collect_tick,
             CronTrigger(hour=",".join(str(h) for h in sorted(WECHAT_LISTEN_HOURS)), minute="0"),
             "wechat_collect_tick", "wechat", misfire_grace_time=3600)
    _add_job(scheduler, run_fixed_time_digests, CronTrigger(minute="*"), "alert_fixed_time", "both")
    for func, job_id in ((retry_failed_runs, "auto_retry_failed_runs"), (check_collect_failures, "collect_failed_alert"),
                         (check_health_stalls, "health_stall_alert")):
        _add_job(scheduler, func, CronTrigger(minute="*/30"), job_id, "both")
    # 数据保留治理:每天删除超过各自窗口的旧快照/运行/日志。
    # ⚠️ **03:10,不是 04:00**(2026-10-03 改):04:00 与 `wechat_collect_tick`(4/8/14/20 定点,
    # 网络长任务)**同分同秒**,而这里要跑跨 ~22 张表的 DELETE + 23MB SQLite 全量快照拷贝 ——
    # 两者都在压 SQLite 写锁(库是单写者),是全天最挤的一处。错开 50 分钟。
    from app.db.maintenance import cleanup_old_data

    _add_job(scheduler, cleanup_old_data, CronTrigger(hour=3, minute=10), "data_cleanup", "both")
    from app.services.early_agent import agent_tick_all_users
    from app.services.hotspot_agent import settle_suggestions_all_users
    from app.services.wechat_monitor import (candidate_discover_tick, candidate_import_tick,
                                              keyword_article_all_users,
                                              pan_cookie_keepalive_tick, weread_refresh_tick)
    from app.services.hot_sources import hot_source_tick_all_users
    from app.services.wechat_monitor import retire_dormant_tick_all_users
    from app.services.wechat_digest import run_wechat_digest_all_users
    from app.services.remote_sync import remote_sync_tick
    from app.services.chain_health import chain_report_tick
    from app.services.push_timeline import tick as push_timeline_tick
    from app.services.telegram_source import collect_tick as tg_collect_tick
    from app.services.cross_accounts import cross_account_tick
    from app.services.douyin_leads import douyin_leads_tick
    from app.services.xunlei_sync import xunlei_sync_tick
    from app.services.xunlei_group import xunlei_group_tick
    from app.services.pan_discovery import pan_discovery_tick
    from app.services.health import health_push_tick
    from app.services.disk_guard import disk_guard_tick
    from app.services.resource_presence import presence_tick
    from app.services.xunlei_cleanup import cleanup_tick as xunlei_cleanup_tick
    from app.services.xunlei_cleanup import dedupe_tick as xunlei_dedupe_tick
    from app.services.lead_settlement import record_reminder_tick as recruit_reminder_tick

    jobs = [
        # 阅读量采样(traffic_tick)已停用:2026-09-29 用户决策放弃 dajiala(不充值),
        # read_zan_pro 为其付费接口,免费源(微信读书/WeRSS)无阅读数字段。
        # 方案A结算的采样端随之停用,归因(article_id)保留,效果评估走方案B拉新周录。
        # ⚠️ 2026-10-03:连它的 `wechat_traffic_cron` 配置一起删了(零引用)—— 只留这段说明,
        # 免得下一个人以为"作业还在、只是没启用"。
        # 元组末位是**实例角色**(见 _role_allows):wechat=公众号+闲鱼侧 / hotspot=热点侧 /
        # both=中性(各按自己库里的数据跑)。分体部署时本机设 wechat、远程设 hotspot。
        (pan_cookie_keepalive_tick, "0 7 * * *", {"minute": 0, "hour": 7}, "pan_cookie_keepalive", "wechat"),
        # 多平台热榜采集(v2.2.0):每小时 05 分——bilibili/douban 自研 + newsnow 长尾
        (hot_source_tick_all_users, "5 * * * *", {"minute": 5}, "hot_source", "hotspot"),
        # 死号清理(v2.6.0):每日 05:30——7 天无发文的对标号自动停监控(带链路安全阀)
        (retire_dormant_tick_all_users, "30 5 * * *", {"minute": 30, "hour": 5}, "bench_retire", "wechat"),
        # 选题复盘周报已并入推送时段表(默认**周一 16:30**)
        # 多平台热榜速览卡已并入推送时段表(默认 10:30/21:30)
        # 会员续费检查:每日 10:05(到期该收续费/超 24h 该踢名单 → 飞书);业务运营,归主实例
        (_member_renewal, "5 10 * * *", {"minute": 5, "hour": 10}, "member_renewal", "wechat"),
        # 事件归属:每 15 分钟把近 24h 快照归并为事件(跨平台共振/生命周期的基础层)
        (_event_assign, "*/15 * * * *", {"minute": "*/15"}, "event_assign", "hotspot"),
        (_agent_learn_all, "0 6 * * *", {"minute": 0, "hour": 6}, "agent_learning", "hotspot"),
        (agent_tick_all_users, "*/30 * * * *", {"minute": "*/30"}, "early_agent_tick", "hotspot"),
        (douhot_window_tick, _get_settings().douhot_window_cron, {"minute": "*/20"}, "douhot_window_tick", "hotspot"),
        # wr_skey 短效且轮换:主动换新则永不过期;失败即时推飞书。
        # 计划读 settings.weread_refresh_cron(默认对齐 4 个监听定点前 10 分钟)。
        # ⚠️ 当年"对齐定点"的理由是"renewal 换新会话后 mp/articles 列表能列一会儿" ——
        # 2026-10-04 实测**开不出这个窗口**(续期 success+verified 后立刻再拉仍恒 -2041;
        # 续期只是同会话换 skey,不是新会话)。对齐现只为"每轮监听都拿到最新鲜的会话",
        # **别再拿它去赌列表可用**。
        # ⚠️ 此前这里硬编码 "50 */6 * * *",settings 的对齐改动从未生效(2026-09-28 修复)
        (weread_refresh_tick, _get_settings().weread_refresh_cron, {"minute": 50, "hour": "*/6"}, "weread_refresh", "wechat"),
        # 选题 Agent 已并入推送时段表(默认 09:10/14:10/20:40)
        # 建议结算:每日 22:00(v5 结算端:盘链全网扩散增量 repost_gain,2026-09-30 起 reads_gain 采样已废)
        (settle_suggestions_all_users, "0 22 * * *", {"minute": 0, "hour": 22}, "suggestion_settle", "hotspot"),
        # 搜狗验证码红线约 30~50 次/天:每 4 小时一轮 × 每轮最多 5 词 = 30 次/天(安全区)
        (keyword_article_all_users, "40 */4 * * *", {"minute": 40}, "keyword_article", "wechat"),
        (candidate_discover_tick, _get_settings().candidate_discover_cron, {"minute": 20, "hour": 8}, "wechat_candidates", "wechat"),
        # 候选自动收录:紧随发现之后,按标准挑号补进 WeRSS 订阅池(带数量闸门,见 settings)
        (candidate_import_tick, _get_settings().candidate_auto_import_cron, {"minute": 30, "hour": 8}, "wechat_candidate_import", "wechat"),
        # 公众号板块总结(2026-10-04):按**阅读数**总结 + **闭环体检**(发现/收录/监控三段
        # 各自最近产出),推**管理群**。⚠️ 为什么必须体检:这条闭环的四段早就都在,但**各自静默**
        # —— 收录连续几天 0、监听一篇阅读数都没拿到,都没人知道(2026-10-04 实测
        # WeRSS 明明是好的、而 `listenable=0` 连了三天,分不清"正常"还是"断了")。
        # 每周一轮(阅读数 ~2 天轮一圈,日推只会重复);放周一 09:50,早于复盘周报。
        (run_wechat_digest_all_users, _get_settings().wechat_digest_cron,
         {"minute": 50, "hour": 9, "day_of_week": "mon"}, "wechat_digest", "wechat"),
        # 跨平台同类资源号发现(2026-10-01):拿资源库的**已验证资源名**去知乎等平台搜,
        # 只收录内容里真含网盘链的账号(各平台门槛见 cross_platform.py 头注)
        (cross_account_tick, _get_settings().cross_discover_cron, {"minute": 0, "hour": 9}, "cross_account_discover", "wechat"),
        # 抖音推广线索(2026-10-02):标题带《…》前缀的推广视频 → 解析口令 → **自动转存入库**
        # → 推卡片(标题+视频链接+我方分享链)。搜索词来自**群组新资源 + 公众号已验证资源**;
        # **每天** 11:00 一轮(低频——它要开浏览器,一次几分钟)。
        (douyin_leads_tick, _get_settings().douyin_leads_cron, {"minute": 0, "hour": 11}, "douyin_leads", "wechat"),
        # 网盘资源发现(2026-10-02):**直链型**那条 —— 按资源词搜知乎 → 抽夸克/百度盘链 →
        # 转存成我方链 → 推知乎群。与抖音(口令型)**形态不同但互补**,错开半小时跑。
        (pan_discovery_tick, _get_settings().pan_discovery_cron, {"minute": 30, "hour": 11}, "pan_discovery", "wechat"),
        # 跨平台资源热度(2026-10-02):抓**资源名** → 回**资源库**匹配链 —— 用于"平台上没有链"
        # 的那些平台(小红书/快手/贴吧)。每平台各开一次浏览器,所以**每周一轮**。
        (presence_tick, _get_settings().presence_cron, {"minute": 0, "hour": 9}, "resource_presence", "wechat"),
        # 拉新周录提醒(2026-10-03):每周一提醒录上周拉新 —— `pan_recruit_weekly` 是转化回路
        # **唯一的真值入口**(链接级真值不可得,已定案),却至今 0 行。**录了就不再提醒**。
        (recruit_reminder_tick, _get_settings().recruit_reminder_cron, {"minute": 40, "hour": 9}, "recruit_reminder", "wechat"),
        # **本机 → 远程 单向同步**(2026-10-04):公众号监听必须在本机(微信读书 Cookie 绑出口 IP),
        # 而选题 Agent 在远程 —— 两边库独立,不同步的话远程 Agent 的"竞品供给"永远是旧快照。
        # ⚠️ 方向**单向**(本机→远程);没配 `remote_db_url` 时它是空转(不记运行记录)。
        (remote_sync_tick, _get_settings().remote_sync_cron, {"minute": "*/30"},
         "remote_sync", "wechat"),
        # **链路体检推送**(2026-10-05):把**全部链路**(不只公众号)的运行情况推管理群。
        # 之前只有公众号有周期总结,其余几条链全靠人手动跑脚本看 —— 等于没有主动通报。
        (chain_report_tick, _get_settings().chain_report_cron, {"minute": 30, "hour": 9},
         "chain_report", "wechat"),
        # 跨实例健康可见(2026-10-03):远端每天推一张**板块健康卡**到管理员群。
        # 为什么推卡而不是本机轮询远端库:两边**库是独立的**,推飞书零配置、零新增暴露面;
        # 远端整机挂了这张卡就断,"该来没来"本身是信号(本机另有 peer_status 探活兜底)。
        # 角色 hotspot → 只在远端跑,本机不重复推。
        (health_push_tick, _get_settings().health_push_cron, {"minute": 20, "hour": 9}, "health_push", "hotspot"),
        # 磁盘水位守卫(2026-10-03 全项目审查补):磁盘写满 → **整站 502**,而水位是**逐渐**涨的,
        # 远在崩溃前就有征兆 —— 属"本可以预警却没预警"的典型。角色 **both**:两端机器各有各的盘,
        # 各查自己的(不是"同一件事两边都推")。超阈值推管理员群,标题不含数字以免冷却门失效。
        (disk_guard_tick, _get_settings().disk_guard_cron, {"minute": 40, "hour": 9}, "disk_guard", "both"),
        # 迅雷盘同步(2026-10-02):扫用户迅雷盘 → 新转存进来的资源自动生成我方分享链 → 入库。
        # "用口令找资源并转存"那步只有手机 App 能做(服务端搜索接口不对外 + 部分口令是群组口令),
        # 所以人工只在 App 里搜+转存,本作业接手扫盘/二次分享/入库。
        (xunlei_sync_tick, _get_settings().xunlei_sync_cron, {"minute": "*/30"}, "xunlei_sync", "wechat"),
        # 迅雷**群组**采集(2026-10-02):群消息流里群主发的分享卡**自带分享链** ——
        # 客户端唯一的"口令→shareID"那步,群组替我们做了。两步:采集登记(纯 HTTP 读,
        # 秒级、可高频) + **限量**转存(转存慢且占盘,每轮默认 5 条)。
        (xunlei_group_tick, _get_settings().xunlei_group_cron, {"minute": "*/20"}, "xunlei_group", "wechat"),
        # 过期转存清理(2026-10-03 用户口径:"一个星期内没有人再发了就可以删除")。
        # ⚠️ **默认关**(`xunlei_cleanup_enabled`),开了才动手;动作是**移入回收站**不是永久删,
        # 且单轮限量。它**解决不了"盘快满"** —— 实测本流水线在盘里只有 ~0.9TB(见体检方案)。
        (xunlei_cleanup_tick, _get_settings().xunlei_cleanup_cron, {"minute": 30, "hour": 3}, "xunlei_cleanup", "wechat"),
        # 同名去重(2026-10-04 用户:"里面我发现一些重名的文件你去删除吧" + "做成定时作业"):
        # 迅雷转存同名会自动加 `(1)`/`(2)`,同一份资源躺好几份。
        # ⚠️ **与上面那条相反,这个默认开** —— 它只删**内容可证明完全相同**的副本
        # (逐个进文件夹比子项名+大小),且走**移入回收站**;风险不是一个量级。
        # 每周一次(重名积累得慢),周日 04:00 与 `xunlei_cleanup`(03:30)错开。
        (xunlei_dedupe_tick, _get_settings().xunlei_dedupe_cron, {"minute": 0, "hour": 4,
                                                                 "day_of_week": "0"},
         "xunlei_dedupe", "wechat"),
        # Telegram 频道资源源(2026-10-01):公众号之外的第二路盘链 feed。
        # 默认关闭——本机直连 t.me 不通;能出网的机器把 TG_ENABLED 打开即可(见 settings)。
        (tg_collect_tick, _get_settings().tg_cron, {"minute": "*/30"}, "tg_collect", "both"),
        # 推送时段表(2026-10-01):日报/热榜速览/选题分析/Agent/爆点回顾/复盘周报/洞察周报
        # 共 7 类推送不再各占一条 Cron,改由这一个每分钟 tick 按库里的时段配置判定
        # (见 app/services/push_timeline.py)。改时间即刻生效,不必重启或重建作业。
        # 角色标 both:tick 本身两边都跑,但**推哪些类**由 push_timeline 按角色过滤
        # (热点类归远程发、公众号类归本机发),否则同一个群会收到两份。
        (push_timeline_tick, "* * * * *", {"minute": "*"}, "push_timeline", "both"),
    ]
    for func, expr, default, job_id, role in jobs:
        _add_job(scheduler, func, _cron_trigger(expr, default), job_id, role)


def _scheduler_kwargs() -> dict:
    """内嵌(API)与独立(python -m app.main)两种模式的调度器公共参数。

    13+ 个作业共享默认 10 线程会互相饿死(采集/监听都是网络长任务),扩到 24;
    misfire_grace_time=300:每日作业在重启/卡顿后 5 分钟内仍补跑(否则直接跳过)。
    """
    from apscheduler.executors.pool import ThreadPoolExecutor

    return {
        "timezone": "Asia/Shanghai",
        "executors": {"default": ThreadPoolExecutor(24)},
        "job_defaults": {
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 300,
        },
    }


def start(settings: Settings | None = None) -> BackgroundScheduler | None:
    """启动后台调度器(幂等:重复调用只会启动一次)。

    `SCHEDULER_ENABLED=false` 时不启动(测试环境、或改用独立调度容器时)。
    """
    global _scheduler
    settings = settings or get_settings()
    if not settings.scheduler_enabled:
        logger.info("SCHEDULER_ENABLED=false,后台调度器未启动")
        return None
    with _lock:
        if _scheduler is not None:
            return _scheduler
        scheduler = BackgroundScheduler(**_scheduler_kwargs())
        build_jobs(scheduler)
        scheduler.start()
        _scheduler = scheduler
    logger.info("后台调度器已启动:按各用户设置的频率采集(每分钟检查一次到期任务)")
    return _scheduler


def shutdown() -> None:
    """停止调度器(应用关闭时调用)。"""
    global _scheduler
    with _lock:
        if _scheduler is None:
            return
        _scheduler.shutdown(wait=False)
        _scheduler = None
    logger.info("后台调度器已停止")
