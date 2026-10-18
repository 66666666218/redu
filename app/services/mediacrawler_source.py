"""MediaCrawler 集成(2026-10-01):跨平台搜索的第二条腿。

**为什么需要它**:知乎能直接搜(见 `cross_accounts.py`),但**微博/贴吧/小红书/抖音**
光带 Cookie 过不去——实测微博 `ok:-100`、贴吧 403、小红书/抖音要请求签名。
`NanmiCoder/MediaCrawler`(★66k) 的解法是**用真浏览器算签名**:Playwright 开 Chromium
→ 扫码登录一次(登录态缓存在 `browser_data/`) → 在页面上下文里发请求 → 由平台自己的 JS
算出 x-s/a_bogus → 服务器认。

**集成方式**(它不是服务,是 CLI 爬虫):
  1. **独立 venv**——它锁了 `fastapi==0.110.2`/`uvicorn==0.29.0`,与本项目版本冲突,不能混装;
  2. 每轮把关键词写进它的 `config/base_config.py`,设 `SAVE_DATA_OPTION = "jsonl"`;
  3. `subprocess` 跑 `main.py --platform <p> --lt qrcode --type search`;
  4. 读它产出的 jsonl → 提取账号 + 盘链筛选 → 进我们的 `cross_platform_accounts`。

**⚠️ 风险与约束(重要)**:
- 用的是**用户自己的账号**登录,而**小红书/抖音对爬虫零容忍** → **强烈建议用小号**;
- 只做**低频发现**(每周几次),不做持续监控——高频轮询是最容易被风控抓的模式;
- 首次要**扫码**(每平台一次),之后登录态自动复用。

平台 id 对照:`xhs` 小红书 / `dy` 抖音 / `ks` 快手 / `bili` B站 / `wb` 微博 / `tieba` 贴吧 / `zhihu` 知乎。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from app.utils import get_logger

logger = get_logger(__name__)

# 工具目录(tools/ 已 gitignore,不进仓库)
ROOT = Path(__file__).resolve().parents[2] / "tools" / "MediaCrawler"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CONFIG = ROOT / "config" / "base_config.py"
DATA_DIR = ROOT / "data"

# 我们的平台名 → MediaCrawler 的 --platform 取值
PLATFORM_IDS = {"xiaohongshu": "xhs", "douyin": "dy", "kuaishou": "ks",
                "bilibili": "bili", "weibo": "wb", "tieba": "tieba", "zhihu": "zhihu"}

# 我们的平台名 → MediaCrawler **产出目录**名。⚠️ 别用 --platform 的缩写:实测传 `--platform dy`
# 出来的是 `data/douyin/jsonl/`,目录用的是平台全名(xhs 是唯一缩写特例)。曾用
# `rglob("*dy*.jsonl")` 匹配、因文件名是 `search_contents_<日期>.jsonl` 而不含平台名,
# 结果一条都读不到(2026-10-02 实跑发现)。
DIR_NAMES = {"xiaohongshu": "xhs", "douyin": "douyin", "kuaishou": "kuaishou",
             "bilibili": "bilibili", "weibo": "weibo", "tieba": "tieba", "zhihu": "zhihu"}


class MediaCrawlerError(RuntimeError):
    """MediaCrawler **硬失败**(未装 / 超时 / 非零退出 / 起不来)。

    ⚠️ **为什么必须与"跑通了但没结果"分开**(2026-10-03):此前 `crawl()` 把**所有**失败都吞成
    `[]`,于是"工具没登录/扫码超时"和"真的一条都没搜到"完全一样 —— 下游 `douyin_leads` 会把它
    记成 `success(线索0)`。而它**只在每天 11:00 无人值守时跑**,失败你不会收到任何信号
    (与闲鱼那次"假成功"、知乎那次静默失败是同一类问题)。
    """


def available() -> tuple[bool, str]:
    """工具是否就绪(venv + 依赖装好)。返回 (是否可用, 原因)。"""
    if not ROOT.exists():
        return False, "未安装(tools/MediaCrawler 不存在;见本模块头注的集成步骤)"
    if not VENV_PY.exists():
        return False, "独立 venv 未建(在 tools/MediaCrawler 下跑 python -m venv .venv)"
    return True, "ok"


def kill_stale_browsers() -> int:
    """杀掉 **MediaCrawler 自己起的、残留没退** 的 Edge;返回杀掉的个数。

    ⚠️ **为什么必须做**(2026-10-05 实测):`subprocess.run(timeout=...)` 超时**只杀直接子进程**,
    而 `main.py` 是**另起 Edge** 的 —— Edge 不在它的进程树里,于是**留在那儿**。
    实测今早 09:00 小红书超时后留下 **10 个** Edge,一直占着那个 profile;
    下一个跑同平台的进程就会**撞锁**,于是失败会**自我延续**。

    ⚠️ **只杀命令行里带 MediaCrawler 路径的**:机器上还有用户自己的 Edge
    (实测 36 个里面只有 10 个是我们的)—— **杀错就是把人正在用的浏览器关了**。
    ⚠️ 非 Windows 直接返回 0(本模块只在 Windows 上跑)。
    """
    import subprocess
    import sys

    if sys.platform != "win32":
        return 0
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -like '*MediaCrawler*browser_data*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
        "Write-Output $_.ProcessId }"
    )
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=60)
        n = len([x for x in p.stdout.decode("utf-8", "ignore").split() if x.strip().isdigit()])
    except Exception:  # noqa: BLE001 - 清理失败绝不能挡住采集
        logger.debug("清理残留浏览器失败", exc_info=True)
        return 0
    if n:
        logger.warning("清掉 %d 个残留的 MediaCrawler 浏览器进程(它们会占住 profile、"
                       "让下一轮同平台撞锁)", n)
    return n


def _login_cookie(platform: str, user_id: int = 1) -> str:
    """该平台的**登录 cookie**(没有就返回空串 = 退回扫码)。

    ★ **2026-10-19 起抖音也走 cookie 登录**:
    协议路与浏览器档案是**同一个账号、同一个会话**(实测 `sid_guard` 都到 2026-12-02),
    档案里的 `sessionid` 虽然有效,但档案一旦被锁/损坏,浏览器兜底就会在**未登录**状态下
    空跑一圈(看起来和"风控空结果"一模一样)。直接把协议路的 cookie 注进浏览器,
    这条路就不再依赖档案的完好性 —— 这是"档案登录"之外的**第二条退路**。

    小红书走 MediaCrawler 的 `login_by_cookies`,它**只认 `web_session` 一个值**
    (见 `media_platform/xhs/login.py::login_by_cookies`),所以 `.env` 里存的就是那一行。
    取值方式见 `tools/xhs_export_cookie.py`。
    """
    if platform == "douyin":
        try:
            from app.db import get_session_local
            from app.services import cookie_store

            db = get_session_local()()
            try:
                return (cookie_store.get_cookie(db, user_id, "douyin") or "").strip()
            finally:
                db.close()
        except Exception:  # noqa: BLE001 - 读不到就当没配,退回档案登录(不影响采集)
            logger.debug("读取抖音 cookie 失败(退回档案登录)", exc_info=True)
            return ""
    if platform != "xiaohongshu":
        return ""
    try:
        from config.settings import get_settings

        return (getattr(get_settings(), "xhs_web_session", "") or "").strip()
    except Exception:  # noqa: BLE001 - 读不到就当没配,退回扫码(不影响采集)
        return ""


def _write_config(keywords: list[str], cookie: str = "") -> None:
    """把关键词、输出格式、**登录方式**写进它的 base_config.py(原地改几行,其余不动)。

    ⚠️ **登录方式也在这里定**(2026-10-06):有我们自己的 cookie 就写
    `LOGIN_TYPE="cookie"` + `COOKIES="<…>"` —— **不再弹二维码等人扫**。
    为什么这件事值得改:扫码是**无人值守流程里最脆的一环**(等 120s 没人扫就整轮失败,
    而失败长得像"接口挂了"),而 cookie 存进 `.env` 之后它和其他平台凭据一样可管理、
    可验活、失效能报警。

    ⚠️ **值一律用 `repr()`**(2026-10-19):抖音的完整 jar 有 53 个 cookie、5000+ 字符,
    里面可能出现引号/反斜杠 —— 手工拼 `COOKIES = "…"` 会把配置文件写坏,
    而下一轮只会报一个莫名其妙的语法错。`repr()` 同时保留 emoji,不会再触发 GBK 问题。
    """
    text = CONFIG.read_text(encoding="utf-8")
    kw = ",".join(k.strip() for k in keywords if k.strip())
    lt = "cookie" if cookie else "qrcode"
    out = []
    for line in text.splitlines():
        if line.startswith("KEYWORDS = "):
            line = f"KEYWORDS = {kw!r}"
        elif line.startswith("SAVE_DATA_OPTION = "):
            line = 'SAVE_DATA_OPTION = "jsonl"'
        elif line.startswith("LOGIN_TYPE = "):
            line = f"LOGIN_TYPE = {lt!r}"
        elif line.startswith("COOKIES = "):
            line = f"COOKIES = {cookie!r}"
        out.append(line)
    CONFIG.write_text("\n".join(out) + "\n", encoding="utf-8")


def profile_login_state(platform: str) -> bool | None:
    """MediaCrawler 档案里**还有没有登录核心 cookie**(只读 SQLite,不启动浏览器)。

    返回 `True` / `False` / `None`(查不到,别据此判死)。
    只查 **cookie 名**,不解密值 —— 我们只需要回答"档案是不是登着",
    而"值是否还有效"由实际请求决定。
    """
    pid = PLATFORM_IDS.get(platform)
    if not pid:
        return None
    prof = ROOT / "browser_data" / f"cdp_{pid}_user_data_dir"
    if not prof.exists():
        return False
    dbs = list(prof.glob("**/Network/Cookies"))
    if not dbs:
        return None
    import sqlite3

    try:
        c = sqlite3.connect(f"file:{dbs[0].as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            n = c.execute(
                "SELECT COUNT(*) FROM cookies WHERE name IN ('sessionid','sessionid_ss')"
            ).fetchone()[0]
        finally:
            c.close()
        return bool(n)
    except Exception:  # noqa: BLE001 - 档案被锁/结构变了 ⇒ "查不到",不是"没登录"
        return None


#: 这些尾巴代表**瞬时**故障:重试一次通常就好,与"登录失效/风控空结果"不是一回事。
_TRANSIENT_HINTS = ("timeout", "timed out", "err_", "net::", "targetclosed",
                    "connection", "econn", "socket")


def _is_transient(tail: str) -> bool:
    low = (tail or "").lower()
    return any(h in low for h in _TRANSIENT_HINTS)


def crawl(platform: str, keywords: list[str], timeout: int = 600,
          cookie: str = "", user_id: int = 1) -> list[dict]:
    """跑一轮关键词搜索 → `[{uid, name, url, snippet, pan_link}]`。

    `platform` 用我们的名字(xiaohongshu/douyin/…),内部转成 MediaCrawler 的 id。
    **硬失败抛 `MediaCrawlerError`**(不再返回空列表冒充"没搜到");跑通但没结果才返回 `[]`。

    ★ **2026-10-19 的三处加固**(起因:抖音 16:10 之后浏览器兜底也空了):
      ① `--lt` 跟着 cookie 走 —— MediaCrawler 的 CLI 会覆盖 `base_config` 里的
         `LOGIN_TYPE`,以前恒传 `--lt qrcode`,于是**写了 cookie 登录也没用**;
      ② **瞬时故障重试一次** —— 00:10 那次的真因是 `Page.goto` 30s 超时(不是登录/风控);
      ③ **档案未登录要单独报** —— 别把它和"风控空结果"混成一句"全部无结果"。
    """
    ok, why = available()
    if not ok:
        raise MediaCrawlerError(why)
    pid = PLATFORM_IDS.get(platform)
    if not pid or not keywords:
        return []
    login_cookie = (cookie or _login_cookie(platform, user_id=user_id)).strip()
    try:
        _write_config(keywords, login_cookie)
    except OSError as exc:
        raise MediaCrawlerError(f"写配置失败:{exc}") from exc
    if platform == "douyin":
        state = profile_login_state(platform)
        if state is False and not login_cookie:
            raise MediaCrawlerError(
                "MediaCrawler 抖音档案未登录(缺 sessionid),且没有可注入的 cookie —— "
                "跑 `tools/MediaCrawler/.venv/Scripts/python.exe scripts/mc_login.py dy`,"
                "或检查 cookie_store 里的 douyin 凭据")
        if state is False:
            logger.info("MediaCrawler 抖音档案未登录,本轮改用 cookie 注入")
        elif state is True:
            logger.info("MediaCrawler 抖音档案登录态齐全")
    #: ⚠️ **`--lt` 必须与 cookie 一致**:`cmd_arg/arg.py` 最后一行是
    #: `config.LOGIN_TYPE = lt.value` ⇒ 只要还在传 `--lt qrcode`,base_config 里写的
    #: `LOGIN_TYPE="cookie"` 就会被**当场覆盖**,扫码登录照旧。这是本仓母题
    #: 「配置写了没人用」的又一例。
    lt = "cookie" if login_cookie else "qrcode"
    cmd = [str(VENV_PY), "main.py", "--platform", pid, "--lt", lt, "--type", "search"]
    started = time.time()          # ⚠️ 必须在**起进程之前**取:用来判"哪个文件是本轮写的"
    last_error = ""
    try:
        for attempt in (1, 2):
            # ⚠️ **每次起进程前都清残留**(2026-10-05):上一轮的 Edge 若没退,会占着同一个
            # profile,这次起来就撞锁 —— 失败于是**自我延续**。
            kill_stale_browsers()
            try:
                proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                # 最常见的无人值守失败:等扫码等到超时(600s)。必须冒泡,否则这轮会被当成"没线索"。
                raise MediaCrawlerError(f"{platform} 超时({timeout}s)——多半卡在扫码登录") from exc
            except OSError as exc:
                raise MediaCrawlerError(f"{platform} 启动失败:{exc}") from exc
            if proc.returncode == 0:
                out = _read_results(platform, since=started)
                if not out:
                    _raise_if_all_keywords_empty(platform, proc, keywords)
                return out
            tail = ((proc.stderr or b"") + (proc.stdout or b""))[-400:].decode("utf-8", "ignore")
            last_error = f"{platform} 退出码 {proc.returncode}:{_explain(tail)}"
            #: ★ **只重试"瞬时故障"**(2026-10-19 实测):`Page.goto: Timeout` 属于这类;
            #: 而"全空/登录失效"**不重试** —— 重试只会对同一个账号多打一次风控。
            if attempt == 1 and _is_transient(tail):
                logger.warning("MediaCrawler %s 瞬时失败,清理后重试一次:%s",
                               platform, tail[-160:])
                time.sleep(3)
                started = time.time()     # ⚠️ 重试的产出必须晚于新的 started,别读上次的文件
                continue
            raise MediaCrawlerError(last_error)
        raise MediaCrawlerError(last_error or f"{platform} 失败")
    finally:
        # ⚠️ **不论成败都清**:`subprocess.run(timeout=)` 只杀直接子进程,
        # 而 Edge 是 `main.py` **另起**的、不在它的进程树里 ⇒ 超时那轮**必然**留下它们。
        # 不清就是"这次失败 → 占住 profile → 下次也失败"(2026-10-05 实测 10 个残留)。
        kill_stale_browsers()


def _explain(tail: str) -> str:
    """把常见的退出原因翻成**能照做**的话(否则一律只是漫长的 Playwright 堆栈)。

    ⚠️ 最要命的一条:`TargetClosedError` —— **采集过程中浏览器窗口被手工关掉了**。
    本项目的 CDP 模式 `CDP_CONNECT_EXISTING=False`,程序**会自己启动 Edge 并接管**、
    跑完自己关;所以采集期间弹出来的那个窗口是**程序正在用的**,关它 = 直接失败
    (2026-10-04 实测:小红书/快手就是这样挂的,却很容易被误读成"没登录")。
    """
    if "TargetClosedError" in tail or "Target page, context or browser has been closed" in tail:
        return ("浏览器在采集过程中被关闭 —— 采集会自己启动窗口、跑完自己关,"
                "**期间请不要手工关掉那个窗口**(不是登录问题)。原始栈:" + tail[-160:])
    if "CDP port" in tail and "not accessible" in tail:
        return "连不上浏览器的 CDP 端口 —— 检查是否有另一个 Edge 占着同一档案目录。原始栈:" + tail[-160:]
    return tail


def _raise_if_all_keywords_empty(platform: str, proc, keywords: list[str]) -> None:
    """**一个词都没搜到** → 必须报出来,不能安静地交出 0 条。

    2026-10-03 实测:抖音对 `网盘资源` 这种必然有结果的泛词也返回 `aweme_list:[]`,
    而 10-02 同一工具、同一档案能出 40 条 —— 这就是**扫码登录过期**的样子。
    MediaCrawler 的日志里每个词打一行 `keyword:<词>, aweme_list:[…]`,全空即是该信号。

    ⚠️⚠️ **但"全空"不止这一个原因**(2026-10-16 订正)。同一天的实测:
      · 08:10 全空 ⇒ **09:30 同一档案、同一出口 IP 又搜到了 8 条线索**;
      · 协议路把同一件事**明说**成 `verify_check`(服务端要求过验证)。
    ⇒ 原来这里直接断言「**多半是登录态失效,需要重扫一次码**」是**给错动作**:
    若真因是风控,重扫一遍**什么都不会变**(本仓为这种"喊错方向"栽过多次:
    报告喊重导、人照做、无效,而真问题被这句话盖住)。
    两个原因**动作相反** ⇒ 必须**分开说,并给出怎么分**。
    """
    text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "ignore")
    total = len(re.findall(r"aweme_list:\[", text))
    empty = len(re.findall(r"aweme_list:\[\]", text))
    if total and empty == total:
        raise MediaCrawlerError(
            f"{platform} 的 {total} 个关键词全部无结果(词:{','.join(keywords[:3])})—— "
            f"⚠️ **两种原因、动作相反,别先猜一个**:"
            f"① **登录态过期** ⇒ 档案要重扫一次码;"
            f"② **被要求过验证**(风控) ⇒ 重扫**没用**,动作是**等 + 别再打**"
            f"(同一天 08:10 全空、09:30 同一档案又搜到 8 条 —— 这种「时好时坏」就是②)。"
            f"怎么分:**先看体检报告「抖音(线索搜索)」那一行**;"
            f"协议路会把②明说成 `search_nil_info.search_nil_type=verify_check`。")


def _read_results(platform: str, since: float | None = None) -> list[dict]:
    """从 `data/<平台全名>/jsonl/` 读**本轮**产出的内容型 jsonl,归一化成我们的结构。

    ⚠️ **`since` 是防"读旧数据"的闸门**(2026-10-03 实测踩到):MediaCrawler 在**搜到 0 条时
    根本不写文件**,而那些文件是按日期命名的(`search_contents_2026-10-02.jsonl`)——
    于是"今天什么都没搜到"会**回退读到昨天的文件**,把 40 条旧线索当成新线索返回,
    下游就会**每天把同一批旧线索再推一遍**。判据用文件 mtime:只有本轮起进程之后写过的才算。

    只读 `search_contents_*.jsonl`:同目录的 `search_comments_*.jsonl` 是**评论者**记录
    (不是发帖人),拿它当对标号会把评论区路人一起收进来。
    """
    d = DATA_DIR / DIR_NAMES.get(platform, platform) / "jsonl"
    if not d.is_dir():
        logger.info("MediaCrawler 没有产出目录(%s)", d)
        return []
    files = sorted(d.glob("search_contents_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if since is not None:
        files = [f for f in files if f.stat().st_mtime >= since]
    if not files:
        logger.info("MediaCrawler 本轮没有产出新文件(%s)——按「本轮无结果」处理,不读旧文件", platform)
        return []
    out: list[dict] = []
    for line in files[0].read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        item = _parse_record(rec, platform)
        if item:
            out.append(item)
    return out


# 各平台**曝光/互动指标**的归一键(与 `conversion.ALIASES` 对齐)。
# ⚠️ **播放量并非每个平台都有**:小红书/贴吧的搜索接口根本没有该字段
# (2026-10-04 实测;小红书/抖音的播放量是"创作者私有数据",别人的内容拿不到)。
# 所以这里**取不到就不放进 `metrics`** —— 让 `conversion` 去走它的降级阶梯,
# 而不是在这里填 0(填 0 会被读成"没人看")。
_METRIC_KEYS: tuple[str, ...] = ("play_count", "liked_count", "collected_count",
                                 "comment_count", "share_count")

#: 各平台**字段名不同**的指标 → 归一成 `_METRIC_KEYS` 里的名字(2026-10-16 实测补齐)。
#: ⚠️ 不映射的话**那个平台永远没有互动数** —— 卡上「对标数据」那一列恒 `—`,
#:   而看起来像"这条没人看"(用户报的原话:「微博的有些东西没给啊不显示数据」)。
#: 实测来源:
#:   · 快手 `viewd_count` = 1310138(播放量) —— 不是 `play_count`,所以原来读不到;
#:   · 贴吧 `total_replay_num` = 39(回复数,就是它的"评论")。
_LABEL_ALIASES: dict[str, str] = {
    "viewd_count": "play_count", "play_num": "play_count", "view_count": "play_count",
    "total_replay_num": "comment_count", "replay_num": "comment_count",
}

#: **认不出 URL 字段时,按平台由 id 拼出作品地址**(2026-10-16,见 `_derive_url`)。
#: 只放"地址与 id 一一对应、且我们真见过 id 字段"的平台;拿不准的宁可不放
#: (`—(源未给链接)`至少是**诚实的空**,拼错了是**错的链**)。
_DERIVE_URL: dict[str, tuple[tuple[str, str], ...]] = {
    "douyin": (("aweme_id", "https://www.douyin.com/video/{v}"),),
    "kuaishou": (("video_id", "https://www.kuaishou.com/short-video/{v}"),),
    "xiaohongshu": (("note_id", "https://www.xiaohongshu.com/explore/{v}"),),
    "bilibili": (("bvid", "https://www.bilibili.com/video/{v}"),),
    "tieba": (("tid", "https://tieba.baidu.com/p/{v}"),),
}


def _metric_int(v: object) -> int | None:
    """指标值 → 非负整数;**缺失/解析不出/负数一律返回 `None`**(= 没有这个数据,不是 0)。

    ⚠️ **空串也算缺失** —— `""` 与 `"0"` 是两回事:前者是"平台没给",后者是"确实是 0"。
    混在一起会让"字段缺失"被读成"没人看"。
    ⚠️ **要认"1.3万"这种中文量词**(2026-10-16):小红书实测 `liked_count = "1.3万"`,
    而 `int(float("1.3万"))` 抛 ValueError ⇒ 那个平台的互动数**一直被静默丢掉**。
    """
    if v is None:
        return None
    text = str(v).strip()
    if not text or text in ("None", "null"):     # "" / "None" ⇒ 缺失
        return None
    import re

    m = re.match(r"^([\d.]+)\s*([万亿千万wWkK]?)$", text)
    if m:
        try:
            num = float(m.group(1))
        except ValueError:
            return None
        mult = {"万": 1e4, "w": 1e4, "W": 1e4, "亿": 1e8,
                "千": 1e3, "k": 1e3, "K": 1e3}.get(m.group(2), 1)
        n = int(num * mult)
        return n if n >= 0 else None
    try:
        n = int(float(text))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _derive_url(rec: dict, platform: str) -> str:
    """**从记录里的 id 拼出作品地址** —— URL 字段认不出来时的兜底(2026-10-16 加)。

    为什么要它:本仓一天之内踩了**两次同一族**的坑 —— 「那个平台的『对标作品』列**恒为空**」,
    而根因都是**字段名对不上**(快手 `video_url`、微博 `mblogid`/`url`)。逐字段打补丁
    是**打地鼠**:上游改一次名,那一列就再空一次,而且**空得毫无提示**(卡片上只是 `—`,
    看起来像"这条没有作品")。⇒ 兜底改成"**从 id 拼**":这些平台的规范地址与 id
    **一一对应**,而 id 字段比 URL 字段稳得多。

    ⚠️ 只按**该平台自己的 id** 拼(`_DERIVE_URL` 按平台分),不做跨平台猜测 ——
    「给错链比不给链更坏」。
    """
    for key, tpl in _DERIVE_URL.get(platform, ()):
        v = str(rec.get(key) or "").strip()
        if v:
            return tpl.format(v=v)
    return ""


def _parse_record(rec: dict, platform: str) -> dict | None:
    """把各平台**字段名不同**的记录归一化成统一结构。

    实测字段:xhs 用 `user.nickname`/`user.user_id` + `title`/`desc`;
    **抖音用 `nickname` + `creator_hash`**(没有 user_id/uid);其余平台多为
    `nickname`/`user_id` + `content`/`title`。这里做多路兼容。

    ⚠️ 该工具是教学版:`nickname` 已被中间脱敏(`籽***）`)、`creator_hash` 是 sha256 截断、
    账号主页链接不采集——所以收上来的号**名字不可用、也点不进去**,这也是它默认停用的原因。
    """
    from app.services.wechat_monitor import _extract_pan_urls

    user = rec.get("user") if isinstance(rec.get("user"), dict) else {}
    # ⚠️ 各平台字段名不同(实测):抖音/小红书/快手用 `nickname`,**贴吧用 `user_nickname`** ——
    # 少认一个,那个平台就**永远解析出 0 条**(2026-10-02 贴吧实跑踩到:爬到了 10 条、解析返回 0)
    name = str(rec.get("nickname") or rec.get("user_nickname")
               or user.get("nickname") or rec.get("author") or "").strip()
    uid = str(rec.get("user_id") or user.get("user_id") or rec.get("uid")
              or user.get("id") or rec.get("creator_hash") or "").strip()
    if not name or not uid:
        return None
    # 抖音的 `title` 与 `desc` 常常是**同一段文字**(实测),不去重的话卡片里会显示两遍。
    parts = [str(rec.get(k) or "").strip() for k in ("title", "desc", "content", "text")]
    text = " ".join(dict.fromkeys(p for p in parts if p))   # dict.fromkeys = 保序去重
    urls = _extract_pan_urls("", text)
    # ⚠️ **URL 的键各平台不同**(2026-10-16 实测补齐):抖音 `aweme_url`、小红书/贴吧
    #    `note_url`、**快手 `video_url`** —— 少认一个,那个平台的「对标作品」列就**恒为空**
    #    (症状就是用户看到的 `—(源未给链接)`)。
    url = str(rec.get("note_url") or rec.get("url") or rec.get("aweme_url")
              or rec.get("video_url") or "").strip()
    if not url:
        #: ★ **兜底:由 id 拼**(见 `_derive_url`)—— 上游再改一次字段名也不会让那一列变空。
        url = _derive_url(rec, platform)
    # **全部曝光/互动指标**(2026-10-04):原版只取 `share_count`,其余(尤其**播放量**)全丢。
    # 归一成 `metrics` 字典 ⇒ **直接喂给 `conversion.estimate(platform, metrics)`**,
    # 省得每个调用方各写一遍字段名映射(那是"两处真相"的开始)。
    # ★ 2026-10-16:过一层 `_LABEL_ALIASES`(快手 `viewd_count` / 贴吧 `total_replay_num`),
    #   否则**那两个平台的互动数永远读不到**。
    metrics: dict[str, int] = {}
    for key, val in rec.items():
        want = _LABEL_ALIASES.get(key, key)
        if want not in _METRIC_KEYS or want in metrics:
            continue
        n = _metric_int(val)
        if n is not None:
            metrics[want] = n
    return {"uid": uid, "name": name, "url": url[:500], "snippet": text[:255],
            "pan_link": (urls[0] if urls else "")[:500],
            # 兼容既有调用方(douyin_leads 读它):缺了就 0 —— 它的列本来就是这么定义的
            "share_count": metrics.get("share_count", 0),
            "metrics": metrics,                  # 交给 conversion 算曝光
            # 该条来自哪个搜索词(抖音 jsonl 的 source_keyword)——
            # 抖音线索要按词回显"这条是搜什么词搜出来的"。
            "keyword": str(rec.get("source_keyword") or "")[:80],
            # **帖子发布时间**(Unix 秒,2026-10-06):`create_time` 一直在 jsonl 里
            # (实测字段齐全),**只是这里从来没人往上传** ⇒ 下游只能拿"我们发现的时刻"
            # 当新鲜度,而那是**假的新鲜度**(老帖被反复推时照样"刚发现")。
            "publish_at": int(rec.get("create_time") or 0)}
