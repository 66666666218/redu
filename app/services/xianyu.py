"""闲鱼虚拟商品热榜采集(见 doc/dev.md)。

用闲鱼登录 Cookie(mtop 签名)按"虚拟商品"关键词搜索,以闲鱼"综合"顺序作为热度基准,
跨关键词聚合、去重、排名,得到热销虚拟商品榜。数据源可行、合规(读自己登录态下的公开搜索)。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
from datetime import datetime, time as dt_time
from pathlib import Path

import requests

from config.settings import Settings
from app.utils import get_logger

logger = get_logger(__name__)

H5_BASE = "https://h5api.m.goofish.com/h5"
API = "mtop.taobao.idlemtopsearch.pc.search"
APP_KEY = "34839810"  # 闲鱼 mtop appKey
_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
)


class XianyuError(Exception):
    """闲鱼请求/解析失败。"""


# mtop 令牌错误码(需刷新 _m_h5_tk 后重试;参考开源 goofish-client)
TOKEN_ERRORS = {
    "FAIL_SYS_TOKEN_EMPTY",
    "FAIL_SYS_TOKEN_ILLEGAL",
    "FAIL_SYS_SESSION_EXPIRED",
    "FAIL_SYS_TOKEN_EXOIRED",
    "FAIL_SYS_USER_NOT_LOGIN",
}
# 限流/风控码(需退避,不重试)
RATE_ERRORS = {"FAIL_SYS_USER_VALIDATE", "FAIL_SYS_RATE_LIMIT", "FAIL_SYS_USER_LIMIT"}


class XianyuClient:
    """mtop 签名 + 登录态的最小客户端(带令牌刷新/重试)。"""

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie
        self.token = self._extract_token(cookie)

    @staticmethod
    def _extract_token(cookie: str) -> str:
        m = re.search(r"_m_h5_tk=([0-9a-f]{32})_", cookie)
        return m.group(1) if m else ""

    def _headers(self) -> dict:
        return {
            "User-Agent": _UA,
            "Cookie": self.cookie,
            "Referer": "https://www.goofish.com/",
            "Origin": "https://www.goofish.com",
        }

    def _refresh_token_from(self, resp: requests.Response, append_cookie: bool = False) -> bool:
        """从响应 Set-Cookie 刷新 _m_h5_tk 令牌;返回是否发生了更新。"""
        set_cookie = resp.headers.get("set-cookie", "")
        if not set_cookie:
            return False
        if append_cookie and set_cookie not in self.cookie:
            self.cookie = f"{self.cookie}; {set_cookie}"
        m = re.search(r"_m_h5_tk=([0-9a-f]{32})_", set_cookie)
        if m and m.group(1) and m.group(1) != self.token:
            self.token = m.group(1)
            return True
        return False

    def _refresh_session(self, resp: requests.Response) -> None:
        """每次请求后刷新令牌(与开源库 updateFromHeaders 一致)。"""
        self._refresh_token_from(resp, append_cookie=True)

    def _sign(self, t: str, data: str) -> str:
        raw = f"{self.token}&{t}&{APP_KEY}&{data}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _post(self, api: str, data_obj: dict) -> dict:
        data = json.dumps(data_obj, ensure_ascii=False, separators=(",", ":"))
        t = str(int(time.time() * 1000))
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": t,
            "sign": self._sign(t, data),
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.search.0.0",
        }
        try:
            resp = requests.post(
                f"{H5_BASE}/{api}/1.0/", params=params, data={"data": data}, headers=self._headers(), timeout=20
            )
        except requests.RequestException as exc:
            raise XianyuError(f"闲鱼请求失败:{exc}") from exc
        self._refresh_session(resp)
        obj = resp.json()
        ret = obj.get("ret", [""])[0]
        code = ret.split("::")[0]
        if code in TOKEN_ERRORS:
            # 刷新 token 后重试一次
            if self._refresh_token_from(resp) and obj is not None:
                return self._post(api, data_obj)
            raise XianyuError(f"闲鱼令牌错误:{ret}")
        if code in RATE_ERRORS:
            raise XianyuError(f"闲鱼限流,请稍后再试:{ret}")
        if code and not code.startswith("SUCCESS"):
            raise XianyuError(f"闲鱼接口返回:{ret}")
        return obj

    def search(self, keyword: str, page: int = 1, rows: int = 30) -> list[dict]:
        """按关键词搜索,返回按闲鱼"综合"顺序的商品列表。"""
        payload = {
            "pageNumber": page,
            "keyword": keyword,
            "fromFilter": False,
            "rowsPerPage": rows,
            "sortValue": "",
            "sortField": "",
            "customDistance": "",
            "gps": "",
            "propValueStr": {},
            "customGps": "",
            "searchReqFromPage": "pcSearch",
            "extraFilterValue": "{}",
            "userPositionJson": "{}",
        }
        obj = self._post(API, payload)
        items = _extract_items(obj)
        if not items:
            raise XianyuError(f"未解析到商品,keyword={keyword}")
        logger.debug("闲鱼搜索 %s → %s 条", keyword, len(items))
        return items

    def detail(self, item_id: str) -> dict:
        """请求商品详情,返回 JSON。带令牌刷新与重试。"""
        return self._post("mtop.taobao.idle.pc.detail", {"itemId": item_id, "id": item_id})


def _extract_items(obj: object) -> list[dict]:
    """递归找出所有含 title+itemId 的商品卡(保持 return 顺序 = 综合顺序)。"""
    found: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "title" in node and "itemId" in node:
                found.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return found


def item_title(it: dict) -> str:
    return str(it.get("title", "")).strip()


def item_price(it: dict) -> str:
    price = it.get("price") or []
    if isinstance(price, list):
        text = "".join(str(p.get("text", "")) for p in price if isinstance(p, dict))
    else:
        text = str(price)
    return re.sub(r"¥+", "¥", text).strip()


def load_cookie(path: str) -> str:
    """从文件读取闲鱼 Cookie 字符串。"""
    return Path(path).read_text(encoding="utf-8").strip()


def _deep_find(node: object, field: str):
    """递归找第一个命中的字段值。"""
    if isinstance(node, dict):
        if field in node:
            return node[field]
        for v in node.values():
            r = _deep_find(v, field)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _deep_find(v, field)
            if r is not None:
                return r
    return None


def fetch_detail(client: XianyuClient, item_id: str) -> dict:
    """抓取单个闲鱼商品深度指标(想要数/收藏/已售/类目/卖家粉丝)。

    数据来自 `mtop.taobao.idle.pc.detail` 的 `data.itemDO`:
    wantCnt(人想要)、collectCnt(收藏)、soldCnt(已售)、itemCatDTO/categoryId(类目)。
    返回可作为 XianyuDaily 快照字段的字典。
    """
    out = {"category": "", "want_count": 0, "collect_count": 0, "sold_count": 0, "seller_fans": 0}
    try:
        obj = client.detail(item_id) or {}
        item = _deep_find(obj, "itemDO") or {}
        out["want_count"] = int(item.get("wantCnt") or 0)
        out["collect_count"] = int(item.get("collectCnt") or 0)
        out["sold_count"] = int(item.get("soldCnt") or 0)
        cat = item.get("itemCatDTO") or {}
        out["category"] = str(cat.get("catName") or item.get("categoryId") or cat.get("catId") or "")[:64]
        seller = _deep_find(obj, "sellerDO") or {}
        out["seller_fans"] = int(
            seller.get("followerCount") or seller.get("fansCount") or seller.get("sellerFans") or 0
        )
    except Exception:  # noqa: BLE001
        pass
    return out


def collect_hot(settings: Settings, client: XianyuClient | None = None) -> list[dict]:
    """搜索多个虚拟商品关键词,按综合顺序聚合、去重、排名。"""
    keywords = [k.strip() for k in settings.xianyu_keywords.split(",") if k.strip()]
    client = client or XianyuClient(load_cookie(settings.goofish_cookie_file))

    buckets: dict[str, dict] = {}
    base_delay = getattr(settings, "request_delay_seconds", 2.5)
    for idx, kw in enumerate(keywords):
        try:
            items = client.search(kw)
        except XianyuError as exc:
            logger.warning("闲鱼关键词 %s 失败:%s", kw, exc)
        else:
            for pos, it in enumerate(items, start=1):
                iid = str(it.get("itemId"))
                if not iid:
                    continue
                bucket = buckets.setdefault(iid, {"item": it, "keywords": [], "ranks": []})
                bucket["keywords"].append(kw)
                bucket["ranks"].append(pos)
        # 请求间隔(随机抖动),避免连续请求触发风控
        if idx < len(keywords) - 1:
            time.sleep(base_delay * random.uniform(0.8, 1.4))

    # 排名:命中关键词次数多优先,其次综合序靠前(min rank)优先
    ranked = sorted(
        buckets.values(),
        key=lambda b: (-len(b["keywords"]), min(b["ranks"]), item_title(b["item"])),
    )
    top = ranked[: settings.xianyu_top_n]
    result = []
    for b in top:
        item = b["item"]
        result.append(
            {
                "item_id": str(item.get("itemId")),
                "title": item_title(item),
                "price": item_price(item),
                "seller": str(item.get("userNickName", "")),
                "pic": str(item.get("picUrl", "")),
                "hit_keywords": len(b["keywords"]),
                "best_rank": min(b["ranks"]),
                "keywords": ",".join(b["keywords"]),
            }
        )
    return result
