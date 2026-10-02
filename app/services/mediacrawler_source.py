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
import subprocess
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


def available() -> tuple[bool, str]:
    """工具是否就绪(venv + 依赖装好)。返回 (是否可用, 原因)。"""
    if not ROOT.exists():
        return False, f"未安装(tools/MediaCrawler 不存在;见本模块头注的集成步骤)"
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
    失败(未装/超时/登录态失效)一律返回空列表——发现任务不该被单平台拖垮。
    """
    ok, why = available()
    if not ok:
        logger.info("MediaCrawler 不可用:%s", why)
        return []
    pid = PLATFORM_IDS.get(platform)
    if not pid or not keywords:
        return []
    try:
        _write_config(keywords)
    except OSError as exc:
        logger.warning("写 MediaCrawler 配置失败:%s", exc)
        return []
    cmd = [str(VENV_PY), "main.py", "--platform", pid, "--lt", "qrcode", "--type", "search"]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("MediaCrawler 超时(%s):%s", platform, keywords)
        return []
    except OSError as exc:
        logger.warning("MediaCrawler 启动失败:%s", exc)
        return []
    if proc.returncode != 0:
        tail = (proc.stderr or b"")[-300:].decode("utf-8", "ignore")
        logger.warning("MediaCrawler 退出码 %s(%s):%s", proc.returncode, platform, tail)
        return []
    return _read_results(platform)


def _read_results(platform: str) -> list[dict]:
    """从 `data/<平台全名>/jsonl/` 读**最新的内容型** jsonl,归一化成我们的结构。

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
    if not files:
        logger.info("MediaCrawler 没有产出 jsonl(%s)", platform)
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
    name = str(rec.get("nickname") or user.get("nickname") or rec.get("author") or "").strip()
    uid = str(rec.get("user_id") or user.get("user_id") or rec.get("uid")
              or user.get("id") or rec.get("creator_hash") or "").strip()
    if not name or not uid:
        return None
    text = " ".join(str(rec.get(k) or "") for k in ("title", "desc", "content", "text")).strip()
    urls = _extract_pan_urls("", text)
    url = str(rec.get("note_url") or rec.get("url") or rec.get("aweme_url") or "").strip()
    return {"uid": uid, "name": name, "url": url[:500], "snippet": text[:255],
            "pan_link": (urls[0] if urls else "")[:500]}
