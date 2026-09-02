"""多租户平台应用工厂(前后端分离后端的装配层)。

路由按领域拆在 `app/api/`(auth/cookies/dashboard/collect/alerts/admin/misc);
本文件只负责**装配**:初始化日志/数据库、注册路由与中间件、托管前端构建产物。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import APP_VERSION
from app.api import all_routers
from app.db import init_db
from app.services import scheduler
from app.utils import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """应用启停:随 API 进程启动后台调度器(按各用户设置的频率采集)。"""
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


def create_app() -> FastAPI:
    # 生产用 `uvicorn app.platform:app` 直启,不经 app/main.py;须先初始化日志,
    # 否则 root logger 无 handler:INFO 全丢、异常堆栈无格式,线上难排查。
    setup_logging()
    app = FastAPI(title="热点监控平台", version=APP_VERSION, lifespan=_lifespan)

    from config.settings import Settings, get_settings

    _settings = get_settings()
    # 漏配 DATABASE_URL 时到启动日志说清楚,避免服务照常起、直到有人点注册才 500。
    if _settings.database_url == Settings.model_fields["database_url"].default:
        from urllib.parse import urlsplit

        logger.warning(
            "⚠️ 未配置 DATABASE_URL,正在使用默认值(主机 %s);若该地址上没有数据库,"
            "所有涉及读写的接口都会 OperationalError。请在 .env 或容器环境变量中设置 DATABASE_URL,"
            "并确认数据库容器已启动;可访问 /healthz 查看 db 状态",
            urlsplit(_settings.database_url).hostname,
        )
    init_db()

    if not _settings.jwt_secret:
        logger.warning("⚠️ 未配置 JWT_SECRET,已用临时密钥(生产请设置强随机 ≥32 字节,否则重启后登录态失效)")

    @app.middleware("http")
    async def log_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        import time

        import logging

        t0 = time.time()
        response = await call_next(request)
        dur = (time.time() - t0) * 1000
        logging.getLogger("access").info("%s %s -> %s %.0fms", request.method, request.url.path, response.status_code, dur)
        return response

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception):  # type: ignore[no-untyped-def]
        """兜底:记录完整堆栈并返回带异常类型的中文提示(不向用户暴露裸的 Internal Server Error)。"""
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"服务器内部错误({type(exc).__name__}),请稍后重试;详情见服务日志"},
        )

    # 按领域注册路由(顺序无关;APIRouter 内部路径已含 /api 前缀)
    for router in all_routers:
        app.include_router(router)

    # 托管前端构建产物(SPA)
    dist = Path(__file__).parent / "static" / "spa"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/", response_class=HTMLResponse)
        def index() -> str:
            return (dist / "index.html").read_text(encoding="utf-8")

        # SPA history 路由回退:直接访问/刷新 /login 等必须回退到 index.html。
        # 注册在所有 API 路由之后;healthz/docs 等都是真实路由会先匹配,
        # 这里只需放过 API 与 assets 前缀,让接口路径仍返回 JSON 404。
        @app.get("/{full_path:path}", response_class=HTMLResponse)
        def spa_fallback(full_path: str) -> str:
            if full_path.startswith(("api/", "assets/")):
                raise HTTPException(404, "Not Found")
            return (dist / "index.html").read_text(encoding="utf-8")

    return app


app = create_app()
