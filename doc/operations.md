# 运维手册(FAQ)——上线第一周常见问题

> 2026-09-09 基于代码路径整理。按「症状 → 原因 → 处理」组织。

## 1. 公众号监听没有推送?

**排查顺序**:
1. 「公众号监听」页顶部状态条:在监号数是 0?
   → 还没有对标号:微信读书关注 → 书架导入,或贴文章链添加
2. 对标号有了但没推送:
   - 看「采集频率」页公众号监听是否启用(默认 60 分钟)
   - 手动点「立即监听一轮」,看返回的 `new` 值
3. `status=skipped reason=no_source` → .env 缺 WEREAD_COOKIE 且缺 DAJIALA_KEY
4. `reason=low_balance` → dajiala 余额不足,充值或忽略(免费源不受影响)

## 2. 微信读书 Cookie 过期(收到 🟠 提醒)

- 浏览器登录 weread.qq.com → F12 → Network → 刷新 → 复制任意请求的 Cookie 整行
- **确认含 `wr_skey=` 和 `wr_vid=`**(只复制 weread.qq.com 的,不是 mp.weixin.qq.com 的)
- 粘贴到「Cookie 管理」页 weread 平台 → 保存(不用重启)
- 系统有自动续期(wr_rt 换新 skey),正常情况几周才需要手动更新一次

## 3. 闲鱼触发人机验证(收到 🔴 提醒)

- 这是账号/IP 级风控,**退避无效**,处理顺序:
  1. 手机闲鱼 App 完成一次滑块验证
  2. 换出口 IP(住宅 IP 最有效):`.env` 配 `XIANYU_PROXY_URL=http://user:pass@ip:port`
  3. 系统已自动冷却 30 分钟,期间轮次跳过
- **不要**调高采集频率(每轮 5 词/8s 间隔是社区验证的安全区)

## 4. 夸克转存失败(推送回落原链接)

- 收到「夸克 Cookie 已失效」告警 → pan.quark.cn 重新登录复制 Cookie → 更新 .env 的 QUARK_COOKIE
- 「容量不足」→ 清理网盘空间
- 转存失败不影响监听和推送,只是推送里没有你的链接

## 5. 阅读量显示 "—"

- 还没采样:点「刷新阅读量」(¥0.06/篇)或等每日 09:30/21:30 自动采样
- dajiala 余额不足会自动跳过采样(充值后恢复)
- 微信读书源的文章自带站内阅读值(免费),推送里直接显示

## 6. 推送重复(总群和专属群都收到)

- 设计如此:公众号监听双推(专属群+总群);不想收总群可在飞书总群关闭该机器人

## 7. 数据备份

- **自动**(SQLite 本地部署):每日 04:00 快照到 `data/backups/platform_YYYYMMDD.db`,保留最近 7 份。
  走 SQLite 在线备份 API,库正被写入时也一致;快照产出后会校验非空 + `PRAGMA quick_check`,
  不合格的直接删掉——**宁可没有,也不留 0 字节的假备份**。
- **手动**:`sh scripts/backup.sh`(按 `DATABASE_URL` 自动选 SQLite / MySQL;SQLite 与自动快照同一实现)
- 检查备份是否健康:`ls -la data/backups/` —— **任何 0 字节文件都说明备份失败**,别当成"有备份"。
  备份失败会在服务日志里记 `SQLite 快照备份失败`(stdout,`docker logs` 可见)。
- ⚠️ 不要直接 `cp`/`gzip` 库文件当备份:写入过程中拷贝可能得到撕裂的中间状态,恢复时才发现坏。

## 8. 关键文件位置

| 文件 | 用途 |
| --- | --- |
| `.env` | 所有配置(Cookie/key/webhook) |
| `data/platform.db` | SQLite 数据库(本地部署) |
| `doc/dajiala-api.md` | dajiala 接口规格(含坑位说明) |
| `doc/dev.md` §5.8b/§5.14 | 公众号监听/苗头 Agent 架构 |

## 9. 微信读书 Cookie:为什么"过期快",怎么免维护

- **机制**:wr_skey 短效(约 12~24h)且**轮换制**——浏览器和服务端谁调续期,旧 skey 都会失效。
  长效兜底是 `wr_rt`(约 30 天)。
- **自动续期**:调度每 6 小时跑 `weread_refresh_tick`(50 */6 * * *),用 wr_rt 换新 wr_skey
  并回写平台内「weread」Cookie,**有效期内主动轮换 = 永不过期**。
- **失败即报**:续期失败(wr_rt 也死了)会即时推公众号飞书群(带冷却),不等监听断掉才发现。
- **⚠️ 换 Cookie 后的关键动作**:复制 Cookie 后**尽量别再在原浏览器使用微信读书**——
  浏览器会自己轮换 wr_skey,把服务端这份顶失效。这是"Cookie 过期好快"的最常见主因。
  另外 wr_rt 也可能因在别处重新扫码登录被顶掉。
- 手动续期:前端「微信读书」页有续期按钮(POST /api/wechat/weread/refresh)。

## 10. 闲鱼风控体系(现状)

- 传输层 curl_cffi 模拟 Chrome TLS 指纹;证书校验默认开启,本机 CA 损坏(curl:77)自动降级并告警一次。
- 每轮只抓 `XIANYU_BATCH_KEYWORDS`(默认 5)个关键词,按运行次数轮转窗口;请求间隔
  `XIANYU_REQUEST_DELAY`(默认 8s)带 ±20% 抖动。
- 限流(FAIL_SYS_RATE_LIMIT)指数退避 30/90/180s;滑块(FAIL_SYS_USER_VALIDATE)立即停止本轮。
- 滑块后自动冷却:`XIANYU_COOLDOWN_MINUTES`(默认 30)起,**24h 内每再触发一次翻倍**,封顶 240 分钟,
  期间调度轮次自动跳过(run 记录 `verify_cooldown`),并即时推闲鱼飞书群。
- 深采(详情)限 `XIANYU_DETAIL_LIMIT`(默认 10)个/轮、`XIANYU_DEEP_INTERVAL_HOURS`(默认 6h)一轮,
  当天已抓过的商品不重复请求;网关空响应(WAF 静默拦截)会报"疑似 WAF 风控拦截"并带响应片段。
- 治本手段是**固定住宅出口代理**(`XIANYU_PROXY_URL`);轮换代理池不可用(token 绑定出口 IP)。

## 11. Cookie 加密密钥事故(2026-09-13)

- 现象:重启后闲鱼/微博/抖音采集报"未配置 Cookie",但界面看明明配置过。
- 根因:`JWT_SECRET` 未配置的时期,进程用**随机临时密钥**加密 Cookie,重启即全部失读;
  且旧进程带旧密钥持续回写坏行,新进程读不了。`get_cookies` 此前静默跳过坏行,故障被掩盖。
- 修复:① `get_cookies` 对"行存在但解不开"自动用该平台全局配置/Cookie 文件以当前密钥回写(自愈);
  无兜底源的告警提示重贴。② `.env` 必须固定 `JWT_SECRET`(生产),否则每次重启丢所有登录态和 Cookie。
