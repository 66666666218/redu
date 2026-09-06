"""公众号板块路由:文章录入 + 内容选题分析(暂不含流量数据)。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User, WechatArticle
from app.services.wechat_analyzer import analyze_articles

router = APIRouter()


def _row_to_dict(r: WechatArticle) -> dict:
    return {"id": r.id, "author": r.author, "title": r.title, "content": r.content,
            "url": r.url, "publish_at": r.publish_at.isoformat() if r.publish_at else None}


@router.post("/api/wechat/articles")
def wechat_article_add(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """录入一篇公众号文章(供内容选题分析)。body: {author, title, content?, url?, publish_at?}。"""
    title = str(payload.get("title", "")).strip()
    if not title:
        raise HTTPException(400, "文章标题不能为空")
    from datetime import datetime

    pub = payload.get("publish_at")
    pub_dt = None
    if pub:
        try:
            pub_dt = datetime.fromisoformat(str(pub).replace("Z", "+00:00")) if isinstance(pub, str) else pub
        except ValueError:
            pub_dt = None
    db.add(WechatArticle(user_id=user.id, author=str(payload.get("author", "")).strip()[:128],
                         title=title, content=str(payload.get("content", ""))[:100000],
                         url=str(payload.get("url", "")).strip()[:500], publish_at=pub_dt))
    db.commit()
    return {"ok": True, "title": title}


@router.get("/api/wechat/analyze")
def wechat_analyze(limit: int = 200, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """对已录入的公众号文章跑内容选题分析,返回报告。"""
    rows = db.scalars(
        select(WechatArticle).where(WechatArticle.user_id == user.id)
        .order_by(WechatArticle.publish_at.desc().nulls_last()).limit(min(int(limit), 500))
    ).all()
    articles = [{"title": r.title, "content": r.content, "author": r.author, "publish_at": r.publish_at} for r in rows]
    return {"articles": len(articles), **analyze_articles(articles)}
