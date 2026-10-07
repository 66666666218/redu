# -*- coding: utf-8 -*-
"""抖音 web 端**签名**参数生成(`a_bogus` / `X-Bogus`)—— 纯协议采集用。

## 这不是我们写的代码
本包是 **`Johnserf-Seed/f2` 的逐字移植**(Apache-2.0),来源与改动范围见同目录
`NOTICE.md`,上游许可证全文在 `LICENSE.f2.txt`。**搬过来而不是 pip 装 f2** 的原因:
f2 是 9MB 的全能下载器(拖 httpx/rich/curl_cffi/protobuf 一大串),我们只要这
两份签名;而签名一旦被改版打断,**源码在我们手里才修得动**(对照小红书那条
只能等上游更新 `xhshow` 的教训)。

## 对外只有三样
| 名字 | 干什么 | 依赖 |
|---|---|---|
| `ABogus` | 算 `a_bogus`(**当前抖音主用的那个**) | `gmssl`(纯 Python SM3) |
| `XBogus` | 算 `X-Bogus`(老版本,部分接口仍认) | **零依赖** |
| `BrowserFingerprintGenerator` | 造一个像真浏览器的 `fp`(指纹) | 零依赖 |

## 用法
```python
from app.services.douyin_sign import ABogus, BrowserFingerprintGenerator

fp = BrowserFingerprintGenerator.generate_fingerprint("Chrome")
sign, _ = ABogus(fp=fp, user_agent=UA).generate_abogus(query_string)
```

⚠️ **返回值是元组,签名在 `[1]`**(`[0]` 是拼好签名的完整 url,`[2]` 是 ua,
`ABogus` 甚至返回 4 个元素)。**未做简化封装**是有意的:调用方需要 `[0]` 时不该再拼一遍;
`tests/test_douyin_sign.py` 把这两个形状(`XBogus` 3 元组 / `ABogus` 4 元组)钉住了,
上游哪天改形状会先在那里红。
"""
from app.services.douyin_sign.abogus import ABogus, BrowserFingerprintGenerator
from app.services.douyin_sign.xbogus import XBogus

__all__ = ["ABogus", "BrowserFingerprintGenerator", "XBogus"]
