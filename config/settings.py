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
    # ⚠️ 2026-10-03 全项目审查:删掉 13 个**生产代码零引用**的配置 —— 其中 3 个
    # (`INDEX_SOURCES`/`ALERT_MODE`/`FEISHU_DAILY_CRON`)**用户已在 `.env` 里配了却毫无作用**,
    # `.env.example` 也把它们当生效项列出。留着的害处很具体:下一个人照它去改,改了不生效。
    # 删的清单:baidu_cookie / wechat_traffic_cron(作业早已停用)/ index_sources / mock_index /
    # alert_mode / slope_threshold / min_heat / min_samples(前者多为旧指数分析残骸)/
    # feishu_daily_cron·feishu_wechat_cron·feishu_insight_cron·weekly_summary_cron
    # (**已被 `push_timeline.PUSH_KINDS` 取代**,改推送时段去那里)/ data_dir(备份路径是从库路径推的)。
    weibo_cookie: str = ""          # 微博登录态
    douyin_cookie: str = ""         # 抖音创作者中心/巨量算数登录态
    # B站 cookie(2026-10-05):`space/wbi/arc/search` 的**风控比普通搜索严得多** ——
    # 实测同一时刻 `search/type` 两边都正常,而 space 端点**本机家宽能过、机房 IP 稳定
    # `code=-352 风控校验失败`**(带 `v_voucher`,是应用层身份校验而非频率限制);
    # **配上登录 cookie 后两边都能过**。所以:本地跑 `python scripts/bili_login.py` 扫一次码
    # 写进 `cookie_store` 即可 —— **这个作业挂 `wechat`(本机),不必再往远程搬 cookie**。
    # 本字段只作兜底(临时手工指定),平时留空。
    bili_cookie: str = ""
    goofish_cookie_file: str = "data/goofish_cookie.txt"  # 闲鱼登录 Cookie 文件(gitignored)
    xianyu_keywords: str = "ps教程,网盘资源,代充,剪映会员,软件,素材,cad,ae,pr,office,会员,课程,影视,源码"  # 虚拟商品关键词
    xianyu_top_n: int = 100         # 前 N 虚拟商品榜(搜索级,无风控)
    xianyu_detail_limit: int = 10   # 慢速抓详情(想要数)的商品数;详情是触发 mtop 风控的最大爆发点,默认降到 10
    xianyu_deep_interval_hours: int = 6  # 闲鱼深采自动跑的最小间隔(小时):搜索接力深采时,距上次成功深采≥该值才跑,防风控
    xianyu_request_delay: float = 8.0  # 闲鱼相邻请求间隔(秒,带抖动);比通用更大,防 mtop 风控
    xianyu_batch_keywords: int = 5     # 每次采集最多处理的关键词数(风控降频:少量多次,按运行数轮转覆盖全部)
    xianyu_cooldown_minutes: int = 30  # 闲鱼触发人机验证(滑块)后,暂停采集该分钟数,避免反复撞枪口
    # **采集路径**(2026-10-02):`browser` = Playwright 打开真页面、在页面里调闲鱼自己的
    # `window.lib.mtop.request`(签名/指纹全由它的 JS 做);`protocol` = 老的自算签名纯协议
    # (实测被"哎哟喂,被挤爆啦"**账号级**限流,换出口也没用;页面内调用则正常)。
    xianyu_use_browser: bool = True
    xianyu_proxy_url: str = ""      # 闲鱼专用"单一固定"出口代理(http://user:pass@host:port,如住宅IP);留空直连。勿用轮换代理池——mtop token/session 绑定出口 IP
    # 闲鱼浏览器**闲置多久自动关**(秒;**≤0 = 不关**,退回旧行为)。为什么要关:
    # `xianyu_browser.get_client()` 是进程级单例,而全项目**没有任何调用方调 `close()`** ——
    # 只有进程退出时才关一次,于是那个**可见窗口会一直挂在桌面上**(2026-10-04 实测挂了
    # 10.5 小时、1.4 GB;用户看到的是"监控开的浏览器没关、还停在 about:blank")。
    # 轮内复用不受影响(一轮里多个关键词仍共用一个页面),代价只是每轮多 10~20 秒启动。
    xianyu_browser_idle_close_sec: float = 900
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
    # ⚠️ 此处原有 `agent_cooldown_hours` 与 `hotspot_agent_cron`,**生产代码零引用**(死配置):
    # 前者被 `AgentStage` 状态机取代,后者的时段早已迁到 `push_timeline.PUSH_KINDS`
    # (`hotspot_agent._tick` 根本没有独立 cron)。2026-10-03 审计删除 —— 留着只会让
    # 下一个人照它去改调度、改了却不生效。
    hotspot_agent_enabled: bool = True   # 热点→网盘选题 Agent(监控词热度 × 供应商新资源 → 发货建议,站内推送)
    hotspot_min_growth: float = 50.0     # 热点词 24h 涨幅达标线(低于此值不生成建议)
    hotspot_agent_top_n: int = 15        # 单轮最多产出几条选题建议(2026-10-01 由 8 上调)
    hotspot_agent_llm_top: int = 10      # 没现成资源的热点里,最多几个交给 LLM 生成选题
                                         # (2026-10-01 由 3 上调:实测 430 条候选里只有 3 条能进
                                         #  LLM,热点利用率不足 4%;DeepSeek 单价低,提到 10)
    # **单平台候选的保底名额**(2026-10-05 实测出来的):候选排序把"跨平台同现"放最前,
    # 于是当**跨平台条数 ≥ cap(60)** 时,`out[:cap]` **全是跨平台条目** ——
    # 平台权重(`SOURCE_FIT`)那段排序等于白做,**所有单平台源一条都进不了 LLM**。
    # 远程实测 24h:去重 **2027** 条候选里**跨平台只有 81 条** ⇒ 81 > 60,1946 条单平台全被埋
    # (B站对标号 `source="bili-pan"` 那条链正是这样一条都进不去,权重 0.60 全场最高也白搭)。
    hotspot_single_platform_slots: int = 20
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
    # ---- 本机 → 远程 单向同步(2026-10-04)----
    # **为什么需要**:公众号监听必须在本机(微信读书 Cookie 绑家宽出口 IP),而**选题 Agent 在远程**
    # —— 两边库独立 ⇒ 远程 Agent 的"竞品供给"一直吃的是旧快照(实测远程 wechat_articles 停在 09-28)。
    # 这里把本机的公众号数据**单向推过去**(只写远程,不回读)。
    # ⚠️ **凭据只放 `.env`,不写进代码/文档/提交**。留空 = 关(不同步)。
    remote_db_url: str = ""            # 直连用(留空 + 下面配 SSH = 走隧道);两种都留空 = 关
    # ⚠️ **实测结论:直连走不通,要走 SSH 隧道**(2026-10-04):
    #   `redu-mysql` 容器**没把 3306 映射到宿主机**,公网上那个 3306 是**宝塔自己装的另一个 MySQL**。
    #   业务库在 docker 网络里是 `172.22.0.2:3306` ⇒ 本机只能经 SSH 隧道打进去。
    # ⚠️ 以下凭据**只放 `.env`**,不写进代码/文档/提交。
    remote_ssh_host: str = ""          # 例:103.117.136.167(填了它才走隧道)
    remote_ssh_port: int = 22
    remote_ssh_user: str = "root"
    remote_ssh_password: str = ""     # 兜底(有 key 时不用它)
    remote_ssh_key: str = ""          # 专用私钥路径(优先于密码;可随时吊销)
    remote_db_host: str = "172.22.0.2"  # 隧道对端(容器 IP);
    remote_db_port: int = 3306
    remote_db_user: str = "redu"
    remote_db_password: str = ""
    remote_db_name: str = "redu"
    remote_sync_cron: str = "*/30 * * * *"
    # 链路体检推送(2026-10-05 用户口径:「没有推送小红书、B站、知乎、贴吧多平台的运行情况啊」):
    # 每天把**全部链路**的运行情况推**管理群**。⚠️ 全绿也推 —— "今天全绿"本身是运维要知道的信息,
    # 而且只有每天都来,人才会注意到"今天没来"(与 health_push 同一条)。
    chain_report_cron: str = "30 9 * * *"   # 每天 09:30(早上开工前,压在 wechat_digest 09:50 前面)   # 每 30 分钟一轮(数据是"补最新",不必更密)
    remote_sync_days: int = 14         # 每次回看多少天(靠天然键去重,天然幂等)

    candidate_auto_import_cron: str = "30 8 * * *"  # 每日自动收录时间(默认 08:30,紧随候选发现之后)
    # 公众号板块总结(2026-10-04):按**阅读数**总结 + **闭环体检**(发现/收录/监控三段各自
    # 最近产出),推**管理群**(具体数字只进管理群,客户群只看火爆程度)。
    # **每周一轮**:阅读数受列表额度限制(~2 天轮一圈),日推会重复同样的数。
    # 放**周一 09:50** —— 在拉新周录提醒(09:40)与复盘周报(16:30)之间,先让数字落地。
    wechat_digest_cron: str = "50 9 * * 1"
    # ---- Telegram 频道资源源(2026-10-01):公众号之外的第二路盘链 feed ----
    # ⚠️ 需能出网的环境(本机直连 t.me 超时且无本地代理端口)。解析逻辑已单测覆盖,
    # 启用条件只是网络——所以默认关,免得每轮刷失败日志。
    tg_enabled: bool = False        # 总开关
    tg_channels: str = ""           # 频道名(逗号分隔,不带 @),如 "channel_a,channel_b"
    tg_proxy: str = ""              # 出口代理(http://host:port);留空直连
    tg_limit: int = 30              # 单频道每轮取最近几条
    tg_cron: str = "*/30 * * * *"   # 采集频率(默认每 30 分钟;频道更新密度远低于热点榜)
    # ---- 跨平台同类资源号发现(2026-10-01):拿资源关键词去知乎/B站等平台搜同类号 ----
    # 只收录**内容里真含网盘链**的账号;各平台门槛见 app/services/cross_platform.py 头注
    cross_discover_enabled: bool = True
    cross_discover_keywords: int = 3        # 每轮取几个资源名当搜索词(**宁少勿多**:
                                            # 每个词都是一次平台请求,风控盯的就是"访问量")
    cross_discover_cron: str = "0 9 * * 1,4"  # **每周只跑两轮**(周一/周四 09:00)——
                                              # 用户要求"一次不要访问太多";持续高频轮询
                                              # 是最容易被平台判定为爬虫的模式
    # B站(账号**垂直**平台)的搜索词:**行业词,不是资源词**(2026-10-02 实测差异极大:
    # 拿资源词去 B站 搜用户返回 0 个;拿"网盘资源"搜返回 20 个号、20 个全是网盘号)。
    #
    # ⚠️ **2026-10-05:从 3 个扩成 9 个,并改成按窗口轮转**。原来三个词**每轮全用**,
    # 而词是固定的 ⇒ **每周跑两轮挖到的永远是同一批号**:实测三个词各挖满一页(20 个)后,
    # **59 个号全部停在 10-02**,此后三天 `新增0` —— **"每周两轮"的排期形同虚设**
    # (跑多少轮结果都一样)。这与抖音"热榜种子"是**同一类问题:词源不轮转 ⇒ 发现停摆**。
    #
    # 现在:池子 9 个、每轮取 3 个、按游标轮转 ⇒ **3 轮转一圈**(周一/周四 ⇒ 约 1.5 周全覆盖)。
    # **要加方向就往这里加词**(逗号分隔),窗口大小由 `cross_accounts._BILI_WINDOW` 定
    # —— 每轮取的词数**别调大**:每个词都是一次平台请求,风控盯的就是访问量。
    cross_bili_keywords: str = (
        "网盘资源,夸克网盘,百度网盘,"      # 已验证有效的基础词
        "迅雷网盘,UC网盘,"                  # 另两个盘的品牌词
        "影视网盘资源,漫剧资源,"            # 影视 / 漫剧方向
        "资料网盘,问卷资源"                 # 资料 / 问卷方向
    )
    # MediaCrawler 分支(**默认关**,2026-10-02 实测后降级):
    # 抖音实跑 143 条(13 内容 + 130 评论)的结论——① **账号拿不到**(该版本是作者的
    # "教学版",tools/user_hash.py 刻意把昵称脱敏成 `籽***）`、user id 换成 sha256 截断的
    # creator_hash、主页链接一律不采集 → 无法作为对标号入库);② **目标内容也没有**
    # (抖音盘链不放文案/评论里,盘链在视频内/主页/私信,搜索 API 层抓不到:13+130 条里
    # 真盘链 0 条);③ 其 LICENSE 为 NON-COMMERCIAL LEARNING LICENSE 1.1,明禁商业用途。
    # 保留代码与登录态备用,但**不再进定时轮**——开它只是每周多开一次浏览器白招风控。
    cross_mediacrawler_enabled: bool = False
    # ---- B站对标号**投稿标题**采集(2026-10-05)见 app/services/bili_account_scan.py ----
    # 用户口径:「**b站如果没有链可以只采集标题,从资源库里搜然后完善**」。
    # 这绕开了实测的死结:`space` 接口能通,但**视频简介里没有盘链**(实测 30 条投稿的
    # `desc` 里一条夸克/百度链都没有)。而**标题本身就是资源名**("野鹅敢死队 经典影片"),
    # 够用了 —— 落进 `hot_source_items`,选题 Agent 会自动拿它去**资源库**查有没有。
    bili_scan_enabled: bool = True
    # ⚠️ **匿名额度极低**:实测连发两次即 `HTTP 412`(HTML 拦截页),再试 `code=-352`。
    # 所以**每轮只扫 1 个号**、两小时一轮 ⇒ 一天 12 个号,59 个号约 5 天轮完一圈。
    # 调大前先想清楚:**被限流时整轮记 `failed`**(不静默),但你看到的会是"扫不动"。
    # ⚠️ 作业角色是 **hotspot(远程)**:产物落 `hot_source_items`,而**读它的选题 Agent
    #    (`agent_tick_all_users`)也在远程**;挂本机的话 `remote_sync` **不推这张表**,链当场就断。
    # ⚠️ **限流是按端点分的、与 IP 无关**(2026-10-05 受控对比:`search/type` 本机与远程都 OK,
    #    `space/wbi/arc/search` 两边都 412)⇒ 远程 IP **没被封**;烧额度的是当天的密集探测。
    bili_scan_cron: str = "0 */2 * * *"
    # ⚠️ **每轮扫几个号**:2026-10-06 由 **1 提到 6**。
    # 原来 `1 × 每 2 小时` = **12 个/天**,而池子有 59 个 ⇒ 跑一圈要 **5 天**
    # (用户口径:"59 个号只跑了 9 个,太慢")。池子还都是"没扫过的优先",
    # 所以慢的代价不只是慢 —— 新收录的号要等好几天才知道它是不是空壳。
    # 提到 6 ⇒ 72 个/天,**一天转一圈**。号与号之间有 `time.sleep(2.0)`,一轮约 12 秒,
    # 对 space 端点仍然很轻;而且库里有登录态 cookie(能显著放宽风控)。
    # ⚠️ 提它**必须先有** `scan_accounts` 里的"每号落盘":否则一次 -352 就把整轮战果清掉,
    # 提得越高丢得越多(那一条同日一起修了)。
    bili_scan_accounts_per_run: int = 6
    # 投稿数**低于这个值**就算"内容极少",轮转时降到空壳前面(但仍排在正常号之后)。
    # 为什么不能只降权 `==0`(2026-10-06 实测):59 个号里**空壳只有 1 个**,却有
    # 两个号**只有 1 条投稿**(`网盘资源分发`/`-网盘资源官-`)—— 它们不是空的,
    # 但实质产出与空壳无异,**每次轮到都白烧一次本就紧张的 space 额度**。
    # 设 0 = 关掉这条(退回"只降权空壳")。
    bili_scan_thin_below: int = 2
    # ---- 抖音推广线索(2026-10-02,用户提供的判据)见 app/services/douyin_leads.py ----
    # 抖音推广号的标题里会多出一段与内容无关的文字(常见是《…》包裹),推给运营人工确认。
    # 账号信息被 MediaCrawler 教学版脱敏,所以**只推视频链接、不自动收号**。
    douyin_leads_enabled: bool = True
    # **外部种子**条数(2026-10-03 用户口径:"能否做到自己去发现新的资源呢") ——
    # 见 `douyin_leads.hot_seed_words`。在此之前,所有搜索词都来自**我们已知的东西**
    # (群组里的资源名 + 资源库里的资源名),本质是"在已知圈子里向外扩散";
    # 热榜词**不来自我们的数据**(来自"抖音今天什么火"),才可能撞见完全没听说过的东西。
    # 每个词一次搜索(约 90 秒),所以条数要小。
    # ⚠️ **2026-10-05 从 3 降到 1** —— 第一次有了数据依据:按 `douyin_leads.keyword`
    # 归因全历史 43 条线索,**热榜种子有效率 0%(0/5)、资源名 86%(25/29)**;
    # 而它每轮占 3/7 个名额(43%)⇒ 约四成搜索预算花在 0% 有效率的词上。
    # 根因:热榜回答"**大家在聊什么**",这条链要的是"**谁在推资源**"。
    # ⚠️ 样本只有 5 条,**不够下定论** ⇒ **降额 + 单独计量**,攒够样本再决定要不要去掉。
    # ⚠️ 此前这个值**只当开关用**(真正常量是 `hot_seed_words(limit=3)` 的默认参数),
    # 改它不生效 —— 那个坑已修,现在它**真的**是条数。
    douyin_leads_hot_keywords: int = 1
    # **每天** 11:00(用户口径 2026-10-02:"我想要你每天都在抖音发现新的资源")。
    # 它要开浏览器,一次几分钟 —— 所以每天只跑一轮,别加频次。
    douyin_leads_cron: str = "0 11 * * *"
    douyin_leads_keywords: int = 4            # 每轮一共几个搜索词(每个词一次抖音搜索)
    # **群里新冒出来的资源名**贡献几个词。它是**资源名称的一种来源**(另一种是资源库)。
    #
    # ⚠️ **别再把它当成"词源方向错了"**(2026-10-04 一度这么改并默认关掉,用户当即纠正)。
    # 用户口径:"**你抖音搜索就跟着群里面的资源名字走,结合资源库里面的名称**"。
    # 自循环的真正解法**不是**不用群里的名字,而是**按类目轮换**(`category_topics.py`):
    # 这轮只搜「资料」类的资源名、下轮「影视」类 —— 换了类目,自然就换了一批群,
    # 而不是在同一批群里反复打转。
    douyin_leads_group_keywords: int = 3
    # **类目表**(可扩展,2026-10-04 用户口径:"上面那些类目只是举例但是**并不是全部**,
    # **不要仅仅局限这几个**")。
    #
    # 格式:`类目:话题1,话题2|类目2:话题3,话题4` —— 用 `|` 分类目、用 `:` 分话题、用 `,` 分多个。
    # **顺序 = 轮换顺序**(每跑一轮抖音线索换下一个类目,见 `category_topics`)。
    # 留空则用代码里的**草稿种子**(`category_topics.DEFAULT_CATEGORIES`)—— 那只是起点,不是全集,
    # 随时往这里加即可(**不用改代码**)。
    #
    # 例:`资料:真题,考公,四六级|影视:短剧,漫剧,解说|问卷:问卷,测评|软件:安装包,插件`
    lead_categories: str = ""
    # **我们的品牌词**(2026-10-02 用户口径"不要带别人的关键词,可以把你关键词也换成我们的,
    # 我们的关键词念飞思雪"):推送里**只出现它** —— 别人的口令《…》/搜索词/来源群名一律不露
    # (卡片是发到客户群看的,露出别人的群名等于把人往别人那儿送)。
    # 同时也作为**发现用**的搜索词之一(看谁在蹭我们的牌子)。
    brand_name: str = "念飞思雪"
    # 线索里的《口令》**自动变成资源**(2026-10-02):解析成分享链就直接转存入库,
    # 指向群组就加群(群里的资源由群采集轮收)。转存慢且占盘,故每轮限量。
    douyin_leads_auto_transfer: bool = True
    douyin_leads_transfer_limit: int = 3      # 每轮最多真转存几条(0 = 只解析不转存)
    # ---- 迅雷盘同步(2026-10-02)见 app/services/xunlei_sync.py ----
    # 扫用户的迅雷盘 → 新转存进来的资源**自动生成我方分享链** → 入库(与公众号资源统一管理)。
    # ⚠️ "用口令找到资源并转存"这一步**只有手机 App 能做**:服务端搜索接口不对外开放
    # (2026-10-02 实测 `/drive/v1/share/search` 要 share_id、`api-shoulei-ssl` 搜索端点 403),
    # 而且部分口令是**群组口令**(要先进群,PC 客户端没有进群功能,所以 PC 端搜不出来)。
    # 所以人工只保留"App 里搜一下 + 点转存",本作业接手剩下的全自动部分。
    xunlei_sync_enabled: bool = True
    xunlei_sync_cron: str = "*/30 * * * *"    # 每 30 分钟扫一次(秒级完成,不打风控)
    # ---- 迅雷群组采集(2026-10-02)见 app/services/xunlei_group.py ----
    # 群消息流里**群主发的分享卡自带 `pan.xunlei.com/s/<share_id>`** —— 客户端唯一的
    # "口令 → shareID"那一步,**群组替我们做了**。两步走:①采集登记(pending,纯 HTTP 读,
    # 可高频)②**限量**转存(转存慢且占盘,所以每轮只放 `transfer_limit` 条)。
    xunlei_group_enabled: bool = True
    xunlei_group_cron: str = "*/20 * * * *"   # 每 20 分钟采一轮
    xunlei_group_transfer_limit: int = 5      # 每轮最多转存几条(0 = 只采集不转存)
    # **转存闸门**(2026-10-02):盘使用率到这条线就**整批不搬**。
    # 为什么必须有:实测自动转存把群里的大合集搬进盘,空间顶到 126%,之后全部
    # `file_space_not_enough`;而**文件夹的体积 API 根本不给**(分享详情里 size 恒为 0,
    # parent_id/file_id 被忽略),所以只能靠"盘级"兜底 + 名字判泛化大包(见 BULK_WORDS)。
    xunlei_transfer_max_usage_ratio: float = 0.9
    # **转存落点**(2026-10-02 用户口径):所有自动转存的资源都放进这个**目录名**下,
    # 不再散落在网盘根目录。按**名字**找(不是写死 id)——目录被改名/重建也能跟上。
    # 找不到就落根目录(并记一条 warning),不会因为目录没了就整条链停摆。
    xunlei_transfer_parent: str = "最全文件"
    # ⚠️ 但**名字查找不可靠**:2026-10-02 实测「最全文件」明明存在(GET by id 返回 200),
    # 却**不出现在根目录列表里**(那个列表只给 19 项,另一处大坑) → 解析不到就落根目录。
    # 所以把 id 直接配上,**优先用 id**,名字只在没配 id 时兜底。
    xunlei_transfer_parent_id: str = ""
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
    # newsnow 容器地址(2026-10-01 可配):默认本机直跑;**容器化部署时必须改**——
    # 在 redu-api 容器里 `127.0.0.1` 指容器自己,不是宿主机,newsnow 就全连不上。
    #
    # ✅ **2026-10-04 登录远程实测定案**(此前这段注释里的"远程 newsnow 40 个全空"**是错的**):
    #   · 远程 `.env` 配的是 **`http://172.22.0.1:4444`**,**能连通**;
    #   · `redu-api` 实际在 **`redu_default`** 网络,其网关就是 **172.22.0.1**;
    #   · **`172.17.0.1` 同样能通** —— 那是 `bridge` 网络的网关,而 newsnow 绑的是
    #     `0.0.0.0:4444`,**宿主机任一 IP 都能到**。⇒ **两个都能用,别纠结哪个"对"**。
    #   · 真正要避开的只有一件事:**别填容器自己的 127.0.0.1**。
    #   · 远程实测 `hot_source` 每几分钟一轮、`ok=42 failed=1 items=959`、`hot_source_items` 7 万+ 条。
    # ⚠️ 当时误判成"全空"的原因:拿**本机**的 `hot_source_items`(冻结在 10-01)当远程现状 ——
    # 而**两边库是独立的,且远程用 MySQL、本机用 SQLite**。
    hot_newsnow_url: str = "http://127.0.0.1:4444"
    growth_threshold: float = 0.30  # 环比增长率判定阈值
    top_n: int = 10                 # 进入指数分析的热搜词数量

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
    # ---- 线索平台(2026-10-02):"按资源词搜内容 → 抓口令 → 转存"这条链的搜索源 ----
    # 现状:只有**抖音**成立(内容层真带《口令》,实测);B站/知乎**实测没有**口令形态,
    # 快手/小红书/微博/贴吧**待验证**(需先扫码登录一次,见 doc/pan-promotion-channels.md §九)。
    # 接新平台 = 这里加一个名字 + 配它的专属群(下面)+ 用 MediaCrawler 登录一次。
    leads_platforms: str = "douyin"        # 逗号分隔;每轮按顺序各搜一遍(每个都开浏览器,别贪多)
    feishu_webhook_kuaishou: str = ""      # 快手专属群(未配回落主群)
    feishu_webhook_xiaohongshu: str = ""   # 小红书专属群
    feishu_webhook_bilibili: str = ""      # B站专属群
    feishu_webhook_zhihu: str = ""         # 知乎专属群
    feishu_webhook_tieba: str = ""         # 贴吧专属群
    # 多平台"雷达"专属群(2026-10-06 用户新建):「🔥 多平台热榜速览」推这里。
    # 未配则回落主群 —— 与其余板块同一条规矩(专属群各收各的,主群不因此少收)。
    feishu_webhook_multiplatform: str = ""
    # ---- 网盘资源发现(2026-10-02)见 app/services/pan_discovery.py ----
    # 与抖音那条链**形态不同**:抖音是**口令型**(标题里《群名》,要先解析),知乎是**直链型**
    # (回答里直接贴夸克/百度盘链,拿到就能转存)。实测 5 个资源词搜知乎 → 87 条里 3 条带直链。
    pan_discovery_enabled: bool = True
    pan_discovery_cron: str = "30 11 * * *"   # 每天 11:30(错开抖音那条的 11:00)
    pan_discovery_keywords: int = 5           # 每轮几个资源词(**逐词限速**,别贪多)
    # ⚠️ **原为 3,2026-10-05 拆成「积压 + 新发现」两份额度并调高**。
    # 实测(2026-10-05 运行记录):一轮 **候选 44 条、只转存 3 条** ⇒ 库里堆了 **41 条
    # `pending`,理由清一色是「本轮转存额度用完」**—— 也就是说它们是**健康的、能搬的链**,
    # 纯粹被额度卡住;按 3 条/天算要**两周**才清得完,而每天还在往里灌新的。
    # 更糟的是**积压会把新发现饿死**:backlog 排在最前且吃满额度 ⇒ fresh 永远轮不到,
    # 而**新资源时效性最强**(热点过了就没意义),积压的反而是已经错过高峰的。
    # 所以拆成两份:积压吃大额慢慢清,新发现保底推进。
    pan_discovery_transfer_limit: int = 10    # **积压**额度:每轮最多搬几条存量(pending/failed)
    pan_discovery_fresh_limit: int = 3        # **新发现**额度:每轮最多搬几条本轮新搜到的
    # 积压超过这个数就告警。**为什么必须告警**:积压原来是**完全静默**的——运行记录只写
    # 「候选44 转存3」,没有一个字说"还剩 41 条在排队"。系统看起来 success,实际在堆积,
    # 与本仓反复踩的「静默失败 = 假成功」是同一类问题。
    pan_discovery_backlog_alert: int = 60
    # ---- 夸克**口令(U口令)**见 app/services/quark_kouling.py(2026-10-06)----
    # 抖音线索里那些**迅雷搬不动**的,用**模拟器里的夸克 App** 再试一遍。
    # ⚠️ **只能走 UI**:解析接口带 `sign` 签名(算法未解出),抓包被**证书固定**挡住;
    #    而实测**改写过的抖音标题原样喂进去 App 也能匹配**(`咐置铸剑上供叩苓` → 「铸剑纳贡」),
    #    所以不需要"提取口令"这一步。
    # ⚠️ **每轮别贪多**:一个口令 15–20 秒,而雷电模拟器是单实例独占的。
    # **夸克口令专搜词**(2026-10-06):上面那批词来自**我们自己的资源库** ——
    # 搜出来自然是"我们已有的"、而且**几乎全是迅雷形态**(抖音的《》判据)。
    # 实测用下面这几个词搜抖音,**一次就捞到 15 条夸克口令帖**,而它们此前**一条都进不来**。
    douyin_leads_quark_keywords: str = "夸克口令,咐置,网盘口令"
    # **发布时间下限**(2026-10-06 用户口径:「**2026年10月份之前的不要再保存进来了**」)。
    # 起因:抖音上的网盘推广帖**高度重复** —— 一条 8 月的老帖会被反复推,照样"刚被发现",
    # 于是"每天搬 8 条"搬回来的大半是**已有的老资源**(实测一轮 8 条里 5 条命中三盘互通)。
    # 留空 = 不过滤。⚠️ **没有发布时间的也不存** —— 判不了就宁可不收,
    # 但**会打日志报数**(绝不静默丢,那是本仓最忌讳的)。
    douyin_leads_min_publish_date: str = "2026-10-01"
    # **通用**发布/投稿时间下限(2026-10-06):抖音与 B站对标号**共用同一条口径** ——
    # 用户口径是「2026年10月份之前的不要再保存进来了」,那就该**所有内容源**都按它筛,
    # 而不是只筛抖音(只筛一条链 = 老资源从另一条链照样进来)。
    content_min_publish_date: str = "2026-10-01"
    quark_kouling_enabled: bool = True
    quark_kouling_cron: str = "0 */2 * * *"
    # ⚠️ **雷电模拟器单实例独占** —— 一轮跑着别的轮次进不来,所以间隔要够宽。
    # 吞吐算过:一条 15–20 秒,`8 条/轮 × 12 轮/天` ≈ **96 条/天**(一轮约 2.5 分钟),
    # 而实测抖音上夸克口令帖的**日供给只有几条到十几条** ⇒ 8 已远超供给,
    # 多出的产能专门用来**清存量待办**;再调大只是在同一批存量上多烧时间。
    quark_kouling_per_run: int = 8
    # 模拟器常开是这条链的前提;不开时它只会一路失败并记 failed(不静默)
    # 贴吧源(2026-10-03 加,**实测是主源**):同样一批资源词,贴吧 16 条里 **9 条带盘链(56%)**,
    # 知乎加限定词后也才 17.8%,且贴吧全是百度网盘、内容集中在影视剧集(对口赛道)。
    # 代价:走 MediaCrawler(一次浏览器 ≈32s,**需要先登录过一次**,见 tools/tieba_login.py)。
    pan_discovery_tieba: bool = True
    # 微博源(2026-10-05 新增):实测**质量最好**的盘链源 —— 微博有"资源铺"账号
    # (#小说资源铺# 等超话),帖子里直接挂盘链;「资源 合集」实测去重 18 条(百度14/夸克3/迅雷1)。
    pan_discovery_weibo: bool = True
    # ⚠️ **知乎上"裸词"搜不到盘链**(2026-10-03 实测,同账号同时段对照):
    #     兰香如故→0 / 霸王茶姬杯贴→0 / 鞠婧祎→0 / 苏超→0;而同一批词**补上限定词**后:
    #     兰香如故 全集 网盘→1 / 教程 资料 网盘→2 / 四级真题 网盘→4 / PS教程 全套 网盘→6。
    #   命中率 0% → 17.8%。原因:知乎的盘链回答集中在**资料/合集**类问题,裸剧名搜到的全是剧情讨论。
    #   所以给每个动态词自动补下面的后缀。
    pan_discovery_suffix: str = " 网盘"
    # 可选的**资料型词表**(逗号分隔,**优先于动态词**)—— 按你的赛道填,实测这类词命中率最高。
    # 留空则只用「动态词 + 后缀」。示例(可直接抄):四级真题 网盘,考研资料 合集 网盘,PS教程 全套 网盘
    pan_discovery_terms: str = ""
    # ---- 跨平台资源热度(2026-10-02)见 app/services/resource_presence.py ----
    # 用户口径:"小红书可以只抓取资源名称,网盘链接从资源库匹配,别的也照这个模式"——
    # 绕开"平台上没有链"的死结:平台上有没有链不重要,只要**有人在做同一个资源**,库里就有链。
    presence_enabled: bool = True
    # **每天** 09:00(2026-10-03 用户口径:"小红书也每天定时跑一次"、"快手也每天")。
    # 原先每周一次是怕"每平台各开一次浏览器"太费;实测小红书单关键词约 277s(它逐条拉笔记
    # 详情),两个平台 × 3 个资源名 ≈ 20~30 分钟,每天一轮可接受。
    presence_cron: str = "0 9 * * *"
    # 探哪些平台(逗号分隔)。
    # · 贴吧 2026-10-03 实测可用(38 秒 10 条,「《平凡的世界》全集百度网盘」这类影视推广)。
    # · **B站走公开 API,不开浏览器**(wbi 签名本地可算、匿名即可):实测搜「网盘资源」
    #   20 条标题就是「【原版】火影忍者720集网盘资源」—— 而 MediaCrawler 抓 B站 起不来
    #   (`Chromium distribution 'chrome' is not found`),所以给它单开一条 API 路。
    presence_platforms: str = "xiaohongshu,kuaishou,tieba,bilibili"
    presence_names: int = 3                             # 探几个资源名(词越多越慢)
    # 匹配上但**还没有我方链**的资源,每轮自动转存几个(2026-10-04 用户口径:
    # "配上了就自动转存 + 推我们的链")。⚠️ 转存是**网络写操作**且**占盘**,
    # 一轮开几十个会把盘顶满(迅雷那边刚因空间不足整批停过),所以给上限。
    # 设 0 = 不自动转存(退回旧行为:只附库里已有的链)。
    presence_transfer_limit: int = 3
    # ---- 过期转存清理(2026-10-03 用户口径)----
    # "可以定期清理久远资源,如果**一个星期内没有人再发了**就可以删除了" —— 见
    # `app/services/xunlei_cleanup.py`(判据:转存时间 / 群分享 / 线索 / 发现链 取最新那个)。
    # ⚠️ **默认关**:删盘难逆,先让人看过清单(`GET /api/xunlei/cleanup/plan`)再开。
    # ⚠️ 它**解决不了"盘快满"**:实测本流水线在盘里只有 ~0.9TB,配额 24TB 的大头在别处。
    xunlei_cleanup_enabled: bool = False
    xunlei_cleanup_days: int = 7                        # 闲置超过这么多天视为过期
    xunlei_cleanup_max_per_run: int = 20                # 单轮最多移入回收站几个(接口无批量)
    xunlei_cleanup_cron: str = "30 3 * * *"             # 每天 03:30(避开 04:00 那波重活)
    # ---- 同名去重(2026-10-04,用户:"里面我发现一些重名的文件你去删除吧" + "做成定时作业")----
    # 迅雷转存同名时会自动加 `(1)`/`(2)`,同一份资源就躺了好几份。
    # ⚠️ **与上面的 `xunlei_cleanup` 默认值**相反**:那个**默认关**(删盘难逆,先让人看清单),
    # 这个**默认开** —— 因为它**只删"内容可证明完全相同"的副本**(逐个进文件夹比子项名+大小),
    # 而且走**移入回收站**(可恢复),风险不是一个量级。用户也明确要求它按时跑。
    xunlei_dedupe_enabled: bool = True
    xunlei_dedupe_max_per_run: int = 30                 # 单轮最多移入回收站几个(接口无批量)
    xunlei_dedupe_cron: str = "0 4 * * 0"               # 每周日 04:00(重名积累得慢,不用每天)

    # ---- 拉新周录提醒(2026-10-03)----
    # `pan_recruit_weekly` 是**转化回路唯一的真值入口**,但至今 0 行 —— 入口(接口/前端)早就有,
    # 缺的只是"有人去录"。每周一提醒一次;**录了就不提醒**(没录才推),免得变成噪音。
    # 只推**管理员群**(内部待办 → 不回落客户群)。
    recruit_reminder_enabled: bool = True
    recruit_reminder_cron: str = "40 9 * * 1"           # 每周一 09:40(周报 16:30 之前,先录后复盘)
    # ---- 跨实例健康可见(2026-10-03)----
    # 背景:本机(wechat)与远程(hotspot)**数据库各自独立**,微博/抖音/百度热榜归远程跑,
    # 本机只知道它们"数据停在某天",分不清是**远端整机挂了**还是**那些源本身没更新**。
    # ① `peer_health_url`:对端基址 —— 本机填远程地址,用对端**已有的公开 `/healthz`** 探活
    #    (不新增暴露面)。留空则本机不探、显示"未配置"。
    peer_health_url: str = ""
    # ② 远端每天推一张**板块健康卡**到管理员群(零配置、零暴露面);远端整机挂了这张卡就断,
    #    "该来没来"本身就是信号。作业角色 hotspot → 只在远端跑,本机不重复推。
    health_push_enabled: bool = True
    health_push_cron: str = "20 9 * * *"
    # ③ 磁盘水位守卫(2026-10-03 全项目审查补):磁盘写满 → **整站 502**(写不进库、写不进日志),
    #    而水位是**逐渐**涨上来的,远在崩溃之前就有征兆 —— 这是最典型"本可以预警却没预警"的故障。
    #    每天查一次,超 `disk_warn_ratio` 推**管理员群**(运维信息,按 2026-10-01 受众分流口径);
    #    `disk_crit_ratio` 只决定措辞级别(🔴严重/🟠偏高),不改变是否推送。
    #    告警去重靠 `notify_incident` 的冷却门(标题里**不含数字**,否则每天比值都变、冷却形同虚设)。
    disk_guard_enabled: bool = True
    disk_guard_cron: str = "40 9 * * *"      # 与 health_push(9:20) 错开,别挤在一起
    disk_warn_ratio: float = 0.85
    disk_crit_ratio: float = 0.95
    feishu_secret: str = ""         # 机器人签名校验密钥(为空则不签名)
    own_account_names: str = "天一项目拆解"  # 自营号名单(逗号分隔):飞书推送一律脱敏为「内部号」,防自营身份暴露(2026-09-29)
    feishu_hot_rank_jump: int = 3          # 排名跳升 ≥ 该名次即实时推送
    feishu_hot_ratio: float = 0.30         # 分值环比涨幅 ≥ 该比例即实时推送
    feishu_burst_min_confidence: str = "高"  # 实时推送"预测爆发"所需最低置信度(高/中/低);中低置信只进日报与洞察、不实时推,减少噪音
    feishu_alert_cooldown_hours: int = 6   # 同一话题实时推送冷却(小时),防刷屏
    # 抖音热点宝走代理池:部分服务器 IP 会被抖音风控(直接返回 502 nginx),
    # 开这个后 douhot 采集走 PROXY_EXTRACT_URL 提取的住宅代理。生产服务器建议开启。
    douhot_use_proxy: bool = False

    # ---- 服务 ----
    app_port: int = 8080

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
