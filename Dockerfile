# 热点监控系统
# 基于 python:3.11-slim

FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 如需巨量算数(Playwright),取消注释并下载 chromium
# RUN playwright install --with-deps chromium

# 拷贝代码
COPY config ./config
COPY app ./app

# 运行数据目录
RUN mkdir -p data

# 默认调度模式;--api 可切换到监控接口
CMD ["python", "-m", "app.main"]
