"""夸克分享统计路由:触发采集 / 查询「我的分享」保存-浏览数据(拉新效果回填)。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import QuarkShareStat, User
from app.services import cookie_store, quark_share_stats
from app.utils import get_logger
from app.utils.net import redact_proxy_creds

logger = get_logger(__name__)

router = APIRouter()


@router.post("/api/quark/shares/collect")
def collect_share_stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """采集当前用户夸克「我的分享」统计并落库(精确回填建议表未人工干预的行)。"""
    cookie = cookie_store.get_cookie(db, user.id, "quark")
    if not cookie:
        raise HTTPException(400, "未配置夸克 Cookie,请先到 Cookie 管理粘贴")
    try:
        return quark_share_stats.collect_for_user(db, user.id, cookie)
    except quark_share_stats.QuarkShareStatsError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("夸克分享统计采集失败(用户=%s)", user.id)
        raise HTTPException(500, f"采集失败:{redact_proxy_creds(str(exc))}") from exc


@router.get("/api/quark/shares")
def list_share_stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """查询已采集的分享统计(按保存数降序)。"""
    rows = db.scalars(select(QuarkShareStat).where(
        QuarkShareStat.user_id == user.id).order_by(QuarkShareStat.save_pv.desc())).all()
    return {
        "total": len(rows),
        "list": [{
            "share_id": r.share_id, "title": r.title, "share_url": r.share_url,
            "save_pv": r.save_pv, "click_pv": r.click_pv, "download_pv": r.download_pv,
            "visit_user_count": r.visit_user_count, "file_num": r.file_num,
            "status": r.status, "audit_status": r.audit_status, "path_info": r.path_info,
            "share_created_at": r.share_created_at.isoformat() if r.share_created_at else None,
            "captured_at": r.captured_at.isoformat() if r.captured_at else None,
        } for r in rows],
    }
