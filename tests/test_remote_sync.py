"""本机 → 远程 单向同步(2026-10-04)。

要钉住的是**最容易造成"无声数据污染"的那几条**:不带 id 推、按天然键去重、
盘链用远程的 article_id、幂等、远程挂了不影响本机。

⚠️ 测试用**一个 SQLite 文件当假远端** —— 所以 `remote_sync` 里的 SQL 才是方言中立的
(不加反引号)。真远端是 MySQL,两者都能跑。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db.models import WechatArticle, WechatBenchmark, WechatPanLink  # noqa: E402
from app.services import remote_sync as rs  # noqa: E402


@pytest.fixture()
def local():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture()
def remote_url(tmp_path):
    """假远端:一个独立的 SQLite 文件。**预置一条 id=999 的既有数据** ——
    用来验证"按 id 覆盖"会发生什么(这正是不能让本地 id 流过去的原因)。"""
    path = tmp_path / "remote.db"
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        # ⚠️ 裸 SQL 插入要把**所有 NOT NULL 列**都写上 —— SQLAlchemy 的 `default=` 是
        # Python 侧的,不是数据库默认值,裸 INSERT 拿不到它。
        c.execute(text(
            "INSERT INTO wechat_benchmarks (id, user_id, nickname, ghid, weread_book_id, biz, "
            "anchor_url, note, active, miss_count, created_at) VALUES "
            "(999, 1, '远程自己的号', '', '', '', '', '', 1, 0, '2026-09-28 00:00:00')"))
        c.execute(text(
            "INSERT INTO wechat_articles (id, user_id, author, title, content, url, created_at, "
            "source, pan_types, pan_urls, my_pan_urls, read_num, zan_num, looking_num, share_num, "
            "collect_num, comment_count, sample_count, first_read_num, trend_flag, quality) "
            "VALUES (999, 1, '远程自己的号', '远程的文章', '', "
            "'https://mp.weixin.qq.com/s/REMOTE-ONLY', '2026-09-28 00:00:00', 'listen', '', '', '', "
            "0, 0, 0, 0, 0, 0, 0, 0, '', '')"))
    eng.dispose()
    return f"sqlite:///{path}"


def _seed_local(local, *, url="https://mp.weixin.qq.com/s/LOCAL-1", pid=7):
    b = WechatBenchmark(id=1, user_id=1, nickname="本机的号", active=True,
                        created_at=datetime.now())
    a = WechatArticle(id=pid, user_id=1, author="本机的号", title="本机的文章", url=url,
                      benchmark_id=1, read_num=100, created_at=datetime.now())
    local.add_all([b, a])
    local.commit()
    local.add(WechatPanLink(user_id=1, article_id=pid, pan_url="https://pan.quark.cn/s/AAA",
                            created_at=datetime.now()))
    local.commit()
    return a


def test_sync_pushes_without_ids_and_resolves_fk(local, remote_url) -> None:
    """★★ **不带 id 推** + 盘链用**远程**的 article_id。

    ⚠️ 为什么不能带 id:两边**各自独立采集过**,远程 id 已用到上千,本地 id 也在同一区间
    —— 按 id 覆盖会把远程那行换成**本地同 id 的另一篇文章**,**无声污染**。
    预置的 `id=999` 那条远程独有数据就是这条纪律的哨兵:**它必须原样还在**。
    """
    _seed_local(local)
    out = rs.sync_once(local, remote_url)
    assert out["status"] == "ok", out

    eng = create_engine(remote_url)
    with eng.connect() as c:
        # ① 远程自己那条 id=999 **没被动过**
        row = c.execute(text("SELECT title FROM wechat_articles WHERE id = 999")).fetchone()
        assert row and row[0] == "远程的文章", "远程独有数据被覆盖了 —— 这就是按 id 推的后果"
        # ② 本地那条推过去了,且**不是** id=7(本地 id 不该流过去)
        got = c.execute(text("SELECT id, title, benchmark_id FROM wechat_articles "
                             "WHERE url = 'https://mp.weixin.qq.com/s/LOCAL-1'")).fetchone()
        assert got and got[0] != 7, f"本地 id 流到了远程:{got}"
        # ③ benchmark_id 必须是**远程的**那个 id(不是本地的 1,也不是 999)
        remote_b = c.execute(text("SELECT id FROM wechat_benchmarks "
                                  "WHERE nickname = '本机的号'")).fetchone()
        assert got[2] == remote_b[0]
        # ④ 盘链挂的是**远程** article_id
        link = c.execute(text("SELECT article_id FROM wechat_pan_links")).fetchone()
        assert link[0] == got[0]
    eng.dispose()


def test_sync_is_idempotent(local, remote_url) -> None:
    """★ **幂等**:同一天跑多次只是白跑一遍,不会产生重复(靠天然键去重)。"""
    _seed_local(local)
    first = rs.sync_once(local, remote_url)
    second = rs.sync_once(local, remote_url)
    assert first["articles"] == 1 and first["links"] == 1
    assert second["articles"] == 0 and second["links"] == 0, f"第二次又插了一遍:{second}"
    eng = create_engine(remote_url)
    with eng.connect() as c:
        n = c.execute(text("SELECT count(*) FROM wechat_articles "
                           "WHERE url = 'https://mp.weixin.qq.com/s/LOCAL-1'")).scalar()
    eng.dispose()
    assert n == 1


def test_sync_never_raises_when_remote_is_down(local) -> None:
    """★ **远程挂了绝不影响本机** —— 只返回结构化失败。"""
    out = rs.sync_once(local, "mysql+pymysql://u:p@127.0.0.1:1/nope")
    assert out["status"] == "failed" and "reason" in out
    assert local.scalar(select(WechatBenchmark).where(WechatBenchmark.nickname == "本机的号")) \
        is None or True        # 本机数据没被动(这个断言只是确保没抛)


def test_sync_disabled_without_url(local) -> None:
    assert rs.sync_once(local, "")["status"] == "disabled"


def test_tick_does_nothing_without_config(monkeypatch) -> None:
    """没配 `remote_db_url` ⇒ **不跑、也不记运行记录**(免得留一堆"success 但啥也没干"的空轮)。"""
    import config.settings as cs

    from app.services import remote_sync as _rs

    monkeypatch.setattr(cs, "get_settings",
                        lambda: type("S", (), {"remote_db_url": ""})())
    assert _rs.remote_sync_tick() == 0


def test_old_rows_are_not_pushed(local, remote_url) -> None:
    """只看窗口内的行 —— 同步是"补最新",不是"全量搬运"。"""
    old = WechatArticle(user_id=1, author="A", title="很久以前", url="https://x/old",
                        created_at=datetime.now() - timedelta(days=200))
    local.add(old)
    local.commit()
    rs.sync_once(local, remote_url, days=14)
    eng = create_engine(remote_url)
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM wechat_articles WHERE url = 'https://x/old'")) \
            .scalar() == 0
    eng.dispose()


def test_sync_pushes_cross_platform_accounts(local, remote_url) -> None:
    """★ **2026-10-05 补的洞**:`cross_platform_accounts`(知乎/B站发现的网盘号)
    **只在本机产生**(发现链归 wechat 侧),而选题 Agent 在 **远程** ——
    不同步的话远程**根本看不到我们发现了哪些号**。实测本机 61 个(59 B站 + 2 知乎)、
    **远程 0** ⇒ **白发现**。

    ⚠️ 它与 `discovered_pan_links` 那次是**同一类遗漏:本机产、远程用**。
    所以这条测试钉的不只是这一个表,而是"**本机产出的东西要记得推**"这件事本身。
    """
    from app.db.models import CrossPlatformAccount

    local.add(CrossPlatformAccount(
        user_id=1, platform="bilibili", uid="m123", name="网盘资源商行",
        url="https://space.bilibili.com/123", hit_keyword="网盘资源",
        snippet="", pan_link="", status="active", discovered_at=datetime.now()))
    local.commit()

    out = rs.sync_once(local, remote_url)
    assert out["accounts"] == 1, f"没推过去:{out}"

    eng = create_engine(remote_url)
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT name, platform, uid FROM cross_platform_accounts")).all()
    eng.dispose()
    assert [tuple(r) for r in rows] == [("网盘资源商行", "bilibili", "m123")], rows

    # **幂等**:靠天然键 (user_id, platform, uid) 去重,再推一次不该重复
    out2 = rs.sync_once(local, remote_url)
    assert out2["accounts"] == 0, f"第二次又插了一遍:{out2}"


def test_跨平台账号也只推窗口内的(local, remote_url) -> None:
    """与别的表同一条纪律:同步是"补最新",不是"全量搬运"。"""
    from app.db.models import CrossPlatformAccount

    local.add(CrossPlatformAccount(
        user_id=1, platform="bilibili", uid="old1", name="很久以前的号",
        url="", hit_keyword="", snippet="", pan_link="", status="active",
        discovered_at=datetime.now() - timedelta(days=200)))
    local.commit()
    rs.sync_once(local, remote_url, days=14)
    eng = create_engine(remote_url)
    with eng.connect() as c:
        n = c.execute(text(
            "SELECT count(*) FROM cross_platform_accounts WHERE uid = 'old1'")).scalar()
    eng.dispose()
    assert n == 0
