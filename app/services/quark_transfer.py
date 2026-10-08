"""夸克网盘转存 + 二次分享(移植自 xg.djxx.club providers/quark,async→sync 同构改写)。

协议要点(来自 QuarkPanDirectLine 项目,xg 生产验证):
- 分享域名: https://drive-pc.quark.cn   文件域名: https://drive-h.quark.cn
- 取分享 token: POST /1/clouddrive/share/sharepage/token  {pwd_id, passcode}
- 列分享详情:  GET  /1/clouddrive/share/sharepage/detail?pwd_id&stoken&pdir_fid&_page&_size
- 转存:        POST /1/clouddrive/share/sharepage/save (任务轮询 /1/clouddrive/task)
- 创建分享:    POST /1/clouddrive/share (任务轮询) + POST /1/clouddrive/share/password
- 凭证: 浏览器登录 pan.quark.cn 后复制的完整 Cookie
- 错误语义: 401=Cookie 失效;"capacity limit"=容量不足
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from collections.abc import Mapping
from typing import Any

import requests

from app.utils import get_logger

logger = get_logger(__name__)

QUARK_SHARE_API = "https://drive-pc.quark.cn"
QUARK_FILE_API = "https://drive-h.quark.cn"
SHARE_RE = re.compile(r"https?://pan\.quark\.cn/s/([0-9A-Za-z]+)")
SHARE_URL_RE = re.compile(r"https?://pan\.quark\.cn/s/[0-9A-Za-z]+")
PWD_RE = re.compile(r"(?:提取码|密码|passcode|pwd)[:：=\s]*([0-9A-Za-z]{4})", re.IGNORECASE)
QUARK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
    "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
)
COMMON_PARAMS = {"pr": "ucpro", "fr": "pc", "uc_param_str": "", "sys": "win32",
                 "ve": "2.5.56", "ut": "", "guid": ""}


class QuarkError(Exception):
    """夸克转存/分享失败(message 带语义)。"""


class QuarkAuthError(QuarkError):
    """Cookie 失效(401),需重新复制。"""


def extract_quark_urls(text: str) -> list[str]:
    """从文本提取夸克分享链接(保序去重)。"""
    seen: set[str] = set()
    out = []
    for m in SHARE_URL_RE.finditer(text or ""):
        if m.group(0) not in seen:
            seen.add(m.group(0))
            out.append(m.group(0))
    return out


class QuarkTransfer:
    """夸克转存 + 二次分享最小客户端(同步)。"""

    def __init__(self, cookie: str, timeout: float = 30.0,
                 fid_store: str = "") -> None:
        self.cookie = str(cookie or "").strip()
        self.timeout = timeout
        self._dir_cache: dict[str, str] = {}  # path -> fid(一轮监听多次转存复用,免重复解析)
        self._used_store: set[str] = set()    # 本次从 fid_store 复用的路径(保存失败时自愈回重建)
        # 本地**主动作废**过的路径(墓碑)—— 合并写回时要把它们排除,
        # 否则刚被 invalidate 掉的 fid 会被磁盘上的旧值**又并回来**(见 `_persist_fids`)
        self._dropped: set[str] = set()
        self._fid_store_path = str(fid_store or "")
        self._persisted: dict[str, str] = {}
        if self._fid_store_path and os.path.isfile(self._fid_store_path):
            try:
                with open(self._fid_store_path, encoding="utf-8") as f:
                    self._persisted = {str(k): str(v) for k, v in (json.load(f) or {}).items()}
            except (OSError, ValueError):
                self._persisted = {}

    def _persist_fids(self) -> None:
        """把 path→fid 写回 fid_store。

        ⚠️⚠️ **必须先读回磁盘再合并,不能直接 dump 内存那份**(2026-10-08 修)。

        原来是把 `self._persisted`(构造时载入、之后只增不减的那一份)**整份覆盖写**。
        后果是一个典型的**丢更新**:盘上明明有一份正确的映射,某个进程只要在
        "别人写之后"落一次盘,就会用自己那份**旧快照**把它抹掉 ⇒ 下次 `_ensure_dir`
        缓存缺失 ⇒ 走梯度候选**新建一个 `redian监听_MMDD`**。

        代价是实打实的:实测该账号盘上有 **19 个 `redian监听_*` 目录**
        (0913/0915/0916/0918/0920/0926~0929/1001/1004/1007,约每 3 天一个),
        而查重是**按目录**做的(在目标目录里找同名同大小)——
        换了目录就等于**一个都找不到** ⇒ 同一个资源在新目录里**又存一份**。
        实例:`高性价比人生指南-HowToLiveBetter-现代-338页.pdf` 同时躺在
        `redian监听_0929` 与 `redian监听_1004` 里各一份。

        ⇒ 现在:读磁盘 → 合并(我们的值优先)→ 排除本地刚作废的路径 → 原子替换。
        """
        if not self._fid_store_path:
            return
        try:
            os.makedirs(os.path.dirname(self._fid_store_path) or ".", exist_ok=True)
            merged: dict[str, str] = {}
            try:
                if os.path.isfile(self._fid_store_path):
                    with open(self._fid_store_path, encoding="utf-8") as f:
                        merged = {str(k): str(v) for k, v in (json.load(f) or {}).items()}
            except (OSError, ValueError):
                merged = {}
            merged.update(self._persisted)
            for gone in self._dropped:      # ⚠️ 本地作废的**不许**被磁盘上的旧值并回来
                merged.pop(gone, None)
            self._persisted = merged
            tmp = self._fid_store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False)
            os.replace(tmp, self._fid_store_path)
        except OSError:
            logger.warning("夸克 fid 缓存写入失败: %s", self._fid_store_path)

    def invalidate_dir(self, path: str) -> None:
        """清除目录缓存(目录被删/fid 失效时),下次 _ensure_dir 会重新创建。

        ⚠️ 同时记进 `_dropped`(墓碑):`_persist_fids` 会合并磁盘上的旧值,
        不排除的话**这个刚被作废的 fid 会被原样并回来**,作废等于没作。
        """
        path = path.strip("/") or "/来自监听"
        self._dir_cache.pop(path, None)
        self._used_store.discard(path)
        self._persisted.pop(path, None)
        self._dropped.add(path)
        self._persist_fids()

    # ---- HTTP ----
    def _headers(self) -> dict:
        if not self.cookie:
            raise QuarkAuthError("夸克网盘缺少 Cookie(浏览器登录 pan.quark.cn 后复制)")
        return {"User-Agent": QUARK_UA, "Accept": "application/json, text/plain, */*",
                "Cookie": self.cookie, "Origin": "https://pan.quark.cn",
                "Referer": "https://pan.quark.cn/", "Content-Type": "application/json"}

    @staticmethod
    def _params(params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(COMMON_PARAMS)
        if params:
            merged.update(params)
        return merged

    @staticmethod
    def _raise(data: Mapping[str, Any], fallback: str) -> None:
        code, status = data.get("code"), data.get("status")
        if status == 401 or code == 401:
            raise QuarkAuthError("夸克 Cookie 已失效或未登录,请重新复制 pan.quark.cn 的 Cookie")
        if code not in (None, 0, 200) or status not in (None, 0, 200):
            message = str(data.get("message") or data.get("msg") or data.get("error") or fallback)
            if "capacity limit" in message.lower():
                raise QuarkError("夸克网盘容量不足,请清理空间或更换账号")
            raise QuarkError(f"夸克接口失败: {message}")

    def _request(self, method: str, path: str, *, api: str = QUARK_SHARE_API,
                 params: Mapping[str, Any] | None = None,
                 json: Mapping[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        # 66010(query fail)是间歇性限流,自动重试 2 次
        last_err = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)  # 2s / 4s
            try:
                resp = requests.request(method, api + path, params=self._params(params),
                                        json=json, timeout=timeout or self.timeout, headers=self._headers())
            except requests.RequestException as exc:
                # 连接/超时等瞬时网络故障纳入同一重试:耗尽后归一为 QuarkError 抛出。
                # 否则它会绕过 per-URL 的 except QuarkError 直接掀翻整轮监听(连带百度转存
                # 与共振推送全被跳过),与百度侧宽兜底不对称。只取类型名,比照 urllib3 防 URL 回显。
                last_err = QuarkError(f"夸克网络异常:{type(exc).__name__}")
                continue
            try:
                data = resp.json()
            except ValueError:
                last_err = QuarkError(f"夸克接口返回非 JSON: {resp.text[:300]}")
                continue
            code = data.get("code")
            if code in (None, 0, 200):
                return data
            msg = str(data.get("message") or data.get("msg") or resp.text[:200])
            if code == 401 or data.get("status") == 401:
                raise QuarkAuthError("夸克 Cookie 已失效,请重新复制 pan.quark.cn 的 Cookie")
            if "capacity limit" in msg.lower():
                raise QuarkError("夸克网盘容量不足,请清理空间或更换账号")
            # 66010 = 间歇性 query fail,重试
            last_err = QuarkError(f"夸克接口失败({code}): {msg}")
        raise last_err or QuarkError("夸克接口连续失败")

    # ---- 业务 ----
    def _parse_share(self, url: str):
        m = SHARE_RE.search(url or "")
        if not m:
            raise QuarkError(f"无法解析夸克分享链接:{url}")
        pm = PWD_RE.search(url)
        return m.group(1), (pm.group(1) if pm else "")

    def _get_stoken(self, share_id: str, password: str) -> str:
        data = self._request("POST", "/1/clouddrive/share/sharepage/token",
                             json={"pwd_id": share_id, "passcode": password})
        stoken = data.get("data", {}).get("stoken")
        if not stoken:
            raise QuarkError("夸克分享 token 获取失败,链接可能失效或提取码错误")
        return str(stoken)

    def _list_share_files(self, share_id: str, stoken: str) -> list[dict]:
        items: list[dict] = []
        for page in range(1, 51):
            data = self._request("GET", "/1/clouddrive/share/sharepage/detail",
                                 params={"pwd_id": share_id, "stoken": stoken, "pdir_fid": "0",
                                         "force": "0", "_page": page, "_size": 200})
            page_items = list(data.get("data", {}).get("list", []) or [])
            items.extend(x for x in page_items if x.get("fid"))
            if len(page_items) < 200:
                break
        return items

    def _list_dir(self, fid: str = "0") -> list[dict]:
        entries = []
        for page in range(1, 51):
            data = self._request("GET", "/1/clouddrive/file/sort", api=QUARK_FILE_API,
                                 params={"pdir_fid": fid, "_page": page, "_size": 200,
                                         "_sort": "file_name:asc"})
            items = list(data.get("data", {}).get("list", []) or [])
            entries.extend(x for x in items if x.get("fid"))
            if len(items) < 200:
                break
        return entries

    def list_dir(self, fid: str = "0") -> list[dict]:
        """列目录的**公开门面**(2026-10-06)。夸克口令那条链要"在 App 把文件存进我盘后把它找出来",
        而原方法叫 `_list_dir`(私有)—— 跨模块调私有方法等于把两边实现焊死。"""
        return self._list_dir(fid)

    def search_files(self, keyword: str, size: int = 20) -> list[dict]:
        """按名字搜我盘里的文件(`/1/clouddrive/file/search`)。

        ⚠️ **为什么必须用它、而不是 `list_dir`**(2026-10-06 实测):找「来自：分享」这种目录时,
        `list_dir` 返回的**正好是 10000 项** —— 也就是 50 页 × 200 的**硬上限**,
        而该目录**排在第 1 万条之外**,全量遍历**根本够不着**(我当时据此误判成"文件没保存成功")。
        搜索接口**一次就命中**。
        """
        data = self._request("GET", "/1/clouddrive/file/search", api=QUARK_FILE_API,
                             params={"q": keyword, "_page": 1, "_size": size})
        return list((data.get("data") or {}).get("list") or [])

    def list_recent(self, fid: str = "0", size: int = 30) -> list[dict]:
        """按**更新时间倒序**取一页 —— 回答"**刚存进来的文件在哪**"(2026-10-06)。

        ⚠️ 别用 `list_dir`(它按文件名分页遍历**整个目录**):在大盘上(实测该账号上万文件)
        既慢、又**可能翻不到目标**(`_list_dir` 最多 50 页 × 200 = 1 万条就停),
        而"刚保存的"**一定在最新一页**。
        """
        data = self._request("GET", "/1/clouddrive/file/sort", api=QUARK_FILE_API,
                             params={"pdir_fid": fid, "_page": 1, "_size": size,
                                     "_sort": "updated_at:desc"})
        return list((data.get("data") or {}).get("list") or [])

    def share_fids(self, fid_list: list, title: str = "监听转存",
                   password: str = "", expire_days: int = 0) -> dict:
        """给**已经在盘里**的文件建我方分享链 → `{"share_url","password","share_id"}`。

        ⚠️ **为什么抽出来**(2026-10-06):这段原来**内联在 `transfer_and_share` 里**,
        于是"文件已经在盘里、只想建条链"的场景(夸克口令链正是这种)没法复用,
        只能把 `/1/clouddrive/share` 那套协议**再抄一遍** —— 同一协议两处实现,迟早飘。
        现在 `transfer_and_share` 也改成调它。
        """
        ids = [str(x) for x in (fid_list or []) if x]
        if not ids:
            raise QuarkError("无可分享文件(fid_list 为空)")
        expired_type = 1 if expire_days <= 0 else 2
        share_payload: dict[str, Any] = {"fid_list": ids, "title": (title or "监听转存")[:100],
                                         "url_type": 1, "expired_type": expired_type}
        if expire_days > 0:
            share_payload["expire_time"] = expire_days * 86400
        if password:
            share_payload["passcode"] = password
        share_resp = self._request("POST", "/1/clouddrive/share", json=share_payload)
        share_id = self._find_share_id(share_resp)
        if not share_id:
            tid = str(self._find_first(share_resp, {"task_id", "taskId"}) or "") \
                if isinstance(share_resp, Mapping) else ""
            if not tid:
                raise QuarkError(f"夸克创建分享失败,未返回 share_id/task_id: {str(share_resp)[:200]}")
            share_id = self._find_share_id(self._wait_share_task(tid))
            if not share_id:
                raise QuarkError("夸克创建分享完成但未返回 share_id")
        pwd_resp = self._request("POST", "/1/clouddrive/share/password",
                                 json={"share_id": share_id})
        sd = pwd_resp.get("data", {}) or {}
        um = SHARE_URL_RE.search(str(sd.get("share_url") or "")) if sd.get("share_url") else None
        return {"share_url": um.group(0) if um else f"https://pan.quark.cn/s/{share_id}",
                "password": str(sd.get("passcode") or password or ""), "share_id": share_id}

    def _mk_dir(self, parent_fid: str, name: str) -> str:
        """创建目录;成功返回 fid,撞名/幽灵占用(23008)抛 QuarkError。"""
        created = self._request("POST", "/1/clouddrive/file", api=QUARK_FILE_API,
                                json={"pdir_fid": parent_fid, "file_name": name,
                                      "dir_path": "", "dir_init_lock": False})
        fid = created.get("data", {}).get("fid")
        if not fid:
            raise QuarkError(f"夸克创建目录未返回 fid: {name}")
        return str(fid)

    def set_dir_fid(self, path: str, fid: str) -> None:
        """预设目录 fid(跳过解析,大盘必备)。调用方通过 API/配置获取 fid 后注入。"""
        path = path.strip("/") or "/"
        parts = [x.strip() for x in path.split("/") if x.strip()]
        walked = ""
        for part in parts:
            walked += "/" + part
        self._dir_cache[walked.rstrip("/")] = fid

    def _ensure_dir(self, path: str) -> str:
        """确保目录存在并返回末级 fid。

        大盘实测:根目录 10000+ 文件时**按名分页遍历**一次要几十个慢请求
        (一次补偿转存拖到半小时),故**不做分页重扫** —— 存在性判定走
        「搜索接口一次命中」(`_adopt_existing_dir`)或「创建接口的成功/23008」,
        后者撞名时沿 "原名→_MMDD→_MMDD_2→_MMDD_3" 候选梯继续建。
        ⚠️ **但"新建"必须是最后手段**(2026-10-08 订正):旧 docstring 把
        "完全不做重扫"当特性,连**查找**都不做,于是缓存一丢就新建一个 `_MMDD` 目录、
        而查重按目录做 ⇒ 同一资源重复占用(实测 19 个 `redian监听_*`),
        详见 `_adopt_existing_dir`。23008 还包含"幽灵占用"(目录实际不存在却报同名,
        旧转存任务残留,线上案例 "redian监听")—— 搜索会先证伪它,证伪不了才建新名。
        解析出的 fid 持久化到 fid_store,下轮直接复用,避免幽灵场景每轮新堆目录;
        fid 失效(目录被删)时保存会报错,调用方经 invalidate_dir() 清除后自动重建。
        """
        path = path.strip("/") or "/来自监听"
        if path in self._dir_cache:
            return self._dir_cache[path]
        cached = self._persisted.get(path, "")
        if cached:
            self._dir_cache[path] = cached
            self._used_store.add(path)
            return cached
        parts = [x.strip() for x in path.split("/") if x.strip()]
        parent = "0"
        walked = ""
        for part in parts:
            walked += "/" + part
            if walked in self._dir_cache:
                parent = self._dir_cache[walked]
                continue
            parent = self._dir_cache[walked] = self._create_with_fallback(parent, part)
        self._dir_cache[path] = parent
        self._persisted[path] = parent
        self._persist_fids()
        return parent

    def _adopt_existing_dir(self, parent_fid: str, name: str) -> str:
        """按名字找回**已经在用**的那个目录,而不是新建一个。找不到返回 `""`。

        ★★ 这是 2026-10-08 那次「夸克反复保存同一个资源、白占空间」的**正解**。

        ## 病根
        `_ensure_dir` 原来**只创建、从不查找**(它的 docstring 就把"完全不做重扫"当成
        特性写的,理由是"大盘上按名分页重扫要几十个慢请求")。于是缓存一旦缺失:
        `redian监听` 撞 `23008` → 梯度候选 —— **新建 `redian监听_MMDD`** → 新目录是空的。

        而**查重是按目标目录做的**(在 `target_fid` 里找同名同大小),所以"家"一换,
        之前的资源一个都认不出来 ⇒ **每个资源又存一份**。实测代价:
        盘上 **19 个 `redian监听_*`**(约每 3 天新增一个),同一个
        `高性价比人生指南-HowToLiveBetter-现代-338页.pdf` 在 `_0929` 和 `_1004` 里各一份。

        ## 为什么"查找"这次是便宜的
        用**搜索接口**(`/1/clouddrive/file/search`),一次请求就有结果 ——
        这正是本文件 `search_files` 的 docstring 记的那件事:
        `list_dir` 在大盘上要分页扫、还可能翻不到,而**搜索一次就命中**。
        ⇒ 只挂在"缓存缺失"这条冷路径上(热路径仍然零额外请求)。

        ## 为什么取"最近更新的那个"、不取同名那个
        真实情况是同名目录与一堆 `_MMDD` 兄弟**并存**(实测 `redian监听` 本身只剩 4 项、
        停在 09-13,而最近在用的是 `redian监听_1007`)。"家"的定义是
        **最近在往里写东西的那个**,所以按 `updated_at` 倒序取第一个。
        取同名那个会把后续资源全搬进一个早已不用的旧目录 —— 那等于**主动制造**下一次重复。
        """
        if not name:
            return ""
        try:
            hits = self.search_files(name, size=50)
        except Exception as exc:  # noqa: BLE001 - 搜不到就当不存在,退回"新建"(与旧行为一致)
            logger.warning("夸克按名找回目录失败(%s),将按旧逻辑新建:%s",
                           name, str(exc)[:100])
            return ""
        cands = [h for h in hits
                 if h.get("dir")
                 and str(h.get("pdir_fid") or "") == str(parent_fid)
                 and (str(h.get("file_name") or "") == name
                      or str(h.get("file_name") or "").startswith(f"{name}_"))]
        if not cands:
            return ""
        cands.sort(key=lambda h: int(h.get("updated_at") or 0), reverse=True)
        best = cands[0]
        logger.warning(
            "夸克目录「%s」:缓存里没有,但盘上已有 %d 个同名/带日期兄弟 —— "
            "**沿用最近在用的「%s」,不新建**(先前每丢一次缓存就新建一个 `_MMDD`,"
            "而查重按目录做 ⇒ 同一资源在新目录里又存一份,这就是空间被重复占用的根因)",
            name, len(cands), best.get("file_name"))
        return str(best["fid"])

    def _create_with_fallback(self, parent_fid: str, name: str) -> str:
        """先**找回已有目录**;确实没有才逐个候选名创建(全部撞名才抛错)。"""
        adopted = self._adopt_existing_dir(parent_fid, name)
        if adopted:
            return adopted
        last = ""
        for cand in (name, f"{name}_{datetime.now():%m%d}",
                     f"{name}_{datetime.now():%m%d}_2", f"{name}_{datetime.now():%m%d}_3"):
            try:
                return self._mk_dir(parent_fid, cand)
            except QuarkAuthError:
                raise  # Cookie 失效不是撞名:直接上抛让调用方告警重登,别再空转候选名
            except QuarkError as exc:
                last = str(exc)
        raise QuarkError(f"夸克目录创建失败(含后缀候选均撞名): {name}; 最后错误: {last}")

    def _wait_task_fids(self, task_id: str) -> list[str]:
        if not task_id:
            return []
        for retry_index in range(10):
            if retry_index:
                time.sleep(0.5)
            resp = self._request("GET", "/1/clouddrive/task",
                                 params={"task_id": task_id, "retry_index": retry_index})
            data = resp.get("data", {})
            fids = (data.get("save_as", {}) or {}).get("save_as_top_fids", []) or []
            if fids:
                return [str(f) for f in fids]
            status = data.get("status") or data.get("task_status")
            if status in (-1, 3, 4, "fail", "failed", "error"):
                raise QuarkError(f"夸克保存任务失败: {resp.get('message') or str(resp)[:200]}")
        logger.warning("夸克保存任务未返回文件ID task_id=%s", task_id)
        return []

    def _wait_share_task(self, task_id: str) -> dict:
        for retry_index in range(12):
            if retry_index:
                time.sleep(1)
            resp = self._request("GET", "/1/clouddrive/task",
                                 params={"task_id": task_id, "retry_index": retry_index})
            if self._find_share_id(resp):
                return resp
            status = (resp.get("data", {}) or {}).get("status") or (resp.get("data", {}) or {}).get("task_status")
            if status in (-1, 3, 4, "fail", "failed", "error"):
                raise QuarkError(f"夸克创建分享任务失败: {resp.get('message') or str(resp)[:200]}")
        raise QuarkError(f"夸克创建分享任务超时 task_id={task_id}")

    @staticmethod
    def _find_share_id(data: Any) -> str:
        if isinstance(data, Mapping):
            for k in ("share_id", "sid"):
                if data.get(k) not in (None, ""):
                    return str(data[k])
            for v in data.values():
                found = QuarkTransfer._find_share_id(v)
                if found:
                    return found
        if isinstance(data, list):
            for v in data:
                found = QuarkTransfer._find_share_id(v)
                if found:
                    return found
        return ""

    def delete_files(self, fids: list, *, to_recycle: bool = True) -> dict:
        """把若干 fid 删进**回收站**(默认)或彻底删除。返回 `{"ok", "message", "count"}`。

        ⚠️⚠️ **这是破坏性能力,调用方必须先确认**。本方法**不抛业务异常**之外的错,
        但它删的是**用户网盘里的真东西** —— 所以:
          · `to_recycle=True`(默认)走 `action_type=2` = **进回收站**(可恢复);
          · 只有明确要"彻底删"时才传 `False`,那时**不可恢复**。

        ⚠️ **端点是按公开协议写的,没在真数据上试过** —— 上线前请先用
        `scripts/quark_delete_probe.py`(**它建一个临时文件夹再删它**)验证,
        别拿真资源当小白鼠。
        """
        fids = [str(f) for f in (fids or []) if str(f).strip()]
        if not fids:
            return {"ok": False, "message": "没有要删的 fid", "count": 0}
        try:
            data = self._request("POST", "/1/clouddrive/file/delete", api=QUARK_FILE_API,
                                 json={"action_type": 2 if to_recycle else 1,
                                       "filelist": fids, "exclude_fids": []})
        except (QuarkAuthError, QuarkError) as exc:
            return {"ok": False, "message": str(exc)[:160], "count": 0}
        return {"ok": True, "message": "已" + ("移入回收站" if to_recycle else "彻底删除"),
                "count": len(fids), "raw": str(data.get("data"))[:120]}

    def keepalive(self) -> bool:
        """每日保活:轻量列根目录,让服务端滚动延长 __puus 有效期(防闲置过期)。

        认证失败抛 QuarkAuthError,由调用方告警;成功返回 True。
        """
        self._list_dir("0")
        return True

    def transfer_and_share(self, share_url: str, save_dir: str = "/来自监听",
                           password: str = "", expire_days: int = 0) -> dict:
        """转存分享到自己网盘并创建二次分享,返回 {share_url, password, files}。"""
        share_id, pwd = self._parse_share(share_url)
        stoken = self._get_stoken(share_id, pwd)
        files = self._list_share_files(share_id, stoken)
        if not files:
            raise QuarkError("分享内无可转存文件")

        target_fid = self._ensure_dir(save_dir)
        # 文件级去重:盘商"同一资源换条分享链再发"是常态,wechat_monitor 的链接级复用
        # (批内/历史两层)管不住这种。同名+同大小视为同一文件已存过:跳过保存、直接把
        # 已有文件并入分享——避免同资源重复占空间;大小任一侧缺失不参与匹配
        # (宁多存一份,不冒领错文件)。
        by_name: dict[str, list[dict]] = {}
        for e in self._list_dir(target_fid):
            if e.get("file_name"):
                by_name.setdefault(e["file_name"], []).append(e)
        fresh: list[dict] = []
        matched_ids: list[str] = []
        for f in files:
            cands = [e for e in by_name.get(f.get("file_name") or "", [])
                     if f.get("size") is not None and e.get("size") == f.get("size")]
            if cands:
                matched_ids.append(str(cands[0]["fid"]))
            else:
                fresh.append(f)
        if matched_ids and not fresh:
            logger.info("夸克文件级复用:分享内文件均已存在,免保存直接分享(%d 个)",
                        len(matched_ids))
        if fresh:
            payload = {"fid_list": [f["fid"] for f in fresh],
                       "fid_token_list": [f.get("share_fid_token", "") for f in fresh],
                       "to_pdir_fid": target_fid, "pwd_id": share_id, "stoken": stoken,
                       "pdir_fid": "0", "scene": "link"}
            norm_dir = save_dir.strip("/") or "/来自监听"
            try:
                data = self._request("POST", "/1/clouddrive/share/sharepage/save",
                                     json=payload, timeout=60.0)
            except QuarkError:
                # 复用持久化 fid 时目录可能已被用户删/移动:清缓存重建后重试一次
                if norm_dir not in self._used_store:
                    raise
                logger.warning("夸克缓存 fid 已失效(%s),重建目录后重试", norm_dir)
                self.invalidate_dir(norm_dir)
                payload["to_pdir_fid"] = self._ensure_dir(save_dir)
                data = self._request("POST", "/1/clouddrive/share/sharepage/save",
                                     json=payload, timeout=60.0)
            task_data = data.get("data", {})
            new_ids = (task_data.get("save_as", {}) or {}).get("save_as_top_fids", []) or []
            task_id = str(task_data.get("task_id") or task_data.get("taskId") or "")
            if not new_ids:
                new_ids = self._wait_task_fids(task_id)
            if not new_ids:
                names = [f.get("file_name") for f in fresh if f.get("file_name")]
                new_ids = [e["fid"] for e in self._list_dir(target_fid) if e.get("file_name") in names]
            if not new_ids and not matched_ids:
                raise QuarkError(f"夸克保存任务未返回新文件 ID task_id={task_id or '空'}")
        else:
            new_ids = []
        # 分享清单 = 新存文件 + 已存在的同名同大小文件(部分命中时资源完整)
        share_ids = [str(x) for x in (list(new_ids) + matched_ids)]
        if not share_ids:
            raise QuarkError("无可分享文件(保存未返回且目录无匹配)")

        _share = self.share_fids(share_ids, title="监听转存", password=password,
                                 expire_days=expire_days)
        new_url, out_password = _share["share_url"], _share["password"]
        logger.info("夸克转存+分享完成: %s 个文件(含复用 %s 个)→ %s",
                    len(share_ids), len(matched_ids), new_url)
        return {"share_url": new_url, "password": out_password, "files": len(share_ids)}

    @staticmethod
    def _find_first(data: Any, keys: set[str]) -> Any:
        if isinstance(data, Mapping):
            for k in keys:
                if data.get(k) not in (None, ""):
                    return data[k]
            for v in data.values():
                found = QuarkTransfer._find_first(v, keys)
                if found not in (None, ""):
                    return found
        if isinstance(data, list):
            for v in data:
                found = QuarkTransfer._find_first(v, keys)
                if found not in (None, ""):
                    return found
        return None

