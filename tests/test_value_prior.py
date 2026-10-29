# -*- coding: utf-8 -*-
"""历史数据先验单测(2026-10-29)。

用户口径:「可以根据之前的数据来进行策略决策」「现在的数据不够用吗」。
实测(近 30 天 1056 条带口令线索):**软件 92.5% / 问卷 90.9% / 资料 86.9% /
影视 56.5% / 小游戏 54.3%** 的线索引能搬成 —— 赛道差异远大于"同资源条数"。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from datetime import datetime  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: F401,E402
from app.db.database import Base  # noqa: E402
from app.db.models import DouyinLead, User  # noqa: E402
from app.services import value_prior as vp  # noqa: E402


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _clear_cache():
    vp._CACHE.clear()
    yield
    vp._CACHE.clear()


def test_niche_rates_from_history(session) -> None:
    rows = [
        # 软件:2 条都搬成
        DouyinLead(user_id=1, aweme_id="1", mark="PS安装包", title="PS软件安装包",
                   our_url="https://pan.xunlei.com/s/a", found_at=datetime.now()),
        DouyinLead(user_id=1, aweme_id="2", mark="PR插件", title="PR插件教程安装包",
                   our_url="https://pan.xunlei.com/s/b", found_at=datetime.now()),
        # 影视:2 条只成 1
        DouyinLead(user_id=1, aweme_id="3", mark="某短剧", title="短剧全集80集",
                   our_url="https://pan.xunlei.com/s/c", found_at=datetime.now()),
        DouyinLead(user_id=1, aweme_id="4", mark="另一短剧", title="短剧全集",
                   our_url="", found_at=datetime.now()),
    ]
    session.add_all(rows)
    session.commit()
    rates = vp.niche_rates(session, 1)
    assert rates["软件"]["n"] == 2 and rates["软件"]["rate"] == 1.0
    assert rates["影视"]["n"] == 2 and rates["影视"]["rate"] == 0.5


def test_prompt_note_skips_small_samples(session) -> None:
    session.add(DouyinLead(user_id=1, aweme_id="1", mark="x", title="软件安装包",
                           our_url="u", found_at=datetime.now()))
    session.commit()
    rates = vp.niche_rates(session, 1)
    assert vp.prompt_note(rates, min_n=10) == ""      # 样本不足 ⇒ 不喂给模型
    note = vp.prompt_note({"软件": {"n": 20, "moved": 18, "rate": 0.9}}, min_n=10)
    assert "软件90%" in note and "历史经验" in note
