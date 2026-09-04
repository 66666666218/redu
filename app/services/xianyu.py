"""闲鱼虚拟商品热榜采集(见 doc/dev.md)。

用闲鱼登录 Cookie(mtop 签名)按"虚拟商品"关键词搜索,以闲鱼"综合"顺序作为热度基准,
跨关键词聚合、去重、排名,得到热销虚拟商品榜。数据源可行、合规(读自己登录态下的公开搜索)。

传输层用 `curl_cffi`(impersonate="chrome")模拟 Chrome TLS/HTTP2 指纹,替代裸 `requests`——
纯协议下伪浏览器指纹能降低被 mtop 风控识别为机器人的概率(见 scripts/probe_xianyu_curl.py)。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
from datetime import datetime, time as dt_time
from pathlib import Path

from curl_cffi import requests as curl

from config.settings import Settings
from app.utils import get_logger

logger = get_logger(__name__)

H5_BASE = "https://h5api.m.goofish.com/h5"
API = "mtop.taobao.idlemtopsearch.pc.search"
APP_KEY = "34839810"  # 闲鱼 mtop appKey
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
_MTOP_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
    "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": '"Chromium";v="147", "Not.A/Brand";v="8", "Google Chrome";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Origin": "https://www.goofish.com",
    "Referer": "https://www.goofish.com/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "priority": "u=1, i",
    "Content-Type": "application/x-www-form-urlencoded",
}


class XianyuError(Exception):
    """闲鱼请求/解析失败。"""


class XianyuRateLimit(XianyuError):
    """闲鱼限流(退避后仍失败)。"""


class XianyuVerify(XianyuError):
    """闲鱼人机验证(滑块):需人工过滑块或换出口 IP。

    与限流不同——重试/退避均无效(实测连续重试仍 `FAIL_SYS_USER_VALIDATE`),
    应尽快抛给上层识别为"账号/出口被标记",而非干等数分钟。
    """


# mtop 令牌错误码(需刷新 _m_h5_tk 后重试;参考开源 goofish-client)
TOKEN_ERRORS = {
    "FAIL_SYS_TOKEN_EMPTY",
    "FAIL_SYS_TOKEN_ILLEGAL",
    "FAIL_SYS_SESSION_EXPIRED",
    "FAIL_SYS_TOKEN_EXOIRED",
    "FAIL_SYS_USER_NOT_LOGIN",
}
# 真限流码(可退避重试);注意 USER_VALIDATE 属"人机验证",归 XianyuVerify,不进此表
RATE_ERRORS = {"FAIL_SYS_RATE_LIMIT", "FAIL_SYS_USER_LIMIT"}


class XianyuClient:
    """mtop 签名 + 登录态的最小客户端(参考开源 cv-cat/XianYuApis)。

    用 curl_cffi(模拟 Chrome 指纹)的 cookie jar + 完整浏览器头;data 只传 {"itemId":X} 等,
    带 spm_pre/log_id;令牌随响应刷新,令牌错自动重试,限流优雅报错。
    """
    APP_KEY = "34839810"

    def __init__(self, cookie: str) -> None:
        # 用 curl_cffi 模拟 Chrome 的 TLS/HTTP2 指纹,冒充浏览器从协议层发出,
        # 降低被闲鱼 mtop 风控识别为机器人而触发人机验证(滑块)的概率。
        self.session = curl.Session(impersonate="chrome")
        self.session.headers.update(_MTOP_HEADERS)
        self._seed_cookies(cookie)

    def _seed_cookies(self, cookie: str) -> None:
        for pair in cookie.split("; "):
            if "=" not in pair:
                continue
            name, _, val = pair.partition("=")
            self.session.cookies.set(name.strip(), val.strip(), domain=".goofish.com", path="/")

    def _token(self) -> str:
        return (self.session.cookies.get("_m_h5_tk", "") or "").split("_")[0]

    def _refresh(self, resp: curl.Response) -> bool:
        m = re.search(r"_m_h5_tk=([0-9a-f]{32})_", resp.headers.get("set-cookie", ""))
        if m and m.group(1):
            self.session.cookies.set("_m_h5_tk", m.group(1), domain=".goofish.com", path="/")
            return True
        return False

    def _sign(self, t: str, token: str, data: str) -> str:
        return hashlib.md5(f"{token}&{t}&{self.APP_KEY}&{data}".encode()).hexdigest()

    def _post(self, api: str, data_obj: dict) -> dict:
        """发一次 mtop 请求;令牌错自动刷新重试,限流**指数退避**,人机验证立即抛 `XianyuVerify`。

        退避策略:遇 `FAIL_SYS_RATE_LIMIT`/`FAIL_SYS_USER_LIMIT` 等真限流码,
        按 30s/90s/180s 递增等待后重试(最多 3 次),仍失败抛 `XianyuRateLimit`。
        关键:限流时**不能连环猛打**(会加重风控),而应拉长时间隔再试。
        `FAIL_SYS_USER_VALIDATE`(人机验证/滑块)不是限流——实测退避重试仍无效,
        立即抛 `XianyuVerify`,由上层判定为"需人工过滑块或更换出口 IP"。
        """
        last_rate_err: str | None = None
        backoff = [30, 90, 180]
        for attempt in range(1 + len(backoff)):
            data_val = json.dumps(data_obj, ensure_ascii=False, separators=(",", ":"))
            t = str(int(time.time() * 1000))
            params = {
                "jsv": "2.7.2", "appKey": self.APP_KEY, "t": t, "sign": self._sign(t, self._token(), data_val),
                "v": "1.0", "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
                "timeout": "20000", "api": api, "sessionOption": "AutoLoginOnly",
                "spm_cnt": "a21ybx.im.0.0", "spm_pre": "a21ybx.item.want.1.14ad3da6ALVq3n", "log_id": "14ad3da6ALVq3n",
            }
            try:
                resp = self.session.post(f"{H5_BASE}/{api}/1.0/", params=params, data={"data": data_val}, timeout=20)
            except curl.RequestsError as exc:
                raise XianyuError(f"闲鱼请求失败:{exc}") from exc
            try:
                obj = resp.json()
            except ValueError as exc:
                raise XianyuError(f"闲鱼响应非 JSON:{exc}") from exc
            ret = obj.get("ret", [""])[0]
            code = ret.split("::")[0]
            if code in TOKEN_ERRORS:
                if self._refresh(resp):
                    continue  # 令牌已刷新,重试
                raise XianyuError(f"闲鱼令牌错误:{ret}")
            if "USER_VALIDATE" in code:
                # 人机验证(滑块):不是限流,退避无效(实测连续重试仍失败)。
                # 立即抛 XianyuVerify,由上层识别为"需人工过滑块/换IP",避免干等数分钟。
                raise XianyuVerify(f"闲鱼人机验证(滑块),需人工处理:{ret}")
            if code in RATE_ERRORS:
                last_rate_err = ret
                if attempt < len(backoff):
                    wait = backoff[attempt]
                    logger.warning("闲鱼限流,%.0fs 后重试:%s", wait, ret)
                    time.sleep(wait)
                    continue
                raise XianyuRateLimit(f"闲鱼限流,请稍后再试:{ret}")
            if code and not code.startswith("SUCCESS"):
                raise XianyuError(f"闲鱼接口返回:{ret}")
            return obj
        raise XianyuRateLimit(f"闲鱼限流,请稍后再试:{last_rate_err}")

    def search(self, keyword: str, page: int = 1, rows: int = 30) -> list[dict]:
        payload = {
            "pageNumber": page, "keyword": keyword, "fromFilter": False, "rowsPerPage": rows,
            "sortValue": "", "sortField": "", "customDistance": "", "gps": "", "propValueStr": {},
            "customGps": "", "searchReqFromPage": "pcSearch", "extraFilterValue": "{}", "userPositionJson": "{}",
        }
        obj = self._post(API, payload)
        items = _extract_items(obj)
        if not items:
            raise XianyuError(f"未解析到商品,keyword={keyword}")
        logger.debug("闲鱼搜索 %s → %s 条", keyword, len(items))
        return items

    def detail(self, item_id: str) -> dict:
        return self._post("mtop.taobao.idle.pc.detail", {"itemId": item_id})


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
    except (XianyuVerify, XianyuRateLimit):
        raise  # 人机验证/限流:交给上层停止连环抓详情(避免加剧风控)
    except Exception:  # noqa: BLE001
        pass
    return out


def collect_hot(settings: Settings, client: XianyuClient | None = None) -> list[dict]:
    """搜索多个虚拟商品关键词,按综合顺序聚合、去重、排名。"""
    keywords = [k.strip() for k in settings.xianyu_keywords.split(",") if k.strip()]
    client = client or XianyuClient(load_cookie(settings.goofish_cookie_file))

    buckets: dict[str, dict] = {}
    base_delay = getattr(settings, "xianyu_request_delay", None) or getattr(settings, "request_delay_seconds", 2.5)
    saw_verify = False
    success = 0
    for idx, kw in enumerate(keywords):
        try:
            items = client.search(kw)
        except XianyuVerify:
            # 人机验证是账号/IP 级的,一旦出现后续关键词多半也会被挡——
            # 立即停止尝试,避免连环猛打加重风控,保留已采到的部分数据。
            saw_verify = True
            break
        except XianyuError as exc:
            logger.warning("闲鱼关键词 %s 失败:%s", kw, exc)
        else:
            success += 1
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

    # 一个词都没采到且被验证 → 让上层识别为"需人工处理",避免误报成功 0 条
    if success == 0 and saw_verify:
        raise XianyuVerify("闲鱼人机验证(滑块),全部关键词均未采集")

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
