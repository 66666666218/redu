"""建表与轻量迁移(2026-10-05)。

⚠️ 这个文件的起因是一次**生产事故**:远程库的 schema **静默冻住了几天** ——
`_migrate()` 原来所有列共用一个事务,**一列失败就整段抛出**,它**后面所有列全没加上**,
而只留一条启动期 ERROR 日志。远程的选题 Agent 于是每次全字段 SELECT 都
`Unknown column 'draft'`。
"""
import pytest


class TestColdefForMysql:
    """**MySQL 不允许 TEXT/BLOB/JSON 列带 DEFAULT**(报 1101),SQLite 允许 ——
    所以 `channels TEXT DEFAULT ''` 那条**在远程库上必失败**,并把后面的列全挡掉。
    """

    def test_text_带默认值要被摘掉并标记补空串(self) -> None:
        from app.db.database import _coldef_for_mysql

        ddl, backfill = _coldef_for_mysql("channels TEXT DEFAULT ''")
        assert ddl == "channels TEXT", f"默认值必须摘掉(MySQL 不认),实际 {ddl!r}"
        assert backfill is True, "摘了默认值就得靠 UPDATE 把存量行补成空串,语义才等价"

    def test_非_TEXT_原样保留(self) -> None:
        """VARCHAR/INTEGER/DATETIME **能**带默认值,别乱改(改了语义就变了)。"""
        from app.db.database import _coldef_for_mysql

        for c in ("our_url VARCHAR(500) DEFAULT ''", "saves INTEGER DEFAULT 0",
                  "saves_at DATETIME", "role VARCHAR(16) DEFAULT 'user'"):
            ddl, bf = _coldef_for_mysql(c)
            assert ddl == c and bf is False, f"{c} 不该被改,实际 {ddl!r}/{bf}"

    def test_类型是第二个词_不是第一个(self) -> None:
        """⚠️ **我第一版就栽在这**:`coldef` 是 `列名 类型 ...`,**第一个词是列名**。
        按第一个词判的话全部原样返回、等于没改 —— 而且测试不打出来根本看不见。
        """
        from app.db.database import _coldef_for_mysql

        assert _coldef_for_mysql("channels TEXT DEFAULT ''")[1] is True
        assert _coldef_for_mysql("name TEXT DEFAULT ''")[1] is True, \
            "列名恰好也叫 text 之类的也不行"

    def test_带长度的类型也认得出(self) -> None:
        from app.db.database import _coldef_for_mysql

        # 万一将来写成 `MEDIUMTEXT(100)`,括号要去掉再比
        assert _coldef_for_mysql("x MEDIUMTEXT(100) DEFAULT ''")[1] is True


class TestMigrateRuns:
    """⚠️ **必须有这一条**:上面那些测的是"表里的写法对不对",**没有任何一条真的跑过
    `_migrate()`** —— 而我把迁移表提到模块级时漏改了循环里的一处引用
    (`additions.items()` → `ADDITIONS.items()`),**pyflakes 报了 undefined name、
    测试却全绿**。这类"测了个寂寞"正是本仓反复踩的坑。
    """

    def test_migrate_能真跑通(self, tmp_path, monkeypatch) -> None:
        from sqlalchemy import create_engine

        import app.db.database as dbmod
        from app.db.models import Base

        eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
        Base.metadata.create_all(eng)
        monkeypatch.setattr(dbmod, "get_engine", lambda: eng)
        dbmod._migrate()          # 不抛就算过;抛了说明引用/语法坏了
        eng.dispose()

    def test_缺列能被补上(self, tmp_path, monkeypatch) -> None:
        """反向:先建一张**缺列**的表,跑完 `_migrate` 该列要出现。"""
        from sqlalchemy import create_engine, inspect, text

        import app.db.database as dbmod

        eng = create_engine(f"sqlite:///{tmp_path / 'm2.db'}")
        with eng.begin() as c:
            c.execute(text("CREATE TABLE hotspot_suggestions (id INTEGER PRIMARY KEY, user_id INTEGER)"))
        monkeypatch.setattr(dbmod, "get_engine", lambda: eng)
        dbmod._migrate()
        cols = {c["name"] for c in inspect(eng).get_columns("hotspot_suggestions")}
        assert "draft" in cols, f"缺列没被补上:{sorted(cols)[:8]}"
        eng.dispose()

    """★ **对着真实迁移表跑一遍** —— 防"以后又加一条 MySQL 吃不下的写法"。"""

class TestAdditionsAreMysqlSafe:
    """★ **对着真实迁移表跑一遍** —— 防"以后又加一条 MySQL 吃不下的写法"。"""

    def test_迁移表里没有_Mysql_不兼容的_TEXT_默认值(self) -> None:
        from app.db.database import ADDITIONS, _coldef_for_mysql

        bad = []
        for table, coldefs in ADDITIONS.items():
            for c in coldefs:
                ddl, _ = _coldef_for_mysql(c)
                parts = ddl.split()
                typ = parts[1].upper().split("(")[0] if len(parts) > 1 else ""
                if typ in ("TEXT", "LONGTEXT", "MEDIUMTEXT", "TINYTEXT", "BLOB", "JSON") \
                        and " DEFAULT " in ddl:
                    bad.append(f"{table}.{c}")
        assert not bad, (
            "这些列定义 MySQL 会报 1101 并**把它后面的列全挡掉** —— "
            "`_coldef_for_mysql` 应该已经处理,漏网说明它没覆盖到:" + str(bad))

    def test_迁移表非空_且含远程曾经缺的那两列(self) -> None:
        """`draft` 与 `channels` 是 2026-10-05 远程冻库那次**真的缺过**的两列 ——
        它们必须在表里(否则下次部署又补不上)。"""
        from app.db.database import ADDITIONS

        assert any("draft" in c for c in ADDITIONS["hotspot_suggestions"]), "draft 没了"
        assert any("channels" in c for c in ADDITIONS["pan_recruit_weekly"]), "channels 没了"
