"""闲鱼登录助手(2026-10-03 迁入 scripts/,取代同名的纯协议版)。

⚠️ **它取代了本文件原来的内容** —— 旧版走 **passport.goofish.com 纯协议二维码流程**、把
登录态写进 `cookie_store`;但 **2026-10-02 起采集默认走浏览器档案**(`app/services/xianyu_browser.py`,
登录态在 `tools/xianyu_profile`),`tenant.run_xianyu` 在浏览器模式下**不读也不校验**那个 cookie
—— 于是旧版扫了**完全没效果**(详见 `doc/API.md` §16b.3 的删除说明)。

**现在这条才是活的**:直接在**服务采集用的那个档案**里登一次,登录态落在档案里,采集即生效。

跑法:`python scripts/xianyu_login.py`
浏览器打开后:① 登录闲鱼 ② 遇到滑块就拖一下。脚本会一直等,检测到登录后**当场验一次接口**。

(原文件在 `tools/` 下 —— 但 **`tools/` 整个被 .gitignore 忽略**,仓库里存的一直是**废弃的协议版**,
等于"正确做法只存在于这台机器上,机器一丢就没了"。所以搬进受跟踪的 `scripts/`。)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# 档案在 `tools/` 下(那是**服务采集时用的同一个档案**,别改)
PROFILE = Path(__file__).resolve().parents[1] / "tools" / "xianyu_profile"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
HOME = "https://www.goofish.com/"
DETAIL_API = "mtop.taobao.idle.pc.detail"
SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
#: 判登录态用的两个接口 —— ⚠️ **都不单独可信**,见 `_session_state`。
#: 实测(2026-11-18)页面明明登录着,而 `loginuser.get` / `user.page.nav`
#: 都回 `FAIL_SYS_SESSION_EXPIRED`,所以要多取一个 `redpoint.query` 交叉验证。
USER_INFO_API = "mtop.idle.web.user.page.nav"
REDPOINT_API = "mtop.taobao.idlemessage.pc.redpoint.query"

_JS = """
async ([api, data]) => {
  const mtop = window.lib && window.lib.mtop;
  if (!mtop) return JSON.stringify({ __err: "no_mtop" });
  try {
    const r = await mtop.request({ api: api, v: "1.0", type: "POST", dataType: "json", data: data });
    return JSON.stringify(r).slice(0, 1500);
  } catch (e) { let x = ""; try { x = JSON.stringify(e); } catch (_) { x = String(e); }
                return JSON.stringify({ __err: x }).slice(0, 1500); }
}
"""


def _session_state(pg) -> str:
    """**真的打一次取用户信息的接口**,别拿 cookie 存在当登录有效。

    ⚠️ 判据取 `user.page.nav`(取当前登录用户),**不是** `loginuser.get` ——
    2026-11-18 实测:页面明明登录着(右上角有用户名、`redpoint.query` 回
    `SUCCESS total=14`),而那两条却都回 `FAIL_SYS_SESSION_EXPIRED`。
    拿 `loginuser.get` 当判据会**把已登录的档案判成"没登录"**,叫用户白登一次。
    `user.page.nav` 同批里也可能回，要配 `redpoint.query`/页面用户名一起看。

    返回 `ok` / `expired` / `unknown`(**认不出来时不要假装已登录**),
    `expired` 只在**两道都否**时才成立。
    """
    def _hit(api: str) -> str:
        try:
            raw = pg.evaluate(_JS, [api, {}])
        except Exception as exc:  # noqa: BLE001 - 页面在跳转(已知竞态)
            return f"unknown({type(exc).__name__})"
        if "SUCCESS" in raw:
            return "ok"
        if "SESSION_EXPIRED" in raw or "USER_NOT_LOGIN" in raw:
            return "expired"
        return "unknown"

    for api in (USER_INFO_API, REDPOINT_API):
        st = _hit(api)
        if st == "ok":
            return "ok"
    return "unknown"


def main() -> int:
    PROFILE.mkdir(parents=True, exist_ok=True)
    print(f"=== 档案:{PROFILE} ===", flush=True)
    print("=== 请在这个窗口里:① 登录闲鱼 ② 有滑块就拖一下 ===", flush=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=False, no_viewport=True,
            channel="msedge", executable_path=EDGE)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            pg.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            print("(首屏导航异常,继续):", exc, flush=True)

        # 等登录:⚠️ **cookie 存在 ≠ 登录有效**(2026-11-18 现场订正)。
        #
        # 原来只判 `{"unb", "cookie2"} <= names` —— 而实测档案里 `unb` 是 11-12
        # 就种下的、一直**留在档案里**,于是脚本会在**登录态早已过期**的档案上
        # 立刻打印「已登录」并收工。同一次抓包里的证据是硬的:
        #   mtop.taobao.idlemessage.pc.loginuser.get → FAIL_SYS_SESSION_EXPIRED::Session过期
        #   mtop.idle.web.user.page.nav              → FAIL_SYS_SESSION_EXPIRED::Session过期
        # 而搜索侧的回包是 `RGV587_ERROR::SM::哎哟喂,被挤爆啦` —— 匿名请求被当成机器人。
        # ⇒ 判据必须是**真的打一次需要登录态的接口**,cookie 只是必要条件。
        #
        # ⚠️ **必须用 `ctx.cookies()` 而不是 `document.cookie`**(2026-10-03 修):
        # `document.cookie` **读不到 HttpOnly cookie**,而 `cookie2`(登录态核心,
        # 服务端采集靠的就是它)正是 HttpOnly —— 用它判据会**永远等不到登录**,
        # 于是"明明已登录"却被判成没登(用户当场指出)。`ctx.cookies()` 走 CDP,
        # HttpOnly 也拿得到。
        ok = False
        for i in range(80):                     # 最多等 4 分钟
            try:
                names = {c.get("name") for c in ctx.cookies()}
            except Exception:  # noqa: BLE001
                names = set()
            if {"unb", "cookie2"} <= names:
                st = _session_state(pg)
                if st == "ok":
                    print(f"=== 已登录且**会话有效**(第 {i*3}s)===", flush=True)
                    ok = True
                    break
                if i % 5 == 0:
                    print(f"    ...cookie 齐了但会话无效({st})—— 请在窗口里重新登录({i*3}s)",
                          flush=True)
            elif i % 5 == 0:
                print(f"    ...等待登录({i*3}s)", flush=True)
            time.sleep(3)
        if not ok:
            print("=== 未检测到**有效**登录态,仍试一次接口 ===", flush=True)

        for _ in range(30):                     # 等 mtop 挂上来
            try:
                if pg.evaluate("() => !!(window.lib && window.lib.mtop)"):
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)

        # 先用搜索拿一个真实商品 id(匿名也能搜)
        s = pg.evaluate(_JS, [SEARCH_API, {"pageNumber": 1, "keyword": "剪映会员", "fromFilter": False,
                                           "rowsPerPage": 5, "sortValue": "", "sortField": "",
                                           "customDistance": "", "gps": "", "propValueStr": {},
                                           "customGps": "", "searchReqFromPage": "pcSearch",
                                           "extraFilterValue": "{}", "userPositionJson": "{}"}])
        iid = ""
        try:
            s2 = s.replace('\\"', '"')
            m = __import__("re").search(r'"itemId":\s*"?(\d+)', s2)
            iid = m.group(1) if m else ""
        except Exception:  # noqa: BLE001
            pass
        print(f"=== 搜索拿到商品 id:{iid or '(没拿到)'} ===", flush=True)

        if iid:
            r = pg.evaluate(_JS, [DETAIL_API, {"itemId": iid}])
            if "TIMEOUT" in r or "__err" in r:
                print(f"✗ 详情接口仍失败:{r[:160]}", flush=True)
                print("   → 若刚才是匿名状态,登录后应能通;仍不行就说明这个接口另有限制", flush=True)
            else:
                print(f"✓ **详情接口通了**({len(r)} 字):{r[:400]}", flush=True)
                print("   → 行情(想要数/收藏/出单)可采,日快照能继续", flush=True)
        time.sleep(5)
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
