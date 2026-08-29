"""指数数据获取(见 doc/dev.md §5.4)。

设计:定义抽象源 `IndexSource`;子类 `DouyinIndexSource`(Playwright 拦截 XHR)、
`BaiduIndexSource`(requests + 会话 Cookie)、`MockIndexSource`(合成,用于本地/测试);
由 `IndexFetcher` 按"降级链"顺序尝试,单源失败自动降级到下一源。

降级策略(与 dev.md 一致):
- 优先抖音(巨量算数);失败回退百度指数。
- `mock_index=true` 时整链替换为 `MockIndexSource`,本地/测试无网络也可跑通管道。

解析:所有真实源共用一套容错提取器 `_find_trend_points`,从嵌套 JSON 中递归
找出"时间序列点列表"(含一个数值键 + 一个时间键),保证解析逻辑可独立单测。
"""
from __future__ import annotations

import hashlib
import json

from abc import ABC, abstractmethod
from datetime import datetime, timedelta

import requests

from config.settings import Settings
from app.models import IndexPoint, IndexSource as IndexSourceName, TrendSeries
from app.storage import ArchiveRepository
from app.utils import get_logger, get_proxies, playwright_proxy, retry

logger = get_logger(__name__)

# 巨量算数(抖音指数)页面与占位搜索框选择器。
_DOUYIN_URL = "https://trendinsight.oceanengine.com/arithmetic-index/index"
_DOUYIN_SEARCH_SELECTOR = 'input[placeholder*="关键词"], input'
# 百度指数接口(MVP 占位,需携带 BAIDU_COOKIE)。
_BAIDU_URL = "https://index.baidu.com/api/SearchApi/index"

_TIME_KEYS = ("day", "date", "ds", "dt", "ts", "time", "datetime")
_VALUE_KEYS = ("value", "val", "num", "index", "cnt", "score")


class IndexFetchError(Exception):
    """指数获取失败(源不可用 / 解析失败 / 依赖缺失)。"""


# --------------------------------------------------------------------------- #
# 容错提取:从任意嵌套 JSON 中找出"时间序列点列表"。
# --------------------------------------------------------------------------- #
def _coerce_dt(value: object) -> datetime | None:
    """把常见时间字段(日期字符串 / epoch)转成 datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # 毫秒级 epoch
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _row_to_point(row: object) -> IndexPoint | None:
    """若 row 是一行"时间+数值"的点,返回 IndexPoint,否则 None。"""
    if not isinstance(row, dict):
        return None
    value: float | None = None
    ts: datetime | None = None
    for key, item in row.items():
        lk = key.lower()
        if value is None and lk in _VALUE_KEYS:
            try:
                value = float(item)
            except (TypeError, ValueError):
                pass
        elif ts is None and lk in _TIME_KEYS:
            ts = _coerce_dt(item)
    if value is None:
        return None
    return IndexPoint(ts=ts or datetime.now(), value=value)


def _find_trend_points(obj: object) -> list[IndexPoint]:
    """递归查找最可能的指数序列,返回按时间升序的点列表(可能为空)。"""
    best: list[IndexPoint] = []

    if isinstance(obj, dict):
        row = _row_to_point(obj)
        if row is not None and len(obj) >= 2:
            best = [row]
        for val in obj.values():
            sub = _find_trend_points(val)
            if len(sub) > len(best):
                best = sub
    elif isinstance(obj, list):
        rows = [r for r in (_row_to_point(x) for x in obj) if r is not None]
        if len(rows) >= 2:
            rows.sort(key=lambda p: p.ts)
            best = rows
        else:
            for item in obj:
                sub = _find_trend_points(item)
                if len(sub) > len(best):
                    best = sub

    best.sort(key=lambda p: p.ts)
    return best


# --------------------------------------------------------------------------- #
# 数据源实现。
# --------------------------------------------------------------------------- #
class IndexSource(ABC):
    """指数数据源接口。"""

    @abstractmethod
    def fetch(self, keyword: str) -> TrendSeries:
        """获取某关键词的指数时间序列。失败抛 `IndexFetchError`。"""


class MockIndexSource(IndexSource):
    """合成指数源:按关键词哈希生成确定性数据,用于本地与测试。"""

    def __init__(self, points: int = 8, source: IndexSourceName = IndexSourceName.BAIDU) -> None:
        self._points = points
        self._source = source

    def fetch(self, keyword: str) -> TrendSeries:
        seed = int(hashlib.md5(keyword.encode("utf-8")).hexdigest(), 16) % 1000
        base = 1000 + seed % 400
        now = datetime.now()
        points = [
            IndexPoint(ts=now - timedelta(hours=(self._points - i)), value=float(base + i * 50 + seed % 7))
            for i in range(self._points)
        ]
        return TrendSeries(keyword=keyword, source=self._source, points=points)


class DouyinIndexSource(IndexSource):
    """抖音指数(巨量算数)。

    用 Playwright 无头浏览器注入 Cookie,拦截页面的 XHR/JSON 响应,取到指数序列。
    若 Playwright 不可用或取不到数据,抛 `IndexFetchError` 交给上层降级到百度指数。
    注意:页面选择器与实际接口随站点变动,此处为可运行骨架。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def fetch(self, keyword: str) -> TrendSeries:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise IndexFetchError("playwright 未安装,无法请求巨量算数") from exc

        responses: list[dict] = []

        def _on_response(response) -> None:  # type: ignore[no-untyped-def]
            try:
                content_type = response.headers.get("content-type", "")
                if "json" in content_type:
                    responses.append(response.json())
            except Exception:  # noqa: BLE001
                pass

        try:
            with sync_playwright() as p:
                proxy_cfg = playwright_proxy(self._settings)
                browser = (
                    p.chromium.launch(headless=True, proxy=proxy_cfg)
                    if proxy_cfg
                    else p.chromium.launch(headless=True)
                )
                page = browser.new_page()
                page.on("response", _on_response)
                if self._settings.weibo_cookie:
                    page.add_init_script(f"document.cookie='{self._settings.weibo_cookie}';")
                page.goto(_DOUYIN_URL, wait_until="domcontentloaded", timeout=60_000)
                page.fill(_DOUYIN_SEARCH_SELECTOR, keyword)
                page.press(_DOUYIN_SEARCH_SELECTOR, "Enter")
                page.wait_for_timeout(8_000)
                browser.close()
        except Exception as exc:  # noqa: BLE001
            raise IndexFetchError(f"巨量算数请求失败:{exc}") from exc

        points = _find_trend_points(responses)
        if not points:
            raise IndexFetchError("未能从巨量算数响应中解析出指数序列")
        return TrendSeries(keyword=keyword, source=IndexSourceName.DOUYIN, points=points)


class BaiduIndexSource(IndexSource):
    """百度指数(MVP 降级源,替代微信指数)。

    requests 带会话 Cookie 与代理请求趋势接口,解析同套容错提取器。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @retry(attempts=2, base_delay=1.0, exceptions=(requests.RequestException,))
    def _post(self, session: requests.Session, data: dict[str, str]) -> dict:
        resp = session.post(_BAIDU_URL, data=data, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def fetch(self, keyword: str) -> TrendSeries:
        session = requests.Session()
        proxies = get_proxies(self._settings)
        if proxies:
            session.proxies.update(proxies)
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Referer": "https://index.baidu.com/",
            }
        )
        if self._settings.baidu_cookie:
            session.headers["Cookie"] = self._settings.baidu_cookie

        payload = json.dumps([[{"name": keyword}]])
        try:
            data = self._post(session, {"word": payload, "area": "0"})
        except Exception as exc:  # noqa: BLE001
            raise IndexFetchError(f"百度指数请求失败:{exc}") from exc
        finally:
            session.close()

        points = _find_trend_points(data)
        if not points:
            raise IndexFetchError(f"百度指数未返回可解析数据,keyword={keyword}")
        return TrendSeries(keyword=keyword, source=IndexSourceName.BAIDU, points=points)


class WeiboHeatIndexSource(IndexSource):
    """微博热度序列指数源。

    不做外部抓取:读取 ArchiveRepository 中跨多轮调度累积的热搜热度,
    构成该关键词的时间序列,再交给趋势分析引擎判涨(见 doc/dev.md §5.4)。
    需在采集入库后经多轮运行累积样本(样本下限见 `MIN_SAMPLES`)。
    """

    def __init__(self, repo: ArchiveRepository, limit: int = 30) -> None:
        self._repo = repo
        self._limit = limit

    def fetch(self, keyword: str) -> TrendSeries:
        points = self._repo.keyword_heat_series(keyword, limit=self._limit)
        if not points:
            raise IndexFetchError(f"暂无微博热度历史,keyword={keyword}")
        return TrendSeries(keyword=keyword, source=IndexSourceName.WEIBO, points=points)


class IndexFetcher:
    """按降级链获取指数序列。"""
    def __init__(self, chain: list[IndexSource]) -> None:
        self._chain = chain

    def fetch(self, keyword: str) -> TrendSeries | None:
        """依次尝试降级链上的源,返回首个成功的序列;全部失败返回 `None`。"""
        for source in self._chain:
            try:
                series = source.fetch(keyword)
                logger.info("指数获取成功 keyword=%s source=%s", keyword, series.source)
                return series
            except IndexFetchError as exc:
                logger.warning("指数源 %s 获取 %s 失败:%s", type(source).__name__, keyword, exc)
        return None

    def fetch_all(self, keywords: list[str]) -> list[TrendSeries]:
        """对每个关键词逐个获取;单个关键词失败不影响其它。"""
        series_list: list[TrendSeries] = []
        for keyword in keywords:
            series = self.fetch(keyword)
            if series is not None:
                series_list.append(series)
        return series_list


def build_index_fetcher(settings: Settings, repo: ArchiveRepository | None = None) -> IndexFetcher:
    """依据配置构造指数获取器。

    - `mock_index=true`:使用 `MockIndexSource`(本地/测试可跑通)。
    - 否则按 `INDEX_SOURCES`(逗号分隔、顺序即优先级)构建链,可用值:
      `weibo`(需 repo)、`douyin`、`baidu`。
    - 若配置为空或全部无效,兜底用 Mock 源,避免空链。
    """
    if settings.mock_index:
        return IndexFetcher([MockIndexSource()])

    names = [n.strip().lower() for n in settings.index_sources.split(",") if n.strip()]
    chain: list[IndexSource] = []
    for name in names:
        if name == "weibo":
            if repo is None:
                logger.warning("INDEX_SOURCES 含 weibo 但未提供 repo,已跳过")
                continue
            chain.append(WeiboHeatIndexSource(repo))
        elif name == "douyin":
            chain.append(DouyinIndexSource(settings))
        elif name == "baidu":
            chain.append(BaiduIndexSource(settings))
        else:
            logger.warning("未知指数源 %s,忽略", name)

    if not chain:
        logger.warning("INDEX_SOURCES=%s 无有效源,回退 MockIndexSource", settings.index_sources)
        return IndexFetcher([MockIndexSource()])
    return IndexFetcher(chain)
