# 接口规范(API)

> 版本: v1.0　|　最后更新: 2026-08-29
> 基础地址: 调度/监控系统暴露的 HTTP 服务(默认 `http://localhost:8080`)
> 认证: MVP 阶段无鉴权;生产部署请前置网关/内网访问(见 `doc/dev.md` §10)

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
  "version": "1.0.0",
  "time": "2026-08-29T10:00:00+08:00"
}
```

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

> 由调度器每日按 `DAILY_SUMMARY_CRON` 生成并**以 HTML 邮件**推送;`is_new=true` 表示较上次总结新上榜,`new_count` 为当天新上榜总数。看板页 `GET /` 亦展示。

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

