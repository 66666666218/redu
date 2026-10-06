"""探夸克「口令」这条路能不能通(2026-10-06)—— **结论:网页版不通,要通只能抓 App 包**。

## 起因
用户指出:**抖音不止有迅雷口令,也有夸克口令**,形态如 `咐置铸剑上供叩苓`。
而现系统对它是 **100% 漏的** —— `wechat/_text.py` 的 `PAN_PATTERNS` **只认 URL**
(`pan.quark.cn/s/xxx`),`douyin_leads` **只认 `《…》`**,所以这类帖子在系统眼里等于没口令。

## 已确认的事实(本脚本可复现①③)
1. **候选服务端端点全 404**(`sharepage/search`、`share/resolve`、`koul/parse`、
   `sharepage/info`)—— 猜接口名这条路走不通,接口面太大。
2. **阳性对照通过**:同一套请求/同一份 cookie 打 `sharepage/token`,能正常拿到 `stoken`
   ⇒ **请求通道与 Cookie 都没问题**,404 是真的"没这个接口",不是"没权限/没登录"。
   (⚠️ 这一步不能省:没有阳性对照时,"全 404" 也可能只是"cookie 失效"。)
3. **夸克网页版没有口令入口**(2026-10-06 用 Playwright 带**已登录** cookie 实测):
   网盘页 `/list`、首页、分享页 `/s/xxx` **通篇不含「口令」字样**;
   网页只能靠 `pan.quark.cn/s/xxx` 打开分享。

## 推论
**夸克口令(若服务端可解)是 App 独有功能,网页版完全接不到** —— 与迅雷当初
「口令 → shareID 只在客户端」**完全同构**(见 memory `xunlei-pan-api-via-urldb`)。
要真打通,只有两条路:
- **App 抓包**(mitmproxy + `adb reverse` + 系统 CA)—— 项目做过两次(迅雷/闲鱼),成本高一个量级;
- 或者**不解析、只当需求信号入库**(见下面「先答一个更便宜的问题」)。

## ⚠️ 先答一个更便宜的问题(否则可能白投入)
`咐置…叩苓` 到底**是不是一个可解析的口令**?还有另一种同样合理的解释:
它是「**复制…口令**」的**规避审核写法**(抖音封"复制口令"这类词),中间夹的是**资源名**。
支持这个解释的证据:库里唯一那条样本是
`咐置[人生指南得]叩苓｜高性价比人生指南获取教程来啦…《高性价比人生指南》…`
—— **中间夹的正是资源名**,而同一篇帖子另一半的《》里就是《高性价比人生指南》。
若是这样,**根本不存在可解析的口令**,"打通解析"就是伪命题;
该做的是把**资源名**当需求信号收进来。

**判据(30 秒,人肉可做)**:把 `咐置铸剑上供叩苓`(或中间的 `铸剑上供`)粘进**夸克 App**看能不能打开资源。
- 能打开 ⇒ 是真口令 ⇒ 值得上 App 抓包;
- 打不开 ⇒ 是规避写法 ⇒ 别做解析,改做"资源名入库"。

## ⭐ 2026-10-06 更新:拿到了一份**完整的夸克分享文本**,终于有了 ground truth
```
我用夸克网盘给你分享了「铸剑纳贡（ForgeTax）」，点击链接或复制整段内容，打开「夸克APP」即可获取。
伏脂乞台盆蛙盛洞座          ← 口令 = 8 个**随机汉字**
/~498b3bHYcr~:/            ← 另一个带 ~ 的**令牌**
链接：https://pan.quark.cn/s/6fc59982de5b     ← **标准答案**
```
有了答案,**每个候选都能当场证伪**,不用再猜语义。已试并**全部否定**:
| 试法 | 结果 |
|---|---|
| 口令当 `pwd_id`(sharepage/token) | `404 分享不存在`(code 41006) |
| 令牌裸串当 `pwd_id` | 同上 |
| 口令当搜索词(`file/search`) | 401 + **加密响应体** |
| 令牌拼 URL(5 种:`/kl/`、`/~x~/`、`/s/`、`/share/`、`quark.cn/x`) | 全 302 回首页或 SPA 兜底,**没有一个指向标准答案** |
| **阳性对照**:标准答案的 id 当 `pwd_id` | ✅ 200 + stoken ⇒ **上面那些"否"都有效** |

**⇒ 口令解析在 App 客户端内**,与迅雷当初「口令 → shareID 只在客户端」**完全同构**。

## 还要注意:这份文本里**没有 `咐置…叩苓`**
用户先前给的 `咐置铸剑上供叩苓` 与这份是**同一个资源**(铸剑纳贡):`咐置`≈**复制**、`叩苓`≈**口令**、
`铸剑上供`≈**铸剑纳贡**(连资源名都被改成近义词)。所以那种写法是**给人看的规避版**,
**机器解析不了** —— 它不含真口令。App 之所以"能通",多半是靠资源名做模糊匹配(待验)。

## 下一步(两条重活,都要环境)
- **A 扫 APK dex**:下夸克 APK → grep `classes*.dex` 找 `/~` 令牌 或口令接口路径。
  **不用模拟器、不用证书**,纯离线;项目对迅雷就是这么拿全端点的。
- **B 抓 App 包**:雷电模拟器(`D:/leidian/LDPlayer14` 已在)+ `tools/mitmvenv`(mitmdump 已在)+ 系统 CA,
  在 App 里粘口令抓真实请求。**最可靠**,环境项目做过两次。

跑法:`python scripts/probe_quark_kouling.py`(只读、少量请求、带间隔)
"""
from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

from app.db import get_session_local
from app.services.cookie_store import get_cookie

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE = "https://drive-pc.quark.cn"

KOULINGS = ["咐置铸剑上供叩苓", "铸剑上供", "咐置人生指南得叩苓", "人生指南得"]
CONTROL_SHARE = "https://pan.quark.cn/s/872b2c790c29"   # 已知能解的夸克链(阳性对照)


def main() -> int:
    db = get_session_local()()
    ck = get_cookie(db, 1, "quark") or ""
    db.close()
    print("夸克 cookie 长度:", len(ck))
    if not ck:
        print("!! 没有夸克 cookie,探测无从谈起")
        return 1
    h = {"User-Agent": UA, "Cookie": ck, "Origin": "https://pan.quark.cn",
         "Referer": "https://pan.quark.cn/", "Accept": "application/json, text/plain, */*"}

    def probe(label: str, method: str, path: str, **kw) -> None:
        try:
            r = requests.request(method, BASE + path, headers=h, timeout=20, **kw)
            print(f"  [{r.status_code}] {label:<32} {r.text[:150]}".replace("\n", " "))
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERR] {label:<32} {type(exc).__name__}: {str(exc)[:70]}")

    # ① 阳性对照:这一条通过,才说明后面那些 404 是"真没这接口"
    print("\n=== ① 阳性对照:已知分享链能取到 token 吗 ===")
    probe("sharepage/token(已知链)", "POST", "/1/clouddrive/share/sharepage/token",
          json={"pwd_id": CONTROL_SHARE.rsplit("/", 1)[-1], "passcode": ""})
    time.sleep(2)

    # ② 口令当关键词搜分享
    print("\n=== ② 口令当关键词搜分享 ===")
    for k in KOULINGS[:2]:
        probe(f"sharepage/search?keyword={k[:8]}", "GET",
              f"/1/clouddrive/share/sharepage/search?keyword={k}&_page=1&_size=10")
        time.sleep(2)

    # ③ 其它候选端点
    print("\n=== ③ 其它候选端点 ===")
    for label, method, path in (
        ("share/resolve", "POST", "/1/clouddrive/share/resolve"),
        ("koul/parse", "POST", "/1/clouddrive/koul/parse"),
        ("sharepage/info", "GET", f"/1/clouddrive/share/sharepage/info?keyword={KOULINGS[0]}"),
    ):
        probe(label, method, path, json={"kouling": KOULINGS[0], "keyword": KOULINGS[0]})
        time.sleep(2)

    print("\n(网页版无口令入口的实测见本文件头部说明;那次探测是 Playwright 一次性做的)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
