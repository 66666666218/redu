"""「资源推广热度榜」的口径守卫（`scripts/pan_promo_rank.py`）。

## 为什么值得钉

它回答的是「**哪些内容值得做成网盘推广**」—— 一个会被人**照着去搬资源**的清单。
三类错误都会直接造成动作错：

① **把套话当作品** ⇒ 榜单头部变成「25 人在推『链接自取可直接打开』」,
   照它去搬无从搬起;
② **把真作品当套话** ⇒ 静默丢掉真资源(实测 `七宗罪测试` 30 人、`犬系人格测试` 16 人
   都含动作词,一刀切会全部消失) —— 本仓「静默失败=假成功」那条;
③ **自有链只认夸克** ⇒ 我们**已经有**的迅雷链(群转存 ok 2182 条)会被算成
   「缺链可搬」,人一动手就撞自己已有的库。

⚠️ 计数必须是**不同作者**不是行数:同一资源被我们日结/补推 3 遍是 3 行、1 个人。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import sys  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pan_promo_rank import _is_cta, rank  # noqa: E402


@pytest.fixture()
def session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models as _models       # 建表要靠它注册模型
    from app.db.database import Base

    assert _models is not None
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    yield db
    db.close()


# ------------------------------------------------------------------ 套话滤网

@pytest.mark.parametrize("name", ["链接自取可直接打开", "入口直达", "资料分享", "2026最新"])
def test_纯套话被挡(name) -> None:
    assert _is_cta(name) is True


@pytest.mark.parametrize("name", ["七宗罪测试", "犬系人格测试", "奶娃冬季礼物获取",
                                  "顶级差距", "麦穗与羊", "后会友期人格测试"])
def test_含动作词但仍是真作品的名字不许被挡(name) -> None:
    """★ 这是上一版的真实错误:只要命中动作词就丢 ⇒ `七宗罪测试`(30 人)整条消失。"""
    assert _is_cta(name) is False


# ------------------------------------------------------------------ 计数口径

def _add(session, *, author, title, pan_url="https://pan.quark.cn/s/AAA", pan_id="",
         section="bilibili", when=None, pan_type="夸克"):
    from app.db.models import PushLedger

    session.add(PushLedger(user_id=1, section=section, item_key=f"{author}-{title}-{pan_url}",
                           title=title, pan_url=pan_url, pan_id=pan_id, pan_type=pan_type,
                           author=author, metrics="", pushed_at=when or datetime.now()))


def test_数人不数行_且同一个人的多行只算一个(session) -> None:
    """同一作者推 3 遍 = 1 个人;3 个作者各 1 遍 = 3 个人。"""
    for _ in range(3):
        _add(session, author="甲", title="资源《野火》")
    for who in ("乙", "丙"):
        _add(session, author=who, title="资源《野火》")
    session.commit()

    items, _stats = rank(session, days=30, min_hands=2)
    assert len(items) == 1, items
    assert items[0]["work"] == "野火"
    assert items[0]["hands"] == 3, "行数 5 但只有 3 个作者 —— 必须是 3"
    assert items[0]["rows"] == 5


def test_同一盘链挂两个作品要分桶(session) -> None:
    """一条链被复用挂不同资源 ⇒ 按链聚会把 A 的人算到 B 头上(`push_surge` 头注的老坑)。"""
    for who in ("甲", "乙"):
        _add(session, author=who, title="资源《作品A》")
    for who in ("丙", "丁", "戊"):
        _add(session, author=who, title="资源《作品B》")
    session.commit()

    items, _ = rank(session, days=30, min_hands=2)
    got = {x["work"]: x["hands"] for x in items}
    #: `resource_key` 会小写化(全仓判重同一口径),所以键是小写的
    assert got == {"作品a": 2, "作品b": 3}, got


def test_无作者的行不算推手但要在统计里(session) -> None:
    _add(session, author="", title="资源《野火》")
    _add(session, author="甲", title="资源《野火》")
    session.commit()

    items, stats = rank(session, days=30, min_hands=1)
    assert items[0]["hands"] == 1
    assert stats["no_author"] == 1, "静默丢行 = 假数据;必须记账"


# ------------------------------------------------------------------ 自有链

def _own_quark(session, url="https://pan.quark.cn/s/OURS"):
    from app.db.models import PanOwnedShare

    session.add(PanOwnedShare(user_id=1, platform="quark", account_key="quark",
                              pwd_id="OURS", share_id="s", url=url, title="我们自己传的"))


def _own_xunlei(session, url="https://pan.xunlei.com/s/OURSXL"):
    from app.db.models import XunleiGroupShare

    session.add(XunleiGroupShare(user_id=1, group_id="g", group_name="群", share_id="s",
                                 origin_url="https://pan.xunlei.com/s/SRC", title="源",
                                 status="ok", our_url=url, msg_time=datetime.now()))


def test_夸克自有链被认出来(session) -> None:
    _own_quark(session)
    for who in ("甲", "乙"):
        _add(session, author=who, title="资源《作品A》", pan_url="https://pan.quark.cn/s/OURS")
    session.commit()

    items, _ = rank(session, days=30, min_hands=2)
    assert items[0]["own"] is True
    assert items[0]["own_url"].endswith("/s/OURS")


def test_迅雷自有链也要认_不然会叫你去搬自己已经有的(session) -> None:
    """★ `pan_owned_shares` 只有夸克(6651);迅雷自有链在 `xunlei_group_shares.our_url`
    (status=ok, 2182 条)。只认夸克 ⇒ 实测 5 个桶被误报成「缺链可搬」。"""
    _own_xunlei(session)
    for who in ("甲", "乙"):
        _add(session, author=who, title="资源《作品A》",
             pan_url="https://pan.xunlei.com/s/OURSXL", pan_type="迅雷")
    session.commit()

    items, _ = rank(session, days=30, min_hands=2)
    assert items[0]["own"] is True, "迅雷自有链没被认出来 ⇒ 会让人重复搬"


def test_别人建的源链不算自有_而且要把链交出来(session) -> None:
    """没有自有链的作品 ⇒ `own=False`,并给出**能直接转存**的那条源链。"""
    for who in ("甲", "乙", "丙"):
        _add(session, author=who, title="资源《作品B》",
             pan_url="https://pan.xunlei.com/s/VP4OHe-g6K5?pwd=6att", pan_type="迅雷")
    session.commit()

    items, _ = rank(session, days=30, min_hands=2)
    assert items[0]["own"] is False
    assert items[0]["src_urls"] == {"迅雷": "https://pan.xunlei.com/s/VP4OHe-g6K5?pwd=6att"}


def test_只数窗口内的行(session) -> None:
    old = datetime.now() - timedelta(days=40)
    _add(session, author="甲", title="资源《老作品》", when=old)
    _add(session, author="乙", title="资源《老作品》", when=old)
    _add(session, author="丙", title="资源《新作品》")
    session.commit()

    items, _ = rank(session, days=30, min_hands=1)
    assert [x["work"] for x in items] == ["新作品"]