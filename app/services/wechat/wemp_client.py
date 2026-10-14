"""公众号后台 appmsgpublish 直连客户端(自研,2026-09-30)。

**为什么自研**:WeRSS 同类项目有停维前科(wewe-rss 归档/wechat-article-exporter 停维),
列表源不能赌任何单一开源项目的存活。这份实现按公开接口合同独立完成,凭据自持
(system_config[wemp_cred_{uid}]),WeRSS 存废与本模块无关。

接口合同(逆向自公开实现,2026-09-30 生效):
    GET https://mp.weixin.qq.com/cgi-bin/appmsgpublish
      ?sub=list&sub_action=list_ex&begin={偏移}&count={条数}
      &fakeid={目标号 base64(数字)}&token={后台 token}&lang=zh_CN&f=json&ajax=1
    头带公众号后台 Cookie。响应 {base_resp:{ret}, publish_page:"<json 字符串>"},
    ret: 0=ok / 200013=频率限制 / 200003=会话无效。
    publish_page 解析: publish_list[*].publish_info(json 字符串) → appmsgex[*] 为文章。
"""
from __future__ import annotations
from app.utils.ua import CHROME_WINDOWS  # 统一 UA(见该模块注释)

import base64
import json

from typing import Any

import requests

from app.services.reader_platform_client import PlatformError
from app.utils import get_logger

logger = get_logger(__name__)

WEMP_BASE = "https://mp.weixin.qq.com"
_UA = CHROME_WINDOWS

#: 后台 `appmsgex[*]` 里"阅读量 / 点赞量"的字段名。
#:
#: ⚠️⚠️ **这几个名字是"待首次真会话确认"的,不是已验证的事实** ——
#: 2026-10-14 实测时后台凭据已失效(`200003 invalid session`),**没能验证**。
#: 所以这里不赌一个名字,而是:(a) 兼容社区通行的几个形态;
#: (b) **第一次解析成功就把真实字段名打进日志**(`_log_shape_once`)——
#: 让第一次真会话**自己回答**"它到底叫什么",而不是我猜一个然后静默拿不到数。
_READ_KEYS = ("read_num", "readNum", "read_count")
_LIKE_KEYS = ("like_num", "likeNum", "old_like_num", "like_count")

_shape_logged = False


def _count(value: Any) -> int:
    """`"12345"` / `12345` / `"10万+"` / `""` → int。取不出就 **0**。

    ⚠️ **0 在这条链上表示"不知道",不是"没有"** —— 与全仓 `read_num` 的语义一致:
    `_insert_new_articles` 只在「旧值 0 且新值 > 0」时回填,**从不写 0**。
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    import re as _re

    text = str(value).strip().replace(",", "").replace("+", "")
    if not text:
        return 0
    m = _re.match(r"^(\d+(?:\.\d+)?)\s*([万wW])?", text)
    if not m:
        return 0
    num = float(m.group(1))
    if m.group(2):
        num *= 10000
    return max(0, int(num))


def _pick(art: dict, keys: tuple[str, ...]) -> int:
    """按候选名取第一个"取得出数"的值。"""
    for k in keys:
        if k in art:
            got = _count(art.get(k))
            if got:
                return got
    return 0


def _log_shape_once(art: dict) -> None:
    """**第一次**解析成功时,把后台真实的字段名打出来(每进程一次)。

    这条日志是给"字段名到底叫什么"这个问题留的**唯一权威出口** ——
    没有它,名字猜错了就是"阅读数一直是 0",而没有任何地方会说出原因。
    """
    global _shape_logged
    if _shape_logged:
        return
    _shape_logged = True
    logger.info("appmsgpublish 文章字段(首次解析,用于确认阅读数键名)= %s", sorted(art.keys()))


class WempError(PlatformError):
    """公众号后台接口异常(频率/会话/传输)。继承 PlatformError:监听轮 ⓪ 分支的
    `except PlatformError` 降级逻辑自动衔接(失败交给后续源,零改动)。"""


class WempRateLimited(WempError):
    """频率限制(ret=200013)——调用方应降级到其他源,勿重试打穿。"""


class WempAuthError(WempError):
    """会话无效(ret=200003)——凭据需重新扫码获取。"""


def mp_id_to_fakeid(mp_id: str) -> str:
    """MP_WXS_{数字} → base64(数字)(后台接口的 fakeid 形态)。"""
    num = (mp_id or "").replace("MP_WXS_", "").strip()
    return base64.b64encode(num.encode()).decode()


def fakeid_to_mp_id(fakeid: str) -> str:
    """`fakeid`(base64(数字))→ `MP_WXS_<数字>` —— **就是我们的 `biz` / `weread_book_id`**。

    ⚠️ 与 `mp_id_to_fakeid` 是**同一件事的两个方向**,必须成对维护:WeRSS 当年就是把
    `MP_WXS_<base64解码(fakeid)>` 当订阅 id 的,微信读书的 bookId 又是同一编号
    (2026-10-01 用 `mp_cover` 在未关注的号上验过)⇒ 解出它,号就**立刻可监听**
    (走微信读书那条路,不必等 WeRSS 抓)。
    解不出(非法 base64 / 空)返回空串 —— **不猜**。
    """
    try:
        num = base64.b64decode(str(fakeid or "").strip(), validate=True).decode()
    except Exception:  # noqa: BLE001 - 垃圾输入返回""是合法结果
        return ""
    return f"MP_WXS_{num}" if num.isdigit() else ""


class WempClient:
    """appmsgpublish 直连(读侧);与 WerssClient 的 mp_articles 合同一致。

    只做只读的"拉某号文章列表"——与我们监听轮的 ⓪ 分支需求完全对齐;
    抓取副作用(入库/推送)由调用方处理。凭据:cookie(后台登录态)+ token(后台 URL 里的)。
    """

    def __init__(self, cookie: str, token: str, timeout: int = 20) -> None:
        if not cookie or not token:
            raise WempError("公众号后台凭据缺失(cookie/token)")
        self.cookie = cookie
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"User-Agent": _UA, "Referer": f"{WEMP_BASE}/cgi-bin/appmsg",
                "Cookie": self.cookie, "X-Requested-With": "XMLHttpRequest"}

    def _get_json(self, path: str, params: dict) -> dict:
        """打后台 JSON 接口,并把 `base_resp` 的错翻成人话。

        **两个端点共用这一处**(`appmsgpublish` 拉文章 / `searchbiz` 按名找号)——
        「同一套判据写两遍迟早飘一个」是本仓反复栽过的坑,错误码分流尤其不能有两个版本。
        """
        try:
            resp = requests.get(f"{WEMP_BASE}{path}", params=params,
                                headers=self._headers(), timeout=self.timeout)
            msg = resp.json()
        except requests.RequestException as exc:
            raise WempError(f"{path} 请求失败:{type(exc).__name__}") from exc
        except ValueError as exc:
            raise WempError(f"{path} 响应非 JSON(会话可能已失效)") from exc

        base = msg.get("base_resp") or {}
        ret = base.get("ret")
        if ret == 200013:
            raise WempRateLimited(f"{path} 频率限制(200013)")
        if ret == 200003:
            raise WempAuthError("公众号后台会话失效(200003),请重新扫码授权")
        if ret != 0:
            raise WempError(f"{path} 错误 ret={ret}:{base.get('err_msg', '')}")
        return msg

    def search_biz(self, kw: str, limit: int = 10) -> list[dict]:
        """按名搜**全量公众号**(后台 `searchbiz`)→ `[{fakeid, nickname, alias}]`。

        ★ 为什么要自己实现(2026-10-14 实测):WeRSS 的 `search_mp` 本来就是**转发这个接口**,
        而**这套凭据我们手里就有** ⇒ 「公众号名 → 可监听标识」不必再依赖那个容器/Docker 镜像。
        当日实测同一枚凭据:`appmsgpublish` 回 `200013`(**限流**),而 `searchbiz` **回 `ret=0`
        且命中 3 个号** ⇒ **限流是按端点的**,不是整个后台会话 —— 所以"另一个端点"是一条真正可用的路。

        ⚠️ 两条纪律(与 `_subscribe_by_name` 同源):返回里的 `fakeid` 是 **base64(数字)**,
        要 `fakeid_to_mp_id()` 才变成 `biz`;而且**同名与形近号会一起返回**
        (实测搜「小猫爱吃米」回了「小猫咪爱吃米」「爱吃的小猫」)⇒ **必须按昵称精确比对**,
        重名歧义一律不用(猜错的代价是把 A 号的文章当 B 号推给员工)。
        """
        query = str(kw or "").strip()
        if not query:
            return []
        msg = self._get_json("/cgi-bin/searchbiz", {
            "action": "search_biz", "begin": "0",
            "count": str(max(1, min(int(limit), 20))), "query": query,
            "token": self.token, "lang": "zh_CN", "f": "json", "ajax": "1",
        })
        out: list[dict] = []
        for raw in msg.get("list") or []:
            if not isinstance(raw, dict):
                continue
            fakeid = str(raw.get("fakeid") or "").strip()
            nickname = str(raw.get("nickname") or "").strip()
            if not fakeid or not nickname:
                continue      # 没有 fakeid 就没法换 id;没有昵称就没法精确比对
            out.append({"fakeid": fakeid, "nickname": nickname,
                        "alias": str(raw.get("alias") or "").strip()})
        return out

    def mp_articles(self, mp_id: str, page: int = 1, limit: int = 20) -> list[dict]:
        """某号文章列表(发布时间降序),归一化为 ReaderPlatformClient 同一结构。"""
        limit = max(1, min(int(limit), 20))  # 后台接口单页上限 20,超了整请求被拒
        page = max(1, int(page))
        params = {
            "sub": "list", "sub_action": "list_ex",
            "begin": str((page - 1) * limit), "count": str(limit),
            "fakeid": mp_id_to_fakeid(mp_id), "token": self.token,
            "lang": "zh_CN", "f": "json", "ajax": "1",
        }
        msg = self._get_json("/cgi-bin/appmsgpublish", params)

        page_raw = msg.get("publish_page")
        if not page_raw:
            return []  # 无 publish_page = 没有更多文章(正常翻页终止)
        try:
            publish = json.loads(page_raw)
        except ValueError as exc:
            raise WempError("publish_page 解析失败(接口形态可能已变)") from exc

        items: list[dict] = []
        for block in publish.get("publish_list") or []:
            info_raw = block.get("publish_info")
            if not info_raw:
                continue
            try:
                info = json.loads(info_raw)
            except ValueError:
                continue
            for art in info.get("appmsgex") or []:
                title = str(art.get("title") or "").strip()
                url = str(art.get("link") or "").strip()
                if not title or not url:
                    continue  # 无标题/链接的条目上不了卡片也无法去重
                _log_shape_once(art)
                items.append({
                    "id": str(art.get("aid") or url),
                    "title": title,
                    "url": url,
                    "summary": str(art.get("digest") or "").strip(),
                    "publish_at_raw": art.get("create_time") or art.get("update_time"),
                    # ★ 2026-10-14:后台这个接口**是免费阅读数唯一不受"29 个号额度"限制的来源**
                    #   (见 `doc/外部接口速查.md` 第 3 节的"还剩什么")。原来这两项被直接丢掉,
                    #   于是即便后台活着,卡片上阅读数也还是全 `—`。取不到就是 0(=不知道)。
                    "read_num": _pick(art, _READ_KEYS),
                    "like_num": _pick(art, _LIKE_KEYS),
                })
        return items
