@echo off
rem One-click setup for WeRSS (optional list source, Docker Desktop required).
rem See doc/architecture.md section 4 and doc/operations.md section 4g.
rem Idempotent: exits 0 if the container is already running.
rem Keep this file ASCII-only (cmd codepage safety).

where docker >nul 2>&1
if not %errorlevel%==0 (
  echo [ERROR] docker not found. Install/start Docker Desktop first.
  exit /b 1
)
docker ps >nul 2>&1
if not %errorlevel%==0 (
  echo [ERROR] docker daemon not running. Start Docker Desktop and retry.
  exit /b 1
)

docker ps --filter name=we-mp-rss --format "{{.Names}}" | findstr /x we-mp-rss >nul
if %errorlevel%==0 (
  echo we-mp-rss is already running: http://127.0.0.1:8001
  exit /b 0
)

docker image inspect docker.1ms.run/rachelos/we-mp-rss:latest >nul 2>&1
if not %errorlevel%==0 (
  echo Pulling image via mirror (falls back to Docker Hub)...
  docker pull docker.1ms.run/rachelos/we-mp-rss:latest
  if not %errorlevel%==0 docker pull rachelos/we-mp-rss:latest
)

if not exist D:\werss\data mkdir D:\werss\data

docker run -d --name we-mp-rss -p 127.0.0.1:8001:8001 -v D:\werss\data:/app/data docker.1ms.run/rachelos/we-mp-rss:latest
if not %errorlevel%==0 (
  echo [ERROR] container start failed. If the name exists but stopped: docker start we-mp-rss
  exit /b 1
)
echo Done. Open http://127.0.0.1:8001 (auth + AK/SK: see doc/operations.md 4g)
exit /b 0
