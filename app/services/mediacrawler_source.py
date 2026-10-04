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


def _write_config(keywords: list[str]) -> None:
    """把关键词与输出格式写进它的 base_config.py(原地改两行,保持其余不动)。"""
    text = CONFIG.read_text(encoding="utf-8")
    kw = ",".join(k.strip() for k in keywords if k.strip())
    out = []
    for line in text.splitlines():
        if line.startswith("KEYWORDS = "):
            line = f'KEYWORDS = "{kw}"'
        elif line.startswith("SAVE_DATA_OPTION = "):
            line = 'SAVE_DATA_OPTION = "jsonl"'
        out.append(line)
    CONFIG.write_text("\n".join(out) + "\n", encoding="utf-8")


def crawl(platform: str, keywords: list[str], timeout: int = 600) -> list[dict]:
    """跑一轮关键词搜索 → `[{uid, name, url, snippet, pan_link}]`。

    `platform` 用我们的名字(xiaohongshu/douyin/…),内部转成 MediaCrawler 的 id。
    **硬失败抛 `MediaCrawlerError`**(不再返回空列表冒充"没搜到");跑通但没结果才返回 `[]`。
    """
    ok, why = available()
    if not ok:
        raise MediaCrawlerError(why)
    pid = PLATFORM_IDS.get(platform)
    if not pid or not keywords:
        return []
    try:
        _write_config(keywords)
    except OSError as exc:
        raise MediaCrawlerError(f"写配置失败:{exc}") from exc
    cmd = [str(VENV_PY), "main.py", "--platform", pid, "--lt", "qrcode", "--type", "search"]
    started = time.time()          # ⚠️ 必须在**起进程之前**取:用来判"哪个文件是本轮写的"
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # 最常见的无人值守失败:等扫码等到超时(600s)。必须冒泡,否则这轮会被当成"没线索"。
        raise MediaCrawlerError(f"{platform} 超时({timeout}s)——多半卡在扫码登录") from exc
    except OSError as exc:
        raise MediaCrawlerError(f"{platform} 启动失败:{exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or b"")[-300:].decode("utf-8", "ignore")
        raise MediaCrawlerError(f"{platform} 退出码 {proc.returncode}:{_explain(tail)}")
    out = _read_results(platform, since=started)
    if not out:
        _raise_if_all_keywords_empty(platform, proc, keywords)
    return out


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
    """**一个词都没搜到** → 多半是登录态失效,必须报出来而不是安静地交出 0 条。

    2026-10-03 实测:抖音对 `网盘资源` 这种必然有结果的泛词也返回 `aweme_list:[]`,
    而 10-02 同一工具、同一档案能出 40 条 —— 这就是**扫码登录过期**的样子。
    MediaCrawler 的日志里每个词打一行 `keyword:<词>, aweme_list:[…]`,全空即是该信号。
    """
    text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "ignore")
    total = len(re.findall(r"aweme_list:\[", text))
    empty = len(re.findall(r"aweme_list:\[\]", text))
    if total and empty == total:
        raise MediaCrawlerError(
            f"{platform} 的 {total} 个关键词全部无结果(词:{','.join(keywords[:3])})——"
            f"多半是**登录态失效**,需要重扫一次码(实测连泛词都返回空)")


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


def _metric_int(v: object) -> int | None:
    """指标值 → 非负整数;**缺失/解析不出/负数一律返回 `None`**(= 没有这个数据,不是 0)。

    ⚠️ **空串也算缺失** —— `""` 与 `"0"` 是两回事:前者是"平台没给",后者是"确实是 0"。
    混在一起会让"字段缺失"被读成"没人看"。
    """
    if v is None:
        return None
    text = str(v).strip()
    if not text:                       # "" / "  " / "None" 之外的空白 ⇒ 缺失
        return None
    try:
        n = int(float(text))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


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
    url = str(rec.get("note_url") or rec.get("url") or rec.get("aweme_url") or "").strip()
    # **全部曝光/互动指标**(2026-10-04):原版只取 `share_count`,其余(尤其**播放量**)全丢。
    # 归一成 `metrics` 字典 ⇒ **直接喂给 `conversion.estimate(platform, metrics)`**,
    # 省得每个调用方各写一遍字段名映射(那是"两处真相"的开始)。
    metrics = {k: n for k in _METRIC_KEYS if (n := _metric_int(rec.get(k))) is not None}
    return {"uid": uid, "name": name, "url": url[:500], "snippet": text[:255],
            "pan_link": (urls[0] if urls else "")[:500],
            # 兼容既有调用方(douyin_leads 读它):缺了就 0 —— 它的列本来就是这么定义的
            "share_count": metrics.get("share_count", 0),
            "metrics": metrics,                  # 交给 conversion 算曝光
            # 该条来自哪个搜索词(抖音 jsonl 的 source_keyword)——
            # 抖音线索要按词回显"这条是搜什么词搜出来的"。
            "keyword": str(rec.get("source_keyword") or "")[:80]}
