"""抖音 **CDP 路** —— 用**普通浏览器**(不是 Playwright 启动的)打开搜索页读结果。

## 为什么要有这条路(2026-10-25,当场实测)

抖音线索链连续多轮「8 个关键词全部无结果」。受控判别把原因一步步排掉了:

| 判别 | 结果 |
|---|---|
| MediaCrawler 浏览器路 + 泛词「美食」 | **0 条** |
| 协议接口 | `kind=verify` / `search_nil_info.search_nil_type=verify_check` |
| 档案登录态(`profile_login_state`) | `True`(但那只查 **cookie 名**) |
| **CDP:普通 Edge + 注入库里的 cookie** | **16 条**(`logged_out=false`) |

⇒ **账号和出口都没被限**,cookie 也是**好的**;卡住的是"我们那条自动化路"。
`webdriver` 实测 `False`(与小红书那条同一个结论),这就是本模块存在的理由。

## 与 `xhs_cdp_source` 的关系

同一个范式、同一套纪律(见下),但**不共用实现**:两家的搜索 URL / DOM / 卡片结构完全不同,
硬抽公共层只会造出一个谁都不像的中间件。

## 三条纪律

1. **连不上就说连不上**,绝不静默返回空 —— "CDP 没起来"被读成"抖音没人推资源"
   就是本仓最恨的那类假结论。
2. **只关自己开的那个标签**:接管的是浏览器上下文,`ctx.pages` 里可能有别人正在看的页面。
3. **词间必须休眠**(硬下限 5 秒):这条路唯一的优势是"像人",连发就把它丢掉了。

## 返回形状

与 `mediacrawler_source.crawl()` **同形状**:
`[{uid, name, url, snippet, pan_link, keyword}]` ⇒ 上层(`douyin_leads`)不用改判据。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from app.utils import get_logger

logger = get_logger(__name__)

#: 词间休眠**硬下限**(毫秒)。配置写多小都不许低于它。
_MIN_BETWEEN_MS = 5000
_GOTO_TIMEOUT_MS = 60000
_RESULT_WAIT_S = 8.0

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


class DouyinCdpError(RuntimeError):
    """CDP 路不可用/取数失败。**绝不返回空列表冒充"没人推"**。"""


def endpoint(settings=None) -> str:
    """CDP 端点(默认 `http://127.0.0.1:9333` —— 与小红书那条的 9222 分开)。"""
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    return str(getattr(settings, "douyin_cdp_url", "") or "").strip() or \
        "http://127.0.0.1:9333"


def profile_dir(settings=None) -> Path:
    """专用档案目录。

    ⚠️ **不用** MediaCrawler 那份(`tools/MediaCrawler/browser_data/cdp_douyin_user_data_dir`):
    实测它**本身是未登录的**(打开搜索页直接弹扫码框),登着的是我们**注入的 cookie**。
    两份共用会让"到底谁在登"变得说不清 —— 换一份干净的专用档案,登录态一律靠注入。
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    raw = str(getattr(settings, "douyin_cdp_profile", "") or "").strip()
    p = Path(raw) if raw else Path("data/_douyin_profile")
    return p if p.is_absolute() else Path.cwd() / p


def between_kw_ms(settings=None) -> int:
    """词间休眠(毫秒),夹着硬下限。"""
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    try:
        got = int(getattr(settings, "douyin_cdp_between_kw_ms", 0) or 0)
    except (TypeError, ValueError):
        got = 0
    return max(got, _MIN_BETWEEN_MS)


def available(settings=None) -> tuple[bool, str]:
    """端点**现在**能不能连上。**只问 `/json/version`**,不发任何抖音请求。"""
    url = endpoint(settings).rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        return True, f"CDP 可达:{d.get('Browser') or '?'}"
    except Exception as exc:  # noqa: BLE001 - 连不上是预期状态
        return False, f"CDP 不可达({url}):{type(exc).__name__}"


def _edge_exe() -> str:
    for p in _EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    raise DouyinCdpError("找不到 Edge/Chrome 可执行文件")


def ensure_running(settings=None) -> tuple[bool, str]:
    """端点不通就用**普通命令行**拉起浏览器(不是 Playwright launch),再等它就绪。

    ⚠️ **必须是普通启动**:Playwright 自己 launch 出来的浏览器 `navigator.webdriver=True`,
    那正是小红书被处罚、抖音被要求过验证的形态;这里只 `subprocess.Popen`,
    所以实测 `webdriver === false`。
    """
    ok, why = available(settings)
    if ok:
        return True, why
    prof = profile_dir(settings)
    prof.mkdir(parents=True, exist_ok=True)
    port = urllib.parse.urlparse(endpoint(settings)).port or 9333
    exe = _edge_exe()
    try:
        subprocess.Popen([exe, f"--remote-debugging-port={port}",
                          f"--user-data-dir={prof}", "--no-first-run",
                          "--no-default-browser-check", "--start-minimized",
                          "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001 - 起不来要如实报
        raise DouyinCdpError(f"启动浏览器失败:{type(exc).__name__}: {exc}") from exc
    for _ in range(40):
        time.sleep(0.5)
        ok, why = available(settings)
        if ok:
            return True, why + "(已由本模块启动)"
    return False, "浏览器已启动但 CDP 端口一直没就绪"


def _cookies_of(session, user_id: int) -> list[dict]:
    """把库里的抖音 cookie 摊成 Playwright 的 cookie 结构。

    ⚠️ 登录态**只靠注入**(见 `profile_dir` 的说明):档案是干净的,不注入就是未登录 ——
    那时搜索页会显示「登录后即可搜索更多精彩视频」,与"没人推资源"长得**一模一样**。
    """
    from app.services import cookie_store

    raw = (cookie_store.get_cookie(session, user_id, "douyin") or "").strip()
    if not raw:
        raise DouyinCdpError("库里没有抖音 cookie —— 无法以登录态搜索")
    out: list[dict] = []
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        if not name.strip():
            continue
        out.append({"name": name.strip(), "value": value.strip(),
                    "domain": ".douyin.com", "path": "/"})
    return out


def _search_url(keyword: str) -> str:
    return ("https://www.douyin.com/search/"
            f"{urllib.parse.quote(keyword)}?type=video")


#: 从页面里抽结果。**判据写在 JS 里**(DOM 才是这条路的事实源)。
#: ⚠️ 作者**不是**链接(实测搜索卡里 `a[href*="/user/"]` 命中 0 个),而是卡片正文里
#: 单独一行的 `@昵称` ⇒ 只能从 `innerText` 里按行取。
_GRAB_JS = """() => {
    const links = [...document.querySelectorAll('a[href*="/video/"]')];
    const body = document.body ? document.body.innerText : '';
    const seen = new Set();
    const rows = [];
    for (const a of links) {
        const href = a.href || '';
        const m = href.match(/\\/video\\/(\\d+)/);
        if (!m || seen.has(m[1])) continue;
        seen.add(m[1]);
        const box = a.closest('li') || a.parentElement;
        const text = box ? (box.innerText || '').trim() : (a.innerText || '').trim();
        const am = text.match(/^@(\\S.*)$/m);
        rows.push({vid: m[1], text: text, author: am ? am[1].trim() : ''});
    }
    return {
        rows: rows,
        logged_out: /登录后即可搜索/.test(body),
        verify: /请完成.{0,6}验证|拖动.{0,6}滑块|安全验证/.test(body),
    };
}"""


def _rows_of(raw: dict, keyword: str) -> list[dict]:
    """页面原始行 → 上层统一的记录形状(**同一个作品 id 只留第一条**)。"""
    out: list[dict] = []
    seen: set[str] = set()
    for r in raw.get("rows") or []:
        vid = str(r.get("vid") or "").strip()
        if not vid or vid in seen:
            continue
        seen.add(vid)
        name = str(r.get("author") or "").strip()
        text = str(r.get("text") or "")
        if name:
            #: 把作者那一行 `@昵称` 从正文里剔掉(它已经在 `name` 里了,卡片会单独显示)
            text = re.sub(r"^@.*$", "", text, flags=re.M)
        out.append({
            "uid": vid,
            "name": name,
            "url": f"https://www.douyin.com/video/{vid}",
            #: 抖音搜索卡里那串文字**同时**含时长/点赞/标题 ⇒ 整段当 snippet 交给上层
            #: (`《…》` 口令就在里面,而判据本来就是"标题里有没有《》")。
            "snippet": " ".join(text.split()),
            "pan_link": "",
            "keyword": keyword,
        })
    return out


def search(keywords: list[str], *, session=None, user_id: int = 1,
           settings=None) -> list[dict]:
    """按关键词逐个开搜索页读结果。**一个词都没读到就抛 `DouyinCdpError`。**

    ⚠️ 抛错而不是返回空列表 —— 与 `mediacrawler_source.crawl` 同一条纪律:
    空列表会与"抖音上真没人推这个资源"**完全同形**。
    """
    from playwright.sync_api import sync_playwright

    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return []
    ok, why = ensure_running(settings)
    if not ok:
        raise DouyinCdpError(why)

    own = session is None
    if session is None:
        from app.db import get_session_local

        session = get_session_local()()
    try:
        cookies = _cookies_of(session, user_id)
    finally:
        if own:
            session.close()

    gap = between_kw_ms(settings) / 1000.0
    out: list[dict] = []
    seen: set[str] = set()
    last: dict = {}
    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp(endpoint(settings))
        ctx = br.contexts[0] if br.contexts else br.new_context()
        ctx.add_cookies(cookies)
        page = ctx.new_page()                     #: 只开**自己的**标签(纪律 2)
        try:
            for i, kw in enumerate(kws):
                if i:
                    time.sleep(gap)
                page.goto(_search_url(kw), wait_until="domcontentloaded",
                          timeout=_GOTO_TIMEOUT_MS)
                time.sleep(_RESULT_WAIT_S)
                last = page.evaluate(_GRAB_JS) or {}
                if last.get("logged_out"):
                    raise DouyinCdpError(
                        "抖音搜索页显示**未登录**(「登录后即可搜索更多精彩视频」)"
                        "—— 库里的 cookie 不被接受,需要人重登一次")
                if last.get("verify"):
                    raise DouyinCdpError(
                        "抖音要求**过验证**(页面出现安全验证/滑块)—— 本轮停手,别再打")
                for r in _rows_of(last, kw):
                    if r["uid"] in seen:
                        continue
                    seen.add(r["uid"])
                    out.append(r)
        finally:
            try:
                page.close()                      #: 只关自己开的那个标签
            except Exception:  # noqa: BLE001 - 关标签失败不该影响已取到的结果
                logger.debug("关闭 CDP 标签失败", exc_info=True)

    if not out:
        raise DouyinCdpError(
            f"CDP 路 {len(kws)} 个关键词**一条都没读到**"
            f"(页面未登录/未验证,但 DOM 里没有任何 `/video/` 链接)—— 别当成「没人推」")
    logger.info("抖音CDP:%d 个词 → %d 条(去重后)", len(kws), len(out))
    return out
