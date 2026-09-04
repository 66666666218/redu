# 热点追踪与自动化监控系统 — 开发文档

> 版本: v1.0(完善版)　|　最后更新: 2026-08-29
> 技术栈: Python 3.9+ / requests / NumPy / APScheduler / FastAPI / SQLite(Playwright 仅用于 `scripts/probe_*.py` 抓包探测)

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
| 网络请求 | requests | 处理常规 API 请求(微博 Ajax、闲鱼 mtop、抖音热点宝) |
| 自动化浏览器 | Playwright | **仅开发期**:`scripts/probe_*.py` 抓包核验接口是否仍免签名;运行时采集不依赖 |
| 数据分析 | NumPy | 线性回归计算斜率,性能高 |
| 任务调度 | APScheduler | 支持 Cron 表达式,优于内置 `schedule` |
| 代理 IP | 隧道代理 | 按流量计费、自动轮换,免维护 IP 池 |
| 归档存储 | SQLite + JSON 快照 | 零运维、随项目走,满足 MVP |
| 监控接口 | FastAPI + uvicorn | 轻量,暴露结果与手动触发 |
| 配置管理 | pydantic-settings + python-dotenv | 强类型、读 `.env`、敏感项分离 |

### 2.4 目录结构

```
redian/
├─ doc/                         # dev.md(本开发文档) / API.md(接口规范)
├─ CHANGELOG / README.md / requirements.txt / .env.example
├─ Dockerfile / docker-compose.yml
├─ config/settings.py           # 强类型配置(读 .env),含调度/飞书/代理等
├─ app/
│  ├─ main.py                   # 独立调度进程入口(python -m app.main)
│  ├─ platform.py               # 应用工厂:装配路由/中间件/静态托管(薄)
│  ├─ auth.py  security.py      # 用户鉴权(JWT/bcrypt)与 Cookie 加密
│  ├─ admin.py                  # 管理后台服务(工作台/用户/日志/洞察/导出)
│  ├─ api/                      # ⭐ 按领域拆分的 APIRouter
│  │  ├─ deps.py                #   请求模型 + 依赖 + 登录限速
│  │  └─ auth/cookies/dashboard/collect/alerts/admin/misc.py
│  ├─ db/                       # SQLAlchemy 数据层
│  │  ├─ models.py              #   ORM 模型(users/cookies/douhot/...)
│  │  ├─ database.py            #   引擎/会话/init_db/迁移/db_status
│  │  └─ repository.py          #   ⭐ Repository 数据访问层(按聚合取数)
│  ├─ services/                 # 业务逻辑(SRP,按领域)
│  │  ├─ collector(微博)/xianyu/douhot + douhot_client(直连)
│  │  ├─ tenant(采集编排)/tenant_base(共享)/alert_service/schedule_service/scheduler
│  │  ├─ keyword_agent(预测)/keyword_watch(关键词监控)/feishu + feishu_client(飞书)
│  │  ├─ xianyu_analytics(闲鱼分析)/cookie_store/notifier
│  │  └─ trend_analyzer(纯函数)/proxy·logger·retry
│  └─ utils/                    # proxy · logger · retry
├─ tests/                       # 单测(纯逻辑,无网络)
└─ scripts/                     # probe_* 抓包探测 / shoot_ui 截图
```

> ⚠️ 历史清理:早期**单用户微博指数管线**(`app/models.py` dataclass、
> `services/cleaner.py`、`services/index_fetcher.py`、`storage/repository.py`、`services/archive.py`)
> 已随多租户改造移除——只保留被复用的 `collector.fetch_hot_search`(微博热搜)与
> `trend_analyzer.compute_growth/compute_slope`(智能体)。`platform.py` 原 553 行/50 路由
> 已拆到 `app/api/`。

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
| 抖音指数 Cookie | `DOUYIN_COOKIE` | 空 | 抖音创作者中心/巨量算数登录态 |
| 闲鱼热榜关键词 | `XIANYU_KEYWORDS` | `ps教程,网盘资源,代充...` | 虚拟商品搜索关键词(逗号分隔) |
| 闲鱼热榜条数 | `XIANYU_TOP_N` | `50` | 热榜条数上限 |
| 闲鱼 Cookie 文件 | `GOOFISH_COOKIE_FILE` | `data/goofish_cookie.txt` | 闲鱼登录 Cookie 文件(gitignored) |
| 代理入口 | `PROXY_URL` | 空 | 隧道代理地址(HTTP/SOCKS) |
| 代理账号 | `PROXY_USER` | 空 | 隧道代理鉴权账号 |
| 代理密码 | `PROXY_PASS` | 空 | 隧道代理鉴权密码 |
| 是否启用代理 | `USE_PROXY` | `false` | 本地调试可关闭 |
| 提取式代理API | `PROXY_EXTRACT_URL` | 空 | 厂商 getips 完整 URL(含 trade_no/sign);设置后优先走代理池 |
| 提取池刷新秒数 | `PROXY_REFRESH_SECONDS` | `170` | 代理 IP 约 3 分钟有效,需周期刷新 |
| 增长阈值 | `GROWTH_THRESHOLD` | `0.30` | 环比增长率判定阈值 |
| 斜率阈值 | `SLOPE_THRESHOLD` | `0.0` | 线性回归斜率阈值 |
| 最小热度 | `MIN_HEAT` | `200000` | 候选词清洗下限 |
| 候选词数量 | `TOP_N` | `10` | 进入指数分析的热搜词数量 |
| 分析最少样本 | `MIN_SAMPLES` | `3` | 线性回归所需的最少指数点数 |
| 指数源优先级链 | `INDEX_SOURCES` | `weibo` | 逗号分隔、顺序即优先级;可用 `weibo`/`douyin`/`baidu` |
| 指数源模拟 | `MOCK_INDEX` | `true` | 本地/测试用合成指数,免真实抓取 |
| 告警模式 | `ALERT_MODE` | `both` | `both`=所有信号源同涨才告警;`any`=任一源涨即告警 |
| 采集频率 | — | — | 无全局 Cron:由每个用户在「采集频率」页自行设置(见 §6.1) |
| 内嵌调度器 | `SCHEDULER_ENABLED` | `true` | 随 API 进程启动调度器;多 worker 部署须设 false 并单起调度容器 |
| 请求随机间隔 | `REQUEST_DELAY_SECONDS` | `2.5` | 每次外部请求间的随机基础间隔(秒) |
| 抖音热点 Cookie 文件 | `DOUHOT_COOKIE_FILE` | `data/douhot_cookie.txt` | 抖音热点宝 Cookie(gitignored) |
| 抖音热词条数 | `DOUHOT_TOP_N` | `50` | 内容词趋势条数上限 |
| 抖音告警条数上限 | `DOUHOT_ALERT_MAX` | `5` | 单次判涨告警上限(防刷屏) |
| 抖音告警冷却 | `DOUHOT_ALERT_COOLDOWN_HOURS` | `24` | 同一内容词告警冷却(小时) |
| SMTP 主机 | `SMTP_HOST` | 空 | 邮件服务器 |
| SMTP 端口 | `SMTP_PORT` | `465` | 邮件服务器端口(SSL) |
| SMTP 账号 | `SMTP_USER` | 空 | 发件账号 |
| SMTP 密码 | `SMTP_PASS` | 空 | SMTP 授权码 |
| 收件人列表 | `NOTIFY_TO` | 空 | 逗号分隔 |
| 是否开发模式(不发邮件) | `IS_DEV` | `true` | 关闭外发告警 |

---

## 5. 核心模块详细设计

### 5.1 代理 IP 封装 `utils/proxy.py`

- 职责:统一构造作用于所有外部请求的代理配置,避免在每个请求中硬编码。
- 接口:
  - `get_proxies(settings) -> dict[str, str] | None`:给 requests 的 `http`/`https` 代理。
  - `proxy_url(settings) -> str | None`:单条代理 URL(含鉴权)。
  - `playwright_proxy(settings) -> dict | None`:给 Playwright `browser.launch(proxy=...)`。
  - `ProxyPool`:提取式代理池(见下)。
- 两种模式(§4 配置):
  - **隧道代理**:`USE_PROXY=true` + `PROXY_URL/USER/PASS`,单网关静态。
  - **提取式代理池**:配置 `PROXY_EXTRACT_URL`(厂商 getips 完整 URL,含 `trade_no/sign`),自动拉取一批 `ip:port:user:pass`,按 `PROXY_REFRESH_SECONDS` 刷新、随机轮换;`get_proxies()` 优先走代理池。
- 兼容:代理地址可带或不带 scheme;支持 `http/https`/`socks5`;socks5 时 Playwright 走直连并提示。
- 健壮性:`ProxyPool.parse_line` 会校验 `ip:port` 形状——厂商额度到期时会返回一段 JSON(`{"code":405,"msg":"业务已到期..."}`),按冒号切开同样有 4 段,只看段数会拼出垃圾代理 URL 把所有采集一起带崩;校验失败的行直接丢弃,池空则退回直连。
- 部署:生产必须启用代理(防服务器 IP 连坐);若用提取式,注意 IP 约 3 分钟有效,超时需重新提取;白名单/账密按服务商方式配置。

### 5.2 微博热搜采集 `services/collector.py`

- 职责:请求微博热搜 Ajax 接口,得到 `list[HotItem]`。
- 接口:`fetch_hot_search(settings, session) -> list[HotItem]`
- 实现要点:
  - 目标接口:`https://weibo.com/ajax/side/hotSearch`(GET,携带 Cookie)。
  - 必须经过代理,带随机 UA。
  - 用 `utils/retry.py` 包裹,处理超时/5xx;状态码异常时抛出 `CollectionError`。
  - Cookie 失效(返回错误码/重定向到登录)时抛出 `AuthError`,由上层触发人工更新告警。

### 5.3 数据清洗与过滤 `services/cleaner.py`

> ⚠️ **已废弃**:该模块已随多租户改造移除,保留仅供理解旧架构。

- 职责:剔除广告/低频词,规范化,去重,产出去噪后的候选词。
- 接口:`clean(items: list[HotItem], settings) -> list[HotItem]`
- 规则:
  1. 过滤 `heat < MIN_HEAT` 的词。
  2. 过滤含黑名单词(置顶词、广告词、`置顶`/`广告` 等)。
  3. 按标题去重,保留首次出现。
  4. 截取 `top_n`,按热度降序。
- 纯函数,核心逻辑可单测。

### 5.4 指数数据获取 `services/index_fetcher.py`

> ⚠️ **已废弃**:该模块已随多租户改造移除,保留仅供理解旧架构。

- 职责:对候选词逐一获取指数时间序列,得到 `list[TrendSeries]`。
- 设计:定义抽象源 `IndexSource`,子类 `WeiboHeatIndexSource`(跨轮次累积的微博热度序列)、`DouyinIndexSource`、`BaiduIndexSource`,由 `IndexFetcher` 按降级策略选用。降级链顺序由配置 `index_sources` 决定(逗号分隔、顺序即优先级,如 `weibo,douyin,baidu`)。
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
- 设计:定义抽象接口 `Notifier`,统一走 `send(subject, body)`(老 `notify(alert)` 已随单用户管线移除)。
  - `EmailNotifier`:SMTP SSL 发送,正文含关键词、增长率、斜率、来源快照。
  - `DingTalkNotifier`(增强)/`WebhookNotifier`(增强):预留实现。
- 约定:
  - `IS_DEV=true` 时仅打印日志,不外发。
  - `AuthError`(Cookie 失效)触发"系统级告警",渠道与热点告警一致。
- 发信异常必须 try/except,返回标准化错误,不阻塞主流程。

### 5.7 日志与归档 `services/archive.py` + `storage/repository.py`

> ⚠️ **已废弃**:该模块已随多租户改造移除,保留仅供理解旧架构。

- 职责:持久化原始热搜、分析结果、告警记录,并提供查询。
- `ArchiveRepository`(SQLite):
  - 表:`hot_items`、`trend_analysis`、`alerts`、`runs`。
  - 方法:`save_run(run)`, `save_items(items)`, `save_analysis(analysis)`, `save_alert(alert)`, `latest_analysis(limit) `。
- 每次运行额外写一份 JSON 快照到 `data/snapshots/YYYYMMDD_HHMMSS.json`(便于人工排查)。

### 5.8 闲鱼虚拟商品热榜 `services/xianyu.py`(独立数据源)

- 职责:按虚拟商品关键词搜索闲鱼,以闲鱼"综合"顺序作为热度基准,跨关键词聚合、去重、排名,输出热销虚拟商品榜。
- 技术:闲鱼 H5 的 mtop 接口(`mtop.taobao.idlemtopsearch.pc.search`,`appKey=34839810`),用 `GOOFISH_COOKIE_FILE` 里的登录 Cookie + 标准 mtop 签名(算法=md5(token&t&appKey&data)),返回真实商品;每次响应会刷新 `_m_h5_tk` 令牌。传输层用 **`curl_cffi`(impersonate="chrome")模拟 Chrome TLS/HTTP2 指纹**替代裸 `requests`(纯协议伪指纹,见下)。
- 排名规则:`hit_keywords`(命中关键词数)降序优先,其次 `best_rank`(综合序最小)升序。
- 接口:`run_xianyu() -> {run_id, count, items}`;API 见 `doc/API.md` §6;深采 `run_xianyu_deep` 抓商品详情(`mtop.taobao.idle.pc.detail`)取想要数/收藏/已售/卖家粉丝,写 `xianyu_daily` 快照。
- 说明:**搜索卡片不带"已售/想要数"(`want` 为空)**,故想要数必须抓详情页(`XIANYU_DETAIL_LIMIT`,默认 20 条);热榜综合序由 `hit_keywords` + `best_rank` 近似。**"只留虚拟商品"靠 `XIANYU_KEYWORDS` 虚拟词采集保证**——榜本身就没有实物,故**未做类目过滤**:类目仅在详情(`itemCatDTO`),按类目过滤需给候选逐个抓详情、会重新引入大量 mtop 请求加剧风控(与降风控目标冲突),且需一份易碎的类目ID白名单。闲鱼为合规公开检索,读自己登录态下的数据,注意频率与 ToS。
- **指纹与滑块(实测 `scripts/probe_xianyu_curl.py`)**:纯 `requests`(Python urllib3 TLS 指纹)极像机器人,易被触发滑块;`curl_cffi` 伪 Chrome 指纹是**防患于未然**层(主流闲鱼采集器 goofish_spider/cn-scraper-mcp 就这么做),能降低被标记概率。**但实测:同一 Cookie 已被反复 `FAIL_SYS_USER_VALIDATE` 时,`requests` 和 `curl_cffi` 都会被拦、且都不下发新 token**——此时是**账号/会话已被标记,指纹伪装救不了**。**恢复办法(已实测验证)**:在浏览器用该账号**手动过一次滑块**,或**换清洁出口 IP**(用单一稳定住宅 IP,别用轮换代理池——闲鱼 token/session 绑定 IP)。过滑块后 `_m_h5_tk` 刷新并带回 `x5sec` 验证 cookie,`requests`/`curl_cffi` **均恢复 `SUCCESS`**;注意过滑块**不是一劳永逸**,持续低频率 + 清洁 IP 才能减少复发(否则会再次被标记,靠既有降级 + 失败告警(含原因)兜底)。
- **风险控制错误模型**(mtop 网关):`FAIL_SYS_TOKEN_*` 令牌错误 → 刷新 `_m_h5_tk` 重试;`FAIL_SYS_RATE_LIMIT`/`FAIL_SYS_USER_LIMIT` 真限流 → 指数退避 30/90/180s 后抛 `XianyuRateLimit`;**`FAIL_SYS_USER_VALIDATE`= 人机验证(滑块),不是限流**——实测退避重试仍无效,改为**立即抛 `XianyuVerify`**,由上层识别为"需人工过滑块或更换出口 IP"。深采详情循环遇 `XianyuVerify`/`XianyuRateLimit` 即**停止抓取并保留已采部分**(状态 `partial`),不再连环猛打加剧风控;整轮被验证时 `run_xianyu_deep` 优雅返回 `status:"failed"` 而非 500。`check_collect_failures` 的飞书告警会带上最近一次失败原因(人工可据此行动)。

### 5.9 抖音热点·内容词趋势 `services/douhot.py` + `services/douhot_client.py`(独立数据源)

- 职责:监控抖音「生活服务热点中心」的**内容词趋势**(词 + 飙升指数 + 热度时间序列),判涨、入库、对外提供。
- 分层:`douhot_client.py` 只管取数(HTTP/封装拆解/翻页),`douhot.py` 只管字段解析(统一成 `{title, score, ...}`)。
- 技术:**纯 requests 直连,无需签名、无需浏览器**。实测(`scripts/probe_douhot_direct.py`、`scripts/probe_douhot_apis.py`)`douhot.douyin.com` 的榜单接口只校验登录 Cookie,把 `a_bogus`/`X-Bogus`/`_signature`/`msToken` 查询参数**全部剥掉仍返回真实数据**,且改 `page_num`/`date_window` 数据随之变化(服务端实算,非重放缓存)。需 `DOUHOT_COOKIE_FILE` 里的授权 Cookie(登录"生活服务热点中心")。
- 接口约定:响应封装 `{"code":0,"data":{...}}`;Cookie 失效为 `{"code":8,"data":"用户未登录"}` → 抛 `DouhotAuthError`。内容词 `page_size` 服务端硬顶 24(要更多须翻页),搜索/视频/话题榜 `page_size` 可放大到 50。
- 榜单:内容词 `hot_word/query_list`、搜索榜 `hot_search/query_list`、视频榜 `material/video_billboard`、话题榜 `material/challenge_billboard`、订阅 `subscribe/query_list`(后四者按用户关注类型按需拉,失败降级为空列表,不中断主采集)。
- 数据为**明文 JSON**(非巨量算数那种加密),词条自带 `trends` 热度序列。
- 入库:`douhot_words` 表(score/latest_value/trend_delta/…),每次运行一条快照;`run_douhot_trend() -> {run_id, count, items}`。
- 性能:内容词 50 条约 1.3s(旧浏览器方案 24 条约 15s,且只能拿首屏);五个榜单全量约 8s(旧方案需开 5 次浏览器,约 60s+)。条数由 `DOUHOT_TOP_N` 控制(自动翻页,上限 200)。
- 历史:曾用 Playwright 打开热点页拦截响应,重、吃内存、子Tab 点击常因改版失效;直连后已移除运行时浏览器依赖(Playwright 仅保留给 `scripts/probe_*.py` 抓包核验)。
- 跨轮判涨:每轮把词条飙升指数入库,`run_douhot_trend` 末尾按词取历史序列,复用双重校验(环比涨幅 > `GROWTH_THRESHOLD` 且 斜率>0)判涨;命中则邮件告警,带冷却去重(`DOUHOT_ALERT_COOLDOWN_HOURS`)与单次上限(`DOUHOT_ALERT_MAX`),返回 `rising`。(需 ≥2 轮历史才生效。)

### 5.10.1 关键词监控智能体 `services/keyword_agent.py`(可选)

用户可为**任意关键词**设置监控(不限于 top100)——采集时会**按关键词定向查询**
`query_list`(请求体 `keyword` 字段),即使该词不在当前榜单里也能取到它的飙升指数
与趋势(实测 "世界杯" 等榜外词可查到 score=211、trends=35)。

- **定向查询**:`douhot_client.hot_word_keyword(keyword)` → 查接口;`douhot.fetch_keyword_heat`
  优先取精确匹配,否则取相关结果第一,无结果返回冷启动零值。榜单类型(内容词/搜索/
  视频/话题/订阅)可选,`run_douhot::_record_douhot_watch_snaps` 对 `word` 类走定向查询,
  其余榜在已采集榜单里找(词须在榜内)。
- **趋势分析 + 预测**:`keyword_agent.analyze(keyword, scores)` 用历史热度序列(多轮采集的
  score 按时间排)算环比涨幅、线性回归斜率,外推**下一轮预测值**,并判定 上升期/回落期/
  震荡/平稳,生成一段中文摘要。纯 NumPy、离线可用。进阶:对序列算**置信度**(样本数 + 拟合 R²)、**加速度**(前后半段斜率差,>0 = 上升加速)、**爆发预警**标记;并可按快照时间算**历史回溯**(首次上涨/峰值/持续时长);命中爆发信号且配置了飞书的关注词,会在采集后推送`🔮 智能体预测·可能爆发` 到群(复用 `feishu_alerts` 冷却去重)。仪表盘关键词卡片按爆发→预测→环比排序并显示置信度。
- **展示**:仪表盘"关键词监控 · 智能体"卡片——当前分、环比、预测下一轮、迷你趋势线、
  分析摘要。`douhot_watch_analytics` 返回 `trend_label / forecast_next / summary / series`。

### 5.11 飞书群机器人 `services/feishu.py`(可选)

监控结果可推送到飞书群,分**两条通道**:

1. **每日热点日报** `run_feishu_daily()`:按 `FEISHU_DAILY_CRON`(默认每天 08:00)推送三板块
   "最近一批 vs 上一批"榜单对比——每个话题标注排名涨跌(如 `🔥+3名`/`📉-2名`)或新增(✅新增),
   末尾附一段趋势分析(上升最多/新增/回落最多)。`build_daily(db, uid, settings)` 生成文本。
2. **实时提醒** `run_feishu_realtime(section, user_id)`:每次采集成功后调用,只把**当日新增**、
   **排名跳升 ≥ `FEISHU_HOT_RANK_JUMP` 名**、或**分值环比 ≥ `FEISHU_HOT_RATIO`** 的话题立即推群,
   并按 `FEISHU_ALERT_COOLDOWN_HOURS` 去重(表 `feishu_alerts`),防刷屏。

- **签名校验**:飞书自定义机器人若开启"签名校验",请求体须带 `sign`。
  官方算法:`string_to_sign = f"{timestamp}\\n{secret}"`,`sign = base64(HmacSHA256(string_to_sign, ""))`
  (以 string_to_sign 为 HMAC key、空消息)。**注意不要把 secret 当 key,反之会 `sign match fail`。**
  密钥为空则不带 `sign`(即机器人关闭签名校验)。
- **IP 白名单**:机器人若开了 IP 白名单,须把**服务器公网 IP** 加进去,否则回报 `19022 Ip Not Allowed`。
- **触发挂载**:实时提醒在 `app/services/scheduler.py::collect_tick` 与手动采集接口 `/api/collect/{platform}`
  成功采集后调用;日报挂在 `scheduler.build_jobs()` 的 `feishu_daily` job。
- 未配置 `FEISHU_WEBHOOK` 时两条通道均自动关闭(不影响其他功能)。

---

## 6. 业务流程与调度

### 6.1 采集频率:每用户自定义

采集频率由**每个用户自己设置**(不再是全局 Cron),存于 `user_schedules` 表:
`(user_id, section, interval_minutes, enabled, last_run_at)`,三个板块(微博/闲鱼/抖音)分别设置。

- **调度模型**:`app/services/scheduler.py::collect_tick()` **每分钟**跑一次,取出所有
  "已启用 且 距 `last_run_at` 已满 `interval_minutes`"的记录逐个执行。
  用"间隔 + 上次运行时间"而非每人一条 Cron 作业,好处是**改设置无需重建调度作业**,
  下一分钟即按新频率生效,也天然避开"API 进程改了配置、调度进程不知道"的跨进程同步问题。
- **下限保护**:`MIN_INTERVAL=10` 分钟,在**后端**强制(`schedule_service.normalize_interval`)。
  三个板块都是登录态接口,过于频繁会触发平台风控或导致 Cookie 失效。
- **自动纳入**:每轮 tick 开头调用 `ensure_all_users()`,为新注册用户(及本功能上线前的老用户)
  补齐默认频率记录(30 分钟),不必等他们打开一次设置页。
- **缺 Cookie 跳过**:未配置对应平台 Cookie 的板块直接跳过且**不标记** `last_run_at`——
  既不会每轮刷一条"未配置 Cookie"的失败记录,用户配好后下一分钟也能立刻开跑。
- **失败也标记**:采集失败照样写 `last_run_at`,否则失败任务会每分钟重试把三方接口打爆;
  失败重试交给 `retry_failed_runs`(每 30 分钟、单条最多 3 次)。
- 接口见 `doc/API.md` §8;前端页面 `frontend/src/views/Schedule.vue`(`/schedule`)。

### 6.2 调度进程落地

调度器随 **API 进程**启动(`app/platform.py` 的 lifespan → `scheduler.start()`),
所以单容器部署只跑 `uvicorn app.platform:app` 即可,无需额外调度容器。

后台共 3 个作业:

| 作业 | 周期 | 说明 |
| --- | --- | --- |
| `collect_tick` | 每分钟 | 按各用户设置的频率采集 |
| `alert_fixed_time` | 每分钟 | 定时告警摘要(用户设定的发送时刻) |
| `auto_retry_failed_runs` | 每 30 分钟 | 重试近 24h 内失败的采集(≤3 次) |

> ⚠️ **多 worker 部署**:若用 `uvicorn --workers N`,每个 worker 都会启动一份调度器导致重复采集。
> 此时须设 `SCHEDULER_ENABLED=false`,并单独起一个调度容器跑 `python -m app.main`
> (独立模式复用同一套 `build_jobs`,行为完全一致)。

### 6.3 单次采集的编排

`run_pipeline()` 编排:

```
run_id = timestamp
save_run(start)
items = collector()                       # 1 采集
candidates = cleaner(items)               # 2 清洗
series_map = index_fetcher.fetch_parallel(candidates)  # 3 指数(并行多源)
analyses = 逐序列 Analyze                  # 4 分析
rising_keywords = 按 ALERT_MODE 交叉验证    # 5 告警(both/any)
notifier.send(subject, body)              #   5 触达(邮件/飞书)
save_items / save_analysis / snapshot     # 6 归档
save_run(end, status)
```

> 交叉验证:`fetch_parallel` 对每个候选词**并行采集所有信号源**(如 `weibo` + 未来 `wechat`);
> `ALERT_MODE=both` 要求所有源同涨才告警(置信度更高),`any` 任一源涨即告警(召回更高)。

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

- `Dockerfile`:多阶段——阶段1 构建 Vue3 前端,阶段2 基于 `python:3.11-slim` 安装依赖。采集全部走 HTTP 直连,**不再安装 chromium**(镜像更小、内存占用更低)。
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
| 抖音指数数据加密 | 指数接口返回加密密文,需复刻响应解密(违反安全边界,不做);建议用微博热度序列替代 | MVP |
| 百度指数 WAF 拦截 | 补全 AJAX 请求头 / 走隧道代理;仍受限则改用第三方指数 API 或退回到微博自身热度序列 | MVP |
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
