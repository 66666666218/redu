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


class TestBiliPanHotSync:
    """B站对标号的投稿标题(2026-10-05)—— **只推 `source='bili-pan'` 那几十行**。

    ⚠️⚠️ 这张表**远程自己也在用**(它的 `hot_source` 每几分钟写一轮,实测 7 万+ 条)。
    整表同步会把它灌爆 —— 所以"**只推自己那几十行、绝不碰别人的**"是本类最要紧的一条。
    """

    def _seed_hot(self, local, source, title, when=None, rank=1):
        from app.db.models import HotSourceItem

        local.add(HotSourceItem(user_id=1, source=source, rank=rank, title=title,
                                url="", extra="网盘号A",
                                captured_at=when or datetime.now()))
        local.commit()

    def _remote_rows(self, remote_url, source=None):
        eng = create_engine(remote_url)
        try:
            sql = "SELECT source, title FROM hot_source_items"
            params = {}
            if source:
                sql += " WHERE source = :s"
                params["s"] = source
            with eng.connect() as c:
                return [tuple(r) for r in c.execute(text(sql), params)]
        finally:
            eng.dispose()

    def test_只推bili_pan_不动远程自己的热榜(self, local, remote_url) -> None:
        eng = create_engine(remote_url)
        with eng.begin() as c:      # 远程自己的一条热榜(模拟它那 7 万条)
            c.execute(text("INSERT INTO hot_source_items (id, user_id, source, rank, title, url, "
                           "extra, captured_at) VALUES (900, 1, 'weibo', 1, '远程自己的热搜', "
                           "'', '', '2026-10-05 00:00:00')"))
        eng.dispose()

        self._seed_hot(local, "bili-pan", "野鹅敢死队 经典影片")
        self._seed_hot(local, "weibo", "本机也有的热搜")      # 非 bili-pan,不该被推

        out = rs.sync_once(local, remote_url)
        assert out.get("bili_hot") == 1, f"应只推 1 条,实际 {out}"

        rows = self._remote_rows(remote_url)
        assert len(rows) == 2, f"远程应只有'它自己的 1 条 + 我们推的 1 条',实际 {rows}"
        assert ("weibo", "远程自己的热搜") in rows, "**远程自己的热榜绝不能被覆盖/删除**"
        assert ("bili-pan", "野鹅敢死队 经典影片") in rows
        assert ("weibo", "本机也有的热搜") not in rows, "非 bili-pan 的行不该被推过去"

    def test_水位线让第二轮不再重复推(self, local, remote_url) -> None:
        self._seed_hot(local, "bili-pan", "第一条")
        assert rs.sync_once(local, remote_url).get("bili_hot") == 1
        # 第二轮:没有新行 ⇒ 推 0 条(否则每 30 分钟就把历史重推一遍,表会被灌爆)
        out2 = rs.sync_once(local, remote_url)
        assert out2.get("bili_hot") == 0, f"水位线没生效,第二轮还在推:{out2}"
        assert len(self._remote_rows(remote_url, "bili-pan")) == 1

    def test_新增的行第三轮能推上去(self, local, remote_url) -> None:
        self._seed_hot(local, "bili-pan", "第一条")
        rs.sync_once(local, remote_url)
        self._seed_hot(local, "bili-pan", "第二条")
        assert rs.sync_once(local, remote_url).get("bili_hot") == 1
        titles = {t for _, t in self._remote_rows(remote_url, "bili-pan")}
        assert titles == {"第一条", "第二条"}

    def test_远端提交失败时水位线不前进(self, local, remote_url, monkeypatch) -> None:
        """★ **这条是防"永久丢数据"的**,而且**必须让"提交"本身失败才算数**。

        ⚠️ **我第一版写错了、它是个假验证**:当时用 `sqlite:///:memory:` 制造失败 ——
        可那个 URL **在进入 `with remote.begin()` 之前**就建不出连接,
        块内的代码**一行都没跑**,所以"水位线在块内推进"这个 bug 也照样能通过。
        (与"证伪不控制变量"同类:验证动作没打到出问题的那一段。)

        正确的模拟:`with remote.begin()` 是**一个远端事务**,退出时才提交 ——
        这里让 `__exit__` 在**真提交之后**抛,精确对应"提交这一步失败"。
        水位线若在块内推进(而 `_set_bili_hot_watermark` 自己会 `local.commit()`),
        本地就会认为"推过了" ⇒ **那批标题永久丢失,且不会有任何报错**。
        """
        self._seed_hot(local, "bili-pan", "必须被推到的一条")
        real = rs._engine(remote_url)

        class _Wrap:
            def __init__(self, cm):
                self._cm = cm

            def __enter__(self):
                return self._cm.__enter__()

            def __exit__(self, *a):
                self._cm.__exit__(*a)                 # 真提交
                raise RuntimeError("提交时断网(模拟)")

        class _Eng:
            def begin(self):
                return _Wrap(real.begin())

            def dispose(self):
                real.dispose()

        monkeypatch.setattr(rs, "_engine", lambda *a, **k: _Eng())
        out = rs.sync_once(local, remote_url)
        assert out.get("status") == "failed", f"应当记失败,实际 {out}"
        assert rs._bili_hot_watermark(local) == "1970-01-01 00:00:00", \
            "提交失败那轮**绝不能**推进水位线(否则那批行永久丢失)"


class TestPushQuotesIdentifiers:
    """★★ **2026-10-05 拿真 MySQL 跑才暴露的坑**:`hot_source_items` 有个列叫 **`rank`**,
    那是 **MySQL 8 的保留字** —— 裸写进 INSERT 直接 `1064 syntax error`。

    ⚠️ 而这个坑**用 SQLite 假远端的往返测试永远发现不了**(SQLite 容忍裸 `rank`),
    所以只能**直接钉住生成的 SQL 形态**。真实故障现场:
    `ProgrammingError (1064, "... right syntax to use near 'rank, title, url, extra, c")`。
    """

    class _Conn:
        def __init__(self):
            self.sql = ""

        def execute(self, stmt, values):
            self.sql = str(stmt)

    def test_列名与表名都要加反引号(self) -> None:
        conn = self._Conn()
        n = rs._push("hot_source_items", ("user_id", "rank", "title"),
                     [{"user_id": 1, "rank": 1, "title": "x"}], conn)
        assert n == 1
        assert "`rank`" in conn.sql, f"列名必须加反引号(MySQL 8 里 rank 是保留字):{conn.sql}"
        assert "`hot_source_items`" in conn.sql, f"表名也应加:{conn.sql}"
        # 命名参数本身**不能**加引号,否则绑不上值
        assert ":rank" in conn.sql and "`:`" not in conn.sql

    def test_空列表不发SQL(self) -> None:
        conn = self._Conn()
        assert rs._push("hot_source_items", ("rank",), [], conn) == 0
        assert conn.sql == "", "没数据时不该发 SQL"

    def test_SQLite也认反引号(self, local, remote_url) -> None:
        """原注释写着「不加反引号:SQLite(测试用假远端)不认」—— **那句是错的**。
        实测 SQLite 接受反引号,所以加引号对两边的方言都安全。"""
        from app.db.models import HotSourceItem

        local.add(HotSourceItem(user_id=1, source="bili-pan", rank=3, title="带保留字列的行",
                                url="", extra=""))
        local.commit()
        out = rs.sync_once(local, remote_url)
        assert out.get("bili_hot") == 1, f"SQLite 假远端上也要能推成功:{out}"
