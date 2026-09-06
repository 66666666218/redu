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

import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

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

    def __init__(self, cookie: str, timeout: float = 30.0) -> None:
        self.cookie = str(cookie or "").strip()
        self.timeout = timeout
        self._dir_cache: dict[str, str] = {}  # path -> fid(一轮监听多次转存复用,免重复扫描)

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
        resp = requests.request(method, api + path, params=self._params(params),
                                json=json, timeout=timeout or self.timeout, headers=self._headers())
        try:
            data = resp.json()
        except ValueError as e:
            raise QuarkError(f"夸克接口返回非 JSON: {resp.text[:300]}") from e
        if resp.status_code >= 400:
            self._raise(data, resp.text[:300])
            raise QuarkError(f"夸克接口 HTTP {resp.status_code}: {resp.text[:300]}")
        self._raise(data, resp.text[:300])
        return data

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

    def _create_dir(self, parent_fid: str, name: str) -> str:
        created = self._request("POST", "/1/clouddrive/file", api=QUARK_FILE_API,
                                json={"pdir_fid": parent_fid, "file_name": name,
                                      "dir_path": "", "dir_init_lock": False})
        fid = created.get("data", {}).get("fid")
        if not fid:
            raise QuarkError(f"夸克创建目录失败: {name}")
        return str(fid)

    def _ensure_dir(self, path: str) -> str:
        """确保目录存在并返回末级 fid。

        大盘优化:① 实例级缓存(一轮监听多次转存只解析一次);② 查找改"逐页早停"——
        根目录上万文件时全量扫要 50 个请求,目标目录名往往前几页就能命中。
        """
        path = path.strip("/") or "/来自监听"
        if path in self._dir_cache:
            return self._dir_cache[path]
        parts = [x.strip() for x in path.split("/") if x.strip()]
        parent = "0"
        walked = ""
        for part in parts:
            walked += "/" + part
            if walked in self._dir_cache:
                parent = self._dir_cache[walked]
                continue
            existing_fid = ""
            for page in range(1, 51):  # 逐页找,命中即停
                data = self._request("GET", "/1/clouddrive/file/sort", api=QUARK_FILE_API,
                                     params={"pdir_fid": parent, "_page": page, "_size": 200,
                                             "_sort": "file_name:asc"})
                items = list(data.get("data", {}).get("list", []) or [])
                hit = next((x for x in items if x.get("dir") and x.get("file_name") == part), None)
                if hit:
                    existing_fid = str(hit["fid"])
                    break
                if len(items) < 200:
                    break  # 翻完了也没有
            parent = existing_fid or self._create_dir(parent, part)
            self._dir_cache[walked] = parent
        self._dir_cache[path] = parent
        return parent

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
        logger.warning("夸克保存任务未返回文件ID task_id={}", task_id)
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

    def transfer_and_share(self, share_url: str, save_dir: str = "/来自监听",
                           password: str = "", expire_days: int = 0) -> dict:
        """转存分享到自己网盘并创建二次分享,返回 {share_url, password, files}。"""
        share_id, pwd = self._parse_share(share_url)
        stoken = self._get_stoken(share_id, pwd)
        files = self._list_share_files(share_id, stoken)
        if not files:
            raise QuarkError("分享内无可转存文件")

        target_fid = self._ensure_dir(save_dir)
        payload = {"fid_list": [f["fid"] for f in files],
                   "fid_token_list": [f.get("share_fid_token", "") for f in files],
                   "to_pdir_fid": target_fid, "pwd_id": share_id, "stoken": stoken,
                   "pdir_fid": "0", "scene": "link"}
        data = self._request("POST", "/1/clouddrive/share/sharepage/save",
                             json=payload, timeout=60.0)
        task_data = data.get("data", {})
        new_ids = (task_data.get("save_as", {}) or {}).get("save_as_top_fids", []) or []
        task_id = str(task_data.get("task_id") or task_data.get("taskId") or "")
        if not new_ids:
            new_ids = self._wait_task_fids(task_id)
        if not new_ids:
            names = [f.get("file_name") for f in files if f.get("file_name")]
            new_ids = [e["fid"] for e in self._list_dir(target_fid) if e.get("file_name") in names]
        if not new_ids:
            raise QuarkError(f"夸克保存任务未返回新文件 ID task_id={task_id or '空'}")

        expired_type = 1 if expire_days <= 0 else 2
        share_payload: dict[str, Any] = {"fid_list": new_ids, "title": "监听转存",
                                         "url_type": 1, "expired_type": expired_type}
        if expire_days > 0:
            share_payload["expire_time"] = expire_days * 86400
        if password:
            share_payload["passcode"] = password
        share_resp = self._request("POST", "/1/clouddrive/share", json=share_payload)
        new_share_id = self._find_share_id(share_resp)
        if not new_share_id:
            task_id = ""
            if isinstance(share_resp, Mapping):
                task_id = str(self._find_first(share_resp, {"task_id", "taskId"}) or "")
            if not task_id:
                raise QuarkError(f"夸克创建分享失败,未返回 share_id/task_id: {str(share_resp)[:200]}")
            new_share_id = self._find_share_id(self._wait_share_task(task_id))
            if not new_share_id:
                raise QuarkError("夸克创建分享完成但未返回 share_id")
        pwd_resp = self._request("POST", "/1/clouddrive/share/password",
                                 json={"share_id": new_share_id})
        share_data = pwd_resp.get("data", {}) or {}
        url_m = SHARE_URL_RE.search(str(share_data.get("share_url") or "")) \
            if share_data.get("share_url") else None
        new_url = url_m.group(0) if url_m else f"https://pan.quark.cn/s/{new_share_id}"
        out_password = str(share_data.get("passcode") or password or "")
        logger.info("夸克转存+分享完成: {} 个文件 → {} ({} 个文件)", len(new_ids), new_url, len(new_ids))
        return {"share_url": new_url, "password": out_password, "files": len(new_ids)}

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


def quote_share_token(token: str) -> str:
    return quote(token, safe="~")
