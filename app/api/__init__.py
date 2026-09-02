"""平台 API 路由包:按领域拆分的 APIRouter 模块。"""
from app.api import admin, alerts, auth, collect, cookies, dashboard, misc

all_routers = [
    auth.router,
    cookies.router,
    dashboard.router,
    collect.router,
    alerts.router,
    admin.router,
    misc.router,
]

__all__ = ["all_routers", "auth", "cookies", "dashboard", "collect", "alerts", "admin", "misc"]
