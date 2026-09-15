"""事件层:把多平台的热点标题归并为同一真实事件(Hotspot → Event)。

算法(纯本地,无 embedding 依赖):
  ① 标题规范化:去话题符/标点/空白/平台后缀,小写
  ② 相似度:字符 bigram 的 Jaccard 系数(对中文短标题鲁棒)
  ③ 归并:与"活跃事件"(48h 内有seen)的规范化标题比对,≥阈值并入,否则新建事件
  ④ 生命周期字段:首见/最近seen/峰值(值+时刻)/平台数/样本数

每 15 分钟跑一次 assign_tick,扫近 24h 各平台快照做归属。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import get_settings
from app.db.models import (BaiduHotItem, DouhotWord, EventMembership, HotspotEvent,
                           WeiboHotItem, XianyuItem)
from app.utils import get_logger

logger = get_logger(__name__)

SIM_THRESHOLD = 0.5      # bigram Dice 归并阈值(变体 0.5+,无关 <0.3,对立词直接 0)
ACTIVE_WINDOW_H = 48     # 活跃事件窗口:超窗后不再归并(旧事件自然终结)
SCAN_HOURS = 24          # 每轮回扫的快照时间范围
_END_AFTER_H = 72        # 超 72h 无 seen → 事件置 ended

_NOISE_RE = re.compile(r"[#＃\s。,，、!！?？:：;；'\"“”‘’()（）\[\]【】《》<>·\-_~～|]")
_SUFFIXES = ("上了热搜", "热搜第一", "登顶热搜", "爆了", "上热搜")

_TABLES = {
    "weibo": (WeiboHotItem, "captured_at", "title", "heat"),
    "baidu": (BaiduHotItem, "captured_at", "title", "heat"),
    "douhot": (DouhotWord, "created_at", "title", "score"),
    "xianyu": (XianyuItem, "created_at", "title", None),  # 闲鱼无热度,用 1(出现即热度)
}


def normalize_title(title: str) -> str:
    """标题规范化:去话题符/标点/热度后缀/空白,小写。"""
    t = str(title or "").strip().lower()
    t = _NOISE_RE.sub("", t)
    for suf in _SUFFIXES:
        t = t.replace(suf, "")
    return t.strip()


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) >= 2 else {text}


# 强对立词对:相似度再高也不归并(结婚/离婚是两个事件——误归并比漏归并危害大)
_OPPOSED_PAIRS = [
    ("结婚", "离婚"), ("结婚", "分手"), ("官宣结婚", "官宣离婚"),
    ("夺冠", "失利"), ("夺冠", "淘汰"), ("去世", "康复"), ("去世", "出院"),
    ("上任", "辞职"), ("上任", "落马"), ("被捕", "获释"), ("被捕", "出狱"),
    ("胜诉", "败诉"), ("中标", "落标"), ("上涨", "下跌"), ("涨", "跌"),
]


def dice_similarity(a: str, b: str) -> float:
    """字符 bigram Dice 系数(0~1)。比 Jaccard 对长度差/语序变化宽容:
    "官宣结婚"vs"宣布结婚" Jaccard 仅 0.33(漏归并),Dice 0.46。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def similarity(a: str, b: str) -> float:
    """归并相似度:Dice 系数;强对立词对直接判 0。"""
    for x, y in _OPPOSED_PAIRS:
        if (x in a and y in b) or (y in a and x in b):
            return 0.0
    return dice_similarity(a, b)


def _dice(grams_a: set[str], grams_b: set[str]) -> float:
    """已切好 bigram 的 Dice(热路径:归属循环里避免重复切分)。"""
    if not grams_a or not grams_b:
        return 0.0
    return 2 * len(grams_a & grams_b) / (len(grams_a) + len(grams_b))


def assign_tick(db: Session, user_id: int, settings=None) -> dict:
    """归属一轮:扫近 24h 快照 → 归并到活跃事件/新建事件 → 刷新生命周期字段。"""
    settings = settings or get_settings()
    now = datetime.now()
    # 增量扫描:只扫上次归属之后的新快照(带 10min 重叠缓冲防写入延迟漏扫)。
    # 旧实现每轮重扫 24h 全部快照(幂等保证正确但纯重复计算,139 次归属全是白跑);
    # 游标持久化到 system_config,进程重启不丢。首次运行(无游标)退回全窗扫描。
    from app.db.models import SystemConfig

    cursor_row = db.scalar(select(SystemConfig).where(
        SystemConfig.key == f"event_cursor_{user_id}"))
    scan_since = now - timedelta(hours=SCAN_HOURS)
    if cursor_row and cursor_row.value:
        try:
            last = datetime.fromisoformat(cursor_row.value)
            scan_since = max(scan_since, last - timedelta(minutes=10))
        except ValueError:
            pass
    active_cutoff = now - timedelta(hours=ACTIVE_WINDOW_H)

    events = db.scalars(select(HotspotEvent).where(
        HotspotEvent.user_id == user_id,
        HotspotEvent.status == "active",
        HotspotEvent.last_seen >= active_cutoff,
    )).all()
    # 事件 bigram 预计算:相似度匹配的分母是全部活跃事件(数百个),
    # 每轮每个新条目都重切一遍事件 bigram 是 O(N×M×L) 的纯浪费
    ev_grams: dict[int, set[str]] = {e.id: _bigrams(e.norm_title) for e in events}
    # 事件指纹:规范化标题 → 事件(优先精确命中,再做相似度)
    by_exact: dict[str, HotspotEvent] = {e.norm_title: e for e in events}
    # 复燃索引:近 7 天终结的事件(词二次起势时重激活,生命周期含"复燃"态)
    ended_recent = db.scalars(select(HotspotEvent).where(
        HotspotEvent.user_id == user_id, HotspotEvent.status == "ended",
        HotspotEvent.last_seen >= now - timedelta(days=7))).all()
    by_exact_ended: dict[str, HotspotEvent] = {e.norm_title: e for e in ended_recent}

    created = merged = ended = 0
    seen_pairs: set[tuple[int, str, str, datetime]] = set()  # (event_id, board, norm, ts)——同批同 norm 不同时刻的快照行都要计

    for board, (model, ts_col, title_col, value_col) in _TABLES.items():
        rows = db.scalars(select(model).where(
            model.user_id == user_id,
            getattr(model, ts_col) >= scan_since,
        ).order_by(getattr(model, ts_col).asc())).all()
        for r in rows:
            raw_title = str(getattr(r, title_col, "") or "").strip()
            norm = normalize_title(raw_title)
            if len(norm) < 4:   # 过短(单字/词)无聚类价值
                continue
            ts = getattr(r, ts_col, None) or now
            ts = ts if isinstance(ts, datetime) else now
            value = float(getattr(r, value_col, 0) or 0) if value_col else 1.0

            ev = by_exact.get(norm)
            if ev is None and norm in by_exact_ended:
                # 复燃:已终结事件重现 → 重激活并计数(生命周期: …→ENDED→复燃)
                ev = by_exact_ended.pop(norm)
                ev.status, ev.ended_at = "active", None
                ev.reappear_count = (ev.reappear_count or 0) + 1
                events.append(ev)
                by_exact[norm] = ev
                logger.info("事件复燃(#%s):%s", ev.id, ev.primary_title[:40])
            if ev is None:  # 相似度匹配(bigram 预计算,只对当前新条目切一次)
                grams = _bigrams(norm)
                for e in events:
                    ge = ev_grams.get(e.id)
                    if ge and _dice(grams, ge) >= SIM_THRESHOLD:
                        ev = e
                        break
            if ev is None:  # 新事件
                ev = HotspotEvent(user_id=user_id, primary_title=raw_title[:255],
                                  norm_title=norm, platforms=json.dumps([board]),
                                  platform_count=1, first_seen=ts, last_seen=ts,
                                  peak_value=value, peak_at=ts, sample_count=0)  # 统一由 membership 计数
                db.add(ev)
                db.flush()
                by_exact[norm] = ev
                events.append(ev)
                ev_grams[ev.id] = _bigrams(norm)  # 新事件同步登记指纹,后续变体才能匹配到
                created += 1
            else:
                changed = False
                if ev.last_seen < ts:
                    ev.last_seen = ts
                    changed = True
                if value > ev.peak_value:
                    ev.peak_value, ev.peak_at = value, ts
                    changed = True
                plats = json.loads(ev.platforms or "[]")
                if board not in plats:
                    plats.append(board)
                    ev.platforms = json.dumps(plats)
                    ev.platform_count = len(plats)
                    changed = True
                if changed:
                    merged += 1

            pair = (ev.id, board, norm, ts)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            m = db.scalar(select(EventMembership).where(
                EventMembership.user_id == user_id, EventMembership.board == board,
                EventMembership.norm_title == norm, EventMembership.event_id == ev.id))
            if m is None:
                db.add(EventMembership(user_id=user_id, board=board, norm_title=norm,
                                       event_id=ev.id, latest_value=value, last_seen=ts))
                ev.sample_count = (ev.sample_count or 0) + 1
            elif ts > m.last_seen:
                # 增长率:最近两次样本环比(事件级"当前增速"事实)
                if m.latest_value:
                    ev.last_growth = (value - m.latest_value) / m.latest_value
                m.latest_value, m.last_seen = value, ts
                ev.sample_count = (ev.sample_count or 0) + 1
            else:
                continue  # 同一快照行重复归属(重跑 tick):不重复计样本(幂等)

    # 事件终结:独立查询 active 且超 72h 无 seen → ended。
    # (不能遍历活跃列表:活跃窗口 48h < 终结阈值 72h,超窗事件已被查询排除,永不终结)
    stale = db.scalars(select(HotspotEvent).where(
        HotspotEvent.user_id == user_id, HotspotEvent.status == "active",
        HotspotEvent.last_seen < now - timedelta(hours=_END_AFTER_H))).all()
    for e in stale:
        e.status, e.ended_at = "ended", now
        ended += 1

    db.commit()
    # 游标推进到本轮扫描起点+缓冲(用 commit 前的 now,保证严格单调)
    payload = now.isoformat()
    if cursor_row:
        cursor_row.value = payload
    else:
        db.add(SystemConfig(key=f"event_cursor_{user_id}", value=payload))
    db.commit()
    if created or merged or ended:
        logger.info("事件归属完成:新建=%s 归并=%s 终结=%s", created, merged, ended)
    return {"created": created, "merged": merged, "ended": ended}


def list_events(db: Session, user_id: int, limit: int = 50,
                status: str = "active") -> list[dict]:
    """活跃(或已终结)事件列表:按平台数/峰值排序,附最近 seen。"""
    now = datetime.now()
    rows = db.scalars(select(HotspotEvent).where(
        HotspotEvent.user_id == user_id, HotspotEvent.status == status
    ).order_by(HotspotEvent.platform_count.desc(), HotspotEvent.peak_value.desc()
               ).limit(min(int(limit), 200))).all()
    out = []
    for e in rows:
        duration_h = (e.last_seen - e.first_seen).total_seconds() / 3600
        out.append({
            "id": e.id, "title": e.primary_title, "platforms": json.loads(e.platforms or "[]"),
            "platform_count": e.platform_count, "peak_value": e.peak_value,
            "peak_at": e.peak_at.isoformat(sep=" ", timespec="seconds") if e.peak_at else None,
            "first_seen": e.first_seen.isoformat(sep=" ", timespec="seconds"),
            "last_seen": e.last_seen.isoformat(sep=" ", timespec="seconds"),
            "duration_hours": round(duration_h, 1), "sample_count": e.sample_count,
            "status": e.status,
            "reappear_count": e.reappear_count or 0,
            "last_growth": round(e.last_growth, 3) if e.last_growth is not None else None,
            # 简易阶段:最近 2h 有 seen 且平台≥2 → 扩散;仅单平台 → 潜伏/爆发由 Agent 层细化
            "stage": ("扩散" if e.platform_count >= 2 else "单平台")
                     + ("·活跃" if (now - e.last_seen).total_seconds() < 7200 else ""),
        })
    return out


def event_assign_all_users(settings=None) -> None:
    """调度入口:为所有用户各跑一轮归属。"""
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    db = get_session_local()()
    try:
        for (uid,) in db.execute(select(User.id).order_by(User.id)).all():
            try:
                assign_tick(db, uid, settings)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("事件归属失败 user=%s", uid)
    finally:
        db.close()
