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

import base64
import json

import requests

from app.services.reader_platform_client import PlatformError
from app.utils import get_logger

logger = get_logger(__name__)

WEMP_BASE = "https://mp.weixin.qq.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


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
        try:
            resp = requests.get(
                f"{WEMP_BASE}/cgi-bin/appmsgpublish", params=params,
                headers={"User-Agent": _UA, "Referer": f"{WEMP_BASE}/cgi-bin/appmsg",
                         "Cookie": self.cookie, "X-Requested-With": "XMLHttpRequest"},
                timeout=self.timeout)
            msg = resp.json()
        except requests.RequestException as exc:
            raise WempError(f"appmsgpublish 请求失败:{type(exc).__name__}") from exc
        except ValueError as exc:
            raise WempError("appmsgpublish 响应非 JSON(会话可能已失效)") from exc

        base = msg.get("base_resp") or {}
        ret = base.get("ret")
        if ret == 200013:
            raise WempRateLimited("appmsgpublish 频率限制(200013)")
        if ret == 200003:
            raise WempAuthError("公众号后台会话失效(200003),请重新扫码授权")
        if ret != 0:
            raise WempError(f"appmsgpublish 错误 ret={ret}:{base.get('err_msg', '')}")

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
                items.append({
                    "id": str(art.get("aid") or url),
                    "title": title,
                    "url": url,
                    "summary": str(art.get("digest") or "").strip(),
                    "publish_at_raw": art.get("create_time") or art.get("update_time"),
                })
        return items
