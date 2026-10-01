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
    weread_cookie: str = ""         # 微信读书 Cookie(免费监听数据源;优先用平台内按用户配置的「weread」Cookie)
    wechat_reader_platform_url: str = ""  # 读书平台地址(wewe-rss v2 兼容,免费全量文章列表;如 https://weread.xxx 自建实例)
    wechat_reader_token: str = ""   # 读书平台 token(含 vid 的 JWT)
    wechat_reader_vid: str = ""     # 读书平台 vid(微信读书用户ID)
    wechat_werss_url: str = ""      # 自建 WeRSS(rachelos/we-mp-rss)地址,免费全量列表首选源;配了它就优先于上面的读书平台
    wechat_werss_ak: str = ""       # WeRSS Access Key(管理界面「Access Key 管理」创建)
    wechat_werss_sk: str = ""       # WeRSS Secret Key(创建时只显示一次)
    wechat_sync_max_pages: int = 3  # 一键同步默认最多翻页数(history_by_ghid ¥0.14/页,每页约10次发文)
    wechat_sync_push_limit: int = 20  # 一次「同步文章」转存+推飞书的篇数上限(资源文优先;同盘链去重后仍超量才截断)
    wechat_burst_min_reads: int = 100  # 爆点检测最低站内阅读(微信读书口径,免费数据)
    wechat_burst_median_mult: float = 3.0  # 爆点判定:新文阅读 ≥ 同号近14天中位数×该倍数
    wechat_dormant_retire_days: int = 7  # 死号清理:连续 N 天无发文自动停监控(2026-10-01 用户口径"一星期没发文就取消";0=关闭)
    wechat_listen_batch_size: int = 0  # 监听轮每批号数:0=自适应(2026-10-01,按池子规模自动分批+沉睡号降频,扩建无需手调);非 0=固定批(旧行为);负数=回全量
    quark_cookie: str = ""                 # 夸克网盘 Cookie(pan.quark.cn 登录后复制);用于转存对标文的分享
    quark_save_dir: str = "/redian监听"     # 转存目标目录(自动逐级创建)
    quark_fid_store: str = "data/quark_fid_cache.json"  # 目录 fid 持久缓存(大盘免重扫;幽灵同名复用)
    quark_share_password: str = ""         # 二次分享提取码(空=无)
    pan_transfer_enabled: bool = True      # 是否自动转存(需 quark_cookie;失败回落原链接推送)
    pan_transfer_backfill_limit: int = 8   # 每轮监听额外补转存的历史文章数
                                           # (同步当场转存失败/早于该逻辑入库的旧文,靠这个队列慢慢补)
    wechat_traffic_cron: str = "30 21 * * *"  # 每日阅读量采样时间(默认 21:30)
    wechat_resonance_hours: int = 48         # 资源共振窗口(同一盘链 N 小时内 ≥2 篇文章)
    wechat_repush_window_hours: int = 24     # 补推窗口:入库 N 小时内未送达飞书的文章还要补
    wechat_repush_limit: int = 100           # 单轮补推篇数上限(超出留给下一轮,不一次刷屏)
    wechat_listen_lock_ttl_minutes: int = 20 # 监听"在跑"标记的有效期(超过视为进程被杀,允许后来者接管)
    weread_shelf_gate: bool = True    # 监听轮书架粗筛:1 次书架先分"谁没更新",cover 只问有变化的号(判据可证安全,任何不确定自动停用)
    weread_shelf_gate_every: int = 4  # 书架说"没更新"的号每 N 轮仍强制问一次 cover(错判盲区上界=N 轮;4 轮/天 → 每号每天至少真问一次)
    deepseek_api_key: str = ""               # DeepSeek API key(LLM 叙事层,OpenAI 兼容)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    llm_narrate_limit: int = 3               # 每轮推送最多交给 LLM 解读的文章数
    focus_alert_enabled: bool = True   # 重点关键词告警(跨板块共振/板块内反复)开关
    focus_repeat_rounds: int = 3       # 板块内"反复出现"的轮数阈值(近24h同关键词出现≥N轮)
    focus_min_len: int = 4             # 跨板块匹配的最短归一化关键词长度(防误配)
    focus_cooldown_hours: int = 24     # 同一关键词的告警冷却(小时)
    focus_max_items: int = 10          # 每次推送的重点关键词上限
    quiet_hours_start: int = 23           # 免打扰开始(小时,23=晚11点);非紧急推送延到免打扰结束
    quiet_hours_end: int = 8              # 免打扰结束(小时,8=早8点);实时热点等紧急推送不受限
    agent_enabled: bool = True             # 早期苗头 Agent(全板块自主预测)开关
    agent_score_threshold: int = 55        # 苗头判定分数线(≥70 上升,≥85 爆发)
    agent_cooldown_hours: int = 12         # 同一(板块,关键词)的苗头冷却(小时)
    hotspot_agent_enabled: bool = True   # 热点→网盘选题 Agent(监控词热度 × 供应商新资源 → 发货建议,站内推送)
    hotspot_agent_cron: str = "10 9,15,21 * * *"  # Agent 运行时刻(跟在白天三个定点监听后面,数据最鲜)
    hotspot_min_growth: float = 50.0     # 热点词 24h 涨幅达标线(低于此值不生成建议)
    hotspot_agent_top_n: int = 15        # 单轮最多产出几条选题建议(2026-10-01 由 8 上调)
    hotspot_agent_llm_top: int = 10      # 没现成资源的热点里,最多几个交给 LLM 生成选题
                                         # (2026-10-01 由 3 上调:实测 430 条候选里只有 3 条能进
                                         #  LLM,热点利用率不足 4%;DeepSeek 单价低,提到 10)
    weread_fullsync_on_renewal: bool = False  # renewal 后的全量补采开关(默认关!)
                                     # 2026-09-28 实测:81 号×cover+列表 ≈162 次的补采炸弹会在
                                     # renewal 后的新会话上一次性打穿全部额度,会话数小时内即死,
                                     # 与书架门轻量监听抢同一份额度;需要补采历史时手动开+手动设
                                     # weread_fullsync_pending_<uid> 标记
    weread_refresh_cron: str = "50 3,7,13,19 * * *"  # 微信读书 Cookie 续期:对齐到 4 个监听定点(4/8/14/20 点)前 10 分钟
                                     # ——① skey 恒新,轮内 auth 兜底几乎不用出手;② renewal=换新会话,
                                     # mp/articles 列表只在会话初期可用,每轮都跑在窗口 freshly 重开的会话上,
                                     # 同日多篇枚举机会最大化(2026-09-28 定稿)
    candidate_search_terms: str = ""   # 候选发现搜索词(逗号分隔;空=仅用对标号标题画像词)
    keyword_search_terms: str = "夸克网盘资源,百度网盘资源,Switch模拟器,PS5游戏资源,剪映模板"  # 关键词文章监控搜索词(逗号分隔,每4h一轮)
    candidate_mine_terms: int = 6      # 标题画像词上限(从已入库标题挖高频内容词)
    candidate_max_terms: int = 8       # 单轮候选发现的搜索词总数上限(搜狗限频,宁少勿封)
    candidate_discover_cron: str = "20 8 * * *"  # 每日候选对标号发现时间(默认 08:20)
    # ---- 候选自动收录(2026-10-01):发现→筛选→补进 WeRSS 订阅池→监听自动接上 ----
    candidate_auto_import: bool = True     # 关掉则只在候选页手工点收录
    candidate_auto_import_max: int = 8     # 单轮最多收录几个号(上游加订阅会排一次历史抓取,
                                           # 是重操作;8/天≈11 天消化掉积压的 89 个候选)
    candidate_auto_import_min_accounts: int = 2  # 资源库验证阈值:该来源词对应的资源被 ≥N 个
                                                 # 对标号发过才算"需求已验证",随 LLM 资源号一并收录
    candidate_auto_import_cron: str = "30 8 * * *"  # 每日自动收录时间(默认 08:30,紧随候选发现之后)
    # ---- Telegram 频道资源源(2026-10-01):公众号之外的第二路盘链 feed ----
    # ⚠️ 需能出网的环境(本机直连 t.me 超时且无本地代理端口)。解析逻辑已单测覆盖,
    # 启用条件只是网络——所以默认关,免得每轮刷失败日志。
    tg_enabled: bool = False        # 总开关
    tg_channels: str = ""           # 频道名(逗号分隔,不带 @),如 "channel_a,channel_b"
    tg_proxy: str = ""              # 出口代理(http://host:port);留空直连
    tg_limit: int = 30              # 单频道每轮取最近几条
    tg_cron: str = "*/30 * * * *"   # 采集频率(默认每 30 分钟;频道更新密度远低于热点榜)
    # ---- 实例角色(2026-10-01):分体部署时避免两端重复跑同一批作业 ----
    # 本项目有两套部署:本机(公众号 + 闲鱼)与远程 VPS(热点四路),**各自的数据库是独立的**,
    # 但推的是同一个飞书群。不加约束的话两边会各跑一套完整调度器 —— 重复推飞书、重复打上游,
    # 而且本机的热点数据源早已停用(见 user_schedules),跑热点作业纯属拿 3 天前的旧数据空转。
    #   all     = 单实例/开发,全跑(默认)
    #   wechat  = 只跑公众号 + 闲鱼侧(本机)
    #   hotspot = 只跑热点侧(远程)
    scheduler_role: str = "all"
    douhot_cookie_file: str = "data/douhot_cookie.txt"  # 抖音热点宝 Cookie 文件(gitignored)
    douhot_top_n: int = 100         # 内容词趋势条数(抖音热点接口可到 200)
    douhot_watch_entry_cap: int = 100  # 榜单搜索类关注(话题/搜索/视频)每次采集最多记录的相关主题条数
    douhot_watch_daily_top: int = 100  # 每日日报里榜单搜索类关键词最多列出的相关主题条数
    douhot_alert_max: int = 5       # 单次判涨告警上限(防刷屏)
    douhot_alert_cooldown_hours: int = 24  # 同一内容词告警冷却(小时)
    douhot_window_windows: str = "1,24"  # 关键词多窗口对比的窗口集(小时,逗号分隔;默认近1h+近1天)
    douhot_window_cron: str = "*/20 * * * *"  # 多窗口对比采集频率(默认每20分钟,与榜单采集互补)
    alert_cooldown_hours: int = 6   # 预警规则冷却(小时),避免重复刷
    proxy_url: str = ""             # 隧道代理地址
    proxy_user: str = ""            # 隧道代理账号
    proxy_pass: str = ""            # 隧道代理密码
    use_proxy: bool = False         # 是否启用代理(本地调试可关闭)
    proxy_extract_url: str = ""     # 提取式代理 API(巨量IP getips URL,含 trade_no/sign)
    proxy_refresh_seconds: int = 170  # 提取池刷新间隔(每个 IP 约 3 分钟)

    # ---- 分析阈值 ----
    index_sources: str = "weibo"  # 指数源优先级链(逗号分隔):weibo/douyin/baidu
    # newsnow 容器地址(2026-10-01 可配):默认本机直跑;**容器化部署时必须改**——
    # 在 redu-api 容器里 127.0.0.1 指容器自己,不是宿主机,newsnow 就全连不上
    # (实测:远程只有 bilibili/douban 两个自研源有数据,newsnow 的 40 个全空)。
    # Docker 里填宿主机网关 `http://172.17.0.1:4444`(newsnow 需绑 0.0.0.0 而非 127.0.0.1)。
    hot_newsnow_url: str = "http://127.0.0.1:4444"
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
    feishu_webhook: str = ""        # 群机器人 Webhook 地址(总群=客户看的内容群;板块推送未配专属群时回落这里)
    feishu_webhook_admin: str = ""  # 管理员群(2026-10-01):告警/诊断/运维类推这里,与客户内容分开
                                    # 未配则回落总群(维持旧行为)
    feishu_webhook_weibo: str = ""  # 微博专属群 Webhook(非空则微博监控推到这里,否则推总群)
    feishu_webhook_xianyu: str = "" # 闲鱼专属群 Webhook
    feishu_webhook_douhot: str = "" # 抖音专属群 Webhook
    feishu_webhook_baidu: str = ""  # 百度专属群 Webhook
    feishu_webhook_wechat: str = "" # 公众号专属群 Webhook
    feishu_secret: str = ""         # 机器人签名校验密钥(为空则不签名)
    own_account_names: str = "天一项目拆解"  # 自营号名单(逗号分隔):飞书推送一律脱敏为「内部号」,防自营身份暴露(2026-09-29)
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
    jwt_expire_minutes: int = 10080  # 登录有效期(分钟,默认 7 天;勿按秒填——604800 会变成 420 天)
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
