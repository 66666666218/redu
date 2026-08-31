"""多租户平台 API(前后端分离的后端)。

- `/api/auth/*` 注册/登录/当前用户(JWT)。
- `/api/cookies/*` 用户自行配置各平台 Cookie。
- `/api/collect/{platform}` 用该用户 Cookie 触发采集(按 user_id 存)。
- `/api/dashboard` / `/api/xianyu/daily` 该用户数据。
- `/` 托管前端构建产物(frontend/dist,存在时)。
"""
from __future__ import annotations

from pathlib import Path

import pydantic
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.db import get_db, init_db
from app.db.models import User
from app.auth import authenticate, get_current_user, register_user
from app.security import create_access_token
from app.services import tenant
from app.services.cookie_store import delete_cookie as del_cookie
from app.services.cookie_store import list_cookies, set_cookie

APP_VERSION = "2.0.0"


class AuthIn(pydantic.BaseModel):
    username: str
    password: str


class TokenOut(pydantic.BaseModel):
    token: str
    username: str


class MeOut(pydantic.BaseModel):
    id: int
    username: str


class CookieIn(pydantic.BaseModel):
    cookie: str


class CookieOut(pydantic.BaseModel):
    platform: str
    configured: bool
    preview: str = ""
    updated_at: str | None = None


def create_app() -> FastAPI:
    app = FastAPI(title="热点监控平台", version=APP_VERSION)
    init_db()

    @app.get("/healthz")
    def healthz() -> dict:
        from datetime import datetime

        return {"status": "ok", "version": APP_VERSION, "time": datetime.now().isoformat()}

    @app.post("/api/auth/register", response_model=TokenOut)
    def register(body: AuthIn, db: Session = Depends(get_db)) -> TokenOut:
        user = register_user(db, body.username.strip(), body.password)
        token = create_access_token(user.id)
        return TokenOut(token=token, username=user.username)

    @app.post("/api/auth/login", response_model=TokenOut)
    def login(body: AuthIn, db: Session = Depends(get_db)) -> TokenOut:
        token = authenticate(db, body.username.strip(), body.password)
        if token is None:
            raise HTTPException(401, "用户名或密码错误")
        return TokenOut(token=token, username=body.username.strip())

    @app.get("/api/auth/me", response_model=MeOut)
    def me(user: User = Depends(get_current_user)) -> MeOut:
        return MeOut(id=user.id, username=user.username)

    @app.get("/api/cookies", response_model=list[CookieOut])
    def cookies_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[CookieOut]:
        return [CookieOut(**c) for c in list_cookies(db, user.id)]

    @app.put("/api/cookies/{platform}", response_model=CookieOut)
    def cookies_put(platform: str, body: CookieIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> CookieOut:
        try:
            set_cookie(db, user.id, platform, body.cookie.strip())
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return CookieOut(platform=platform, configured=bool(body.cookie.strip()))

    @app.delete("/api/cookies/{platform}")
    def cookies_del(platform: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
        del_cookie(db, user.id, platform)
        return {"platform": platform, "deleted": True}

    @app.post("/api/collect/{platform}")
    def collect(platform: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        if platform not in ("weibo", "xianyu", "douhot"):
            raise HTTPException(400, "不支持的平台")
        try:
            runner = {"weibo": tenant.run_weibo, "xianyu": tenant.run_xianyu, "douhot": tenant.run_douhot}[platform]
            return runner(db, user.id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"采集失败:{exc}") from exc

    @app.get("/api/dashboard")
    def dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        return tenant.dashboard(db, user.id)

    @app.get("/api/xianyu/daily")
    def xianyu_daily(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        return tenant.xianyu_daily(db, user.id)

    # 托管前端构建产物(SPA)
    dist = Path(__file__).parent / "static" / "spa"
    if dist.exists():
        @app.get("/", response_class=HTMLResponse)
        def index() -> str:
            return (dist / "index.html").read_text(encoding="utf-8")

        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    return app


app = create_app()
