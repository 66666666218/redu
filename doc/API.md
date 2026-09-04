# 接口规范(API)

> 版本: v1.1　|　最后更新: 2026-09-03
> 基础地址: 调度/监控系统暴露的 HTTP 服务(默认 `http://localhost:8080`)
> 认证: 除 `/healthz`(健康检查)外,所有接口需 **JWT Bearer** 登录态;管理接口另需 admin/operator 角色权限。

---

## 通用约定

- 请求与响应均为 `application/json`。
- 时间统一为 ISO 8601(含时区),如 `2026-08-29T10:00:00+08:00`。
- 错误响应统一结构:

```json
{
  "error": {
    "code": "INTERNAL_ERROR",
    "message": "描述性错误信息",
    "detail": "可选的更多细节"
  }
}
```

| 错误码 | HTTP | 说明 |
| --- | --- | --- |
| `BAD_REQUEST` | 400 | 参数错误 |
| `NOT_FOUND` | 404 | 资源不存在 |
| `INTERNAL_ERROR` | 500 | 系统内部错误 |

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

- **接口名称**: 监控仪表盘
- **请求方式**: GET
- **URL 路径**: `/`

页面加载后自动请求 `/api/v1/trends/latest`、`/api/v1/xianyu/hot`、`/api/v1/xianyu/daily` 并渲染。

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

> **风险控制(闲鱼 mtop)**:闲鱼为登录态接口,过度请求会触发网关风控。实测 `FAIL_SYS_USER_VALIDATE`(人机验证/滑块)**经退避重试仍无效**,已与真限流区分——前者立即抛 `XianyuVerify`(由运维人工过滑块/换出口 IP),后者(`FAIL_SYS_RATE_LIMIT`/`FAIL_SYS_USER_LIMIT`)才走指数退避。
> `POST /api/xianyu/collect-deep` 在**整轮**被验证/限流时返回 **200 + `status:"failed"`**(0 条,不再 500);详情抓取**中途**被验证/限流则停止抓取并保留已采部分,**返回 `status:"partial"`**。`run_xianyu`(`/api/collect/{platform}` 或调度)遇验证则记 `failed`,由既有"采集持续失败"告警提醒运维。

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
| `list_type` | str | 否 | `word` | 抖音可选 `word`/`search`/`video`/`topic`/`subscribe`;微博/闲鱼/百度固定 `word` |

**响应示例 (200)**
```json
{ "section": "weibo", "list_type": "word", "keyword": "世界杯" }
```

**列出某板块的关注词**: GET `/api/watch/{section}` → `[{"section","list_type","keyword"}, ...]`

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
> (**词须在榜内才命中**,榜外记 0);抖音 `word`/`search`/`video`/`topic` 四类均走**定向查询**
> (榜外也能查到专属热度,与热点宝官网输入关键词看到的一致)。其中 `video` 为标题模糊/分词检索、
> 取命中视频里最高分作为热度代理(`rank_now` 恒 0,因无单条榜单排名);`subscribe`(我的订阅)
> **无 keyword 参数、不支持定向查询**,仍从榜单内查找。
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

### 7.6 独立板块页(微博/闲鱼/抖音/百度)

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

- **接口名称**: 实时拉取抖音某个子榜
- **请求方式**: GET
- **URL 路径**: `/api/douhot/list/{list_type}`(list_type ∈ `word`/`search`/`video`/`topic`/`subscribe`;需登录)
- **请求参数**: 无(用当前用户抖音 Cookie 实时请求)

**响应示例 (200)**
```json
{ "list_type": "search", "items": [ { "title": "关键词", "score": 121852270 } ] }
```

> 顶部 tab 对应 内容词榜/搜索榜/视频榜/话题榜/订阅;需先在该账号「Cookie 管理」配好抖音 Cookie。

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
      "next_run_at": "2026-09-01 21:20:35"
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
