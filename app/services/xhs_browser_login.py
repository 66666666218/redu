# -*- coding: utf-8 -*-
"""小红书:**浏览器持久化登录**(2026-10-12)。

## 为什么要有这条路
前面几轮都卡在"**怎么拿到一枚真登录态 session**"。先看事实:

- `tools/xhs_profiles_scan.py` 实测:**三个档案的 `web_session` 全是 `03…`(游客档)**;
- `.env` 里那枚 `04…` 是真的,但**已过期**;
- 纯协议扫码(`xhs_login.py`)状态机能走到 `2`,但 `qrcode/status` 稳定回 `471 + 空 data`,
  **凭据拿不到**(链路文档 §7 末尾);

⇒ 也就是说:**我们手上从来没有一枚"成套且活着"的真凭据**。
于是所有 `-100 登录已过期` / `-104 没有权限` 都是在**游客身份**上发生的 ——
看起来像风控,其实是**压根没登录**。继续调签名是在错误的层上使劲。

## 这条路的做法(与 `social-auto-upload` 等开源项目同一思路)
**真人浏览器里登一次**,把**成套凭据**(`a1`+`web_session`+`id_token`+… 以及
`localStorage["webSsk"]`)导出归档。之后采集继续走纯协议。

它比纯协议扫码稳在哪:指纹、设备证明、风控挑战全部由**真实浏览器**完成,
我们只需要站在一旁**等它登上去**,不自己模拟任何一步。

## ⚠️ 三条纪律
1. **绝不把 `03…` 写进凭据库** —— 游客档写进去就会"静默失效"(看不出没登录)。
   判据:`web_session` 必须 `04…`,且(能拿到时)`user/me` 的 `guest === false`。
2. **`ssk` 必须与 cookie 同源、成对存**(见 `xhs_xys.py`)。缺它的已登录请求回 `-100`。
3. 用 **持久化档案**(`launch_persistent_context`),不是"注入 cookie 到新浏览器" ——
   后者会让服务端因指纹不符而下发删除指令(链路文档 §7 坑③)。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.utils import get_logger

logger = get_logger(__name__)

#: 真登录态 session 前缀(链路文档 §7 坑① 实测:`03…` 是 `activate` 铸的游客档)
REAL_PREFIX = "04"
#: 登录成功的必要条件:这些 cookie 都到位才算成套
MUST_COOKIES = ("a1", "web_session", "webId")
#: `localStorage` 里 ssk 的键(见 `xhs_xys.LS_KEY`)
LS_SSK = "webSsk"

HOME = "https://www.xiaohongshu.com/explore"
LOGIN_URL = "https://www.xiaohongshu.com/login"


class XhsBrowserLoginError(Exception):
    """浏览器登录的硬失败。`kind` 说清是哪一层。"""

    def __init__(self, msg: str, kind: str = "api") -> None:
        super().__init__(msg)
        self.kind = kind


@dataclass
class LoginResult:
    """登录结果。**`cookies` 与 `ssk` 是成套的**,必须成对存、成对用。"""

    ok: bool
    cookies: dict[str, str]
    ssk_b64: str
    profile: str
    waited_s: float = 0.0
    #: 为什么没成(成功时为空)
    reason: str = ""
    #: ★ **这枚会话是在哪条出口 IP 上铸出来的**(2026-10-12)。
    #: 小红书会话**绑定出口 IP**:登录走 A 出口、采集走 B 出口 ⇒ 服务端回
    #: `300011 当前账号存在异常` 并作废会话。把它记进凭据文件,采集时就能
    #: **先比对再发**(见 `tools/xhs_egress.py`)—— 而不是等到号被判异常才反推。
    egress_ip: str = ""
    #: ★★ **这枚会话是"哪个浏览器"铸出来的 —— UA 原文**(2026-10-12)。
    #: 与 `egress_ip` 同一类"绑定"关系,但绑的是**客户端指纹**:
    #: 本项目第一次拿到真 `04` 凭据时被判 `300011`,逐项排除后定位到的就是这个 ——
    #: 登录用 **MS Edge 154**(`Edg/154.0.0.0`),而协议路硬编码发 **Chrome 138**(无 `Edg/`),
    #: 差 16 个大版本还换了品牌 ⇒ "登录设备 ≠ 调用设备"。
    #: ⇒ **存下来,让采集时原样发出去**,这样浏览器再升级也不会漂(详见 `xhs_fingerprint`)。
    ua: str = ""
    #: ★ **`sec-ch-ua*` 客户端提示头**(与 `ua` 同一类:真实浏览器一定带,我们原来一个都不发)。
    #: 同样从浏览器抓(`navigator.userAgentData`)。抓不到就是空 dict ⇒ 采集时**不发**
    #: (宁可缺,也不编一份 —— 编错的 self-report 比不报更糟)。
    client_hints: dict = field(default_factory=dict)


def _read_cookies(ctx) -> dict[str, str]:
    """读 `xiaohongshu.com` 域下的 cookie。**只读**,不导航。"""
    try:
        return {c["name"]: c["value"]
                for c in ctx.cookies("https://www.xiaohongshu.com") if c.get("value")}
    except Exception:  # noqa: BLE001
        return {}


def _read_ssk(page) -> str:
    """读 `localStorage["webSsk"]`。拿不到回空串(调用方要明确提示缺它)。"""
    try:
        return str(page.evaluate(f"() => localStorage.getItem('{LS_SSK}') || ''") or "")
    except Exception:  # noqa: BLE001
        return ""


def _read_ua(page) -> str:
    """★ 读**这个浏览器**的真实 UA —— 会话指纹的**唯一可信来源**(2026-10-12)。

    为什么必须**当场从浏览器读**,而不是维护一份常量表:
    会话绑定的是"签发它的那个浏览器"。本项目踩过的坑正是 —— 登录用 Edge 154,
    而采集端硬编码 `Chrome/138`,差 16 个大版本且**没有 `Edg/` 标记**,
    服务端于是判"登录设备 ≠ 调用设备"(回 `300011 当前账号存在异常`)。
    ⇒ 从浏览器**原样取出来存进凭据**,采集时原样发出去,就永远不可能漂。

    ⚠️ 无头模式会给出 `HeadlessChrome/...`,那是**自动化自报家门**,
    一律交给 `xhs_fingerprint.normalize_ua` 洗成正常的 `Chrome/`。
    """
    try:
        from app.services.xhs_fingerprint import read_ua

        return read_ua(page)
    except Exception:  # noqa: BLE001
        return ""


def _read_client_hints(page) -> dict:
    """★ 读浏览器的 **`sec-ch-ua*` 客户端提示头**(2026-10-12)。

    与 `_read_ua` 同一类缺陷:真实 Chrome/Edge 发请求时**一定**带这组头,而我们原来一个都不发
    ⇒ 自称浏览器却拿不出浏览器该有的头。⇒ 同样**从浏览器抓,存进凭据,采集时原样发出**。

    ⚠️ 抓不到(无头模式常没有 `userAgentData`)就**回空** —— 调用方**不发**。
    宁可缺,也绝不编一份(self-report 编错比不发更糟)。
    """
    try:
        from app.services.xhs_fingerprint import read_hints

        return read_hints(page)
    except Exception:  # noqa: BLE001
        return {}


def _probe_egress(ctx) -> str:
    """量**浏览器**当前那条出口的公网 IP(开个新标签页,别动正在用的那个)。

    为什么记它:会话是**绑出口 IP** 的。登录时的出口被记下来,后面采集就能
    **先比对再发**,而不是等 `300011` 才知道两条路不在同一个出口上。
    ⚠️ 量不到就回空(**绝不**因为量 IP 失败就影响登录本身)。
    """
    try:
        from app.services import xhs_net

        pg = ctx.new_page()
        try:
            for url in xhs_net._EGRESS_URLS[:3]:
                try:
                    pg.goto(url, wait_until="domcontentloaded", timeout=12000)
                    txt = (pg.inner_text("body") or "")[:300]
                    m = xhs_net._IPV4.search(txt)
                    if m:
                        return m.group(0)
                except Exception:  # noqa: BLE001 - 换下一个回声服务再试
                    continue
        finally:
            try:
                pg.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        logger.debug("量浏览器出口 IP 失败", exc_info=True)
    return ""


def is_real_session(cookies: dict[str, str]) -> bool:
    """★ **是不是真登录态**(不是游客档)。

    判据就是那枚 session 的前缀 —— 这是链路文档 §7 坑① 用 `activate` 对照实测出来的:
    `03…` 游客 / `04…` 真档。**只认这一条**,不拿 `userId` 之类当判据(未登录时也返回)。
    """
    return str(cookies.get("web_session") or "").startswith(REAL_PREFIX)


def wait_for_login(profile_dir: str, *, timeout_s: float = 900.0, poll_s: float = 3.0,
                   headless: bool = False, on_qr: Callable[[], None] | None = None,
                   edge_path: str = "", proxy: dict | None = None) -> LoginResult:
    """打开持久化档案,**等人在浏览器里登进去**,然后返回成套凭据。

    ⚠️ `headless=False` 是**默认且有意的**:这条路的价值就在于"真人 + 真浏览器",
    无头模式下人没法扫码/过验证。只有在"档案里已经登好了、只想核对"时才用 headless。

    `timeout_s` 默认 15 分钟 —— 够人掏手机扫一次码。超时返回 `ok=False`,**不写库**。

    `proxy`:显式给浏览器用的代理(见 `xhs_net.for_playwright`)。
    ⚠️ **登录走哪个出口,采集就必须走同一个** —— 小红书会话绑定出口 IP,
    出口一变就回 `300011 当前账号存在异常`。所以这里与协议路共用同一个开关。
    """
    from playwright.sync_api import sync_playwright

    prof = Path(profile_dir)
    if not prof.is_dir():
        raise XhsBrowserLoginError(f"档案不存在:{prof}", kind="env")

    started = time.time()
    with sync_playwright() as p:
        kw: dict = dict(user_data_dir=str(prof), headless=headless, no_viewport=True)
        if edge_path and Path(edge_path).exists():
            kw.update(channel="msedge", executable_path=edge_path)
        if proxy:
            kw["proxy"] = proxy
        else:
            #: ★ **"不传 proxy" ≠ 直连** —— Chromium 仍会自动探测系统代理,在本机
            #: 会拿到错的东西 ⇒ 登录页直接 `net::ERR_PROXY_CONNECTION_FAILED`(实测)。
            #: 要走直连就必须**显式禁用**它。见 `xhs_net.playwright_direct_args`。
            from app.services import xhs_net

            kw["args"] = list(xhs_net.playwright_direct_args())
        ctx = p.chromium.launch_persistent_context(**kw)
        try:
            page = ctx.new_page() if not ctx.pages else ctx.pages[0]

            #: 先**只读**一次 —— 档案里可能早就登好了(那就不必让人再扫一次)
            ck0 = _read_cookies(ctx)
            ws0 = str(ck0.get("web_session") or "")
            ssk0 = _read_ssk(page) if is_real_session(ck0) else ""

            #: ★★ **`04` 前缀是"必要但不充分"的**(2026-10-12 实测踩到的大坑):
            #: 一枚**已被服务端判 `-100 登录已过期` 的死会话,前缀仍然是 `04`**。
            #: 只认前缀 ⇒ 脚本 **26 秒就报告"登录成功"**,其实只是把死会话又归档了一遍
            #: (顺带把 15 项 cookie 覆盖成 12 项、`webSsk` 也没了 —— 越归档越差)。
            #: ⇒ 早退必须**同时**满足:前缀 `04` **且** `webSsk` 还在(活会话才留着它)。
            if is_real_session(ck0) and ssk0:
                logger.info("小红书浏览器登录:档案 %s 已是真登录态,无需重登", prof.name)
                return LoginResult(True, ck0, ssk0, str(prof), 0.0,
                                   egress_ip=_probe_egress(ctx), ua=_read_ua(page),
                                   client_hints=_read_client_hints(page))

            if is_real_session(ck0) and not ssk0:
                #: 说准是"哪种坏":不是没登过,是**登过但已经不健康** ⇒ 必须重登。
                #: (含糊地报"已登录"会让人以为成功,然后拿死凭据去采,白等一轮。)
                logger.warning("小红书浏览器登录:档案 %s 里有一枚 `04` 会话,但 "
                               "`webSsk` 没了 ⇒ 多半**已被服务端作废**。"
                               "不把它当成功,继续等人重新登录。", prof.name)

            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            if on_qr:
                on_qr()

            deadline = started + timeout_s
            last_n = -1
            while time.time() < deadline:
                time.sleep(poll_s)
                ck = _read_cookies(ctx)
                ws = str(ck.get("web_session") or "")
                n = len(ck)
                if n != last_n:
                    logger.info("小红书浏览器登录:等待中 cookie %d 项,"
                                "web_session=%s…", n, ws[:8])
                    last_n = n
                if is_real_session(ck) and ws != ws0:
                    #: ★★ **光"前缀是 04"不能算登上了** —— 档案里很可能本来就躺着一枚
                    #: **死掉的 `04`**(见 `ws0` 上方那段)。那样这里会在第 3 秒就"成功",
                    #: 拿到的还是原来那枚已被作废的会话。
                    #: ⇒ 唯一可信的判据是:**`web_session` 的值真的变了** ⇒ 服务端重新签发过。
                    #:
                    #: ⚠️ **不要**再加"`webSsk` 从无到有也算"这种逃生通道 —— 试过,会误触发:
                    #: 登录页一加载就可能重新签发 `webSsk`,而 `web_session` 根本没变
                    #: ⇒ 又变成"假成功"。宁可让人多扫一次码,也不要静默地交一枚死凭据。
                    missing = [k for k in MUST_COOKIES if k not in ck]
                    if missing:
                        logger.warning("小红书浏览器登录:session 前缀对了但缺 %s,继续等",
                                       missing)
                        continue
                    ssk = _read_ssk(page)
                    waited = time.time() - started
                    logger.info("小红书浏览器登录:检测到真登录态(等待 %.0fs);ssk %s",
                                waited, "有" if ssk else "**缺**")
                    if not ssk:
                        #: 缺 ssk 会让已登录请求回 -100 —— **明确说出来**,别让它是"静默"
                        logger.warning("小红书浏览器登录:没读到 webSsk —— "
                                       "登录态可能不稳定(已登录请求会缺 x6/x7)")
                    return LoginResult(True, ck, ssk, str(prof), waited,
                                      egress_ip=_probe_egress(ctx), ua=_read_ua(page),
                                      client_hints=_read_client_hints(page))

            ck = _read_cookies(ctx)
            ws = str(ck.get("web_session") or "")
            if is_real_session(ck) and ws == ws0:
                #: ★ 说准:**不是"没登录",而是"没有重新登录"** —— 这两件事修法完全不同。
                #: 前者是去登录,后者是**先退出当前账号再登一次**(否则服务端不会换发会话)。
                reason = (f"等待 {timeout_s:.0f}s:**档案里那枚 `04` 会话一直没被换掉** —— "
                          f"它多半已被服务端作废(`webSsk` 也没了)。"
                          f"请在浏览器里**先退出当前账号,再重新登录**一次。")
            else:
                reason = (f"等待 {timeout_s:.0f}s 未检测到真登录态("
                          f"当前 web_session={ws[:8] or '无'}…,"
                          f"{'游客档' if ws.startswith('03') else '未知'})")
            return LoginResult(False, ck, "", str(prof), time.time() - started, reason)
        finally:
            ctx.close()


def export_to_store(session, user_id: int, res: LoginResult,
                    db=None) -> tuple[bool, str]:
    """把**成套凭据**写进加密 cookie 库 + ssk 表。

    ⚠️ **游客档一律不写** —— 写进去表现就是"静默失效"(看不出没登录),这比报错更糟。
    返回 `(是否写入, 说明)`。
    """
    if not res.ok:
        return False, f"未写入(登录没成功:{res.reason})"
    if not is_real_session(res.cookies):
        return False, "**拒绝写入**:web_session 不是 04… 真档(游客档写进去会静默失效)"

    try:
        from app.services.cookie_store import set_cookie

        blob = "; ".join(f"{k}={v}" for k, v in res.cookies.items())
        set_cookie(session, user_id, "xiaohongshu", blob)
    except Exception as exc:  # noqa: BLE001
        return False, f"写 cookie 库失败:{type(exc).__name__}: {str(exc)[:100]}"

    if res.ssk_b64 and db is not None:
        try:
            from app.services.xhs_xys import save_ssk

            save_ssk(db, user_id, res.ssk_b64)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写 ssk 失败(凭据已存,但已登录请求会缺 x6/x7):%s", exc)

    #: ★★ **指纹也必须跟着 cookie 存**(2026-10-14 定位):库里的凭据**只放得下 cookie**,
    #: 而会话是**这个浏览器**签发的 —— 不存指纹,协议路就会静默退回硬编码 `Chrome/138`,
    #: 撞上 `300011`"登录设备 ≠ 调用设备"。⚠️ 与 cookie/ssk **同一次登录写入**,
    #: 否则会出现"cookie 是新号、指纹还是上一个浏览器"这种**看起来合理的错值**。
    try:
        from app.services import xhs_accounts

        xhs_accounts.save_fp(db or session, user_id, res.ua, res.client_hints)
    except Exception as exc:  # noqa: BLE001
        logger.warning("写指纹失败(凭据已存,但协议路会退回硬编码 UA):%s", exc)

    n = len(res.cookies)
    return True, (f"已写入 {n} 项 cookie"
                  f"{' + ssk' if res.ssk_b64 else ' (**缺 ssk**)'}"
                  f"{' + 指纹' if res.ua else ' (**缺指纹**)'}")
