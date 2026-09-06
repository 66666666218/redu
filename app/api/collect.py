"""采集触发与关键词监控路由:collect / xianyu collect-deep / douhot watch。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services import tenant

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
        except Exception:  # noqa: BLE001
            pass
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"采集失败:{exc}") from exc


@router.post("/api/xianyu/collect-deep")
def xianyu_collect_deep(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return tenant.run_xianyu_deep(db, user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"采集失败:{exc}") from exc


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


@router.post("/api/watch/{section}")
def watch_add(section: str, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """在某板块添加关键词监控(微博/闲鱼/抖音/百度通用)。"""
    from app.services.keyword_watch import add_watch

    try:
        return add_watch(db, user.id, section, str(payload.get("list_type", "word")),
                         str(payload.get("keyword", "")), str(payload.get("filter_keyword", "")),
                         payload.get("date_window"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


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
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


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
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"删除关注失败:{exc}") from exc
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
        raise HTTPException(500, f"榜单获取失败:{exc}") from exc
