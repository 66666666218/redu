"""多租户平台 API(前后端分离的后端)。

- `/api/auth/*` 注册/登录/当前用户(JWT)。
- `/api/cookies/*` 用户自行配置各平台 Cookie。
- `/api/collect/{platform}` 用该用户 Cookie 触发采集(按 user_id 存)。
- `/api/dashboard` / `/api/xianyu/daily` 该用户数据。
- `/` 托管前端构建产物(frontend/dist,存在时)。
"""
from __future__ import annotations

from pathlib import Path
import time

import pydantic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, init_db
from app.db.models import User
from app.auth import authenticate, create_password_reset_token, get_current_user, register_user, reset_password
from app.security import create_access_token
from app.services import tenant
from app.services.cookie_store import delete_cookie as del_cookie
from app.services.cookie_store import list_cookies, set_cookie
from app.services.notifier import get_notifier

APP_VERSION = "2.0.0"

# 轻量登录限速(内存滑窗,防爆破)
_LOGIN_WINDOW = 600
_LOGIN_MAX = 10
_login_attempts: dict[str, list[float]] = {}


def _login_allowed(key: str) -> bool:
    now = time.time()
    arr = [t for t in _login_attempts.get(key, []) if now - t < _LOGIN_WINDOW]
    _login_attempts[key] = arr
    return len(arr) < _LOGIN_MAX


def _record_login(key: str) -> None:
    _login_attempts.setdefault(key, []).append(time.time())


def _clear_login(key: str) -> None:
    _login_attempts.pop(key, None)


class RegisterIn(pydantic.BaseModel):
    email: str
    password: str
    username: str | None = None


class LoginIn(pydantic.BaseModel):
    login: str
    password: str


class ForgotIn(pydantic.BaseModel):
    email: str


class ResetIn(pydantic.BaseModel):
    token: str
    new_password: str


class AlertRuleIn(pydantic.BaseModel):
    section: str
    rule_type: str
    metric: str | None = None
    threshold: float | None = None
    keyword: str | None = None
    alert_time: str | None = None


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
    def register(body: RegisterIn, db: Session = Depends(get_db)) -> TokenOut:
        user = register_user(db, body.email, body.password, body.username)
        token = create_access_token(user.id)
        return TokenOut(token=token, username=user.username)

    @app.post("/api/auth/login", response_model=TokenOut)
    def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
        key = f"{request.client.host if request.client else '?'}:{body.login.strip().lower()}"
        if not _login_allowed(key):
            raise HTTPException(429, "登录尝试过多,请 10 分钟后再试")
        token = authenticate(db, body.login, body.password)
        if token is None:
            _record_login(key)
            raise HTTPException(401, "账号或密码错误")
        _clear_login(key)
        return TokenOut(token=token, username=body.login.strip())

    @app.post("/api/auth/forgot")
    def forgot(body: ForgotIn, db: Session = Depends(get_db)):
        from config.settings import get_settings

        settings = get_settings()
        token = create_password_reset_token(db, body.email)
        # 不暴露邮箱是否存在,统一提示
        if token:
            try:
                notifier = get_notifier(settings)
                link = f"{settings.public_base_url}/reset?token={token}"
                notifier.send(
                    "重置密码",
                    f"请点击以下链接重置密码(30 分钟内有效):\n\n{link}",
                )
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning("发送重置邮件失败:%s", exc)
        return {"message": "如果该邮箱已注册,重置邮件已发送"}

    @app.post("/api/auth/reset")
    def reset(body: ResetIn, db: Session = Depends(get_db)):
        ok = reset_password(db, body.token, body.new_password)
        if not ok:
            raise HTTPException(400, "重置链接无效或已过期")
        return {"message": "密码已重置,请重新登录"}

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

    @app.post("/api/xianyu/collect-deep")
    def xianyu_collect_deep(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        try:
            return tenant.run_xianyu_deep(db, user.id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"采集失败:{exc}") from exc

    @app.get("/api/xianyu/analytics")
    def xianyu_analytics(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        return tenant.xianyu_analytics(db, user.id)

    @app.post("/api/douhot/watch")
    def douhot_watch_add(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        return tenant.add_douhot_watch(db, user.id, str(payload.get("list_type", "word")), str(payload.get("keyword", "")))

    @app.get("/api/douhot/watch")
    def douhot_watch_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        return tenant.list_douhot_watch(db, user.id)

    @app.get("/api/douhot/watch-analytics")
    def douhot_watch_analytics(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        return tenant.douhot_watch_analytics(db, user.id)

    # ---- 预警规则(每板块) ----
    @app.get("/api/alerts/rules")
    def alerts_rules(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        from app.services import alert_service

        return alert_service.list_rules(db, user.id)

    @app.post("/api/alerts/rules")
    def alerts_rule_add(payload: AlertRuleIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        from app.services import alert_service

        try:
            r = alert_service.add_rule(
                db, user.id, payload.section, payload.rule_type,
                payload.metric, payload.threshold, payload.keyword, payload.alert_time,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": r.id, "section": r.section, "rule_type": r.rule_type}

    @app.delete("/api/alerts/rules/{rule_id}")
    def alerts_rule_del(rule_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        from app.services import alert_service

        return {"deleted": alert_service.delete_rule(db, user.id, rule_id)}

    @app.get("/api/alerts/list")
    def alerts_list(limit: int = 30, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        from app.db.models import AlertRecord

        rows = db.scalars(
            select(AlertRecord).where(AlertRecord.user_id == user.id).order_by(AlertRecord.id.desc()).limit(limit)
        ).all()
        return [{"keyword": r.keyword, "reason": r.reason, "time": r.triggered_at.isoformat()} for r in rows]

    # 托管前端构建产物(SPA)
    dist = Path(__file__).parent / "static" / "spa"
    if dist.exists():
        @app.get("/", response_class=HTMLResponse)
        def index() -> str:
            return (dist / "index.html").read_text(encoding="utf-8")

        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    return app


app = create_app()
