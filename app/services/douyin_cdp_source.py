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
import shutil
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
#: 首页预热后停多久再进搜索页。人打开抖音不会 0 秒后立刻跳搜索。
_WARMUP_WAIT_S = 3.0
#: 隔离档案的目录后缀 / 保留份数(每份约 600MB,攒着会把盘吃满)。
_QUARANTINE_TAG = ".flagged-"
_QUARANTINE_KEEP = 1
#: 「换全新档案也没用」的冷却键 —— 见 `_quarantine_allowed`。
_QUARANTINE_AT_KEY = "douyin_cdp_quarantine_failed_at_{uid}"

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


class DouyinCdpError(RuntimeError):
    """CDP 路不可用/取数失败。**绝不返回空列表冒充"没人推"**。

    `profile_suspect=True` 表示页面给的是**「验证码中间页」** —— 这是**页面级证据**,
    说明这个浏览器身份被平台标记了(2026-10-28 受控实测,见 `quarantine_profile`)。
    调用方据此可以**隔离档案、换全新档案重试一次**;别的失败(端口不通/未登录)不适用。
    """

    def __init__(self, msg: str, *, profile_suspect: bool = False) -> None:
        super().__init__(msg)
        self.profile_suspect = profile_suspect


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


def _port(settings=None) -> int:
    return urllib.parse.urlparse(endpoint(settings)).port or 9333


def _close_browser(port: int) -> bool:
    """关掉浏览器(按端口找 PID)。

    ⚠️ 不能用 Playwright 的 `browser.close()`:CDP 连接上它**只是断开连接**,
    浏览器照旧开着(实测)。
    """
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=15)
        text = (out.stdout or b"").decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 - 查不到就当没关掉
        return False
    pids = {line.split()[-1] for line in text.splitlines()
            if f":{port}" in line and "LISTENING" in line.upper()}
    for pid in pids:
        if pid.isdigit():
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10)
    return bool(pids)


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


def quarantine_profile(settings=None) -> str:
    """把**被平台标记的**专用档案挪开 ⇒ 下一次启动得到一个全新空档案。

    ★★ 2026-10-28 受控实测(这条路的档案为什么会坏、换了为什么就好):

    | 实验 | 结果 |
    |---|---|
    | `data/_douyin_profile` + 注入库里 cookie | **连首页都是「验证码中间页」**(body 4 字节) |
    | **全新空白档案**,不注入 cookie | 正常(跳 `/jingxuan`,正文 2573 字) |
    | **全新空白档案** + 注入库里那 66 条 cookie | **正常且已登录**(无「登录」按钮) |
    | 隔离旧档案后跑本模块 `search(["美食"])` | **16 条** |

    ⇒ 不是出口 IP、不是 cookie、不是账号 —— **就是这份档案的身份被标记了**。
    而登录态本来就**只靠注入 cookie**(见 `profile_dir`),所以换空档案**不丢登录态**。

    返回挪走后的路径(没挪成就返回空串)。
    """
    prof = profile_dir(settings)
    if not prof.exists():
        return ""
    #: 档案正在被浏览器占用时 Windows 上 rename 会失败 ⇒ 先按端口把浏览器关掉。
    _close_browser(_port(settings))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = prof.with_name(f"{prof.name}{_QUARANTINE_TAG}{stamp}")
    for attempt in range(3):
        try:
            prof.rename(dest)
        except OSError as exc:
            logger.warning("抖音CDP:隔离档案失败(第 %d 次):%s", attempt + 1, exc)
            time.sleep(1.5)
            continue
        logger.warning("抖音CDP:档案被平台标记,已隔离到 %s ⇒ 下一次启动会新建空档案", dest)
        _prune_quarantine(prof)
        return str(dest)
    return ""


def _prune_quarantine(prof: Path) -> None:
    """只留最近 `_QUARANTINE_KEEP` 份隔离档案。

    每份约 600MB,留着排查一两次就够了;无限攒着会把盘吃满,而盘满了**采集会以
    "文件写不进去"的样子挂掉**,那种失败与抖音完全无关、却最难想到。
    """
    olds = sorted(prof.parent.glob(f"{prof.name}{_QUARANTINE_TAG}*"),
                  key=lambda p: p.name, reverse=True)
    for old in olds[_QUARANTINE_KEEP:]:
        shutil.rmtree(old, ignore_errors=True)
        logger.warning("抖音CDP:清掉更早的隔离档案 %s(只留最近 %d 份)",
                       old, _QUARANTINE_KEEP)


def _quarantine_allowed(session, user_id: int = 1, settings=None) -> bool:
    """**现在**还允不允许再隔离一次?

    ⚠️ 判据是「**上一次隔离之后换的全新档案也被挡了**」这个事实 —— 那种情况下
    再隔离只是每 4 小时丢掉一份 600MB 的档案,而且**真因不在档案**(出口 IP / 账号)。
    所以 `_mark_quarantine_failed` 写下时间后,冷却期内不再隔离,并如实说明。
    """
    if session is None:
        return True
    try:
        from datetime import datetime

        from sqlalchemy import select

        from app.db.models import SystemConfig

        row = session.scalar(select(SystemConfig).where(
            SystemConfig.key == _QUARANTINE_AT_KEY.format(uid=user_id)))
        #: 只读也要收尾事务 —— 后面是几分钟的浏览器慢活,别攥着读事务不放。
        session.commit()
        if not row or not row.value:
            return True
        since = datetime.fromisoformat(str(row.value))
        hours = float(getattr(settings, "douyin_cdp_quarantine_cooldown_hours", 12) or 12) \
            if settings is not None else 12.0
        return (datetime.now() - since).total_seconds() >= hours * 3600
    except Exception:  # noqa: BLE001 - 读不到就当允许(宁可多试一次,也别永远不自愈)
        logger.debug("读取抖音CDP隔离冷却失败,按允许处理", exc_info=True)
        return True


def _mark_quarantine_failed(session, user_id: int = 1) -> None:
    """记下「换全新档案仍被挡」—— 冷却期内别再把档案当替罪羊。"""
    if session is None:
        return
    try:
        from datetime import datetime

        from sqlalchemy import select

        from app.db.models import SystemConfig

        key = _QUARANTINE_AT_KEY.format(uid=user_id)
        val = datetime.now().isoformat(timespec="seconds")
        row = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
        if row:
            row.value = val
        else:
            session.add(SystemConfig(key=key, value=val))
        session.commit()
    except Exception:  # noqa: BLE001 - 写不进去只影响"下次还隔离一次"
        logger.debug("写抖音CDP隔离冷却失败", exc_info=True)


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
        //: ★ 2026-10-25 实测:`/search/<词>` 有时直接返回一张**「验证码中间页」**
        //: (HTTP 200、body 几乎为空)—— 那时 `rows` 为 0,与"这个词没人发过"**完全同形**。
        //: ⚠️ 2026-10-28 收紧成**只看标题**:这个标记现在会触发**自动隔离档案**,
        //: 而正文里出现「验证码」三个字完全可能是某条视频的标题 ⇒ 误判的代价从
        //: "多报一次错"变成了"丢掉一份 600MB 的档案"。实测真信号就是标题这一条。
        captcha: /验证码/.test(document.title || ''),
    };
}"""


_RE_AUTHOR_LINE = re.compile(r"^@.*$", flags=re.M)


def _publish_at(text: str) -> int:
    """从搜索卡片文字里取**发布时间**(epoch 秒);判不出返回 0。

    ★★ 2026-10-28 补 —— 这是个**会静默吞掉全部产出**的缺口:
    卡片文字实测是 5 行 `时长 / 点赞 / 标题 / @作者 / 时间`,时间是相对写法
    (`1小时前`、`2天前`、`1周前`、`5年前`)。而 `douyin_leads.collect_leads` 的
    新鲜度闸对 `publish_at<=0` 的处理是**丢掉**(用户口径「判不了就宁可不收」)
    ⇒ CDP 路哪怕取回 16 条,**一条线索也进不来**,运行记录还写成 success(0 条)。
    这正是本仓最恨的静默失败:链说自己成功,其实是我们自己把数据丢光了。

    ⚠️ 只从**末尾几行**里找:卡片第一行是**时长**(`06:22`),把它当发布时间就全错了
    —— 所以这里不认 `HH:MM`,只看末尾 3 行里能整行匹配相对时间的那些。
    """
    from app.services.hot_time import parse_published_coarse

    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for ln in reversed(lines[-3:]):
        dt = parse_published_coarse(ln)
        if dt is not None:
            return int(dt.timestamp())
    return 0


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
        publish_at = _publish_at(text)          #: ⚠️ 必须在剔掉作者行**之前**取(时间在它后面)
        if name:
            #: 把作者那一行 `@昵称` 从正文里剔掉(它已经在 `name` 里了,卡片会单独显示)
            text = _RE_AUTHOR_LINE.sub("", text)
        out.append({
            "uid": vid,
            "name": name,
            "url": f"https://www.douyin.com/video/{vid}",
            #: 抖音搜索卡里那串文字**同时**含时长/点赞/标题 ⇒ 整段当 snippet 交给上层
            #: (`《…》` 口令就在里面,而判据本来就是"标题里有没有《》")。
            "snippet": " ".join(text.split()),
            "pan_link": "",
            "keyword": keyword,
            #: ★ 缺了它,`collect_leads` 会把整条当"判不出发布时间"丢掉(见 `_publish_at`)。
            "publish_at": publish_at,
        })
    return out


_WARMUP_JS = """() => ({title: document.title || '',
                        captcha: /验证码/.test(document.title || '')})"""


def _warmup(page) -> None:
    """先像人一样落到**首页**,停几秒,再去搜索页。

    ⚠️ 为什么值得多这一次页面加载:改前进搜索页是**冷启动直达**
    `https://www.douyin.com/search/<词>?type=video` —— 全新标签、没有首页访问、
    没有站点自己的首屏脚本跑过。真人不会这么用。小红书那条的实测结论同源
    (`xhs-461-solved-by-page-render`:服务端对"刚铸的会话 + 脚本形状的请求"最敏感)。
    ⚠️ 首页本身就是验证码页 ⇒ **最干净的被标记证据**,直接抛(调用方会换档案重试)。
    预热失败(超时/网络抖动)**不抛** —— 它是改善形状的一步,不该成为新的失败点。
    """
    try:
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                  timeout=_GOTO_TIMEOUT_MS)
        time.sleep(_WARMUP_WAIT_S)
        info = page.evaluate(_WARMUP_JS) or {}
        if info.get("captcha"):
            raise DouyinCdpError(
                "抖音**连首页**都给「验证码中间页」"
                f"(标题:{str(info.get('title'))[:40]})—— 这个浏览器身份被平台标记了,"
                "与「没人推资源」无关", profile_suspect=True)
    except DouyinCdpError:
        raise
    except Exception:  # noqa: BLE001 - 预热失败继续直接搜索
        logger.debug("抖音CDP:首页预热失败(继续直接搜索)", exc_info=True)


def _one_round(kws: list[str], cookies: list[dict], settings, pre_up: bool) -> list[dict]:
    """在**当前**这个浏览器里跑完一轮关键词。任何异常原样抛出,收尾由 finally 负责。"""
    from playwright.sync_api import sync_playwright

    gap = between_kw_ms(settings) / 1000.0
    out: list[dict] = []
    seen: set[str] = set()
    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp(endpoint(settings))
        ctx = br.contexts[0] if br.contexts else br.new_context()
        ctx.add_cookies(cookies)
        page = ctx.new_page()                     #: 只开**自己的**标签(纪律 2)
        try:
            _warmup(page)
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
                if last.get("captcha"):
                    raise DouyinCdpError(
                        "抖音返回的是**「验证码中间页」**(HTTP 200、页面近乎空白)"
                        "—— 这是风控要求过验证,**不是「这个词没人发」**;先停手别再打",
                        profile_suspect=True)
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
            #: ★ **本轮是我们自己起的浏览器 ⇒ 收尾把它关掉**(2026-10-26 用户口径:
            #: 「开了好多的 about:blank,如果没用的可以自动关闭」)。
            #: 只关标签的话,那台浏览器会**停在 about:blank 挂几个小时**等下一轮 ——
            #: 桌面上就是一堆空窗口。下一次采集再起,冷启动约 1~2 秒,代价很低。
            #: ⚠️ 本来就在跑的(比如人工开着调试用)**不许关**。
            if not pre_up:
                _close_browser(_port(settings))
    return out


def search(keywords: list[str], *, session=None, user_id: int = 1,
           settings=None) -> list[dict]:
    """按关键词逐个开搜索页读结果。**一个词都没读到就抛 `DouyinCdpError`。**

    ⚠️ 抛错而不是返回空列表 —— 与 `mediacrawler_source.crawl` 同一条纪律:
    空列表会与"抖音上真没人推这个资源"**完全同形**。

    ★ **档案被标记时自愈一次**(2026-10-28):看到「验证码中间页」⇒ 隔离档案、
    换全新空档案重试**一次**。证据见 `quarantine_profile`;登录态靠注入 cookie,
    换档案**不丢登录**。重试仍被挡 ⇒ 记冷却并如实说"不是档案的问题"。
    """
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return []

    #: ⚠️ 顺序有意如此:**先问端点**再读库 —— 端点不通就不该去碰数据库
    #: (也让"连不上"这条错最快浮出来,不用先等一次解密)。
    pre_up, _ = available(settings)          #: 之前就在跑吗?(决定收尾要不要关)
    ok, why = ensure_running(settings)
    if not ok:
        raise DouyinCdpError(why)

    own = session is None
    if session is None:
        from app.db import get_session_local

        session = get_session_local()()
    try:
        cookies = _cookies_of(session, user_id)

        try:
            out = _one_round(kws, cookies, settings, pre_up)
        except DouyinCdpError as exc:
            if not exc.profile_suspect:
                raise
            if not _quarantine_allowed(session, user_id, settings):
                raise DouyinCdpError(
                    f"{exc}\n⚠️ 本窗口**已经隔离过一次档案、换全新档案仍被挡** ⇒ "
                    f"**问题不在档案**(先查出口 IP / 账号风控),不再隔离",
                    profile_suspect=True) from exc
            moved = quarantine_profile(settings)
            logger.warning("抖音CDP:换全新档案重试一次(旧档案 → %s)", moved or "隔离失败")
            ok, why = ensure_running(settings)   #: 新档案目录是空的,这里会新建
            if not ok:
                raise DouyinCdpError(f"{exc}\n换空档案重试时起不来:{why}") from exc
            try:
                out = _one_round(kws, cookies, settings, pre_up=False)
            except DouyinCdpError as exc2:
                if exc2.profile_suspect:
                    _mark_quarantine_failed(session, user_id)
                    raise DouyinCdpError(
                        f"{exc2}\n⚠️ **换全新空档案也一样被挡** ⇒ 不是档案的问题"
                        f"(出口 IP / 账号风控),12 小时内不再隔离档案",
                        profile_suspect=True) from exc2
                raise

        if not out:
            raise DouyinCdpError(
                f"CDP 路 {len(kws)} 个关键词**一条都没读到**"
                f"(页面未登录/未验证,但 DOM 里没有任何 `/video/` 链接)—— 别当成「没人推」")
        logger.info("抖音CDP:%d 个词 → %d 条(去重后)", len(kws), len(out))
        return out
    finally:
        if own:
            session.close()

