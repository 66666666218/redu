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
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    detail: Mapped[str] = mapped_column(Text(), default="")


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
    """热点宝关键词监控:用户自选榜类型 + 关键词。"""

    __tablename__ = "douhot_watch"
    __table_args__ = (UniqueConstraint("user_id", "list_type", "keyword", name="uq_watch"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    list_type: Mapped[str] = mapped_column(String(32))  # word(内容词)/search(搜索榜)
    keyword: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DouhotWatchSnap(Base):
    """关键词监控快照(每次采集的记录)。"""

    __tablename__ = "douhot_watch_snap"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    list_type: Mapped[str] = mapped_column(String(32))
    keyword: Mapped[str] = mapped_column(String(128), index=True)
    score: Mapped[float] = mapped_column(Float, default=0)   # 该榜中的得分
    rank_now: Mapped[int] = mapped_column(Integer, default=0)  # 当前排名(0=未上榜)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
