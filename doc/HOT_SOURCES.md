# 热榜源清单(44 个)

> 2026-10-05 实测。**改源先跑 `python scripts/probe_hot_sources.py`** ——
> 判据是"**解析出 ≥5 条**",不是 HTTP 200。200 但 0 条会被下游读成"今天没热点"。

## 两种实现

| | 数量 | 说明 |
|---|---|---|
| **自研直连** | 21 | 直接打平台公开接口,零第三方依赖(**命门自持**) |
| **newsnow 容器** | 23 | 只留"确实打不通"的(要签名 / 要 cookie / 是 HTML),跑在**远程** |

⚠️ **一条源只能有一条链**。搬成自研的**绝不能再留 `NewsnowSource`**,否则同一份数据入库两次。

## 自研直连(21)

| 源 id | 名称 | 取数方式 | 备注 |
|---|---|---|---|
| `bilibili` | B站 | 网页接口 | ⚠️ 排行 `-352`(要登录态),**搜索与热搜正常** |
| `bilibili-hotsearch` | B站热搜 | 公开接口 | |
| `douban` | 豆瓣 | 公开 JSON | |
| `toutiao` | 今日头条 | 公开接口 | |
| `tencent-hot` | 腾讯新闻 | `i.news.qq.com` | |
| `zhihu` | 知乎 | 需 Cookie | 复用盘链搜索那份 Cookie |
| `thepaper` | 澎湃新闻 | `cache.thepaper.cn` | |
| `tieba` | 百度贴吧 | `hottopic/browse/topicList` | |
| `dongqiudi` | 懂球帝 | `api.dongqiudi.com` | |
| `nowcoder` | 牛客 | `gw-c.nowcoder.com` | |
| `hackernews` | Hacker News | `hnrss.org` **RSS** | newsnow 支持但容器不返回 items |
| `juejin` | 掘金 | `api.juejin.cn` | 标题在 `content.title`(**点路径**) |
| `sspai` | 少数派 | `sspai.com/api/v1` | |
| `aihot` | AI HOT | **RSS** | |
| `freebuf` | Freebuf | **RSS** | |
| `producthunt` | Product Hunt | **RSS** | |
| `chongbuluo-latest` | 虫部落最新 | **RSS** | |
| `jin10` | 金十数据 | `flash_newest.js` | 响应是 `var newest=[...]`,要**剥壳** |
| `wallstreetcn-hot` | 华尔街见闻热榜 | `api-one.wallstcn.com` | ⚠️ **用 `code=20000` 当成功码** |
| `wallstreetcn-news` | 华尔街见闻要闻 | 同上 | |
| `wallstreetcn-quick` | 华尔街见闻快讯 | 同上 | |

## newsnow 容器(23)

跑在**远程**(`SCHEDULER_ROLE=hotspot`)。⚠️ 本机没有这个容器,在本机跑冒烟会全 `ConnectionError` —— **那是测错机器,不是故障**。

| 源 id | 名称 | 为什么不自研 |
|---|---|---|
| `36kr` / `36kr-quick` / `36kr-renqi` | 36氪 / 快讯 / 人气 | 未试 |
| `cls-hot` / `cls-depth` / `cls-telegraph` | 财联社 热门/深度/电报 | ⚠️ 实测 `code=10012`,**要签名** |
| `xueqiu-hotstock` | 雪球热股 | ⚠️ 实测 **400**,要 Cookie |
| `cankaoxiaoxi` | 参考消息 | ⚠️ 实测 **404**,端点变了 |
| `hupu` | 虎扑 | ⚠️ 端点实测**是 HTML 不是 JSON** |
| `iqiyi` | 爱奇艺 | ⚠️ 实测 `{"code":3}` |
| `solidot` | Solidot | ⚠️ RSS 实测**只剩 1 条**(站点 feed 半废) |
| `fastbull-express` / `fastbull-news` | 法布财经 快讯/新闻 | 未试 |
| `gelonghui` | 格隆汇 | 未试 |
| `github-trending-today` | GitHub 趋势 | 页面是 HTML |
| `coolapk` | 酷安 | 要 app token |
| `ifeng` | 凤凰网 | 未试 |
| `ithome` | IT之家 | 未试 |
| `kuaishou` | 快手 | 要签名 |
| `qqvideo-tv-hotsearch` | 腾讯视频 | 要 POST 体 |
| `sputniknewscn` | 卫星通讯社 | 未试 |
| `steam` | Steam | 未试 |
| `chongbuluo-hot` | 虫部落热帖 | 未试 |

## 未接入但相关(候选)

| 源 id | 名称 | 为什么值得接 |
|---|---|---|
| `ghxi` | 果核剥壳 | **软件资源站**,与网盘拉新业务直接相关 |
| `smzdm` | 什么值得买 | 消费导购,常带资源 |
| `v2ex` | V2EX | `v2ex.com/feed/*.json` 是**公开 JSON API**,好接 |
| `pcbeta` | 远景论坛 | 软件资源 |

## 转化率口径(与源无关,但一起记着)

`app/services/conversion.py` 是单一事实来源:**公众号看阅读数 ×30%**、
**抖音看转发数 ×80%**、**其余看播放量 ×40%**;**拿不到播放量的用点赞 ÷5**
(**5 个赞 ≈ 1 次转存**,用户按抖音样本拍板)。
