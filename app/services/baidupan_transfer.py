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


# 百度把"**登录态过期**"也塞在 `errno=-6` 里,只靠 `show_msg` 认得出 —— 实测
# `转存失败(errno=-6 账户已过期，重新登陆)`。它**不是死链,是"该去重粘 Cookie"**,
# 必须单独抛 `BaiduPanAuthError`,否则下游只会记一条泛泛的 `failed`,
# **卡着一批能搬的资源却没人知道该干什么**(2026-10-04 实测:一轮 15 条里 10 条是这个)。
_AUTH_HINTS = ("账户已过期", "重新登陆", "重新登录", "登录失效", "未登录", "请先登录", "登录已过期")


def _looks_like_auth_issue(show_msg: str) -> bool:
    text = show_msg or ""
    return any(h in text for h in _AUTH_HINTS)


def _transfer_error(errno, show_msg: str = "") -> str:
    """转存失败的**可读**错误串:必须带上百度的 `show_msg`。

    ⚠️ **为什么不能只报 errno**(2026-10-03 实测):贴吧/知乎发现的影视盘链大批回
    `errno=-6`,而**光看数字分不清是"分享已失效"(终态,别再重试)还是"转存被限制"(等一会就好)**;
    这两者对下游的意义完全相反 —— 前者该标 skipped,后者该留 failed 下轮重来。
    带上 `show_msg` 才判得出来。`105/-70` 是已知的"转存被限制"码,单独措辞。
    """
    why = (show_msg or "").strip()[:60]
    tail = f" {why}" if why else ""       # 没原因就别留个悬空空格
    if errno in (105, -70):
        return f"转存被限制(errno={errno}{tail}),稍后重试"
    return f"转存失败(errno={errno}{tail})"


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

    # ---- 盘内操作(为「转存后把宣传简介放进资源目录」而加,2026-10-10)----

    def get_bdstoken(self) -> str:
        """取**我们自己盘**的 `bdstoken` —— 网页版所有写操作都要它。

        ⚠️ 与 `_share_meta` 里从**分享页**抠出来的那个不是一回事:那个只能用于转存别人的分享。
        盘内操作(复制/移动/删除)必须用**本盘**的。
        """
        r = self._browser().get(f"{PAN_API}/api/gettemplatevariable",
                                params={"fields": '["bdstoken","uk"]'},
                                timeout=self.timeout)
        try:
            j = r.json()
        except ValueError as exc:
            raise BaiduPanError(f"取 bdstoken 失败:HTTP {r.status_code}") from exc
        tok = str(((j.get("result") or {}).get("bdstoken")) or "")
        if not tok or j.get("errno") not in (0, None):
            raise BaiduPanAuthError(f"取 bdstoken 失败(errno={j.get('errno')})—— 多半是会话失效")
        return tok

    def list_dir(self, dir_path: str = "/") -> list[dict]:
        """列目录(分页到 200/页,np 上跟到列完)。条目含 `server_filename`/`path`/`isdir`/`size`。"""
        out: list[dict] = []
        start = 0
        while start < 10000:
            r = self._browser().get(
                f"{PAN_API}/api/list",
                params={"dir": dir_path, "order": "name", "desc": 0, "start": start,
                        "limit": 200, "web": 1, "app_id": 250528, "clienttype": 0,
                        "channel": "chunlei"},
                timeout=self.timeout)
            j = r.json()
            if j.get("errno") != 0:
                raise BaiduPanError(f"列目录失败(errno={j.get('errno')} {dir_path})")
            items = list(j.get("list") or [])
            out.extend(items)
            if len(items) < 200:
                break
            start += 200
        return out

    def copy_into(self, dest_dir: str, src_paths: list[str], *, bdstoken: str = "") -> dict:
        """把**我们盘内**的文件复制进 `dest_dir`。

        ⚠️⚠️ **2026-10-10 实测:这个网页版接口调不通,别在参数上浪费时间。**
        对 `POST /api/filemanager?opera=copy` 试了 **6 种参数形态**(带/不带 `async`、
        `ondup`、`dest` 带尾斜杠、`filelist` 用路径/路径串/`fs_id`、最小参数集、换目标目录),
        **一律 `errno=2, info=[]`**。
        ★ **决定性对照**:用**同一个接口**做 `opera=list`(而 `/api/list` 我们是用得通的)
        —— **同样 `errno=2`** ⇒ 是**这条路由不可用**,不是参数没调对。
        (没有这个对照,就会一直在参数上瞎调 —— 本仓的"找不到对照"教训,见
        `falsification-needs-control-variables`。)

        **想真做的话只有两条路**:
          ① 走**官方 xpan 开放接口** `/rest/2.0/xpan/file?method=filemanager`,要 OAuth `access_token`;
          ② 复用**已验证可用**的 `share/transfer` —— 给我们自己的简介建一条常驻分享,
             每次把它转存进 `target_dir`(会多一条常驻分享链)。
        两种都还没做,`pan_intro_baidu_dir` 默认留空 ⇒ **这条分支默认不生效**。
        """
        if not src_paths:
            return {"copied": 0, "errno": 0}
        tok = bdstoken or self.get_bdstoken()
        r = self._browser().post(
            f"{PAN_API}/api/filemanager",
            params={"opera": "copy", "async": "1", "onnest": "fail", "bdstoken": tok,
                    "clienttype": "0", "app_id": "250528", "web": "1", "channel": "chunlei"},
            data={"filelist": json.dumps(src_paths, ensure_ascii=False), "dest": dest_dir},
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "Origin": PAN_API, "Referer": f"{PAN_API}/disk/main"},
            timeout=self.timeout)
        j = r.json()
        info = j.get("info")
        errno = j.get("errno", -1)
        if isinstance(info, list) and info and info[0].get("errno") is not None:
            errno = info[0]["errno"]
        if errno not in (0, 4):
            raise BaiduPanError(f"复制失败(errno={errno} {str(j.get('show_msg') or '')[:60]})")
        return {"copied": len(src_paths), "errno": errno}

    def delete_paths(self, paths: list[str], *, bdstoken: str = "") -> int:
        """按**路径**删除(`opera=delete`)。返回删除条数。

        ⚠️ 百度这边没有"回收站"保证 —— 调用方**必须自己确认路径**。
        加它是因为「复制」需要一个可逆的验证手段(复制→核对→删掉),不是为了批量清理。
        """
        if not paths:
            return 0
        tok = bdstoken or self.get_bdstoken()
        r = self._browser().post(
            f"{PAN_API}/api/filemanager",
            params={"opera": "delete", "async": "1", "onnest": "fail", "bdstoken": tok,
                    "clienttype": "0", "app_id": "250528", "web": "1", "channel": "chunlei"},
            data={"filelist": json.dumps(paths, ensure_ascii=False)},
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "Origin": PAN_API, "Referer": f"{PAN_API}/disk/main"},
            timeout=self.timeout)
        j = r.json()
        errno = j.get("errno", -1)
        if errno != 0:
            raise BaiduPanError(f"删除失败(errno={errno} {str(j.get('show_msg') or '')[:60]})")
        return len(paths)

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
                           share_pwd: str = "8888", intro_dir: str = "") -> dict:
        """转存分享到自己网盘、**放进宣传简介**、创建新分享。返回 {share_url, password, files}。

        ⚠️ **简介必须夹在「转存」与「建分享」之间**(2026-10-10,照夸克侧同一条纪律):
        先建链再放简介,链子快照的就是没有简介的那份 —— 对方保存下来永远看不到它。
        `intro_dir` = **我们盘里**那个装着简介的目录(如 `/redian宣传`),留空 = 不做。
        """
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
            # ⚠️ **show_msg 必须一起带出来**(2026-10-03 实测踩到):只留 `errno=-6` 时
            # **看不出是"分享已失效"(终态,别再重试)还是"转存被限制"(等一会就好)** ——
            # 这两者对下游的意义完全相反。第 165 行创建分享时本来就带了 `show_msg`,这里漏了。
            t["show_msg"] = t["info"][0].get("show_msg") or t.get("show_msg") or ""
        errno = t.get("errno", -1)
        if errno not in (0, 4):  # 4=文件已存在,复用
            why = str(t.get("show_msg") or "")
            # ⚠️ **登录态失效要单独抛**:它和"链死了"完全不同 —— 前者重粘 Cookie 就能搬,
            # 后者永远搬不了。混在一个 `errno=-6` 里,下游既看不出该干什么,也报警不出来。
            if _looks_like_auth_issue(why):
                raise BaiduPanAuthError(f"百度网盘登录态失效({why.strip()[:40]})")
            raise BaiduPanError(_transfer_error(errno, why))
        paths = [f"{target_dir.rstrip('/')}/{n}" for n in names if n]

        # ★ 宣传简介:夹在「转存」与「建分享」之间(顺序反了链子里就没有它)。
        # ⚠️ 失败**不挡转存/分享** —— 但要大声记,别让它变成静默缺失。
        if intro_dir:
            try:
                src = self.list_dir(intro_dir)
                promo = [(str(x.get("path") or ""), str(x.get("server_filename") or ""),
                          int(x.get("size") or 0))
                         for x in src if not x.get("isdir") and x.get("path")]
                if not src:
                    logger.warning("宣传简介目录在我们盘里找不到,本轮跳过:%s", intro_dir)
                elif not promo:
                    logger.warning("宣传简介目录是空的,本轮跳过:%s", intro_dir)
                else:
                    # 已有同款就免复制 —— 判据是**名字 + 大小一起认**(只看名字会把同名不同内容
                    # 的当成同款;与夸克侧的复制去重同一口径)。
                    have = {(str(x.get("server_filename") or ""), int(x.get("size") or 0))
                            for x in self.list_dir(target_dir)}
                    todo = [p for p, n, s in promo if (n, s) not in have]
                    if todo:
                        self.copy_into(target_dir, todo)
                        logger.info("宣传简介已放进资源目录:%d 个", len(todo))
                    else:
                        logger.info("宣传简介:资源目录里已有同款,**免复制**")
            except Exception as exc:                    # noqa: BLE001 - 简介失败不该毁掉整次转存
                logger.warning("放宣传简介失败(不挡转存/分享):%s: %s",
                               type(exc).__name__, str(exc)[:120])

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
        """登录态检查:**查的是"网页会话"**,失败抛 `BaiduPanAuthError`。

        ⚠️ **不能用 `/api/loginStatus` 判**(2026-10-04 实测踩到):一份**只有 `BDUSS`、
        没有 `STOKEN`** 的"半登录"Cookie,`loginStatus` 照样回 `errno:0` ——
        于是体检说"正常",而**转存(`/share/transfer`)与分享页解析全失败**:
        这就是典型的**假成功**,而且它让"Cookie 失效"的告警**永远等不到**。

        真正决定能不能转存的是**网页会话**,所以改探 `/api/quota`
        (未登录回 `errno:-6 用户未登录`)。错误文案也写清**该去哪、缺什么**。
        """
        r = self._browser().get(f"{PAN_API}/api/quota",
                                params={"clienttype": "0", "web": "1"}, timeout=self.timeout)
        errno = r.json().get("errno", -1)
        if errno != 0:
            raise BaiduPanAuthError(
                "百度网盘**网页登录态**失效(常见原因:Cookie 里缺 `STOKEN`)—— "
                "请打开 pan.baidu.com 确认已登录后,重新复制**整份** Cookie")
        return True
