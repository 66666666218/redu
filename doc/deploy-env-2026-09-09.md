# 部署配置清单(2026-09-09)

> 🔴 **安全警告**:本文件曾泄露真实密钥(commit 6cae980),所有相关凭证已要求轮换。
> 当前文件中的值均为占位符,请勿在此文件写入真实密钥。

> 服务器拉新镜像后,将以下配置追加到服务器 `.env` 并 `docker compose up -d`。
> 所有键都有代码默认值,只加核心项即可运行;可选项按需补充。

## 必须配置(缺失则对应功能停用)

```bash
# 微信读书 Cookie(免费监听源;过期收飞书🟠提醒后重新复制)
WEREAD_COOKIE=wr_vid=439862397; wr_skey=<最新值>; ...

# 夸克 Cookie(自动转存换链;缺失则推送回落原链接)
QUARK_COOKIE=b-user-id=<值>; __puus=<已泄露,重新登录复制> ...

# dajiala key(阅读量采样+兜底监听;不充值则采样自动跳过)
DAJIALA_KEY=JZL<已泄露,请联系服务商轮换>
```

## 推荐配置

```bash
DAJIALA_MIN_BALANCE=1.0
WECHAT_SYNC_MAX_PAGES=3
PAN_TRANSFER_ENABLED=true
QUARK_SAVE_DIR=/redian监听
QUARK_FID_STORE=data/quark_fid_cache.json
AGENT_ENABLED=true
FOCUS_ALERT_ENABLED=true
```

## 可选配置(代码已有默认值,无需手动设)

| 键 | 默认值 | 说明 |
|---|---|---|
| WECHAT_LISTEN_SAMPLE_NEW | true | 新文即时采样 |
| WECHAT_LISTEN_SAMPLE_LIMIT | 10 | 即时采样上限/轮 |
| WECHAT_TRAFFIC_SAMPLE_LIMIT | 30 | 每日采样上限 |
| WECHAT_TRAFFIC_MIN_INTERVAL_HOURS | 24 | 采样最小间隔(h) |
| WECHAT_RESONANCE_HOURS | 48 | 资源共振窗口 |
| WECHAT_BURST_MIN_READS | 100 | 爆点最低阅读(站内口径) |
| WECHAT_RESPAMPLE_GROWTH_PCT | 100 | 重采样增长阈值(%) |
| QUARK_SAVE_DIR | /redian监听 | 转存目录 |
| QUARK_SHARE_PASSWORD | (空) | 分享提取码 |
| AGENT_SCORE_THRESHOLD | 55 | 苗头分数线 |
| AGENT_COOLDOWN_HOURS | 12 | 苗头冷却(h) |
| FOCUS_REPEAT_ROUNDS | 3 | 反复轮数阈值 |
| FOCUS_COOLDOWN_HOURS | 24 | 重点冷却(h) |
| FOCUS_MAX_ITEMS | 10 | 重点推送上限 |
| QUIET_HOURS_START | 23 | 免打扰开始 |
| QUIET_HOURS_END | 8 | 免打扰结束 |

## 部署步骤

1. 确认 GitHub Actions 构建完成(绿色 ✓)
2. 服务器 `.env` 追加上方"必须配置"块
3. `docker compose pull && docker compose up -d`
4. 查看容器日志确认"后台调度器已启动"
5. 访问 `/healthz` 返回 ok
6. 运行 `python scripts/check_production.py` 一键验证(可选)

## 首次启动后的操作

1. 平台「公众号监听」页 → 从微信读书书架导入(书架需已关注对标号)
2. 点「立即监听一轮」验证全链路
3. 明早 08:00 四群日报即为第一波真实推送
