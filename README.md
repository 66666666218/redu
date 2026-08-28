# 热点追踪与自动化监控系统

自动采集微博热搜,跨生态(抖音/百度)交叉验证热度,识别"上涨趋势"并通过邮件预警。
7×24 无人值守,具备代理/重试/降级与归档能力。

> 开发文档见 [doc/dev.md](doc/dev.md),接口规范见 [doc/API.md](doc/API.md)。

## 架构

```
[定时触发] → [微博热搜采集] → [清洗过滤] → [指数获取(抖音/百度)] → [趋势分析] → [邮件告警] → [归档]
```

## 快速开始

```bash
# 1. 依赖
pip install -r requirements.txt

# 2. 配置(默认 MOCK_INDEX=true,无需真实抓取即可运行)
cp .env.example .env

# 3. 手动跑一次管道
python -m app.services.pipeline   # 或直接调用 run_pipeline()

# 4. 定时调度
python -m app.main

# 5. 监控接口
python -m app.main --api
# 访问 http://localhost:8080/healthz
```

## 配置

全部配置经 `.env` 注入,见 [doc/dev.md §4](doc/dev.md#4-配置中心)。
关键项:`WEIBO_COOKIE`、`USE_PROXY/PROXY_*`、`SMTP_*`、`GROWTH_THRESHOLD`、`SLOPE_THRESHOLD`、`JOB_CRON`。

## 测试

```bash
pytest -q
ruff check app
mypy app
```

## Docker

```bash
docker compose up -d
```

> 所有外部请求必须走隧道代理,防止服务器 IP 被连坐封禁(见 doc/dev.md §8、§11)。
