"""★★ 2026-11-20:**兜底 `limit` 算出来的比例不许当"满"**。

## 事故(实测)
`xunlei` 四个号的 `GET /drive/v1/about` 全部回 `limit=10737418240`(10 GiB 免费档
兜底值),而 `usage` 各是 121 GiB ~ **16.03 TiB** ⇒ 同一块盘被读成
`164099% / 1217% / 10249% / 7467%`。闸门拿着这串数字判"盘满",
于是 `xunlei_group` **连续 4 天每 20 分钟一轮 `failed`**、453 条 pending、零转存,
运行记录每轮都写「先清理再搬」—— 而清理**不会**让读数恢复(limit 才是坏的那个)。

## 正确处置
「读数不可信」既不是"满"也不是"空",而是**不知道**:
  · 闸门按"不知道"放行(与探针失败同一条纪律);
  · 真实空间不足交给**一手证据** —— 转存自己返回的 `file_space_not_enough`,
    由 `_transfer_with_account_fallback` 换号重试;
  · 原始读数**照样写回池**(只用于排序),并带上 `ratio_suspect` 标记,
    免得下一轮择一又把它当成"用了多少"。

本文件钉住这条链的每一环;不弄红既有用例(它们走的是可信读数)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

_FREE_TIER = 10 * 1024 ** 3                  # 迅雷免费档 10 GiB(四个号逐字节相同)
_FALLBACK_USAGE = 17_620_032_999_871         # 实测主号 16.03 TiB


class _Settings:
    """只覆盖闸门与作业用得到的字段。"""
    xunlei_transfer_max_usage_ratio = 0.99
    xunlei_group_pending_expire_days = 0
    xunlei_group_transfer_max_age_days = 0


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


# ---------------------------------------------------------------- 读数分叉

def test_兜底limit时闸门读数回None_原始读数仍照实报(monkeypatch) -> None:
    """`quota_ratio()` = 闸门口径(不可信 ⇒ None);`quota_ratio_raw()` = 记账口径(照实)。"""
    from app.services import xunlei_transfer as xt

    monkeypatch.setattr(xt, "quota_info", lambda *a, **k: {
        "limit": _FREE_TIER, "usage": _FALLBACK_USAGE,
        "ratio": _FALLBACK_USAGE / _FREE_TIER})

    assert xt.quota_ratio() is None, "兜底 limit 算出的 1640 不是「用了多少」,闸门要按不知道"
    assert xt.quota_ratio_raw() == pytest.approx(1640.99, abs=0.01), \
        "原始读数要留着 —— 池子排序还得靠它"
    ratio, suspect = xt.quota_reading()
    assert ratio is None and suspect is True, "两者必须成对给出,否则池子只能存一半事实"


def test_可信读数照旧参与闸门(monkeypatch) -> None:
    """真超额(38 TB / 30 TB = 126%)**不许**被当成"读数不可信"放过去。"""
    from app.services import xunlei_transfer as xt

    monkeypatch.setattr(xt, "quota_info", lambda *a, **k: {
        "limit": 33_092_723_015_680, "usage": 40_901_000_000_000, "ratio": 1.26})

    assert xt.quota_ratio() == pytest.approx(1.26)
    assert xt.quota_ratio_raw() == pytest.approx(1.26)
    assert xt.quota_reading() == (pytest.approx(1.26), False)


def test_探针失败既不是满也不是不可信(monkeypatch) -> None:
    """空 dict = **不知道**;别顺手把它标成 `suspect`(那会污染池里的可信度标记)。"""
    from app.services import xunlei_transfer as xt

    monkeypatch.setattr(xt, "quota_info", lambda *a, **k: {})
    assert xt.quota_ratio() is None
    assert xt.quota_ratio_raw() is None
    assert xt.quota_reading() == (None, False)


# ---------------------------------------------------------------- 池子记账

def test_known_full_不把不可信读数当满() -> None:
    """`ratio_suspect=True` 的 1640 **不算满** —— 换号/自检都问这个属性。"""
    from app.services.pan_accounts import AccountRef

    bad = AccountRef("主号", "xunlei", "xunlei", last_ratio=1640.99, ratio_suspect=True)
    good = AccountRef("小号", "xunlei", "xunlei#2", last_ratio=0.95, ratio_suspect=False)
    unknown = AccountRef("新号", "xunlei", "xunlei#3", last_ratio=None)

    assert bad.known_full is False, "兜底 limit 算出的 1640 不许判满(那正是停摆 4 天的原因)"
    assert good.known_full is True
    assert unknown.known_full is False, "没数据 = 不知道,不是满"


def test_suspect标记随原始读数一起落库(session, monkeypatch) -> None:
    """`report(ratio=1640, suspect=True)` 之后,池里**两样都在**。

    ⚠️ 只存数字的下场:400 分钟后的另一次择一把 1640 原样当"用了多少" ⇒ 全池判满。
    """
    from app.services import pan_accounts

    data = {"xunlei": [{"label": "主号", "cookie_key": "xunlei", "user_id": 1,
                        "last_ratio": None, "last_check_at": "", "full_until": "",
                        "note": ""}]}
    monkeypatch.setattr(pan_accounts, "_load", lambda s: data)
    monkeypatch.setattr(pan_accounts, "_save", lambda s, d: True)

    ref = pan_accounts.AccountRef("主号", "xunlei", "xunlei")
    pan_accounts.report(session, ref, ratio=1640.99, full=False, suspect=True)

    got = data["xunlei"][0]
    assert got["last_ratio"] == pytest.approx(1640.99), "原始读数要留着排序"
    assert got["ratio_suspect"] is True, "可信度必须一起记,否则下一轮又判满"
    assert got["full_until"] == "", "读数不可信不是满,不许加冷静期"


def test_suspect不传时保留原标记(session, monkeypatch) -> None:
    """`suspect=None` = 调用方这次**没判**(探针抖了)⇒ 别顺手把原标记清掉。"""
    from app.services import pan_accounts

    data = {"xunlei": [{"label": "主号", "cookie_key": "xunlei", "user_id": 1,
                        "last_ratio": 1640.99, "last_check_at": "", "full_until": "",
                        "note": "", "ratio_suspect": True}]}
    monkeypatch.setattr(pan_accounts, "_load", lambda s: data)
    monkeypatch.setattr(pan_accounts, "_save", lambda s, d: True)

    pan_accounts.report(session, pan_accounts.AccountRef("主号", "xunlei", "xunlei"),
                        ratio=1641.0, full=False)
    assert data["xunlei"][0]["ratio_suspect"] is True


# ---------------------------------------------------------------- 择一与闸门

def _add_share(session) -> None:
    from datetime import datetime

    from app.db.models import XunleiGroupShare

    session.add(XunleiGroupShare(user_id=1, group_id="g", group_name="群", share_id="s1",
                                 origin_url="https://pan.xunlei.com/s/1", title="资源1",
                                 status="pending", msg_time=datetime.now()))
    session.commit()


def _wire_pool(monkeypatch, infos: dict[str, dict]):
    """池里四个号 + 按 `cookie_key` 回应的配额读数。返回转存调用记录。"""
    from app.services import pan_accounts
    from app.services import xunlei_transfer as xt

    calls: list[str] = []
    monkeypatch.setattr(pan_accounts, "_load", lambda s: {"xunlei": [
        {"label": ck, "cookie_key": ck, "user_id": 1, "last_ratio": None,
         "last_check_at": "", "full_until": "", "note": ""} for ck in infos]})
    monkeypatch.setattr(pan_accounts, "_save", lambda s, d: True)
    monkeypatch.setattr(xt, "_credentials",
                        lambda settings=None, *, cookie_key="xunlei": {
                            "refresh_token": "t", "_ck": cookie_key})
    monkeypatch.setattr(xt, "quota_info",
                        lambda cred=None, **k: infos.get(str((cred or {}).get("_ck")), {}))

    def _tas(share_url, parent_id="", settings=None, intro_dir=None, *, cookie_key="xunlei"):
        calls.append(cookie_key)
        return {"status": "ok", "share_url": f"https://pan.xunlei.com/s/OUR-{cookie_key}",
                "code": "", "fid": "f"}

    monkeypatch.setattr(xt, "transfer_and_share", _tas)
    return calls


def test_整池都是兜底limit时不再停摆_会真的去转存(session, monkeypatch) -> None:
    """★ 核心:四个号全回 1640/1217/10249/7467 ⇒ **不许 `disk_full`**,要真的搬。

    这正是 2026-11-20 现场:每 20 分钟一轮 failed、453 条 pending、零转存。
    """
    from app.services import xunlei_group as xg

    _add_share(session)
    calls = _wire_pool(monkeypatch, {
        "xunlei":   {"limit": _FREE_TIER, "usage": 17_620_032_999_871,
                     "ratio": 1640.99},
        "xunlei#2": {"limit": _FREE_TIER, "usage": 130_665_201_980, "ratio": 12.17},
        "xunlei#3": {"limit": _FREE_TIER, "usage": 1_100_488_048_678, "ratio": 102.49},
        "xunlei#4": {"limit": _FREE_TIER, "usage": 801_808_684_682, "ratio": 74.67},
    })
    out = xg.transfer_pending(session, 1, limit=1, settings=_Settings())

    assert out["status"] != "disk_full", f"兜底 limit 不是「满了」,不该停摆:{out}"
    assert out["ok"] == 1, f"该真的搬成一条:{out}"
    assert len(calls) == 1, f"该选中一个号去转存:{calls}"


def test_真超额仍然挡_而且照旧走原来那条盘满返回(session, monkeypatch) -> None:
    """真超额(limit 30 TB / usage 38 TB)不许被这次修改放过去 —— 清理确实有用那种。"""
    from app.services import xunlei_group as xg

    _add_share(session)
    calls = _wire_pool(monkeypatch, {
        "xunlei":   {"limit": 33_092_723_015_680, "usage": 40_901_000_000_000,
                     "ratio": 1.26},
        "xunlei#2": {"limit": 33_092_723_015_680, "usage": 40_901_000_000_000,
                     "ratio": 1.26},
    })
    out = xg.transfer_pending(session, 1, limit=1, settings=_Settings())

    assert out["status"] == "disk_full", out
    assert "盘快满了" in out["message"] and "126%" in out["message"]
    assert out["gate_mismatch"] is False, "预闸门按设计挡下的,不是失准"
    assert calls == [], "真满了就不许转存"


def test_全池真满时不再拿默认账号去撞(session, monkeypatch) -> None:
    """★ 全满时 `ratio` 必须带着"最后一个号的比例"进闸门。

    修之前:`chosen/ratio` 只在"选中能用的号"时赋值 ⇒ 全满时 `ratio` 仍是 None
    ⇒ `admit_transfer` 判"不知道"**放行** ⇒ 一路搬到换号 helper 拿默认账号去撞。
    """
    from app.services import xunlei_group as xg

    _add_share(session)
    calls = _wire_pool(monkeypatch, {
        "xunlei":   {"limit": 33_092_723_015_680, "usage": 40_000_000_000_000,
                     "ratio": 1.21},
        "xunlei#2": {"limit": 33_092_723_015_680, "usage": 39_000_000_000_000,
                     "ratio": 1.18},
    })
    out = xg.transfer_pending(session, 1, limit=1, settings=_Settings())

    assert out["status"] == "disk_full", out
    assert calls == [], f"全满时一次转存都不该发(更不该用默认账号占位):{calls}"


def test_换号遇到空间不足会继续换下一个(session, monkeypatch) -> None:
    """一手证据(转存自己报 `file_space_not_enough`)⇒ 换下一个号,别拿着第一个错误收工。"""
    from types import SimpleNamespace

    from app.services import pan_accounts
    from app.services import xunlei_group as xg
    from app.services import xunlei_transfer as xt

    row = SimpleNamespace(origin_url="https://pan.xunlei.com/s/S1", title="资源")
    chosen = pan_accounts.AccountRef("号1", "xunlei", "xunlei")
    a = pan_accounts.AccountRef("号2", "xunlei", "xunlei#2")
    b = pan_accounts.AccountRef("号3", "xunlei", "xunlei#3")
    monkeypatch.setattr(pan_accounts, "ordered", lambda s, p, user_id=1: [a, b])
    monkeypatch.setattr(pan_accounts, "report", lambda s, ref, **kw: None)
    calls: list[str] = []

    def fake(url, *, cookie_key="xunlei", **kw):
        calls.append(cookie_key)
        if cookie_key == "xunlei":
            return {"status": "failed", "code": "file_count_limit", "message": "上限"}
        if cookie_key == "xunlei#2":
            return {"status": "failed", "code": "file_space_not_enough",
                    "message": "file_space_not_enough"}
        return {"status": "ok", "share_url": "https://pan.xunlei.com/s/OUR", "code": "ab12"}

    monkeypatch.setattr(xt, "transfer_and_share", fake)
    out, used = xg._transfer_with_account_fallback(session, 1, row, chosen)

    assert out["status"] == "ok" and used.cookie_key == "xunlei#3"
    assert calls == ["xunlei", "xunlei#2", "xunlei#3"], \
        f"空间不足要换下一个,不是收工:{calls}"


def test_换号遇到无关错误就收工_不白撞整池(session, monkeypatch) -> None:
    """失效分享/凭据坏了与账号无关 —— 换多少个号都一样,第一次失败结果就该交出去。"""
    from types import SimpleNamespace

    from app.services import pan_accounts
    from app.services import xunlei_group as xg
    from app.services import xunlei_transfer as xt

    row = SimpleNamespace(origin_url="https://pan.xunlei.com/s/S1", title="资源")
    chosen = pan_accounts.AccountRef("号1", "xunlei", "xunlei")
    a = pan_accounts.AccountRef("号2", "xunlei", "xunlei#2")
    b = pan_accounts.AccountRef("号3", "xunlei", "xunlei#3")
    monkeypatch.setattr(pan_accounts, "ordered", lambda s, p, user_id=1: [a, b])
    monkeypatch.setattr(pan_accounts, "report", lambda s, ref, **kw: None)
    calls: list[str] = []

    def fake(url, *, cookie_key="xunlei", **kw):
        calls.append(cookie_key)
        if cookie_key == "xunlei":
            return {"status": "failed", "code": "file_count_limit", "message": "上限"}
        return {"status": "failed", "code": "share_not_found", "message": "分享已失效"}

    monkeypatch.setattr(xt, "transfer_and_share", fake)
    out, used = xg._transfer_with_account_fallback(session, 1, row, chosen)

    assert out["status"] == "failed" and "分享已失效" in out["message"]
    assert used.cookie_key == "xunlei#2", "要把**那一号**的结果交出去"
    assert calls == ["xunlei", "xunlei#2"], f"无关错误不该把整池撞一遍:{calls}"