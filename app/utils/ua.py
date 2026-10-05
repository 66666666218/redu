"""浏览器 User-Agent 常量(2026-10-05)。

**为什么要有它**:同一个"Windows 桌面 Chrome"的 UA 被**复制到 11 个模块里**,
而且版本号飘了 —— 实测 **147 ×5 / 131 ×4 / 141 ×1**,同一个 `hot_sources.py` 里
还同时存在两份(131 与 141)。后果:要升一次 UA 得改 11 处,必漏。

**收敛成一处**之后,升级/改 UA 只动这里。

⚠️ **本模块只管"同一形状、只是版本号不同"的那一批**(Windows 桌面 Chrome)。
它**不是**"全仓 UA 的唯一来源" —— 有些地方用 Mac UA 是**刻意的按平台区分**
(如 `collector.py` 里对不同目标平台用不同 UA),那些**不要往这里并**:
盲并会改掉线上行为,而 UA 是采集侧少数几个"改了就可能被风控"的东西之一。

⚠️ 反过来说:**改这里的值 = 同时改 11 个模块的线上行为**。改之前想清楚,
改之后要对**真正在跑的采集**做一次实测(微信读书列表、搜狗候选号、热榜源),
别只看单测 —— 单测是 mock 的,UA 对不对它们不知道。
"""
from __future__ import annotations

# Windows 桌面 Chrome(当前 147)。全仓"同形状 UA"的统一出处。
CHROME_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# Windows 桌面 **Edge**(百度榜与抖音热点在用)。与 Chrome 的形状**不同**(尾部 `Edg/`),
# 所以是**另一个常量**,不是"同一份抄错"。
EDGE_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)

# ⚠️ **以下这些故意不在本模块,别往里并**:
#   · `quark_transfer.QUARK_UA` —— 刻意伪装成**夸克 PC 客户端**
#     (`quark-cloud-drive/2.5.20 … Electron/…`),并进来等于放弃伪装;
#   · `collector.py` 里的 **Mac** UA —— 按目标平台刻意区分;
#   · 迅雷/闲鱼等自研客户端的 App UA —— 各是各家 App 的身份。
