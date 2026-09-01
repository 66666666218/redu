# 热点监控平台(redian)

多租户热点监控 + 管理后台。自动采集微博热点、闲鱼虚拟商品、抖音热点宝内容/搜索/视频/话题/订阅榜,
跨轮判涨、用户自定义预警(阈值/新增/定时+关键词过滤+冷却+邮件),后台可管理用户/角色/权限/日志/报表。

> 开发文档:`doc/dev.md` · 接口规范:`doc/API.md` · 变更日志:`CHANGELOG`

## 技术栈
- 后端:FastAPI · SQLAlchemy · MySQL · JWT · requests · APScheduler · SMTP
- 前端:Vue3 + Vite(SPA,后端托管)
- 部署:Docker Compose(mysql + api)· GitHub Actions → 阿里云 ACR 自动构建

## 功能
- **多租户**:注册/登录(限速防爆破)、每用户自管各平台 Cookie(加密存储)、数据按 `user_id` 隔离、每用户告警邮箱(SMTP)
- **监控**:微博热点(判涨)、闲鱼前100 + Top20详情分析、抖音热点宝 5 类榜单关键词监控
- **预警**:每模块可配 `threshold`(指标超阈值)/`new`(新增)/`fixed_time`(定时总结)+ 关键词过滤 + 冷却 + 邮件/站内
- **管理后台**:工作台(指标+30天图+类目分布+待办)、用户管理(搜索/详情/启停/删除/导入/导出)、采集数据明细、登录/操作日志、系统设置、**按钮级 RBAC 权限**

## 快速开始(开发)
```bash
cp .env.example .env        # 填 JWT_SECRET、DATABASE_URL、SMTP、各 Cookie 等
pip install -r requirements.txt
python -m pytest -q
python -m app.main          # 调度模式(采集+预警+定时)
python -m app.main --api    # API/看板 模式
```

## 配置(.env)
| 键 | 说明 |
| --- | --- |
| `JWT_SECRET` | 登录密钥(必填,≥32字节强随机) |
| `DATABASE_URL` | MySQL(如 `mysql+pymysql://redu:redu@mysql:3306/redu?charset=utf8mb4`) |
| `ADMIN_EMAIL` | 该邮箱注册即自动成为管理员(逗号分隔) |
| `PUBLIC_BASE_URL` | 站点对外地址(重置链接用) |
| `SMTP_HOST/PORT/USER/PASS/FROM` | 全局发信(用户也可自配邮箱) |
| `XIANYU_TOP_N/DETAIL_LIMIT` | 前 N 榜 / 慢速TopN详情 |
| `SCHEDULER_ENABLED` | 随 API 进程启动后台调度器(默认 true);多 worker 部署须设 false 并单起调度容器 |
| 采集频率 | 无全局 Cron:每个用户在**平台内**「采集频率」页自行设置(10~1440 分钟,三个板块分别设) |
| 各平台 Cookie | 用户在**平台内**自行配置,不写在 .env |

## Docker 一键部署
```bash
cp .env.example .env        # 设 JWT_SECRET/DATABASE_URL/ADMIN_EMAIL/SMTP
docker compose up -d        # 启动 mysql + redu-api
# 访问 http://IP:8080/
```
容器编排见 `docker-compose.yml`(mysql + api,数据挂载于卷)。

### 管理后台
1. `.env` 设 `ADMIN_EMAIL=<你的邮箱>`。
2. 用该邮箱在平台注册 → 自动管理员。
3. 注册/登录后顶部出现「管理后台」;普通用户不可见、无权限。
4. **角色权限**:`admin`(全部)/`operator`(查/启停/导出,不可删/导入/改配置)可改 `app/admin.py` 的 `PERMS`。

## GitHub Actions 自动构建 + 推 ACR
`.github/workflows/docker-push.yml`:`push main` → 构建镜像 → 推 ACR `redu:latest`。
在仓库 `Settings → Secrets → Actions` 配置:
- `ACR_USERNAME`、`ACR_PASSWORD`(阿里云 ACR 访问凭证)

## 备份
```bash
sh scripts/backup.sh        # MySQL 导出到 ./backup/
```

## 目录结构
```
config/settings.py   配置中心
app/                 后端(FastAPI:platform=主API, auth/db/admin/services)
frontend/            Vue3 + Vite 源码(构建后由后端托管)
scripts/             backup.sh 等
tests/               单测(42 项)
doc/                 dev.md · API.md
```
