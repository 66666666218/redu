# 热点监控平台(多租户 · 前后端分离)
# 阶段1:构建 Vue3 前端;阶段2:Python 运行时(纯 requests 采集,无需浏览器)

FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npx vite build --outDir dist

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config ./config
COPY app ./app
# 前端构建产物 → 后端托管目录
COPY --from=frontend /build/dist ./app/static/spa

RUN mkdir -p /app/data

# 默认:多租户平台 API(鉴权 + 采集 + 看板)
CMD ["python", "-m", "uvicorn", "app.platform:app", "--host", "0.0.0.0", "--port", "8080"]
