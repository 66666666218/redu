"""闲鱼虚拟商品热榜采集(见 doc/dev.md)。

用闲鱼登录 Cookie(mtop 签名)按"虚拟商品"关键词搜索,以闲鱼"综合"顺序作为热度基准,
跨关键词聚合、去重、排名,得到热销虚拟商品榜。数据源可行、合规(读自己登录态下的公开搜索)。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, time
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


class XianyuClient:
    """mtop 签名 + 登录态的最小客户端。"""

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie
        self.token = self._extract_token(cookie)
        self._session = requests.Session()

    @staticmethod
    def _extract_token(cookie: str) -> str:
        m = re.search(r"_m_h5_tk=([0-9a-f]{32})_", cookie)
        return m.group(1) if m else ""

    def _refresh_session(self, resp: requests.Response) -> None:
        """从响应 set-cookie 刷新 _m_h5_tk 令牌与 Cookie,避免签名过期。"""
        if "set-cookie" in resp.headers:
            self.cookie = f"{self.cookie}; {resp.headers['set-cookie']}"
            m = re.search(r"_m_h5_tk=([0-9a-f]{32})_", resp.headers["set-cookie"])
            if m:
                new_token = m.group(1)
                if new_token and new_token != self.token:
                    self.token = new_token

    def _sign(self, t: str, data: str) -> str:
        raw = f"{self.token}&{t}&{APP_KEY}&{data}"
        return hashlib.md5(raw.encode()).hexdigest()

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
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
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
            "api": API,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.search.0.0",
        }
        headers = {"User-Agent": _UA, "Cookie": self.cookie, "Referer": "https://www.goofish.com/"}
        try:
            resp = self._session.post(f"{H5_BASE}/{API}/1.0/", params=params, data={"data": data}, headers=headers, timeout=20)
        except requests.RequestException as exc:
            raise XianyuError(f"闲鱼请求失败:{exc}") from exc
        self._refresh_session(resp)
        try:
            obj = resp.json()
        except ValueError as exc:
            raise XianyuError("闲鱼响应非 JSON") from exc
        ret = obj.get("ret", [])
        if ret and not any("SUCCESS" in r for r in ret):
            raise XianyuError(f"闲鱼接口返回:{ret}")
        items = _extract_items(obj)
        if not items:
            raise XianyuError(f"未解析到商品,keyword={keyword}")
        logger.debug("闲鱼搜索 %s → %s 条", keyword, len(items))
        return items


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


def collect_hot(settings: Settings, client: XianyuClient | None = None) -> list[dict]:
    """搜索多个虚拟商品关键词,按综合顺序聚合、去重、排名。"""
    keywords = [k.strip() for k in settings.xianyu_keywords.split(",") if k.strip()]
    client = client or XianyuClient(load_cookie(settings.goofish_cookie_file))

    buckets: dict[str, dict] = {}
    for kw in keywords:
        try:
            items = client.search(kw)
        except XianyuError as exc:
            logger.warning("闲鱼关键词 %s 失败:%s", kw, exc)
            continue
        for pos, it in enumerate(items, start=1):
            iid = str(it.get("itemId"))
            if not iid:
                continue
            bucket = buckets.setdefault(iid, {"item": it, "keywords": [], "ranks": []})
            bucket["keywords"].append(kw)
            bucket["ranks"].append(pos)

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


def run_xianyu(settings: Settings | None = None, repo: object | None = None) -> dict:
    """执行一次闲鱼热榜采集:收集 → 存库 → 返回结果。"""
    from config.settings import get_settings
    from app.storage import ArchiveRepository

    settings = settings or get_settings()
    repo = repo or ArchiveRepository(settings.data_dir)
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    items = collect_hot(settings)
    repo.save_xianyu_top(run_id, items)  # type: ignore[attr-defined]
    logger.info("闲鱼热榜完成 run=%s 条数=%s", run_id, len(items))
    return {"run_id": run_id, "count": len(items), "items": items}


def summarize_today(items: list[dict]) -> list[dict]:
    """把当日多条热榜条目按 item 聚合,得到"今日热榜"。

    排序:当日出现场次(occurrences)降序优先,其次 best_rank(综合序)升序。
    """
    agg: dict[str, dict] = {}
    for it in items:
        iid = str(it["item_id"])
        a = agg.setdefault(
            iid,
            {"item_id": iid, "title": it["title"], "price": it["price"], "occurrences": 0, "best_rank": 9999, "keywords": set()},
        )
        a["occurrences"] += 1
        if int(it["best_rank"]) < a["best_rank"]:
            a["best_rank"] = int(it["best_rank"])
        a["title"] = it["title"]
        a["price"] = it["price"]
        for k in str(it.get("keywords", "")).split(","):
            if k:
                a["keywords"].add(k)
    ranked = sorted(agg.values(), key=lambda a: (-a["occurrences"], a["best_rank"]))
    for a in ranked:
        a["keywords"] = ",".join(sorted(a["keywords"]))
    return ranked


def build_daily_body(summary: list[dict], date_str: str) -> str:
    lines = [f"今日闲鱼虚拟商品热榜({date_str})", f"共上榜 {len(summary)} 个(按当日出现场次 / 综合序)", ""]
    for i, a in enumerate(summary, start=1):
        lines.append(
            f"{i}. [{a['occurrences']}场/综合#{a['best_rank']}] {a['title'][:40]}  {a['price']}  ({a['keywords']})"
        )
    return "\n".join(lines)


def run_xianyu_daily(
    settings: Settings | None = None,
    repo: object | None = None,
    notifier: object | None = None,
    top_n: int | None = None,
) -> dict:
    """生成并发送"今日闲鱼热榜"总结:聚合当日采集 → 存库 → 发邮件。"""
    from config.settings import get_settings
    from app.services.notifier import get_notifier
    from app.storage import ArchiveRepository

    settings = settings or get_settings()
    repo = repo or ArchiveRepository(settings.data_dir)
    notifier = notifier or get_notifier(settings)
    today = datetime.now().date()
    start_iso = datetime.combine(today, time.min).isoformat()
    items = repo.xianyu_items_between(start_iso)
    summary = summarize_today(items)[: (top_n or settings.xianyu_top_n)]
    repo.save_xianyu_summary(today.isoformat(), summary)  # type: ignore[attr-defined]
    subject = f"今日闲鱼虚拟商品热榜({today.isoformat()})"
    body = build_daily_body(summary, today.isoformat())
    sent = notifier.send(subject, body)  # type: ignore[attr-defined]
    logger.info("每日闲鱼热榜总结完成 date=%s 条数=%s sent=%s", today, len(summary), sent)
    return {"date": today.isoformat(), "count": len(summary), "items": summary, "sent": sent}


if __name__ == "__main__":
    import json
    import sys

    from app.utils import setup_logging

    setup_logging()
    if "--daily" in sys.argv:
        outcome = run_xianyu_daily()
        print("每日闲鱼热榜总结完成:", outcome["date"], "条数", outcome["count"], "已发", outcome.get("sent"))
        for it in outcome["items"][:10]:
            print(f"  [{it['occurrences']}场/综合#{it['best_rank']}] {it['title'][:32]}  {it['price']}")
    else:
        outcome = run_xianyu()
        print("闲鱼热榜收集完成:", outcome["count"], "条")
        for it in outcome["items"][:10]:
            print(f"  [{it['hit_keywords']}kw/综合#{it['best_rank']}] {it['title'][:32]}   {it['price']}")
