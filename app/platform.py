"""多租户平台 API(前后端分离的后端)。

- `/api/auth/*` 注册/登录/当前用户(JWT)。
- `/api/cookies/*` 用户自行配置各平台 Cookie。
- `/api/collect/{platform}` 用该用户 Cookie 触发采集(按 user_id 存)。
- `/api/dashboard` / `/api/xianyu/daily` 该用户数据。
- `/` 托管前端构建产物(frontend/dist,存在时)。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import time

import pydantic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_db, init_db
from app.db.models import User
from app.auth import authenticate, create_password_reset_token, get_current_user, log_login, register_user, reset_password, require_admin
from app.security import create_access_token
from app.services import tenant
from app.services import scheduler
from app.services import schedule_service
from app import admin as admin_svc
from app.services.cookie_store import delete_cookie as del_cookie
from app.services.cookie_store import list_cookies, set_cookie
from app.services.notifier import get_notifier
from app.utils import get_logger, setup_logging

logger = get_logger(__name__)

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


class UserSmtpIn(pydantic.BaseModel):
    host: str = ""
    port: int = 465
    user: str = ""
    password: str = ""
    from_name: str = ""


class TokenOut(pydantic.BaseModel):
    token: str
    username: str


class MeOut(pydantic.BaseModel):
    id: int
    username: str
    role: str = "user"


class CookieIn(pydantic.BaseModel):
    cookie: str


class CookieOut(pydantic.BaseModel):
    platform: str
    configured: bool
    preview: str = ""
    updated_at: str | None = None


class ScheduleIn(pydantic.BaseModel):
    """采集频率设置(两个字段均可单独提交)。"""

    interval_minutes: int | None = None
    enabled: bool | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """应用启停:随 API 进程启动后台调度器(按各用户设置的频率采集)。

    单容器部署即可,无需额外调度容器;`SCHEDULER_ENABLED=false` 可关闭
    (多 worker 部署时必须关掉,否则每个 worker 都会重复采集)。
    """
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


def create_app() -> FastAPI:
    # 生产用 `uvicorn app.platform:app` 直启,不经过 app/main.py,
    # 若不在这里初始化,root logger 就没有 handler:INFO 日志全丢、异常堆栈也没有格式,
    # 出问题时根本没法从容器日志定位。
    setup_logging()
    app = FastAPI(title="热点监控平台", version=APP_VERSION, lifespan=_lifespan)

    from config.settings import Settings, get_settings

    _settings = get_settings()
    # 漏配 DATABASE_URL 时要在启动日志里立刻说清楚:否则服务照常起、healthcheck 照常绿,
    # 直到有人点注册才报 OperationalError,排查成本极高(线上真实踩过)。
    if _settings.database_url == Settings.model_fields["database_url"].default:
        from urllib.parse import urlsplit

        logger.warning(
            "⚠️ 未配置 DATABASE_URL,正在使用默认值(主机 %s);若该地址上没有数据库,"
            "所有涉及读写的接口都会 OperationalError。请在 .env 或容器环境变量中设置 DATABASE_URL,"
            "并确认数据库容器已启动;可访问 /healthz 查看 db 状态",
            urlsplit(_settings.database_url).hostname,  # 只打主机名,不泄露账号密码
        )
    init_db()

    if not _settings.jwt_secret:
        logger.warning(
            "⚠️ 未配置 JWT_SECRET,已用临时密钥(生产请设置强随机 ≥32 字节,否则重启后登录态失效)"
        )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        import time

        t0 = time.time()
        response = await call_next(request)
        dur = (time.time() - t0) * 1000
        import logging

        logging.getLogger("access").info("%s %s -> %s %.0fms", request.method, request.url.path, response.status_code, dur)
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        """健康检查(含数据库自查)。

        故意**始终返回 200**:容器 healthcheck 用的就是本接口,不能因为库暂时
        不可用就把容器判成 unhealthy;数据库状况看返回体里的 `db` 字段。
        """
        from datetime import datetime

        from app.db.database import db_status

        return {
            "status": "ok",
            "version": APP_VERSION,
            "time": datetime.now().isoformat(),
            "db": db_status(),
        }

    @app.post("/api/auth/register", response_model=TokenOut)
    def register(body: RegisterIn, db: Session = Depends(get_db)) -> TokenOut:
        user = register_user(db, body.email, body.password, body.username)
        token = create_access_token(user.id)
        return TokenOut(token=token, username=user.username)

    @app.post("/api/auth/login", response_model=TokenOut)
    def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
        key = f"{request.client.host if request.client else '?'}:{body.login.strip().lower()}"
        if not _login_allowed(key):
            log_login(db, None, body.login.strip(), request.client.host if request.client else "?", request.headers.get("user-agent", ""), False)
            raise HTTPException(429, "登录尝试过多,请 10 分钟后再试")
        token = authenticate(db, body.login, body.password)
        ip = request.client.host if request.client else "?"
        if token is None:
            _record_login(key)
            log_login(db, None, body.login.strip(), ip, request.headers.get("user-agent", ""), False)
            raise HTTPException(401, "账号或密码错误")
        _clear_login(key)
        uid = db.scalar(select(User.id).where(or_(User.username == body.login.strip(), User.email == body.login.strip().lower())))
        log_login(db, uid, body.login.strip(), ip, request.headers.get("user-agent", ""), True)
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
        return MeOut(id=user.id, username=user.username, role=user.role)

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
            result = runner(db, user.id)
            # 采集成功后触发飞书实时提醒;失败不影响采集结果返回
            try:
                from app.services.feishu import run_feishu_keyword_alerts, run_feishu_realtime

                run_feishu_realtime(platform, user.id)
                if platform == "douhot":
                    run_feishu_keyword_alerts(user.id)
            except Exception:  # noqa: BLE001
                pass
            return result
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"采集失败:{exc}") from exc

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception):  # type: ignore[no-untyped-def]
        """兜底:未预期异常记录完整堆栈,并返回带异常类型的中文提示。

        否则用户只看到裸的 `Internal Server Error`,前端也拿不到可读信息,
        排查全靠猜。响应里只带**异常类型名**(如 OperationalError)方便定位,
        异常消息可能含 SQL/路径等细节,只写进服务日志。
        """
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"服务器内部错误({type(exc).__name__}),请稍后重试;详情见服务日志"},
        )

    @app.get("/api/schedules")
    def schedules_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        """当前用户三个板块的采集频率(含可选档位与下次运行时间)。"""
        return schedule_service.list_schedules(db, user.id)

    @app.put("/api/schedules/{section}")
    def schedules_set(
        section: str,
        body: ScheduleIn,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """设置某板块的采集间隔(分钟)/开关。"""
        try:
            return schedule_service.set_schedule(db, user.id, section, body.interval_minutes, body.enabled)
        except schedule_service.ScheduleError as exc:
            raise HTTPException(400, str(exc)) from exc

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

    # ---- 每用户 SMTP(发信给本人) ----
    @app.get("/api/user/smtp")
    def user_smtp_get(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        return {"host": user.smtp_host or "", "port": user.smtp_port or 465,
                "user": user.smtp_user or "", "from_name": user.smtp_from or ""}

    @app.put("/api/user/smtp")
    def user_smtp_put(body: UserSmtpIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        user.smtp_host = body.host or None
        user.smtp_port = body.port or None
        user.smtp_user = body.user or None
        user.smtp_pass = body.password or None
        user.smtp_from = body.from_name or None
        db.commit()
        return {"saved": True}

    # ================= 管理后台(require_admin + 按钮级权限) =================
    class ConfigIn(pydantic.BaseModel):
        value: str

    def _require_perm(perm: str):
        def dep(user: User = Depends(require_admin)) -> User:
            if not admin_svc.has_perm(user.role, perm):
                raise HTTPException(403, f"无权限:{perm}")
            return user

        return dep

    @app.get("/api/admin/me")
    def admin_me(user: User = Depends(require_admin)):
        return {"role": user.role, "username": user.username, "perms": admin_svc.perms_list(user.role)}

    @app.get("/api/admin/dashboard")
    def admin_dashboard(user: User = Depends(_require_perm("dashboard.view")), db: Session = Depends(get_db)):
        return admin_svc.dashboard(db)

    @app.get("/api/admin/insights")
    def admin_insights(user: User = Depends(_require_perm("data.view")), db: Session = Depends(get_db)):
        return admin_svc.insights(db)

    @app.get("/api/admin/users")
    def admin_users(q: str = "", user: User = Depends(_require_perm("users.view")), db: Session = Depends(get_db)):
        return admin_svc.list_users(db, q)

    @app.post("/api/admin/users/{user_id}/toggle")
    def admin_users_toggle(user_id: int, user: User = Depends(_require_perm("users.toggle")), db: Session = Depends(get_db)):
        res = admin_svc.toggle_user(db, user_id)
        if res is None:
            raise HTTPException(404, "用户不存在")
        admin_svc.log_admin(db, user, "toggle_user", f"user#{user_id}", f"enabled={res['enabled']}")
        return res

    @app.delete("/api/admin/users/{user_id}")
    def admin_users_del(user_id: int, user: User = Depends(_require_perm("users.delete")), db: Session = Depends(get_db)):
        if user_id == user.id:
            raise HTTPException(400, "不能删除自己")
        ok = admin_svc.delete_user(db, user_id)
        if not ok:
            raise HTTPException(404, "用户不存在")
        admin_svc.log_admin(db, user, "delete_user", f"user#{user_id}")
        return {"deleted": True}

    @app.get("/api/admin/logins")
    def admin_logins(user: User = Depends(_require_perm("logs.view")), db: Session = Depends(get_db)):
        return admin_svc.list_logins(db)

    @app.get("/api/admin/logs")
    def admin_logs(user: User = Depends(_require_perm("logs.view")), db: Session = Depends(get_db)):
        return admin_svc.list_admin_logs(db)

    @app.get("/api/admin/config")
    def admin_config(user: User = Depends(_require_perm("config.view")), db: Session = Depends(get_db)):
        return admin_svc.config_get(db)

    @app.put("/api/admin/config/{key}")
    def admin_config_set(key: str, body: ConfigIn, user: User = Depends(_require_perm("config.set")), db: Session = Depends(get_db)):
        admin_svc.config_set(db, key, body.value)
        admin_svc.log_admin(db, user, "set_config", key, body.value)
        return {"key": key, "value": body.value}

    @app.get("/api/admin/export/users")
    def admin_export_users(user: User = Depends(_require_perm("data.export")), db: Session = Depends(get_db)):
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(admin_svc.export_users(db), media_type="text/csv")

    @app.get("/api/admin/export/alerts")
    def admin_export_alerts(user: User = Depends(_require_perm("data.export")), db: Session = Depends(get_db)):
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(admin_svc.export_alerts(db), media_type="text/csv")

    @app.get("/api/admin/users/{user_id}")
    def admin_user_detail(user_id: int, user: User = Depends(_require_perm("users.view")), db: Session = Depends(get_db)):
        detail = admin_svc.user_detail(db, user_id)
        if detail is None:
            raise HTTPException(404, "用户不存在")
        return detail

    @app.post("/api/admin/import/users")
    def admin_import_users(body: dict, user: User = Depends(_require_perm("users.import")), db: Session = Depends(get_db)):
        text = str(body.get("text", ""))
        res = admin_svc.import_users(db, text)
        admin_svc.log_admin(db, user, "import_users", f"created={res['created']} skipped={res['skipped']}")
        return res

    @app.get("/api/admin/data/{section}")
    def admin_data(section: str, user_id: int | None = None, limit: int = 50,
                   user: User = Depends(_require_perm("data.view")), db: Session = Depends(get_db)):
        return admin_svc.data_browse(db, section, user_id, limit)

    @app.get("/api/admin/categories")
    def admin_categories(user: User = Depends(_require_perm("dashboard.view")), db: Session = Depends(get_db)):
        return admin_svc.category_dist(db)

    @app.get("/api/admin/runs/failed")
    def admin_runs_failed(user: User = Depends(_require_perm("logs.view")), db: Session = Depends(get_db)):
        return admin_svc.failed_runs(db)

    @app.get("/api/admin/alert-trend")
    def admin_alert_trend(days: int = 30, user: User = Depends(_require_perm("dashboard.view")), db: Session = Depends(get_db)):
        return admin_svc.alert_trend(db, days)

    @app.get("/api/admin/category-pie")
    def admin_category_pie(user: User = Depends(_require_perm("dashboard.view")), db: Session = Depends(get_db)):
        return admin_svc.category_pie(db)

    @app.post("/api/admin/runs/{run_id}/retry")
    def admin_run_retry(run_id: str, user: User = Depends(_require_perm("users.toggle")), db: Session = Depends(get_db)):
        res = admin_svc.retry_run(db, run_id)
        admin_svc.log_admin(db, user, "retry_run", run_id, "ok" if res.get("ok") else str(res.get("msg", "")))
        return res

    # 托管前端构建产物(SPA)
    dist = Path(__file__).parent / "static" / "spa"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/", response_class=HTMLResponse)
        def index() -> str:
            return (dist / "index.html").read_text(encoding="utf-8")

        # SPA history 路由回退:前端用 createWebHistory,直接访问或刷新 /login、
        # /schedule、/admin 时浏览器会真的向服务端请求该路径。没有这个兜底就会 404
        # ——找回密码邮件里的 /reset?token=... 链接同样会打不开。
        # 必须注册在所有 API 路由之后(FastAPI 按注册顺序匹配),并放过 API 前缀,
        # 让写错的接口路径仍返回 JSON 404 而不是一篇 HTML。
        @app.get("/{full_path:path}", response_class=HTMLResponse)
        def spa_fallback(full_path: str) -> str:
            if full_path.startswith(("api/", "assets/")) or full_path in {"healthz", "docs", "openapi.json"}:
                raise HTTPException(404, "Not Found")
            return (dist / "index.html").read_text(encoding="utf-8")

    return app


app = create_app()
