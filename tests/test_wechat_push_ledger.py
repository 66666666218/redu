# -*- coding: utf-8 -*-
"""公众号推送的**盘链闸**端到端单测(2026-11-19)。

链路:`_push_listen` 在 AI 筛选之后、渲染之前过 `push_ledger.drop_recent`。
本文件验四件事:
  ① 同一份资源(**我方链相同**)的第二篇文章,第二轮被挡;
  ② 没盘链的文章不受这道闸影响(行为与改动前一致);
  ③ 被挡的文章**当场盖 `pushed_at`**,`repush_unpushed` 不会把它捞回来;
  ④ 真涨了(阅读数 +30% 以上)的重复仍放行。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import json  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: F401,E402
from app.db.database import Base  # noqa: E402
from app.db.models import WechatArticle  # noqa: E402
from app.services import wechat_monitor  # noqa: E402
from app.services.wechat import _listen as L  # noqa: E402
from config.settings import Settings  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def _settings(**kw) -> Settings:
    base = {"wechat_sync_max_pages": 2,
            #: 自营号名单默认是「天一项目拆解」;这些用例的作者都是号A/号B,不受影响。
            "wechat_push_skip_own_pan": False}
    base.update(kw)
    return Settings(_env_file=None, is_dev=True, **base)


def _fake_feishu(monkeypatch, cards: list) -> None:
    import app.services.feishu as feishu_mod
    from app.services import feishu_client

    monkeypatch.setattr(feishu_mod, "webhook_for",
                        lambda settings, section: "https://open.feishu.cn/hook/x")

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            return True

        def send_card(self, card: dict) -> bool:
            cards.append(card)
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)


def _art(url: str, author: str, pan: str = "", my: str = "", read: int = 0,
         title: str = "资源文") -> WechatArticle:
    return WechatArticle(user_id=1, title=title, author=author, url=url,
                         source="listen", pan_urls=pan, my_pan_urls=my,
                         read_num=read)


def test_同一份资源的第二篇被挡且当场打戳(session, monkeypatch) -> None:
    """两篇**不同文章**、贴同一个我方链 ⇒ 第二轮不再推,且盖 `pushed_at`。

    ⚠️ 只盖"全被挡"那种情况是不够的:被挡的若不打戳,`repush_unpushed`
    下一轮会把它们当成"从没到过飞书"捞回来、再挡一遍 —— 永远补不完。
    """
    cards: list[dict] = []
    _fake_feishu(monkeypatch, cards)

    #: ⚠️ 标题必须**真的像同一份**(这条链上我方链那一档要过标题判据)——
    #: 用真实的两条同资源标题,而不是「A文/B文」这种测试造的占位串。
    a = _art("https://mp.weixin.qq.com/s/a", "号A",
             pan="https://pan.quark.cn/s/srcA",
             my="https://pan.quark.cn/s/SAME",
             title="危险人格测试入口链接（自取）")
    session.add(a)
    session.commit()
    assert L._push_listen(session, 1, _settings(), [a]) == 1

    #: 另一篇文章、**同一份资源**(我方链相同、源链不同、标题是同资源的另一种写法)
    b = _art("https://mp.weixin.qq.com/s/b", "号B",
             pan="https://pan.quark.cn/s/srcB",
             my="https://pan.quark.cn/s/SAME",
             title="危险人格测试 最新入口【链接自取】")
    session.add(b)
    session.commit()
    got = L._push_listen(session, 1, _settings(), [b])
    assert got == 0, "同一份资源 72h 内推过,第二篇该被挡"
    session.expire_all()
    assert b.pushed_at is not None, "被挡的也要打戳,否则每轮重捞"
    #: 补推不该把它翻出来(它已经"处理过"了)
    assert wechat_monitor.repush_unpushed(session, 1, _settings()) == 0


def test_同一批里的系列文一篇不落(session, monkeypatch) -> None:
    """★ 回归:批内**不许**互相挡。

    起因(2026-11-19 实测):`_enrich` 的「资源级复用」把「新发资源文0/1/2/3」
    判成同一份资源、都复用同一条我方链 ⇒ 批内闸一次吃掉 3 篇。
    而产品硬要求是「近 24h 发的文章一篇不落全推」,且这几篇本来就**各有各的源链**。
    """
    cards: list[dict] = []
    _fake_feishu(monkeypatch, cards)

    arts = [_art(f"https://mp.weixin.qq.com/s/s{n}", "号A",
                 pan=f"https://pan.quark.cn/s/fresh{n}",
                 my="https://pan.quark.cn/s/SAME",  # 资源级复用了同一条
                 title=f"新发资源文{n}") for n in range(4)]
    session.add_all(arts)
    session.commit()
    assert L._push_listen(session, 1, _settings(), arts) == 4
    blob = json.dumps(cards, ensure_ascii=False)
    assert all(f"新发资源文{n}" in blob for n in range(4))


def test_没盘链的文章不受盘链闸影响(session, monkeypatch) -> None:
    """`pan_key("")` 为空 ⇒ 这道闸根本不管它们(行为与改动前一致)。"""
    cards: list[dict] = []
    _fake_feishu(monkeypatch, cards)

    a = _art("https://mp.weixin.qq.com/s/n1", "号A", title="A文 没盘")
    b = _art("https://mp.weixin.qq.com/s/n2", "号B", title="B文 也没盘")
    session.add_all([a, b])
    session.commit()
    assert L._push_listen(session, 1, _settings(), [a]) == 1
    assert L._push_listen(session, 1, _settings(), [b]) == 1


def test_真的涨了阅读数的重复仍放行(session, monkeypatch) -> None:
    """`hot_rise_only` 的语义:重复但**真的火了**要推(且打 🔥 标记)。"""
    cards: list[dict] = []
    _fake_feishu(monkeypatch, cards)

    a = _art("https://mp.weixin.qq.com/s/h1", "号A",
             pan="https://pan.quark.cn/s/srcH",
             my="https://pan.quark.cn/s/HOT", read=5000, title="A文 首推")
    session.add(a)
    session.commit()
    assert L._push_listen(session, 1, _settings(), [a]) == 1

    #: 同一份资源、换篇文章,阅读 5000 → 20000(涨 4 倍)
    b = _art("https://mp.weixin.qq.com/s/h2", "号B",
             pan="https://pan.quark.cn/s/srcH2",
             my="https://pan.quark.cn/s/HOT", read=20000, title="B文 又火了")
    session.add(b)
    session.commit()
    assert L._push_listen(session, 1, _settings(), [b]) == 1
    blob = json.dumps(cards, ensure_ascii=False)
    assert "又火了" in blob