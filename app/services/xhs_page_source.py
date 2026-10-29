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

import json
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

#: ★★ **反自动化痕迹**(2026-10-13 实测补上)
#: 我们这条页面路实测 `navigator.webdriver === true`、无头时 UA 里还写着
#: `HeadlessChrome/154.0.0.0` ⇒ 等于举着牌子说"我是脚本"。
#:
#: 这不是玄学,是**本项目踩过两次的同一个错配换了位置**:协议路上我们已经修好了
#: "登录 UA(Edge 154)≠ 请求 UA(硬编码 Chrome 138)";而**页面路上它一直在** ——
#: 登录脚本用**有头**浏览器、`xhs_page_source` 默认**无头**,于是采集时的 UA
#: 变成 `HeadlessChrome`,与签发会话的那个浏览器**不是同一个**。
#:
#: 社区口径也一致(16k stars 的 `xpzouying/xiaohongshu-mcp`、RedBookC 等):
#: **Cookie 离开原始浏览器环境就会"设备特征对不上、行为链路断了"** ⇒
#: 要尽量让采集与登录处在**同一个浏览器环境**里。
#:
#: ⚠️ 只做**业界标准那三条**,不做过度伪造:伪造出一堆互相矛盾的特征
#: (Windows + Safari、分辨率与 GPU 不匹配)反而更像假环境。
_STEALTH_JS = """(() => {
  const hide = (obj, prop, val) => {
    try { Object.defineProperty(obj, prop, { get: () => val }); } catch (e) {}
  };
  hide(navigator, 'webdriver', undefined);
  if (!window.chrome) { window.chrome = {}; }
  if (!window.chrome.runtime) { window.chrome.runtime = {}; }
})();"""

#: 无头 Chromium 会在 UA 里写这个 ⇒ 见 `_STEALTH_JS` 的说明
_HEADLESS_UA_TOKEN = "HeadlessChrome/"


def _launch_ua(prof: Path) -> str:
    """取这个档案**登录时存档里那份真实 UA**(读不到回空 ⇒ 不覆盖,但要记 warning)。

    与 `xhs_accounts.ua_of()` 同源(都是 `data/xhs_session_<档案名>.json` 的 `ua` 字段);
    这里单独读一遍是为了**不在页面路引入对账号池的模块级依赖**(那边反向依赖本模块)。
    """
    try:
        fp = _root() / "data" / f"xhs_session_{prof.name}.json"
        got = json.loads(fp.read_text(encoding="utf-8"))
        return str((got or {}).get("ua") or "").strip()
    except Exception:  # noqa: BLE001 - 读不到只是"没法对齐",不该让采集起不来
        return ""


#: 关掉 Blink 的"正被自动化控制"特性(与 `_STEALTH_JS` 配套)
_NO_AUTOMATION_ARG = "--disable-blink-features=AutomationControlled"


def _launch_kwargs(prof: Path, headed: bool) -> dict:
    """组出 `launch_persistent_context` 的参数(**单独抽出来是为了能测**)。

    ★ 无头时要**显式带上登录时那份 UA**:否则 Chromium 会把它写成
    `HeadlessChrome/<ver>`,与签发会话的那个浏览器**不是同一个**。
    拿不到存档 UA 就**宁可不覆盖**(覆盖成错的更糟),但调用方会记一条 warning。

    ★★ **出口必须显式给浏览器**(2026-10-22):这里原来**完全不管代理** ——
    浏览器只会去探测系统代理。代理软件退出后注册表残留死端口(实测
    `127.0.0.1:7892`)⇒ 两条页面路一起 `ERR_PROXY_CONNECTION_FAILED`,
    表现为"小红书整平台失败",而协议路看起来又是"账号被限",极难归因。
    ⇒ 统一用 `xhs_net.for_playwright()` 判出口:
      · 有可用出口(专用代理/活着的系统代理)→ 显式 `proxy=`;
      · 没有 → `--proxy-server=direct://`(**实测有效**;`--no-proxy-server` 无效,
        见 `xhs_net.playwright_direct_args` 的对照表)。
    """
    kw: dict = {"user_data_dir": str(prof), "headless": not headed,
                "no_viewport": True, "args": [_NO_AUTOMATION_ARG]}
    try:
        from app.services import xhs_net

        proxy = xhs_net.for_playwright()
    except Exception:  # noqa: BLE001 - 判出口失败时**按直连**走,别让浏览器自作主张
        logger.debug("小红书页面路:解析出口失败,按直连处理", exc_info=True)
        proxy = None
        kw["args"] = [_NO_AUTOMATION_ARG, "--proxy-server=direct://"]
    else:
        if proxy:
            kw["proxy"] = proxy
        else:
            kw["args"] = [_NO_AUTOMATION_ARG, *xhs_net.playwright_direct_args()]
    ua = _launch_ua(prof)
    if ua:
        kw["user_agent"] = ua
    return kw


class XhsPageError(Exception):
    """页面路硬失败(被拦 / 未登录 / 浏览器起不来)。**必须冒泡**,不能当成"没搜到"。

    调用方(`search`)按这两个标记决定"怎么处理这个号":
      · `needs_human=True` ⇒ 这号要人去动一下(**过安全验证** 或 **重新登录**)——
        标进池子(`need_verify`,12 小时冷却)并**换下一个**;
      · `rate_limit=True`  ⇒ 这是**频率风控**(「请勿频繁操作」)——
        ⚠️ 与上面**不是一回事**:这一类**不用人做什么,只需要等**。
        标进池子(`rate_limited`,3 小时冷却)并**换下一个**;
        **绝不能**复用 `needs_human` 那条(它没有时长概念 ⇒ 会把号**永远**钉在
        "需验证"上,等不到任何一次"成功"来清它 ⇒ 一盏**永远红的假红灯**)。
      · 两者都 `False` ⇒ 换个号也没用(如浏览器起不来),直接上抛。
    """

    def __init__(self, msg: str, needs_human: bool = False,
                 rate_limit: bool = False) -> None:
        super().__init__(msg)
        self.needs_human = needs_human
        self.rate_limit = rate_limit


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


def _pkey(profile) -> str:
    """档案的**归一化身份**(绝对规范路径)。

    ⚠️⚠️ **为什么必须有**(2026-10-09 实测踩到):原来 `mark`/`clear` 两边都直接用
    `str(profile)` —— 于是**存的是绝对路径、摘的时候传相对路径就永远对不上**
    (绝对路径 与 相对路径 不是同一个字符串,字符串相等永远为假)。
    后果是**单向陷阱**:记号一旦写下,除了"轮次成功"那一条路,**谁也别想摘掉它**;
    而小红书当时整条链是坏的(协议 `-104` + 页面路不可用)⇒ **那一轮永远等不到**
    ⇒ 体检上就挂了一条**永远红的假红灯**,而假红灯会训练人忽略整份报告。
    """
    try:
        return str(Path(profile).resolve())
    except Exception:  # noqa: BLE001 - 归不了就当原样,别让体检自己崩
        return str(profile)


def mark_need_verify(profile: Path) -> None:
    """把某个账号标成"要人过一次安全验证 / 重新登录",这样轮换会**跳过它**、不继续烧风控。

    ★ **2026-10-12:同时写进统一账号池**(`xhs_accounts`,状态 `need_verify` / 12h 冷却)。
      - 老的那张 `xhs_account_need_verify` 名单**继续写**:一是兼容历史读取方,
        二是它**没有时长**,语义正好等于"要人、且不会自己好" —— 与 `need_verify` 一致。
      - 池子那份是给**协议路**看的:同一台机器上"这个号要人去弄"这件事,
        两条路从此**共享**,不用各记一份。
    """
    try:
        import json

        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import SystemConfig

        with get_session_local()() as db:
            row = db.scalar(select(SystemConfig).where(SystemConfig.key == _NEED_KEY))
            cur = set(json.loads(row.value)) if row is not None and row.value else set()
            cur.add(_pkey(profile))
            blob = json.dumps(sorted(cur), ensure_ascii=False)
            if row is None:
                db.add(SystemConfig(key=_NEED_KEY, value=blob))
            else:
                row.value = blob
            db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("标记'需验证'失败(不影响本轮)", exc_info=True)
    #: 池子那份**单独 try** —— 上面那张名单写失败不该连累它,反之亦然
    try:
        from app.services import xhs_accounts as pool

        pool.report_profile(profile, pool.STATE_NEED_VERIFY, "页面路:需人工过验证/重登")
    except Exception:  # noqa: BLE001
        logger.debug("把'需验证'写进账号池失败", exc_info=True)


def _raise_not_logged_in(name: str, prof) -> None:
    """★ 抛"未登录"错 —— **但两种未登录的修法相反,必须分开**(2026-10-12)。

    ① **从来没登过**(存档里没有 `04` 会话)⇒ `needs_human=True` ⇒ 要人去登;
    ② **登过、但被踢下线**(存档里明明记着 `04`,页面却说未登录)⇒ `rate_limit=True`
       ⇒ **只需等**。这是**设备维度**的风控:小红书按设备限制同时在线的账号数,
       同一台机器短时间内连登多个号会把前一个挤掉(实测:刚登完几分钟 `04` 就降级成
       `03`、`webSsk` 归零)。

    ⚠️ **把 ② 报成"去重登"是本仓最坏的一类错**:它会把人推进一个自我加剧的循环 ——
    每登一个新号就再触发一次设备风控、又把新号踢掉,于是永远修不好。
    """
    if _had_real_session(prof):
        _report_profile_rate_limited(prof, "会话被踢下线(设备登录超限)")
        raise XhsPageError(
            f"账号**被踢下线**(档案 {name} 存档里是 `04` 真登录,"
            f"但页面提示「登录后查看搜索结果」)—— 这不是没登录,"
            f"而是**设备维度**的风控:同一台机器短时间内登多个号,"
            f"小红书会把先前的挤掉(实测刚登完几分钟 `04` 就降级成 `03`)。"
            f"⚠️ **修法是等,不是重登** —— 此时再登只会再触发一次,"
            f"把新号也踢掉。等几小时~一天,之后**只用这一个号、别再切换**。",
            needs_human=False, rate_limit=True)
    raise XhsPageError(
        f"账号未登录(搜索页提示「登录后查看搜索结果」)—— 对档案 "
        f"{name} 跑 `python tools/xhs_pass_verify.py {prof}` 登录一次",
        needs_human=True)


def _had_real_session(profile) -> bool:
    """这个档案**存档里**是否记着一枚 `04` 真登录会话?

    用来把"未登录"分成两种(修法相反,见调用处):
      · 存档有 `04`、页面却说未登录 ⇒ **被踢下线**(设备/频率风控)⇒ 该等;
      · 存档没有 ⇒ 真没登过 ⇒ 该去登。

    只读本地文件,**不发任何请求**。
    """
    try:
        fp = _root() / "data" / f"xhs_session_{Path(profile).name}.json"
        if not fp.exists():
            return False
        d = json.loads(fp.read_text(encoding="utf-8"))
        ws = str((d.get("cookies") or {}).get("web_session") or "")
        return ws.startswith("04")
    except Exception:  # noqa: BLE001 - 读不到就当"没登过",退回原来的修法
        logger.debug("读档案会话存档失败(按没登过处理)", exc_info=True)
        return False


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _pool_need_verify() -> set[str]:
    """池里"需验证"的档案集合。**纯读**,读不到就当空(不让体检/轮换因此崩)。"""
    try:
        from app.services import xhs_accounts as pool

        return pool.need_verify_profiles()
    except Exception:  # noqa: BLE001
        logger.debug("读账号池'需验证'失败(当空处理)", exc_info=True)
        return set()


def _pool_cooling() -> set[str]:
    """池里"还在冷却"的档案集合(**绝对路径**)。纯读,读不到当空。"""
    try:
        from app.services import xhs_accounts as pool

        return pool.cooling_profiles()
    except Exception:  # noqa: BLE001
        logger.debug("读账号池冷却失败(当空处理)", exc_info=True)
        return set()


def _pool_harmful() -> dict[str, int]:
    """池里"**越试越糟**"的冷却档案 `{绝对路径: 剩余分钟}`(频率/账号限制)。纯读,读不到当空。"""
    try:
        from app.services import xhs_accounts as pool

        return pool.harmful_cooldowns()
    except Exception:  # noqa: BLE001
        logger.debug("读账号池'有害冷却'失败(当空处理)", exc_info=True)
        return {}


def _report_profile_rate_limited(profile, detail: str) -> None:
    """把"这个号进了频率风控"报进池子 ⇒ 拿一个**带时刻**的冷却(3 小时)。

    ⚠️ 这是原先**缺的那一环**:页面路早就能认出「请勿频繁操作」,但那时**没有地方
    存"要冷多久"**,只能"如实报出来"然后每轮重新撞(而越撞越久)。
    现在池子按 `rate_limited` 记 3 小时,到点自己放出来。
    """
    try:
        from app.services import xhs_accounts as pool

        pool.report_profile(profile, pool.STATE_RATE_LIMITED, detail)
    except Exception:  # noqa: BLE001
        logger.debug("把'频率冷却'写进账号池失败", exc_info=True)


def _norm_path(p) -> str:
    """路径的**归一化身份**,与 `xhs_accounts.profile_key` 同一套(按仓库根解析)。

    用来把"配置里写的相对路径"与"池子里的绝对路径"对上 —— 不然比对永远为假。
    """
    try:
        from app.services import xhs_accounts as pool

        return pool.profile_key(p)[len(pool.PROFILE_PREFIX):]
    except Exception:  # noqa: BLE001
        try:
            return str(Path(p).resolve())
        except Exception:  # noqa: BLE001
            return str(p)


def need_verify_profiles() -> set[str]:
    """哪些账号当前需要人过一次验证(供巡检/告警用)。

    返回 **老名单 ∪ 账号池** 的并集 —— 老名单里的历史写法(相对路径)也照旧返回,
    池子里那份(绝对路径)一并带上。两边都空才是空。
    """
    out: set[str] = set()
    try:
        import json

        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import SystemConfig

        with get_session_local()() as db:
            row = db.scalar(select(SystemConfig).where(SystemConfig.key == _NEED_KEY))
            if row is not None and row.value:
                out |= set(json.loads(row.value))
    except Exception:  # noqa: BLE001
        logger.debug("读'需验证'名单失败(当空处理)", exc_info=True)
    out |= _pool_need_verify()
    return out


def clear_need_verify(profile: Path) -> None:
    """验证过了(或本轮成功)就把它从"需验证"名单里摘掉 —— **两边都摘**。"""
    try:
        import json

        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import SystemConfig

        with get_session_local()() as db:
            row = db.scalar(select(SystemConfig).where(SystemConfig.key == _NEED_KEY))
            if row is None or not row.value:
                pass
            else:
                # ⚠️ 按**归一化身份**摘,而不是字符串相等:这样**以前用相对路径写下的旧记号
                #    也能被清掉**(否则历史遗留的那些永远红着)。
                key = _pkey(profile)
                cur = {x for x in json.loads(row.value) if _pkey(x) != key}
                row.value = json.dumps(sorted(cur), ensure_ascii=False)
                db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("清'需验证'标记失败", exc_info=True)
    #: 池子那份也清(报一次"健康" ⇒ 顺手抹掉冷却)—— 单独 try,互不连累
    try:
        from app.services import xhs_accounts as pool

        pool.report_profile(profile, pool.STATE_HEALTHY, "验证已通过/本轮成功")
    except Exception:  # noqa: BLE001
        logger.debug("清账号池'需验证'失败", exc_info=True)


def _profile(settings=None) -> Path:
    if settings is None:
        from config.settings import get_settings
        settings = get_settings()
    p = str(getattr(settings, "xhs_browser_profile", "") or "").strip()
    raw = Path(p) if p else Path(DEFAULT_PROFILE)
    # ⚠️ 相对路径按**仓库根**解析 —— 不能依赖进程 CWD(调度器/脚本启动位置不一定一样)
    return raw if raw.is_absolute() else Path(__file__).resolve().parents[2] / raw


#: 搜索页上"**没登录**"的痕迹。⚠️ 必须与"真的没有结果"分开 ——
#: 实测:号 2 的 cookie 注进去但站点不认,搜索页写着「登录后查看搜索结果」;
#: 那时 `section.note-item` 是 **0**、`search-empty-wrapper` 也是 **0**,
#: 于是旧代码走进了"这次没搜到"的分支 ⇒ **静默返回空**(假阴性:日志会说成"小红书没热度")。
NEED_LOGIN = (
    "登录后查看搜索结果", "扫码成功", "请在手机上确认", "重新扫码",
    # ⚠️ **"被踢下线"也算未登录**(2026-10-07 实测):号 1 的搜索页写着
    # 「电脑设备登录超限,请重新登录」—— 小红书**按设备/IP 限制同时登录的账号数**,
    # 同一台电脑登第二个号会把第一个踢掉。这句不在词表里 ⇒ 又是**静默返回空**。
    "请重新登录", "登录超限", "登录已过期", "账号异常",
)


def needs_login(body: str = "") -> bool:
    """页面是不是在要求**登录**(而不是"真的没有结果")。"""
    return any(w in (body or "") for w in NEED_LOGIN)


#: **频率风控冷却**的痕迹(2026-10-11 实测,用户亲眼看到)。
#: 截图原话:**「请勿频繁操作,稍后重试」**。
#:
#: ⚠️⚠️ **它必须单列一类,不能并进 `NEED_LOGIN`**:
#:   · `NEED_LOGIN` 那类的修法是**人去过一次**(过验证/重登);
#:   · 这一类的修法是**等**(服务端按账号/IP 记账,几小时~一天自愈),
#:     **做什么都没用**,而且**越试越久**(每次重试都可能把冷却往后推)。
#:   · 混在一起的后果是**两头都错**:该等的时候让人去重登(白折腾 + 加重风控),
#:     或者被当成"这次没搜到"**静默返回空**(本仓那条母题)。
#:
#: ⚠️ 还有一条纪律:**别再拿它去撞** —— 2026-10-11 这个账号进冷却,一部分就是
#: 我自己在已被风控的账号上一小时内跑了好几轮测试造成的(记忆里「压测的代价是账号」)。
RATE_LIMITED = (
    "请勿频繁操作", "稍后重试", "操作过于频繁", "访问频繁", "请求过于频繁",
)


def rate_limited(body: str = "") -> bool:
    """页面是不是在说**操作太频繁、要冷一会儿**(而不是"没结果"、也不是"要验证")。"""
    return any(w in (body or "") for w in RATE_LIMITED)


def verify_needed(url: str, title: str, body: str = "") -> bool:
    """页面是不是**被安全验证拦住**了(而不是"真的没结果")。

    判据用 **url/标题**,不用"卡片数" —— ⚠️ 我第一版就是看卡片数,于是把
    "被重定向到验证页"误判成"页面没 hydrate",结论整个反了。
    """
    u = (url or "").lower()
    t = (title or "")
    return ("captcha" in u or "website-login" in u or "安全验证" in t
            or "xhsCaptcha" in (body or ""))


#: ★★ 2026-10-15 **抓真实 DOM 定下来的**结构(不是猜的;见 CHANGELOG):
#:
#:     section.note-item
#:       a[href="/explore/<笔记id>"]                                    ← 隐藏锚点,规范地址从这来
#:       a.cover…[href="/search_result/<id>?xsec_token=…"]
#:       div.footer
#:         a.title > span                                               ← **标题**
#:         div.card-bottom-wrapper
#:           a.author[href="/user/profile/<作者id>…"]                   ← 作者主页(暂未入库)
#:             div.name-time-wrapper
#:               div.name                                               ← **作者名**
#:               div.time…(已被截断,按位置是日期)
#:
#: ⚠️ **为什么必须按结构读**(实测,不是洁癖):改用行序(`lines[1]` 当作者)之前,
#: 输出里**至少 3/20 条**的"作者"其实是**日期**(`08-13` / `6天前` / `2025-08-03`)——
#: 有些卡片**没有作者那一行**,行序就整体错位了。本仓推送卡片的「作者列」直接吃这个字段。
AUTHOR_SEL = ".author .name"
TITLE_SEL = ".title"

#: 点赞数节点(2026-10-16 补)。**2026-10-16 21:55 从小红书自己的前端 bundle 坐实**
#: (`data/xhs_sign_dump/index.5890f478.js`,零新请求;那条 `Like` 组件的根节点
#: `class:["like-wrapper", {…}]`,数字那一格是 `T={key:0,class:"count"}` /
#: `x={key:1,class:"count"}` ⇒ `<span class="like-wrapper"><span class="count">4.5万</span>`);
#: ⚠️ 但它**仍然只算首选** —— bundle 与线上 DOM 之间还隔着一层(灰度/版本),所以
#: `_card_rows` 里**必须留着实测过的末行兜底**(那条有两份真 DOM dump 作证)。
LIKE_SEL = ".like-wrapper .count"


def _node_text(card, sel: str, timeout: int = 1500) -> str:
    """按**结构**读卡片里某个节点的文本;读不到回空串(**不抛** —— 单卡片失败不拖垮整批)。

    ⚠️ 与 `card.inner_text()` 的区别是重点:那个读的是**整张卡的文字**,
    要靠**行序**猜哪行是作者 —— 实测会猜错(见 `AUTHOR_SEL` 上面那段)。
    """
    try:
        got = card.locator(sel).first.inner_text(timeout=timeout)
    except Exception:  # noqa: BLE001 - 取不到就回空,由调用方回退
        return ""
    return " ".join(str(got or "").split())


def _card_rows(cards, kw: str, limit: int = 30) -> list[dict]:
    """★ 从 `section.note-item` 卡片里抽出笔记行(2026-10-12 建,2026-10-15 改作者取法)。

    ⚠️ **只收有笔记链接的卡片** —— 页面上"大家都在搜 …"那个推荐栏**也匹配**
    `section.note-item`(实测抓 DOM 时它稳定是那批里的**第 7 张**、`href=None`),
    靠这一条滤掉。

    卡片文本形如「标题 / 作者 / 日期 / 点赞数」⇒ 首行当标题、**次行当作者(兜底)**、
    **末行当点赞数(兜底)**。

    ★ **2026-10-15 改**:作者与标题**先按 DOM 结构读**(`AUTHOR_SEL` / `TITLE_SEL`),
    读不到才回退"次行当作者"。改的原因见 `AUTHOR_SEL` 上面那段实测。

    ★ **2026-10-16 补「点赞数」**(用户报「**小红书没有具体的数据**」):在那之前本函数
    **一个指标都不产出** ⇒ 推送卡的「对标数据」列**恒为 `—`**,而它看起来像"这条没人看"
    (同一天我在 17:55 修的是 **MediaCrawler 那条路**的 `_metric_int`/`万` 解析 ——
    可小红书 2026-10-07 起就走**页面/CDP 路**,根本不过 `_parse_record` ⇒ 那条修法
    **碰不到这里**,列照样是空的)。⇒ 采集层要自己把 `metrics` 建出来。

    ⚠️ **URL 要去掉 `?xsec_token=…`**:那是**会话级**访问令牌(换一次登录就失效,
    带在库里过两天再打开就是坏链)⇒ 只存**规范地址**,`uid` 与 `url` 同源,
    下游要拼链接随时能拼。

    参数 `cards`:任何带 `.count()` / `.nth(i)` 的对象(Playwright locator 或测试替身)。
    """
    #: 点赞数的解析**借用采集层那一个实现**(`mediacrawler_source._metric_int`):
    #: 它认 `4.5万`/`18.8万`/`123` 三种形态,而"同一件事写两处迟早各飘各的"是本仓母题。
    from app.services.mediacrawler_source import _metric_int

    rows: list[dict] = []
    like_by_dom = 0
    for i in range(min(cards.count(), limit)):
        try:
            card = cards.nth(i)
            href = card.locator(
                'a[href*="/explore/"], a[href*="/search_result/"]'
            ).first.get_attribute("href", timeout=1500)
            if not href:
                continue                      #: 推荐栏之类的非笔记卡片
            raw = card.inner_text() or ""
        except Exception:  # noqa: BLE001 - 单卡片读失败不拖垮整词
            continue
        lines = [s.strip() for s in raw.split("\n") if s.strip()]
        if not lines:
            continue
        # ★ **发布时间**(2026-10-29 用户口径「所有板块都要新鲜度检查」)。
        # 卡片文字里本来就有日期(`08-13` / `6天前` / `2025-08-03`),之前**读出来就扔了**
        # ⇒ 小红书的条目在推送侧永远显示「无时间」,新鲜度闸门对它形同虚设。
        # 从**末尾往前**找(日期在后面,标题里偶尔也有像日期的串);认不出就给 0(闸门按"不知道"处理)。
        pub = 0
        try:
            from datetime import datetime as _dt

            from app.services.hot_time import parse_published, parse_published_coarse

            for ln in reversed(lines):
                got = parse_published(ln) or parse_published_coarse(ln)
                if isinstance(got, _dt):
                    pub = int(got.timestamp())
                    break
        except Exception:  # noqa: BLE001 - 时间解析失败不该拖垮采集
            pub = 0
        path = href.split("?", 1)[0].rstrip("/")       #: 先剥查询串,再剥尾斜杠
        uid = path.rsplit("/", 1)[-1]
        url = (path if path.startswith("http")
               else f"https://www.xiaohongshu.com{path}")
        #: ★「对标数据」:`liked_count` 就是小红书的主指标(见 `conversion.PLATFORM_PRIMARY`)。
        #: ⚠️ **取不到就不放进去** —— 渲染成 `—`。填 0 会被读成"没人看"(与采集层同口径)。
        likes = _metric_int(_node_text(card, LIKE_SEL))
        if likes is not None:
            like_by_dom += 1
        else:
            #: 兜底**是实测过的,不是猜的**:卡片文本末行就是点赞数 ——
            #: `4.5万` / `18.8万` / `123` / `7`(2026-10-12、10-15 两次真实 DOM dump 都长这样;
            #: `tests/test_xhs_page_source.py` 里那些 `_FakeCard` 的文本就是那两次的记录)。
            #: ⚠️ 只认**裸数字或带中文量词**的末行 ⇒ 标题、日期(`08-07`/`6天前`)一律解析成
            #: `None`,不会瞎填一个数上去。
            #: ★ **「没有数字」时那一格渲染的是字面量 `赞`**(站点 bundle 里原文是
            #: `likeCountDisplayText || "赞"`)⇒ 解析不出 → 空 `metrics` → 卡上 `—`,
            #: 与采集层口径一致(**不许变成 0**)。
            likes = _metric_int(lines[-1])
        metrics = {"liked_count": likes} if likes is not None else {}
        rows.append({
            "uid": uid,
            #: ★ 结构优先;`lines[1]` 只作**兜底**(老卡片/节点没渲染出来时)
            "name": _node_text(card, AUTHOR_SEL) or (lines[1] if len(lines) > 1 else ""),
            "url": url,
            "snippet": " ".join(lines)[:255], "pan_link": "",
            "keyword": kw,
            #: ★ 卡片上读出来的**发布日期**(0 = 读不出,闸门按"不知道"放行)
            "publish_at": pub,
            "metrics": metrics,
        })
    if rows:
        #: ★ **把"取到几张"喊出来**(本仓母题:静默失败=假成功)。若某天 DOM 改了、
        #: 兜底也不认了,这一行会变成 `0/N` —— 那才是"采集层没给数",与"这条没人看"
        #: 分得开;不报的话两种情况在卡片上长得一模一样(都是 `—`)。
        got = sum(1 for r in rows if r["metrics"])
        logger.info("小红书:点赞数取到 %d/%d 张卡(DOM 节点 %d 张,末行兜底 %d 张)",
                    got, len(rows), like_by_dom, got - like_by_dom)
    return rows


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
    # ★ 要**跳过**两类号:
    #   · 老的"需验证"名单(没有时长概念 ⇒ 得人去过一次才会摘);
    #   · 账号池里的冷却(**带时刻** —— 主要是"频率风控"这类"等着就会好"的失败)。
    # ⚠️ 两边**都按归一化路径**比对:配置/调用方手上可能是相对路径,而池子里存的是
    #    绝对路径,直接用字符串相等会**永远对不上**(本仓踩过这种"单向陷阱")。
    skip_raw = need_verify_profiles()
    skip_norm = {_norm_path(s) for s in skip_raw}
    blocked = _pool_need_verify() | _pool_cooling()
    usable = [p for p in order
              if str(p) not in skip_raw
              and _norm_path(p) not in skip_norm
              and _norm_path(p) not in blocked]

    if not usable:
        # ★ **全被跳过时,不能一律"硬试一轮"**:
        #   若它们是在**「越试越糟」**的冷却里(频率风控 / 账号限制),硬试会把冷却
        #   **推后**(本仓教训:在已被风控的号上反复重试 ⇒ 越试越久)。
        #   这种情形**直接抛**,让上层等 —— 而不是"反正都试一遍"。
        #   (其余状态如"需人过验证":硬试一轮没用、但**不会更糟**,所以仍然放行。)
        harmful = _pool_harmful()
        if order and all(_norm_path(p) in harmful for p in order):
            mins = min(harmful[_norm_path(p)] for p in order)
            raise XhsPageError(
                f"所有小红书账号都在**频率/账号限制冷却**中(最快还有约 {mins} 分钟)。"
                f"这一类**只需等** —— ⚠️ 重试会把冷却推后(越试越久),"
                f"到点池子会自己把它们放出来;别重登、别换档案。", rate_limit=True)
        usable = order
    tried: list[str] = []
    last_err = ""
    with sync_playwright() as p:
        for prof in usable:
            try:
                rows = _search_with_profile(p, prof, kws, per_kw_wait_ms, headed)
                clear_need_verify(prof)          # 成功 ⇒ 两边都摘(含池子里的冷却)
                logger.info("小红书:本轮用账号「%s」,拿到 %d 条", prof.name, len(rows))
                return rows
            except XhsPageError as exc:
                tried.append(prof.name)
                last_err = str(exc)
                if getattr(exc, "rate_limit", False):
                    # ★ **频率风控**:报进池子(3 小时冷却)后**换下一个号** ——
                    #   ⚠️ 关键是**不等人**:这一类只要等就会好,标记成"需人工验证"
                    #   会把这号钉死(那个标记没有时长)⇒ 一盏永远红的假红灯。
                    _report_profile_rate_limited(prof, str(exc))
                    logger.warning("小红书账号「%s」进**频率冷却**(报进池子 3h),换下一个",
                                   prof.name)
                    continue
                if getattr(exc, "needs_human", False):
                    mark_need_verify(prof)
                    logger.warning("小红书账号「%s」需人工处理(过验证/重新登录)"
                                   " —— 标记并换下一个", prof.name)
                    continue
                raise                        # 别的错(打不开页等)不是"换账号"能解决的
    raise XhsPageError(
        f"所有小红书账号都不可用(试过 {tried})。修法:对每个档案各跑一次 "
        f"`python tools/xhs_pass_verify.py <档案目录>`(**登录 或 过安全验证**)。"
        f"⚠️ 若是「频率风控」导致的,那**只需等**(池子已按 3 小时冷却,到点会自己放出来),"
        f"别再重试。最后原因:{last_err[:120]}")


def _search_with_profile(p, prof, kws: list[str], per_kw_wait_ms: int,
                         headed: bool) -> list[dict]:
    """**单个账号**跑一轮搜索。被安全验证拦住时抛 `XhsPageError`(由调用方换账号)。"""
    rows: list[dict] = []
    if True:
        #: ★ 见 `_launch_kwargs`:**UA 对齐 + 关自动化特性**
        kwargs = _launch_kwargs(prof, headed)
        ua = kwargs.get("user_agent") or ""
        if not ua and not headed:
            logger.warning(
                "小红书页面路:档案「%s」的存档里**没有 UA**,而无头采集会自带 "
                "`HeadlessChrome/...` ⇒ 与登录时的浏览器**不是同一个**"
                "(会被判「登录设备 ≠ 调用设备」)。"
                "修法:重登一次 `python tools/xhs_browser_login.py %s`",
                prof.name, prof.name)
        try:
            ctx = p.chromium.launch_persistent_context(channel="msedge",
                                                       executable_path=EDGE, **kwargs)
        except Exception:  # noqa: BLE001 - 没有 Edge 就退回自带 chromium
            logger.info("小红书页面路:未找到 Edge,改用自带 Chromium")
            ctx = p.chromium.launch_persistent_context(**kwargs)
        try:
            #: ⚠️ **必须在取页面之前**注入:脚本只对**之后创建的文档**生效
            ctx.add_init_script(_STEALTH_JS)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            # ⚠️⚠️ **不要"先热身 /explore 再跳搜索"**(2026-10-07 实测,踩了两轮):
            # 那样跳过去 URL 会被前端路由改写成 `/search_result/`(**多一个斜杠**),
            # 标题看着正常(「网盘资源 - 小红书搜索」)却**一张卡都不渲染** ⇒ 静默返回空。
            # 我当初加热身是为了绕 `domcontentloaded` 超时 —— 而**真正的解法是
            # `wait_until="commit"`**(只等导航提交),不用热身。现在直接跳搜索页:
            # 实测 22 张卡。
            for kw in kws:
                # ⚠️ **冷启动第一次导航会超时**(实测:档案越大越明显,xhs_1 稳定撞 60s,
                # 而 xhs_2 一次就过)⇒ **重试一次**,第二次基本都成。
                # 这比"先热身 /explore"好:热身会让前端路由把 URL 改写成
                # `/search_result/`(多一个斜杠)、页面**一张卡都不渲染** —— 我踩过。
                last_goto = ""
                for _try in (1, 2):
                    try:
                        pg.goto(SEARCH_URL.format(kw=kw), wait_until="commit",
                                timeout=60000)
                        last_goto = ""
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_goto = f"{type(exc).__name__}: {str(exc)[:70]}"
                        logger.info("小红书页面路:打开搜索页第 %d 次失败(%s),重试",
                                    _try, last_goto)
                        pg.wait_for_timeout(2000)
                if last_goto:
                    raise XhsPageError(f"打开搜索页失败({kw[:20]}):{last_goto}") from None
                try:
                    pg.wait_for_selector(CARD_SEL, timeout=20000)
                except Exception:  # noqa: BLE001 - 没有卡片:下面按"空态/被拦"分辨
                    pass
                pg.wait_for_timeout(per_kw_wait_ms)
                _title = pg.title() or ""
                if verify_needed(pg.url, _title, pg.content()[:4000]):
                    raise XhsPageError(
                        "小红书要**安全验证**(被重定向到 captcha 页)—— 跑 "
                        "`python tools/xhs_pass_verify.py <档案目录>` 人工过一次即可,"
                        "过完会话恢复(实测搜索页能出 38 张卡)", needs_human=True)
                _body = ""
                try:
                    _body = pg.inner_text("body")[:600]
                except Exception:  # noqa: BLE001
                    pass
                if rate_limited(_body):
                    # ★ **频率冷却单列一类**(2026-10-11,用户亲眼看到那句「请勿频繁操作,稍后重试」)。
                    # ⚠️ `needs_human=False` 是**有意**的:这一类**不需要人做什么,只需要等**。
                    #    而且**不能**复用「需人验证」那个标记 —— 那个标记**没有过期时间**
                    #    (只由"轮次成功"或人跑验证工具清除),拿它去标冷却会让账号
                    #    **永远被跳过**、永远等不到"成功"来清它 ⇒ 一盏永远红的假红灯。
                    #    ⇒ 2026-10-12 起:`rate_limit=True` ⇒ 调用方把它报进**账号池**,
                    #    拿一个**带时刻**的 3 小时冷却(这才是"按时长退避"的正解)。
                    raise XhsPageError(
                        f"小红书**频率风控冷却中**({_body[:60]!r})—— 报「请勿频繁操作/稍后重试」。"
                        f"这类修法是**等**(服务端按账号/IP 记账,几小时~一天自愈),"
                        f"**做什么都没用,而且越试越久**;别重登、别重试、别换档案。",
                        needs_human=False, rate_limit=True)
                if needs_login(_body):
                    # ⚠️⚠️ **这一条是修一个假阴性**:没有它,搜索页的
                    # 「登录后查看搜索结果」会走进下面的"没卡片 ⇒ 这次没搜到"分支,
                    # 于是**静默返回空** —— 日志里长得像"小红书没热度"。
                    #
                    # ★★ **2026-10-12:「未登录」有两种,修法完全相反,绝不能都叫人去重登**
                    #   ① **从来没登过**(存档里没有 `04` 会话)⇒ 要人去登 —— 重登是对的;
                    #   ② **登过、但被踢下线**(存档里明明记着 `04` 真登录,页面却说未登录)
                    #      ⇒ 这是**设备/频率维度**的风控(小红书按设备限制同时在线的账号数,
                    #      同一台机器连登多个号会把前一个踢掉 —— 实测:刚登完 5 分钟内
                    #      `04` 就被降级成 `03`、`webSsk` 归零)。
                    #      这时**重登是最坏的动作**:每登一个新号就再触发一次设备风控,
                    #      把刚登的也踢掉 ⇒ 越修越糟。正确的做法是**停下来等**。
                    #   混成一句"去登录" ⇒ 会把人推成一个**自我加剧的循环**。
                    _raise_not_logged_in(prof.name, prof)
                # 兜底:万一被路由改写成 `/search_result/`,用规范 URL 再来一次。
                # (判据仍是 url/标题,**不是**卡片数 —— 但这里"没卡片"是唯一的信号)
                if "/search_result/" in pg.url:
                    logger.info("小红书页面路:URL 被改写成带斜杠,切回规范 URL 重试")
                    try:
                        pg.goto(SEARCH_URL.format(kw=kw), wait_until="commit",
                                timeout=60000)
                        pg.wait_for_timeout(per_kw_wait_ms)
                    except Exception:  # noqa: BLE001
                        pass
                pg.mouse.wheel(0, 1600)          # 触发懒加载
                pg.wait_for_timeout(2000)
                cards = pg.locator(CARD_SEL)
                n = cards.count()
                if n == 0 and pg.locator(EMPTY_SEL).count():
                    logger.info("小红书页面路:「%s」确实没有结果(空态容器在)", kw[:26])
                    continue                      # **真没有** ≠ 被拦
                rows.extend(_card_rows(cards, kw))
                logger.info("小红书页面路:「%s」拿到 %d 条", kw[:26], n)
        finally:
            ctx.close()                           # 顺序执行的资源不跨调用(见模块头纪律 1)
    return rows
