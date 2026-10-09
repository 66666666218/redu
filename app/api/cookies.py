"""Cookie 与用户 SMTP 路由:/api/cookies/*、/api/user/smtp。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services.cookie_store import delete_cookie as del_cookie
from app.services.cookie_store import list_cookies, set_cookie
from app.api.deps import CookieIn, CookieOut, UserSmtpIn, WempCredIn

router = APIRouter()


@router.get("/api/cookies", response_model=list[CookieOut])
def cookies_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[CookieOut]:
    return [CookieOut(**c) for c in list_cookies(db, user.id)]


@router.put("/api/cookies/{platform}", response_model=CookieOut)
def cookies_put(platform: str, body: CookieIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> CookieOut:
    try:
        set_cookie(db, user.id, platform, body.cookie.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return CookieOut(platform=platform, configured=bool(body.cookie.strip()))


@router.delete("/api/cookies/{platform}")
def cookies_del(platform: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    del_cookie(db, user.id, platform)
    return {"platform": platform, "deleted": True}


# ⚠️ **闲鱼扫码登录的两个接口已删除**(2026-10-03)。
# 原实现(`/api/cookies/goofish/qr-start|qr-status` → `app.services.xianyu_login`)走的是
# **纯协议二维码流程**,把登录态写进 `cookie_store`。但 2026-10-02 起采集默认改走
# **浏览器档案**(`xiangyu_browser`,登录态在 `tools/xianyu_profile` 里),`tenant.run_xianyu`
# 在浏览器模式下**不读也不校验**那个 cookie —— 于是这个按钮**扫了完全没效果,却显示"✅ 登录成功"**
# (比报错更糟:用户以为修好了)。
# **现在闲鱼登录的正确做法**:关掉面板,跑 `scripts/xianyu_login.py` 或直接用
# `tools/xianyu_profile` 那个档案开浏览器登录(见 doc/operations.md §10)。
# 纯协议那条路(`XIANYU_USE_BROWSER=false`)不推荐 —— 实测被"哎哟喂,被挤爆啦"挡住。
# ⚠️ **2026-10-08 订正**:此前写的"**账号级**限流"**已证伪**(匿名请求同样被挤爆),
# 所以**换号/换 Cookie 不是解法**;详见 `app/services/xianyu_browser.py`。
@router.get("/api/user/smtp")
def user_smtp_get(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"host": user.smtp_host or "", "port": user.smtp_port or 465,
            "user": user.smtp_user or "", "from_name": user.smtp_from or ""}


@router.put("/api/user/smtp")
def user_smtp_put(body: UserSmtpIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 用户可填 host 若原样入库,notifier.get_user_notifier 会 smtplib.SMTP_SSL(host,port)
    # 直连,容器即被当作内网端口探测跳板(`host=mysql`/`169.254.169.254` 等);
    # 与 assert_public_url 同一咽喉,只是入口是 (host,port)。清空 host(=None) 允许。
    if body.host:
        from app.utils.net import UnsafeUrlError, assert_public_host

        try:
            assert_public_host(body.host, body.port or 465)
        except UnsafeUrlError as exc:
            raise HTTPException(400, f"SMTP 主机不合法:{exc}") from exc
    user.smtp_host = body.host or None
    user.smtp_port = body.port or None
    user.smtp_user = body.user or None
    # SMTP 密码加密存储(gAAAAAB 前缀标识密文;历史明文在下次保存时自然替换)
    if body.password:
        from app.security import encrypt_cookie

        user.smtp_pass = "enc:" + encrypt_cookie(body.password)
    else:
        user.smtp_pass = None
    user.smtp_from = body.from_name or None
    db.commit()
    return {"saved": True}

# ---------------------------------------------------------------------------
# 公众号后台(wemp)凭据 —— **唯一需要两个字段的凭据**,所以单列几个接口
# ---------------------------------------------------------------------------
# ⚠️⚠️ **为什么不能复用上面那套 `/api/cookies/{platform}`**(2026-10-09,用户问的):
# 那套走 `cookie_store`(`user_cookies`,**单字段**),而 wemp 的凭据是
# **`cookie` + 地址栏里的 `token` 两个值**,存在 `system_config[wemp_cred_{uid}]` 的
# **加密 JSON** 里(`app/services/wemp_cred.py` 是唯一读写入口)。
# ⇒ 通用 Cookie 页**根本管不到它**,用户只能手抄命令行(而命令行会把凭据留在 shell 历史里)。
# 这里补一条正路:**粘两个框 → 保存 → 当场验活**,结果直接显示"能拿几篇 / 会话失效 / 被限流"。


@router.get("/api/wemp/credential")
def wemp_cred_get(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """凭据状态。⚠️ **只回长度,不回凭据本体** —— 它不该出现在任何响应里。"""
    from app.services.wemp_cred import load

    cred = load(db, user.id)
    return {"configured": bool(cred.get("cookie") and cred.get("token")),
            "cookie_len": len(cred.get("cookie") or ""),
            "token_len": len(cred.get("token") or "")}


@router.put("/api/wemp/credential")
def wemp_cred_put(body: WempCredIn,
                  user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)) -> dict:
    """保存并**当场验活**。

    ⚠️⚠️ **顺序是"先验活、成功了才落库"**(2026-10-09):反过来(先存再验)的话,
    一次**粘错**就会把还能用的那份**覆盖成死的**,而症状是"公众号列表源悄悄少一个"
    —— 那种静默退化正是本仓最恨的。两种例外照旧落库:
      · **被限流(200013)**:凭据**本身有效**,只是配额满了 ⇒ 存;
      · **库里还没对标号**:探不了,但**不是失败** ⇒ 存。
    """
    from app.services.wechat.wemp_client import WempAuthError, WempError, WempRateLimited
    from app.services.wemp_cred import probe, save

    cookie, token = (body.cookie or "").strip(), (body.token or "").strip()
    if not cookie or not token:
        raise HTTPException(400, "cookie 与 token **都要填** —— 后台凭据缺任何一个都用不了")
    try:
        out = probe(db, user.id, cookie, token)
    except WempRateLimited as exc:
        save(db, user.id, cookie, token)
        raise HTTPException(400, f"凭据**有效**但被频率限制(200013):{exc}"
                                 f" —— 已保存;换新注册的号更干净,或等配额恢复") from exc
    except WempAuthError as exc:
        raise HTTPException(400, f"**会话失效**:{exc} —— **没有保存**(免得覆盖掉还能用的那份)。"
                                 f"请重新登录 mp.weixin.qq.com,把整条 Cookie 与地址栏里的 token 再取一次") from exc
    except WempError as exc:
        raise HTTPException(400, f"探针失败(**没有保存**):{exc}") from exc
    save(db, user.id, cookie, token)
    return {"saved": True, **out}


@router.delete("/api/wemp/credential")
def wemp_cred_del(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    from app.db.models import SystemConfig
    from sqlalchemy import select as _select

    key = f"wemp_cred_{user.id}"
    row = db.scalar(_select(SystemConfig).where(SystemConfig.key == key))
    if row is not None:
        row.value = ""
        db.commit()
    return {"deleted": True}
