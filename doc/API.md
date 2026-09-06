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

> **风险控制(闲鱼 mtop)**:闲鱼为登录态接口,过度请求会触发网关风控。实测 `FAIL_SYS_USER_VALIDATE`(人机验证/滑块)**经退避重试仍无效**,已与真限流区分——前者立即抛 `XianyuVerify`(由运维人工过滑块/换出口 IP,传输层已用 `curl_cffi` 伪 Chrome 指纹降低被标记概率,dock/dev.md §5.8),后者(`FAIL_SYS_RATE_LIMIT`/`FAIL_SYS_USER_LIMIT`)才走指数退避。
> `POST /api/xianyu/collect-deep` 在**整轮**被验证/限流时返回 **200 + `status:"failed"`**(0 条,不再 500);详情抓取**中途**被验证/限流则停止抓取并保留已采部分,**返回 `status:"partial"`**。`run_xianyu`(`/api/collect/{platform}` 或调度)遇验证则记 `failed`,由既有"采集持续失败"告警提醒运维(告警消息含最近一次失败原因,人工可据"需人工过滑块/换出口 IP"行动)。

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
- **用途**: 聚合各平台最近一次采集、近 24h 运行/失败、最新数据写入、飞书推送统计、Cookie 配置——免手查 MySQL。

**响应示例 (200)**
```json
{
  "generated_at": "2026-09-06T13:00:00",
  "platforms": {
    "douhot": { "last_run": "...", "last_status": "success", "last_detail": "ok", "runs_24h": 5, "failed_24h": 0 },
    "xianyu": { "last_run": "...", "last_status": "failed", "last_detail": "闲鱼人机验证(滑块),全部关键词均未采集", "runs_24h": 2, "failed_24h": 1 },
    "xianyu_deep": { "last_run": null, "last_status": null, "last_detail": null, "runs_24h": 0, "failed_24h": 0 },
    "weibo": { "...": "..." }, "baidu": { "...": "..." }
  },
  "data": { "weibo": "...", "xianyu": null, "douhot": "...", "baidu": "..." },
  "feishu": { "pushes_by_section": [ {"section": "douhot", "count": 106}, {"section": "weibo", "count": 976} ], "last_push": "..." },
  "cookies": { "goofish": 1, "douyin": 1, "weibo": 1, "baidu": 1 }
}
```
> `platforms` 每项为最近一次 `RunRecord`(采集运行)状态;`last_status=failed` 且 `last_detail` 含"滑块/限流"即闲鱼被风控。
> `data` 各平台最新一条数据写入时间(为空=该平台从未进数据);`feishu` 为飞书推送计数/最近推送;`cookies` 为各平台已配 Cookie 的用户数。



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

## 9b. 公众号 · 对标号监听与同步(2026-09-07,基于 dajiala API,见 doc/dajiala-api.md)

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

### 9b.3 更新/删除对标号

- PATCH `/api/wechat/benchmarks/{id}`:body `{"active": false}` 停用(暂停监听,不删数据)
- DELETE `/api/wechat/benchmarks/{id}`:删除(已入库文章保留)

### 9b.4 一键同步该号全部/近期文章

- **请求方式**: POST `/api/wechat/benchmarks/{id}/sync?max_pages=3`
- 每页约 10 次发文(¥0.14/页),`max_pages` 缺省为 `WECHAT_SYNC_MAX_PAGES`(3);翻到 `IsEnd` 提前停止
- **响应示例**: `{ "platform":"wechat_sync", "status":"success", "pages":2, "new":17, "ghid":"gh_xxx", "nickname":"微信派" }`

### 9b.5 监听(手动触发;定时走调度器"公众号监听"板块)

- **请求方式**: POST `/api/wechat/listen`
- 行为:每个启用中的对标号查一次"当天发文"(`post_condition`,¥0.14/号)→ 新文按链接去重入库
  (`wechat_articles.source='listen'`)→ 标题含网盘词的**免费自抓正文**,按四家盘链正则
  (pan.quark.cn / pan.baidu.com / drive.uc.cn / pan.xunlei.com)标记 `pan_types` → 新文推公众号专属飞书群
- 余额保护:开始前查余额(免费),低于 `DAJIALA_MIN_BALANCE` 整轮跳过(`reason:"low_balance"`)
- **响应示例**: `{ "platform":"wechat", "status":"success", "accounts":2, "new":5, "failed":0 }`

### 9b.6 文章列表(支持盘链过滤)

- **请求方式**: GET `/api/wechat/articles?limit=100&has_pan=1&benchmark_id=1`
- **响应**: `{"count":N,"items":[{"id","author","title","url","content","publish_at","source"(manual/listen/sync),"pan_types","benchmark_id","created_at"}]}`

> 文章来源标记:`manual` 手动录入 / `listen` 监听新文 / `sync` 历史同步;`pan_types` 为涉及的网盘类型
> (标题=盘名疑似级,自抓正文命中链接=确认级),逗号分隔,如 `"夸克网盘,百度网盘"`。

### 9b.7 微信读书免费源(2026-09-07)

- **书架预览**: GET `/api/wechat/weread/shelf` → `{"count":N,"items":[{"book_id":"MP_WXS_*","name":"公众号名"}]}`
- **书架一键导入**: POST `/api/wechat/benchmarks/import_shelf`
  → `{"status":"success","shelf":N,"created":新增,"updated":回填bookId}`(重复导入幂等)
- 前置:在微信读书 App 内关注目标公众号,并配置微信读书 Cookie(平台「Cookie管理」新增的
  **weread** 平台,按用户;或 `.env` 全局 `WEREAD_COOKIE`)
- 数据源优先级:对标号有 `weread_book_id` 且有 Cookie → 微信读书(免费,每轮拿"最新一篇");
  否则 dajiala `post_condition`(¥0.14/号)。微信读书登录失效(-2012/-2010)自动降级 dajiala。
- 监听/同步的其余行为不变;`sync` 无 dajiala key 时仅能同步"最新一篇"(返回 `partial`)。
- **读书平台(wewe-rss v2 兼容,免费全量)**:配置 `.env` 的 `WECHAT_READER_PLATFORM_URL/TOKEN/VID`
  后自动成为首选源——监听每号拿最新 20 篇、同步翻页拉全量(免费);对标号列表新增 `biz` 字段
  (文章页 `__biz`,加号时自动解析)。优先级:平台 → 微信读书 cover → dajiala。
