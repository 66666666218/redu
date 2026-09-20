"""百度网盘转存+换链客户端(协议经 2026-09-19 真实链接实测打通)。

协议要点(与 wangpan 项目一致,页面模板已适配新版逐字段赋值):
- 凭证: Cookie 必须含 BDUSS(+STOKEN 更稳)
- 双 UA: 转存流程用旧版 Mac Chrome 77 浏览器 UA;
        创建分享用 NetdiskUA 调 /share/pset(path_list,无需 bdstoken)
- 提取码: POST /share/verify(bdstoken=null)
- 元数据: GET /s/1{surl} 页面内逐字段正则(share_uk/shareid/bdstoken/file_list)
- 转存:   POST /share/transfer?shareid&from&bdstoken  data={fsidlist, path}
- 分享:   POST /share/pset  data={path_list, schannel=4, period=7, pwd, share_type=9}
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import requests

logger = __import__("app.utils", fromlist=["get_logger"]).get_logger(__name__)

PAN_API = "https://pan.baidu.com"
SURL_RE = re.compile(r"https?://pan\.baidu\.com/s/1([0-9A-Za-z_\-]+)")
PWD_RE = re.compile(r"(?:提取码|访问码|密码)[：:\s]*([0-9A-Za-z]{4})|[?&]pwd=([0-9A-Za-z]{4})", re.IGNORECASE)

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/77.0.3865.75 Safari/537.36")
_NETDISK_UA = "netdisk;P2SP;2.2.51.6;netdisk;11.0.0.0;PC;PC-Windows;6.2.9200;WindowsBaiduYunGuanJia"


class BaiduPanError(Exception):
    """百度网盘转存/分享失败(message 带语义)。"""


class BaiduPanAuthError(BaiduPanError):
    """Cookie 失效(BDUSS 过期),需重新复制。"""


def extract_baidu_urls(text: str) -> list[str]:
    """从文本提取百度盘分享链接(保序去重)。"""
    seen: set[str] = set()
    out = []
    for m in SURL_RE.finditer(text or ""):
        u = m.group(0)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_pwd(text: str, url: str) -> str:
    """提取码: 优先 URL ?pwd= 参数,否则文本内 提取码/密码 标记。"""
    m = re.search(r"[?&]pwd=([0-9A-Za-z]{4})", url or "")
    if m:
        return m.group(1)
    m = PWD_RE.search(text or "")
    if m:
        return m.group(1) or m.group(2) or ""
    return ""


@dataclass
class BaiduPanClient:
    cookie: str
    timeout: float = 30.0
    _session: requests.Session = field(default_factory=requests.Session, repr=False)
    _ready: bool = False

    def __post_init__(self) -> None:
        self._session.headers.update({"Accept-Encoding": "identity"})
        for kv in (self.cookie or "").split(";"):
            name, _, val = kv.strip().partition("=")
            if name:
                self._session.cookies.set(name.strip(), val.strip(), domain=".baidu.com", path="/")

    def _browser(self) -> requests.Session:
        self._session.headers["User-Agent"] = _BROWSER_UA
        return self._session

    # ---- 协议步骤 ----
    def _verify(self, surl: str, password: str) -> None:
        """提取码验证;无密码分享跳过。失败抛 BaiduPanError。"""
        if not password:
            return
        r = self._browser().post(
            f"{PAN_API}/share/verify",
            params={"surl": surl, "t": str(int(time.time() * 1000)), "channel": "chunlei",
                    "web": "1", "bdstoken": "null", "clienttype": "0"},
            data={"pwd": password, "vcode": "", "vcode_str": ""},
            headers={"Referer": f"{PAN_API}/share/init?surl={surl}"},
            timeout=self.timeout)
        errno = r.json().get("errno", -1)
        if errno != 0:
            raise BaiduPanError(f"提取码验证失败(errno={errno})")

    def _share_meta(self, surl: str) -> tuple[str, str, str, list[dict]]:
        """分享页解析: 返回 (uk, shareid, bdstoken, files)。页面为逐字段赋值模板。"""
        r = self._browser().get(f"{PAN_API}/s/1{surl}", timeout=self.timeout)
        html = r.text

        def grab(key: str) -> str:
            m = (re.search(rf'"{key}"\s*:\s*"([^"]*)"', html)
                 or re.search(rf'"{key}"\s*:\s*(\d+)', html))
            return m.group(1) if m else ""

        uk = grab("share_uk") or grab("uk")
        shareid = grab("shareid")
        bdstoken = grab("bdstoken")
        m = re.search(r'"file_list":\s*(\[[^\]]*\])', html, re.S)
        files = json.loads(m.group(1)) if m else []
        if not (uk and shareid and files):
            # -12/-2 类: 链接失效或需要密码而未提供
            raise BaiduPanError(f"分享页解析失败(链接失效或需提取码): uk={bool(uk)} files={len(files)}")
        return uk, shareid, bdstoken, files

    def transfer_and_share(self, share_url: str, password: str = "",
                           target_dir: str = "/redian百度转存",
                           share_pwd: str = "8888") -> dict:
        """转存分享到自己网盘并创建新分享。返回 {share_url, password, files}。"""
        surl = share_url.split("/s/1")[1]
        self._verify(surl, password)
        uk, shareid, bdstoken, files = self._share_meta(surl)
        if not files:
            raise BaiduPanError("分享内无文件")
        fs_ids = [int(f["fs_id"]) for f in files]
        names = [f.get("server_filename", "") for f in files]

        S = self._browser()
        r = S.post(f"{PAN_API}/share/transfer",
                   params={"shareid": shareid, "from": uk, "bdstoken": bdstoken,
                           "channel": "chunlei", "clienttype": "0", "web": "1"},
                   data={"fsidlist": json.dumps(fs_ids), "path": target_dir},
                   headers={"X-Requested-With": "XMLHttpRequest",
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                            "Origin": PAN_API, "Referer": share_url},
                   timeout=60)
        t = r.json()
        if t.get("info") and isinstance(t["info"], list) and t["info"][0].get("errno"):
            t["errno"] = t["info"][0]["errno"]
        errno = t.get("errno", -1)
        if errno not in (0, 4):  # 4=文件已存在,复用
            if errno in (105, -70):
                raise BaiduPanError(f"转存被限制(errno={errno}),稍后重试")
            raise BaiduPanError(f"转存失败(errno={errno})")
        paths = [f"{target_dir.rstrip('/')}/{n}" for n in names if n]

        # pset 分享(NetdiskUA,无需 bdstoken);转存后立刻分享偶发未就绪,重试 2 次
        last = ""
        for attempt in range(3):
            ck_hdr = "; ".join(f"{c.name}={c.value}" for c in S.cookies)
            r4 = requests.post(f"{PAN_API}/share/pset",
                               data={"path_list": json.dumps(paths), "schannel": "4",
                                     "channel_list": "[]", "period": "7",
                                     "pwd": share_pwd, "share_type": "9"},
                               headers={"User-Agent": _NETDISK_UA,
                                        "Content-Type": "application/x-www-form-urlencoded",
                                        "Cookie": ck_hdr},
                               timeout=self.timeout)
            d4 = r4.json()
            if d4.get("errno") == 0:
                link = d4.get("link") or d4.get("shorturl") or ""
                logger.info("百度转存+分享完成: {} → {}", names[:2], link)
                return {"share_url": link, "password": share_pwd, "files": names}
            last = f"errno={d4.get('errno')} {str(d4.get('show_msg') or '')[:40]}"
            time.sleep(2 * (attempt + 1))
        raise BaiduPanError(f"创建分享失败({last})")

    def keepalive(self) -> bool:
        """登录态检查: 失败抛 BaiduPanAuthError。"""
        r = self._browser().get(f"{PAN_API}/api/loginStatus",
                                params={"clienttype": "0", "web": "1"}, timeout=self.timeout)
        errno = r.json().get("errno", -1)
        if errno != 0:
            raise BaiduPanAuthError("百度网盘 Cookie 已失效,请重新复制")
        return True
