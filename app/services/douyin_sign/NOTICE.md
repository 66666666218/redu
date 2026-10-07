# 第三方代码归属声明 —— `app/services/douyin_sign/`

本目录里的算法**不是本项目写的**。它逐字取自开源项目 **f2**,按 Apache License 2.0
分发。此文件是对 Apache-2.0 §4 要求的 "保留版权声明 / 注明修改" 的履行记录。

## 来源

| 项 | 值 |
|---|---|
| 项目 | [f2](https://github.com/Johnserf-Seed/f2) |
| 作者 | JohnserfSeed \<support@f2.wiki\> |
| 提交 | `f6be8c0ffba9a127075bbeafe4838716650b6325`(2026-10-06) |
| 许可证 | Apache License 2.0,全文见本目录 [`LICENSE.f2.txt`](LICENSE.f2.txt) |

逐字取用的文件:

| 本仓文件 | 上游路径 | 许可证 |
|---|---|---|
| `abogus.py` | `f2/utils/crypto/bytedance/abogus.py` | Apache-2.0 |
| `xbogus.py` | `f2/utils/crypto/bytedance/xbogus.py` | Apache-2.0 |

## 本仓做过的改动(**仅此两处,算法体一字未动**)

1. 删掉上游首行的 `# path: f2/...` 注释(那是 f2 仓库内的路径,搬过来已无意义);
2. 在文件顶部加了一段 `# ===` 横幅,标注来源、提交号与许可证。

改动后算法体的 sha256(去掉横幅、换行归一化为 `\n` 之后计算):

```
abogus.py  461b0c093627abbd972a076ce52391f27c8d220dcbdd0434b0f621f342d06015
xbogus.py  2f1d033ee8db74e6e63e2fd0cbbb105ba2780ab690bc51c778751db68ff3f707
```

`tests/test_douyin_sign.py::test_算法体未被改动` 会校验这两个值 —— **这是有意的绊索**:
任何对算法体的修改都会让测试变红,逼你在改之前先确认那是必要的、并同步更新这里的哈希。

## 为什么**搬**而不是 `pip install f2`

1. f2 是 9MB 的全能下载器,依赖 httpx / rich / curl_cffi / protobuf / websockets … 一大串;
   而这两个接口我们只用了 40KB、只需 `gmssl`(纯 Python 的 SM3)。
2. **签名被改版打断时,源码在自己手里才修得动。** 小红书那条链用的是 PyPI 上的 `xhshow`,
   它哪天跟不上小红书改版,我们只能等上游 —— 这是个已经吃过一次的教训。

## 上游更新了怎么办

```bash
# 1. 看上游这两个文件是否变了(比对 commit)
# 2. 变了 → 重新下载、重跑本目录的移植脚本、跑 tests/test_douyin_sign.py
# 3. 若签名输出的金标值变了,**先确认上游是有意改的**(翻它的 CHANGELOG / commit message),
#    再更新 tests 里的金标与上面的 sha256。
```

⚠️ **金标值变了不等于签名错了**。抖音的算法本身会随版本演化,上游跟进是正常的;
真正要警惕的是"我们改了但没同步金标"。
