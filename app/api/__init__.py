"""平台 API 路由包:按领域拆分的 APIRouter 模块。

⚠️ 原 `quark` 模块已删除(2026-10-03):它只含 `POST /api/quark/shares/collect` 与
`GET /api/quark/shares`,而这两个端点**零调用方**(前端无方法、无页面,调度器也无 quark 作业),
生产库里 `quark_share_stats` 表因此**冻结在 2026-09-29**;结算早已改道 `repost_gain`(盘链扩散)。
用户选定"删端点、保留服务与只读探测脚本"—— 接口沉淀在 `app/services/quark_share_stats.py` 的
模块说明里,复现靠 `scripts/probe_quark_share_stats.py`。
"""
from app.api import admin, alerts, auth, collect, cookies, cross, dashboard, events_api, hotspot, members, misc, wechat, xunlei

all_routers = [
    auth.router,
    cookies.router,
    dashboard.router,
    collect.router,
    alerts.router,
    members.router,
    events_api.router,
    admin.router,
    misc.router,
    wechat.router,
    hotspot.router,
    cross.router,
    xunlei.router,
]

__all__ = ["all_routers", "auth", "cookies", "dashboard", "collect", "alerts", "members", "events_api", "admin", "misc", "wechat", "hotspot", "cross", "xunlei"]
