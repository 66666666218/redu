# 热点监控系统
# 基于 python:3.11-slim
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright(供抖音内容词浏览器采集)与 chromium + 系统依赖
RUN playwright install --with-deps chromium

# 拷贝应用代码
COPY config ./config
COPY app ./app

# 运行数据目录(归档/快照;Cookie 等请经卷挂载注入,勿打进镜像)
RUN mkdir -p /app/data

# 默认:调度模式(微博/闲鱼/抖音 4 个作业)
# 覆盖为 --api 可单独起监控接口/看板;建议以独立容器跑 API 以避免 SQLite 多进程竞争。
CMD ["python", "-m", "app.main"]
