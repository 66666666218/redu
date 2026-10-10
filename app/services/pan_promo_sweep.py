"""**按强模式搜索**清扫各盘里别人塞的引流文件(2026-10-10)。

## 判据**复用** `quark_dup`,不重写

`STRONG_PATTERNS` / `WEAK_PATTERNS` / `PROTECT_NAMES` 与 `is_promo_name()` 是
**三次真数据收紧**出来的(见那个模块里「第三次收紧」的注释:裸 `代发`/`引流` 全是误判源)。
⇒ 本模块只提供**各盘的搜索/删除适配**,判据一律从那边引 —— 两处各写一遍必然飘。

## 为什么是"按模式搜",不是"递归扫"

本仓实测(见 `scripts/quark_promo_sweep.py`):递归扫每个资源包(深度 ≥2)在真盘上要
**几十上百次** `list_dir`,而且**只能扫到扫过的深度** —— 扫完两层之后,
`切勿卸载网盘` 这类模板 txt 盘上**仍有 96 份**(藏在更深的子目录里)。
**搜索接口一次就命中全部深度**,省调用也扫得全。

## 安全

* **弱模式只报告,永不自动删**(理由同 `quark_dup.classify`);
* 删一律进**回收站**(百度走 `opera=delete`、迅雷走 `trash_files`);
* 默认 dry-run,要 `--yes` 才动手。
"""
from __future__ import annotations

from typing import Any, Protocol

from app.services.quark_dup import WEAK_PATTERNS, is_promo_name
from app.utils import get_logger

logger = get_logger(__name__)

#: 搜索用哪些词。**用强模式里的短词**(搜索是子串匹配,长词反而漏)。
SEARCH_WORDS: tuple[str, ...] = (
    "切勿卸载", "不要卸载", "下载前必看", "先保存", "加群", "进群", "赚米",
    "扫码", "防丢失", "收藏不迷路", "代发",
)


class PanAdapter(Protocol):
    """一个盘要能做的两件事。**实现者只管搬运,判定归本模块。**"""

    name: str
    label: str

    def search_by_name(self, keyword: str, limit: int = 200) -> list[dict]:
        """按名字搜(全深度)。返回 `[{"name","path","size","dir","raw"}]`。"""
        ...

    def delete(self, items: list[dict]) -> dict:
        """把条目移进**回收站**。返回 `{"deleted": n, "errors": [...]}`。"""
        ...


# ───────────── 百度 ─────────────

class BaiduAdapter:
    """百度网盘(网页会话)。

    ⚠️ `copy` 那条路由实测**不可用**(`opera=copy` 恒 `errno=2`,而 `delete` 正常)——
    所以本模块在百度上**只做搜索 + 删除**,不做复制。
    """

    name = "baidu"
    label = "百度网盘"

    def __init__(self, cookie: str) -> None:
        from app.services.baidupan_transfer import PAN_API, BaiduPanClient

        self.cli = BaiduPanClient(cookie)
        self._api = PAN_API

    def search_by_name(self, keyword: str, limit: int = 200) -> list[dict]:
        out: list[dict] = []
        page = 1
        while len(out) < limit and page <= 10:
            r = self.cli._browser().get(
                f"{self._api}/api/search",
                params={"key": keyword, "recursion": 1, "page": page, "num": 100, "web": 1},
                timeout=self.cli.timeout)
            j = r.json()
            if j.get("errno") != 0:
                raise RuntimeError(f"百度搜索失败(errno={j.get('errno')} key={keyword})")
            lst = list(j.get("list") or [])
            for x in lst:
                out.append({"name": str(x.get("server_filename") or ""),
                            "path": str(x.get("path") or ""),
                            "size": int(x.get("size") or 0),
                            "dir": bool(x.get("isdir")), "raw": x})
            if len(lst) < 100:
                break
            page += 1
        return out

    def delete(self, items: list[dict]) -> dict:
        paths = [str(x["path"]) for x in items if x.get("path")]
        if not paths:
            return {"deleted": 0, "errors": []}
        try:
            self.cli.delete_paths(paths)
            return {"deleted": len(paths), "errors": []}
        except Exception as exc:                     # noqa: BLE001 - 失败要如实报
            return {"deleted": 0, "errors": [f"{type(exc).__name__}: {str(exc)[:120]}"]}


# ───────────── 迅雷 ─────────────

class XunleiAdapter:
    """迅雷网盘(纯协议)。删走 `trash_files`(**回收站**)。"""

    name = "xunlei"
    label = "迅雷网盘"

    def __init__(self, settings: Any = None) -> None:
        from app.services import xunlei_transfer as xt

        self.xt = xt
        self.cred = xt._fresh_cred(xt._credentials(settings))

    def search_by_name(self, keyword: str, limit: int = 200) -> list[dict]:
        import requests

        out: list[dict] = []
        # ⚠️ 端点是 `/drive/v1/files:search`(冒号式路由,扫 dex 拿到的)—— 与
        #    `/drive/v1/files?parent_id=` 那种不是一回事,别写混。
        r = requests.get(f"{self.xt._API}/drive/v1/files:search",
                         headers=self.xt._drive_headers(self.cred),
                         params={"keyword": keyword, "limit": min(limit, 100), "page": 1},
                         timeout=30)
        j = self.xt._json(r)
        for x in (j.get("files") or j.get("list") or []):
            out.append({"name": str(x.get("name") or ""), "path": str(x.get("name") or ""),
                        "size": int(x.get("size") or 0),
                        "dir": str(x.get("kind")) == "drive#folder",
                        "id": str(x.get("id") or ""), "raw": x})
        return out

    def delete(self, items: list[dict]) -> dict:
        ids = [str(x["id"]) for x in items if x.get("id")]
        if not ids:
            return {"deleted": 0, "errors": []}
        try:
            self.xt.trash_files(ids, self.cred)
            return {"deleted": len(ids), "errors": []}
        except Exception as exc:                     # noqa: BLE001
            return {"deleted": 0, "errors": [f"{type(exc).__name__}: {str(exc)[:120]}"]}


# ───────────── 判定(与盘的实现无关) ─────────────

def build_plan(adapter: PanAdapter, *, words: tuple[str, ...] = SEARCH_WORDS) -> dict:
    """搜一遍 → 分三堆:**删**(强模式)/ **待确认**(弱模式)/ **留**。**只读。**"""
    seen: dict[str, dict] = {}
    errors: list[str] = []
    for w in words:
        try:
            for it in adapter.search_by_name(w):
                key = it.get("path") or it.get("id") or ""
                if key and key not in seen:
                    it["why_word"] = w
                    seen[key] = it
        except Exception as exc:                     # noqa: BLE001 - 单词语失败不中断
            errors.append(f"{w}: {type(exc).__name__}: {str(exc)[:80]}")

    delete: list[dict] = []
    review: list[dict] = []
    for it in seen.values():
        nm = it.get("name") or ""
        if not nm or it.get("dir"):
            continue                                  # 目录不删(里面可能是资源本身)
        if nm in ("简介.doc",) or "简介" in nm:
            continue                                  # ⚠️ 我们自己的简介,永不删
        if is_promo_name(nm):                         # 强模式(已剥括号再比)
            delete.append(it)
        elif any(p in nm for p in WEAK_PATTERNS):
            review.append(it)                         # 弱模式:只报告
    return {"delete": delete, "review": review, "seen": len(seen), "errors": errors,
            "n_delete": len(delete), "freed": sum(d.get("size") or 0 for d in delete)}


def apply_plan(adapter: PanAdapter, plan: dict) -> dict:
    """把 `delete` 那堆移进回收站。**只删这一堆,别的都不碰。**"""
    return adapter.delete(plan.get("delete") or [])


def adapters() -> dict[str, Any]:
    """按平台名造适配器(延迟导入各自的客户端)。"""
    return {"baidu": BaiduAdapter, "xunlei": XunleiAdapter}
