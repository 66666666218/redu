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
    xianyu_detail_limit: int = 20   # 慢速抓详情(想要数)的商品数
    douhot_cookie_file: str = "data/douhot_cookie.txt"  # 抖音热点宝 Cookie 文件(gitignored)
    douhot_top_n: int = 50          # 内容词趋势条数
    douhot_alert_max: int = 5       # 单次判涨告警上限(防刷屏)
    douhot_alert_cooldown_hours: int = 24  # 同一内容词告警冷却(小时)
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
    # 频次已按"避免风控"调低;各接口间加入随机延迟(见 request_delay_seconds)。
    job_cron: str = "0 * * * *"  # 微博采集(每小时,1 次请求,防风控)
    xianyu_cron: str = "0 */3 * * *"  # 闲鱼热榜采集(每 3 小时,~13 次 mtop)
    daily_summary_cron: str = "0 20 * * *"  # 每日"今日热榜"总结(Cron,默认 20:00)
    douhot_cron: str = "0 */3 * * *"  # 抖音热点·内容词趋势采集(每 3 小时,浏览器较重)
    request_delay_seconds: float = 2.5  # 每次外部请求间的随机基础间隔(秒)

    # ---- 邮件通知 ----
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_pass: str = ""             # SMTP 授权码
    smtp_from: str = "热点监控"      # 邮件发件人显示名
    notify_to: str = ""             # 收件人,英文逗号分隔
    is_dev: bool = True             # 开发模式:不真正外发邮件

    # ---- 服务 ----
    app_port: int = 8080
    data_dir: str = "data"          # 归档与快照根目录

    # ---- 多租户平台 ----
    database_url: str = "mysql+pymysql://redu:redu@db:3306/redu?charset=utf8mb4"  # MySQL
    jwt_secret: str = ""            # 生产必须设置强随机密钥
    jwt_expire_minutes: int = 604800  # 登录有效期(默认 7 天)
    cookie_encrypt_key: str = ""    # Cookie 加密密钥(Fernet);为空则用 jwt_secret 派生
    public_base_url: str = "http://localhost:8080"  # 站点对外地址(重置链接等)

    @property
    def notify_to_list(self) -> list[str]:
        """将逗号分隔的收件人字符串转为列表,并去掉空项。"""
        return [addr.strip() for addr in self.notify_to.split(",") if addr.strip()]


@lru_cache
def get_settings() -> Settings:
    """返回单例 Settings 实例。"""
    return Settings()
