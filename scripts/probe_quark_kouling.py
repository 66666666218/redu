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

## ⭐⭐ 2026-10-06 深夜:反编译 APK,**端点和参数全找到了**(还差签名)

从模拟器里 `adb pull` 出夸克 base.apk(130MB,不用找下载源),用 **androguard** 解 dex。

### 功能真身
内部代号 **「U口令」(`utoken` = U Token)**。域名:
```
https://utoken2.quark.cn        (备用 https://utoken2.uc.cn)
```
### 端点(全部**实测存活**)
| 端点 | 证据 |
|---|---|
| `POST /utoken/v2/parse` | 空 body → `无效的请求:app=null`;传 app 后 → `requestBytes invalid` |
| `POST /utoken/v2/create` | 空 body → `分享计划无效:shareCode=null`;带 shareCode → `新建口令请求反序列化失败` |
| `POST /third/share/landing/info` | GET → `405`,POST → `无效的请求` |

### 请求参数(从 `Lzh1/c` 的方法里读出来的原样字符串)
```
parse :  kps · app · QUARK · clipboard · identifier · shareSecret · timestamp · sign
create:  shareCode · businessCode · kps · sign
         POST · application/json
```
**⚠️ 请求带 `sign`** —— 这就是为什么裸 JSON 和手搓 protobuf 全被拒(`requestBytes invalid`)。
我先前误判成 protobuf,**是错的**:服务器说的是"反序列化失败",但结构其实是**带签名的 JSON**。

### 为什么之前所有请求都 404
真实 URL **由服务端 CMS 下发**,配置键原样是:
```
cms_utoken_request_query_url / cms_utoken_request_build_url
cms_utoken_min_length / cms_utoken_limit_length / cms_utoken_regular_rules
cms_utoken_direct_jump_config / cms_utoken_title_suffix_config / cms_utoken_blocking_dialog_config
```
⇒ **硬编码里根本搜不到**,猜路径必然 404。

### 拿到的错误码表(有诊断价值)
`SHARE_PLAN_INVALID` · `SHARE_PLAN_UNAUTHORATIZED` · `SHARE_TIMES_OVERLIMIT` ·
`SHARE_TIMES_OVERLIMIT_PLAN` · `SELF_UTOKEN` · `口令已过期` ·
`今日生成口令次数已达到上限` · `口令数量总数已达到系统上限` · `口令违规`

### ❌ 还差最后一步:`sign` 怎么算
需要知道`sign = f(哪些字段, 顺序, 密钥, 算法)`。**可能在 native `.so` 里** —— 那样成本要再上一个台阶。
**GitHub 上没有现成实现**:`ihmily/quarkpantool`、`lich0821/QuarkPan`、`ByLsPro/JxPan`
三个主流夸克库**都没有 utoken/口令相关代码**;精确搜域名也**无结果**。

### 备选方案(不需签名)
模拟器里实测过:**App 已登录、剪贴板粘口令会自动弹出解析卡片**(显示资源标题 +「立即查看」)。
⇒ 可以用 `adb shell input` + `uiautomator dump` **驱动 UI 自动化**拿结果,**完全绕开签名**。
代价:要模拟器常开、每个口令一次 UI 操作。

跑法:`python scripts/probe_quark_kouling.py`(只读、少量请求、带间隔)

## ⭐⭐ 2026-10-09(第二轮):签名的**完整调用链**读出来了,只差 native

APK 已不在盘上,这轮重新 `adb pull`(模拟器还在:`emulator-5554`),
并把 `androguard 4.1.4` 装进 `tools/mitmvenv`(**不污染主环境**)。
用**字符串交叉引用**找到了真正的请求构造处,链条如下(全是反编译读出来的,不是推测):

```
Lzh1/c;->a(...)                      ← 构造 /utoken/v2/parse 的请求
    HashMap{ app=QUARK, clipboard, identifier, shareSecret, timestamp=当前毫秒, kps }
    sign = Lzh1/b;->a(map, 第二个字符串参数)
        list = new ArrayList(m.entrySet())
        Collections.sort(list, new Lzh1/b$a())     // Lzh1/b$a.compare = key 的 String.compareTo ⇒ **按 key 升序**
        for entry: sb.append(entry.getValue())     // ★ **只拼 value**,不拼 key
        return Lcom/uc/encrypt/a;->f( sb + 第二个参数 , 密钥号 )
            UnetEngineFactory.getCrypt() → UnetCrypt
            UnetCrypt->signWithNumber((short)parseShort(密钥号), 内容)   ← ★ 真签名在这
            …→ com.alibaba.wireless.security.open.SecException
            EncryptModel;->f(boolean) 只返回两个字面量:**'12000' / '12001'**(密钥号)
```

**native 在包里**:`libsgmainso-6.6.230703.so`、`libsgsecuritybodyso-6.6.230703.so`
⇒ 就是**阿里聚安全 SecurityGuard 6.6.230703**。**(第三轮核实:这一条是对的;**
**只是它同时在 `libunet.so` 里也注册了入口,见 §1/§2。)**

### ★ 端点级确认:唯一那道门就是 `sign`
拿真值打 `/utoken/v2/parse`(不带 Origin!见下):

| 请求 | 响应 |
|---|---|
| `sign="x"` | `500 Internal Server Error` |
| 不带 `sign` | `500` |
| **`sign=""`** | **`400 sign check error`** ← **服务器明确说"签名校验失败"** |
| `clipboard=/~令牌~/` | `500` |

⇒ 字段名/DTO **都对**(否则会像 `landing/info` 那样报"反序列化失败"),
**唯一没过的就是签名**。**没有绕过的余地**。

### 顺带澄清两处
- **`/third/share/landing/info` 不是口令接口**:它要 `thirdShareUrl` + `platform`;
  传 `platform=XHS` 时错误会**从"反序列化失败"变成"PARSE_CONTENT_ID_ERROR 解析内容id失败"**
  ⇒ 它是「**小红书分享链 → 夸克落地页**」那条功能。它的完整 URL 也不该被截短:
  `…?uc_param_str=dnntnwvepffrgibijbprsvpidicheiut`(请照 dex 里的原样)。
- ⚠️ **我自己这轮踩的坑**:第一版探针带了 `Origin` / `Referer`,**六个请求全 `403 Invalid CORS request`**
  —— App 根本不发这两个头,那是**我的探针错**。**没有这一纠正,我会拿"403 被墙"去写结论。**

### 现在的路只剩两条
1. **找已有人复刻的 `signWithNumber` / SecurityGuard**(与抖音 `a_bogus` 同一类活,
   那次是靠移植 `f2` 的纯 Python 实现解决的)⇒ 已派人在 GitHub 上查;
2. 都找不到 ⇒ 只能逆 `libsgmainso-6.6.230703.so`(成本再上一个台阶)。

## ★★★ 2026-10-09(第三轮):签名链**完整读出来了**,并拿到一个**可用的 oracle**

上一轮卡在「`signWithNumber` 究竟落在哪」。这轮把官方链路从 dex + native 里读全了,
顺手拿到一个**能当场算签名的 oracle**,并把「Frida 一挂就崩」的**真因**查实。
⚠️ 中途我一度写下「签名不在聚安全里」,**当天就纠正了** —— 结论是**仍在聚安全**(见 §2 的撤回记录)。

### 1. 真实链路(全部由 dex 静态读出,不是推测)
```
Lcom/uc/encrypt/a;->f(密钥号字符串, 内容)     ← ⚠️ 第一个参数是**密钥号**,第二个才是内容
    └→ Lcom/uc/base/net/unet/impl/UnetCrypt;->signWithNumber(short, String)  [classes6.dex, 纯 Java]
        └→ Lcom/alibaba/mbg/unet/internal/UNetCryptJni;->nativeSign(J, S, String) [classes.dex, ★native]
            └→ **libunet.so:0x2af168**        ← ★★★ 这里,不是 libsgmainso
```
怎么定位的:`lib/arm64-v8a/` 下 55 个 .so 逐个扫,`nativeSign` **只在 `libunet.so` 里出现**;
再用 `.rela.dyn` 的 `R_AARCH64_RELATIVE` 重定位 + `.rodata` 里的 RegisterNatives 名字表
**反查出函数指针** = `0x2af168`(`nativeEncrypt`=0x2aee68 / `nativeDecrypt`=0x2aefe8 /
`nativeSetDelegate`=0x2aedf0)。这条路本身可复用于任何"注册式 JNI 找不到入口"的库。

### 2. ⚠️⚠️ 撤回:**签名仍然在聚安全里**(这一条我当天先写错了,当场纠正)
链路的后半段是 **native 回抛给 Java delegate**(r2 反编译 + androguard 读字节码看出来的):
```
libunet.so:nativeSign(J, S, String)              ← 只是**转发壳**,不算签名
    └→(JNI 回调) UNetCryptJni.signWithNumber(Delegate, S, String)
        └→ delegate 实现 = Lcom/uc/base/net/unet/impl/UnetSecurityGuardCryptDelegate;
                            ^^^^^^^^^^^^^^ 类名直接写着 SecurityGuard
            └→ SecurityGuardManager.getInstance(ctx).getSecureSignatureComp()
                   .sign(SecurityGuardParamContext{ "INPUT" -> 内容 })
```
⇒ **真算法在聚安全的 native 里**,「AVMP 黑盒」的**难度判断依然成立**。

**我为什么会写错**:我只查了 `libunet.so` **自己**的字符串表和 `DT_NEEDED`
(`sgmain`/`AVMP`/`libsg` 出现次数确实全是 0),**但这条依赖走的是 Java 侧,那种查法根本看不见**。
一个「没看到」被当成了「不存在」 —— 与上一轮「一次崩溃 ⇒ 签名落在 AVMP」是**同一类错误的两面**。
**教训:`DT_NEEDED`/字符串表只能证明"有",不能证明"没有"。**

### 3. ★「Frida 一挂就 SIGSEGV」的真因:**Frida × Houdini**,不是反调试
APK 的 `lib/` **只有 `arm64-v8a`**,而雷电是 x86_64
(`ro.dalvik.vm.native.bridge = libhoudini.so`)⇒ 那套 native 库是**经 Intel 翻译层**跑的。
墓碑实锤(两次:`tombstone_08` / `tombstone_09`):
```
Cmdline: com.quark.browser   tid: UnetInitThread
signal 11 (SIGSEGV), code 128 (SI_KERNEL), fault addr 0x0
backtrace: 4 帧**全在 /system/vendor/lib64/libhoudini.so 内**
```
**崩在翻译层里、fault addr 是 0**,不像任何主动 `abort()` 的反调试。

**★ 可用姿势(实测一次都没崩)**:别用 `spawn`(App 冷启时 `UnetInitThread` 正撞上 Frida)。
**让 App 自己正常启动 → 等约 35 秒 → 再 attach。**
⚠️ 另一个坑:夸克启动后把**进程名改成了 `m.quark.browser`** ⇒ 按包名 attach 会报
`ProcessNotFound`(**进程其实活得好好的**),只能按 PID attach。

### 4. ★★★ 现在有 **signature oracle** 了(实测可用)
```
Java.use("com.uc.encrypt.a").c()             → 单例
        .f(String 密钥号, String 内容)        → 返回真签名
```
实测(跨 App 重启**逐字节一致** ⇒ **密钥是固定的,不是会话协商的**):
```
f("12000", "hello")  → 2ee08189927c2afb5f2d3308d55710671fa765a975ba
f("12000", "12000")  → 2ee02d52ca098a6f90cb51b1e06548a096498c8be17e
f("12001", "hello")  → 2ee136ab7dd84e0549180cf6be66ac95bfcc3a21a031
f("12000", "ZZZZZZZZ") → 2ee08ceaa78a9adcfc40c2e208142c3122497ddda175
```

### 5. ★ 签名的**结构**已经解出来了
```
sign(密钥号 k, 内容 c) = 大端 2 字节(k) ‖ 20 字节 MAC(k, c)
```
判据:**18/18** 个样本的前 4 个 hex 都等于 `struct.pack(">h", k).hex()`
(`12000`→`2ee0`,`12001`→`2ee1`);同一输入多次调用结果**逐字节一致**(确定性)。

### 6. ★ 那 20 字节是谁算的:**聚安全的 `ISecuritySignatureComponent.sign()`**
`UnetSecurityGuardCryptDelegate.sign(String)` 的字节码(androguard 逐条读出来的):
```
SecurityGuardManager.getInstance(context).getSecureSignatureComp()   // ISecuritySignatureComponent
HashMap(1).put("INPUT", 内容)
new SecurityGuardParamContext()
Short.toString(密钥号)
comp.sign(paramContext)                       // ★ 聚安全出签名
ByteBuffer.allocate(2).putShort(密钥号)        // ★ 2 字节**大端**密钥号
byteToHexString(...)
StringBuilder().append(密钥号hex).append(聚安全签名)   // ★★ 最终结果
```
**与实测逐字吻合**:`sign = hex(大端2字节密钥号) ‖ 聚安全签名`。
⇒ 那 20 字节**不是我们能离线构造的普通哈希**(所以 2520 个构造 + 4400 万个只读区密钥候选全 0 命中),
它是聚安全签名的输出。**要闭式解只有两条**:
1. 对 **`libsgmainso-6.6.230703.so` 走 unidbg**(把 .so 当黑盒签名机跑,不逆算法)—— 这是本轮的
   "换思路"里唯一没试过的主路;或
2. 直接用 §4 的 **oracle**(模拟器当签名机,已验证可用)。

⚠️ 附一条 angr 的坑(将来谁再走静态路会踩):angr 能建出 CFG(3.7 万个函数),但
`cfg.functions.get(0x2af1c0)` 是 **None** —— 这些入口**只被 RegisterNatives 表引用**,
CFG 看不到调用边,**必须显式传 `function_starts=[...]`**。

## ★★★★ 2026-10-09(第四轮):**端到端打通** —— 口令 → 分享码,服务端接受我们自己发的请求

### 1. 算签那段(classes9.dex `Lzh1/b;->a(Map, String)`,静态读出来的)
```
content = map 按 key **升序**、**只拼 value**(跳过空 key)
key     = EncryptModel.f(flag)                 // "12000" / "12001"
return com.uc.encrypt.a.c().f(key, content + 第二个参数)     // 第二个参数 = timestamp
```

### 2. 请求本体(classes9.dex `Lzh1/c;->a` 构造)—— **是明文 JSON,不是加密 blob**
```
POST https://utoken2.quark.cn/utoken/v2/parse      (Content-Type: application/json)
{"app":"QUARK","clipboard":"<口令原文>","identifier":"<~>","shareSecret":"<token>",
 "timestamp":"<毫秒>","kps":"",
 "sign":"<f(key, \"QUARK\" + 口令原文 + identifier + token + timestamp)>"}
```
- `kps` 是**空串**;
- 签名只覆盖 4 个字段(`app`/`clipboard`/`identifier`/`shareSecret`),但 `timestamp`
  **被拼进签名内容** ⇒ **换了 timestamp 必须重签**。

### 3. 实测(2026-10-09,`/~498b3bHYcr~:/`)
抓到的真值:`identifier="~"`、`token="498b3bHYcr"`、keyNumber=`12001`、
签名内容 = `QUARK/~498b3bHYcr~:/~498b3bHYcr1791554078829`、sign = `2ee104d93c0cbb14552c5bfee202ec282da1ee0c1c65`。

| 请求 | 响应 |
|---|---|
| App 算出的真签名 | **200 `{"success":true,"code":"OK"}`**,`androidUrl` 里带 `"pwd_id":"6fc59982de5b"` |
| sign 改坏一个字符 | **400 `sign check error`** |
| 换 timestamp 但不重签 | **400** |

`6fc59982de5b` **正是库里这条口令配对的分享码**(`https://pan.quark.cn/s/6fc59982de5b`)⇒ 闭环。

### 4. 结论
**除了那一段聚安全签名,整条请求都能在 Python 里 100% 复现。**
⇒ 「**App 当签名机、其余全纯协议**」这条路是**实测成立**的。拿到的 `shareCode` 直接拼
`https://pan.quark.cn/s/<shareCode>`,后面接我们**已有的**夸克转存链(纯协议、免签名)。
⇒ 这是当前性价比最高的一条:**不用碰 unidbg,也不用改生产里那条 UI 链**。
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
