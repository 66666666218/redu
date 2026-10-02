"""迅雷**口令解析**(2026-10-02):抖音标题里《…》包的那串口令 → 真实资源入口。

**这一环补上了「全自动」的最后一步**。此前的链条断在"口令 → 资源":以为只有客户端能做
(服务端搜索端点试过全 403/404)。实际是**没找对端点** —— 迅雷 App 搜索框"粘贴口令"走的
就是一个 REST 接口,而且**我们的登录态直接能用**:

    GET {shouleiAPI}/xlppc.searcher.api/v3/associate_search?keyword=<口令>&guid=<device_id>

返回 `birdkey`(**口令直达**),`action_url` 就是答案,分两种:

    type=share_page → `https://pan.xunlei.com/s/<share_id>?pwd=xxxx&from=BHO/paste/kouling`
                      **直接可转存**(连群都不用加)
    群邀请链         → `https://sj-m-ssl.xunlei.com/group-invite?group_id=<id>&type=cmd`
                      **先加群**,再由 `xunlei_group` 的采集轮把群里的分享捞出来
    两者都没有(type 为空) → 该词不是口令,跳过

**实测样本**(2026-10-02):「玩车不求人」→ 分享链、「三岁宝库」→ 分享链、
「三岁分享」/「白泽的梦」→ 群邀请、「diplay」/「My Dearest」→ 空。

⚠️ **`guid` 是必填**(不传直接 400 `guid is required`),用凭据里的 `device_id` 即可。
`data.list` 是**外部网页联想词**(from=external),不是资源,别拿它当结果。

链路全貌:抖音《口令》→ **本模块** → 分享链/群 → 转存 → 我方分享链 → 入库。
"""
from __future__ import annotations

import re
from urllib.parse import unquote

import requests
from sqlalchemy import select

from app.db.models import XunleiResource
from app.utils import get_logger

logger = get_logger(__name__)

_BASE = "https://api-shoulei-ssl.xunlei.com"
_TIMEOUT = 25
KIND_SHARE = "share"
KIND_GROUP = "group"
KIND_NONE = "none"

_GROUP_ID_RE = re.compile(r"group_id=(\d+)")
_SHARE_ID_RE = re.compile(r"pan\.xunlei\.com/s/([A-Za-z0-9_-]+)")
_PWD_RE = re.compile(r"[?&]pwd=([A-Za-z0-9]{1,8})")


def _headers() -> tuple[dict, str] | None:
    """(请求头, guid);未配置凭据返回 None。`guid` 就是凭据里的 device_id。"""
    from app.services import xunlei_transfer as xt

    cred = xt._credentials()
    if not cred:
        return None
    at = xt._access_token(cred)
    return xt._headers(at, cred.get("captcha_token") or "", cred.get("device_id") or ""), \
        cred.get("device_id") or ""


def resolve(kouling: str) -> dict:
    """口令 → 资源入口。

    返回 `{"kind": "share"|"group"|"none", "share_url", "pass_code", "group_id",
    "title", "raw_type"}`。网络/凭据异常一律降级成 `kind="none"`(不抛)——
    它跑在抖音线索链上,单条解析不了不该拖垮整轮。
    """
    empty = {"kind": KIND_NONE, "share_url": "", "pass_code": "", "group_id": "",
             "title": "", "raw_type": ""}
    kouling = (kouling or "").strip()
    if not kouling:
        return empty
    auth = _headers()
    if not auth:
        return empty
    headers, guid = auth
    try:
        resp = requests.get(f"{_BASE}/xlppc.searcher.api/v3/associate_search",
                            headers=headers, timeout=_TIMEOUT,
                            params={"keyword": kouling, "guid": guid})
        birdkey = (resp.json() or {}).get("birdkey") or {}
    except Exception:  # noqa: BLE001
        logger.exception("迅雷口令解析失败:%s", kouling[:40])
        return empty

    url = unquote(birdkey.get("action_url") or "")
    raw_type = str(birdkey.get("type") or "")
    if not url:
        return {**empty, "raw_type": raw_type, "title": str(birdkey.get("content") or "")}

    group = _GROUP_ID_RE.search(url)
    if group:
        return {"kind": KIND_GROUP, "share_url": "", "pass_code": "",
                "group_id": group.group(1), "title": kouling, "raw_type": raw_type}
    share = _SHARE_ID_RE.search(url)
    if share:
        pwd = _PWD_RE.search(url)
        return {"kind": KIND_SHARE, "share_url": f"https://pan.xunlei.com/s/{share.group(1)}",
                "pass_code": (pwd.group(1) if pwd else ""), "group_id": "",
                "title": kouling, "raw_type": raw_type}
    return {**empty, "raw_type": raw_type}


def join_group(group_id: str) -> dict:
    """按群号加群(`group/join`)。**已在群里**也返回 ok(`newly_joined=false`)。"""
    from app.services import xunlei_group

    if not str(group_id):
        return {"status": "failed", "message": "没有群号"}
    return xunlei_group.join_group(group_id)


def ingest(session, user_id: int, kouling: str, settings=None) -> dict:
    """口令 → 解析 → 转存 → 入库(一条龙)。

    - `kind=share`:**直接转存**并生成我方分享链,写进 `xunlei_resources`;
    - `kind=group`:**加群**后返回 `deferred` —— 群里的资源交给 `xunlei_group` 的
      采集轮(它按群消息流逐条处理),这里不重复实现;
    - `kind=none`:该词不是口令。

    返回 `{"status", "kind", "kouling", "group_id", "share_url", "our_url", "message"}`。
    """
    from app.services import xunlei_transfer as xt

    info = resolve(kouling)
    out = {"status": "ok", "kind": info["kind"], "kouling": kouling,
           "group_id": info["group_id"], "share_url": info["share_url"],
           "our_url": "", "message": ""}
    if info["kind"] == KIND_NONE:
        out["status"] = "not_kouling"
        return out
    if info["kind"] == KIND_GROUP:
        joined = join_group(info["group_id"])
        out["status"] = "deferred" if joined.get("status") == "ok" else "failed"
        out["message"] = joined.get("message") or "已加群,等采一轮会收进群里的分享"
        return out

    url = info["share_url"] + (f"?pwd={info['pass_code']}" if info["pass_code"] else "")
    res = xt.transfer_and_share(url)
    if res.get("status") != "ok":
        out.update(status="failed", message=(res.get("message") or "")[:200])
        return out
    out["our_url"] = res.get("share_url") or ""
    session.add(XunleiResource(user_id=user_id, fid=res.get("fid") or "",
                               name=kouling[:255], kind="drive#folder", size="",
                               parent_name="口令解析", share_url=out["our_url"][:500],
                               pass_code=(res.get("code") or "")[:32]))
    session.flush()                       # ⚠️ 同轮去重靠它(见 cross_accounts 的教训)
    session.commit()
    return out


def resolve_many(keywords: list[str]) -> list[dict]:
    """批量解析(去重、保序),供"一批线索先看能不能解析"的场景用。"""
    seen: set[str] = set()
    out: list[dict] = []
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        out.append({**resolve(kw), "kouling": kw})
    return out


def known_koulings(session, user_id: int) -> set[str]:
    """已经通过口令解析转存过的词(按 `xunlei_resources.name` 去重),避免重复转存。"""
    return set(session.scalars(select(XunleiResource.name).where(
        XunleiResource.user_id == user_id,
        XunleiResource.parent_name == "口令解析")).all())
