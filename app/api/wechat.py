"""公众号板块路由:文章录入/列表 + 内容选题分析 + 对标号监听/同步。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User, WechatArticle
from app.services.dajiala_client import DajialaError
from app.services.wechat_analyzer import analyze_articles
from app.services import wechat_monitor

router = APIRouter()


def _row_to_dict(r: WechatArticle) -> dict:
    return {"id": r.id, "author": r.author, "title": r.title, "content": r.content,
            "url": r.url, "publish_at": r.publish_at.isoformat() if r.publish_at else None,
            "source": r.source, "pan_types": r.pan_types,
            "pan_urls": r.pan_urls, "my_pan_urls": r.my_pan_urls,
            "benchmark_id": r.benchmark_id,
            "read_num": r.read_num, "zan_num": r.zan_num, "looking_num": r.looking_num,
            "share_num": r.share_num, "collect_num": r.collect_num,
            "comment_count": r.comment_count,
            "traffic_at": r.traffic_at.isoformat(sep=" ", timespec="seconds") if r.traffic_at else None,
            "created_at": r.created_at.isoformat(sep=" ", timespec="seconds") if r.created_at else None}


@router.post("/api/wechat/articles")
def wechat_article_add(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """录入一篇公众号文章(供内容选题分析)。body: {author, title, content?, url?, publish_at?}。"""
    title = str(payload.get("title", "")).strip()
    if not title:
        raise HTTPException(400, "文章标题不能为空")
    pub = payload.get("publish_at")
    pub_dt = None
    if pub:
        try:
            pub_dt = datetime.fromisoformat(str(pub).replace("Z", "+00:00")) if isinstance(pub, str) else pub
        except ValueError:
            pub_dt = None
    db.add(WechatArticle(user_id=user.id, author=str(payload.get("author", "")).strip()[:128],
                         title=title, content=str(payload.get("content", ""))[:100000],
                         url=str(payload.get("url", "")).strip()[:500], publish_at=pub_dt,
                         source="manual"))
    db.commit()
    return {"ok": True, "title": title}


@router.get("/api/wechat/articles")
def wechat_article_list(limit: int = 100, has_pan: int | None = None, benchmark_id: int | None = None,
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """文章列表(新→旧);has_pan=1 只看带网盘链接的,benchmark_id 过滤某对标号。"""
    q = select(WechatArticle).where(WechatArticle.user_id == user.id)
    if benchmark_id:
        q = q.where(WechatArticle.benchmark_id == benchmark_id)
    if has_pan:
        q = q.where(WechatArticle.pan_types != "")
    rows = db.scalars(q.order_by(WechatArticle.created_at.desc()).limit(min(int(limit), 500))).all()
    return {"count": len(rows), "items": [_row_to_dict(r) for r in rows]}


@router.get("/api/wechat/analyze")
def wechat_analyze(limit: int = 200, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """对已录入的公众号文章跑内容选题分析,返回报告。"""
    rows = db.scalars(
        select(WechatArticle).where(WechatArticle.user_id == user.id)
        .order_by(WechatArticle.publish_at.desc().nulls_last()).limit(min(int(limit), 500))
    ).all()
    articles = [{"title": r.title, "content": r.content, "author": r.author, "publish_at": r.publish_at} for r in rows]
    return {"articles": len(articles), **analyze_articles(articles)}


# ---------------------------------------------------------------- 对标号:监听/同步
@router.get("/api/wechat/benchmarks")
def wechat_benchmark_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """对标账号列表(含活跃度/是否有文章)。"""
    items = wechat_monitor.list_benchmarks(db, user.id)
    return {"count": len(items), "items": items}


@router.post("/api/wechat/benchmarks")
def wechat_benchmark_add(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """贴该号任意一篇**文章链接**即加号(body: {url, nickname?, note?});链接同时是监听锚点。"""
    try:
        row = wechat_monitor.add_benchmark(db, user.id, str(payload.get("url", "")),
                                           nickname=str(payload.get("nickname", "")),
                                           note=str(payload.get("note", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **row}


@router.patch("/api/wechat/benchmarks/{benchmark_id}")
def wechat_benchmark_update(benchmark_id: int, payload: dict,
                            user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """更新对标号(当前支持 active 开关)。"""
    try:
        wechat_monitor.set_benchmark_active(db, user.id, benchmark_id, bool(payload.get("active", True)))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


@router.delete("/api/wechat/benchmarks/{benchmark_id}")
def wechat_benchmark_remove(benchmark_id: int, user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    """删除对标号(已同步的文章保留)。"""
    try:
        wechat_monitor.remove_benchmark(db, user.id, benchmark_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


@router.post("/api/wechat/benchmarks/{benchmark_id}/sync")
def wechat_benchmark_sync(benchmark_id: int, max_pages: int | None = None,
                          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """一键同步该号历史文章(history_by_ghid 翻页,¥0.14/页,默认 WECHAT_SYNC_MAX_PAGES 封顶)。"""
    try:
        return wechat_monitor.sync_wechat_account(db, user.id, benchmark_id, max_pages=max_pages)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except DajialaError as exc:
        raise HTTPException(502, str(exc))


@router.post("/api/wechat/benchmarks/import_shelf")
def wechat_benchmark_import_shelf(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """从微信读书书架一键导入对标号(免费):需在微信读书内先关注目标号 + 配置「weread」Cookie。"""
    return wechat_monitor.import_benchmarks_from_shelf(db, user.id)


@router.get("/api/wechat/weread/shelf")
def wechat_weread_shelf(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """列出微信读书书架上的公众号(导入预览)。"""
    try:
        items = wechat_monitor.weread_shelf(db, user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001 - 会话失效/风控等
        raise HTTPException(502, f"微信读书书架获取失败:{exc}")
    return {"count": len(items), "items": items}


@router.post("/api/wechat/traffic/refresh")
def wechat_traffic_refresh(payload: dict | None = None, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    """刷新阅读量(dajiala read_zan_pro,¥0.06/篇)。body 可选: {benchmark_id?, limit?}。"""
    payload = payload or {}
    try:
        return wechat_monitor.sample_traffic(db, user.id,
                                             benchmark_id=payload.get("benchmark_id"),
                                             limit=payload.get("limit"))
    except DajialaError as exc:
        raise HTTPException(502, str(exc))


@router.get("/api/wechat/articles/{article_id}/traffic")
def wechat_article_traffic(article_id: int, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    """单篇文章的流量采样曲线(供增长折线)。"""
    from app.db.models import WechatTrafficSample

    row = db.scalar(select(WechatArticle).where(WechatArticle.id == article_id,
                                                WechatArticle.user_id == user.id))
    if row is None:
        raise HTTPException(404, "文章不存在")
    samples = db.scalars(select(WechatTrafficSample).where(
        WechatTrafficSample.user_id == user.id,
        WechatTrafficSample.article_id == article_id).order_by(WechatTrafficSample.sampled_at)).all()
    return {"article_id": article_id, "count": len(samples), "items": [
        {"read_num": s.read_num, "zan_num": s.zan_num, "looking_num": s.looking_num,
         "share_num": s.share_num, "collect_num": s.collect_num,
         "comment_count": s.comment_count,
         "sampled_at": s.sampled_at.isoformat(sep=" ", timespec="seconds")} for s in samples]}


@router.post("/api/wechat/listen")
def wechat_listen(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """手动触发一轮监听(全部启用中的对标号各查一次"当天发文",新文入库+推公众号群)。"""
    try:
        return wechat_monitor.run_wechat_listen(db, user.id)
    except DajialaError as exc:
        raise HTTPException(502, str(exc))
