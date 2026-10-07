"""多租户 ORM 模型(数据均以 `user_id` 隔离)。

- `User` / `UserCookie`:用户与用户自行配置的各平台 Cookie。
- 监控数据表(WeiboHotItem / WeiboTrend / XianyuItem / XianyuDaily /
  DouhotWord / DouhotAlerted / AlertRecord / RunRecord)全部带 `user_id`。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                       UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")   # admin/operator/user
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    smtp_host: Mapped[str | None] = mapped_column(String(128), nullable=True)   # 用户自定义SMTP(可选)
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_user: Mapped[str | None] = mapped_column(String(128), nullable=True)
    smtp_pass: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_from: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reset_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reset_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    cookies: Mapped[list["UserCookie"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserCookie(Base):
    __tablename__ = "user_cookies"
    __table_args__ = (UniqueConstraint("user_id", "platform", name="uq_user_platform"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32))  # weibo/baidu/douyin/goofish
    cookie: Mapped[str] = mapped_column(Text())        # 加密存储
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    user: Mapped[User] = relationship(back_populates="cookies")


class HotSourceItem(Base):
    """多平台热榜统一表(v2.2.0 命门自持热榜源体系;bilibili/douban 自研 + newsnow 长尾)。

    每轮全量快照(同 weibo_hot_items 模式),retention 治理(cleanup_old_data)覆盖。
    """

    __tablename__ = "hot_source_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)   # bilibili/douban/zhihu/...
    rank: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(700), default="")
    extra: Mapped[str] = mapped_column(String(200), default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    # **内容自身的发布时间**(2026-10-06)。⚠️ 与 `captured_at`(我们抓到的时刻)是两个东西:
    # 只有它才是新鲜度的真依据 —— 老帖被反复扫到时,`captured_at` 照样是"刚刚"。
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WeiboHotItem(Base):
    __tablename__ = "weibo_hot_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    heat: Mapped[int] = mapped_column(Integer, default=0)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class BaiduHotItem(Base):
    """百度热搜条目(多租户,按 user 隔离;见 doc/dev.md §5.2b)。"""

    __tablename__ = "baidu_hot_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    heat: Mapped[int] = mapped_column(Integer, default=0)   # 热度值
    rank: Mapped[int] = mapped_column(Integer, default=0)
    url: Mapped[str] = mapped_column(String(500), default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class WeiboTrend(Base):
    __tablename__ = "weibo_trends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    keyword: Mapped[str] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(32))
    growth: Mapped[float | None] = mapped_column(Float, nullable=True)
    slope: Mapped[float | None] = mapped_column(Float, nullable=True)
    rising: Mapped[bool] = mapped_column(Boolean, default=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class XianyuItem(Base):
    __tablename__ = "xianyu_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(500))
    price: Mapped[str] = mapped_column(String(64), default="")
    seller: Mapped[str] = mapped_column(String(128), default="")
    pic: Mapped[str] = mapped_column(String(500), default="")
    hit_keywords: Mapped[int] = mapped_column(Integer, default=0)
    best_rank: Mapped[int] = mapped_column(Integer, default=0)
    keywords: Mapped[str] = mapped_column(String(500), default="")
    # 行情三件套:**搜索响应自带**(2026-10-03 发现于 fishTags),不必打详情接口 → 绕开滑块
    want_count: Mapped[int] = mapped_column(Integer, default=0)   # 想要数(需求端热度)
    sold_price: Mapped[str] = mapped_column(String(32), default="")  # 到手价(价位行情用,好解析)
    tags: Mapped[str] = mapped_column(String(255), default="")     # 其他标签(降价%/发货速度…)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DouhotWord(Base):
    __tablename__ = "douhot_words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(128), index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    rising_ratio: Mapped[float] = mapped_column(Float, default=0)
    rising_speed: Mapped[str] = mapped_column(String(64), default="")
    trend_len: Mapped[int] = mapped_column(Integer, default=0)
    latest_value: Mapped[float] = mapped_column(Float, default=0)
    trend_delta: Mapped[float] = mapped_column(Float, default=0)
    query_day: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DouhotAlerted(Base):
    __tablename__ = "douhot_alerted"
    __table_args__ = (UniqueConstraint("user_id", "title", name="uq_douhot_user_title"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(128))
    alerted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AlertRecord(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    section: Mapped[str] = mapped_column(String(32), default="")   # weibo/xianyu/douhot
    keyword: Mapped[str] = mapped_column(String(500))
    reason: Mapped[str] = mapped_column(Text(), default="")
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RunRecord(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))      # weibo/xianyu/douhot
    status: Mapped[str] = mapped_column(String(16), default="running")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # ⚠️⚠️ **这一列从来没被写过(2026-10-07 实测全表 100% 为 NULL)**,也**没有任何读取方** ——
    # `_record_run` 是在作业**跑完之后**才被调用的,它只写 `started_at`(= 落记录的时刻,
    # 严格说是**结束**时刻,名字也是错的)。所以「这次跑了多久」这个维度**结构上就缺失**,
    # 想量侵占风险(比如"模拟器作业占着库多久")时拿不到数。
    # ⇒ **别查 `finished_at`**(你会得到一片 NULL,还以为是"没跑完");
    #   作业级时长看 `JobHeartbeat.last_duration_ms`(由 `scheduler._safe` 计时写入)。
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    detail: Mapped[str] = mapped_column(Text(), default="")


class UserSchedule(Base):
    """每用户每板块的采集频率(见 doc/dev.md §6)。

    调度器每分钟扫一次:`enabled` 且距 `last_run_at` 已满 `interval_minutes` 的就跑。
    改频率后下一分钟即生效(读的是库,不需要重建调度作业)。
    """

    __tablename__ = "user_schedules"
    __table_args__ = (UniqueConstraint("user_id", "section", name="uq_schedule_user_section"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    section: Mapped[str] = mapped_column(String(32))   # weibo/xianyu/douhot
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class XianyuDaily(Base):
    """闲鱼商品每日快照:想要数/浏览量/类目等,用于今日vs昨日与类目分布。"""

    __tablename__ = "xianyu_daily"
    __table_args__ = (UniqueConstraint("user_id", "item_id", "snap_date", name="uq_xy_item_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(64), default="")
    price: Mapped[str] = mapped_column(String(64), default="")
    want_count: Mapped[int] = mapped_column(Integer, default=0)   # 想要数
    collect_count: Mapped[int] = mapped_column(Integer, default=0)  # 收藏数
    sold_count: Mapped[int] = mapped_column(Integer, default=0)   # 已售/出单量
    view_count: Mapped[int] = mapped_column(Integer, default=0)  # 浏览量
    seller_fans: Mapped[int] = mapped_column(Integer, default=0)  # 卖家粉丝
    # 这行的来源:`search` = 每轮搜索免费带回来的热度(想要数,95% 覆盖);
    # `detail` = 深采详情接口补全的(含收藏/出单/浏览量,但受滑块限制)。
    # 同日同商品只有一行(唯一约束),靠它判优先级:**detail 不被 search 覆盖**。
    source: Mapped[str] = mapped_column(String(16), default="detail")
    tags: Mapped[str] = mapped_column(String(255), default="")
    snap_date: Mapped[str] = mapped_column(String(16))            # YYYY-MM-DD
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DouhotWatch(Base):
    """关键词监控:用户自选板块 + 榜单类型 + 关键词(微博/闲鱼/抖音/百度通用)。

    不加数据库唯一约束:同一关键词可在不同板块同时监控,去重靠 `get_watch`。
    历史表曾有 `(user_id, list_type, keyword)` 唯一约束,已在 _migrate 中去除
    (否则跨板块同词会 UNIQUE 冲突)。
    """

    __tablename__ = "douhot_watch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    section: Mapped[str] = mapped_column(String(16), default="douhot")  # weibo/xianyu/douhot/baidu
    list_type: Mapped[str] = mapped_column(String(32))  # word(内容词)/search(搜索榜);微博/闲鱼/百度固定 word
    keyword: Mapped[str] = mapped_column(String(128))
    filter_keyword: Mapped[str] = mapped_column(String(64), default="")  # 只监控标题含该词的主题(每个关键词独立,默认空=不过滤)
    date_window: Mapped[int | None] = mapped_column(Integer, default=None)  # 监控时段(小时):1/24/72/168=近1h/近1天/近3天/近7天;None=按榜单默认
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DouhotWatchSnap(Base):
    """关键词监控快照(每次采集的记录)。

    `entry_title` 用于**榜单定向搜索**类关注(搜索/视频/话题):一次采集会把搜出的
    多个相关主题各存一条(每条 entry_title=该主题标题),从而逐条追踪趋势;
    内容词(word)类单值,entry_title 留空。
    """

    __tablename__ = "douhot_watch_snap"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    section: Mapped[str] = mapped_column(String(16), default="douhot")
    list_type: Mapped[str] = mapped_column(String(32))
    keyword: Mapped[str] = mapped_column(String(128), index=True)
    entry_title: Mapped[str] = mapped_column(String(255), default="")  # 命中条目标题(榜单搜索类每条一记录;内容词留空)
    score: Mapped[float] = mapped_column(Float, default=0)   # 该榜中的得分
    rank_now: Mapped[int] = mapped_column(Integer, default=0)  # 当前排名(0=未上榜)
    trend_growth: Mapped[float] = mapped_column(Float, default=0)  # 该主题(窗口)趋势增长,由 trends 序列算出
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class FeishuAlert(Base):
    """飞书实时提醒去重记录:同一 (用户, 板块, 话题) 在冷却期内只推一次,防刷屏。"""

    __tablename__ = "feishu_alerts"
    __table_args__ = (UniqueConstraint("user_id", "section", "title", name="uq_feishu_alert"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    section: Mapped[str] = mapped_column(String(32))   # weibo/xianyu/douhot
    title: Mapped[str] = mapped_column(String(500))
    reason: Mapped[str] = mapped_column(String(255), default="")
    alerted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DouhotWindowSnap(Base):
    """抖音关键词**多窗口**热度快照(与 DouhotWatchSnap 单窗口互补,独立表零侵入)。

    每个监控词每轮采集**同时**拉多个时间窗(默认近1h=1 + 近1天=24,见 DOUHOT_WINDOW_WINDOWS),
    各窗一条快照 → 同一关键词跨窗口对比找趋势(1h 激增=爆发 / 1h 降温=回落 / 1天冷1h起=新起势)。

    `entry_title`=该窗口命中条目标题(内容词留空);`captured_at` 同一轮共享一批时间戳。
    """

    __tablename__ = "douhot_window_snap"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    list_type: Mapped[str] = mapped_column(String(32), default="word")
    keyword: Mapped[str] = mapped_column(String(128), index=True)
    entry_title: Mapped[str] = mapped_column(String(255), default="")
    window: Mapped[int] = mapped_column(Integer, default=24)   # 小时:1=近1h, 24=近1天
    score: Mapped[float] = mapped_column(Float, default=0)
    rank_now: Mapped[int] = mapped_column(Integer, default=0)
    trend_growth: Mapped[float] = mapped_column(Float, default=0)  # 该窗口由 trends 序列算的增长率
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class WechatArticle(Base):
    """公众号文章(暂供内容选题分析;等接入带流量的 API 后扩展流量字段)。"""

    __tablename__ = "wechat_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    author: Mapped[str] = mapped_column(String(128), default="")   # 公众号名(对标号)
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text(), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    publish_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # ---- 监听/同步扩展(2026-09-07)----
    source: Mapped[str] = mapped_column(String(16), default="manual")     # manual/listen/sync
    benchmark_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 来自哪个对标号
    pan_types: Mapped[str] = mapped_column(String(128), default="")       # 命中的网盘类型,逗号分隔
    pan_urls: Mapped[str] = mapped_column(Text(), default="")             # 命中的分享链接(换行分隔)
    my_pan_urls: Mapped[str] = mapped_column(Text(), default="")          # 转存后自己的分享链接(换行分隔)             # 提取到的分享链接(换行分隔)
    # ---- 流量数据(dajiala read_zan_pro 采样,¥0.06/篇/次)----
    read_num: Mapped[int] = mapped_column(Integer, default=0)
    zan_num: Mapped[int] = mapped_column(Integer, default=0)
    looking_num: Mapped[int] = mapped_column(Integer, default=0)
    share_num: Mapped[int] = mapped_column(Integer, default=0)
    collect_num: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    traffic_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 最近一次采样时间
    # ⚠️ `traffic_at` **已废弃**与 `WechatTrafficSample` 同因(dajiala 摘除)。
    # **实测全库非空数 = 0** ⇒ **别再拿它当判断条件**:`feishu/_cards.py` 曾用它
    # 过滤出一行"已采样阅读 N 篇",而那个 N **结构上永远是 0**(长得像统计、其实恒定)。
    # 要判"这篇有没有读数",用 `read_num > 0`。
    sample_count: Mapped[int] = mapped_column(Integer, default=0)         # 已采样次数
    first_read_num: Mapped[int] = mapped_column(Integer, default=0)     # 首采样阅读数(基线对比)
    trend_flag: Mapped[str] = mapped_column(String(16), default="")     # 爆点苗头 / 回落 / 空
    quality: Mapped[int] = mapped_column(Integer, default=0)            # 内容质量分 0~10
    # 成功进过飞书卡片的时间。NULL = 采到却从未推出去(飞书抖动/超时/进程被杀),
    # 由 `repush_unpushed` 在下一轮监听开头补推——"近24h全推"是铁律,静默少推不能存在。
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class HotspotSuggestion(Base):
    """热点→网盘拉新建议(hotspot_agent 产出):落表以便回看与效果回填。

    kind: match=热点已有现成资源(供应商已发) / llm=无资源由 LLM 生成选题。
    效果回填(P3):后续按 keyword+created_at 关联「用户是否发文/转存增长」即可闭环。
    """

    __tablename__ = "hotspot_suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    keyword: Mapped[str] = mapped_column(String(128), index=True)
    growth: Mapped[float] = mapped_column(Float, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="llm")  # match / llm
    resource_title: Mapped[str] = mapped_column(String(255), default="")
    link: Mapped[str] = mapped_column(String(500), default="")
    plan: Mapped[str] = mapped_column(String(500), default="")
    draft: Mapped[str] = mapped_column(Text, default="")  # AI 生成的发布文案(v2.5.0,按需生成)
    saves: Mapped[int] = mapped_column(Integer, default=0)     # 夸克 App 分享页读到的保存人数(人工回填)
    saves_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 最近一次回填时间
    platforms: Mapped[str] = mapped_column(String(64), default="")  # 热点来源平台(douyin/weibo/baidu,+号连接);共振热点=多平台同现
    category: Mapped[str] = mapped_column(String(16), default="")   # 验证品类(资料/影视/漫剧/问卷/大瓜/软件);结算归因按它聚合"哪类真赚"(2026-10-01)
    opportunity: Mapped[float] = mapped_column(Float, default=0)  # 机会分=需求×竞争稀疏度×窗口因子;排序与取舍依据(2026-09-29 v5)
    acted: Mapped[bool] = mapped_column(Boolean, default=False)   # 已发货标记(下注):只有 acted 的建议才构成"预测→结算"学习样本
    acted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 标记已发时间
    article_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 结算归因的发文(按盘链精确匹配)
    reads_gain: Mapped[int] = mapped_column(Integer, default=0)   # 阅读数×30%**预估拉新量**(2026-10-04 复活:
                                                                  # 微信读书列表接口带精确 readNum;需求侧预估,非实收)
    repost_gain: Mapped[int] = mapped_column(Integer, default=0)  # 盘链扩散增量:发文后全网新增的该文盘链记录数(免费自动,2026-09-29 起=结算主信号)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 最近一次结算时间
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PanRecruitWeekly(Base):
    """夸克官方拉新后台的周度拉新总数(人工录入,总账校准用)。

    链接级转存统计夸克不开放(2026-09-29 确认),逐条建议走发文阅读增量结算;
    本表是人工粗颗粒兜底:每周从拉新活动后台抄一次总数,与建议侧信号对账。
    """

    __tablename__ = "pan_recruit_weekly"
    __table_args__ = (UniqueConstraint("user_id", "week_start", name="uq_prw_user_week"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    week_start: Mapped[datetime] = mapped_column(DateTime)   # 统计周期起始日(默认周一)
    recruits: Mapped[int] = mapped_column(Integer, default=0)
    # **分渠道明细**(2026-10-03):JSON 如 `{"douyin": 42, "wechat": 18}`。
    # 用户口径:"我只能给你我的"且要**分渠道**给 —— 只有分开录,才能分别对账两个渠道
    # (总账一个数没法回答"哪条链在起作用")。用 JSON 列而不是给唯一键加 channel:
    # 加 channel 要重建 `(user_id, week_start)` 唯一约束(SQLite 得整表重建),
    # 而每周每用户本来就只有一行,嵌套进去最省事。
    channels: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DouyinLead(Base):
    """抖音推广线索(2026-10-03):标题带《口令》的推广视频 → 落库,供**结算归因**。

    **为什么必须落库**:结算要用**转发量**当线索级的强弱代理(链接级真实转存数在夸克侧
    不可得,2026-09-29 定案),而 `share_count` 只在**抓取那一次**有效 —— 不存下来,
    一周后就再也算不出"本周发现的线索总量级"了。

    ⚠️ **口径要说清**(免得自己骗自己):`share_count` 是**别人视频**的转发量,它衡量的是
    **"这个资源在抖音有多热"**,不等于**我们自己发文带来的转化**。所以它只用于
    ① 线索强弱排序、② 与人工周录的真值做**趋势对账**;在拿到几周真实偏差之前,
    **不用它自动调权重**(见 `lead_settlement` 的说明)。

    `aweme_id` 当去重键:同一个视频会被多个搜索词命中,也只该算一次。
    """

    __tablename__ = "douyin_leads"
    __table_args__ = (UniqueConstraint("user_id", "aweme_id", name="uq_douyin_lead"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    aweme_id: Mapped[str] = mapped_column(String(64), index=True)   # 抖音视频 id(去重键)
    mark: Mapped[str] = mapped_column(String(64), default="")       # 《》里的口令
    title: Mapped[str] = mapped_column(String(255), default="")
    author: Mapped[str] = mapped_column(String(64), default="")     # 工具脱敏过的账号名
    url: Mapped[str] = mapped_column(String(500), default="")
    keyword: Mapped[str] = mapped_column(String(64), default="")    # 搜哪个词搜出来的
    share_count: Mapped[int] = mapped_column(Integer, default=0)    # **转发量**(转化代理)
    kind: Mapped[str] = mapped_column(String(16), default="")       # 口令解析结果(share/group/none/error)
    # **这条线索搬成了哪条链**(2026-10-04 补,计划里第 12 项):
    # ⚠️ 原来**落库时丢了** —— 于是"这个口令到底搬没搬成、搬成了哪条链"**事后查不出来**,
    # 只能去翻当时的飞书卡片。当天整理本轮线索时正是卡在这里(要为卡片做"条数/资源身份"
    # 都拿不到链)。`share` 类型成功时填**我方分享链**;群口令填群号不填这里。
    our_url: Mapped[str] = mapped_column(String(500), default="")
    found_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    found_date: Mapped[str] = mapped_column(String(16), index=True, default="")  # YYYY-MM-DD
    # 夸克口令那条链**是否已经试过**(2026-10-06):失败也要落痕 ——
    # 否则每轮都会拿同一批"App 匹配不了"的迅雷型线索去烧模拟器时间。
    kouling_tried_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # **首次搬成**的时刻(2026-10-07)。与 `our_url` 配套,但**不是同一件事**:
    # `our_url` 只回答"搬成了哪条链",回答不了"**从看到到搬成花了多久**"——
    # 而后者才是"够不够及时"的真指标(抖音最先看到 80 小时,如果搬成又要 3 天,那提前量是白给的)。
    # ⚠️ **存量行为 NULL** —— 那批当时没记,补不出来(拿 found_at 顶 = 编数据)。
    # 读取端必须把 NULL 当"**没记**"而不是"没搬成"(见 chain_delivery 的 `no_ts` 计数)。
    moved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # **帖子发布时间**(2026-10-06):新鲜度的**唯一真依据**。
    # 没有它时只能拿 `found_at`(我们发现的时刻)当新鲜度 —— 那是**假的新鲜度**:
    # 老帖被推广号反复推时照样"刚发现",于是搬回来的大半是已有的老资源。
    publish_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QuarkShareStat(Base):
    """夸克「我的分享」统计快照(share/update_list 接口采集)。

    每条分享链接一行(user_id+share_id 唯一),采集时覆盖更新统计字段,
    captured_at 记录最近一次采集时间。

    ⚠️ **本表当前处于休眠(2026-10-03)**:原写"保存数喂给拉新效果回填闭环",
    但采集口 `POST /api/quark/shares/collect` **零调用方**(前端无方法、调度器无作业),
    生产库最后一行停在 **2026-09-29 13:01**;结算早已改道 `repost_gain`(盘链扩散)
    + 方案B 人工周录,**不再读这张表**。路由与 `app/api/quark.py` 已删除
    (2026-10-03 用户决定"删端点、保留服务与只读探测脚本")。
    **表保留**:删表要走迁移、且这是夸克唯一一份链接级快照数据,留着零成本。
    复现采集:见 `app/services/quark_share_stats.py` 的模块说明与
    `scripts/probe_quark_share_stats.py`。
    """

    __tablename__ = "quark_share_stats"
    __table_args__ = (UniqueConstraint("user_id", "share_id", name="uq_qss_user_share"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    share_id: Mapped[str] = mapped_column(String(64), index=True)
    pwd_id: Mapped[str] = mapped_column(String(32), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    share_url: Mapped[str] = mapped_column(String(255), default="")
    save_pv: Mapped[int] = mapped_column(Integer, default=0)        # 保存次数(-1=平台未给出)
    click_pv: Mapped[int] = mapped_column(Integer, default=0)       # 浏览次数(-1=平台未给出)
    download_pv: Mapped[int] = mapped_column(Integer, default=0)    # 下载次数
    visit_user_count: Mapped[int] = mapped_column(Integer, default=0)  # 访问人数
    file_num: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[int] = mapped_column(Integer, default=0)
    audit_status: Mapped[int] = mapped_column(Integer, default=0)
    path_info: Mapped[str] = mapped_column(String(255), default="")   # 分享源目录
    share_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    share_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class WechatTrafficSample(Base):
    """公众号文章流量采样点(构成单篇流量增长曲线)。

    ⚠️ **已废弃(2026-09-29),别再往这张表写**(2026-10-05 明确标注)。
    它的唯一数据源 `dajiala` 付费阅读采样当天被用户决策放弃,`traffic_tick` 随之停用,
    读它的 API(`GET /api/wechat/articles/{id}/traffic`)**2026-10-03 也已删除**。

    **实测状态(2026-10-05)**:全库 **0 行**,且**全项目无任何写入点**
    ⇒ 它是一条"只摘了一半"的链:源没了、接口没了,**model 与保留策略还在**。

    ⚠️ **为什么不直接删表**:删表是**不可逆**的(万一历史行还要查),而收益只是整洁。
    本仓的处置约定是"**先标记废弃、再迁移**",不是随手 drop。
    真要清理时,按顺序动这五处(改之前先 `grep` 确认没有新的写入点):
      `app/db/models.py`(本类)→ `app/db/maintenance.py`(保留策略那一行)→
      `app/services/wechat/_source.py`(删文章时顺带删采样,唯一"使用"点)→
      `app/services/wechat_monitor.py`(门面 re-export)→ `tests/test_wechat_monitor.py`。
    """

    __tablename__ = "wechat_traffic_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("wechat_articles.id"), index=True)
    read_num: Mapped[int] = mapped_column(Integer, default=0)
    zan_num: Mapped[int] = mapped_column(Integer, default=0)
    looking_num: Mapped[int] = mapped_column(Integer, default=0)
    share_num: Mapped[int] = mapped_column(Integer, default=0)
    collect_num: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    sampled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class WechatCandidate(Base):
    """候选对标号(自动发现):搜狗按关键词搜到的同类公众号,待人工确认。

    手机微信读书关注该号 → 书架导入 → 成为正式对标号;`status`:
    new=待处理 / dismissed=已忽略(不再推送)。已收录的候选按昵称与
    wechat_benchmarks 匹配,列表标注"已收录"。
    """

    __tablename__ = "wechat_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")                # 公众号名
    title: Mapped[str] = mapped_column(String(500), default="")               # 代表文章标题
    url: Mapped[str] = mapped_column(String(600), default="")                 # 代表文章链接(收录用,v2.6.0)
    title_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 代表文章发布时间
    term: Mapped[str] = mapped_column(String(64), default="")                 # 命中的搜索词
    status: Mapped[str] = mapped_column(String(16), default="new")            # new / dismissed / imported
    import_tries: Mapped[int] = mapped_column(Integer, default=0)             # 自动收录尝试次数(v2.13.0)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class CrossPlatformAccount(Base):
    """跨平台同类资源号(2026-10-01):拿公众号的资源关键词反查其他平台发现的账号。

    与 `WechatBenchmark`(公众号对标号)是同一思路的**跨平台版**——那边在微信生态内用搜狗
    找同类号,这边拿**具体的资源关键词**去知乎/B站等平台搜,并且**只收录内容里真含网盘链
    的账号**(用户口径:"确认其内容,如果确认是推广网盘的就设置成对标账号添加进去")。

    `uid` 是平台内账号 ID(知乎 url_token / B站 mid),与 platform 组成唯一键;
    `status`: active=纳入监控 / dismissed=人工忽略。
    """

    __tablename__ = "cross_platform_accounts"
    __table_args__ = (UniqueConstraint("user_id", "platform", "uid", name="uq_cross_acct"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(16), index=True)      # zhihu / bilibili …
    uid: Mapped[str] = mapped_column(String(64), default="")           # 平台内账号 ID
    name: Mapped[str] = mapped_column(String(128), default="")
    url: Mapped[str] = mapped_column(String(500), default="")          # 账号主页/代表内容
    hit_keyword: Mapped[str] = mapped_column(String(128), default="")  # 由哪个资源词发现
    snippet: Mapped[str] = mapped_column(String(255), default="")      # 代表内容摘要
    pan_link: Mapped[str] = mapped_column(String(500), default="")     # 内容里检出的网盘链
    status: Mapped[str] = mapped_column(String(16), default="active")
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # **扫描状态**(2026-10-05):`last_scan_at` 让轮转"先扫没扫过的"、并把**已知空壳**排最后;
    # `video_count` 是"59 个号里有多少空壳"唯一能量出来的来源(space 端点限流紧,不可能专门扫一圈统计)。
    # ⚠️ `-1` = 还没扫过(别用 0 当"未知":0 是"扫过且确认没投稿",语义完全不同)。
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ⚠️ **必须带 `server_default`**:`remote_sync._push` 是**裸 INSERT**、不带这一列,
    # 而 `default=-1` 只是 **Python 侧**默认值 —— 全新库走 `create_all` 时模型会建出
    # `NOT NULL` 且无 DB 默认的列 ⇒ 裸 INSERT 直接 `IntegrityError`
    # (2026-10-05 被既有测试当场抓到;`ADDITIONS` 迁移那边本来就有 `DEFAULT -1`,两边要对齐)。
    video_count: Mapped[int] = mapped_column(Integer, default=-1, server_default=text("-1"))


class WechatBenchmark(Base):
    """对标公众号:监听(新文检测)与同步(全量文章)的目标账号。

    `anchor_url` 存该号任意一篇**永久**文章长链:post_condition/历史接口认链接不认名字,
    贴一条链接即可当账号锚点,加号动作本身不产生 API 调用。`ghid` 由接口返回后回填,
    之后同步/监听优先用 ghid,锚点仅作兜底。
    """

    __tablename__ = "wechat_benchmarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    nickname: Mapped[str] = mapped_column(String(128), default="")
    ghid: Mapped[str] = mapped_column(String(64), default="")
    weread_book_id: Mapped[str] = mapped_column(String(64), default="")  # 微信读书 bookId(MP_WXS_*),免费数据源
    biz: Mapped[str] = mapped_column(String(64), default="")             # 公众号 __biz(读书平台 mp_id,免费全量列表)
    anchor_url: Mapped[str] = mapped_column(String(500), default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    miss_count: Mapped[int] = mapped_column(Integer, default=0)   # 连续"当天没有发文"次数(≥7 视为沉睡)
    last_item_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class WechatPanLink(Base):
    """文章盘链归一化表:每 (文章, 分享链) 一行,资源共振查询走索引(替代 LIKE 全表扫)。"""

    __tablename__ = "wechat_pan_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("wechat_articles.id"), index=True)
    pan_url: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class XunleiResource(Base):
    """迅雷网盘资源(2026-10-02):扫盘登记 + 自动生成我方分享链。

    **为什么另开一张表**:公众号来源的盘链存在 `WechatPanLink`,而它的 `article_id` 是
    **外键**(每条链必须挂在某篇文章下);迅雷这批资源是"用户在 App 里转存进来的",
    **没有对应文章**,硬塞会破坏约束。资源库查询时把两边合并展示(带来源标签)。

    `fid` 是迅雷侧的文件/文件夹 id,当去重键 —— 同一个资源重复扫到不会重复入库。
    """

    __tablename__ = "xunlei_resources"
    __table_args__ = (UniqueConstraint("user_id", "fid", name="uq_xunlei_fid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    fid: Mapped[str] = mapped_column(String(64), index=True)          # 迅雷文件 id
    name: Mapped[str] = mapped_column(String(255), default="")        # 转存进来的名字
    kind: Mapped[str] = mapped_column(String(16), default="")         # file / folder
    size: Mapped[str] = mapped_column(String(32), default="")         # 字节数(文件夹为 0)
    parent_name: Mapped[str] = mapped_column(String(128), default="")  # 所在目录名(看来源)
    share_url: Mapped[str] = mapped_column(String(500), default="")   # **我方**分享链
    pass_code: Mapped[str] = mapped_column(String(32), default="")    # 提取码
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class XunleiGroupShare(Base):
    """迅雷**群组**里的分享(2026-10-02):群消息流 → 限量转存 → 我方分享链。

    **为什么又开一张表**(与 `XunleiResource` 的区别):`XunleiResource` 记的是
    "**已经在我方盘里**"的资源(有 `fid`、能直接再分享);群里的分享**还没转存**,
    没有我方 fid,却多出"哪个群 / 谁发的 / 群主原链 / 什么时候发的"这些 `XunleiResource`
    没有的维度。转存成功后把结果**回填到本行**(`fid` + `our_url`),不再另插一条。

    `share_id` 是迅雷侧的分享 id,当去重键 —— 同一条分享会被"群文件库更新卡"重复播报,
    按 `share_id` 去重才不会重复转存。
    """

    __tablename__ = "xunlei_group_shares"
    __table_args__ = (UniqueConstraint("user_id", "share_id", name="uq_xunlei_group_share"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)      # 群号
    group_name: Mapped[str] = mapped_column(String(128), default="")   # 群名(展示用)
    message_id: Mapped[str] = mapped_column(String(32), default="")    # 群消息 id(溯源)
    share_id: Mapped[str] = mapped_column(String(64), index=True)      # 迅雷分享 id(去重键)
    origin_url: Mapped[str] = mapped_column(String(300), default="")   # 群主原链
    title: Mapped[str] = mapped_column(String(255), default="")        # 资源名
    sender: Mapped[str] = mapped_column(String(32), default="")        # 发送者 uid
    kind: Mapped[str] = mapped_column(String(16), default="")          # drive#folder / drive#file
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/ok/failed
    message: Mapped[str] = mapped_column(String(200), default="")      # 失败原因
    our_url: Mapped[str] = mapped_column(String(500), default="")      # **我方**分享链
    pass_code: Mapped[str] = mapped_column(String(32), default="")     # 我方提取码
    fid: Mapped[str] = mapped_column(String(64), default="")           # 我方盘文件 id
    msg_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 群消息时间
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # **首次搬成**的时刻(2026-10-07,与 `douyin_leads.moved_at` 同一件事、同一条纪律)。
    # ⚠️ **不能用 `synced_at` 顶**:那一列每轮采集都会刷新(最后一次写库的时刻),
    # 拿它减 `msg_time` 算"搬成耗时"会得出一个**随重跑次数变大**的假数。
    # 存量行同样是 NULL(没记过),读取端当"没记"不当"没搬成"。
    moved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DiscoveredPanLink(Base):
    """**公开平台**上发现的盘链(2026-10-02):知乎等平台的内容里**直接贴着的**别人的分享链。

    **为什么又开一张表**:`WechatPanLink` 的 `article_id` 是**外键**(链必须挂在公众号文章下),
    这里的链来自知乎回答,没有文章;**与 `XunleiResource` 也不同** —— 那张表记的是
    "已经在**迅雷**盘里"的资源,而这里抓到的是**夸克/百度**链,转存后落在**各自的盘**。

    `origin_url`(别人的原链)是去重键:同一条链被多个回答贴出只算一条。
    """

    __tablename__ = "discovered_pan_links"
    __table_args__ = (UniqueConstraint("user_id", "origin_url", name="uq_discovered_pan"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(16), default="")       # 哪个平台发现的(zhihu)
    origin_url: Mapped[str] = mapped_column(String(500), index=True)    # 别人的原链(去重键)
    title: Mapped[str] = mapped_column(String(255), default="")         # 内容标题
    author: Mapped[str] = mapped_column(String(64), default="")         # 作者名
    source_url: Mapped[str] = mapped_column(String(500), default="")    # 内容链接(知乎回答)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/ok/failed/skipped
    message: Mapped[str] = mapped_column(String(200), default="")
    our_url: Mapped[str] = mapped_column(String(500), default="")       # **我方**分享链
    pass_code: Mapped[str] = mapped_column(String(32), default="")
    found_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentStage(Base):
    """苗头 Agent 的关键词生命周期记忆(思维状态):苗头→上升→爆发→回落。"""

    __tablename__ = "agent_stages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    board: Mapped[str] = mapped_column(String(16))
    norm: Mapped[str] = mapped_column(String(128), index=True)
    kw: Mapped[str] = mapped_column(String(255), default="")
    stage: Mapped[str] = mapped_column(String(16), default="苗头")
    score: Mapped[int] = mapped_column(Integer, default=0)
    parts: Mapped[str] = mapped_column(String(255), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint("user_id", "board", "norm"),)


class WechatRewrite(Base):
    """AI 改写稿:对标文 → 原创可发布稿(持久化防丢失,支持多次改写对比)。"""

    __tablename__ = "wechat_rewrites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("wechat_articles.id"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text(), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AlertRule(Base):
    """用户自定义预警规则(每板块)。

    - rule_type=`threshold`:某指标(metric)超过 threshold 即预警;
    - rule_type=`new`:出现"新增"项(关键词/商品/词)即告知;
    - rule_type=`fixed_time`:按 alert_time(HH:MM)发送该板块总结。
    - keyword 非空时只对该关键词/项的变动预警;为空则监控全部。
    """

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    section: Mapped[str] = mapped_column(String(32))          # weibo/xianyu/douhot
    rule_type: Mapped[str] = mapped_column(String(16))         # threshold/new/fixed_time
    metric: Mapped[str | None] = mapped_column(String(32), nullable=True)  # growth/pct/delta/score/count
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    keyword: Mapped[str | None] = mapped_column(String(128), nullable=True)
    alert_time: Mapped[str | None] = mapped_column(String(8), nullable=True)  # HH:MM
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class LoginLog(Base):
    """登录日志(账号/IP/设备/时间)。"""

    __tablename__ = "login_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    ua: Mapped[str] = mapped_column(String(255), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AdminLog(Base):
    """操作日志(谁/何时/对什么/做了什么)。"""

    __tablename__ = "admin_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    admin_name: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(128))   # 如 toggle_user/delete_user/set_config
    target: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(Text(), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SystemConfig(Base):
    """系统设置(键值对,管理后台可改)。"""

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text(), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class JobHeartbeat(Base):
    """**每个调度作业的"上次真实执行"**(2026-10-04 加)。

    为什么需要 —— 作业的"执行事实"此前**没有统一落点**:
      · 有的作业把痕迹写进 `runs`,但用的是**另一个名字**(`wechat_collect_tick` → `wechat_listen`,
        `collect_tick` → `weibo/baidu/xianyu/douhot`);
      · 有的作业(`disk_guard`/`data_cleanup`/`push_timeline`/`cross_account_discover` …)**压根不写**。
    于是"配置里说每天跑"与"实际跑没跑"之间**没有任何可查的对照** —— `resource_presence`
    就这么潜伏着:注册着、trigger 正确、`enabled=True`,**六天一次没跑**,直到人工比对才撞见。

    本表由 `scheduler` **自动维护**,分两个时机、是一件事的两半:

      · **注册时** `_add_job` 落一行、写 `first_seen_at`(只写一次)⇒ **注册了就一定有心跳行**;
      · **执行后** `_safe` 包一层 `_beat` 更新 `last_run_at/run_count/...`。

    ⚠️ **2026-10-05 更正**:此前这里写着"由 `_add_job` 自动维护(每个作业执行后 upsert 一行),
    所以注册了就一定有心跳" —— **两句都不准**:upsert 实际发生在 `_safe` 的包装器里(不是 `_add_job`),
    而且**只在执行后** ⇒ **注册了但没跑过的作业根本没有行**。"注册了就一定有心跳"是假的,
    而 `job_liveness` 判断"没心跳"时正是被这句话带偏的。现在补上注册时那一半,它才成立。
    """

    __tablename__ = "job_heartbeats"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(255), default="")
    # **该作业第一次被注册的时刻**(2026-10-05 加)。由 `scheduler._add_job` 在登记时写。
    #
    # ⚠️ **为什么非要它**:对账脚本要判"没心跳 = 真漏跑 还是 还没到第一次执行点",
    # 而此前只能用**全表最早一条**当基线 —— 那是"心跳机制上线时刻",不是"这个作业的注册时刻"。
    # 于是刚加进来的作业(如 13:10 注册、每天 09:30 触发)会被误判成"注册了却从没执行过"。
    # 有了这一列,判据才是"自**它自己**注册起,本该触发过吗"。
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # **上一次执行耗时(毫秒)**(2026-10-07)。为什么要有它:排期错峰、判断"哪个作业占着单写者
    # 的锁最久"全靠它,而在此之前**全仓拿不到任何时长**(见 `RunRecord.finished_at` 的说明)。
    # 由 `scheduler._safe` 在作业外层计时 —— 一处覆盖全部作业,不用改几十个调用点。
    last_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class NameLexicon(Base):
    """**从数据里学出来的"人名/网红名"**(2026-10-04,用户口径)。

    用户原话:"大瓜一版标题上都会有明星或者公众人物网红的名字……大瓜也可以加一条
    **标题是否带人名**,如果带人名就可去判断一下"、"大瓜**慢慢的学习**可以"。

    ⚠️ **为什么是"学"而不是"写一张名单"**:明星/网红的名字**天天在变**,
    写死的名单必然过期(而且过期了没人知道)。所以按用户说的"慢慢学":
    凡是被判为**大瓜**的标题,把它去掉事件词后剩下的中文片段当**候选名**记一次;
    同一名字**攒够 N 次**才算数 —— 这样 `某明星` 这种泛称会因为"太泛"要么被停用词挡掉、
    要么永远达不到阈值,而真名字会自然浮上来。

    ⚠️ 它只是**弱信号**(在 `category_topics.classify` 里排在其他类目之后):
    有人名**不等于**是瓜 —— 用户也说"如果带人名就**可去判断一下**",是"去看看",不是"直接收"。
    """

    __tablename__ = "name_lexicon"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)


class GroupMember(Base):
    """付费群会员:按入群时间+周期自动生成续费提醒与超期踢人名单。

    状态机(每日 tick 自动流转,仅供展示;自动私信/踢人需接微信机器人,预留 status 字段):
    - active: 未到期
    - due:     到期后 24h 内(该私信收续费)
    - overdue: 到期超 24h(该踢出群聊)
    - renewed: 已续费(last_renewed_at 刷新后回到 active)
    - kicked / exempt: 人工标记
    """

    __tablename__ = "group_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)  # 归属平台账号
    group_name: Mapped[str] = mapped_column(String(128), default="")          # 哪个群
    nickname: Mapped[str] = mapped_column(String(128), default="")            # 群昵称
    wechat_id: Mapped[str] = mapped_column(String(128), default="")           # 微信号(可选,私信用)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    cycle_days: Mapped[int] = mapped_column(Integer, default=30)              # 续费周期(天)
    last_renewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")         # active/kicked/exempt
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class HotspotEvent(Base):
    """热点事件:跨平台/跨变体标题的同一真实事件归并后的上层实体。

    Hotspot(单平台单标题快照) → 归一化 + bigram 相似度聚类 → Event。
    事件层回答"这个事件现在什么阶段/在几个平台/峰值多少",是跨平台共振
    与生命周期分析的基础。归并算法纯本地(字符 bigram Jaccard,无外部依赖)。
    """

    __tablename__ = "hotspot_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    primary_title: Mapped[str] = mapped_column(String(255), default="")   # 首次出现的标题
    norm_title: Mapped[str] = mapped_column(String(255), index=True)      # 规范化主键词
    platforms: Mapped[str] = mapped_column(String(255), default="")       # JSON ["weibo","baidu",...]
    platform_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    peak_value: Mapped[float] = mapped_column(Float, default=0)           # 峰值热度
    peak_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)         # 归并的快照条数
    status: Mapped[str] = mapped_column(String(16), default="active")     # active/ended
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reappear_count: Mapped[int] = mapped_column(Integer, default=0)       # 复燃次数(ended 后再现)
    last_growth: Mapped[float | None] = mapped_column(Float, nullable=True)  # 最近一次样本增长率


class EventMembership(Base):
    """事件成员映射:(板块, 规范化标题) → 事件。不改五张平台老表结构。"""

    __tablename__ = "event_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    board: Mapped[str] = mapped_column(String(16))                        # weibo/xianyu/douhot/baidu
    norm_title: Mapped[str] = mapped_column(String(255))
    event_id: Mapped[int] = mapped_column(ForeignKey("hotspot_events.id"), index=True)
    latest_value: Mapped[float] = mapped_column(Float, default=0)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class NotificationLog(Base):
    """通知送达日志:关键告警(事件级 notify_incident)的投递记录与重试审计。"""

    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="feishu")  # feishu/email/webhook
    section: Mapped[str] = mapped_column(String(32), default="")        # 业务板块/事件类型
    title: Mapped[str] = mapped_column(String(255), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1)           # 重试次数
    error: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
