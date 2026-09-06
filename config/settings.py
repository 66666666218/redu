"""配置中心:从 `.env` 读取全部运行配置。

使用 pydantic-settings 强类型解析,所有敏感项仅经环境变量注入,禁止硬编码。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """系统运行配置(字段与 doc/dev.md §4 配置中心对应)。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 采集 ----
    weibo_cookie: str = ""          # 微博登录态
    baidu_cookie: str = ""          # 百度指数登录态(降级源)
    douyin_cookie: str = ""         # 抖音创作者中心/巨量算数登录态
    goofish_cookie_file: str = "data/goofish_cookie.txt"  # 闲鱼登录 Cookie 文件(gitignored)
    xianyu_keywords: str = "ps教程,网盘资源,代充,剪映会员,软件,素材,cad,ae,pr,office,会员,课程,影视,源码"  # 虚拟商品关键词
    xianyu_top_n: int = 100         # 前 N 虚拟商品榜(搜索级,无风控)
    xianyu_detail_limit: int = 10   # 慢速抓详情(想要数)的商品数;详情是触发 mtop 风控的最大爆发点,默认降到 10
    xianyu_deep_interval_hours: int = 6  # 闲鱼深采自动跑的最小间隔(小时):搜索接力深采时,距上次成功深采≥该值才跑,防风控
    xianyu_request_delay: float = 8.0  # 闲鱼相邻请求间隔(秒,带抖动);比通用更大,防 mtop 风控
    xianyu_batch_keywords: int = 5     # 每次采集最多处理的关键词数(风控降频:少量多次,按运行数轮转覆盖全部)
    xianyu_cooldown_minutes: int = 30  # 闲鱼触发人机验证(滑块)后,暂停采集该分钟数,避免反复撞枪口
    xianyu_proxy_url: str = ""      # 闲鱼专用"单一固定"出口代理(http://user:pass@host:port,如住宅IP);留空直连。勿用轮换代理池——mtop token/session 绑定出口 IP
    douhot_cookie_file: str = "data/douhot_cookie.txt"  # 抖音热点宝 Cookie 文件(gitignored)
    douhot_top_n: int = 100         # 内容词趋势条数(抖音热点接口可到 200)
    douhot_watch_entry_cap: int = 100  # 榜单搜索类关注(话题/搜索/视频)每次采集最多记录的相关主题条数
    douhot_watch_daily_top: int = 100  # 每日日报里榜单搜索类关键词最多列出的相关主题条数
    douhot_alert_max: int = 5       # 单次判涨告警上限(防刷屏)
    douhot_alert_cooldown_hours: int = 24  # 同一内容词告警冷却(小时)
    alert_cooldown_hours: int = 6   # 预警规则冷却(小时),避免重复刷
    proxy_url: str = ""             # 隧道代理地址
    proxy_user: str = ""            # 隧道代理账号
    proxy_pass: str = ""            # 隧道代理密码
    use_proxy: bool = False         # 是否启用代理(本地调试可关闭)
    proxy_extract_url: str = ""     # 提取式代理 API(巨量IP getips URL,含 trade_no/sign)
    proxy_refresh_seconds: int = 170  # 提取池刷新间隔(每个 IP 约 3 分钟)

    # ---- 分析阈值 ----
    index_sources: str = "weibo"  # 指数源优先级链(逗号分隔):weibo/douyin/baidu
    mock_index: bool = True  # 本地/测试用合成指数源(免真实抓取)
    alert_mode: str = "both"  # 交叉验证: both=所有信号源同涨才告警; any=任一源涨即告警
    growth_threshold: float = 0.30  # 环比增长率判定阈值
    slope_threshold: float = 0.0    # 线性回归斜率判定阈值
    min_heat: int = 200_000         # 候选词清洗下限热度
    top_n: int = 10                 # 进入指数分析的热搜词数量
    min_samples: int = 3            # 线性回归所需最少指数样本点

    # ---- 调度 ----
    # 采集频率由**每个用户自行设置**(user_schedules 表,10~1440 分钟),调度器每分钟检查到期任务。
    # 原先的 JOB_CRON / XIANYU_CRON / DOUHOT_CRON / DAILY_SUMMARY_CRON 已无代码引用,故移除。
    scheduler_enabled: bool = True  # 随 API 进程启动后台调度器(多 worker 部署时须关掉,另起调度容器)
    request_delay_seconds: float = 2.5  # 每次外部请求间的随机基础间隔(秒)
    # 采集持续失败告警:某用户某板块近 24h 失败 >= 该次数,推飞书告警(防 Cookie 过期无人知)
    fail_alert_threshold: int = 3
    # 采集停摆告警:某平台已启用(配了 Cookie)但超过该小时数无新数据写入,推飞书(防后端宕机/调度停/被风控全挡却未记为失败)
    health_stall_hours: int = 24
    # 停摆/失败升级:同一平台连续超过该天数无数据/未成功 → 标注"长期,建议人工排查",区分偶发与长期坏
    health_escalate_days: int = 3
    # 数据保留天数:超过该天数的快照/运行/告警/日志会被清理 job 删除(控制库体积)
    data_retention_days: int = 30

    # ---- 邮件通知 ----
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_pass: str = ""             # SMTP 授权码
    smtp_from: str = "热点监控"      # 邮件发件人显示名
    notify_to: str = ""             # 收件人,英文逗号分隔
    is_dev: bool = True             # 开发模式:不真正外发邮件

    # ---- 飞书机器人 ----
    # 未配置 webhook 时,飞书日报与实时提醒自动关闭(不影响其他功能)。
    feishu_webhook: str = ""        # 群机器人 Webhook 地址(总群;板块推送未配专属群时回落这里)
    feishu_webhook_weibo: str = ""  # 微博专属群 Webhook(非空则微博监控推到这里,否则推总群)
    feishu_webhook_xianyu: str = "" # 闲鱼专属群 Webhook
    feishu_webhook_douhot: str = "" # 抖音专属群 Webhook
    feishu_webhook_baidu: str = ""  # 百度专属群 Webhook
    feishu_webhook_wechat: str = "" # 公众号专属群 Webhook
    feishu_secret: str = ""         # 机器人签名校验密钥(为空则不签名)
    feishu_daily_cron: str = "0 8 * * *"   # 每日热点日报时间(默认 08:00)
    feishu_wechat_cron: str = "0 10 * * *"  # 公众号内容选题分析推送时间(默认 10:00)
    feishu_hot_rank_jump: int = 3          # 排名跳升 ≥ 该名次即实时推送
    feishu_hot_ratio: float = 0.30         # 分值环比涨幅 ≥ 该比例即实时推送
    feishu_burst_min_confidence: str = "高"  # 实时推送"预测爆发"所需最低置信度(高/中/低);中低置信只进日报与洞察、不实时推,减少噪音
    feishu_alert_cooldown_hours: int = 6   # 同一话题实时推送冷却(小时),防刷屏
    feishu_insight_cron: str = "0 9 * * 1"  # 每周一 09:00 推"近7天爆点回顾"(day_of_week 用标准 cron,0=周日)
    weekly_summary_cron: str = "0 20 * * 0"  # 每周日 20:00 给每个用户发"本周热点洞察"邮件(day_of_week 0=周日)
    # 抖音热点宝走代理池:部分服务器 IP 会被抖音风控(直接返回 502 nginx),
    # 开这个后 douhot 采集走 PROXY_EXTRACT_URL 提取的住宅代理。生产服务器建议开启。
    douhot_use_proxy: bool = False

    # ---- 服务 ----
    app_port: int = 8080
    data_dir: str = "data"          # 归档与快照根目录

    # ---- 多租户平台 ----
    # 主机名必须与 docker-compose.yml 的服务名一致(mysql)。曾误写为 `@db:3306`,
    # compose 里因显式注入 DATABASE_URL 而没暴露;一旦漏传该变量就会连向不存在的
    # 主机 `db`,表现为所有接口 OperationalError(注册/登录全 500)且极难定位。
    database_url: str = "mysql+pymysql://redu:redu@mysql:3306/redu?charset=utf8mb4"
    jwt_secret: str = ""            # 生产必须设置强随机密钥
    jwt_expire_minutes: int = 604800  # 登录有效期(默认 7 天)
    cookie_encrypt_key: str = ""    # Cookie 加密密钥(Fernet);为空则用 jwt_secret 派生
    admin_email: str = ""           # 注册时若邮箱匹配(逗号分隔)则自动设为 admin
    public_base_url: str = "http://localhost:8080"  # 站点对外地址(重置链接等)

    @property
    def notify_to_list(self) -> list[str]:
        """将逗号分隔的收件人字符串转为列表,并去掉空项。"""
        return [addr.strip() for addr in self.notify_to.split(",") if addr.strip()]


@lru_cache
def get_settings() -> Settings:
    """返回单例 Settings 实例。"""
    return Settings()
