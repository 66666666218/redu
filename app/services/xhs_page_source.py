# -*- coding: utf-8 -*-
"""小红书:**走页面渲染**,不走 API 直连(2026-10-07)。

## 为什么要换一条路(实测,不是猜)
MediaCrawler 直连 `/api/sns/web/v1/search/notes` 现在回
`461 CAPTCHA`,响应体 `{'code': 300011, 'msg': '检测到账号异常'}`。
上游 issue #757 里维护者说"已集成 `xhshow` 签名、拉最新代码就好" ——
但我们本地那份是**脱敏教学版**(旧版 + 许可证禁商用),升级不合适。

**换来的这条路是我们在闲鱼上验证过的同一个套路**(见记忆 `[闲鱼必须页面内调mtop]`):
**不自算签名,让页面自己的 JS 去发请求,我们只读渲染出来的 DOM。**

## 根因不是签名,是**会话被判成"需安全验证"**(2026-10-07 实测)
拿已登录的档案打开 `/explore`,被**重定向**到
`https://www.xiaohongshu.com/website-login/captcha?redirectPath=...`(标题「安全验证」);
搜索页则是 `search-empty-wrapper` + `xhsCaptcha_feedback_link`。
⇒ 连**推荐流**都进不去,和 461 是同一件事的两面。

**过法**:用同一个档案开一次真实浏览器、**人工过一次人机验证**,会话就恢复
(`tools/xhs_pass_verify.py` 就是干这个的;实测过完之后推荐流 30 张卡、**搜索页 38 张卡**)。

## ⚠️ 三条实现纪律
1. **整个生命周期在同一次调用里**(开浏览器 → 逐词搜 → 关)。
   Playwright 的 sync 对象**有线程亲和性**(只能在其创建线程用),而调度器是多线程的 ——
   做成跨轮次复用的单例就会**偶发崩**(本仓踩过)。这里用"一次调用开一次"换掉那个坑,
   代价是每轮多 ~15 秒启动。
2. **"真没有结果" 与 "被拦" 必须分开**:`search-empty-wrapper` = 真没有(返回空);
   captcha 重定向 = **被拦**(抛错)。混在一起就变成"小红书没热度"这种假结论。
3. **有头**(`headless=False`):验证页要人看得到、点得着;同 `[闲鱼必须页面内调mtop]`
   那条"无头不行"。
"""
from __future__ import annotations

from pathlib import Path

from app.utils import get_logger

logger = get_logger(__name__)

#: 已登录的浏览器档案(MediaCrawler 一直用的那个 —— 登录态在里面,别另开一个新的)
DEFAULT_PROFILE = ("tools/MediaCrawler/browser_data/cdp_xhs_user_data_dir")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={kw}&source=web_explore_feed"
CARD_SEL = "section.note-item"
#: 搜索结果区**空**时的容器(小红书自己的空态)—— 用它把"真没有"和"被拦"分开
EMPTY_SEL = ".search-empty-wrapper"


class XhsPageError(Exception):
    """页面路硬失败(被拦 / 浏览器起不来)。**必须冒泡**,不能当成"没搜到"。"""


_CURSOR_KEY = "xhs_account_cursor"
_NEED_KEY = "xhs_account_need_verify"


def _profiles(settings=None) -> list[Path]:
    """**多账号**:逗号分隔的浏览器档案目录;没配就退回单个 `_profile()`。

    为什么要多账号:**风控是账号级的**(实测码 `300011 检测到账号异常`)——
    单账号频率除以账号数,是最直接的一条降险手段。
    ⚠️ 每个账号必须是**各自独立的档案目录**(登录态在里面);共用同一个档案 = 同一个账号。
    """
    if settings is None:
        from config.settings import get_settings
        settings = get_settings()
    raw = str(getattr(settings, "xhs_browser_profiles", "") or "").strip()
    if not raw:
        return [_profile(settings)]
    out: list[Path] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        p = Path(part)
        out.append(p if p.is_absolute() else Path(__file__).resolve().parents[2] / p)
    return out or [_profile(settings)]


def _cursor_index(n: int) -> int:
    """读轮换游标(**存在 `system_config`,跨进程有效**)并自增,返回本轮用第几个账号。"""
    if n <= 1:
        return 0
    try:
        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import SystemConfig

        with get_session_local()() as db:
            row = db.scalar(select(SystemConfig).where(SystemConfig.key == _CURSOR_KEY))
            cur = int(str(row.value)) if row is not None and str(row.value).isdigit() else 0
            nxt = str((cur + 1) % n)
            if row is None:
                db.add(SystemConfig(key=_CURSOR_KEY, value=nxt))
            else:
                row.value = nxt
            db.commit()
            return cur % n
    except Exception:  # noqa: BLE001 - 游标读不到就固定用第 0 个,不该因此停摆
        logger.debug("小红书账号游标读写失败(本轮用第 0 个)", exc_info=True)
        return 0


def mark_need_verify(profile: Path) -> None:
    """把某个账号标成"要人过一次安全验证",这样轮换会**跳过它**、不继续烧风控。"""
    try:
        import json

        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import SystemConfig

        with get_session_local()() as db:
            row = db.scalar(select(SystemConfig).where(SystemConfig.key == _NEED_KEY))
            cur = set(json.loads(row.value)) if row is not None and row.value else set()
            cur.add(str(profile))
            blob = json.dumps(sorted(cur), ensure_ascii=False)
            if row is None:
                db.add(SystemConfig(key=_NEED_KEY, value=blob))
            else:
                row.value = blob
            db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("标记'需验证'失败(不影响本轮)", exc_info=True)


def need_verify_profiles() -> set[str]:
    """哪些账号当前需要人过一次验证(供巡检/告警用)。"""
    try:
        import json

        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import SystemConfig

        with get_session_local()() as db:
            row = db.scalar(select(SystemConfig).where(SystemConfig.key == _NEED_KEY))
            return set(json.loads(row.value)) if row is not None and row.value else set()
    except Exception:  # noqa: BLE001
        return set()


def clear_need_verify(profile: Path) -> None:
    """验证过了就把它从"需验证"名单里摘掉。"""
    try:
        import json

        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import SystemConfig

        with get_session_local()() as db:
            row = db.scalar(select(SystemConfig).where(SystemConfig.key == _NEED_KEY))
            if row is None or not row.value:
                return
            cur = set(json.loads(row.value)) - {str(profile)}
            row.value = json.dumps(sorted(cur), ensure_ascii=False)
            db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("清'需验证'标记失败", exc_info=True)


def _profile(settings=None) -> Path:
    if settings is None:
        from config.settings import get_settings
        settings = get_settings()
    p = str(getattr(settings, "xhs_browser_profile", "") or "").strip()
    raw = Path(p) if p else Path(DEFAULT_PROFILE)
    # ⚠️ 相对路径按**仓库根**解析 —— 不能依赖进程 CWD(调度器/脚本启动位置不一定一样)
    return raw if raw.is_absolute() else Path(__file__).resolve().parents[2] / raw


def verify_needed(url: str, title: str, body: str = "") -> bool:
    """页面是不是**被安全验证拦住**了(而不是"真的没结果")。

    判据用 **url/标题**,不用"卡片数" —— ⚠️ 我第一版就是看卡片数,于是把
    "被重定向到验证页"误判成"页面没 hydrate",结论整个反了。
    """
    u = (url or "").lower()
    t = (title or "")
    return ("captcha" in u or "website-login" in u or "安全验证" in t
            or "xhsCaptcha" in (body or ""))


def search(keywords: list[str], settings=None, per_kw_wait_ms: int = 6000,
           headed: bool = True) -> list[dict]:
    """按关键词搜小红书,返回 `[{uid, name, url, snippet, pan_link, keyword}]`。

    形状与 `mediacrawler_source.crawl` 一致(调用方 `resource_presence.probe` 直接用),
    这样两条路可以互换,不用改上层。
    """
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return []
    profs = [p for p in _profiles(settings) if p.exists()]
    if not profs:
        raise XhsPageError(
            f"小红书浏览器档案都不存在:{[str(p) for p in _profiles(settings)]} —— "
            f"先跑 `python tools/xhs_pass_verify.py` 过一次验证")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # noqa: BLE001
        raise XhsPageError(f"没有 playwright:{exc}") from exc

    # ⚠️ 开跑前清残留 Edge:上一轮若没退会占着**同一个 profile**(本仓踩过,
    # 而且失败会**自我延续**)。复用 MediaCrawler 那份实现,不另写一个。
    try:
        from app.services.mediacrawler_source import kill_stale_browsers
        kill_stale_browsers()
    except Exception:  # noqa: BLE001
        logger.debug("清理残留浏览器失败(继续)", exc_info=True)

    # ★ **多账号轮换 + 失败自动切换**(2026-10-07,用户口径「我给你多个账号」):
    #   · 从**轮换游标**处开始,连续几轮落在不同账号上 ⇒ 单账号频率 = 1/N;
    #   · 已知"需人过一次安全验证"的账号**直接跳过**,不继续烧风控;
    #   · 万一还是被拦 ⇒ **标记它 + 自动换下一个**,不用等人。
    start = _cursor_index(len(profs))
    order = [profs[(start + i) % len(profs)] for i in range(len(profs))]
    skip = need_verify_profiles()
    usable = [p for p in order if str(p) not in skip] or order
    tried: list[str] = []
    last_err = ""
    with sync_playwright() as p:
        for prof in usable:
            try:
                rows = _search_with_profile(p, prof, kws, per_kw_wait_ms, headed)
                clear_need_verify(prof)
                logger.info("小红书:本轮用账号「%s」,拿到 %d 条", prof.name, len(rows))
                return rows
            except XhsPageError as exc:
                tried.append(prof.name)
                last_err = str(exc)
                if "安全验证" in str(exc):
                    mark_need_verify(prof)
                    logger.warning("小红书账号「%s」要安全验证 —— 标记并换下一个", prof.name)
                    continue
                raise                        # 别的错(打不开页等)不是"换账号"能解决的
    raise XhsPageError(
        f"所有小红书账号都不可用(试过 {tried})。修法:对每个档案各跑一次 "
        f"`python tools/xhs_pass_verify.py <档案目录>` 人工过验证。最后原因:{last_err[:120]}")


def _search_with_profile(p, prof, kws: list[str], per_kw_wait_ms: int,
                         headed: bool) -> list[dict]:
    """**单个账号**跑一轮搜索。被安全验证拦住时抛 `XhsPageError`(由调用方换账号)。"""
    rows: list[dict] = []
    if True:
        kwargs = {"user_data_dir": str(prof), "headless": not headed, "no_viewport": True}
        try:
            ctx = p.chromium.launch_persistent_context(channel="msedge",
                                                       executable_path=EDGE, **kwargs)
        except Exception:  # noqa: BLE001 - 没有 Edge 就退回自带 chromium
            logger.info("小红书页面路:未找到 Edge,改用自带 Chromium")
            ctx = p.chromium.launch_persistent_context(**kwargs)
        try:
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            # ⚠️ **先开一次首页"热身"再跳搜索**(2026-10-07 实测):直接 goto 搜索页会
            # `Timeout 60000ms exceeded`(整站 app 冷启动 + 直落深链更慢),而
            # **先 /explore 再跳搜索就正常**(探针就是这么跑通的)。这不是重试能救的 ——
            # 冷启动的第一次导航本来就该给更长的时间,而且是同一条路径。
            try:
                pg.goto("https://www.xiaohongshu.com/explore",
                        wait_until="domcontentloaded", timeout=90000)
                pg.wait_for_timeout(4000)
            except Exception:  # noqa: BLE001 - 热身失败仍往下试(可能只是慢)
                logger.info("小红书页面路:首页热身超时,继续试搜索页")
            for kw in kws:
                try:
                    pg.goto(SEARCH_URL.format(kw=kw), wait_until="commit",
                            timeout=60000)
                except Exception as exc:  # noqa: BLE001
                    raise XhsPageError(f"打开搜索页失败({kw[:20]}):{str(exc)[:80]}") from exc
                try:
                    pg.wait_for_selector(CARD_SEL, timeout=20000)
                except Exception:  # noqa: BLE001 - 没有卡片:下面按"空态/被拦"分辨
                    pass
                pg.wait_for_timeout(per_kw_wait_ms)
                if verify_needed(pg.url, pg.title() or "", pg.content()[:4000]):
                    raise XhsPageError(
                        "小红书要**安全验证**(被重定向到 captcha 页)—— 跑 "
                        "`python tools/xhs_pass_verify.py` 人工过一次即可,"
                        "过完会话恢复(实测搜索页能出 38 张卡)")
                pg.mouse.wheel(0, 1600)          # 触发懒加载
                pg.wait_for_timeout(2000)
                cards = pg.locator(CARD_SEL)
                n = cards.count()
                title_text = (pg.inner_text("body") or "")
                if n == 0 and pg.locator(EMPTY_SEL).count():
                    logger.info("小红书页面路:「%s」确实没有结果(空态容器在)", kw[:26])
                    continue                      # **真没有** ≠ 被拦
                for i in range(min(n, 30)):
                    try:
                        txt = cards.nth(i).inner_text().replace("\n", " ").strip()
                    except Exception:  # noqa: BLE001 - 单卡片读失败不拖垮整词
                        continue
                    if not txt:
                        continue
                    rows.append({"uid": "", "name": "", "url": "",
                                 "snippet": txt[:255], "pan_link": "",
                                 "keyword": kw})
                logger.info("小红书页面路:「%s」拿到 %d 条", kw[:26], n)
        finally:
            ctx.close()                           # 顺序执行的资源不跨调用(见模块头纪律 1)
    return rows
