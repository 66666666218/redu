"""管理后台路由:/api/admin/*(require_admin + 按钮级权限)。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.db.models import User
from app import admin as admin_svc
from app.api.deps import ConfigIn, require_perm

router = APIRouter()


@router.get("/api/admin/me")
def admin_me(user: User = Depends(require_admin)):
    return {"role": user.role, "username": user.username, "perms": admin_svc.perms_list(user.role)}


@router.get("/api/admin/dashboard")
def admin_dashboard(user: User = Depends(require_perm("dashboard.view")), db: Session = Depends(get_db)):
    return admin_svc.dashboard(db)


@router.get("/api/admin/insights")
def admin_insights(user: User = Depends(require_perm("data.view")), db: Session = Depends(get_db)):
    return admin_svc.insights(db)


@router.get("/api/admin/users")
def admin_users(q: str = "", user: User = Depends(require_perm("users.view")), db: Session = Depends(get_db)):
    return admin_svc.list_users(db, q)


@router.post("/api/admin/users/{user_id}/toggle")
def admin_users_toggle(user_id: int, user: User = Depends(require_perm("users.toggle")), db: Session = Depends(get_db)):
    res = admin_svc.toggle_user(db, user_id)
    if res is None:
        raise HTTPException(404, "用户不存在")
    admin_svc.log_admin(db, user, "toggle_user", f"user#{user_id}", f"enabled={res['enabled']}")
    return res


@router.delete("/api/admin/users/{user_id}")
def admin_users_del(user_id: int, user: User = Depends(require_perm("users.delete")), db: Session = Depends(get_db)):
    if user_id == user.id:
        raise HTTPException(400, "不能删除自己")
    ok = admin_svc.delete_user(db, user_id)
    if not ok:
        raise HTTPException(404, "用户不存在")
    admin_svc.log_admin(db, user, "delete_user", f"user#{user_id}")
    return {"deleted": True}


@router.get("/api/admin/logins")
def admin_logins(user: User = Depends(require_perm("logs.view")), db: Session = Depends(get_db)):
    return admin_svc.list_logins(db)


@router.get("/api/admin/logs")
def admin_logs(user: User = Depends(require_perm("logs.view")), db: Session = Depends(get_db)):
    return admin_svc.list_admin_logs(db)


@router.get("/api/admin/config")
def admin_config(user: User = Depends(require_perm("config.view")), db: Session = Depends(get_db)):
    return admin_svc.config_get(db)


@router.put("/api/admin/config/{key}")
def admin_config_set(key: str, body: ConfigIn, user: User = Depends(require_perm("config.set")), db: Session = Depends(get_db)):
    admin_svc.config_set(db, key, body.value)
    admin_svc.log_admin(db, user, "set_config", key, body.value)
    return {"key": key, "value": body.value}


@router.get("/api/admin/export/users")
def admin_export_users(user: User = Depends(require_perm("data.export")), db: Session = Depends(get_db)):
    return PlainTextResponse(admin_svc.export_users(db), media_type="text/csv")


@router.get("/api/admin/export/alerts")
def admin_export_alerts(user: User = Depends(require_perm("data.export")), db: Session = Depends(get_db)):
    return PlainTextResponse(admin_svc.export_alerts(db), media_type="text/csv")


@router.get("/api/admin/users/{user_id}")
def admin_user_detail(user_id: int, user: User = Depends(require_perm("users.view")), db: Session = Depends(get_db)):
    detail = admin_svc.user_detail(db, user_id)
    if detail is None:
        raise HTTPException(404, "用户不存在")
    return detail


@router.post("/api/admin/import/users")
def admin_import_users(body: dict, user: User = Depends(require_perm("users.import")), db: Session = Depends(get_db)):
    text = str(body.get("text", ""))
    res = admin_svc.import_users(db, text)
    admin_svc.log_admin(db, user, "import_users", f"created={res['created']} skipped={res['skipped']}")
    return res


@router.get("/api/admin/data/{section}")
def admin_data(section: str, user_id: int | None = None, limit: int = 50,
               user: User = Depends(require_perm("data.view")), db: Session = Depends(get_db)):
    return admin_svc.data_browse(db, section, user_id, limit)


@router.get("/api/admin/categories")
def admin_categories(user: User = Depends(require_perm("dashboard.view")), db: Session = Depends(get_db)):
    return admin_svc.category_dist(db)


@router.get("/api/admin/runs/failed")
def admin_runs_failed(user: User = Depends(require_perm("logs.view")), db: Session = Depends(get_db)):
    return admin_svc.failed_runs(db)


@router.get("/api/admin/alert-trend")
def admin_alert_trend(days: int = 30, user: User = Depends(require_perm("dashboard.view")), db: Session = Depends(get_db)):
    return admin_svc.alert_trend(db, days)


@router.get("/api/admin/category-pie")
def admin_category_pie(user: User = Depends(require_perm("dashboard.view")), db: Session = Depends(get_db)):
    return admin_svc.category_pie(db)


@router.post("/api/admin/runs/{run_id}/retry")
def admin_run_retry(run_id: str, user: User = Depends(require_perm("users.toggle")), db: Session = Depends(get_db)):
    res = admin_svc.retry_run(db, run_id)
    admin_svc.log_admin(db, user, "retry_run", run_id, "ok" if res.get("ok") else str(res.get("msg", "")))
    return res
