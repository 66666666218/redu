# -*- coding: utf-8 -*-
"""网盘链路**自检**(2026-10-19,用户口径「增加一道自检功能」)。

## 它解决什么

清理与转存这两条链最大的风险不是"报错",而是「**看着在跑、其实空转**」:
  · 10-12~10-14 夸克连续三轮一字不差地「跳过(没扫完)」—— 数字纹丝不动;
  · 10-18 迅雷周去重「扫 2581 个目录 / 800 次调用」后撞预算,**一个都没删**;
  · 10-18 23:40 一次测试把百度生产清单覆盖成 `dup0.apk` 这种造出来的数据。

这些在 `runs` 里**都记了**,但没人每天去翻。本模块就是那个"每天替你翻一遍"的作业。

## 边界

1. **只读**:不列盘、不转存、不删除 —— 只查库、查文件、查配置。撞锁/网络都不可能。
2. **正常也要留一行**:`runs.kind='pan_self_check'` 每轮都写,detail 写清各项结果 ——
   否则「自检自己没跑」会是整个体系里**最安静的那个静默失败**。
3. **异常才推管理群**:正常不打扰;异常的标题带具体项,不写"请检查"这种没用的话。

## 检查项

| 项 | 判据 |
|---|---|
| 作业时效 | `pan_auto_clean`/`xunlei_cleanup` ≤26h;`xunlei_dedupe` ≤8d;从未跑过 = 错误 |
| 最近一轮结果 | `failed` 或 detail 命中「没扫完 / 撞上限 / 撞 API 预算 / 不可信 / 异常」 |
| 盘容量 | 按**每个盘池里最空的号**判:≥95% 警告、≥99% 错误;**>100%** 单独报「配额降档/读数不可信」,不给"清理"这种错动作 |
| 资源库复用 | 近 N 天有成功搬链、却**一次复用都没有** ⇒ 复用入口可能没接上 |
| **资源库净增长** | 近 N 天搬成的我方链**按分享身份去重**后,扣掉**已判死**的还剩多少;一条没搬成 / 净增 ≤0 / 存活率过低各自一种说法 |
| 百度清单 | 不存在 / 超过 48h / 内容是测试造的(`dup0.apk` 形状) |
| 配置 | 自动清理关着、dry-run 还开着、单轮上限或周去重预算偏小 |
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 作业 → 允许的最大间隔(小时)。日作业留 2h 余量;周作业留 1 天余量。
_JOB_MAX_AGE_H: dict[str, float] = {
    "pan_auto_clean": 26.0,
    "xunlei_cleanup": 26.0,
    "xunlei_dedupe": 24 * 8.0,
}

_JOB_LABEL: dict[str, str] = {
    "pan_auto_clean": "三盘自动清理",
    "xunlei_cleanup": "迅雷过期清理",
    "xunlei_dedupe": "迅雷整包去重",
}

#: 运行记录里出现这些词 = 这一轮「看着成功、其实空转/受阻」。
_BAD_MARKERS: tuple[tuple[str, str], ...] = (
    ("异常", "error"),
    ("failed", "error"),
    ("没扫完", "warn"),
    ("撞上限", "warn"),
    ("撞 API 预算", "warn"),
    ("不可信", "warn"),
)


def _item(name: str, level: str, detail: str, **extra: Any) -> dict:
    """一项检查结果。`level` ∈ `ok` / `warn` / `error`(同 `info` 不入告警)。"""
    return {"name": name, "level": level, "detail": detail, **extra}


def _age_hours(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return round((datetime.now() - dt).total_seconds() / 3600, 1)


def _last_run(db: Session, user_id: int, kind: str):
    from app.db.models import RunRecord

    return db.execute(
        select(RunRecord)
        .where(RunRecord.user_id == user_id, RunRecord.kind == kind)
        .order_by(RunRecord.id.desc())
        .limit(1)
    ).scalars().first()


def _last_heartbeat(db: Session, job_id: str):
    """调度器的「作业真的跑过」心跳。`runs` 不是每个作业都写(如 `xunlei_cleanup`)。"""
    from app.db.models import JobHeartbeat

    return db.get(JobHeartbeat, job_id)


def _check_job_age(db: Session, user_id: int, kind: str, max_age_h: float) -> dict:
    label = _JOB_LABEL.get(kind, kind)
    run = _last_run(db, user_id, kind)
    if run is not None:
        age = _age_hours(run.started_at)
        detail = f"最近一轮 {run.started_at:%Y-%m-%d %H:%M}({run.status}),{age}h 前"
        if age is None or age > max_age_h:
            return _item(f"{label}时效", "error",
                         f"{detail};超过阈值 {max_age_h:.0f}h")
        if run.status == "failed":
            return _item(f"{label}时效", "error", f"{detail};该轮失败")
        return _item(f"{label}时效", "ok", detail)
    #: ★ **`runs` 不是每个作业都写**(2026-10-19 实测):`xunlei_cleanup` 只写
    #: `job_heartbeats`。没有这一层,自检会把它误报成「从未运行」—— 而误报会让
    #: 人开始忽略自检,那是告警体系最坏的结局。
    hb = _last_heartbeat(db, kind)
    if hb is None or hb.last_run_at is None:
        return _item(f"{label}时效", "error", f"{kind} **从未运行过**")
    age = _age_hours(hb.last_run_at)
    detail = (f"心跳:最近 {hb.last_run_at:%Y-%m-%d %H:%M}"
              f"({hb.run_count} 次,错误 {hb.error_count}),{age}h 前")
    if age is None or age > max_age_h:
        return _item(f"{label}时效", "error", f"{detail};超过阈值 {max_age_h:.0f}h")
    return _item(f"{label}时效", "ok", detail)


def _check_run_detail(db: Session, user_id: int, kind: str) -> dict:
    """最近一轮的 detail 是否命中「空转/受阻」标记。"""
    label = _JOB_LABEL.get(kind, kind)
    run = _last_run(db, user_id, kind)
    if run is None:
        #: 同 `_check_job_age`:只写心跳的作业没有 detail,不能因此判失败。
        hb = _last_heartbeat(db, kind)
        if hb is None or hb.last_run_at is None:
            return _item(f"{label}结果", "error", "没有运行记录")
        if int(hb.error_count or 0) > 0 and str(hb.last_error or "").strip():
            return _item(f"{label}结果", "warn",
                         f"心跳里有 {hb.error_count} 次错误:{str(hb.last_error)[:160]}")
        return _item(f"{label}结果", "ok",
                     f"该作业只写心跳({hb.run_count} 次,错误 0),无 detail")
    text = str(run.detail or "")
    hits = [word for word, _ in _BAD_MARKERS if word.lower() in text.lower()]
    if run.status == "failed" and "failed" not in [h.lower() for h in hits]:
        hits.insert(0, "failed")
    if not hits:
        return _item(f"{label}结果", "ok", text[:160] or "无异常标记")
    level = "error" if run.status == "failed" or any(
        word in ("异常", "failed") for word in hits) else "warn"
    return _item(f"{label}结果", level,
                 f"命中「{'/'.join(hits)}」:{text[:180]}")


def _probe_ratio(db: Session, platform: str, ref) -> float | None:
    """**补探**一个还没使用率数据的号(2026-10-21)。

    为什么需要:百度池里那唯一的号 `last_ratio` 恒为 `null` —— 因为只有**转存成功路径**
    会写回池,而百度那条链从来没报过配额 ⇒ 盘容量自检对百度是**瞎的**,
    而积压里 28 条正是被「转存空间不足(-32)」卡住的。
    只探**已知有空缺**的号(不是每轮全探),失败返回 None(不知道 ≠ 满)。
    """
    try:
        from app.services import pan_accounts

        #: 隐式"默认账号"(没配池)不写回池 —— 探到了也没地方存,白打一次接口。
        if getattr(ref, "is_default", False):
            return None
        ck = pan_accounts.cookie_of(ref, db)
        if not ck:
            return None
        if platform == "baidupan":
            from app.services.baidupan_transfer import BaiduPanClient

            return BaiduPanClient(ck).quota_ratio()
        if platform == "quark":
            #: ★ 2026-11-11 补:此前**没有这一支**,夸克两个号的 `last_ratio` 永远是 `None`
            #: ⇒ `pan_accounts.ordered()` 的 `usable_hint` 都退化成 0.0 ⇒ 按 label 排序
            #: ⇒ **超容 123% 的 1 号永远排第一,空着的 2 号永远轮不到**。
            #: 实测代价:近 7 天 67 条「夸克网盘容量不足」转存失败,积压从 235 涨到 269。
            from app.services.quark_transfer import QuarkTransfer

            return QuarkTransfer(ck).quota_ratio()
        if platform == "xunlei":
            from app.services import xunlei_transfer as xt

            cred = xt._credentials(cookie_key=ref.cookie_key)   # noqa: SLF001 - 与转存同一读法
            #: ⚠️ 用**原始读数**(2026-11-20):这里写的是池里的 `last_ratio`,是**记账**,
            #: 不是闸门。`quota_ratio` 现在会把兜底 limit 折成 None(见它的 docstring),
            #: 拿它回填等于把这个号的使用率**冻在旧值**上。兜底读数由自检自己按
            #: `_quota_limit_note` 另外说明,不影响"已用多少"这件事本身的记录。
            return xt.quota_ratio_raw(cred, cookie_key=ref.cookie_key)
    except Exception:  # noqa: BLE001 - 探针失败只是"不知道",不该让自检挂掉
        return None
    return None


def _probe_ratio_suspect(ref) -> bool:
    """刚才那个迅雷读数是不是**兜底 limit 算出来的**(判据只用 `xt.limit_looks_like_fallback`)。

    读的是 `quota_info` 的**进程内缓存**(与 `_probe_ratio` 刚刚那次同一个键)⇒ 不额外打接口。
    """
    try:
        from app.services import xunlei_transfer as xt

        return xt.limit_looks_like_fallback(
            xt.quota_info(cookie_key=getattr(ref, "cookie_key", "") or None))
    except Exception:  # noqa: BLE001 - 判不出来就当"可信",别让自检挂掉
        return False


def _quota_limit_note(platform: str, refs: list) -> str:
    """整池都超出配额时,**探一次各号的 limit**,看是不是"读数塌陷"而不是"真超额"。

    只在**已经判定整池超额**时才探(正常日零成本)。判据是"**所有号的 limit 逐字节相同**
    且用量已超出它" —— 实测 2026-11-18 迅雷四个号的 limit 全是 `10737418240`(10 GiB
    免费档兜底值),而用量从 121 GiB 到 **16.03 TiB** 不等。真实配额不可能让 16 TiB 的
    数据塞进 10 GiB,也不可能有四个号"恰好"同一个 limit ⇒ **这个 limit 不是真配额**。

    ⚠️ **说不出来就闭嘴**:少于 2 个号、或各号 limit 不同 ⇒ 返回空串(不编证据)。
    """
    if platform != "xunlei":
        return ""
    try:
        from app.services import xunlei_transfer as xt

        rows: list[tuple[int, int]] = []
        for ref in refs:
            if getattr(ref, "is_default", False):
                continue
            cred = xt._credentials(cookie_key=ref.cookie_key)   # noqa: SLF001 - 与转存同一读法
            if not cred:
                continue
            info = xt.quota_info(cred, cookie_key=ref.cookie_key)
            if int(info.get("limit") or 0) > 0:
                rows.append((int(info["limit"]), int(info.get("usage") or 0)))
        if len(rows) < 2 or len({r[0] for r in rows}) != 1:
            return ""
        limit = rows[0][0]
        lo, hi = min(r[1] for r in rows), max(r[1] for r in rows)
        return (f"{len(rows)} 个号的 limit **完全相同**({xt.human_bytes(limit)}),"
                f"而用量 {xt.human_bytes(lo)}~{xt.human_bytes(hi)} ⇒ "
                f"**配额读数不可信**(疑似会员降档/兜底值),")
    except Exception:  # noqa: BLE001 - 证据探不到就不说,别让自检挂掉
        return ""


def _check_quota(db: Session, user_id: int) -> dict:
    """盘容量:按**每个盘池里最空的号**判,而不是取全池最满的那个。

    ⚠️ **为什么改**(2026-10-21 实测):旧实现取**全池最大值** ⇒ 每天报
    `迅雷/xunlei#2 已用 10253% —— 转存随时会因盘满停摆`。
    而 xunlei#2 是**免费号,配额只有 10 GiB**,池里早就没人用它;
    同一池的 #3 只用了 9.7%、#4 只用了 4.8%,转存一直落在这两个号上。
    ⇒ 这条告警**给的是错动作**(让人去清一个根本没人用的号),而真正的落点有余量。
    判据改成:**某个盘池里只剩 ≥95% 的号**才算这个盘有问题;
    只要有一个号低于 95%,转存就有落点(最满的那个只在附注里提)。

    ⚠️⚠️ **再改一次(2026-11-18):整个池都 >100% 时要换一种说法**。
    用量超过上限有两种截然不同的成因,**给的动作正好相反**:
      ① **真的超额** —— 百度主号 5.07 TiB / 120 GiB 配额(43 倍),那是"清理/换号";
      ② **限额读数塌了** —— 迅雷四个号的 `limit` **逐字节完全相同**,都是
         `10737418240`(=10 GiB 免费档兜底值),而 `usage` 各是 121 GiB ~ **16.03 TiB**
         ⇒ ratio 12.17 / **1640.99** / 102.50 / 75.56。真实免费档装不下 16 TiB
         ⇒ 这个 limit 不可能是真的(主号 `upgrade_type` 当时已是空字符串)。
    判据**不能靠量级**(4308% 的百度号是真超额,1217% 的迅雷号是假读数),所以这里
    只做一件事:**当整个池都 >100% 时,不给「清理」这个动作** —— 池里没有任何号能
    证明还有余量,而"清一下就好"在成因 ② 下是纯误导(清多少都不会恢复)。
    """
    from app.services import pan_accounts

    by_platform: dict[str, tuple[str, list[tuple[float, str]], list]] = {}
    for platform, label in (("quark", "夸克"), ("baidupan", "百度"), ("xunlei", "迅雷")):
        try:
            refs = pan_accounts.accounts(db, platform, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 - 池读坏了不该让整轮自检挂掉
            return _item("盘容量", "warn",
                         f"账号池读取失败({label}):{type(exc).__name__}: {str(exc)[:80]}")
        for ref in refs:
            #: 池里从没探到过的号(百度那条链从来不写回池)⇒ 自检这里**补探一次**。
            #: 只探缺数据的号,不是每轮全探;探到就写回池,下次不必再探。
            if ref.last_ratio is None:
                fresh = _probe_ratio(db, platform, ref)
                if fresh is None:
                    continue
                ref.last_ratio = fresh
                try:
                    #: ★ 2026-11-20:迅雷这一支现在回的是**原始读数**,要把可信度一起记 ——
                    #: 否则自检刚补上的 1640 会被下一轮择一原样当成"满了"。
                    _suspect = (platform == "xunlei"
                                and _probe_ratio_suspect(ref))
                    pan_accounts.report(db, ref, ratio=fresh, full=False, suspect=_suspect,
                                        why=f"已用 {fresh * 100:.1f}%")
                except Exception:  # noqa: BLE001 - 写不回池只影响下次择号,别让自检挂
                    pass
        #: 池的**原始 refs 也留着** —— "整池都超额"时要去探一次 limit 才能分清
        #: 「真超额」与「limit 是兜底值、读数不可信」(见 `_quota_limit_note`)。
        vals = [(float(ref.last_ratio), f"{label}/{ref.label}")
                for ref in refs if ref.last_ratio is not None]
        vals.sort()               # 最空在前:下面 best/worst 直接取首尾
        if vals:
            by_platform[label] = (platform, vals, refs)
    if not by_platform:
        return _item("盘容量", "info", "账号池里还没有使用率数据(等下一次转存/巡检)")

    problems: list[tuple[str, str]] = []
    usable: list[str] = []
    notes: list[str] = []
    for label, (platform, vals, refs) in by_platform.items():
        best = vals[0]            # 最空的号 = 转存真正的落点(已按比例升序)
        worst = vals[-1]
        best_pct, worst_pct = best[0] * 100, worst[0] * 100
        if best_pct >= 99:
            #: ⚠️ **别急着说"清理"**:用量 >100% 有两种成因,动作正好相反(见 docstring)。
            #: 只有**池里所有号都 >100%** 时才可能是"limit 读数塌陷";此时给出
            #: 「读数不可信 + 需人工确认配额」,而不是一个清多少都不会恢复的动作。
            suspect = (_quota_limit_note(platform, refs)
                       if best_pct > 100 and all(v[0] > 1.0 for v in vals) else "")
            if suspect:
                problems.append(("warn", f"{label} 池里**所有号**都超出配额"
                                          f"(最空 {best[1]} {best_pct:.1f}%)——{suspect}"
                                          f"转存已被闸门挡住,**清盘不会恢复**;"
                                          f"先人工确认这些号的实际配额/会员状态"))
            else:
                problems.append(("error", f"{label} 池里**所有号**都 ≥99%"
                                          f"(最空 {best[1]} {best_pct:.1f}%)—— 转存随时停摆"))
        elif best_pct >= 95:
            problems.append(("warn", f"{label} 池里**所有号**都 ≥95%"
                                     f"(最空 {best[1]} {best_pct:.1f}%)—— 建议尽快清理/加号"))
        else:
            usable.append(f"{label} 最空 {best[1]} {best_pct:.1f}%")
            if worst_pct >= 95 and worst[1] != best[1]:
                #: 超配额/免费小号会长期停在 >100%,它们**不是落点**,不该当告警。
                notes.append(f"{worst[1]} {worst_pct:.0f}%(不可用号,已不参与转存)")

    extra = (";" + ";".join(notes)) if notes else ""
    if problems:
        level = "error" if any(p[0] == "error" for p in problems) else "warn"
        return _item("盘容量", level, ";".join(p[1] for p in problems) + extra)
    return _item("盘容量", "ok", ";".join(usable) + extra)


def _check_reuse(db: Session, user_id: int, days: int) -> dict:
    """近 N 天「复用已有链」/「成功搬链」的比例 —— 复用入口是否真的在工作。"""
    from app.db.models import DiscoveredPanLink, XunleiGroupShare

    since = datetime.now() - timedelta(days=days)
    disc_ok = int(db.scalar(
        select(func.count()).select_from(DiscoveredPanLink).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.found_at >= since,
            DiscoveredPanLink.status == "ok")) or 0)
    disc_reuse = int(db.scalar(
        select(func.count()).select_from(DiscoveredPanLink).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.found_at >= since,
            DiscoveredPanLink.status == "ok",
            DiscoveredPanLink.message.like("%库里已有%"))) or 0)
    grp_ok = int(db.scalar(
        select(func.count()).select_from(XunleiGroupShare).where(
            XunleiGroupShare.user_id == user_id,
            XunleiGroupShare.synced_at >= since,
            XunleiGroupShare.status == "ok")) or 0)
    grp_reuse = int(db.scalar(
        select(func.count()).select_from(XunleiGroupShare).where(
            XunleiGroupShare.user_id == user_id,
            XunleiGroupShare.synced_at >= since,
            XunleiGroupShare.status == "ok",
            XunleiGroupShare.message.like("%三盘互通%"))) or 0)
    total, reused = disc_ok + grp_ok, disc_reuse + grp_reuse
    detail = (f"近 {days} 天:公开发现 {disc_ok} 条(复用 {disc_reuse})、"
              f"迅雷群 {grp_ok} 条(复用 {grp_reuse})")
    if total >= 3 and reused == 0:
        return _item("资源库复用", "warn",
                     detail + ";**一次复用都没有** —— 可能是全是新资源,"
                     "也可能是复用入口没接上(看 `pan_discovery.reuse_if_have` 的调用点)")
    return _item("资源库复用", "ok", detail)


#: 净增长判据的**最小样本**:少于这么多条时不下结论 —— 样本太小时「0 条」可能只是
#: 今天本来就没活干,把它报成故障就是**狼来了**(报警体系最坏的结局,见模块头注)。
_NET_MIN_SAMPLE = 10


def _check_net_growth(db: Session, user_id: int, days: int) -> dict:
    """近 N 天资源库**真的在长吗** —— 搬成多少、死掉多少、净增多少。

    ## 为什么单开一项(2026-11-24)
    `_check_reuse` 只看「复用比例」:它回答的是"有没有走复用入口",**不回答库在不在长**。
    盘满停摆那几天(11-19 起连续 5 天转存 = 0、积压从 0 爬到 327)在自检里**全绿** ——
    作业按时跑了、复用率也没掉,因为**根本没有东西被搬成**,而那个比例的分母正是 0。
    ⇒ 需要一条**直接看净增**的判据:搬成的数量 **减去已判死的数量**。

    ## 口径(三条都必须对,数字才可信)
    1. **按分享身份去重**:同一条我方链会被多个标题复用(实测一条挂过 68 个标题),
       逐行数会把 129 条真实分享数成 818 条 —— 那不是「库长了 818」,是「推了 818 次」。
    2. **判死用出链口那一份**(`resource_library.dead_links`):账号级封禁(夸克1号 41031)
       只有它能认出来;拿业务表自己的 `status` 判,那 6596 条死链一条都看不见。
    3. **认不出盘的链不算死**("没看见"≠"坏的",见 `pan_link_health` 模块头注);
       但它也没法去重,只能按整条 URL 记一次,并单独报 `认不出盘 N`。

    ## 2026-11-24 首次标定的读数(生产库,只读)
    近 7 天:发现 1924 条、搬成 **129 条不同分享**、其中 **61 条已判死**(60 条夸克 =
    已停用的 1 号),净增 **68 条(53%)**;近 14 天净增 417/506(82%)。
    ⇒ 这正是「积压天天涨、库却不怎么长」的那个形态,而此前没有任何一项在看它。
    """
    from app.db.models import DiscoveredPanLink, XunleiGroupShare
    from app.services.pan_link_health import share_key_of

    since = datetime.now() - timedelta(days=days)
    found = int(db.scalar(
        select(func.count()).select_from(DiscoveredPanLink).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.found_at >= since)) or 0)
    found += int(db.scalar(
        select(func.count()).select_from(XunleiGroupShare).where(
            XunleiGroupShare.user_id == user_id,
            XunleiGroupShare.synced_at >= since)) or 0)
    backlog = int(db.scalar(
        select(func.count()).select_from(DiscoveredPanLink).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.found_at >= since,
            DiscoveredPanLink.status == "pending")) or 0)
    backlog += int(db.scalar(
        select(func.count()).select_from(XunleiGroupShare).where(
            XunleiGroupShare.user_id == user_id,
            XunleiGroupShare.synced_at >= since,
            XunleiGroupShare.status == "pending")) or 0)

    urls = [str(u or "") for (u,) in db.execute(
        select(DiscoveredPanLink.our_url).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.found_at >= since,
            DiscoveredPanLink.status == "ok",
            DiscoveredPanLink.our_url != "")).all()]
    urls += [str(u or "") for (u,) in db.execute(
        select(XunleiGroupShare.our_url).where(
            XunleiGroupShare.user_id == user_id,
            XunleiGroupShare.synced_at >= since,
            XunleiGroupShare.status == "ok",
            XunleiGroupShare.our_url != "")).all()]

    from app.services.resource_library import dead_links

    dead = dead_links(db, user_id, fresh=True)
    seen: set[tuple[str, str]] = set()
    dead_n = unparsed = 0
    dead_by_platform: dict[str, int] = {}
    for u in urls:
        plat, key = share_key_of(u)
        ident = (plat, key) if key else ("", u)
        if ident in seen:
            continue
        seen.add(ident)
        if not key:
            unparsed += 1
            continue
        if dead.is_dead(u):
            dead_n += 1
            dead_by_platform[plat] = dead_by_platform.get(plat, 0) + 1
    moved = len(seen)
    net = moved - dead_n

    detail = (f"近 {days} 天:发现 {found}(积压 {backlog}),"
              f"搬成 {moved} 条不同分享,其中已判死 {dead_n}")
    if dead_by_platform:
        detail += "(" + "/".join(f"{p} {n}" for p, n in
                                 sorted(dead_by_platform.items(), key=lambda kv: -kv[1])) + ")"
    detail += f",**净增 {net}**"
    if unparsed:
        detail += f";认不出盘 {unparsed}(未判死)"

    if moved == 0 and found >= _NET_MIN_SAMPLE:
        #: ★ 这是「盘满停摆 / 转存链没在工作」最直接的形态:整整 N 天一条都没搬成,
        #: 而发现量还有那么多 —— 不是"最近没资源",是**出口没了**。
        return _item("资源库净增长", "error",
                     detail + ";★ **一条都没搬成** —— 出口没在工作(先看盘容量与转存失败原因)")
    if moved >= _NET_MIN_SAMPLE and net <= 0:
        return _item("资源库净增长", "warn",
                     detail + ";搬进来的**被死掉的抵消完了** —— 先看死链集中在哪个号,"
                             "别急着搬更多")
    if moved >= _NET_MIN_SAMPLE and net * 2 < moved:
        return _item("资源库净增长", "warn",
                     detail + f";**存活率只有 {net * 100 // moved}%** —— "
                             "死链集中在某几个号时,搬得再多库也长不起来")
    return _item("资源库净增长", "ok", detail)


def _check_baidu_report(path: str | Path) -> dict:
    """百度重复清单:存在、新鲜、且**不是测试造的数据**。

    ⚠️ 为什么专门查"测试数据":`tests/test_baidu_dup.py` 曾经直接调用真实的
    `report_baidu()`(路径写死 `data/pan_dedupe_baidu.json`)⇒ 跑一次测试就把生产清单
    覆盖成 `dup0.apk / 100 字节`。清单是人工决定"要不要开百度删除"的唯一依据,
    污染它比没有清单更坏。
    """
    p = Path(path)
    if not p.exists():
        return _item("百度清单", "warn", f"清单不存在:{p}")
    age = _age_hours(datetime.fromtimestamp(p.stat().st_mtime))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return _item("百度清单", "warn", f"清单读不出来:{type(exc).__name__}: {str(exc)[:80]}")
    dup_files = data.get("dup_files") or []
    looks_test = (
        str(data.get("root") or "") == "/"
        and bool(dup_files)
        and all("dup" in str(x.get("path") or "") for x in dup_files)
        and all(int(x.get("size") or 0) <= 1024 for x in dup_files)
    )
    if looks_test:
        return _item("百度清单", "warn",
                     f"清单**疑似测试数据**(root=/、dupN 形状),文件 {p};"
                     "重扫用 `python scripts/baidu_dedupe.py`")
    if age is not None and age > 48:
        return _item("百度清单", "warn",
                     f"清单已 {age}h 未更新(阈值 48h):{p}")
    return _item("百度清单", "ok",
                 f"{data.get('n_groups', 0)} 组 / 多出 {data.get('n_dup', 0)} 份 / "
                 f"{int(data.get('bytes') or 0) / 2 ** 30:.2f} GiB,{age}h 前")


def _check_config(settings) -> dict:
    """配置是否处在"真的会干活"的状态。"""
    enabled = bool(getattr(settings, "pan_auto_clean_enabled", False))
    dry = bool(getattr(settings, "pan_auto_clean_dry_run", True))
    cap = int(getattr(settings, "pan_auto_clean_max_deletes", 30) or 0)
    budget = int(getattr(settings, "xunlei_dedupe_budget", 800) or 0)
    detail = f"自动清理={'开' if enabled else '关'},dry_run={'开' if dry else '关'},单轮上限={cap},周去重预算={budget}"
    if not enabled:
        return _item("清理配置", "warn", detail + ";清理作业不会执行")
    if dry:
        return _item("清理配置", "warn", detail + ";dry-run 开着,不会真删")
    if cap < 100:
        return _item("清理配置", "warn", detail + ";单轮上限偏小,积压可能追不上新增")
    if budget < 2000:
        return _item("清理配置", "warn", detail + ";周去重预算对 2500+ 目录偏小,可能每次撞上限")
    return _item("清理配置", "ok", detail)


def run_checks(db: Session, user_id: int, settings=None) -> dict:
    """跑一遍自检(**只读**)。返回 `{"status", "checks", "warnings", "checked_at"}`。"""
    from config.settings import get_settings

    settings = settings or get_settings()
    checks = [
        *(_check_job_age(db, user_id, kind, max_h)
          for kind, max_h in _JOB_MAX_AGE_H.items()),
        *(_check_run_detail(db, user_id, kind) for kind in _JOB_MAX_AGE_H),
        _check_quota(db, user_id),
        _check_reuse(db, user_id, int(getattr(settings, "pan_self_check_reuse_days", 7) or 7)),
        _check_net_growth(db, user_id,
                          int(getattr(settings, "pan_self_check_growth_days", 7) or 7)),
        _check_baidu_report(getattr(settings, "pan_auto_clean_baidu_report_path",
                                    "data/pan_dedupe_baidu.json")),
        _check_config(settings),
    ]
    warnings = [c for c in checks if c["level"] in ("warn", "error")]
    status = "error" if any(c["level"] == "error" for c in checks) else (
        "warn" if warnings else "ok")
    return {"status": status, "user_id": user_id,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "checks": checks, "warnings": warnings}


def _format_detail(out: dict) -> str:
    return " | ".join(f"[{c['level']}] {c['name']}: {c['detail']}" for c in out["checks"])


def self_check_all_users(settings=None) -> int:
    """计划任务入口:对全部启用用户各跑一轮自检。返回**有异常的用户数**。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User
    from app.services.tenant_base import _record_run

    settings = settings or get_settings()
    if not getattr(settings, "pan_self_check_enabled", True):
        return 0
    db = get_session_local()()
    warned = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                out = run_checks(db, uid, settings)
                detail = _format_detail(out)
                _record_run(db, uid, "pan_self_check",
                            "failed" if out["status"] == "error" else "success",
                            detail[:900])
                db.commit()
                if out["warnings"]:
                    warned += 1
                    from app.services.alert_service import notify_incident

                    #: ★★ **标题里不许出现数字**(2026-11-23 修,与 `pan_discovery` 同一条):
                    #: 原来写 `f"网盘自检:{len(warnings)} 项需要处理"` ⇒ 项数一变就是新冷却键,
                    #: `notify_incident` 的去重**完全失效** —— 实测 `pan_clean` 30 条送达,
                    #: 同一件事(盘满/撞上限)被反复当成新告警推。项数挪进正文。
                    notify_incident(
                        db, uid, "pan_clean",
                        "网盘自检:有项需要处理",
                        f"**{len(out['warnings'])} 项**需要处理:"
                        + "\n".join(f"· {c['name']}:{c['detail']}"
                                    for c in out["warnings"])[:1500],
                        settings=settings)
            except Exception:  # noqa: BLE001 - 单用户自检失败不影响其余
                db.rollback()
                logger.exception("网盘自检失败 user=%s", uid)
    finally:
        db.close()
    return warned
