# 服务器部署核对清单

> 涵盖近期重构 + 运维加固改动。**必须重建镜像**再 `docker compose up -d`(本次有大量后端/前端改动,不是重启能生效的)。

## 1. 重建并启动

```bash
cd <compose 目录>
# 确认用的是最新 docker-compose.yml(含 mysql 服务,无 redu-monitor)
docker rm -f redu-api redu-monitor   # 旧容器(数据在 MySQL/卷里,不丢)
docker compose up -d --remove-orphans
curl -s http://127.0.0.1:8080/healthz   # 应看到 db.connected=true
```

## 2. 服务器 `.env` 必查项(逐个确认,缺了对应功能失效)

```bash
python scripts/check_env.py   # 一键校验(列缺失/空/占位符,不打印凭据)
```

| 配置 | 期望值 | 缺失后果 |
|---|---|---|
| `JWT_SECRET` | ≥32 字节强随机 | 登录态不安全/重启失效 |
| `DATABASE_URL` | compose 已注入,无需改 | 连不上库 |
| `DOUHOT_USE_PROXY` | `true` | 抖音采集被服务器 IP 风控 → **502** |
| `PROXY_EXTRACT_URL` | 熊猫代理 glip URL(含 secret/orderNo) | 同上 |
| `DOUHOT_TOP_N` | `100` | 只采 50 条(覆盖不到 100) |
| `FEISHU_WEBHOOK` | **新机器人** `2a294b76-74ab-441e-a31e-39eda459c72a` | 告警发到旧机器人 |
| `FEISHU_SECRET` | 空(签名已关) | — |
| `XIANYU_REQUEST_DELAY` | `4.0`(建议) | 闲鱼限流默认 2.5 偏快 |
| `XIANYU_DETAIL_LIMIT` | `10`(建议) | 详情抓取越多越易风控 |
| `FAIL_ALERT_THRESHOLD` | `3`(默认) | 采集失败无主动告警 |
| `DATA_RETENTION_DAYS` | `30`(默认) | 库只增不删 |

## 3. 每个用户各自配 Cookie(多租户,走平台,不写 .env)

登录平台 → **Cookie 管理** → 各平台卡片粘贴对应 Cookie → 保存。

- **闲鱼 Cookie** 必须含 `_m_h5_tk`/`_m_h5_tk_enc`/`cookie2`(缺了 mtop 签名会失败)。
- 微博/抖音也各自配。

## 4. 运维要点(尤其闲鱼)

- **闲鱼保持直连,不要挂代理**——数据中心代理 IP ≠ 账号常用 IP,突然换 IP 反而加重风控。只有抖音需要代理防 502。
- **闲鱼采集频率放慢**(给闲鱼单独设 60 分钟+,别 10/30 分钟)。
- **闲鱼深度采集少跑**(想要数那步,`XIANYU_DETAIL_LIMIT=10`),它是触发风控最大爆发点。
- 账号若已被验证码/滑块卡住:`FAIL_SYS_USER_VALIDATE` 持续 → 去闲鱼网页刷新会话,停几小时再跑,别在冷却期反复触发。

## 5. 部署后自检

```bash
docker compose ps                          # 两个容器 healthy
curl -s http://127.0.0.1:8080/healthz      # db.connected=true, 缺表为空
docker exec redu-api python -c "from app.platform import create_app; print('app ok')"
# 点一次"采集抖音"(应有代理,不再 502)、"采集微博"、"采集闲鱼"(确认不再连续限流)
```

## 本次改动摘要(为什么值得重新部署)

- 框架重构(Controller/Service/Repository 三分离、platform 拆 API、tenant 拆模块)
- 修星期几 cron bug(周报一直晚一天)
- 采集失败主动告警 + 数据保留治理
- **闲鱼限流缓解**(指数退避 + 独立限速 + 压低详情)
- 抖音走代理防 502
