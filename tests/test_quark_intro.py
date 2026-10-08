"""夸克**宣传简介**放入资源的守卫(2026-10-08)。全部离线。

## 背景与**走过的弯路**(别再试回去)
用户口径:「以后出现一个新资源,你就把简介存进去」。夸克**没有上传接口**,于是依次试了:
1. **存自己建的分享** —— 当场撞 `41017 用户禁止转存自己的分享`,换参数/换目录都没用;
2. ✅ **复制**(`/1/clouddrive/file/copy`)—— 简介本来就在我们盘里,复制不动分享、
   不受限制、无需提取码。**实测通过**(`简介.doc` 2,976,256 字节已复制到位)。

## 真正的难点在三个容易做错的地方,这个文件就守这三条
1. **顺序**:简介必须夹在「保存资源」与「建链」**之间**。先建链再放简介,
   链接就是没有简介那份的快照 ⇒ 对方保存下来**永远看不到它**(静默无效,最难发现的那种)。
2. **放哪一层**:资源本身是**一个目录** ⇒ 放进它**里面**(对方整包保存自动带走);
   资源是**散文件** ⇒ 只能与它并列,那就必须**并进分享清单**,否则并列在我们盘里对他不可见。
3. **失败边界**:简介放不进去**不许毁掉整次转存**;而"复制任务发出去了"**不等于"落地了"**
   —— 异步任务必须**按目标目录轮询**确认(判据是名字+大小都在,不是收到了 200)。
"""
from __future__ import annotations

import pytest

from app.services.quark_transfer import QuarkError, QuarkTransfer

TARGET = "TARGETFID"
PROMO_DIR = "PROMODIRFID"

PROMO_FILES = [{"file_name": "简介.doc", "fid": "I1", "size": 2_976_256, "dir": False}]


def _f(name: str, fid: str, size: int, *, is_dir: bool = False) -> dict:
    return {"file_name": name, "fid": fid, "size": size, "dir": is_dir,
            "share_fid_token": f"tok::{fid}"}


@pytest.fixture()
def qt() -> QuarkTransfer:
    return QuarkTransfer("fake-cookie")


def _wire(qt: QuarkTransfer, monkeypatch, *, top: list[dict],
          promo: list[dict] | None = None,
          target_entries: list[dict] | None = None,
          promo_found: bool = True) -> dict:
    """断网假件。**内容是真模型**:复制成功后目标目录里**真的**多出这些条目,
    于是轮询那一步能被真正走到(第一版夹具让复制"看起来成功"但目录永远为空,
    恰好把轮询这条最重要的判据遮住了)。"""
    rec: dict = {"share_ids": None, "order": [], "save_targets": [], "copy_targets": [],
                 "copy_fids": [], "new_fid": {"RES1": "RESNEW"}}
    contents: dict[str, list[dict]] = {
        TARGET: list(target_entries or []),
        PROMO_DIR: list(promo if promo is not None else PROMO_FILES),
        "RESNEW": [],
    }
    monkeypatch.setattr(qt, "_parse_share", lambda url: ("SRC", ""))
    monkeypatch.setattr(qt, "_get_stoken", lambda sid, pwd: "ST")
    monkeypatch.setattr(qt, "_list_share_files", lambda sid, st, pdir_fid="0": list(top))
    monkeypatch.setattr(qt, "_ensure_dir",
                        lambda path: (rec["order"].append("ensure"), TARGET)[1])
    monkeypatch.setattr(qt, "_list_dir", lambda fid="0": list(contents.get(fid, [])))
    monkeypatch.setattr(qt, "_wait_task_fids", lambda tid: [])
    monkeypatch.setattr(qt, "resolve_named_dir",
                        lambda name: PROMO_DIR if promo_found else "")

    def _request(method, path, **kw):
        j = kw.get("json", {})
        if path.endswith("/share/sharepage/save"):
            to = j.get("to_pdir_fid")
            rec["save_targets"].append(to)
            rec["order"].append(f"save:{to}")
            new = [rec["new_fid"].get(str(x), f"NEW::{x}") for x in (j.get("fid_list") or [])]
            for src, nf in zip(j.get("fid_list") or [], new):
                e = next(x for x in top if str(x["fid"]) == str(src))
                contents.setdefault(to, []).append({**e, "fid": nf})
            return {"data": {"save_as": {"save_as_top_fids": new}}}
        if path.endswith("/file/copy"):
            to = j.get("to_pdir_fid")
            rec["copy_targets"].append(to)
            rec["order"].append(f"copy:{to}")
            for fid in j.get("filelist") or []:
                rec["copy_fids"].append(fid)
                e = next(x for x in contents[PROMO_DIR] if str(x["fid"]) == str(fid))
                contents.setdefault(to, []).append({**e, "fid": f"COPY::{fid}"})
            return {"data": {"task_id": "T1"}}
        raise AssertionError(f"没预期的请求:{method} {path}")

    monkeypatch.setattr(qt, "_request", _request)

    def _share(ids, title="监听转存", password="", expire_days=0):
        rec["share_ids"] = list(ids)
        rec["order"].append("share")
        return {"share_url": "https://pan.quark.cn/s/NEW", "password": "", "share_id": "NEW"}

    monkeypatch.setattr(qt, "share_fids", _share)
    return rec


# ---------------------------------------------------------------------------
# ① copy_into:落地判据 + 复用
# ---------------------------------------------------------------------------


def test_复制要按目标目录确认落地_不是发出请求就算(qt, monkeypatch) -> None:
    """★★ 复制是**异步任务**,只返回 task_id(实测 `_wait_task_fids` 解析不出来、返回 `[]`)。
    "发出去了"≠"落地了" —— 这条判据是本仓最贵的假成功。"""
    rec = _wire(qt, monkeypatch, top=[], target_entries=[])
    out = qt.copy_into(PROMO_FILES, TARGET)
    assert out == ["COPY::I1"]
    assert rec["copy_targets"] == [TARGET]


def test_复制不落地时要抛而不是静默成功(qt, monkeypatch) -> None:
    _wire(qt, monkeypatch, top=[], target_entries=[])
    monkeypatch.setattr(qt, "_list_dir", lambda fid="0": [])      # 复制完目录还是空的
    with pytest.raises(QuarkError):
        qt.copy_into(PROMO_FILES, TARGET, timeout=0.05)


def test_目标里已有同款就免复制(qt, monkeypatch) -> None:
    """同名+同大小 = 同一份 ⇒ 复用。**同一资源被重复转存时最容易忽略的一条**:
    每次转存都往资源里再塞一份简介,简介自己就变成了新的空间占用。"""
    rec = _wire(qt, monkeypatch, top=[], target_entries=[_f("简介.doc", "EXISTED", 2_976_256)])
    assert qt.copy_into(PROMO_FILES, TARGET) == ["EXISTED"]
    assert rec["copy_targets"] == [], "已存在却还是复制了一次"


def test_大小不同不算同款(qt, monkeypatch) -> None:
    """简介改过内容(大小变了)时要**复制新的**,不能因为同名就当成已有。"""
    rec = _wire(qt, monkeypatch, top=[], target_entries=[_f("简介.doc", "OLD", 1_000_000)])
    assert qt.copy_into(PROMO_FILES, TARGET) == ["COPY::I1"]
    assert rec["copy_targets"] == [TARGET]


# ---------------------------------------------------------------------------
# ② transfer_and_share:顺序 + 放哪一层
# ---------------------------------------------------------------------------


def _src_dir() -> dict:
    return _f("高性价比人生指南", "RES1", 0, is_dir=True)


def test_简介必须夹在保存与建链之间(qt, monkeypatch) -> None:
    """★★ **这条最要紧**。先建链再放简介 = 链接快照里没有简介 ⇒ 对方永远看不到,
    而且**没有任何报错**(最典型的"看起来成功实则失败")。"""
    rec = _wire(qt, monkeypatch, top=[_src_dir()])
    qt.transfer_and_share("https://pan.quark.cn/s/src", save_dir="/redian监听",
                          intro_dir="/监听宣传")
    assert rec["order"] == ["ensure", f"save:{TARGET}", "copy:RESNEW", "share"], (
        f"顺序应为 解析目录 → 存资源 → 复制简介(进资源目录) → 建链;实际 {rec['order']}")


def test_资源是一个目录时简介放进资源里面_不并列也不并进分享(qt, monkeypatch) -> None:
    rec = _wire(qt, monkeypatch, top=[_src_dir()])
    out = qt.transfer_and_share("https://pan.quark.cn/s/src", save_dir="/redian监听",
                               intro_dir="/监听宣传")
    assert rec["copy_targets"] == ["RESNEW"], "简介应复制进**资源目录本身**"
    assert rec["share_ids"] == ["RESNEW"], "简介在资源目录里,分享资源就等于分享它,不该再并一个"
    assert out["intro"] == 1


def test_资源是散文件时简介并列存并并入分享(qt, monkeypatch) -> None:
    """★ 否则:简介躺在我们自己的 `/redian监听` 里,对方**根本拿不到** ——
    而日志会写「宣传简介已放入」,看起来一切正常。"""
    top = [_f("电影.mp4", "F1", 5_000_000)]
    rec = _wire(qt, monkeypatch, top=top)
    rec["new_fid"] = {"F1": "F1NEW"}
    out = qt.transfer_and_share("https://pan.quark.cn/s/src", save_dir="/redian监听",
                               intro_dir="/监听宣传")
    assert rec["copy_targets"] == [TARGET], "资源与简介都应并列在目标目录里"
    assert rec["share_ids"] == ["F1NEW", "COPY::I1"], (
        f"简介必须**并进分享清单**,否则对方拿不到;实际 {rec['share_ids']}")
    assert out["intro"] == 1


def test_没配简介目录时一切照旧(qt, monkeypatch) -> None:
    rec = _wire(qt, monkeypatch, top=[_src_dir()])
    out = qt.transfer_and_share("https://pan.quark.cn/s/src", save_dir="/redian监听")
    assert rec["copy_targets"] == [], "没配简介就不该多出一次复制"
    assert rec["order"] == ["ensure", f"save:{TARGET}", "share"]
    assert out["intro"] == 0 and rec["share_ids"] == ["RESNEW"]


def test_简介目录找不到时跳过_不新建也不报错(qt, monkeypatch) -> None:
    """⚠️ 用 `_ensure_dir` 去拿简介目录的话,找不到会**建一个空目录**,
    然后往里复制空气 —— 而日志一切正常。所以要"只找不建"。"""
    rec = _wire(qt, monkeypatch, top=[_src_dir()], promo_found=False)
    out = qt.transfer_and_share("https://pan.quark.cn/s/src", save_dir="/redian监听",
                               intro_dir="/监听宣传")
    assert out["share_url"] and out["intro"] == 0
    assert rec["copy_targets"] == [] and "ensure" not in rec["order"][1:], "不该去建目录"


def test_简介目录是空的时候跳过(qt, monkeypatch) -> None:
    rec = _wire(qt, monkeypatch, top=[_src_dir()], promo=[])
    out = qt.transfer_and_share("https://pan.quark.cn/s/src", save_dir="/redian监听",
                               intro_dir="/监听宣传")
    assert out["intro"] == 0 and rec["copy_targets"] == []


def test_简介子目录不参与复制(qt, monkeypatch) -> None:
    """只复制**文件**:把简介目录里的子目录也搬进资源包,会让对方看到一个像引流的文件夹。"""
    rec = _wire(qt, monkeypatch, top=[_src_dir()],
                promo=[_f("简介.doc", "I1", 2_976_256), _f("旧版备份", "D1", 0, is_dir=True)])
    qt.transfer_and_share("https://pan.quark.cn/s/src", save_dir="/redian监听",
                          intro_dir="/监听宣传")
    assert rec["copy_fids"] == ["I1"], f"只该复制文件,实际 {rec['copy_fids']}"


def test_简介失败不毁掉转存(qt, monkeypatch) -> None:
    """转存是主线,简介是增益 —— 简介挂了要**照常建链**,只是记 `intro=0`。"""
    rec = _wire(qt, monkeypatch, top=[_src_dir()])

    def _boom(items, target, *, timeout=25.0):
        raise QuarkError("复制任务没落地")

    monkeypatch.setattr(qt, "copy_into", _boom)
    out = qt.transfer_and_share("https://pan.quark.cn/s/src", save_dir="/redian监听",
                               intro_dir="/监听宣传")
    assert out["share_url"] == "https://pan.quark.cn/s/NEW"
    assert out["intro"] == 0 and rec["share_ids"] == ["RESNEW"]
