"""热点建议路由:一键标记「已发」+ 结算 + 拉新周录(预测→下注→结算闭环的 API 面)。"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import HotspotSuggestion, PanRecruitWeekly, User
from app.services import hotspot_agent

router = APIRouter()


class ActedIn(BaseModel):
    acted: bool = True


class RecruitIn(BaseModel):
    week_start: str          # 统计周期起始日 YYYY-MM-DD(默认按拉新后台口径,一般周一)
    recruits: int
    # **分渠道明细**(2026-10-03 用户口径:"我只能给你我的",且要分渠道):
    # 如 `{"douyin": 42, "wechat": 18}`。只有分开录,才能分别对账两条链;
    # 给了明细就以**明细之和**为准(免得总数和明细打架)。
    channels: dict[str, int] = {}
    note: str = ""


@router.post("/api/hotspot/suggestions/{sid}/acted")
def mark_acted(sid: int, payload: ActedIn | None = None,
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """一键标记某条建议「已发」(下注)。

    只有 acted 的建议 + 夸克 save_pv 结算,才构成 Agent 的"预测→结算"学习样本;
    没执行的建议不进样本(否则把"没发"误学成"发了没效果")。
    """
    row = db.scalar(select(HotspotSuggestion).where(
        HotspotSuggestion.id == sid, HotspotSuggestion.user_id == user.id))
    if row is None:
        raise HTTPException(404, "建议不存在")
    acted = bool(payload.acted) if payload else True
    row.acted = acted
    row.acted_at = datetime.now() if acted else None
    db.commit()
    return {"status": "ok", "id": row.id, "keyword": row.keyword, "acted": row.acted}


@router.post("/api/hotspot/suggestions/{sid}/draft")
def suggestion_draft(sid: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """按建议生成可发布文案(标题×3+正文;资源库有现成我方链会自动带上)。"""
    from app.services import hotspot_agent as _ha

    out = _ha.generate_draft(db, user.id, sid)
    if out.get("status") == "not_found":
        raise HTTPException(404, "建议不存在")
    if out.get("status") == "no_llm_key":
        raise HTTPException(400, "未配置 DEEPSEEK_API_KEY,无法生成文案")
    if out.get("status") == "failed":
        raise HTTPException(502, "AI 文案生成失败,请稍后重试")
    return out


@router.get("/api/hotspot/hot-rank")
def hot_rank(per: int = 10, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """多平台热榜(v2.10.0 前端入口):各源最新一轮 top N(B站/豆瓣自研 + newsnow 长尾)。"""
    from sqlalchemy import func, select

    from app.db.models import HotSourceItem
    from app.services.hot_sources import _PLAT_LABEL

    per = max(1, min(int(per or 10), 30))
    latest = db.execute(
        select(HotSourceItem.source, func.max(HotSourceItem.captured_at))
        .where(HotSourceItem.user_id == user.id)
        .group_by(HotSourceItem.source)).all()
    out = []
    for src, ts in latest:
        rows = db.scalars(select(HotSourceItem).where(
            HotSourceItem.user_id == user.id, HotSourceItem.source == src,
            HotSourceItem.captured_at == ts).order_by(HotSourceItem.rank).limit(per)).all()
        out.append({
            "source": str(src), "label": _PLAT_LABEL.get(str(src), str(src)),
            "captured_at": ts.isoformat(sep=" ", timespec="seconds") if ts else "",
            "items": [{"rank": r.rank, "title": r.title, "url": r.url, "extra": r.extra} for r in rows],
        })
    # 自研优先(B站/豆瓣)在前,其余按平台名
    out.sort(key=lambda p: (p["source"] not in ("bilibili", "douban"), p["source"]))
    return {"platforms": out, "count": len(out)}


@router.get("/api/hotspot/suggestions")
def list_suggestions(limit: int = 50, acted: bool | None = None,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """建议回看列表(按时间倒序;?acted=true 只看已发,供效果结算分析)。"""
    q = select(HotspotSuggestion).where(HotspotSuggestion.user_id == user.id)
    if acted is not None:
        q = q.where(HotspotSuggestion.acted == acted)
    rows = db.scalars(q.order_by(HotspotSuggestion.created_at.desc()).limit(min(limit, 200))).all()
    return {"total": len(rows), "list": [{
        "id": r.id, "keyword": r.keyword, "kind": r.kind, "growth": r.growth,
        "platforms": r.platforms, "opportunity": r.opportunity,
        "resource_title": r.resource_title, "link": r.link, "plan": r.plan,
        "saves": r.saves, "saves_at": r.saves_at.isoformat() if r.saves_at else None,
        "acted": r.acted, "acted_at": r.acted_at.isoformat() if r.acted_at else None,
        "article_id": r.article_id, "reads_gain": r.reads_gain,
        "repost_gain": r.repost_gain,
        "settled_at": r.settled_at.isoformat() if r.settled_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]}


@router.post("/api/hotspot/settle")
def settle(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """结算已发建议的盘链扩散增量(自动跑在每日 22:00,此接口供手动补跑)。"""
    try:
        return hotspot_agent.settle_suggestions(db, user.id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"结算失败:{type(exc).__name__}") from exc


@router.post("/api/hotspot/recruits")
def upsert_recruit(payload: RecruitIn, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """录入/更新夸克官方拉新后台的周度拉新数(方案B 总账,人工周录)。

    给了 `channels` 就**以明细之和为准**(避免总数与明细对不上,后面没法对账)。
    """
    try:
        week = datetime.strptime(payload.week_start, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(400, "week_start 需为 YYYY-MM-DD") from exc
    chans = {str(k)[:16]: max(0, int(v)) for k, v in (payload.channels or {}).items()}
    row = db.scalar(select(PanRecruitWeekly).where(
        PanRecruitWeekly.user_id == user.id, PanRecruitWeekly.week_start == week))
    if row is None:
        row = PanRecruitWeekly(user_id=user.id, week_start=week)
        db.add(row)
    row.recruits = sum(chans.values()) if chans else max(0, payload.recruits)
    row.channels = json.dumps(chans, ensure_ascii=False) if chans else ""
    row.note = payload.note[:255]
    db.commit()
    return {"status": "ok", "id": row.id, "week_start": payload.week_start,
            "recruits": row.recruits, "channels": chans}


@router.get("/api/hotspot/recruits")
def list_recruits(limit: int = 12, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """拉新周录列表(最近在前),供与建议侧信号对账。"""
    rows = db.scalars(select(PanRecruitWeekly).where(
        PanRecruitWeekly.user_id == user.id).order_by(
        PanRecruitWeekly.week_start.desc()).limit(min(limit, 52))).all()
    return {"total": len(rows), "list": [{
        "week_start": r.week_start.strftime("%Y-%m-%d"), "recruits": r.recruits,
        "channels": _channels_json(r.channels),
        "note": r.note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]}


def _channels_json(raw: str) -> dict:
    """周录的分渠道明细(老行没有 → 空表,前端据此显示"未分渠道")。"""
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return {str(k): v for k, v in d.items()} if isinstance(d, dict) else {}


@router.get("/api/hotspot/leads/settlement")
def leads_settlement(weeks: int = 8, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """**线索结算对账**:系统侧(线索数/转发量)与人工周录真值,按周并排。

    ⚠️ 两边**不同源**(转发量是别人视频的,周录是我们自己号的拉新)——
    只看趋势是否同步,**别拿它们相除当转化率**(见 `lead_settlement` 的模块说明)。
    """
    from app.services import lead_settlement

    return lead_settlement.weekly_report(db, user.id, weeks=max(1, min(int(weeks), 26)))
