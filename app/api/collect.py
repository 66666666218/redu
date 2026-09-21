"""采集触发与关键词监控路由:collect / xianyu collect-deep / douhot watch。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services import tenant
from app.utils import get_logger
from app.utils.net import redact_proxy_creds

logger = get_logger(__name__)

router = APIRouter()


@router.post("/api/collect/{platform}")
def collect(platform: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if platform not in ("weibo", "xianyu", "douhot", "baidu"):
        raise HTTPException(400, "不支持的平台")
    try:
        runner = {"weibo": tenant.run_weibo, "xianyu": tenant.run_xianyu, "douhot": tenant.run_douhot, "baidu": tenant.run_baidu}[platform]
        result = runner(db, user.id)
        # 采集成功后触发飞书实时提醒;失败不影响采集结果返回
        try:
            from app.services.feishu import run_feishu_keyword_alerts, run_feishu_keyword_realtime, run_feishu_realtime

            run_feishu_realtime(platform, user.id)
            if platform == "douhot":
                run_feishu_keyword_alerts(user.id)
                run_feishu_keyword_realtime(user.id)  # 话题词新进/上升/爆发实时提醒
        except Exception:  # noqa: BLE001  采集成功后推送失败不影响返回,但要留痕
            logger.exception("采集后飞书实时提醒失败(平台=%s, 用户=%s)", platform, user.id)
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # 走带鉴权代理时 requests 异常文本含 http://user:pass@host,勿原样回给浏览器
        raise HTTPException(500, f"采集失败:{redact_proxy_creds(str(exc))}") from exc


@router.post("/api/xianyu/collect-deep")
def xianyu_collect_deep(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return tenant.run_xianyu_deep(db, user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"采集失败:{redact_proxy_creds(str(exc))}") from exc


@router.post("/api/douhot/watch")
def douhot_watch_add(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return tenant.add_douhot_watch(db, user.id, str(payload.get("list_type", "word")),
                                   str(payload.get("keyword", "")), str(payload.get("filter_keyword", "")),
                                   payload.get("date_window"))


@router.get("/api/douhot/watch")
def douhot_watch_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return tenant.list_douhot_watch(db, user.id)


@router.get("/api/douhot/watch-analytics")
def douhot_watch_analytics(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return tenant.douhot_watch_analytics(db, user.id)


@router.get("/api/douhot/watch-windows")
def douhot_watch_windows(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """关键词多窗口对比分析(近1h vs 近1天):各词两档值 + 比例 + 趋势标签。"""
    from app.services.douhot_window import analytics

    items = analytics(db, user.id)
    return {"count": len(items), "items": items}


@router.post("/api/douhot/watch-windows/refresh")
def douhot_watch_windows_refresh(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """立即采集一轮关键词多窗口热度(近1h/近1天)并落库。"""
    from app.services.douhot_window import collect_windows, run_feishu

    out = collect_windows(db, user.id)
    if out.get("status") == "skipped":
        return out
    pushed = run_feishu(db, user.id)  # 命中异动即时推
    return {"platform": "douhot_window", "status": out.get("status"),
            "words": out.get("words", 0), "ok": out.get("ok", 0),
            "snaps": out.get("snaps", 0), "pushed": pushed}


@router.post("/api/douhot/windows/query")
def douhot_windows_query(payload: dict, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """实时查**任意**关键词的多窗口对比(近1h vs 近1天),不依赖监控词,一次性不落库。"""
    from app.services.douhot_window import query_windows

    return query_windows(db, user.id, str(payload.get("list_type", "video")),
                         str(payload.get("keyword", "")))


@router.post("/api/watch/{section}")
def watch_add(section: str, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """在某板块添加关键词监控(微博/闲鱼/抖音/百度通用)。"""
    from app.services.keyword_watch import add_watch

    try:
        return add_watch(db, user.id, section, str(payload.get("list_type", "word")),
                         str(payload.get("keyword", "")), str(payload.get("filter_keyword", "")),
                         payload.get("date_window"))
    except ValueError as exc:
        # 只回业务校验的中文提示。DB/驱动异常 str(exc) 是 SQLAlchemy 完整 repr,
        # 含 [SQL: INSERT ...]、[parameters: ...]、OperationalError 还带 DB 主机:端口,
        # 任何登录用户构造一次并发/异常即可看到内网 DB 拓扑。
        raise HTTPException(400, str(exc)) from exc
    except Exception:  # noqa: BLE001
        logger.exception("添加关注失败 user=%s section=%s", user.id, section)
        raise HTTPException(500, "操作失败,请稍后重试")


@router.patch("/api/watch/{section}")
def watch_update(section: str, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """修改某板块已关注关键词的观测时段(date_window)。"""
    from app.services.keyword_watch import update_watch

    try:
        return update_watch(db, user.id, section, str(payload.get("list_type", "word")),
                            str(payload.get("keyword", "")), str(payload.get("filter_keyword", "")),
                            payload.get("date_window"))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception:  # noqa: BLE001
        # 同 watch_add:屏蔽 SQLAlchemy 异常文本对客户端的暴露
        logger.exception("更新关注失败 user=%s section=%s", user.id, section)
        raise HTTPException(500, "操作失败,请稍后重试")


@router.get("/api/watch/{section}")
def watch_list(section: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.keyword_watch import list_watch

    return list_watch(db, user.id, section)


@router.delete("/api/watch/{section}")
def watch_delete(section: str, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """取消某板块的一个关键词关注(并删历史快照)。"""
    from app.services.keyword_watch import remove_watch

    try:
        ok = remove_watch(db, user.id, section, str(payload.get("list_type", "word")),
                          str(payload.get("keyword", "")), str(payload.get("filter_keyword", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception:  # noqa: BLE001
        # 同 watch_add:DB 异常原文含 SQL/host/参数,勿回客户端
        logger.exception("删除关注失败 user=%s section=%s", user.id, section)
        raise HTTPException(500, "操作失败,请稍后重试")
    if not ok:
        raise HTTPException(404, "未找到该关注词")
    return {"ok": True}


@router.delete("/api/douhot/watch")
def douhot_watch_delete(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """取消抖音关键词关注(等价 section=douhot 的通用删除,兼容旧调用)。"""
    from app.services.keyword_watch import remove_watch

    ok = remove_watch(db, user.id, "douhot", str(payload.get("list_type", "word")),
                      str(payload.get("keyword", "")), str(payload.get("filter_keyword", "")))
    if not ok:
        raise HTTPException(404, "未找到该关注词")
    return {"ok": True}


@router.get("/api/watch/{section}/analytics")
def watch_analytics(section: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.keyword_watch import watch_analytics as _wa

    return _wa(section, db, user.id)


@router.post("/api/watch/{section}/digest")
def watch_digest(section: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """立即把该板块的「关键词监控」卡片推送到飞书(含名次变化 ↑N名/↓N名)。

    平时每日日报(08:00)自动推总群 + 各板块专属群;此接口用于即时手动推送。
    """
    from config.settings import get_settings
    from app.services.feishu import build_keyword_card, webhook_for
    from app.services.feishu_client import FeishuClient

    settings = get_settings()
    card = build_keyword_card(db, user.id, settings, section=section)
    if not card:
        return {"status": "skipped", "reason": "no_watch"}
    wh = webhook_for(settings, section)
    if not wh:
        return {"status": "skipped", "reason": "no_webhook"}
    ok = FeishuClient(wh, settings.feishu_secret).send_card(card)
    return {"status": "success" if ok else "failed"}


@router.get("/api/douhot/list/{list_type}")
def douhot_list(list_type: str, keyword: str = "", filter_keyword: str = "", date_window: int | None = None,
                user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """实时拉取抖音某个子榜(内容词/搜索/视频/话题/订阅),便于热点宝式 tab 展示。

    `keyword` 非空时按词**定向搜索**(榜外词也能查到),返回过滤后的目标条目列表;
    `filter_keyword` 非空时**只保留标题含该词的主题**(二次过滤,如"完整版"里只留短剧);
    `date_window` 为时段(小时):1/24/72/168 → 近1小时/近1天/近3天/近7天(默认按榜单);
    subscribe 无 keyword 参数,不支持关键词搜索(传了会 400)。
    """
    from app.services.cookie_store import get_cookies
    from app.services import douhot
    from config.settings import get_settings

    cookies = get_cookies(db, user.id)
    cookie = cookies.get("douyin", "")
    if not cookie:
        raise HTTPException(400, "未配置抖音(热点宝) Cookie")
    settings = get_settings()
    kw = keyword.strip()
    fk = filter_keyword.strip()
    try:
        if kw:
            if list_type not in ("word", "search", "video", "topic"):
                raise HTTPException(400, "该榜不支持关键词搜索")
            return {"list_type": list_type, "keyword": kw, "filter_keyword": fk, "date_window": date_window,
                    "items": douhot.fetch_keyword_items(cookie, list_type, kw, settings, filter_keyword=fk,
                                                        date_window=date_window)}
        fetchers = {
            "word": lambda cookie, settings: [{"title": w["title"], "score": w["score"]} for w in douhot.fetch_content_words(cookie, settings, date_window=date_window)],
            "search": lambda cookie, settings: douhot.fetch_search_words(cookie, settings, date_window=date_window),
            "video": lambda cookie, settings: douhot.fetch_video_words(cookie, settings, date_window=date_window),
            "topic": lambda cookie, settings: douhot.fetch_topic_words(cookie, settings, date_window=date_window),
            "subscribe": douhot.fetch_subscribe_words,
        }
        fn = fetchers.get(list_type)
        if fn is None:
            raise HTTPException(400, "不支持的榜单类型")
        return {"list_type": list_type, "items": fn(cookie, settings)}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"榜单获取失败:{redact_proxy_creds(str(exc))}") from exc
