# 热点追踪与自动化监控系统 — 开发文档

> 版本: v1.0(完善版)　|　最后更新: 2026-08-29
> 技术栈: Python 3.9+ / requests / Playwright / NumPy / APScheduler / FastAPI / SQLite

---

## 目录

- [1. 项目概述](#1-项目概述)
- [2. 总体架构](#2-总体架构)
- [3. 数据模型](#3-数据模型)
- [4. 配置中心](#4-配置中心)
- [5. 核心模块详细设计](#5-核心模块详细设计)
- [6. 业务流程与调度](#6-业务流程与调度)
- [7. 接口规范](#7-接口规范)
- [8. 部署与运维](#8-部署与运维)
- [9. 测试与质量](#9-测试与质量)
- [10. 安全与合规](#10-安全与合规)
- [11. 风险与应对](#11-风险与应对)
- [12. 变更记录](#12-变更记录)

---

## 1. 项目概述

### 1.1 项目背景

在内容创作、电商选品和市场营销中,捕捉早期热点并迅速跟进是获取流量的关键。热点的生命周期通常很短(< 12 小时),因此需要一套**无人值守、实时**的监控系统,在热点刚刚进入上升期时就触发预警,而非事后追溯。

### 1.2 核心目标

| 目标 | 说明 | 衡量方式 |
| --- | --- | --- |
| 自动化 | 7×24 小时无人值守,定时抓取与分析 | 作业调度率、故障自愈率 |
| 智能化 | 算法过滤无效词汇,精准识别"上涨趋势" | 误报率、漏报率 |
| 高可用 | 反爬应对(代理 IP、随机延迟)+ 异常重试 | 采集成功率、告警及时率 |
| 可观测 | 有日志、健康检查与失败告警 | MTTD(故障发现时间) |

### 1.3 范围界定(Scope)

- **In-Scope(MVP)**:微博热搜采集、数据清洗、指数(抖音/百度)获取、趋势分析、邮件告警、SQLite 归档、FastAPI 监控接口。
- **Out-of-Scope(增强)** 见 [11. 风险与应对](#11-风险与应对) 的 Roadmap。

### 1.4 名词术语

| 术语 | 定义 |
| --- | --- |
| 热点条目 | 微博热搜榜中的一条热搜(词 + 热度 + 排名) |
| 指数 | 某关键词在某一生态中的热度时间序列(抖音/百度) |
| 环比增长率 | 本期指数相对上期的增长比例 |
| 趋势斜率 | 时间序列线性回归拟合线的斜率(反映增长方向) |
| 上涨趋势 | 环比增长率和斜率同时满足阈值的状态 |

---

## 2. 总体架构

### 2.1 业务流程

```
[定时触发]
   │  (APScheduler Cron)
   ▼
[1. 微博热搜采集] ──► [2. 数据清洗与过滤] ──► [3. 指数数据获取] ──► [4. 趋势分析引擎]
   │                       │                       │                     │
   │                       │                       │                     ▼
   └──► 得到候选词(去噪)  └──► 产出候选词列表    └──► 指数时间序列      [5. 消息触达/告警]
                                                                         │
                                                                         ▼
                                                                    [6. 日志与归档]
                                                                         │
                                                                         ▼
                                                                  (SQLite + JSON 快照)
```

### 2.2 逻辑架构

```
┌───────────────────────────── 应用层 ─────────────────────────────┐
│  scheduler.py (APScheduler)  │  api.py (FastAPI 监控接口,可选)   │
└───────────────────────────────┬─────────────────────────────────┘
                                │ 编排
┌───────────────────────────────▼─────────────────────────────────┐
│  services 服务层(业务逻辑 / SRP)                                     │
│  collector · cleaner · index_fetcher · trend_analyzer           │
│  notifier · archive                                             │
└───────────────────────────────┬─────────────────────────────────┘
                                │ 访问
┌───────────────────────────────▼─────────────────────────────────┐
│  storage 数据访问层(Repository) — SQLite / JSON                    │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│  utils 通用层: proxy · logger · retry                              │
└─────────────────────────────────────────────────────────────────┘
```

### 2.3 技术选型

| 模块 | 技术/工具 | 选型理由 |
| --- | --- | --- |
| 开发语言 | Python 3.9+ | 生态丰富,爬虫与数据处理库完善 |
| 网络请求 | requests | 处理常规 API 请求(微博 Ajax 接口) |
| 自动化浏览器 | Playwright | 处理巨量算数等复杂反爬、动态渲染页 |
| 数据分析 | NumPy | 线性回归计算斜率,性能高 |
| 任务调度 | APScheduler | 支持 Cron 表达式,优于内置 `schedule` |
| 代理 IP | 隧道代理 | 按流量计费、自动轮换,免维护 IP 池 |
| 归档存储 | SQLite + JSON 快照 | 零运维、随项目走,满足 MVP |
| 监控接口 | FastAPI + uvicorn | 轻量,暴露结果与手动触发 |
| 配置管理 | pydantic-settings + python-dotenv | 强类型、读 `.env`、敏感项分离 |

### 2.4 目录结构

```
redian/
├─ doc/dev.md                  # 本开发文档
├─ doc/API.md                  # 接口规范
├─ CHANGELOG                   # 变更日志
├─ README.md                   # 使用说明
├─ requirements.txt
├─ .env.example                # 配置模板
├─ .gitignore
├─ Dockerfile
├─ docker-compose.yml
├─ config/settings.py          # 强类型配置(读 .env)
├─ app/
│  ├─ main.py                  # 入口(调度器 + 可选 FastAPI)
│  ├─ models.py                # 数据模型(dataclass / pydantic)
│  ├─ services/                # 业务逻辑:(collector / cleaner /
│  │                           #   index_fetcher / trend_analyzer /
│  │                           #   notifier / archive)
│  ├─ storage/repository.py    # SQLite 归档 Repository
│  └─ utils/                   # proxy · logger · retry
├─ tests/                      # 单测(纯逻辑,无网络)
└─ scripts/generate_doc.py     # (可选)从本文档生成 Word 版
```

---

## 3. 数据模型

> 内部管道使用 `@dataclass` 薄 DTO;API 层复用 pydantic 模型(见 doc/API.md)。

### 3.1 热点条目 `HotItem`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| rank | `int` | 热搜排名 |
| title | `str` | 热搜词 |
| heat | `int` | 热度值(微博) |
| category | `str \| None` | 榜单分类(如 社会/娱乐) |
| url | `str \| None` | 原始链接 |
| tag | `str \| None` | 标签(新 / 热 / 爆) |
| captured_at | `datetime` | 采集时间戳 |

### 3.2 指数采样点 `IndexPoint`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| ts | `datetime` | 采样时间 |
| value | `float` | 指数值 |

### 3.3 指数序列 `TrendSeries`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| keyword | `str` | 关键词 |
| source | `str` | 数据源:`douyin` / `baidu` |
| points | `list[IndexPoint]` | 时间序列(升序) |
| fetched_at | `datetime` | 获取时间 |

### 3.4 趋势分析结果 `TrendAnalysis`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| keyword | `str` | 关键词 |
| source | `str` | 数据源 |
| growth | `float` | 环比增长率(本期 vs 上期) |
| slope | `float` | 线性回归斜率 |
| rising | `bool` | 是否判定为上涨趋势(双条件同时满足) |
| decided_at | `datetime` | 判定时间 |

### 3.5 告警 `Alert`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| keyword | `str` | 触发告警的关键词 |
| reason | `str` | 触发原因 |
| sources | `list[dict]` | 涉及的指数/指标快照 |
| triggered_at | `datetime` | 触发时间 |

---

## 4. 配置中心

所有配置统一由 `config/settings.py` 的 `Settings`(pydantic-settings)读入,来源 `.env`。

| 配置项 | 环境变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 微博 Cookie | `WEIBO_COOKIE` | 空 | 微博登录态,失效会触发告警 |
| 百度指数 Cookie | `BAIDU_COOKIE` | 空 | 百度指数降级源登录态 |
| 代理入口 | `PROXY_URL` | 空 | 隧道代理地址(HTTP/SOCKS) |
| 代理账号 | `PROXY_USER` | 空 | 隧道代理鉴权账号 |
| 代理密码 | `PROXY_PASS` | 空 | 隧道代理鉴权密码 |
| 是否启用代理 | `USE_PROXY` | `false` | 本地调试可关闭 |
| 增长阈值 | `GROWTH_THRESHOLD` | `0.30` | 环比增长率判定阈值 |
| 斜率阈值 | `SLOPE_THRESHOLD` | `0.0` | 线性回归斜率阈值 |
| 最小热度 | `MIN_HEAT` | `200000` | 候选词清洗下限 |
| 候选词数量 | `TOP_N` | `10` | 进入指数分析的热搜词数量 |
| 分析最少样本 | `MIN_SAMPLES` | `3` | 线性回归所需的最少指数点数 |
| 调度 Cron | `JOB_CRON` | `*/30 * * * *` | 采集与分析周期 |
| SMTP 主机 | `SMTP_HOST` | 空 | 邮件服务器 |
| SMTP 端口 | `SMTP_PORT` | `465` | 邮件服务器端口(SSL) |
| SMTP 账号 | `SMTP_USER` | 空 | 发件账号 |
| SMTP 密码 | `SMTP_PASS` | 空 | SMTP 授权码 |
| 收件人列表 | `NOTIFY_TO` | 空 | 逗号分隔 |
| 是否开发模式(不发邮件) | `IS_DEV` | `true` | 关闭外发告警 |

---

## 5. 核心模块详细设计

### 5.1 代理 IP 封装 `utils/proxy.py`

- 职责:统一构造作用于所有外部请求的 `proxies` 字典。
- 接口:`get_proxies(settings) -> dict[str, str] | None`
- 规则:`USE_PROXY=false` 返回 `None`;否则按 `http/https` 构造隧道代理,携带 `PROXY_USER/PROXY_PASS`。
- 注意:隧道代理自动轮换出口,无需维护 IP 池。

### 5.2 微博热搜采集 `services/collector.py`

- 职责:请求微博热搜 Ajax 接口,得到 `list[HotItem]`。
- 接口:`fetch_hot_search(settings, session) -> list[HotItem]`
- 实现要点:
  - 目标接口:`https://weibo.com/ajax/side/hotSearch`(GET,携带 Cookie)。
  - 必须经过代理,带随机 UA。
  - 用 `utils/retry.py` 包裹,处理超时/5xx;状态码异常时抛出 `CollectionError`。
  - Cookie 失效(返回错误码/重定向到登录)时抛出 `AuthError`,由上层触发人工更新告警。

### 5.3 数据清洗与过滤 `services/cleaner.py`

- 职责:剔除广告/低频词,规范化,去重,产出去噪后的候选词。
- 接口:`clean(items: list[HotItem], settings) -> list[HotItem]`
- 规则:
  1. 过滤 `heat < MIN_HEAT` 的词。
  2. 过滤含黑名单词(置顶词、广告词、`置顶`/`广告` 等)。
  3. 按标题去重,保留首次出现。
  4. 截取 `top_n`,按热度降序。
- 纯函数,核心逻辑可单测。

### 5.4 指数数据获取 `services/index_fetcher.py`

- 职责:对候选词逐一获取指数时间序列,得到 `list[TrendSeries]`。
- 设计:定义抽象源 `IndexSource`,子类 `DouyinIndexSource`、`BaiduIndexSource`,由 `IndexFetcher` 按降级策略选用。
- 降级策略(见 dev.md 原始设计):优先抖音(巨量算数);失败/不支持时回退百度指数。
  - `DouyinIndexSource`:用 Playwright 无头浏览器注入 Cookie,拦截 XHR 获取 JSON。为降低依赖与反爬风险,MVP 将 Playwright 封装为可插拔适配器,失败即降级。
  - `BaiduIndexSource`:请求百度指数接口(PC 网页版),返回 `list[IndexPoint]`。
- 接口:`fetch(keyword: str, fallback_chain: list[str]) -> TrendSeries | None`
- 单源失败不抛全局异常,记日志并继续下一个词/降级。

### 5.5 趋势分析引擎 `services/trend_analyzer.py`

- 职责:对每个 `TrendSeries` 做双重校验,输出 `TrendAnalysis`。
- 接口:`analyze(series: TrendSeries, settings) -> TrendAnalysis`
- 算法(双重校验,**两者同时满足**才认定为上涨):
  1. **环比增长率**:`growth = (latest - previous) / previous`(`,`previous == 0` 时记 `growth=None` 不达标)。
  2. **线性回归斜率**:用 `np.polyfit(ts, values, 1)` 计算斜率 `slope`。
  3. 判定:`rising = growth > GROWTH_THRESHOLD and slope > SLOPE_THRESHOLD`。
- **样本要求**:`len(points) < MIN_SAMPLES` 时直接判定不可分析(避免过少样本误判)。
- 阈值与指标可为纯函数,核心逻辑可单测。

### 5.6 消息触达/告警 `services/notifier.py`

- 职责:将告警发送到配置的通道。
- 设计:定义抽象接口 `Notifier`:`notify(alert: Alert, context: str)`。
  - `EmailNotifier`:SMTP SSL 发送,正文含关键词、增长率、斜率、来源快照。
  - `DingTalkNotifier`(增强)/`WebhookNotifier`(增强):预留实现。
- 约定:
  - `IS_DEV=true` 时仅打印日志,不外发。
  - `AuthError`(Cookie 失效)触发"系统级告警",渠道与热点告警一致。
- 发信异常必须 try/except,返回标准化错误,不阻塞主流程。

### 5.7 日志与归档 `services/archive.py` + `storage/repository.py`

- 职责:持久化原始热搜、分析结果、告警记录,并提供查询。
- `ArchiveRepository`(SQLite):
  - 表:`hot_items`、`trend_analysis`、`alerts`、`runs`。
  - 方法:`save_run(run)`, `save_items(items)`, `save_analysis(analysis)`, `save_alert(alert)`, `latest_analysis(limit) `。
- 每次运行额外写一份 JSON 快照到 `data/snapshots/YYYYMMDD_HHMMSS.json`(便于人工排查)。

---

## 6. 业务流程与调度

`app/main.py` 提供两种启动方式:

1. **调度模式(默认)**:`APScheduler` 按 `JOB_CRON` 触发 `run_pipeline()`。
2. **API 模式**:`uvicorn app.api:app`,可由 `POST /api/v1/runs` 手动触发。

`run_pipeline()` 编排:

```
run_id = timestamp
save_run(start)
items = collector()                    # 1 采集
candidates = cleaner(items)            # 2 清洗
series = index_fetcher.fetch_all(candidates)  # 3 指数
analyses = trend_analyzer.analyze_all(series) # 4 分析
rising = [a for a in analyses if a.rising]
if rising:
    notifier.notify_alerts(rising)     # 5 告警
save_items / save_analysis / snapshot   # 6 归档
save_run(end, status)
```

错误处理:整条管道包裹在最外层 try/except;任一步失败默认执行"失败告警 + 记录 `runs.status=failed`",避免静默失败。

---

## 7. 接口规范

详见 [doc/API.md](API.md)。摘要:

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| GET | `/api/v1/trends/latest` | 最近一次上涨趋势列表 |
| GET | `/api/v1/alerts/latest` | 最近告警 |
| POST | `/api/v1/runs` | 手动触发一次全量管道 |
| GET | `/api/v1/runs/latest` | 最近一次运行状态 |

---

## 8. 部署与运维

### 8.1 Docker 容器化

- `Dockerfile`:基于 `python:3.11-slim`,安装依赖 + `playwright install chromium`。
- `docker-compose.yml`:单容器即可运行调度器;可额外暴露 API 端口。
- 所有外部请求必须走隧道代理,避免服务器 IP 被连坐封禁。

### 8.2 环境准备

```bash
cp .env.example .env     # 填写 Cookie / 代理 / SMTP / 阈值
pip install -r requirements.txt
```

### 8.3 运行

```bash
# 调度模式
python -m app.main
# API 模式(容器则映射端口)
uvicorn app.api:app --host 0.0.0.0 --port 8080
```

---

## 9. 测试与质量

- **单元测试**:`tests/`,覆盖 `cleaner`、`trend_analyzer`、`proxy` 等纯逻辑(勿依赖网络)。
- **静态检查**:`mypy`(类型)、`ruff`(lint/format)。
- **CI**(增强):push 后执行 `ruff check` + `mypy app` + `pytest -q`。

---

## 10. 安全与合规

- 敏感信息(Cookie / 代理密码 / SMTP 授权码)一律经 `.env` 注入,禁止硬编码;`.env` 加入 `.gitignore`。
- 日志对敏感字段脱敏(Cookie / 密码部分掩码)。
- 控制抓取频率(随机 3–8 秒休眠、限速),遵守目标站点 robots 与 ToS。

---

## 11. 风险与应对

| 风险 | 应对 | 阶段 |
| --- | --- | --- |
| 微博 Cookie 失效 | 监控状态码,触发系统级告警提示人工更新 | MVP |
| 巨量算数反爬升级 | 严格住宅隧道代理 + 随机休眠 3–8s + Playwright 注入 | MVP |
| 邮件被判垃圾 | 企业邮箱 / 正规 SMTP 授权码,规避营销词 | MVP |
| 指数源缺失(微信) | MVP 用百度/头条指数替代,后期接第三方 API | MVP |
| 误报率高 | 双重校验 + 样本数下限 + 阈值可配置调参 | MVP |
| 需要多渠道触达 | 通知接口抽象,扩展钉钉/企业微信/Webhook | 增强 |
| 需要历史可视化 | 前端看板挂载 `GET /api/v1/trends/latest` | 增强 |
| 指数 API 需付费 | 预留第三方数据源适配器 | 增强 |

---

## 12. 变更记录

> 变更记录统一写入 `CHANGELOG`。

- 2026-08-29:文档从脚本形态重写为 Markdown 开发文档,补齐数据模型 / 配置 / 模块接口 / 部署 / 安全 / 风险。
