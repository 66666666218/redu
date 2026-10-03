# 接口规范(API)

> 版本: v1.5　|　最后更新: 2026-10-03(全项目审查:错误体订正 + 幽灵路由清理)
> 基础地址: 调度/监控系统暴露的 HTTP 服务(默认 `http://localhost:8080`)
> 认证: 除 `/healthz`(健康检查)外,所有接口需 **JWT Bearer** 登录态;管理接口另需 admin/operator 角色权限。

> ⚠️ **维护规则(2026-10-03 加)**:本文件里的每个 `/api/...` 路径都会**被测试对照真实路由表**校验
> (`tests/test_api_doc_routes.py`) —— 改了路由不改文档,**测试直接红**。
> 唯一的例外是 **WeRSS 那个外部服务**的 `/api/v1/wx/*`(不是我们的路由,已在测试里白名单)。

---

## 通用约定

- 请求与响应均为 `application/json`。
- 时间统一为 ISO 8601(含时区),如 `2026-08-29T10:00:00+08:00`。
- **错误响应结构**(2026-10-03 订正 —— 此前本文写的 `{"error": {"code": …}}` **代码从来不产生**,
  是设计稿残留,照着它写前端会取不到错误文案):

| 场景 | 实际结构 | 说明 |
| --- | --- | --- |
| 业务错误(`HTTPException`) | `{"detail": "描述性错误信息"}` | 绝大多数错误的形态 |
| 参数校验失败(422) | `{"detail": [{"loc": [...], "msg": "…"}]}` | **`detail` 是数组**,当字符串用会显示成 `[object Object]` |
| 未捕获异常(500) | `{"detail": "服务器开小差了…"}` | 前端对 500 **不展示 body**(可能含 SQL/连接串) |
| 401 / 403 / 404 | `{"detail": …}` | 前端另有固定文案兜底 |

> 前端解析入口:`frontend/src/api.js` 的 `errMessage()` —— 它按上表分档处理(含 422 数组展开)。
> **没有 `code` 字段**,也没有 `BAD_REQUEST`/`NOT_FOUND`/`INTERNAL_ERROR` 这套错误码。

---

## 1. 健康检查

- **接口名称**: 健康检查
- **请求方式**: GET
- **URL 路径**: `/healthz`

**请求参数**: 无

**响应示例 (200)**
```json
{
  "status": "ok",
  "version": "2.0.0",
  "time": "2026-08-29T10:00:00+08:00",
  "db": { "connected": true, "missing_tables": [], "users_missing_columns": [] }
}
```

> `db` 为数据库自查,用于部署后快速定位故障(建表失败/连不上库时接口会报 `OperationalError`):
> - `connected=false` + `error_type` → 连不上数据库(检查 `DATABASE_URL`、MySQL 是否就绪、账号密码)
> - `missing_tables` / `users_missing_columns` 非空 → 建表或迁移没跑成功
>
> **本接口始终返回 200**(容器 healthcheck 依赖它),数据库状况只体现在 `db` 字段;
> 出于安全只返回结构信息与异常类型,不含异常消息。

---

## 1.1 Web 看板

> ⚠️⚠️ **§1.1–§5 的路径是 v1.0 设计稿残留,实现时全部改过名** —— 2026-10-03 全项目审查时
> 逐条对照 `app/api/` 的真实路由表确认:**这些 `/api/v1/*` 一条都不存在**。
> 本文**保留它们**是为了留下对照关系,但**别照着它们写代码**。现状以 **§6 起**为准。
>
> | 本文旧路径(不存在) | 真实路径 |
> | --- | --- |
> | `/api/v1/trends/latest` | **`/api/trending`**(跨平台统一标准化快照) |
> | `/api/v1/alerts/latest` | **`/api/alerts/list`** |
> | `/api/v1/runs`(触发采集) | **`POST /api/collect/{platform}`** |
> | `/api/v1/runs/latest`(运行状态) | **`/api/admin/health`**(各源最近采集状态) |
> | `/api/v1/xianyu/hot` | **`/api/dashboard`**(其中的 `xianyu_hot` 字段) |
> | `/api/v1/xianyu/runs` | **`POST /api/collect/xianyu`** |
> | `/api/v1/xianyu/daily` | **`/api/xianyu/daily`**(仅少了 `/v1`) |
> | `/api/v1/douhot/trends` | **`/api/douhot/watch-analytics`** |
> | `/api/v1/douhot/runs` | **`POST /api/collect/douhot`** |
>
> `tests/test_api_doc_routes.py` 会**逐个核对本文提到的路径**,上面这批以"已知旧路径"白名单放行 ——
> 除它们和外部服务 WeRSS 的 `/api/v1/wx/*` 之外,本文再出现对不上的路径**测试就红**。

- **接口名称**: 监控仪表盘
- **请求方式**: GET
- **URL 路径**: `/`

页面加载后自动请求 `/api/trending`、`/api/dashboard` 并渲染。

---

## 2. 最近上涨趋势

- **接口名称**: 获取最近一次分析出的上涨趋势列表
- **请求方式**: GET
- **URL 路径**: `/api/v1/trends/latest`
- **请求参数 (Query)**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `limit` | int | 否 | 20 | 返回条数上限 |

**响应示例 (200)**
```json
{
  "count": 2,
  "items": [
    {
      "keyword": "某明星官宣",
      "source": "douyin",
      "growth": 0.45,
      "slope": 12.3,
      "rising": true,
      "decided_at": "2026-08-29T10:00:00+08:00"
    }
  ]
}
```

---

## 3. 最近告警

- **接口名称**: 获取最近告警记录
- **请求方式**: GET
- **URL 路径**: `/api/v1/alerts/latest`
- **请求参数 (Query)**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `limit` | int | 否 | 20 | 返回条数上限 |

**响应示例 (200)**
```json
{
  "count": 1,
  "items": [
    {
      "keyword": "某明星官宣",
      "reason": "环比增长 45% 且斜率为正",
      "triggered_at": "2026-08-29T10:00:00+08:00"
    }
  ]
}
```

---

## 4. 手动触发一次管道

- **接口名称**: 手动触发一次全量采集→分析→告警→归档流程
- **请求方式**: POST
- **URL 路径**: `/api/v1/runs`
- **请求参数 (Body)**: 无(可选 JSON)

```json
{}
```

**响应示例 (202)**
```json
{
  "run_id": "20260829100000",
  "status": "started",
  "message": "采集任务已触发"
}
```

---

## 5. 最近运行状态

- **接口名称**: 获取最近一次运行的状态与统计
- **请求方式**: GET
- **URL 路径**: `/api/v1/runs/latest`

**请求参数**: 无

**响应示例 (200)**
```json
{
  "run_id": "20260829100000",
  "status": "success",
  "started_at": "2026-08-29T10:00:00+08:00",
  "finished_at": "2026-08-29T10:00:10+08:00",
  "items_collected": 50,
  "analyses_count": 10,
  "rising_count": 2
}
```

---

## 6. 闲鱼虚拟商品热榜

> 需要本地 `.env` 配置 `GOOFISH_COOKIE_FILE`(闲鱼登录 Cookie)与 `XIANYU_KEYWORDS`。

### 6.1 获取最近热榜

- **接口名称**: 获取闲鱼虚拟商品热榜
- **请求方式**: GET
- **URL 路径**: `/api/v1/xianyu/hot`
- **请求参数 (Query)**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `limit` | int | 否 | 50 | 返回条数上限 |

**响应示例 (200)**
```json
{
  "count": 2,
  "items": [
    {
      "item_id": "1066044260035",
      "title": "PS零基础教程全套学习 【拍下秒发】...",
      "price": "¥1",
      "seller": "卖家甲",
      "pic": "https://img.alicdn.com/...",
      "hit_keywords": 2,
      "best_rank": 1,
      "keywords": "ps教程,软件",
      "created_at": "2026-08-30T19:55:10"
    }
  ]
}
```

> 排名依据:闲鱼"综合"顺序(可选指标,见 doc/dev.md §5.8)。

### 6.2 手动触发一次热榜采集

- **接口名称**: 手动触发闲鱼热榜采集
- **请求方式**: POST
- **URL 路径**: `/api/v1/xianyu/runs`
- **请求参数 (Body)**: 无

**响应示例 (202)**
```json
{
  "run_id": "20260830195510",
  "count": 50,
  "items": []
}
```

### 6.3 获取最近一次"今日热榜"总结

- **接口名称**: 获取闲鱼每日热榜总结
- **请求方式**: GET
- **URL 路径**: `/api/v1/xianyu/daily`
- **请求参数**: 无

**响应示例 (200)**
```json
{
  "summary_date": "2026-08-30",
  "created_at": "2026-08-30T23:18:24",
  "items": [
    {
      "item_id": "1066044260035",
      "title": "PS零基础教程全套学习 ...",
      "price": "¥1",
      "occurrences": 2,
      "best_rank": 1,
      "keywords": "ps教程,软件",
      "is_new": true
    }
  ]
}
```

> `is_new=true` 表示较上次总结新上榜,`new_count` 为当天新上榜总数。看板页 `GET /` 亦展示。
>
> ⚠️ 本接口是**读取**接口(从 `xianyu_daily` 快照聚合)。目前**没有**"每日定时生成并邮件推送总结"的调度作业——
> 早期文档描述的 `DAILY_SUMMARY_CRON` 已无任何代码引用,该配置已移除。如需定时推送需另行实现。

> **风险控制(闲鱼 mtop)**:闲鱼为登录态接口,过度请求会触发网关风控。实测 `FAIL_SYS_USER_VALIDATE`(人机验证/滑块)**经退避重试仍无效**,已与真限流区分——前者立即抛 `XianyuVerify`(由运维人工过滑块/换出口 IP,传输层已用 `curl_cffi` 伪 Chrome 指纹降低被标记概率,dock/dev.md §5.8),后者(`FAIL_SYS_RATE_LIMIT`/`FAIL_SYS_USER_LIMIT`)才走指数退避。
> `POST /api/xianyu/collect-deep` 在**整轮**被验证/限流时返回 **200 + `status:"failed"`**(0 条,不再 500);详情抓取**中途**被验证/限流则停止抓取并保留已采部分,**返回 `status:"partial"`**。`run_xianyu`(`/api/collect/{platform}` 或调度)遇验证则记 `failed`,由既有"采集持续失败"告警提醒运维(告警消息含最近一次失败原因,人工可据"需人工过滑块/换出口 IP"行动)。

---

### 6.4 价位行情(价格=供给热度,想要数=需求热度)

- **接口名称**: 闲鱼价位行情 / 供需比
- **请求方式**: GET
- **URL 路径**: `/api/xianyu/market`
- **请求参数**: `days`(可选,查询窗口天数,默认 30,取值 1–365)

**响应示例 (200)**
```json
{
  "days": 30,
  "item_count": 87,
  "seller_count": 61,
  "supply": { "min_price": 1.0, "median_price": 2.5, "avg_price": 12.4, "p25_price": 1.28 },
  "demand": { "want_total": 24810, "want_median": 203.0 },
  "ratio": 285.2,
  "keywords": [
    { "keyword": "ps教程", "items": 12, "min_price": 1.0, "median_price": 1.28,
      "avg_price": 3.1, "want_total": 12800, "ratio": 1066.7, "price_cut": 4 }
  ],
  "blue_ocean": [ { "keyword": "ps教程", "items": 12, "ratio": 1066.7 } ],
  "red_ocean":  [ { "keyword": "剪映会员", "items": 31, "ratio": 88.0 } ],
  "price_buckets": [ { "range": "0-1", "count": 22 }, { "range": "1-3", "count": 41 } ]
}
```

> **口径(2026-10-03 起)**:
> - **价格是供给端信号** —— 同款在售条数越多、价被压得越低,只说明**红海**(有人验证过能卖,但你在跟几百个同行抢)。单看价格**看不出需求热度**。
> - **想要数才是需求端信号**,且**2026-10-03 起由搜索响应免费提供**(藏在商品卡 `fishTags` 的渲染标签里,如「6770人想要」;实测 90 条命中 86 条 = **95% 覆盖**),**不需要打详情接口**,因而**不再被滑块验证阻塞**。搜索每轮顺路写当日快照(`xianyu_daily.source='search'`)。
> - **供需比 `ratio` = 想要总数 ÷ 在售条数**:高 = 想买的人多而供给少 → 蓝海。
> - `blue_ocean` / `red_ocean` **只收 ≥3 条在售的词**(1 条样本能刷出任何离谱比值)。`price_cut` 为该词下打了「累计降价」标签的在售条数(内卷程度)。
> - 深采(详情接口)仍存在,降级为**锦上添花**:多给收藏/出单/浏览量,受滑块限制、允许失败;其行标 `source='detail'`,**不被搜索快照覆盖**。

---

## 7. 抖音热点 · 内容词趋势

> 需要本地 `.env` 配置 `DOUHOT_COOKIE_FILE`(抖音热点宝授权 Cookie);采集为**纯 requests 直连**(接口只校验登录 Cookie,不需要签名参数,也不再依赖浏览器)。Cookie 失效时采集记录为 failed,原因为「热点宝 Cookie 已失效」。

### 7.1 获取最新内容词趋势

- **接口名称**: 获取抖音内容词趋势
- **请求方式**: GET
- **URL 路径**: `/api/v1/douhot/trends`
- **请求参数 (Query)**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `limit` | int | 否 | 30 | 返回条数上限 |
| `min_score` | float | 否 | 0 | 飙升指数下限过滤 |

**响应示例 (200)**
```json
{
  "count": 2,
  "items": [
    {
      "title": "景甜",
      "score": 50707012,
      "rising_ratio": 0,
      "trend_len": 24,
      "latest_value": 300,
      "trend_delta": 200,
      "query_day": "20260829",
      "created_at": "2026-08-31T00:47:01"
    }
  ]
}
```

### 7.2 手动触发一次采集

- **接口名称**: 手动触发抖音内容词趋势采集
- **请求方式**: POST
- **URL 路径**: `/api/v1/douhot/runs`
- **请求参数 (Body)**: 无

**响应示例 (202)**
```json
{ "run_id": "20260831004701", "count": 24, "items": [], "rising_count": 0 }
```

> 采集完成后会做跨轮判涨:命中(环比涨幅>阈值 且 斜率>0)的内容词会发邮件告警(带冷却去重),`rising_count` 为本轮判涨数。

### 7.3 关键词监控 · 智能体

> 用户可为**任意关键词**设置监控:抖音走**按关键词定向查询**(榜外的词也能取到专属热度);
> 微博/闲鱼/百度见 §7.3.1 四板块泛化,词须出现在榜内才记录。历史热度序列经算法分析后
> 给出**趋势判定 + 下一轮预测**。

**添加关注**

- **请求方式**: POST `/api/douhot/watch`
- **请求参数 (Body)**:

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `list_type` | str | 是 | `word`(内容词)/`search`/`video`/`topic`/`subscribe` |
| `keyword` | str | 是 | 要监控的关键词 |
| `filter_keyword` | str | 否 | 只保留标题含该词的主题(如"短剧";可空=不过滤) |
| `date_window` | int | 否 | 监控时段(小时):`1`/`24`/`72`/`168` = 近1小时/近1天/近3天/近7天(默认按榜单) |

**移除关注**: ⚠️ 不存在该接口(文档曾误标,见 §7.3.1 说明)。

**智能体分析**

- **请求方式**: GET `/api/douhot/watch-analytics`

**响应示例 (200)**
```json
[
  {
    "keyword": "世界杯",
    "list_type": "word",
    "last_score": 211,
    "rank_now": 0,
    "points": 1,
    "growth": 0.15,
    "trend_label": "上升期",
    "forecast_next": 305.2,
    "summary": "「世界杯」当前热度 211 环比 +15.0% 预测下一轮约 305 处于上升期,热度在走高,可关注",
    "series": [150, 180, 211],
    "slope": 35.1
  }
]
```

> `trend_label`:上升期/回落期/震荡/平稳;`forecast_next` 为线性外推的下一轮预测热度;

### 9b.x 抖音关键词多窗口对比(2026-09-08)

- **对比分析**: GET `/api/douhot/watch-windows`
  → `{"count":N,"items":[{"list_type","keyword","entry_title","h1_score","h24_score","ratio","label","signal","captured_at"}]}`
- **立即采集对比**: POST `/api/douhot/watch-windows/refresh`
  → `{"status":"success","words":N,"ok":N,"snaps":N,"pushed":M}`(skipped: `no_watch`/`no_cookie`)
- **任意词即查(不依赖监控词)**: POST `/api/douhot/windows/query` body `{"list_type":"video","keyword":"性格测试"}`
  → `{"status":"success","keyword","list_type","h1","h24","ratio","label","signal"}`(一次查询不落库)
- **原理**:同一监控词每轮**同时**拉近1h + 近1天热度(`DOUHOT_WINDOW_WINDOWS` 默认 `1,24`,可改
  `1,24,72,168`),各窗口记一条 `douhot_window_snap`;`ratio`=近1h/近1天,标签:
  🆕新起势(近1天冷近1h起)· 🔥爆发(≥1.5x)· 📉回落(<0.5x)· ➡️高位延续 · 冷启动
- **调度**:每 20 分钟 `douhot_window_tick`(`DOUHOT_WINDOW_CRON`),命中🆕/🔥/📉自动推抖音飞书群
- 独立 `douhot_window_snap` 表,不侵入单窗口 watch 链路
> `series` 为历史热度序列(供前端画迷你趋势线);`summary` 为自动生成的中文分析摘要。
> 样本 <2 时 `growth`/`forecast_next` 为 `null`(尚未积累足够数据)。
> ⚠️ 旧文档曾写有 "移除关注 DELETE `/api/douhot/watch`" ——**当前代码并无该 DELETE 接口**,请勿调用。

### 7.3.1 四板块关键词监控(v1.1 泛化)

> **2026-09-03 变更**:关键词监控从"仅抖音"泛化到 **微博 / 闲鱼 / 抖音 / 百度** 四个板块。
> 旧的 `/api/douhot/watch*` 三个接口保留(等价于 section=douhot),新接口按 `{section}` 寻址。
> 同一关键词可在**多个板块同时监控**(去重按 user+section+list_type+keyword,由代码承担,不再有 DB 唯一约束)。

**在某板块添加监控**

- **请求方式**: POST `/api/watch/{section}`(section ∈ `weibo` / `xianyu` / `douhot` / `baidu`;需登录)
- **请求参数 (Body)**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `keyword` | str | 是 | — | 要监控的关键词 |
| `filter_keyword` | str | 否 | `""` | **只保留标题命中该词的主题**(子串,大小写不敏感;为"短剧"时额外用短剧特征词兜底,覆盖"标题不含'短剧'二字但确实是短剧"的主题;每个关键词独立,默认空=不过滤)。如"只监控'完整版'里的短剧" → `keyword=完整版`、`filter_keyword=短剧` |
| `date_window` | int | 否 | 空 | 监控时段(小时):`1`/`24`/`72`/`168` = 近1小时/近1天/近3天/近7天(默认按榜单:搜索/视频/话题=近1小时,内容词=近1天) |
| `list_type` | str | 否 | `word` | 抖音可选 `word`/`search`/`video`/`topic`/`subscribe`;微博/闲鱼/百度固定 `word` |

**响应示例 (200)**
```json
{ "section": "weibo", "list_type": "word", "keyword": "世界杯" }
```

**列出某板块的关注词**: GET `/api/watch/{section}` → `[{"section","list_type","keyword"}, ...]`

**修改观测时段**

- **请求方式**: PATCH `/api/watch/{section}`(需登录)
- **请求参数 (Body)**:

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `keyword` | str | 是 | 已关注的关键词 |
| `list_type` | str | 是 | 原关注时的榜单类型 |
| `filter_keyword` | str | 否 | 原关注时的过滤词(默认空) |
| `date_window` | int | 是 | 新的观测时段(小时):`1`/`24`/`72`/`168` = 近1小时/近1天/近3天/近7天 |

**响应示例 (200)**
```json
{ "section": "weibo", "list_type": "word", "keyword": "世界杯", "filter_keyword": "", "date_window": 168 }
```

> 只改观测时段 date_window,关键词/过滤词/板块不变;找不到关注返回 404。

**某板块的智能体分析**

- **请求方式**: GET `/api/watch/{section}/analytics`(需登录)
- **请求参数**: 无

**响应示例 (200)**
```json
[
  {
    "section": "weibo",
    "keyword": "世界杯",
    "list_type": "word",
    "last_score": 2400000,
    "rank_now": 3,
    "points": 5,
    "growth": 0.15,
    "trend_label": "上升期",
    "forecast_next": 2760000,
    "summary": "「世界杯」当前热度 2400000 环比 +15.0% 预测下一轮约 2760000 处于上升期,热度在走高,可关注",
    "series": [1800000, 2000000, 2100000, 2200000, 2400000],
    "slope": 120000.5,
    "confidence": "高",
    "r2": 0.92,
    "accel": 30000.0,
    "burst": false
  }
]
```

> **快照记录规则**(决定词会不会有数据):微博/闲鱼/百度**每次采集后**从榜单里找该词记录快照
> (**词须出现在榜内某条**标题里**才命中**,榜外记 0;匹配为**标题包含子串**——大小写不敏感,
> 为适配闲鱼这类长标题/关键词堆砌板块,加"PS教程"能命中标题含"ps教程"的商品,取命中间排名最靠前那条的 score/rank);抖音 `word`/`search`/`video`/`topic` 四类均走**定向查询**
> (榜外也能查到专属热度,与热点宝官网输入关键词看到的一致)。其中抖音 `search`/`video`/`topic`
> 定向查询后把**搜出的每个相关主题各记一条快照**(`entry_title`=该主题标题,单次最多
> `DOUHOT_WATCH_ENTRY_CAP` 默认 **100** 条;话题榜实测可取满 100,搜索/视频受服务端上限约 50;
> 各条独立算趋势/预测,关注卡片逐条展示);`video` 为标题模糊/分词检索,`rank_now` 恒 0;
> `word` 内容词为**单值**(记该词热度/排名)。`subscribe`(我的订阅)**无 keyword 参数、不支持定向查询**,仍从榜单内查找。
> **2026-09-05 变更**:微博/闲鱼/百度关键词监控从"标题精确相等"改为"标题包含子串"匹配——此前短词/长标题板块几乎命中不了。
> **2026-09-04 变更**:定向查询由"仅抖音 `word`"扩展至 `word`/`search`/`video`/`topic` 四类
> (此前 `search`/`video`/`topic` 退化到全榜默认数据里找词,榜外记 0)。
> 闲鱼板块记录的 score 为该商品命中的关键词数(`hit_keywords`),数值量级与其他板块不同。
> 飞书的「智能体预警(预测爆发)」与日报【关键词关注】段落也已覆盖四个板块的全部关注词。

---


---

### 7.5 多平台智能体预测(微博/闲鱼)

- **接口名称**: 微博/闲鱼 热点智能体预测
- **请求方式**: GET
- **URL 路径**: `/api/platform-agent`(需登录)
- **请求参数**: 无

**响应示例 (200)**
```json
{
  "weibo": [ { "title": "冲榜词", "last_score": 2400, "growth": 0.5, "trend_label": "上升期",
               "forecast_next": 3100, "burst": false, "series": [1000,1200,1600,2400] } ],
  "xianyu": [ { "title": "教程", "last_score": 22, "growth": 0.57, "trend_label": "上升期",
                "forecast_next": 28, "burst": false, "series": [5,8,14,22] } ]
}
```

> 微博用热搜词的 `heat` 序列、闲鱼用商品的 `want_count` 序列(每日快照),喂给与抖音
> 同一套智能体算法,产出趋势 + 预测 + 爆发标记。样本 <2 时为空。

### 7.4 管理后台 · 智能体洞察(跨用户聚合)- **接口名称**: 智能体洞察聚合
- **请求方式**: GET
- **URL 路径**: `/api/admin/insights`(需 admin/operator,`data.view` 权限)
- **请求参数**: 无

**响应示例 (200)**
```json
{
  "stats": { "users": 2, "watchers": 1, "watch_keywords": 1, "burst": 1, "rising": 2, "today_alerts": 1 },
  "burst": [ { "keyword": "爆点", "user_id": 1, "trend_label": "上升期", "growth": 0.44, "forecast_next": 3050, "confidence": "高", "burst": true } ],
  "rising": [ { "keyword": "世界杯", "user_id": 1, "trend_label": "上升期", "growth": 0.15 } ],
  "hot_words": [ { "title": "黎巴嫩", "score": 5233450, "trend_delta": -1200 } ]
}
```

> 跨用户聚合每个关注词的智能体分析:**爆发榜**(可能爆发的词,按预测热度排序)、**上升期榜**、
> **抖音内容词 Top**。便于运维全局扫一眼哪些词值得跟进。

### 7.4b 采集健康度(运维一键看各平台状态)

- **接口名称**: 采集健康度
- **请求方式**: GET
- **URL 路径**: `/api/admin/health`(需 admin/operator,`logs.view` 权限)
- **请求参数**: 无
- **用途**: 聚合各平台最近一次采集、近 24h 运行/失败、最新数据写入、飞书推送统计、公众号监听在监面、Cookie 配置——免手查 MySQL。

**响应示例 (200)**
```json
{
  "generated_at": "2026-09-06T13:00:00",
  "platforms": {
    "douhot": { "last_run": "...", "last_status": "success", "last_detail": "ok", "runs_24h": 5, "failed_24h": 0 },
    "xianyu": { "last_run": "...", "last_status": "failed", "last_detail": "闲鱼人机验证(滑块),全部关键词均未采集", "runs_24h": 2, "failed_24h": 1 },
    "xianyu_deep": { "last_run": null, "last_status": null, "last_detail": null, "runs_24h": 0, "failed_24h": 0 },
    "wechat_listen": { "last_run": "...", "last_status": "partial", "last_detail": "accounts=81 new=0 failed=1", "runs_24h": 4, "failed_24h": 0 },
    "weibo": { "...": "..." }, "baidu": { "...": "..." }
  },
  "data": { "weibo": "...", "xianyu": null, "douhot": "...", "baidu": "...", "wechat": "..." },
  "wechat_monitor": { "benchmarks": 81, "users": 2, "articles_24h": 20,
                      "fixed_hours": "4:00 / 8:00 / 14:00 / 20:00", "next_point": "2026-09-22T14:00",
                      "pan_30d": 226, "transferred_30d": 202, "pending_30d": 0,
                      "dead_source_30d": 24, "quark_cookie_users": 1 },
  "feishu": { "pushes_by_section": [ {"section": "douhot", "count": 106}, {"section": "wechat", "count": 40} ], "last_push": "..." },
  "cookies": { "goofish": 1, "weread": 2, "quark": 1, "baidupan": 1 }
}
```
> `platforms` 每项为最近一次 `RunRecord`(采集运行)状态;`last_status=failed` 且 `last_detail` 含"滑块/限流"即闲鱼被风控。公众号的 kind 是 `wechat_listen`(定点作业,`runs_24h` 恒 ≤4 属正常)。
> `data` 各平台最新一条数据写入时间(为空=该平台从未进数据);`feishu` 为飞书推送计数/最近推送;`cookies` 为各平台已配 Cookie 的用户数(平台名含 `weread`/`quark`/`baidupan`/`dajiala` 等)。
> `wechat_monitor` 是公众号专属指标:`benchmarks` 全站在监对标号数、`users` 有在监号的用户数、`articles_24h` 近 24h 新收文章、`fixed_hours` 定点时刻、`next_point` 下一轮。
> 转存覆盖率(近 30 天带盘链文章,决定飞书标题点进去是"我方夸克链"还是公众号原文):`pan_30d` 带盘链文章数、`transferred_30d` 已换成我方夸克链、`pending_30d` 仍在补转存队列、`dead_source_30d` 源分享被封(41031)永久转不了、`quark_cookie_users` 配了夸克 Cookie 的用户数(为 0 时 `pending` 只增不减)。



- **接口名称**: 某板块的独立页数据(最新榜单 + 每词智能体趋势/预测)
- **请求方式**: GET
- **URL 路径**: `/api/platform/{platform}`(platform ∈ `weibo` / `xianyu` / `douhot` / `baidu`;需登录)
- **请求参数**: 无

**响应示例 (200)**
```json
{
  "platform": "baidu",
  "count": 3,
  "items": [
    { "name": "新词", "score": 12345, "trend_label": "上升期",
      "growth": 0.25, "forecast_next": 15000, "burst": false, "points": 5 }
  ]
}
```

> 每个板块独立页调用此接口;`trend_label`/`growth`/`forecast_next`/`burst` 为智能体对
> 该词历史序列的分析(需 ≥2 轮采集才有)。**百度为公开接口,无需 Cookie**;微博/闲鱼/抖音需各自 Cookie。

### 7.7 抖音子榜单(热点宝式 tab)

- **接口名称**: 实时拉取抖音某个子榜(或按词定向搜索)
- **请求方式**: GET
- **URL 路径**: `/api/douhot/list/{list_type}`(list_type ∈ `word`/`search`/`video`/`topic`/`subscribe`;需登录)
- **请求参数 (Query)**:

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `keyword` | str | 否 | 非空时**按词定向搜索**,返回过滤后的目标条目(榜外词也能查到);`word`/`search`/`video`/`topic` 支持,`subscribe` 不支持(传了 400) |
| `filter_keyword` | str | 否 | 非空时**只保留标题命中该词的主题**(子串,大小写不敏感;为"短剧"时额外用短剧特征词兜底,覆盖"标题不含'短剧'二字但确实是短剧"的标题)。如"完整版"里只留短剧 → `keyword=完整版`、`filter_keyword=短剧` |
| `date_window` | int | 否 | 统计时段(小时):`1`/`24`/`72`/`168` = 近1小时/近1天/近3天/近7天(默认按榜单);`subscribe` 忽略 |

**响应示例 (200,普通全榜)**
```json
{ "list_type": "search", "items": [ { "title": "关键词", "score": 121852270 } ] }
```

**响应示例 (200,按词搜索 `keyword=续火花`)**
```json
{ "list_type": "topic", "keyword": "续火花", "items": [ { "title": "续火花", "score": 15744747 } ] }
```

> 顶部 tab 对应 内容词榜/搜索榜/视频榜/话题榜/订阅;**2026-09-05 变更**:榜可**按词搜索**——
> 输入关键词即定向查该词在榜内的热度(榜外词也能取到),与监控快照的定向查询同源。
> **2026-09-27 变更**:副榜(search/video/topic/subscribe)拉取失败时返回 **502** 而不是 `items: []`
> —— 空列表和"这个榜今天真是空的"同形,前端会渲染成"暂无数据"、被读成"热点冷掉了"
> (采集侧同理:`run_douhot` 把降级的副榜记进 `lists_degraded` 并落 partial)。
> 需先在该账号「Cookie 管理」配好抖音 Cookie。

### 7.8 跨平台共同上升

- **接口名称**: 找出在 ≥2 个板块同处"上升期"的关键词
- **请求方式**: GET
- **URL 路径**: `/api/cross/rising`(需登录)
- **请求参数**: 无

**响应示例 (200)**
```json
[
  { "keyword": "世界杯", "platforms": ["weibo", "baidu"],
    "forecasts": { "weibo": 3100, "baidu": 5200 }, "burst": false, "avg_forecast": 4150 }
]
```

> `platforms` 为同处上升期的板块。**采集后会自动推送命中词到飞书**(`cross_up` 去重,
> 冷却内不重发);需各板块均有 ≥2 轮采集数据才可能命中。

---
## 8. 采集频率(每用户自定义)

> 需登录(JWT)。每个用户可为 **微博 / 闲鱼 / 抖音 / 百度** 四个板块**分别**设置多久采集一次。
> 调度器每分钟检查一次到期任务,改完**下一分钟即生效**(无需重启)。
> 未配置对应平台 Cookie 的板块会被自动跳过(不采集、也不产生失败记录)。

### 8.1 获取当前用户的采集频率

- **接口名称**: 获取采集频率设置
- **请求方式**: GET
- **URL 路径**: `/api/schedules`
- **请求参数**: 无(用户由 JWT 标识)

**响应示例 (200)**
```json
{
  "choices": [10, 30, 60, 180, 360, 720, 1440],
  "min_interval": 10,
  "items": [
    {
      "section": "douhot",
      "label": "抖音热点",
      "interval_minutes": 10,
      "enabled": true,
      "cookie_ready": true,
      "last_run_at": "2026-09-01 21:10:35",
      "next_run_at": "2026-09-01 21:20:35",
      "fixed_hours": ""
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `choices` | 建议档位(分钟);后端不限定只能取这些值,但会强制 `min_interval` 下限 |
| `min_interval` | 最小间隔(分钟),默认 10;低于该值返回 400 |
| `cookie_ready` | 该板块是否已配好 Cookie;为 `false` 时不会采集 |
| `enabled` | 是否启用该板块的定时采集;停用时 `next_run_at` 为 `null` |
| `fixed_hours` | **定点作业板块**(公众号监听)的触发时刻,如 `"4:00 / 8:00 / 14:00 / 20:00"`。此时 `interval_minutes` **不参与调度**(抢占走 `force=True`),`next_run_at` 也按下一个定点而非"上次+间隔"计算;其它板块恒为 `""` |

### 8.2 设置某板块的采集频率

- **接口名称**: 设置采集频率
- **请求方式**: PUT
- **URL 路径**: `/api/schedules/{section}`(`section` ∈ `weibo` / `xianyu` / `douhot` / `baidu`)
- **请求参数 (Body)**: 两个字段均可单独提交

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `interval_minutes` | int | 否 | 采集间隔(分钟),范围 10~1440 |
| `enabled` | bool | 否 | 是否启用该板块定时采集 |

**请求示例**
```json
{ "interval_minutes": 10 }
```

**响应示例 (200)**:同 8.1 的单个 `items` 元素。

**错误响应 (400)**
```json
{ "detail": "采集间隔不能小于 10 分钟(防止触发平台风控)" }
```

> 下限校验在**后端**强制执行(前端限制不可信),防止把三方接口打爆导致风控或 Cookie 失效。

### 8.3 推送时段表(2026-10-01)

> 取代原先写死在 `settings` 里的 7 条推送 cron。调度器**每分钟**比对一次"当前时刻是否命中某类推送",
> 所以改完**立即生效**(不必重启,也不必重建调度作业)。

- **接口名称**: 获取推送时段 / 保存推送时段
- **请求方式**: GET / PUT
- **URL 路径**: `/api/push-timeline`
- **请求参数 (Body)**: 仅 PUT —— 整表覆盖

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kinds` | array | 是 | 推送类型列表,每项 `{key, times, days, enabled}` |
| `kinds[].key` | string | 是 | `daily`/`hotrank`/`analysis`/`agent`/`insight`/`review`/`weekly` |
| `kinds[].times` | string[] | 是 | 发送时刻 `HH:MM`;**清空即停发该类** |
| `kinds[].days` | int[] | 是 | 生效星期,**POSIX 口径 0=周日 … 6=周六**;`[0..6]`=每天 |
| `kinds[].enabled` | bool | 否 | 总开关(默认 true);`times` 为空时后端强制置 false |

**请求示例**
```json
{ "kinds": [ { "key": "daily", "times": ["08:00"], "days": [1, 2, 3, 4, 5], "enabled": true } ] }
```

**响应示例 (200)**
```json
{ "kinds": [
  { "key": "daily", "label": "热点日报", "times": ["08:00"],
    "days": [1, 2, 3, 4, 5], "enabled": true },
  { "key": "hotrank", "label": "多平台热榜速览", "times": ["09:30", "21:30"],
    "days": [0, 1, 2, 3, 4, 5, 6], "enabled": true }
] }
```

> **非法值只丢它自己**,不报废整表:写个 `25:00` 或星期 `9` 会被剔除,其余照常保存。
> 推送内容不因此丢失——采集照旧,只是发送时刻改了。
> 单类推送执行失败**不重试**也不影响同一分钟的其他类:这类都是"当期内容",晚一分钟补发没有意义。

## 9. 公众号 · 内容选题分析

> 需登录(JWT)。手动录入公众号文章后,按 **标题/内容/作者/发布时间** 跑内容选题分析(选题分布、标题风格、发布时段、对标号对比 + 选题建议)。
> 注:当前为**内容选题视角**的规则建议,非流量归因;等接入带阅读量的第三方 API 后可升级。

### 9.1 录入一篇公众号文章

- **请求方式**: POST `/api/wechat/articles`
- **请求参数 (Body)**:

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `title` | str | 是 | 文章标题 |
| `author` | str | 否 | 公众号名(对标号),空 = 未知 |
| `content` | str | 否 | 正文/摘要 |
| `url` | str | 否 | 文章链接 |
| `publish_at` | str | 否 | 发布时间(ISO 或 `YYYY-MM-DD HH:MM`) |

**响应示例 (200)**: `{ "ok": true, "title": "揭秘AI副业3个方法" }`

### 9.2 内容选题分析

- **请求方式**: GET `/api/wechat/analyze?limit=200`(默认 200,上限 500)
- **请求参数**: 无(用户由 JWT 标识)

**响应示例 (200)**
```json
{
  "articles": 3,
  "count": 3,
  "topics": [ { "word": "副业", "count": 3 } ],
  "title_style": { "avg_len": 12.3, "num_pct": 0.67, "emoji_pct": 0.0, "question_pct": 0.0, "hook_pct": 1.0, "top_words": ["副业"] },
  "publish": { "count": 3, "by_hour": { "9": 1, "20": 1, "21": 1 }, "peak_hours": [9, 20, 21] },
  "authors": [ { "author": "科技君", "count": 2, "top_topics": ["副业"], "avg_title_len": 14.0 } ],
  "suggestions": [ "近期选题主线集中在「副业」,可围绕它深挖/做系列" ],
  "summary": "共 3 篇;选题主线「副业」;平均标题 12 字,吸引词占比 100%;发布高峰 21点。"
}
```

> `topics` 为轻量中文 2 字窗口词频(选题主线);`title_style` 含标题长度/数字/emoji/疑问/悬念词占比与高频词;
> `publish` 为发布时段分布与高峰;`authors` 为各公众号对比;`suggestions`/`summary` 为中文分析结论。

---

## 9b. 公众号 · 对标号监听与同步(2026-09-07 建,2026-09-30 起 dajiala 链路已摘除,纯免费源)

> 前置:配置 `DAJIALA_KEY`(付费接口按次扣费,监听 ¥0.14/号/次,同步 ¥0.14/页,阅读量 ¥0.06/次);
> 未配置时所有接口返回 `{"status":"skipped","reason":"no_key"}` 或 400。
> 监听频率走「采集频率」页的**公众号监听**板块(默认 6 小时/次);新文自动推公众号专属飞书群。

### 9b.1 对标号列表

- **请求方式**: GET `/api/wechat/benchmarks`

**响应示例 (200)**
```json
{ "count": 1, "items": [ { "id": 1, "nickname": "微信派", "ghid": "gh_bc5ec2ee663f",
  "anchor_url": "https://mp.weixin.qq.com/s/xxx", "note": "", "active": true,
  "miss_count": 0, "last_item_at": "2026-09-07 12:00:00", "has_articles": true } ] }
```

### 9b.2 加对标号(贴该号任意一篇**文章链接**即加,免费;链接即监听锚点)

- **请求方式**: POST `/api/wechat/benchmarks`
- **请求体**: `{ "url": "https://mp.weixin.qq.com/s/xxx", "nickname": "可选", "note": "可选" }`
- 配置了 key 时自动解析昵称/ghid(key 无余额不挡加号);重复链接返回 400。
- **不拿文章页的 `__biz` 当订阅 id**(2026-09-27):URL 里解出的是 base64,不是免费列表源认识的
  `MP_WXS_*`,写进去只会让监听误以为"这个号已配好列表源"却每轮撞空列表。
- 配了 WeRSS 时,加号会**按解析出的昵称**顺手查一次订阅(`kw` 搜索,一次请求):唯一同名命中
  就直接填进 `biz`,接上免费全量列表;0 个或重名一律不猜,改由 `scripts/werss_backfill_biz.py` 处理。
- 若本轮真的接上了订阅 id,接口返回后会在**后台**催 WeRSS 立刻抓一次新订阅
  (同步抓取十几秒,故不占本接口耗时;失败只记日志,WeRSS 自己的定时迟早补上)。

### 9b.3 更新/删除对标号

- PATCH `/api/wechat/benchmarks/{id}`:body `{"active": false}` 停用(暂停监听,不删数据)
- DELETE `/api/wechat/benchmarks/{id}`:删除(已入库文章保留)

### 9b.4 一键同步该号全部/近期文章

- **请求方式**: POST `/api/wechat/benchmarks/{id}/sync?max_pages=3`
- 每页约 10 次发文(¥0.14/页),`max_pages` 缺省为 `WECHAT_SYNC_MAX_PAGES`(3);翻到 `IsEnd` 提前停止
- **入库之后当场"转存 → 推送"**(2026-09-23):新入库文章先按盘链去重(同一资源只留一篇,
  更早入库过的链也不再推),再走夸克/百度转存换我方分享链,最后推飞书卡片 → 点文章名直接进我的盘。
  同步在 HTTP 请求里,故 ① 不做即时采样(`allow_paid=False`,阅读量交给采样作业);
  ② 单轮窗口 `WECHAT_SYNC_PUSH_LIMIT`(默认 20)篇,资源文优先,**但近 24h 内发的文章一律先占满窗口、
  不受封顶限制**(封顶只砍 24h 之前的历史补采文,见"近24h全推"铁律);
  ③ 不叠加补转存队列(`run_backfill=False`),避免一次点击多打几十次夸克接口
- **响应示例**: `{ "platform":"wechat_sync", "status":"success", "pages":2, "new":17, "ghid":"gh_xxx",
  "nickname":"微信派", "pushed":12, "transferred":9, "deduped":3, "truncated":2 }`
  (`deduped`=同链接被并掉的篇数,`truncated`=超出单轮窗口未推的篇数,**窗口没排满时是 0 而不是负数**;
  运行记录 detail 同样带这三个数)
- 无 dajiala key 时走微信读书免费源:**先枚举 `mp/articles`(近 3 天,含同一天群发的第 2、3 篇),
  列表被服务端限权(-2041)才退化到 cover 最新一篇** → `status:"partial"` +
  `reason:"weread_list_limited_latest_only"`(或 `weread_list_error_latest_only`),响应带
  `weread_list`(ok/limited/error)与 `items`(本次枚举到的篇数)。
  `wr_skey` 过期会**先自动续期一次**再重试(续期后的新会话正是列表可用窗口)——列不出来的那条分支
  也一样:撞 `-2012/-2010` 必须抛给上层触发续期,不能记成"列表接口异常"继续走单篇(那样 Cookie 死了
  既不续期也不报警)。列表条目没带 `reviewId` 时回落用 cover 的 `reviewId` 取正文,否则该篇正文为空、
  链抽不到(卡片上就是一根 `—`)
- **错误**:404 对标号不存在;502 上游失效——detail 是可执行文案(微信读书登录态失效→去「Cookie 管理」换含
  `wr_rt=` 的 Cookie;dajiala 欠费/风控原文)。前端只把 **502 的 detail 展示给用户**,500 仍显示统一
  "服务器开小差了"(500 的 detail 可能夹带异常堆栈,不外露)

### 9b.5 监听(手动触发;定时走调度器"公众号监听"板块)

- **请求方式**: POST `/api/wechat/listen`
- 行为:每个启用中的对标号查一次"当天发文"(`post_condition`,¥0.14/号)→ 新文按链接去重入库
  (`wechat_articles.source='listen'`)→ 标题含网盘词的**免费自抓正文**,按四家盘链正则
  (pan.quark.cn / pan.baidu.com / drive.uc.cn / pan.xunlei.com)标记 `pan_types` → 新文推公众号专属飞书群
- **正文取哪一路**(决定网盘列是不是 `—`):微信读书的 cover/列表条目**自带 `reviewId`**,监听先直抓
  `mp.weixin.qq.com`,**返回空(风控/JS 壳页)时改用本篇 `reviewId` 走微信读书转发页** `mp_content`
  (与「同步文章」同源,每次过一次 2s 类级节流);直抓成功则不追打。两路都拿不到正文 → 认不出盘链 →
  卡片 `—`(文章照样推,见 §4d 运维排查)
- 余额保护:开始前查余额(免费),低于 `DAJIALA_MIN_BALANCE` 时**仅禁用 dajiala 付费源**
  (微信读书/读书平台等免费源照常监听,响应带 `dajiala_skipped:"low_balance"`+`balance`;
  监听中途欠费同样只停付费,运行记录 detail 记 `dajiala_off(...)`)
- **响应示例**: `{ "platform":"wechat", "status":"success", "accounts":2, "new":5, "failed":0 }`
  余额不足时: `{ "platform":"wechat", "status":"success", "accounts":2, "new":1, "failed":0,
  "dajiala_skipped":"low_balance", "balance":0.02 }`;本轮开头补推了上一轮欠推的文章时多一个 `repushed:N`
- **书架粗筛(2026-09-27)**:微信读书源问 cover 前先发 1 次 `/web/shelf/sync`,书架
  `lastChapterCreateTime`(最新文章发布时间)与该号水位一致的号直接跳过(省白问,判据与
  失效保护见 doc/operations.md §9.2)。粗筛生效时响应多一个键:
  `"weread_shelf": {"signals":81, "skip":60, "force":3, "advanced":5}`
  (signals=认出信号字段的号数 / skip=本轮跳过 / force=强制问询 / advanced=水位前移号数),
  停用时不出现该键,运行记录 detail 以 `shelf(off=原因)` 记录停用原因
- **封文识别与版权清扫预警(2026-09-28)**:正文抓取命中微信封禁页(账号封禁/作者删除/
  违规处理)不再当正文入库,卡片网盘列显示 `⛔原文失效`(网盘列图例同步);
  同轮 ≥2 篇被投诉下架 → 飞书群推「⚠️ 疑似版权清扫」预警(单篇仅站内记录)。
  微信封禁页不区分投诉人,仅透传违规类别(侵权投诉=版权/商标/专利通道,批量出现
  即版权方清扫的典型特征,文案如实标「疑似」)。响应多一个键 `banned`(本轮下架篇数)
- **卡片内容(2026-09-27 升级)**:① cover 文章的 `publish_at` 用书架时间戳补齐(封面文=
  该号最新一篇,书架时间即其发布时间;此前恒空);② 卡头汇总带资源概览
  ("其中 X 篇带网盘资源");③ 账号标题行带篇数(`📢 号名 · N 篇`);④ 旧文标题带
  `·MM-DD` 时效标注(发布日=今天不标;补采/同步/迟到补推的旧文一眼可辨,盘链可能已失效)
- **并发防重(2026-09-26)**: 同一用户同时只跑一轮(在跑标记落 `system_config` 的
  `wechat_listen_running_<uid>`),手动点击撞上定时轮/失败重试时返回
  `{ "platform":"wechat", "status":"skipped", "reason":"running" }`,零采集副作用;
  标记超过 `WECHAT_LISTEN_LOCK_TTL_MINUTES`(默认 20 分钟)视为持有进程已被杀,可被下一轮接管
- **欠推补偿(2026-09-26)**: `wechat_articles.pushed_at` 只在文章**真的**进过飞书卡片时盖上,
  窗口内(`WECHAT_REPUSH_WINDOW_HOURS`)为 NULL 的 listen/sync 文章由下一轮监听开头补发一张
  `⏰ 补推 · 公众号监听 …` 卡;补推仍未送达 → `notify_incident(push_feishu=False)` **只记站内告警**
  ("飞书坏了"的告警发飞书是发不出去的;站内 = `/api/alerts/list`「最近预警」/管理端用户详情/告警 CSV)
- **近 24h 全推(2026-09-26 定的铁律)**:被监控号近 24 小时发的文章必须**一篇不落**推到飞书
  (飞书是员工看新发文的唯一入口)。监听侧本来就没有截断——采到的新文全推;唯一能不能兑现取决于
  **微信读书"近期列表"这一轮是否可枚举**:可枚举时同一天群发的第 2、3 篇一起进来;被限权(-2041)时
  只剩 cover 最新一篇,同日其它篇属**未知丢失**。为此每轮把可枚举性写进运行记录
  (`detail` 里的 `weread_list(ok=可枚举数 off=列不出且无新文 off_with_new=列不出却有新文)`),
  响应同步返回 `weread_list` 计数;只要 `off_with_new>0` 就记一条
  `⚠️ 微信读书只能拿到最新一篇,同日其它篇可能漏推`(**只进站内告警,不发飞书**,
  见 `doc/operations.md` §4f;标题不带数字以便冷却去重,本轮计数写在正文),
  并给出两条根治路径:① 自建 WeRSS 并把订阅 id 回填进对标号 `biz`(免费全量列表,见 §9b.6 与
  `doc/operations.md` §4g);② dajiala 充值走
  `history_by_ghid`(付费)。临时缓解:对高产号多点「同步文章」(它在列表可用时会补同日兄弟篇)

- **飞书推送格式**(wide_screen 网格卡,四列对齐:**公众号 / 文章 / 网盘 / 阅读**):
  文章标题即超链接,优先级 **本轮转存链(附 `🔑提取码`)> 历史我方链 > 公众号原文**;
  网盘列用图标交代点进去是谁的链:`🔴`=我方转存链、`⏳待转存`=有源链还没转好(点开是原文)、
  `⛔源失效`=对方分享已被封(41031,永久转不了)、`—`=**这篇本身不带网盘链**(标题点进去就是公众号原文)。
  **没认出链接的文章照样推**(2026-09-26 用户明确"找不到链接的也必须推送名称"):入库侧所有监听/同步分支
  都是 `require_pan=False`,"不是资源"≠"不用告诉员工";卡片头一行 note 也写明了 `—` 的含义。
  同盘链已被别的文章带过 → `🔥xN`。
  监听与「同步文章」共用这一张卡(见 9b.4)。每卡 20 篇,超出继续发卡,不截断。

- **正文抓取与盘链识别**(2026-09-26,决定"网盘"列是 `—` 还是有链):标题命中 `TITLE_HINTS`(盘商名 +
  引流词"入口/地址/自取/领取/模板/线稿/电子版/答案/教程/壁纸/pdf"…)才花一次自抓;正文解析走
  `app/utils/html_text`,**超链接锚文本后会附上真 URL**(`点此保存 https://pan.quark.cn/s/xxx`),并把左下角
  「阅读原文」的跳转地址(`msg_source_url`,含 `redirect?url=` 包跳还原)拼在末尾——资源号极少把盘链写成明文,
  多数就在这两处。拿不到 `#js_content` 容器 = 出口 IP 被微信挡回 JS 壳页,**判为抓取失败返回空串**
  (旧实现把整页脚本当正文入库,库里十几 KB JS 且伪装成"抓到了")
- **`pan_urls` 列回填**:每轮 `_enrich_new_articles` 开头调 `_backfill_pan_urls(user_id, limit=100)`,
  把"正文里明明有夸克/百度链、`pan_urls` 却为空"的历史行补上(重算 `pan_urls`/`pan_types` + 写归一化表
  `wechat_pan_links`),它们才会进补转存队列。这类行是"百度链提取晚于入库"留下的,不补就永远是一根 `—`。
  回填失败只记日志,不拖垮本轮监听
- 兜底缺口(需付费/节流,未自动开启):自抓被风控的文章可用 dajiala `article_detail`(¥0.01/次)或
  微信读书 `mp_content(reviewId)` 转发页取正文,两者都有频控风险,待运营确认

### 9b.6 文章列表(支持盘链过滤)

- **请求方式**: GET `/api/wechat/articles?limit=100&has_pan=1&benchmark_id=1`
- **响应**: `{"count":N,"items":[{"id","author","title","url","content","publish_at","source"(manual/listen/sync),"pan_types","benchmark_id","created_at"}]}`

### 9b.8 AI 改写 / 运行状态(2026-09-09)

- **AI 改写**: POST `/api/wechat/articles/{id}/rewrite` → `{title, content, my_link}`(DeepSeek 改写为原创可发布稿,≈¥0.01/篇;正文不足自动补抓)
- **key 多租户**:dajiala key 可在「Cookie 管理」按用户配置(dajiala 平台),未配置回落全局 `DAJIALA_KEY`——多用户余额隔离
- **网盘 Cookie 多租户**:夸克(`quark`)、百度网盘(`baidupan`)同样在「Cookie 管理」按用户配置,
  夸克未配置时回落全局 `QUARK_COOKIE`(百度盘无全局默认值);识别到盘链却无可用 Cookie、或 Cookie 中途失效时,
  各推一条冷却去重的飞书告警(不再静默显示"未转存")。**夸克与百度两套告警对等**:缺 Cookie、鉴权失败即时点名,
  另有每日 07:00 `pan_cookie_keepalive_tick` 逐用户主动巡检(同一份 Cookie 只探一次)
- **转存覆盖面**:盘链**识别**四家(夸克/百度/UC/迅雷),**自动转存换链**只有夸克 + 百度;
  UC/迅雷只落 `pan_types` 标签,卡片上仍是 `—`
- **运行状态**: GET `/api/wechat/status` → `{benchmarks, new_24h, pan_articles, burst, candidates}`
- `articles` 列表新增字段:`quality`(质量分 0~10)、`my_pan_urls`(转存后的自己的链接)、`read_num` 等流量字段

> 文章来源标记:`manual` 手动录入 / `listen` 监听新文 / `sync` 历史同步;`pan_types` 为涉及的网盘类型
> (标题=盘名疑似级,自抓正文命中链接=确认级),逗号分隔,如 `"夸克网盘,百度网盘"`。

### 9b.8 候选对标号自动发现 + 自动收录(2026-09-07 发现 / 2026-10-01 收录闭环)

- **候选列表**: GET `/api/wechat/candidates`
  → `{"count":N,"items":[{"id","name","title","term","status"(new/dismissed/imported),"imported","title_ts","discovered_at"}]}`
  (`imported`=该公众号名已收录为正式对标号)
- **手动发现一轮**: POST `/api/wechat/candidates/discover`
  → `{"platform":"wechat","status":"success","terms":[搜索词],"new":新候选数,"blocked":被搜狗验证码拦截的词数}`
- **更新候选状态**: PATCH `/api/wechat/candidates/{id}` body: `{"status":"dismissed"}`(忽略)或 `{"status":"new"}`
- **预览可收录清单**: GET `/api/wechat/candidates/importable?limit=200`
  → `{"count":N,"items":[{"id","name","title","term","reason","accounts"}]}`
  (`reason`=`资源号` 或 `资源库N号验证`,`accounts`=该来源词的资源被多少个对标号发过)
- **一键自动收录**: POST `/api/wechat/candidates/auto-import` body: `{"limit":8}`(省略或 ≤0 用配置闸门)
  → `{"picked","imported","listenable","items":[{id,name,status,reason,listenable,hint}]}`
- **批量收录选中**: POST `/api/wechat/candidates/import-batch` body: `{"ids":[1,2,3]}`
  → `{"count","ok","listenable","items":[{id,status,nickname,listenable,hint}]}`
- **批量忽略选中**: POST `/api/wechat/candidates/dismiss-batch` body: `{"ids":[1,2,3]}`
  → `{"count","dismissed"}`
- **单个收录**: POST `/api/wechat/candidates/{id}/import`
  → `{"status":"ok","benchmark_id","nickname","listenable","created","hint"}`
  (`listenable=false` 时 `hint` 说明原因;HTTP 400 为 `failed`)

- **原理(全免费)**:搜索词 = 标题书名号/【】实体词(《乡村晋升录》《花少2人格》等,最准)
  + 滑窗高频内容词(兜底)+ `CANDIDATE_SEARCH_TERMS` 配置词 → 搜狗微信搜索(免账号,内置 2.5s 限频,
  连续 2 词命中验证码即收手)→ 按**公众号名**与现有对标号/已存在候选去重入库 → 推公众号专属飞书群
- **收录闭环(2026-10-01 补全)**:候选 → `POST /api/wechat/candidates/{id}/import` →
  按号名搜 WeRSS 全量库(`GET /api/v1/wx/mps/search/{名称}`)→ **精确同名**才订阅
  (`POST /api/v1/wx/mps`)→ 拿回 `MP_WXS_*` **同时写进对标号的 `biz` 与 `weread_book_id`** →
  下一轮监听自动抓它的文章。**不需要**再去微信读书关注 + 书架导入。
  - **两列同源**:WeRSS 订阅 id 是 `MP_WXS_<base64解码(fakeid)>`,与微信读书的 `bookId`
    是**同一个编号**——老号 142 个当初就是这么导进 WeRSS 的。所以订阅成功后把同一个串写进
    两列,该号立刻可走微信读书链路;只填 `biz` 是不行的:WeRSS 当前抓不到东西(上游
    `appmsgpublish` 限流 200013,2026-09-30 起未恢复),那样的号是哑号,实测踩过。
  - 微信读书的公众号数据**不要求用户已关注**该号:`mp_cover(bookId)` 在未关注的号上照样返回
    号名/头像/最新一篇(已实测),故收录即生效。
  - 搜狗结果只有公众号名、没有文章链接(其 `/link?url=` 是 JS 二次跳转),故收录走"按名"而非"按链接"
  - 订阅池里已有同名号时零副作用直接接上(不再重复调加订阅接口——那会触发一次历史抓取)
  - 重名歧义(搜到多个同名)与形近名一律不订阅,号先建着并提示人工处理
  - 订阅不上的候选保留 `new` 重试,试满 `_IMPORT_MAX_TRIES`(3)次转 `dismissed`,不长期霸占收录名额
- **自动收录标准**(按需扩展):① LLM 判为**资源号**;② **它发的资源已被
  ≥`CANDIDATE_AUTO_IMPORT_MIN_ACCOUNTS` 个对标号验证过**(同链多号同发 = 需求坐实)
- **数量闸门**:单轮最多 `CANDIDATE_AUTO_IMPORT_MAX`(默认 8)个——WeRSS 加订阅会顺带排一次历史抓取,
  一口气灌几百个号会把抓取队列压垮
- 每日自动:发现(默认 08:20,`CANDIDATE_DISCOVER_CRON`)→ 收录(默认 08:30,`CANDIDATE_AUTO_IMPORT_CRON`);
  配置:`CANDIDATE_SEARCH_TERMS`(词,逗号分隔)、`CANDIDATE_MAX_TERMS`(单轮词数上限,默认 8)、
  `CANDIDATE_MINE_TERMS`(画像词上限,默认 6)、`CANDIDATE_AUTO_IMPORT`(总开关)、
  `CANDIDATE_AUTO_IMPORT_MAX`(单轮闸门)、`CANDIDATE_AUTO_IMPORT_MIN_ACCOUNTS`(验证阈值)


### 9b.7 微信读书免费源(2026-09-07)

- **书架预览**: GET `/api/wechat/weread/shelf` → `{"count":N,"items":[{"book_id":"MP_WXS_*","name":"公众号名"}]}`
- **书架一键导入**: POST `/api/wechat/benchmarks/import_shelf`
  → `{"status":"success","shelf":N,"created":新增,"updated":回填bookId}`(重复导入幂等)
- **Cookie 续期**: POST `/api/wechat/weread/refresh`
  → `{"status":"success","verified":true}` / `{"status":"skipped","reason":"no_cookie|no_rt"}`;
  失败 HTTP 502(wr_rt 已失效,需重新扫码/复制完整 Cookie)
- 前置:在微信读书 App 内关注目标公众号,并配置微信读书 Cookie(平台「Cookie管理」新增的
  **weread** 平台,按用户;或 `.env` 全局 `WEREAD_COOKIE`)
- 数据源优先级:对标号有 `weread_book_id` 且有 Cookie → 微信读书(免费,每轮拿"最新一篇"+ **会话初期可用时
  枚举近 3 天列表**);否则 dajiala `post_condition`(¥0.14/号)。微信读书登录失效(-2012/-2010)→ **自动续期重试一次**
  (wr_rt 换新 wr_skey 并回写存储),续期失败才降级 dajiala。
- **补采窗口**:`renewal` 换出新会话后 `mp/articles`(历史列表)才可用,调度作业 `run_full_sync_if_pending`
  趁这个窗口对各号做一轮全量补采;一旦列表返回 -2041(该会话预算耗尽)就**立即中止本轮**——其余号只会各撞一次
  并退化成"最新一篇",白烧调用密度;pending 标记保留,下个新会话再补。
- **自动续期**:每日调度作业(默认 07:50,`WEREAD_REFRESH_CRON`)为全部用户续期;
  wr_skey 短效且轮换,续期后旧值自动失效,服务端已回写新值,用户无需手动更换 Cookie。
- 监听/同步的其余行为不变;`sync` 无 dajiala key 时优先枚举 `mp/articles`,只有列表被限权才退化为"最新一篇"
  (返回 `partial` + `weread_list`)。
- **免费全量列表源(两家可择一,凭据都在 `.env`)**:
  - **WeRSS(自建,2026-09-27 起现役首选)**:配 `WECHAT_WERSS_URL/AK/SK`(AK 在 WeRSS 后台
    「Access Key 管理」创建,Secret 只显示一次)即启用,`app/services/werss_client.py`。
    它的订阅 id 与我们书架导入的号没有共同标识体系,故要跑一次
    `python scripts/werss_backfill_biz.py [--apply]` **按公众号名称**把 id 回填进 `biz`
    (重名/找不到的只报告不猜)。部署步骤见 `doc/operations.md` §4g。
  - **读书平台(wewe-rss v2 兼容)**:配 `WECHAT_READER_PLATFORM_URL/TOKEN/VID` 即启用;
    ⚠️ 其公共实例已于 2026-07 停服,除非自建同构实例否则留空。
  两者都提供"每号最新一页(≤100 篇)"的全量列表,同步可翻页拉历史(免费)。
  优先级:**WeRSS → 读书平台 → 微信读书 cover(只最新一篇)→ dajiala(付费)**;
  对标号列表的 `biz` 字段就是喂给这两个源的公众号标识。

---

## 10. 夸克 · 分享统计(**已停用并删除端点**,2026-10-03)

> ⚠️ 本节原来的两个端点 `POST /api/quark/shares/collect`、`GET /api/quark/shares`
> **已连同 `app/api/quark.py` 一起删除**(用户决定"删端点、保留服务与只读探测脚本")。
> **为什么**:这是"废弃链只摘了一半"的第二例(第一例是 `wechat/traffic/refresh`)——
>   ① **采集口零触发方**:前端没有对应方法(2026-10-03 随 10 个死方法清掉),调度器里
>      也**没有任何 quark 作业**,生产库 `quark_share_stats` 表因此**冻结在 2026-09-29 13:01**;
>   ② **查询口零展示方**:没有任何页面读它;
>   ③ **结算早已改道**:`settle_suggestions` 2026-09-29 起用 **`repost_gain`(盘链扩散)**
>      做主信号,总账走**方案B 人工拉新周录**(见 §11、§21),**不再读这张表**。
> 根因是 2026-09-29 的定案:夸克官方「分享管理」**不提供链接级转存数**,
> `save_pv/click_pv=-1` 是平台不对外、不是"有开关没打开"。
>
> **保留物**(有价值、零维护成本):
>   - `app/services/quark_share_stats.py` —— 免签名直连 `share/update_list` 的接口细节
>     (`share_read_statues=[0]` 必带、`fr` 参与鉴权等)都写在模块说明里;
>   - `scripts/probe_quark_share_stats.py` —— 只读探测脚本,将来夸克若开放链接级转存数可直接复现;
>   - `quark_share_stats` 表 —— 夸克唯一一份链接级快照数据,删表要走迁移,留着零成本。

---

## 11. 热点建议 · 已发标记与回看(2026-09-29,Agent v5「预测→下注→结算」闭环)

> 建议推送行带 `[# 建议ID]`;运营发货后一键标记「已发」——只有 acted 的建议 +
> **结算信号**才构成 Agent 学习样本(没执行的建议不进样本,避免把"没发"误学成"发了没效果")。
> ⚠️ **结算信号 2026-09-29 起是 `repost_gain`(发文后全网新增的该文盘链记录数),
> 不再是夸克 save_pv**(夸克不提供链接级转存数,采集链已休眠、端点 2026-10-03 删除,见 §10)。

### 11.1 标记已发/取消

- **接口名称**: 建议标记 acted
- **请求方式**: POST
- **URL 路径**: `/api/hotspot/suggestions/{sid}/acted`
- **请求参数 (Body)**:

```json
{ "acted": true }
```

**响应示例 (200)**
```json
{ "status": "ok", "id": 12, "keyword": "兰香如故", "acted": true }
```

> 失败:404(建议不存在或非本人);不传 body 默认 acted=true。

### 11.2 建议回看列表

- **接口名称**: 热点建议列表
- **请求方式**: GET
- **URL 路径**: `/api/hotspot/suggestions`
- **请求参数 (Query)**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| limit | int | 条数上限,默认 50,封顶 200 |
| acted | bool | 可选;`true` 只看已发(供效果结算分析) |

**响应示例 (200)**
```json
{
  "total": 1,
  "list": [
    {
      "id": 12, "keyword": "兰香如故", "kind": "match", "growth": 150.0,
      "platforms": "douyin+baidu", "opportunity": 96.4,
      "resource_title": "兰香如故全集资源", "link": "https://pan.quark.cn/s/xx", "plan": "标题字面命中·匹配自 xx",
      "saves": 0, "saves_at": null, "acted": true, "acted_at": "2026-09-29T15:00:00",
      "article_id": 71, "repost_gain": 12, "settled_at": "2026-09-29T22:00:00",
      "created_at": "2026-09-29T15:10:00"
    }
  ]
}
```

> Agent v5 决策口径(输出内标注):`opportunity = 涨幅 × 共振分级(百度×1.5/微博×1.2) × 竞争稀疏度(1/(1+同话题供给)) × 窗口因子(score 动量 12h/6h/2h)`;推送分「🎯 优先发货(机会分 top3)」与「📋 备选」两组。

### 11.3 触发结算(手动补跑)

- **接口名称**: 建议结算
- **请求方式**: POST
- **URL 路径**: `/api/hotspot/settle`
- **请求参数**: 无

**响应示例 (200)**
```json
{ "status": "ok", "acted_with_link": 5, "settled": 3, "attributed": 5 }
```

> 结算逻辑(2026-09-29 去 dajiala 版):dajiala 阅读采样已放弃(用户决策,无免费阅读数源),
> 结算主信号改为**盘链全网扩散增量**——acted 建议 → 按盘链精确归因到发文 →
> `wechat_pan_links` 中发文后(acted_at 起)新增的该文盘链记录数写入 `repost_gain`
> (被疯转=需求被反复验证,免费自动)。可重复结算(随扩散刷新);总账对账走拉新周录(11.4)。
> 调度作业 id:`suggestion_settle`(每日 22:00)。
> ⚠️ **`reads_gain` 已从响应里摘掉(2026-10-03)**:2026-09-30 放弃 dajiala 阅读采样后它
> **永远是 0**,而返回 0 会被读成"阅读增量为 0"而不是"**这项早就不测了**" ——
> 属于"拿假 0 冒充真数据"。列仍在表里(兼容旧行),只是不再对外给。

### 11.4 拉新周录(方案B 总账)

- **接口名称**: 录入/更新周度拉新数(**支持分渠道**)
- **请求方式**: POST
- **URL 路径**: `/api/hotspot/recruits`
- **请求参数 (Body)**:

```json
{ "week_start": "2026-09-22", "recruits": 60, "channels": { "douyin": 42, "wechat": 18 }, "note": "含国庆活动" }
```

**响应示例 (200)**
```json
{ "status": "ok", "id": 1, "week_start": "2026-09-22", "recruits": 60,
  "channels": { "douyin": 42, "wechat": 18 } }
```

> **`channels` 是分渠道明细**(2026-10-03 用户口径:"我只能给你我的",且要**分渠道**给)——
> 只有分开录,才能分别对账抖音/公众号两条链;**给了明细就以明细之和为准**,
> 免得总数与明细打架。只传 `recruits` 仍按总量录(向后兼容)。
> 落库为 `pan_recruit_weekly.channels`(JSON 列)。

- **查询**: GET `/api/hotspot/recruits?limit=12` → `{"total":N,"list":[{"week_start","recruits","channels","note","created_at"}]}`(最近在前)
- 同 `week_start` 重录 = 覆盖更新;`pan_recruit_weekly` 表按 user_id 隔离。
- 命令行入口:`python scripts/record_recruits.py 2026-09-22=12 2026-09-29=7 [--note 备注]`
- **每周一 09:40 自动提醒**(`recruit_reminder`,推**管理员群**):`pan_recruit_weekly` 是
  **转化回路唯一的真值入口**(链接级真值在夸克/迅雷侧都拿不到,已定案),却长期 0 行 ——
  入口早就有,缺的只是"有人去录"。⚠️ **录了就不再提醒**(只列还缺的周),否则每周一条通知
  很快变成噪音被无视;⚠️ 没配管理员群就**安静跳过**,不回落客户群(这是内部待办)。

### 11.5 线索结算对账(2026-10-03)

- **接口名称**: 系统侧线索量级 vs 人工周录真值(按周并排)
- **请求方式**: GET
- **URL 路径**: `/api/hotspot/leads/settlement?weeks=8`(weeks 1~26)
- **权限**: 登录用户

**响应示例 (200)**
```json
{
  "weeks": [
    { "week_start": "2026-09-29", "leads": 13, "share_total": 4123, "estimated": 2886.1,
      "recorded_total": 60, "recorded_channels": { "douyin": 42, "wechat": 18 },
      "has_record": true }
  ],
  "authors": [
    { "author": "籽***", "leads": 5, "share_total": 812, "estimated": 568.4 }
  ],
  "coefficients": { "douyin": 0.7, "wechat": 0.3 },
  "recorded_weeks": 1,
  "note": "…"
}
```

> **口径(2026-10-03 用户定,含一次更正)**:
> **预估转化 = 该内容的互动量 × 该渠道系数** ——
> **抖音**看**转发量**、60~80% → 取中值 **0.7**;**公众号**看**阅读量**、**30%**
> (用户先说过 10%,当天更正为 30%)。
>
> ⚠️ **为什么是"量 × 系数"而不是人工标「已发」**(用户 2026-10-03 指出,我原方案是错的):
> 人工标记**要人做、而且是自报** —— 而"拉新周录至今 0 行"已经证明**要人做的环节一定没人做**;
> 互动量则是**客观、自动、且能按号归因**的。所以这条链**全自动、不需要任何人填**;
> 原方案里"给发现卡片加『已发』标记"**因此取消**(那是把同一个坑再挖一遍)。
>
> `authors` 是**按推广号聚合**(用户口径:"还能统计不同用户的情况")—— 一眼看出**谁最能带量**。
> ⚠️ 抖音的 `author` 是 MediaCrawler **脱敏过的**(如「籽***」),只能用来**区分不同号**,
> 不能直接去站内搜人(那个工具教学版的已知限制)。
>
> `share_total` 是本周线索所带**转发量之和**(别人视频的),`recorded_*` 是用户从官方后台
> 抄来的**自己号**拉新 —— 两者**不同源**,对账时**看趋势、别相除**。
> `has_record=false` 表示**这一周你还没录**,与"录了 0"是两件事。

### 11.6 供应商评分(2026-10-03 接出来)

- **接口名称**: 对标号(供应商)评分
- **请求方式**: GET
- **URL 路径**: `/api/hotspot/suppliers?days=30`(days 1~180)
- **权限**: 登录用户

**响应示例 (200)**
```json
{ "days": 30, "list": [
  { "author": "小南不臭", "articles": 7, "reposts": 46, "score": 152 }
] }
```

> **用途**:「**优质号加密监控 / 劣质号降权**」—— 产出多、且链被别人反复转载的号,说明它在持续供
> **被验证过的**资源。`score = articles × 2 + reposts × 3`。
>
> ⚠️ **这个函数此前写了但没有任何调用方**(2026-10-03 全项目审查发现,属"白写了"那一类)——
> **接出来而不是删掉**(它算的是真数据:`WechatArticle` × `WechatPanLink`)。

### 11.7 对端实例探活(2026-10-03)

- **接口名称**: 另一侧部署(远程 hotspot)是否在线
- **请求方式**: GET
- **URL 路径**: `/api/source-health/peer`
- **权限**: 登录用户

**响应示例 (200)**
```json
{ "configured": true, "online": true, "url": "https://redu.tian1she.xyz",
  "latency_ms": 1308, "version": "2.14.0", "time": "2026-10-03T14:33:24", "error": "" }
```

> **为什么需要它**:本机(wechat 侧)与远程(hotspot 侧)**数据库各自独立** —— 微博/抖音/百度热榜
> 归远程跑,本机只知道它们"数据停在某天",**分不清是远端整机挂了、还是那些源本身没更新**。
> 这个探活把两种情况分开。用的是对端**已有的公开 `/healthz`**,**不新增任何暴露面**。
> 未配 `PEER_HEALTH_URL` 时返回 `{"configured": false}`,不探、不报错。
>
> 配套:远端每天 09:20(`health_push`)往**管理员群**推一张**板块健康卡**(仅在远端跑,
> 角色 `hotspot`)。⚠️ **只推管理员群,不回落主群** —— 采集源健康属运维噪音,
> 推进客户群是事故;没配 `FEISHU_WEBHOOK_ADMIN` 就安静跳过。

## 11e. 账号健康趋势(2026-10-01)
- **接口名称**: 采集源按天趋势 + 关键信号频次
- **请求方式**: GET
- **URL 路径**: `/api/source-health/trend?days=14`
- **权限**: 登录用户

**响应示例 (200)**
```json
{
  "days": 14,
  "by_day": [ { "date": "2026-10-01", "kinds": { "wechat_listen": { "success": 3, "partial": 1, "failed": 0, "skipped": 0 },
                                                 "xianyu": { "success": 10, "failed": 2, "partial": 0, "skipped": 0 } } } ],
  "signals": { "wechat_quota": { "2026-09-30": 2 },
               "cookie_expired": { "2026-09-30": 5 },
               "xianyu_verify": { "2026-09-28": 1 } }
}
```
> 关键信号从 runs.detail 提取:微信读书额度耗尽(quota_skipped)/Cookie 失效(-2012|WereadAuthError)/闲鱼滑块(XianyuVerify)。
> 前端入口:「数据源健康」页的「近 14 天账号健康趋势」区。

## 11d. 多平台热榜(2026-10-01,15 源雷达)

- **接口名称**: 多平台热榜总览
- **请求方式**: GET
- **URL 路径**: `/api/hotspot/hot-rank?per=10`
- **权限**: 登录用户

**响应示例 (200)**
```json
{
  "count": 11,
  "platforms": [
    { "source": "bilibili", "label": "B站", "captured_at": "2026-10-01 03:05:06",
      "items": [ { "rank": 1, "title": "…", "url": "https://…", "extra": "手机游戏 · 播放123" } ] },
    { "source": "zhihu", "label": "知乎", "captured_at": "…", "items": [ … ] }
  ]
}
```
> 各源**最新一轮** top N(B站/豆瓣自研直连优先在前,其余 newsnow 长尾);前端入口:导航「多平台热榜」。

## 11c. 资源库(2026-10-01,现成资源检索)

### 11c.1 资源检索 / 高共振榜
- **接口名称**: 资源库查询
- **请求方式**: GET
- **URL 路径**: `/api/wechat/resources?q=&days=90&limit=30`
- **权限**: 登录用户

**参数**: `q` 空 = 高共振榜(同链被 ≥2 号同发);有值 = 关键词检索(标题子串,<2 字不检索)。`days` 1-365;`limit` 1-100。

**响应示例 (200)**
```json
{
  "summary": { "total_links": 542, "multi_account": 10, "days": 90 },
  "query": "花少",
  "items": [ { "pan_url": "https://pan.quark.cn/s/…", "pan_type": "夸克",
               "accounts": 4, "titles": ["花少2人格测试直达入口｜最新测试"],
               "first_seen": "2026-09-15 12:00:00", "last_seen": "2026-09-18 20:00:00",
               "my_link": "https://pan.quark.cn/s/mine…" } ]
}
```

### 11c.2 爆款资源榜
- **接口名称**: 资源级爆款(近 N 小时 ≥3 号新同发)
- **请求方式**: GET
- **URL 路径**: `/api/wechat/resources/viral?hours=24`

**响应示例 (200)**: `{"hours": 24, "items": [{...同 11c.1 结构...}]}`

> 前端入口:导航「资源库」页(检索/共振榜/爆款榜三合一)。

## 11b. 热点建议 · AI 发布文案(2026-10-01,发布最后一公里)

- **接口名称**: 按建议生成可发布文案
- **请求方式**: POST
- **URL 路径**: `/api/hotspot/suggestions/{sid}/draft`
- **权限**: 登录用户(建议归属校验,非本用户 404)

**请求参数**: 无 body。

**响应示例 (200)**
```json
{
  "status": "ok",
  "titles": ["亚运电竞开赛了！赛程表+游戏安装包+报考指南一份打包自取",
             "看完亚运电竞想上手同款？游戏安装包+外设清单整理好了",
             "家里孩子想走电竞路？资料合集:报考指南+训练计划"],
  "content": "最近亚运电竞项目开赛……(400~800 字正文,可直接发布)",
  "my_link": "https://pan.quark.cn/s/xxx (提取码 abcd)"
}
```
> 生成逻辑:取建议的 keyword/plan;若**资源库**中该关键词已有我方转存链,自动带上(文末附链,员工复制即用);
> 结果落 `hotspot_suggestions.draft` 供复用(**按需生成**省 LLM 成本)。未配 DEEPSEEK_API_KEY → 400;生成失败 → 502。

## 12. 鉴权与账户(/api/auth/*)

> 无需登录;除 register/login/forgot/reset 外的所有业务接口都需要 `Authorization: Bearer <token>`。
> 登录按 IP+账号滑窗限速,连续失败 5 次锁 10 分钟(HTTP 429)。

### 12.1 注册
- **接口名称**: 用户注册
- **请求方式**: POST
- **URL 路径**: `/api/auth/register`

**请求参数 (Body)**
```json
{ "email": "a@b.com", "password": "******", "username": "可选,缺省取邮箱前缀" }
```

**响应示例 (200)**
```json
{ "token": "eyJhbGciOi...", "username": "a" }
```

### 12.2 登录
- **接口名称**: 登录(用户名或邮箱)
- **请求方式**: POST
- **URL 路径**: `/api/auth/login`

**请求参数 (Body)**
```json
{ "login": "admin 或 a@b.com", "password": "******" }
```

**响应示例 (200)**:同 12.1 `{ "token": "...", "username": "admin" }`;失败 401 `{"detail":"账号或密码错误"}`。

### 12.3 忘记密码
- **接口名称**: 发送重置邮件
- **请求方式**: POST
- **URL 路径**: `/api/auth/forgot`

**请求参数 (Body)**: `{ "email": "a@b.com" }`

**响应示例 (200)**: `{ "message": "如果该邮箱已注册,重置邮件已发送" }`(无论邮箱是否存在都返回同一句,防枚举;IP+邮箱滑窗限速,超频 429)

### 12.4 重置密码
- **接口名称**: 重置密码
- **请求方式**: POST
- **URL 路径**: `/api/auth/reset`

**请求参数 (Body)**: `{ "token": "邮件链接 #token= 片段", "new_password": "******" }`

**响应示例 (200)**: `{ "message": "密码已重置,请重新登录" }`;token 无效/过期 400。

### 12.5 当前用户
- **接口名称**: 我的信息
- **请求方式**: GET
- **URL 路径**: `/api/auth/me`

**响应示例 (200)**
```json
{ "id": 1, "username": "admin", "role": "admin" }
```

---

## 13. 管理后台(/api/admin/*)

> 全部要求 `role ∈ {admin, operator}`,且按**按钮级权限**二次校验(`require_perm`,权限不足 403)。

### 13.1 我的后台身份
GET `/api/admin/me` → `{ "role": "admin", "username": "admin", "perms": ["dashboard.view", "..."] }`

### 13.2 平台总览
GET `/api/admin/dashboard`(perm `dashboard.view`)
```json
{ "users": 2, "enabled_users": 1, "admins": 1, "...": "各板块今日采集/告警计数" }
```

### 13.3 智能洞察
GET `/api/admin/insights`(perm `data.view`)→ 跨用户关键词趋势/预测汇总,含 `burst`(可能爆发词,带置信度)。

### 13.4 采集健康度
GET `/api/admin/health`(perm `logs.view`)→ 各平台最近采集状态 + 数据写入 + 飞书推送 + Cookie 配置,运维一键看。

### 13.5 用户管理
| 动作 | 方式 | 路径 | 说明 |
|---|---|---|---|
| 列表/搜索 | GET | `/api/admin/users?q=` | `[{"id","username","email","role","enabled",...}]` |
| 详情 | GET | `/api/admin/users/{user_id}` | 404=不存在 |
| 启停 | POST | `/api/admin/users/{user_id}/toggle` | 返回 `{"enabled": false, ...}`;不能停自己(403/400) |
| 删除 | DELETE | `/api/admin/users/{user_id}` | 不能删自己;`{"deleted": true}` |
| 批量导入 | POST | `/api/admin/import/users` | Body `{"text": "每行一个用户"}` → `{"created":N,"skipped":N}` |
| 导出 | GET | `/api/admin/export/users` | CSV 纯文本(id,username,email,role,enabled,created) |

### 13.6 日志
| 动作 | 方式 | 路径 | 说明 |
|---|---|---|---|
| 登录日志 | GET | `/api/admin/logins` | `[{"username","ip","ua","ok","time"}]` |
| 管理操作日志 | GET | `/api/admin/logs` | `[{"admin","action","target","time"}]` |
| 失败采集 | GET | `/api/admin/runs/failed` | `[{"run_id","kind","user_id","detail","time","retry"}]` |
| 重试失败采集 | POST | `/api/admin/runs/{run_id}/retry` | `{"ok": true}` 或 `{"ok": false, "msg": "..."}` |

### 13.7 运行配置
| 动作 | 方式 | 路径 | 说明 |
|---|---|---|---|
| 读全部 | GET | `/api/admin/config` | `[{"key","value"}]` |
| 写单项 | PUT | `/api/admin/config/{key}` | Body `{"value": "..."}` → `{"key","value"}` |

### 13.8 数据浏览与图表
| 动作 | 方式 | 路径 | 说明 |
|---|---|---|---|
| 数据表浏览 | GET | `/api/admin/data/{section}?user_id=&limit=50` | section ∈ weibo/baidu/xianyu/douhot/wechat |
| 告警类目分布 | GET | `/api/admin/categories` | `[{"name","count","want"}]` |
| 告警趋势 | GET | `/api/admin/alert-trend?days=30` | 按日聚合序列 |
| 类目饼图 | GET | `/api/admin/category-pie` | `{"alerts_section":[{"name","value"}],"watch_types":[...]}` |
| 告警导出 | GET | `/api/admin/export/alerts` | CSV 纯文本 |

---

## 14. 告警规则(/api/alerts/*)

> 登录用户,数据按 user_id 隔离。

### 14.1 规则列表 / 新增
- GET `/api/alerts/rules` → `[{"id","section","rule_type","metric","threshold","keyword","alert_time",...}]`
- POST `/api/alerts/rules`
```json
{ "section": "weibo", "rule_type": "threshold", "metric": "heat", "threshold": 1000,
  "keyword": "网盘", "alert_time": "08:00" }
```
→ `{"id": 3, "section": "weibo", "rule_type": "threshold"}`;非法参数 400。

### 14.2 删除规则
- DELETE `/api/alerts/rules/{rule_id}` → `{"deleted": true}`

### 14.3 最近告警
- GET `/api/alerts/list?limit=30`(1–200 钳制)
```json
[ { "keyword": "网盘", "reason": "涨幅+120%", "section": "weibo", "time": "2026-09-29T08:00:03" } ]
```

---

## 15. 付费群会员(/api/members)

> 登录用户;按入群时间 + 周期自动算到期,每日 renewal_tick 提醒续费/踢人。

### 15.1 列表 / 新增
- GET `/api/members` → `[{"id","group_name","nickname","wechat_id","joined_at","cycle_days","last_renewed_at","state"(正常/临期/过期),...}]`
- POST `/api/members`
```json
{ "nickname": "群友A", "joined_at": "2026-09-01", "group_name": "资源群",
  "wechat_id": "wx_abc", "cycle_days": 30, "note": "" }
```
→ `{"id": 5, "saved": true}`;昵称空/时间非法(超出 1970–9000)400。

### 15.2 续期
- POST `/api/members/{member_id}/renew` → `{"renewed": true}`;404=不存在。

### 15.3 改状态
- POST `/api/members/{member_id}/status`,Body `{"status": "left"}` → `{"status": "left"}`;无效状态 400。

### 15.4 删除
- DELETE `/api/members/{member_id}` → `{"deleted": true}`;404=不存在。

---

## 16. 热点事件 · 数据源健康 · 通用看板

> 登录用户。

### 16.1 热点事件列表
- GET `/api/events?status=active&limit=50`(limit 1–200)
```json
[ { "id": 7, "title": "世界杯", "platforms": ["weibo","douhot","baidu"], "platform_count": 3,
    "peak_value": 2920000, "peak_at": "2026-09-29 12:00:00",
    "first_seen": "...", "last_seen": "...", "duration_h": 36.5 } ]
```
> Hotspot → Event 归并结果:跨平台共振/生命周期视图,按平台数与峰值排序。

### 16.2 手动触发事件归属
- POST `/api/events/assign` → 本轮归属结果(调度每 15 分钟自动跑;此接口即时刷新)。

### 16.3 数据源健康
- GET `/api/source-health` → 每采集源 `HEALTHY / DEGRADED / CIRCUIT_OPEN` 三态 + 问题明细。

### 16.4 统一标准化快照
- GET `/api/trending` → 近 6h 四平台(weibo/baidu/douhot/xianyu)同构字段:
```json
{ "count": 120, "items": [ { "source": "baidu", "source_id": "123", "title": "...",
  "url": null, "rank": 1, "hot_value": 2915321.0, "captured_at": "2026-09-29 22:00:00" } ] }
```

### 16.5 用户仪表盘
- GET `/api/dashboard` → 微博上涨 + 闲鱼热榜(24h 资源去重) + 抖音热词,按 user 隔离。

### 16.6 用户 SMTP(邮件通知自配)
- GET `/api/user/smtp` → `{"host","port","user","from_name"}`(**不含密码**)
- PUT `/api/user/smtp`,Body:
```json
{ "host": "smtp.qq.com", "port": 465, "user": "a@qq.com",
  "password": "******", "from_name": "热点监控" }
```
→ `{"saved": true}`;密码加密存储(enc: 前缀);host 走公网校验(内网/元数据地址 400,防 SSRF)。

---

## 17. 公众号 · 补充接口(/api/wechat/*)

> 登录用户;§9b 已列 listen/sync 主链路,此处补缺口。

### 17.1 关键词搜索词
- GET `/api/wechat/keywords` → `{"terms": ["网盘","问卷", "..."]}`
- PUT `/api/wechat/keywords` → 恒 400(提示到服务器 .env 的 KEYWORD_SEARCH_TERMS 改,重启生效;占位接口)

### 17.2 对标号 · 单个修改/删除/同步
| 动作 | 方式 | 路径 | 说明 |
|---|---|---|---|
| 改备注/停用 | PATCH | `/api/wechat/benchmarks/{benchmark_id}` | Body `{"note": "...", "active": false}` |
| 删除 | DELETE | `/api/wechat/benchmarks/{benchmark_id}` | `{"deleted": true}` |
| 同步历史文章 | POST | `/api/wechat/benchmarks/{benchmark_id}/sync?max_pages=3` | 免费源优先,付费兜底;返回入库与推送计数 |

### 17.3 文章 · AI 改写
| 动作 | 方式 | 路径 | 说明 |
|---|---|---|---|
| AI 改写 | POST | `/api/wechat/articles/{article_id}/rewrite` | 需配 DEEPSEEK_API_KEY(400);正文未抓到 400;成功 `{"ok":true,"article_id","title","..."}` |
| 改写历史 | GET | `/api/wechat/articles/{article_id}/rewrites` | `{"count":N,"items":[{"id","title","..."}]}` |

> ⚠️ **「单篇采样历史」`GET /api/wechat/articles/{article_id}/traffic` 已停止** —— 接口本身也于
> **2026-10-03 删除**(全项目审查)。原因:它读的 `wechat_traffic_samples` 表**全项目无写入方**
> (只有一个读它、一个删它),**永远返回 `count: 0`**,前端也没有调用方 —— 是把
> "这项早就不测了"伪装成"还没有采样点"的空壳。dajiala 付费阅读采样 2026-09-29 废弃
> (`scheduler.py` 的 `traffic_tick` 停用)后就没有采样端了。
> ⚠️ **阅读/点赞自 2026-09-29 起已断供,字段恒 0/空**(2026-10-04 更正):下面那句
> "取自微信读书站内数(免费)" 是 09-30 写的,**已被 10-04 实测推翻** —— 微信读书
> 的 `/web/mp/articles`(唯一带 readNum 的接口)**永久废弃**(不是"被限权"),
> 其封面接口(见 `doc/外部接口速查.md §3.2`)返回体里根本没有 readNum,
> WeRSS 亦无此能力。逐条实测见 `doc/外部接口速查.md §3.2`。
> **字段与端点保留**(等免费源回来即复活),但**别把 0 读成"这篇没人看"**。
> 效果评估改走**方案B 人工拉新周录**(`GET /api/hotspot/leads/settlement`)。
> 前端同批摘除了那个调不到路由的「刷新阅读量(¥0.06/篇)」按钮
> (守卫:`scripts/check_frontend_routes.py`)。

---

## 18. 闲鱼 · 补充接口

> 登录用户;§6 已列热榜,此处补日结与深度分析。

- GET `/api/xianyu/daily` → 按日汇总(命中词数/新增商品/最佳名次序列)。
- GET `/api/xianyu/analytics` → 深度分析(资源类目分布、词效对比、趋势)。
- GET `/api/xianyu/market` → 价位行情(供给价分布 + 需求想要数 → **供需比**;支持 `?days=`)。

---

## 19. 关键词监控 · 手动推卡

- POST `/api/watch/{section}/digest`(section ∈ weibo/baidu/douhot/xianyu/...)
  立即把该板块「关键词监控」卡片推到飞书(含名次变化 ↑N/↓N);平时每日 08:00 日报自动推。
  → `{"ok": true, "pushed": true}`(未配飞书 webhook 时 pushed 为 false)。

### 16b.3 ~~闲鱼扫码登录~~ → **已删除(2026-10-03)**

> ⚠️ 原有两个接口 `POST /api/cookies/goofish/qr-start` 与
> `GET /api/cookies/goofish/qr-status`(以及 `app/services/xianyu_login.py`)**已删除**。
>
> **为什么删**:它们走纯协议二维码流程、把登录态写进 `cookie_store`;但 **2026-10-02 起采集默认
> 走浏览器档案**(`xianyu_browser`,登录态在 `tools/xianyu_profile`),`tenant.run_xianyu`
> 在浏览器模式下**不读也不校验**那个 cookie —— 于是那个按钮**扫了完全没效果,却会显示
> 「✅ 登录成功」**。**比报错更糟**:用户以为修好了,实际问题一直在。
>
> **现在闲鱼登录的正确做法**:跑 `python scripts/xianyu_login.py`(打开**浏览器档案**让你登,
> 登完关窗口即可);或在项目目录直接用该档案开浏览器登录。见 `doc/operations.md §10`。
> (纯协议那条路 `XIANYU_USE_BROWSER=false` 是被证明会遭**账号级限流**的兜底,不推荐。)
>
> **摘除后的实测**:闲鱼采集照常(`POST /api/collect/xianyu` → `count=90`)。
> 会话存进程内存(15 分钟 TTL);服务重启后旧会话失效,重新生成即可。

## 16b. Cookie 管理(/api/cookies)

> 登录用户,凭据按 user_id 隔离;明文只写不读(GET 不回传 Cookie 本体)。

### 16b.1 已配置列表
- **接口名称**: Cookie 配置一览
- **请求方式**: GET
- **URL 路径**: `/api/cookies`

**响应示例 (200)**
```json
[ { "platform": "weread", "configured": true, "preview": "wr_vid=4398...", "updated_at": "2026-09-29 14:58:29" },
  { "platform": "goofish", "configured": false, "preview": "", "updated_at": null } ]
```

### 16b.2 设置 / 删除
- **设置**: PUT `/api/cookies/{platform}`,Body `{"cookie": "完整 Cookie 串"}` → `{"platform": "weread", "configured": true}`(Fernet 加密落库;platform ∈ weibo/baidu/douyin/goofish/weread/baidupan/quark/zhihu/bilibili)
- **删除**: DELETE `/api/cookies/{platform}` → `{"platform": "goofish", "deleted": true}`

> 网盘类(baidupan/quark)与 dajiala 故意不走自愈:运营者全局凭据不进普通用户可见面。

## 20. 跨平台对标号(/api/cross,2026-10-02)

> 拿公众号监控到的**网盘资源**去别的平台找同类资源号。
> **两类平台的搜索词来源不同**(实测,这是关键):内容平台(知乎)用**资源词**(谁在分享
> 这个**具体资源**);账号垂直平台(B站)用**行业词**(谁在做**这门生意**,见 `CROSS_BILI_KEYWORDS`)。
> ⚠️ 会真实访问外部平台且**带限速**(每请求间隔 4 秒),一轮约 6 个请求,默认每周一/四 09:00。

### 20.1 列表
- **接口名称**: 跨平台对标号列表(新→旧,最多 200)
- **请求方式**: GET
- **URL 路径**: `/api/cross/accounts`
- **请求参数 (Query)**: `platform`(可选,`zhihu` / `bilibili`;空 = 全部)

**响应示例 (200)**
```json
{ "count": 61,
  "items": [ { "id": 61, "platform": "bilibili", "uid": "87482673",
               "name": "百度网盘会员福利酱", "url": "https://space.bilibili.com/87482673",
               "hit_keyword": "百度网盘", "snippet": "...", "pan_link": "",
               "status": "active", "discovered_at": "2026-10-02 10:14:22" } ] }
```

### 20.2 手动发现一轮
- **接口名称**: 立即跑一轮跨平台发现
- **请求方式**: POST
- **URL 路径**: `/api/cross/discover`
- **请求参数**: 无(用户取自登录态)

**响应示例 (200)**
```json
{ "status": "ok", "keywords": ["霸王茶姬杯贴自定义入口链"],
  "platforms": ["zhihu", "bilibili"], "found": 60, "new": 59,
  "items": [ { "platform": "bilibili", "name": "网盘资源商行", "keyword": "网盘资源" } ] }
```

> **收录判据**(或关系):① 内容里含**真网盘链**(知乎口径);② **号名/签名明写网盘**
> (`_PAN_ACCOUNT_HINTS`,只认"网盘"/具体品牌名,**不认泛词"资源"**)——B站搜索层给不出链,
> 靠 ② 才收得到号。
> **前端入口**:「跨平台对标号」页 `/cross`(含平台筛选 + 手动触发)。

---

## 21. 迅雷群组资源采集(/api/xunlei,2026-10-02)

> **为什么有这一节**:此前最大的卡点是「**口令 → 分享 id(shareID)** 只存在于迅雷客户端」,
> 服务端拿不到。而**迅雷群消息流里的分享卡自带现成分享链** —— 抖音标题里《》包的那串口令,
> 本质就是**群名**(实测:「三岁分享」「白泽的梦」既是抖音线索,也是账号已加入的群)。
> 所以采集走群消息,不再需要解析口令。
>
> **两步走**:①**采集**(只登记,纯 HTTP 读,秒级、可高频)②**转存**(限量 —— 每条要真的
> 存进用户迅雷盘并生成我方分享链,慢且占空间,默认每轮 5 条)。
>
> ⚠️ `/api/xunlei/transfer` 会**真实写入用户的迅雷网盘**。
> **前置条件**:需先在「Cookie 管理」里配置迅雷凭据(扫码登录,见 `scripts/xunlei_login.py`)。

### 21.1 群列表

- **接口名称**: 账号所在的迅雷群列表
- **请求方式**: GET
- **URL 路径**: `/api/xunlei/groups`
- **请求参数**: 无(用户取自登录态)

**响应示例 (200)**
```json
{ "count": 7,
  "items": [ { "group_id": "1550069837", "name": "三岁分享", "role": "member" },
             { "group_id": "1555276876", "name": "全网最全宝库", "role": "creator" } ] }
```

> 实时拉一次(不落库);未配置凭据时 `items` 为空数组,不报错。

### 21.2 已采分享列表

- **接口名称**: 群分享列表(新→旧,最多 `limit` 条)
- **请求方式**: GET
- **URL 路径**: `/api/xunlei/shares`
- **请求参数 (Query)**: `status`(可选,`pending` / `ok` / `failed`;空 = 全部)、`limit`(默认 200)

**响应示例 (200)**
```json
{ "count": 28,
  "items": [ { "group_id": "1550069837", "group_name": "三岁分享",
               "title": "手机警报器（警笛模拟器）2.0版",
               "origin_url": "https://pan.xunlei.com/s/VP2rVszEvka7-8jWT0PxdxolA1",
               "our_url": "https://pan.xunlei.com/s/VP2uzaCB0yCevYHQyFrtOXALA1?pwd=64gh",
               "pass_code": "64gh", "status": "ok", "message": "",
               "kind": "drive#folder", "msg_time": "2026-10-02 12:40" } ] }
```

> `status`:`pending` 待转存 / `ok` 已转存(此时 `our_url` 是我方链)/ `failed`(原因在 `message`)。

### 21.3 手动采集一轮

- **接口名称**: 立即扫一轮群消息(只登记,**不转存**)
- **请求方式**: POST
- **URL 路径**: `/api/xunlei/sync`
- **请求参数**: 无

**响应示例 (200)**
```json
{ "status": "ok", "groups": 7, "new": 28 }
```

> `status` 取值:`ok` / `no_cred`(未配置迅雷凭据)/ `empty`(没拿到群列表)。

### 21.4 手动转存一批

- **接口名称**: 转存 pending 的群分享(限量)
- **请求方式**: POST
- **URL 路径**: `/api/xunlei/transfer`
- **请求参数 (Query)**: `limit`(默认 3,服务端夹在 1~10)

**响应示例 (200)**
```json
{ "status": "ok", "picked": 3, "ok": 2, "failed": 1,
  "items": [ { "title": "diplay-车机互联（安卓+苹果）", "group_name": "三岁分享",
               "share_url": "https://pan.xunlei.com/s/VP2uz_O8UT8yOkE4wLQwJbVHA1?pwd=zp47",
               "code": "zp47" } ] }
```

> ⚠️ **会真实写入用户迅雷盘**,单条最慢约 1 分钟(转存任务轮询),别连点。
> 单条失败会被标成 `failed` 留痕,**不会每轮重复重试**它。
> **前端入口**:「迅雷群组」页 `/xunlei`(群列表 + 采集/转存按钮 + 分享表格)。

### 21.5 口令解析(抖音《口令》→ 资源入口)

- **接口名称**: 迅雷口令解析(可选择性直接转存)
- **请求方式**: POST
- **URL 路径**: `/api/xunlei/kouling`
- **请求参数 (Body)**:
  | 字段 | 类型 | 必填 | 说明 |
  | --- | --- | --- | --- |
  | `kouling` | string | 是 | 抖音标题里《…》包的那串口令(也是群名) |
  | `transfer` | bool | 否 | 默认 `false` 只解析;**`true` = 解析到分享链后直接转存入库**(会写用户的盘) |

**响应示例 (200)** —— 解析到**网盘分享链**(最常见):
```json
{ "kind": "share", "share_url": "https://pan.xunlei.com/s/VOtw0rXU99xNQ-XD-0vtBexoA1",
  "pass_code": "gcsk", "group_id": "", "title": "玩车不求人", "raw_type": "share_page" }
```
**响应示例 (200)** —— 解析到**群邀请**(要先进群):
```json
{ "kind": "group", "group_id": "1550069837", "share_url": "", "raw_type": "" }
```
**响应示例 (200)** —— 不是口令(判据干净,不乱动):
```json
{ "kind": "none", "share_url": "", "group_id": "", "raw_type": "" }
```
`transfer=true` 时返回转存结果:`{"status": "ok"|"deferred"|"not_kouling"|"failed",
"kind", "kouling", "group_id", "share_url", "our_url", "message"}` ——
`deferred` 表示解析到的是**群**,已加群,资源交给群采集轮。

> **背景**:这个接口就是迅雷 App 搜索框「粘贴口令」用的那个
> (`associate_search` 返回的链接里带 `from=BHO/paste/kouling`)。它把此前
> 判定"服务端做不了"的**口令 → 分享 id** 那一环补上了,是「抖音发现 → 自动入库」
> 全自动链路的关键一环。

### 21.6 captcha 续期状态 / 手动补铸

> **背景**:盘写操作要 `captcha_token`,它**寿命只有十几分钟**,而且只能由**网页版身份**
> 铸(我们自取的那条路走不通)。转存遇到 `captcha_invalid` 时会**自动补铸并重试一次**,
> 所以正常情况**不需要**调这里的接口。

#### 21.6.1 状态

- **接口名称**: captcha 续期状态
- **请求方式**: GET
- **URL 路径**: `/api/xunlei/captcha`
- **请求参数**: 无

**响应示例 (200)**
```json
{ "last_minted_at": 1790922566.9, "last_error": "", "cooldown_seconds": 60 }
```

#### 21.6.2 手动补铸

- **接口名称**: 立即补铸一枚 captcha
- **请求方式**: POST
- **URL 路径**: `/api/xunlei/captcha/refresh`
- **请求参数**: 无

**响应示例 (200)**
```json
{ "ok": true, "last_minted_at": 1790922620.1, "last_error": "", "cooldown_seconds": 60 }
```

> 会**开一次无头浏览器**(约 15~30 秒)。`ok=false` 时通常意味着登录态本身要重新扫码了。
> **前端入口**:「迅雷群组」页 `/xunlei` 的采集/转存按钮旁。

---

### 21.7 盘空间用量(转存闸门的判据)

- **接口名称**: 迅雷盘空间用量
- **请求方式**: GET
- **URL 路径**: `/api/xunlei/quota`
- **请求参数**: 无

**响应示例 (200)**
```json
{ "ok": true, "ratio": 1.2653, "usage_text": "38.08TB", "limit_text": "30.10TB", "full": true }
```

> **转存闸门**(2026-10-02 加):`21.4 转存` 与 `21.5 口令解析(transfer=true)` 在真正搬之前
> 都要过两道闸门 ——
> ① **盘级**:`ratio ≥ XUNLEI_TRANSFER_MAX_USAGE_RATIO`(默认 `0.9`)→ **整批不搬**;
> ② **名字**:标题命中泛化大包词(合集/大全/最全/资源库…,见 `xunlei_group.BULK_WORDS`)→ 不搬。
>
> 被挡下时**两种终态要分开**(`admit_transfer` 的第三个返回值 `retryable`):
> - **盘满** → **可重试**:群采集**整批停下、行保持 `pending`**(`status="disk_full"` 返回给调用方),
>   清理出空间后**下一轮自动接着搬**,不用人工重新排队;口令解析则**不写库**,
>   下一轮抖音作业自然再试。
> - **泛化大包** → **不可重试**:标 `status="skipped"` 终态留痕,策略性不搬。
> **为什么只能这么判**:分享详情里**文件夹的 `size` 恒为 0**,分享接口也不给展开子目录
> (实测 `parent_id`/`file_id` 都被忽略),所以**拿不到单个资源包的体积** —— 只能"守住总量
> + 按名字挡大包"。背景:实测自动转存把「【全网最齐】游戏软件资源合集」搬进盘,空间顶到
> 126%,之后所有转存都 `file_space_not_enough`。

### 21.8 我方盘资源清单(2026-10-03)

- **接口名称**: 迅雷盘里我们自己的资源(口令转存 + 扫盘)
- **请求方式**: GET
- **URL 路径**: `/api/xunlei/resources?q=&limit=50`(q 可选,按名字筛;limit ≤200)

**响应示例 (200)**
```json
{ "total": 8, "list": [
  { "name": "白泽的梦", "kind": "drive#folder", "size": "0", "parent": "最全文件",
    "share_url": "https://pan.xunlei.com/s/VP2uvm…?pwd=yji8", "pass_code": "yji8",
    "synced_at": "2026-10-03 11:21:05" } ] }
```

> ⚠️ **这是补上的出口**(2026-10-03 全项目审查发现):`xunlei_resources` 此前**写入 2 处
> (口令转存 `xunlei_kouling` / 扫盘 `xunlei_sync`)、读取 0 处**(除健康检查取个时间)——
> 抖音口令搬进来的、扫盘扫出来的资源**在系统里没有任何地方能看见**。
>
> 同时把它**并入资源库检索**(`resource_library.search_resources`):于是
> - 资源库页能搜到这些资源(搜索结果里 `source` 标 **`迅雷盘`**);
> - **presence「名字型」也能匹配到它们** —— 否则"搬进来了但用不上"。
> - ⚠️ **形状差别**:公众号/知乎那两条里 `pan_url` 是"**别人的原链**"、`my_link` 是"我们的";
>   这里 `pan_url` 与 `my_link` **都是我们自己那条分享链**(盘里的东西本来就是我们的)。
>   同一条链若公众号也有,**以公众号那条为准**(它带"多少号同发"这个更强信号)。
> - 没有 `share_url` 的行不入库(库的用途是"给出可用链")。

### 21.9 过期转存清理(2026-10-03)

**用户口径**:"可以定期清理久远资源,**一个星期内没有人再发了**就可以删除了。"

- **预览(只看不删)**: GET `/api/xunlei/cleanup/plan?days=7`
- **执行**: POST `/api/xunlei/cleanup?days=7&dry_run=true` —— **默认 `dry_run=true`(只算不删)**,
  要真动手必须显式传 `dry_run=false`

**响应示例 (200)**
```json
{ "days": 7, "scanned": 31, "stale": 2, "keep": 29, "to_delete": 2, "deleted": 0,
  "dry_run": true, "skipped_by_limit": 0, "errors": [],
  "folders": [ { "name": "亚麻壁纸", "id": "VP…", "created": "2026-09-16T13:32",
                 "last_seen": "2026-09-16T13:32", "days_idle": 17,
                 "matched_by": "转存时间" } ] }
```

> **判据**:每个转存文件夹的 `last_seen` = 下列时刻的**最大值**,闲置超过 `days` 天即过期:
> ① 我们转存它的时间(文件夹 `created_time`);② 最近一次**群分享卡**提到它;
> ③ 最近一次**抖音线索**提到它;④ 最近一次**发现链**提到它。名称匹配走
> `xianyu.resource_key` 归一 + **双向包含**(短的那侧**要求 ≥4 字**,否则「软件」这种会全匹配上)。
>
> ⚠️ **几条必须知道的事**:
> - 默认**只扫 `最全文件`**(`xunlei_transfer_parent_id`);用户自己的 `我的转存/右右玩软件`
>   (车机那批)是**人工收藏**,不在自动清理范围内;
> - 执行走 `trash_files()` = **移入回收站**(可恢复),**不是永久删**;单轮限量
>   `xunlei_cleanup_max_per_run`(默认 20,接口无批量);
> - **定时作业默认关**(`xunlei_cleanup_enabled=False`),开了才在每天 03:30 跑;
> - 目录返回空时 `error` 会说明"可能是真为空、也可能是凭据/网络失败"(迅雷接口不区分)——
>   **绝不把这种情况当成"没有过期资源"**;
> - **它解决不了"盘快满"**:实测本流水线在盘里只有 ~0.9TB,而配额显示已用 24TB ——
>   大头在别处(接口查不到),得在网盘网页端看。见 `doc/体检与优化方案-2026-10-03.md`。




