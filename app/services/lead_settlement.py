"""线索结算对账(2026-10-03):把**人工周录的真值**与**系统侧的线索量级**并排摆出来。

**为什么需要它**:结算回流此前缺的**不是代码**,是数据 ——
  ① **链接级真实转存数不可得**:夸克官方只给 0/-1(2026-09-29 定案),迅雷没有统计接口;
  ② 唯一的真值入口(周录 `pan_recruit_weekly`)**一次都没录过**(0 行)。

所以这里做的是**把两边摆在一起对账**,而不是替用户猜一个转化率出来:

    · 系统侧:本周发现的线索条数 + **转发量之和**  → 一个"资源热度指数"
    · 真人侧:用户从官方后台抄来的**分渠道**周拉新数

⚠️ **不许把两边直接相除当"转化率"** —— **口径不同源**:
系统侧的转发量是**别人视频**的(`share_count`),衡量"这个资源在抖音有多热";
用户录的是**我们自己发文**带来的拉新。硬除出来的系数没有意义,只会误导。
先把几周对照摆出来,偏差稳定了再谈校准。用户给过的先验(抖音 转发→转存 60~80%)只作为
**展示用的估计系数**列在旁边,标成"先验",不参与任何自动决策。
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DouyinLead, PanRecruitWeekly, SystemConfig
from app.utils import get_logger

logger = get_logger(__name__)

_COEF_KEY = "lead_coefficients"
# 用户口径(2026-10-03):"抖音的看转发的量,转化有 60%-80%" —— 取中值当**展示用**先验。
# ⚠️ 它乘的是**别人视频**的转发量,所以是"这个资源大概能带来多少转存"的粗估,
# 不是我们自己号的实际转化率(见模块头注)。
PRIOR_COEFFICIENTS = {"douyin": 0.7}


def load_coefficients(db: Session) -> dict[str, float]:
    """读先验/校准系数(无记录用默认;键缺失补默认)。"""
    out = dict(PRIOR_COEFFICIENTS)
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == _COEF_KEY))
    if row and row.value:
        try:
            saved = json.loads(row.value)
            for k, v in saved.items():
                if k in out and isinstance(v, (int, float)):
                    out[k] = float(v)
        except (ValueError, TypeError):
            pass
    return out


def _channels_of(row: PanRecruitWeekly | None) -> dict[str, int]:
    """周录行的**分渠道明细**;老行没有这个字段(空串)就返回空表。

    空表不等于"没有数据" —— 调用方要拿 `has_record` 区分"录了但没分渠道"。
    """
    if row is None or not row.channels:
        return {}
    try:
        d = json.loads(row.channels)
    except (ValueError, TypeError):
        return {}
    return {str(k): int(v) for k, v in d.items() if isinstance(v, (int, float))}


def _missing_weeks(db: Session, user_id: int, weeks: int = 4) -> list[str]:
    """最近 `weeks` 个**已结束**的周里,哪些还没录(周一日期,倒序)。"""
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    done = {r.week_start.date().isoformat() for r in db.scalars(
        select(PanRecruitWeekly).where(PanRecruitWeekly.user_id == user_id)).all()}
    out: list[str] = []
    for i in range(1, weeks + 1):
        wk = (this_monday - timedelta(weeks=i)).isoformat()
        if wk not in done:
            out.append(wk)
    return out


def record_reminder_tick(settings=None) -> int:
    """每周一提醒录**上周**拉新(推**管理员群**)。返回是否推了(0/1)。

    **为什么值得单独做一个作业**:`pan_recruit_weekly` 是**转化回路唯一的真值入口**
    (链接级真实转存数在夸克/迅雷侧都拿不到,链接级不可得是定案的),而它**至今 0 行** ——
    没有真值,`weekly_report` 那半张对账表永远是空的,系统也无从校准"哪类内容真的带来拉新"。
    入口(接口/前端)早就有,缺的只是**有人去录**。

    ⚠️ **录了就不再提醒**(只列"还缺的周"):这是每周一次的内部待办,不是每周一次的通知 ——
    无条件推会变成噪音然后被无视(与看门狗告警同一个教训)。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    if not getattr(settings, "recruit_reminder_enabled", True):
        return 0
    webhook = str(getattr(settings, "feishu_webhook_admin", "") or "").strip()
    if not webhook:                       # 内部待办 → 只推管理群,不回落客户群
        logger.info("周录提醒跳过:未配管理员群 webhook")
        return 0
    from app.db import get_session_local
    from app.db.models import User

    db = get_session_local()()
    try:
        uid = db.scalar(select(User.id).where(User.enabled.is_(True)).order_by(User.id))
        if not uid:
            return 0
        missing = _missing_weeks(db, int(uid), weeks=4)
        if not missing:                   # 上一周已录 → 静默
            return 0
        last = missing[0]
        end = (date.fromisoformat(last) + timedelta(days=6)).isoformat()
        lines = [
            f"📝 **该录上周拉新了**（{last} ~ {end}）",
            "",
            "从官方后台抄**分渠道**周总量,录进「建议」页(或 `POST /api/hotspot/recruits`):",
            "　抖音 ______　公众号 ______",
            "",
            "**为什么重要**:链接级真实转存数在夸克/迅雷侧都拿不到(已定案),"
            "所以这是转化回路**唯一的真值** —— 没它,「线索结算对账」只有系统侧那半张表,"
            "也无从校准哪类内容真的带来拉新。",
        ]
        if len(missing) > 1:
            lines += ["", f"⚠️ 已累计 **{len(missing)} 周**未录:{' / '.join(missing)}"]
        from app.services.feishu_client import FeishuClient

        ok = FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send("\n".join(lines))
        logger.info("周录提醒已推送(缺 %d 周)", len(missing))
        return 1 if ok else 0
    except Exception:  # noqa: BLE001 - 提醒失败不该炸调度
        logger.exception("周录提醒推送失败")
        return 0
    finally:
        db.close()


def weekly_report(db: Session, user_id: int, weeks: int = 8) -> dict:
    """近 N 周的对账表(每周一行):系统侧线索量级 vs 人工周录真值。

    行形如::

        {"week_start": "2026-09-29", "leads": 13, "share_total": 4123,
         "estimated": 2886.1,               # share_total × 先验系数(标注为粗估)
         "recorded_total": 60,              # 用户录的周总量(0 = 没录)
         "recorded_channels": {"douyin": 42, "wechat": 18},
         "has_record": true}

    `has_record=False` 的周**要明显区别对待**:那是"你还没录",不是"这周没拉新"。
    """
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(weeks=max(1, weeks) - 1)
    coef = load_coefficients(db)

    # 线索侧:按 found_date(YYYY-MM-DD 字符串,字典序=时间序)先按日聚合,再折成周
    rows = db.execute(
        select(DouyinLead.found_date, func.count(), func.sum(DouyinLead.share_count))
        .where(DouyinLead.user_id == user_id,
               DouyinLead.found_date >= start.isoformat())
        .group_by(DouyinLead.found_date)).all()
    per_week: dict[str, dict] = {}
    for fd, n, sc in rows:
        try:
            d = date.fromisoformat(str(fd))
        except (TypeError, ValueError):
            continue
        wk = (d - timedelta(days=d.weekday())).isoformat()
        b = per_week.setdefault(wk, {"leads": 0, "share_total": 0})
        b["leads"] += int(n or 0)
        b["share_total"] += int(sc or 0)

    rec_rows = db.scalars(select(PanRecruitWeekly).where(
        PanRecruitWeekly.user_id == user_id,
        PanRecruitWeekly.week_start >= datetime.combine(start, time.min))).all()
    rec = {r.week_start.date().isoformat(): r for r in rec_rows}

    out: list[dict] = []
    for i in range(max(1, weeks)):
        wk = (start + timedelta(weeks=i)).isoformat()
        side = per_week.get(wk, {"leads": 0, "share_total": 0})
        r = rec.get(wk)
        out.append({
            "week_start": wk,
            "leads": side["leads"],
            "share_total": side["share_total"],
            "estimated": round(side["share_total"] * coef.get("douyin", 0.0), 1),
            "recorded_total": (int(r.recruits) if r else 0),
            "recorded_channels": _channels_of(r),
            "has_record": r is not None,
        })
    recorded_weeks = sum(1 for x in out if x["has_record"])
    return {"weeks": out, "coefficients": coef, "recorded_weeks": recorded_weeks,
            "note": ("系统侧转发量是**别人视频**的(这个资源在抖音有多热),"
                     "与你录的**自己号拉新**不同源 —— 只看趋势是否同步,别直接相除当转化率。"
                     if recorded_weeks else
                     "还没有任何周录 —— 把官方后台的数字录进来,这张表才开始有意义。")}
