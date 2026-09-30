# 项目架构（v2.1.0，2026-10-01）

> 本文是项目的**整体说明**：分层、数据流、部署拓扑、外部依赖与作业矩阵。
> 快速上手见 README；接口规范见 doc/API.md；运维手册见 doc/operations.md。

## 1. 总体形态：单体应用 + 可选外部容器

本项目是**一个 Python 单体应用**（`app/` 包），不是微服务：

```
D:\code\redian
├── app/                 # 应用本体（FastAPI + APScheduler 内嵌调度器）
│   ├── api/             # 路由层：14 文件 1533 行（auth/cookies/wechat/collect/admin/...）
│   ├── services/        # 业务层：50 文件 13623 行
│   │   ├── wechat/      #   公众号域包（v2.1.0 自 2917 行上帝模块拆分）
│   │   ├── feishu/      #   飞书域包（_cards 排版 / _jobs 推送）
│   │   ├── hotspot_agent / agent_learning / early_agent   # 热点选题 Agent 家族
│   │   └── ...          #   采集器（weibo/xianyu/douhot/baidu/quark/weread）
│   ├── db/              # 数据层：models/repository/database/maintenance
│   └── utils/           # 基础设施：logger/proxy(代理免疫)/retry
├── frontend/            # Vue SPA 源码 → 构建到 app/static/spa（后端挂载）
├── config/              # pydantic-settings 配置（.env 驱动）
├── scripts/             # 运维剧本（回填/探测/看门狗/一键脚本）
├── tests/               # 544 项测试（含未定义名守卫 test_undefined_names）
├── data/                # SQLite 数据（platform.db + archive/ 归档）
└── docker-compose.yml   # 远程 VPS 部署（MySQL 方言）
```

**外部可选容器**（不合并进代码，见 §4）：`we-mp-rss`（WeRSS 列表源，127.0.0.1:8001）。

## 2. 分层与数据流

```
                          ┌─────────────── 采集层（调度器驱动）────────────────┐
  微博/百度/抖音/闲鱼  ──▶  collector(douhot/window) / tenant(run_*) ──▶ SQLite
  公众号(微信读书/WeRSS/自研Wemp) ──▶ wechat/_listen 分组轮换监听 ──▶ SQLite
                          └──────────────────────┬───────────────────────────┘
                                                 ▼
                    分析层: keyword_agent(涨跌) → hotspot_agent(选题建议+机会分)
                            → 结算(盘链扩散 repost_gain) → agent_learning(回测权重)
                                                 ▼
                    推送层: feishu/_jobs(日报/洞察/实时/资源共振卡) + notifier(邮件)
                            + alerts(站内告警) + 飞书告警(失败/恢复确认/停摆)
                                                 ▼
                    接口层: app/api/*（82 路由，见 API.md）──▶ Vue SPA（挂载 /spa）
```

**多租户**：所有业务表带 `user_id`；调度按用户 `user_schedules` 开关；凭据经 `cookie_store` 加密隔离。

## 3. 部署拓扑（双环境分工，operations.md §12）

| 环境 | 承载 | 原因 |
|---|---|---|
| **本机（家庭 Windows）** | 公众号监听 + 闲鱼 + 全部推送/Agent/前端 | 微信读书 Cookie 绑家宽出口 IP；闲鱼住宅 IP 过滑块 |
| **远程 VPS（redu.tian1she.xyz）** | 微博/百度/抖音热点采集 | 这些 Cookie 在 VPS 有效；MySQL 部署 |
| **本机容器** | WeRSS（可选列表源） | 只绑 127.0.0.1，不进公网 |

本机进程：`pythonw -m uvicorn app.platform:app`（看门狗 `scripts/win/app_watchdog.bat` 每小时自愈）。

## 4. 列表源体系（v2.1.0 抗停维设计）

公众号"免费全量列表"按**四级降级链**取数，任一环失效链路照常工作：

```
WeRSS(容器,成熟实现,含free_publish降级)
  → 自研 WempClient(wemp_client.py,协议自持,凭据在 system_config[wemp_cred_uid])
  → 读书平台(第三方托管,可配)
  → 微信读书 cover(终极兜底,只保最新一篇)
```

- **运行时切换**：`MultiSourceClient` 依次尝试，异常切下一源 + 进程内熔断 10 分钟；
- **可观测**：`GET /api/admin/health` → `list_sources` 字段；
- **不赌单一开源项目**：WeRSS 同类有停维前科（wewe-rss 归档 / wechat-article-exporter 停维）；
  自研实现 + 凭据自持 + 镜像固化（`D:\werss\we-mp-rss-image-*.tar`）三层防御。

## 4b. 热榜源体系（v2.2.0「命门自持」）

多平台热榜按**契约 + 双路线**取数（`app/services/hot_sources.py`）：

```
契约层（自持）：HotSource.fetch() → [{title, url, extra}] —— 换实现不动调用方
实现层（双路线）：
  ├─ 自研直连（零第三方）：B站排行（官方 API）/ 豆瓣电影（公开 JSON）   ← 实测 200
  └─ newsnow 长尾（自部署容器 127.0.0.1:4444）：知乎/微博/快手/爱奇艺/掘金/虎扑/懂球帝...
     └ 容器数据源在自己机器上；newsnow 停维不影响运行，仅平台清单不再更新→届时自研补齐核心
资产层：全部条目写入我们自己的库（与第三方无关）
保险层：newsnow 镜像快照 D:
ewsnow
ewsnow-image-20261001.tar（51MB，docker load 可恢复）
```

**实测（2026-10-01）**：12 源 11 通（B站/豆瓣直连 ✓ + newsnow 9 源 ✓；知乎 401 故走 newsnow）。

**已接入现有体系（2026-10-01）**：
- **库表** `hot_source_items`（每轮全量快照，retention 治理覆盖）；
- **调度** `hot_source`：每小时 05 分采集（`hot_source_tick_all_users`，290 条/轮）；
- **全进 Agent 选题**：`_platform_hot_candidates`——近 24h 各平台 top10 并入热点池
  （跨平台同现=全网级信号，排序按平台数+名次；与 douhot 同词去重保留涨幅版）；
- **总群速览卡** `hot_rank_card`：每日 09:30/21:30 推「🔥 多平台热榜速览」到总群
  （与命中新平台热点时的 Agent 选题卡互补：本卡是雷达，选题卡是弹药）。
关联调研：TrendRadar（62k★，GPL-仅学设计）/ newsnow（22k★，MIT）。

## 4c. 资源库（v2.4.0）

对标号**全历史盘链**的可检索化（`app/services/resource_library.py`，底料是已有
`wechat_pan_links` 表）——解决"选题只匹配近 72h、大量历史资源被浪费"：

- `search_resources`：关键词（热点词/品类）检索，聚合到盘链级、按**验证强度**（多少号同发）排序；
- `resonance_resources`：高共振榜（同链被 ≥N 号同发 = 需求被反复验证的金矿）；
- `resource_profile` / `library_summary`：单资源画像 / 库概览；
- **Agent 集成**：`_supply_articles` 把高共振资源的文章排进 LLM 候选前列（不改标题，字面匹配逻辑不受影响）；
- CLI：`python scripts/search_resources.py 花少`（含 `--resonance` / `--profile`）。实测：
  库 542 链/10 条多号验证；检索"花少"→5 条资源且**我方链全部已转存可直接复用**。

全部数据来自已采集的对标信息——**不碰第三方资源聚合**（规避版权风险面）。

## 5. 调度作业矩阵（内嵌 APScheduler，24 线程）

| 作业 | 频率 | 职责 |
|---|---|---|
| collect_tick | 每分钟 | 四板块按用户间隔采集（微博/百度/抖音/闲鱼） |
| wechat_collect_tick | 4/8/14/20 点 | 公众号监听(**自适应分批**:批=ceil(池/4)夹[8,75];沉睡号(miss≥7)每3轮1次;扩建无需手调) |
| weread_refresh_tick | 每 6 小时 :50 | 微信读书 Cookie 主动续期（rt 单次编码自洽） |
| douhot_window_tick | 20 分钟 | 抖音多窗口对比 |
| hotspot_agent_tick | 9/15/21 点 | 热点选题建议（LLM 教学式输出） |
| settle_suggestions | 22:00 | 建议结算（盘链扩散增量）+ acted 自动归因 |
| event_assign | 15 分钟 | 跨平台事件归并 |
| run_feishu_* | 定点 | 日报/洞察/实时推送 |
| pan_cookie_keepalive | 7:00 | 网盘 Cookie 保活 |
| check_collect_failures / health_stalls | 30 分钟 | 失败聚合告警（含 ✅ 恢复确认）/ 停摆告警 |
| cleanup_old_data | 4:00 | 数据保留治理 |

## 6. 关键工程约束（踩坑沉淀）

1. **采集器会话资产**：微信读书/闲鱼/WeRSS 均"会话敏感"——禁并发同会话（互斥锁）、禁运行中直写其 SQLite（WAL）、换 Cookie 用 Network 请求头整串。
2. **wechat/ 子模块不可直接 import**：经 `app.services.wechat_monitor` 门面（子模块头部依赖门面）。
3. **系统代理免疫**：`disable_env_proxies()` 三入口生效，代理软件崩溃不再毒杀采集。
4. **告警必须有下文**：失败告警带评估时间戳，恢复必发 ✅ 确认（新旧消息可辨）。
5. **手术脚本纪律**：AST 切割/基线对比验证，正则边界含 `class/Assign`，防吞常量。
