"""小红书 **CDP 路** —— 接管「你真人在用的那个浏览器」,只做关键词搜索。

## 为什么要有这条路(2026-10-15)

页面路(`xhs_page_source`)是 Playwright **自己 launch** 浏览器 ⇒ 实测
`navigator.webdriver === true`,无头时 UA 里还写着 `HeadlessChrome/`。
而用户从 App 里读到的处罚原文是「**与真人操作习惯不一样**」
「**涉及使用第三方脚本或者 AI 工具进行浏览**」—— 那两句说的就是"这不是人在用浏览器"。

CDP 路反过来:**浏览器由人自己开**(专用档案,手动登录一次),我们只**接管**它。
实测(2026-10-15,零小红书请求):

| 怎么进的浏览器 | `navigator.webdriver` |
|---|---|
| Playwright 自己 launch(页面路) | `True` |
| **CDP 接管(本模块)** | **`False`** |

⇒ 这是目前"最像人"的那条路,**不需要**签名逆向、**不需要**换 IP、**不需要**买服务。

## 三条纪律(每条都有源码级守卫,见 `tests/test_xhs_cdp_source.py`)

1. **绝不去杀浏览器** —— 页面路开跑前会 `kill_stale_browsers()`(清它自己上一轮残留的
   Edge)。那条在这里是**灾难**:它会关掉**人正在用的那个浏览器**。
   ⇒ 本模块**不导入、不调用**它(守卫按 AST 查,连导入都不许)。
2. **只关自己开的那个标签** —— 接管的是**别人的**浏览器,`ctx.pages` 里可能有人正在看的
   页面。我们只 `new_page()`,并且**只有在"它确实不是接管前就有的"**时才关它。
   ⚠️ 绝不 `ctx.pages[0]` 拿现成标签去导航(守卫禁止这个写法)。
3. **连不上就说连不上**,不许静默返回空 —— "CDP 没起来"被上层读成"小红书没热度",
   就是本仓反复栽过的那类**假结论**(见 [[silent-failure-is-fake-success]] 同族)。

## 与页面路的关系

**同形状**:返回 `[{uid, name, url, snippet, pan_link, keyword}]`,与
`xhs_page_source.search()` / `mediacrawler_source.crawl()` 一致 ⇒ 上层不用改。
**同解析**:卡片解析、验证页/限流/未登录三类判据**全部复用页面路的实现**,不写第二份。
**不同处**:launch vs attach;以及由此带来的纪律 1 / 2。
"""
from __future__ import annotations

import json
import urllib.request

from app.utils import get_logger

#: ★ **单一来源**:常量与解析全部从页面路借,不复制第二份
#: (本仓母题:同一件事写两处 ⇒ 迟早各飘各的)。
from app.services.xhs_page_source import (
    CARD_SEL,
    EMPTY_SEL,
    SEARCH_URL,
    XhsPageError,
    _HEADLESS_UA_TOKEN,
    _card_rows,
    needs_login,
    rate_limited,
    verify_needed,
)

logger = get_logger(__name__)

#: 词间休眠的**硬下限**。这条路唯一的优势是"像人",连发就把它丢掉了。
_MIN_BETWEEN_MS = 5000
_DEFAULT_WAIT_MS = 6000
_GOTO_TIMEOUT_MS = 45000


def endpoint(settings=None) -> str:
    """CDP 端点(默认 `http://127.0.0.1:9222`)。"""
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    return str(getattr(settings, "xhs_cdp_url", "") or "").strip() or \
        "http://127.0.0.1:9222"


def between_kw_ms(settings=None) -> int:
    """词间休眠(毫秒),**夹着硬下限** —— 配置里写多小都不许低于 `_MIN_BETWEEN_MS`。"""
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    try:
        got = int(getattr(settings, "xhs_cdp_between_kw_ms", 0) or 0)
    except (TypeError, ValueError):
        got = 0
    return max(got, _MIN_BETWEEN_MS)


def available(settings=None) -> tuple[bool, str]:
    """CDP 端点**现在**能不能连上。**只问 `/json/version`,不碰任何页面、不发小红书请求。**

    ⚠️ 返回 `(False, 原因)` 时调用方**必须**把原因说出来 —— 见模块头纪律 3。
    """
    url = endpoint(settings).rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        return True, f"CDP 可达:{d.get('Browser') or '?'}"
    except Exception as exc:  # noqa: BLE001 - 连不上是**预期状态**,不是异常
        return False, (f"CDP 不可达({url}):{type(exc).__name__} —— "
                       f"先由**人**把浏览器开起来:`python tools/xhs_cdp.py launch`,"
                       f"在里面登录一次小红书,再回来跑这条")


def _warn_if_headless(ctx) -> None:
    """接了 CDP 也别掉进"无头"这个坑:无头 UA 会自报 `HeadlessChrome/`(自曝自动化)。

    ⚠️ 只在**连上之后**顺手探一次,不额外导航。
    """
    try:
        ua = str(ctx.pages[0].evaluate("() => navigator.userAgent") if ctx.pages else "")
    except Exception:  # noqa: BLE001 - 探不到不影响主流程
        return
    if _HEADLESS_UA_TOKEN in ua:
        logger.warning(
            "小红书 CDP 路:接管到的浏览器是**无头**的(UA 里写着 %s)⇒ 等于自报自动化,"
            "这条路就白走了。用它的人请在**有头**窗口里开着那个浏览器。", _HEADLESS_UA_TOKEN)


def _close_own_page(page, before: list) -> None:
    """★ 只关**自己开的那一个**标签 —— 别人的浏览器,别人的标签(纪律 2)。

    `before` 是接管瞬间的标签列表;只要 `page` 在里面,说明它不是我们开的,就**不许关**。
    """
    if any(p is page for p in before):
        logger.warning("小红书 CDP 路:要收尾的标签**不是我们开的** ⇒ 不关它(纪律 2)")
        return
    try:
        page.close()
    except Exception:  # noqa: BLE001 - 收尾失败不该盖过真正的报错
        logger.debug("小红书 CDP 路:关自己那个标签失败", exc_info=True)


def _search_one(page, kw: str, wait_ms: int) -> list[dict]:
    """单个关键词:导航 → 三类判据 → 读卡片。判据与页面路**同一套**(从那边借)。"""
    last = ""
    for _try in (1, 2):
        try:
            page.goto(SEARCH_URL.format(kw=kw), wait_until="commit",
                      timeout=_GOTO_TIMEOUT_MS)
            last = ""
            break
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:70]}"
            logger.info("小红书 CDP 路:打开搜索页第 %d 次失败(%s),重试", _try, last)
            page.wait_for_timeout(2000)
    if last:
        raise XhsPageError(f"打开搜索页失败({kw[:20]}):{last}") from None

    try:
        page.wait_for_selector(CARD_SEL, timeout=20000)
    except Exception:  # noqa: BLE001 - 没卡片:下面按"空态/被拦"分辨
        pass
    page.wait_for_timeout(wait_ms)

    if verify_needed(page.url, page.title() or "", page.content()[:4000]):
        raise XhsPageError(
            "小红书要**安全验证**(被重定向到 captcha 页)—— 请**在那个窗口里**由人过一次;"
            "过完会话即可恢复。⚠️ 别用脚本反复试。", needs_human=True)

    body = ""
    try:
        body = page.inner_text("body")[:600]
    except Exception:  # noqa: BLE001
        pass
    if rate_limited(body):
        raise XhsPageError(
            f"小红书**频率风控冷却中**({body[:60]!r})—— 这类修法是**等**,"
            f"别重登、别重试、别换档案;越试越久。",
            needs_human=False, rate_limit=True)
    if needs_login(body):
        raise XhsPageError(
            "小红书:那个窗口里**没有登录态**(搜索页提示「登录后查看搜索结果」)。"
            "请**在那个窗口里手动登录一次**再跑。⚠️ 别用脚本重登 —— "
            "登录后立刻打脚本请求正是被处罚的行为(见 doc/小红书纯协议-链路拆解.md §10)。")

    page.mouse.wheel(0, 1600)            # 触发懒加载
    page.wait_for_timeout(2000)
    cards = page.locator(CARD_SEL)
    n = cards.count()
    if n == 0 and page.locator(EMPTY_SEL).count():
        logger.info("小红书 CDP 路:「%s」确实没有结果(空态容器在)", kw[:26])
        return []
    rows = _card_rows(cards, kw)
    logger.info("小红书 CDP 路:「%s」卡片 %d 张 → %d 条", kw[:26], n, len(rows))
    return rows


def search(keywords: list[str], settings=None, per_kw_wait_ms: int = _DEFAULT_WAIT_MS,
           between_kw_ms_override: int | None = None) -> list[dict]:
    """按关键词搜小红书(接管真人浏览器)。返回形状与 `xhs_page_source.search` **一致**。

    ⚠️ **不 launch、不 kill、不关浏览器** —— 只 `connect_over_cdp` + 开一个自己的标签。
    """
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return []
    url = endpoint(settings)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # noqa: BLE001
        raise XhsPageError(f"没有 playwright:{exc}") from exc

    #: 先问一次可达性 —— 把"没起来"与"被拦"从一开始就分开(纪律 3)
    ok, why = available(settings)
    if not ok:
        raise XhsPageError(why)

    gap = (max(int(between_kw_ms_override), _MIN_BETWEEN_MS)
           if between_kw_ms_override is not None else between_kw_ms(settings))
    rows: list[dict] = []
    with sync_playwright() as p:
        try:
            br = p.chromium.connect_over_cdp(url)
        except Exception as exc:  # noqa: BLE001
            raise XhsPageError(
                f"连不上 CDP({url}):{type(exc).__name__}: {str(exc)[:90]} —— "
                f"⚠️ **不要改成 Playwright 自己 launch** —— 那样 `navigator.webdriver` "
                f"会变回 True,正是被处罚的那件事(见本模块头)。") from exc
        try:
            ctxs = list(br.contexts)
            if not ctxs:
                raise XhsPageError(
                    f"CDP 连上了({url})但**一个 context 都没有** —— "
                    f"那个浏览器可能刚启动还没建窗口,稍等几秒重试")
            ctx = ctxs[0]
            _warn_if_headless(ctx)
            before = list(ctx.pages)            #: ★ 纪律 2 的基准
            page = ctx.new_page()               #: 只开自己的,绝不用现成标签
            try:
                for i, kw in enumerate(kws):
                    if i:
                        page.wait_for_timeout(gap)      #: 低频:词间硬休眠
                    rows.extend(_search_one(page, kw, per_kw_wait_ms))
            finally:
                _close_own_page(page, before)
        finally:
            #: ⚠️ Playwright 的 `browser.close()` 对 **connected** 的 browser 语义是
            #: **断开连接**(官方措辞:"disconnects from the browser server"),
            #: 与 `launch()` 出来的那种"关掉浏览器和它所有页面"**不是一回事**。
            #: 实测(2026-10-15,临时档案 + 9333 端口):`br.close()` 之后
            #: **浏览器进程仍然活着**(`proc.poll() is None`),标签也还在。
            #: ⇒ 这里可以放心调它;**真正的纪律是"绝不 launch"**(见模块头纪律 1)。
            try:
                br.close()
            except Exception:  # noqa: BLE001
                logger.debug("小红书 CDP 路:断开 CDP 失败(忽略)", exc_info=True)
    return rows
