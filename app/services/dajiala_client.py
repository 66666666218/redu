"""大家拉/极致了数据(dajiala.com)公众号 API 客户端(见 doc/dajiala-api.md)。

⚠️ 该平台接口的传输格式**不统一**,写错不报错但返回空照样扣费(实测学费 ¥0.5):
- form 表单: get_remain_money / post_condition / read_zan_pro
- JSON body: web_search / history_by_ghid
- GET query: article_detail

QPS 限制 ≤2 次/秒(超限返回 -1),客户端内置 0.6s 串行间隔。
错误模型:20001 金额不足 / 10002 key 有误 / 参数类错误(100/30001/20002,不扣费)。
"""
from __future__ import annotations

import threading
import time

import requests

from app.utils import get_logger

logger = get_logger(__name__)

BASE = "https://www.dajiala.com/fbmain/monitor/v3"

_NO_BALANCE = 20001
_BAD_KEY = 10002


class DajialaError(Exception):
    """dajiala 请求失败(带平台 code/msg)。"""


class DajialaNoBalance(DajialaError):
    """余额不足(20001)。"""


class DajialaAuthError(DajialaError):
    """key 无效(10002)。"""


class _QpsGate:
    """串行限速:两次请求间隔不低于 `min_gap` 秒(平台 QPS ≤2)。"""

    def __init__(self, min_gap: float = 0.6) -> None:
        self._min_gap = min_gap
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self, sleep=time.sleep) -> None:
        with self._lock:
            gap = time.time() - self._last
            if gap < self._min_gap:
                sleep(self._min_gap - gap)
            self._last = time.time()


class DajialaClient:
    """最小客户端:统一扣费/错误信封解析,QPS 限速;`_request` 可注入供测试。"""

    def __init__(self, key: str, timeout: int = 30, qps_min_gap: float = 0.6) -> None:
        self.key = key
        self.timeout = timeout
        self._gate = _QpsGate(qps_min_gap)

    # ---- 传输层(测试可整体替换) ----
    def _request(self, method: str, path: str, *, form: dict | None = None,
                 json_body: dict | None = None, params: dict | None = None) -> dict:
        url = f"{BASE}/{path}"
        try:
            resp = requests.request(method, url, data=form, json=json_body, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DajialaError(f"dajiala 请求失败:{exc}") from exc
        try:
            obj = resp.json()
        except ValueError as exc:
            raise DajialaError(f"dajiala 响应非 JSON(HTTP {resp.status_code}):{resp.text[:200]}") from exc
        return self._check(obj)

    def _check(self, obj: dict) -> dict:
        """统一信封校验:code!=0 时按语义抛错。"""
        code = obj.get("code")
        if code == 0:
            return obj
        msg = str(obj.get("msg") or obj.get("error_msg") or "未知错误")
        if code == _NO_BALANCE:
            raise DajialaNoBalance(f"dajiala 余额不足,请充值:{msg}")
        if code == _BAD_KEY:
            raise DajialaAuthError(f"dajiala key 无效:{msg}")
        raise DajialaError(f"dajiala 接口错误 code={code}:{msg}")

    def _call(self, method: str, path: str, **kw) -> dict:
        self._gate.wait()
        return self._request(method, path, **kw)

    # ---- 各接口 ----
    def remain_money(self) -> float:
        """账户余额(免费)。"""
        return float(self._call("POST", "get_remain_money", form={"key": self.key}).get("remain_money") or 0)

    def post_condition(self, article_url: str) -> dict:
        """公众号当天发文(¥0.14/次):链接 → {nickname, ghid, data:[当天全部发文]}。"""
        return self._call("POST", "post_condition", form={"key": self.key, "url": article_url})

    def read_zan_pro(self, article_url: str) -> dict:
        """单篇流量六指标(¥0.06/次):read/zan/looking/share_num/collect_num/comment_count。"""
        obj = self._call("POST", "read_zan_pro", form={"key": self.key, "url": article_url})
        return obj.get("data") or {}

    def web_search(self, keyword: str, offset: int = 0, publish_time_type: int = 1,
                   sort_type: int = 1, current_page: int = 1) -> dict:
        """搜一搜实时搜公众号文章(¥0.5/次,JSON body)。默认:最近1天+按最新。"""
        return self._call("POST", "web_search", json_body={
            "key": self.key, "keyword": keyword, "mode": 1, "currentPage": current_page,
            "offset": offset, "publish_time_type": publish_time_type,
            "search_type": 1, "sort_type": sort_type,
        })

    def history_by_ghid(self, ghid: str = "", article_url: str = "", offset: str = "") -> dict:
        """历史发文列表 Pro(¥0.14/页,JSON body):ghid/url 二选一,offset 翻页。"""
        return self._call("POST", "history_by_ghid", json_body={
            "key": self.key, "ghid": ghid, "url": article_url, "offset": str(offset or ""),
        })

    def article_detail(self, article_url: str, mode: int | None = None) -> dict:
        """文章正文(GET;长链 ¥0.01/次、短链 ¥0.03/次;mode=1 带图/2 纯文字)。"""
        params: dict = {"key": self.key, "url": article_url}
        if mode is not None:
            params["mode"] = mode
        return self._call("GET", "article_detail", params=params)
