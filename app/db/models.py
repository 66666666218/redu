"""多租户 ORM 模型(数据均以 `user_id` 隔离)。

- `User` / `UserCookie`:用户与用户自行配置的各平台 Cookie。
- 监控数据表(WeiboHotItem / WeiboTrend / XianyuItem / XianyuSummary /
  DouhotWord / DouhotAlerted / AlertRecord / RunRecord)全部带 `user_id`。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class XianyuSummary(Base):
    __tablename__ = "xianyu_summary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    summary_date: Mapped[str] = mapped_column(String(16))
    summary_json: Mapped[str] = mapped_column(Text())
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


class WechatTrafficSample(Base):
    """公众号文章流量采样点(构成单篇流量增长曲线)。"""

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
