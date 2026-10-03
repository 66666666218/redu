"""闲鱼采集的**浏览器路径**(2026-10-02):让闲鱼自己的 JS 去发请求。

**为什么换这条路**:我们一直是"纯协议" —— 自算 mtop 签名 + `curl_cffi` 伪造 Chrome 指纹,
结果被 `RGV587_ERROR::哎哟喂,被挤爆啦` **账号级**限流(实测换手机热点出口也没用、
请求量只有 6 次/小时,所以既不是 IP 也不是频率)。GitHub 上两个能跑的现成项目
([mercy719/goofish-mcp-server](https://github.com/mercy719/goofish-mcp-server)、
[cnfug/ai-goofish](https://github.com/cnfug/ai-goofish))**都不自己签名**,而是:

    Playwright 打开真页面 → 在页面上下文里调 `window.lib.mtop.request({...})`
    → 签名、token、浏览器指纹**全由闲鱼自己的 JS**完成

这跟我们在**抖音**(MediaCrawler 让页面算签名)、**迅雷**(借网页版铸 captcha)上
跑通的路子一致 —— 闲鱼原本是最后一个还在"伪造指纹"的源。

**实测(2026-10-02,同一台机器同一个闲鱼)**:纯协议被挤爆,而页面内调用正常
(`numFound=224128`)。

**接口对齐**:本类与 `xianyu.XianyuClient` 提供同样的 `.search(keyword, page, rows)`,
所以 `xianyu.collect_hot(settings, client=...)` **直接换客户端即可**,调用方零改动。

⚠️ **浏览器要复用**:开一次约 10~20 秒,一轮里多个关键词共用同一个页面(惰性启动,
调用方用完 `close()`)。另外**闲鱼的重点是虚拟资料**(课程/素材/软件/网盘资源),
所以关键词表本身就是那一路,别拿实物词去测。
"""
from __future__ import annotations

import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.services.xianyu import API, XianyuError, _extract_items

DETAIL_API = "mtop.taobao.idle.pc.detail"      # 商品详情(与 XianyuClient.detail 同一个接口)
from app.utils import get_logger

logger = get_logger(__name__)

HOME = "https://www.goofish.com/"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# 页面内调用:签名/token/指纹都由页面处理。⚠️ `type` 是**请求方法**(GET/POST),
# 不是 dataType —— 我第一版写成 "originaljson",被回 `UNEXCEPT_REQUEST::错误的请求类型`(实测踩过)。
_JS_SEARCH = """
async ([api, payload]) => {
  const mtop = (window.lib && window.lib.mtop) || null;
  if (!mtop || typeof mtop.request !== "function") return JSON.stringify({ __err: "no_mtop" });
  try {
    const res = await mtop.request({ api: api, v: "1.0", type: "POST", dataType: "json",
                                    data: payload });
    return JSON.stringify(res);
  } catch (e) {
    let err = "";
    try { err = JSON.stringify(e); } catch (_) { err = String((e && e.message) || e); }
    return JSON.stringify({ __err: err });
  }
}
"""

# 钩住页面**自己的** mtop 请求,把搜索响应截下来(见 `_search_by_page` 的说明)。
# 用 `add_init_script` 注入:它会在**每次页面加载**时先于站点脚本执行,所以跳转到搜索页之后
# 依然有效(⚠️ 实测:用 `page.evaluate` 装钩子会被跳转清掉,截获恒为 0)。
_HOOK_INIT = """
(() => {
  window.__xy_cap = [];
  const patch = () => {
    if (!(window.lib && window.lib.mtop)) return false;
    if (window.lib.mtop.request.__xy_patched) return true;   // 别重复包(同一页可能加载多次)
    const orig = window.lib.mtop.request;
    window.lib.mtop.request = async function (opts) {
      const r = await orig.call(this, opts);
      try {
        if (String((opts || {}).api).indexOf("mtop.taobao.idlemtopsearch") >= 0) {
          window.__xy_cap.push(r);
        }
      } catch (_) {}
      return r;
    };
    window.lib.mtop.request.__xy_patched = true;
    return true;
  };
  const t = setInterval(() => { if (patch()) clearInterval(t); }, 100);
})();
"""

_SEARCH_URL = "https://www.goofish.com/search?q="


class XianyuBrowserClient:
    """与 `XianyuClient.search` 同接口;惰性开浏览器,一轮内复用同一页。"""

    def __init__(self, settings=None, profile_dir: str = "", headless: bool = False) -> None:
        # ⚠️ **默认有头**:实测无头模式下闲鱼 mtop 回 `TIMEOUT::接口超时`
        # (有头则正常返回 numFound=22 万)—— 它的风控会看无头特征。服务跑在用户桌面,
        # 开一个浏览器窗口可接受(MediaCrawler 那几条链本来就是有头跑的)。
        self._settings = settings
        self._headless = headless
        self._profile = profile_dir or str(
            Path(__file__).resolve().parents[2] / "tools" / "xianyu_profile")
        self._pw = None
        self._ctx = None
        self._pg = None

    # ---------------------------------------------------------------- 生命周期
    def _ensure_page(self):
        """惰性启动:第一次 search 才开浏览器,之后复用(开一次 10~20 秒,别每词开一次)。"""
        if self._pg is not None:
            return self._pg
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        kwargs = {"user_data_dir": self._profile, "headless": self._headless,
                  "no_viewport": True}
        try:                                    # 优先用真实 Edge(指纹更真);没有就退回自带 Chromium
            self._ctx = self._pw.chromium.launch_persistent_context(
                channel="msedge", executable_path=EDGE, **kwargs)
        except Exception:  # noqa: BLE001
            logger.info("闲鱼浏览器路径:未找到 Edge,改用自带 Chromium")
            self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        self._pg = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        # 装钩子:**必须在 goto 之前**,`add_init_script` 保证每次页面加载都先于站点脚本执行
        try:
            self._ctx.add_init_script(_HOOK_INIT)
        except Exception:  # noqa: BLE001 - 钩子装不上还有直连那条兜底
            logger.debug("闲鱼搜索钩子注入失败", exc_info=True)
        self._pg.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        try:                                    # goofish 首页会自己做客户端跳转,等它静下来
            self._pg.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 - 等不到就继续,后面 evaluate 还有重试
            logger.debug("闲鱼首页 networkidle 等待超时,继续")
        for _ in range(30):                     # mtop 是按需加载的,等它挂上来
            try:
                if self._pg.evaluate("() => !!(window.lib && window.lib.mtop)"):
                    break
            except Exception:  # noqa: BLE001
                pass
            self._pg.wait_for_timeout(1000)
        return self._pg

    def close(self) -> None:
        for obj, meth in ((self._ctx, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001 - 收尾失败不该冒泡
                logger.debug("闲鱼浏览器关闭异常", exc_info=True)
        self._pg = self._ctx = self._pw = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def cookie_header(self) -> str:
        """对齐 `XianyuClient.cookie_header()`(调用方会用它回写轮换后的 token)。

        **浏览器路径返回空串** —— 登录态在浏览器档案里、由页面自己维护,
        没有"回写 Cookie"这一步;返回空串让调用方**静默跳过**,
        而不是抛 AttributeError 刷一条 WARNING(实测踩过)。
        """
        return ""

    def _eval_api(self, pg, api: str, payload: dict) -> str:
        """页面内调用 + **重试**。

        ⚠️ 实测(2026-10-02 23:45 那轮定时采集):`goto` 之后页面**自己又跳了一次**
        (goofish 首页有客户端跳转),`evaluate` 正好撞上就报
        `Execution context was destroyed, most likely because of a navigation` ——
        同一段代码手跑却没事,因为是竞态。**等一下重来**即可,别把它当"闲鱼挂了"。
        """
        last = None
        for _ in range(3):
            try:
                return pg.evaluate(_JS_SEARCH, [api, payload])
            except Exception as exc:  # noqa: BLE001
                last = exc
                msg = str(exc)
                if "Execution context was destroyed" in msg or "navigation" in msg:
                    pg.wait_for_timeout(1500)
                    continue
                raise
        raise XianyuError(f"闲鱼页面内调用重试 3 次仍失败({api}):{str(last)[:140]}")

    # ---------------------------------------------------------------- 采集
    def _search_by_page(self, keyword: str) -> list[dict]:
        """**让页面自己去搜**,把它的搜索响应截下来(首选路)。

        ⚠️ **为什么不再自己调 mtop**(2026-10-03 实测):同一浏览器、同一登录态下,
        `window.lib.mtop.request({api: "mtop.taobao.idlemtopsearch.pc.search"})`
        —— 我们用了很久的那条路 —— 会被回 **`TIMEOUT::接口超时`**;
        而**页面自己的 JS 发同样的搜索完全正常**(同一时刻测:搜索接口超时,
        而 `idlehome.feed` / `user.page.nav` 都正常返回 → **不是环境被封、也不是登录失效,
        是这一个接口被单独限了**)。手点搜索页也能正常出商品(实测 121 个节点)。

        做法:`add_init_script` 钩住 `window.lib.mtop.request` → 跳到搜索页让**页面自己**发请求
        → 读回它拿到的**结构化响应**(比解析 DOM 稳,还复用现成的 `_extract_items`)。
        """
        pg = self._pg
        pg.goto(_SEARCH_URL + urllib.parse.quote(keyword), wait_until="domcontentloaded",
                timeout=60000)
        items: list[dict] = []
        for _ in range(24):                       # 最多等 ~24 秒,等页面把搜索请求发出去
            try:
                caps = json.loads(pg.evaluate("() => JSON.stringify(window.__xy_cap || [])") or "[]")
            except Exception:  # noqa: BLE001 - 页面在跳转(已知竞态)→ 等一下再来
                pg.wait_for_timeout(1000)
                continue
            for cap in caps:
                items.extend(_extract_items(cap))
            if items:
                break
            pg.wait_for_timeout(1000)
        return items

    def _search_direct(self, pg, keyword: str, page: int, rows: int) -> list[dict]:
        """**直连**路(老做法):在页面上下文里自己调 mtop。

        现在多半会被回 `TIMEOUT::接口超时`(见 `_search_by_page`),留作兜底 ——
        万一哪天平台又放开、或钩子因为站点改版失效,这条路还能顶上。
        """
        payload = {
            "pageNumber": page, "keyword": keyword, "fromFilter": False, "rowsPerPage": rows,
            "sortValue": "", "sortField": "", "customDistance": "", "gps": "", "propValueStr": {},
            "customGps": "", "searchReqFromPage": "pcSearch", "extraFilterValue": "{}",
            "userPositionJson": "{}",
        }
        raw = self._eval_api(pg, API, payload)
        try:
            obj = json.loads(raw)
        except ValueError as exc:
            raise XianyuError(f"闲鱼浏览器路径返回非 JSON:{str(exc)[:120]}") from exc
        if isinstance(obj, dict) and obj.get("__err"):
            err = str(obj["__err"])
            # 页面内也被要求验证 → 与协议路同样归类,交给上层冷却/告警
            if "USER_VALIDATE" in err or "RGV587" in err:
                raise XianyuError(f"闲鱼页面内也被风控:{err[:160]}")
            raise XianyuError(f"闲鱼浏览器路径失败:{err[:160]}")
        return _extract_items(obj)

    def search(self, keyword: str, page: int = 1, rows: int = 30) -> list[dict]:
        """与 `XianyuClient.search` 同签名同返回(商品 dict 列表)。

        **先走"页面自己搜"那条路**(见 `_search_by_page`),拿不到再退回直连。
        """
        pg = self._ensure_page()
        items: list[dict] = []
        try:
            items = self._search_by_page(keyword)
        except Exception as exc:  # noqa: BLE001 - 页面路失败不该直接判死刑,还有直连兜底
            logger.debug("闲鱼页面驱动搜索失败(%s):%s", keyword, str(exc)[:120])
        if not items:
            items = self._search_direct(pg, keyword, page, rows)
        if not items:
            raise XianyuError(f"未解析到商品,keyword={keyword}")
        logger.debug("闲鱼(浏览器路径)搜索 %s → %s 条", keyword, len(items))
        return items

    def detail(self, item_id: str) -> dict:
        """商品详情 —— **行情(想要数/收藏/出单/浏览量)的唯一来源**,与 `XianyuClient.detail` 同签名。

        ⚠️ 深采此前**还在用纯协议客户端**,而搜索早换了浏览器路 → 行情一直卡在被挤爆那条路上
        (2026-10-03 发现)。这里补齐,让两条路一致。
        """
        pg = self._ensure_page()
        raw = self._eval_api(pg, DETAIL_API, {"itemId": item_id})
        try:
            obj = json.loads(raw)
        except ValueError as exc:
            raise XianyuError(f"闲鱼详情(浏览器路径)返回非 JSON:{str(exc)[:120]}") from exc
        if isinstance(obj, dict) and obj.get("__err"):
            raise XianyuError(f"闲鱼详情(浏览器路径)失败:{str(obj['__err'])[:140]}")
        return obj if isinstance(obj, dict) else {}


class _ThreadBoundClient:
    """把浏览器客户端**钉在一个专属线程**上(Playwright 的 sync 对象有线程亲和性)。

    ⚠️ **为什么必须**(2026-10-03 实测):调度器是 `ThreadPoolExecutor(24)`(见 `scheduler.
    _scheduler_kwargs`),每个作业落在**任意**工作线程;FastAPI 的同步端点又各在自己的线程池
    线程。而进程级单例里那个 Playwright 对象**只能在创建它的线程里用** —— 实测 00:50 那轮采集
    报 `Cannot switch to a different thread`(前两轮恰好复用同一线程所以没事,**第三轮换了线程就炸**;
    这种"偶发、看起来像网络抖动"的失败最难查)。

    做法:`max_workers=1` 的执行器,**惰性**创建 —— 浏览器只在**这一个线程**里开、用、关。
    顺带把"同一档案被两个上下文抢"的 `TargetClosedError` 也一并消掉(单线程 = 单持有者)。
    """

    def __init__(self, settings=None) -> None:
        self._settings = settings
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xianyu-browser")
        self._client: XianyuBrowserClient | None = None

    def _call(self, name: str, *args, **kwargs):
        """在专属线程里执行(客户端也在那个线程里惰性创建)。

        ⚠️ 提交的函数**不许再调 `_call`**(会自己等自己 → 死锁);它只碰 `self._client`。
        """
        def _run():
            if self._client is None:
                self._client = XianyuBrowserClient(settings=self._settings)
            return getattr(self._client, name)(*args, **kwargs)

        return self._pool.submit(_run).result()

    # ---- 与 `XianyuBrowserClient` 同接口(调用方零改动) ----
    def search(self, keyword: str, page: int = 1, rows: int = 30) -> list[dict]:
        return self._call("search", keyword, page, rows)

    def detail(self, item_id: str) -> dict:
        return self._call("detail", item_id)

    def cookie_header(self) -> str:
        return ""

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """关闭也要**在那个线程里**做 —— Playwright 对象同样不能跨线程。"""
        try:
            if self._client is not None:
                self._pool.submit(self._client.close).result()
        except Exception:  # noqa: BLE001 - 收尾失败不该冒泡
            logger.debug("闲鱼浏览器关闭异常", exc_info=True)
        finally:
            self._client = None
            self._pool.shutdown(wait=False)


_CLIENT: "_ThreadBoundClient | None" = None


def get_client(settings=None) -> "_ThreadBoundClient":
    """进程内**复用**一个浏览器客户端(线程安全:实际工作全在专属线程里)。

    开一次浏览器约 10~20 秒,一轮里有多个关键词 —— **复用同一个页面**,别每词开关一次。
    """
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _ThreadBoundClient(settings=settings)
    return _CLIENT


def close_client() -> None:
    """关掉常驻客户端(测试/优雅停机用)。"""
    global _CLIENT
    if _CLIENT is not None:
        _CLIENT.close()
        _CLIENT = None
