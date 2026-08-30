"""监控接口层(对应 doc/API.md)。

暴露:健康检查、最近上涨趋势、最近告警、手动触发管道、最近运行状态。
"""
from __future__ import annotations

import pydantic
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from pathlib import Path

from app.services.pipeline import run_pipeline
from app.storage import ArchiveRepository

APP_VERSION = "1.0.0"
STATIC_DIR = Path(__file__).parent / "static"


class HealthResponse(pydantic.BaseModel):
    status: str
    version: str
    time: str


class TrendItemResponse(pydantic.BaseModel):
    keyword: str
    source: str
    growth: float | None
    slope: float | None
    rising: bool
    decided_at: str


class TrendListResponse(pydantic.BaseModel):
    count: int
    items: list[TrendItemResponse]


class AlertItemResponse(pydantic.BaseModel):
    keyword: str
    reason: str
    triggered_at: str


class AlertListResponse(pydantic.BaseModel):
    count: int
    items: list[AlertItemResponse]


def create_app(data_dir: str = "data") -> FastAPI:
    """构建 FastAPI 应用实例。"""
    repo = ArchiveRepository(data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        repo.close()  # 应用关闭时释放 SQLite 连接,避免 Windows 下文件被占用

    app = FastAPI(title="热点监控系统", version=APP_VERSION, lifespan=lifespan)

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        from datetime import datetime

        return HealthResponse(status="ok", version=APP_VERSION, time=datetime.now().isoformat())

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/v1/trends/latest", response_model=TrendListResponse)
    def trends_latest(limit: int = 20) -> TrendListResponse:
        rows = repo.latest_analysis(limit)
        items = [TrendItemResponse(**row) for row in rows]
        return TrendListResponse(count=len(items), items=items)

    @app.get("/api/v1/alerts/latest", response_model=AlertListResponse)
    def alerts_latest(limit: int = 20) -> AlertListResponse:
        rows = repo.latest_alerts(limit)
        items = [AlertItemResponse(**row) for row in rows]
        return AlertListResponse(count=len(items), items=items)

    @app.post("/api/v1/runs", status_code=202)
    def trigger_run() -> dict:
        # 同步执行一次管道(生产可换为 BackgroundTasks)。
        return run_pipeline(repo=repo)

    @app.get("/api/v1/runs/latest")
    def runs_latest() -> dict:
        return repo.latest_run() or {}

    @app.get("/api/v1/xianyu/hot")
    def xianyu_hot(limit: int = 50) -> dict:
        rows = repo.latest_xianyu(limit)
        return {"count": len(rows), "items": rows}

    @app.post("/api/v1/xianyu/runs", status_code=202)
    def xianyu_run() -> dict:
        from app.services.xianyu import run_xianyu

        return run_xianyu(repo=repo)

    @app.get("/api/v1/xianyu/daily")
    def xianyu_daily() -> dict:
        return repo.latest_xianyu_summary() or {"summary_date": None, "count": 0, "items": []}

    return app


app = create_app()
