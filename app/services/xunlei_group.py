"""迅雷**群组**资源采集(2026-10-02)。

**背景**:用户给了一条群邀链(`https://dlj.8uri.cn/dlj/c9494ae1` → 迅雷群组 1550069837
「三岁分享」)并说"点击链接可直接申请加入,这个就是群聊的"。此前记录在案的最大卡点是
「**口令 → shareID** 只存在于客户端」——而**群消息流本身就是 shareID 的批发出口**:
群主发的每条分享卡里都带着现成的 `pan.xunlei.com/s/<share_id>`。

**接口**(全部 2026-10-02 实测;域名 `api-shoulei-ssl.xunlei.com`,复用 `xunlei_transfer`
的登录态与请求头):

    GET  /chitchat/v1/group/list          账号所在的全部群(实测 7 个)
    POST /chitchat/v1/group/query         单个群信息(邀请页 SSR 无需登录也能拿到)
    POST /chitchat/v1/group/join          按 group_id 加群
    GET  /chitchat/group/records          **群消息 —— 资源就在这儿**
    POST /chitchat/v1/group/member/query  群成员

⚠️ **路由形状不一致**(实测踩过):消息接口**没有 `v1` 前缀**(`/chitchat/group/records`),
群管理接口**有**(`/chitchat/v1/group/*`);对 records 发 POST 一律 404,只有 GET 通。

**群消息体的形状**(`records[].content` 是 **JSON 字符串**,要二次 `json.loads`):

    type 7   群主分享卡 → `data.share_url` + `data.title` + `data.share_id`  ✅ 可直接转存
    type 15  群文件库/更新卡 → `data.folders[]`,每项有 `share_id` / `folder_name` / `pass_code`
    type 11  群公告、type 14 引导语、无 type 的纯文本 → 没有资源,跳过

**分页**:`count` 服务端封顶 **20**;翻页用 `record_id` 游标(配合 `direction`)。

**为什么采集与转存分成两步**:7 个群一天能出新分享几十条,逐条转存要把文件**全搬进
用户盘**(慢 + 占空间)。所以:

    ① `sync_group_shares`  只把分享**登记**成 pending(纯 HTTP 读,秒级,可高频跑)
    ② `transfer_pending`   每轮**限量**转存(默认 5 条),把结果回填成我方分享链

判据是"**群消息里那条现成的分享链**",所以本模块**不需要**任何口令解析 —— 客户端唯一的
那一步,群组替我们做了。
"""
from __future__ import annotations

import json
from datetime import datetime

import requests
from sqlalchemy import select

from app.db.models import XunleiGroupShare
from app.utils import get_logger

logger = get_logger(__name__)

_BASE = "https://api-shoulei-ssl.xunlei.com"
_TIMEOUT = 25
_PAGE_SIZE = 20          # 服务端封顶,传更大也只返回 20
_SHARE_TYPE = 7          # 群主分享卡
_LIBRARY_TYPE = 15       # 群文件库/更新卡(多条 file,共用 share_id)


# ---------------------------------------------------------------- 只读接口

def _headers() -> dict | None:
    """复用 `xunlei_transfer` 的登录态;没配凭据返回 None。"""
    from app.services import xunlei_transfer as xt

    cred = xt._credentials()
    if not cred:
        return None
    return xt._headers(xt._access_token(cred), cred.get("captcha_token") or "",
                       cred.get("device_id") or "")


def list_groups() -> list[dict]:
    """账号所在的全部群:`[{group_id, name, role}]`。失败返回空表(不抛)。"""
    h = _headers()
    if not h:
        return []
    try:
        resp = requests.get(f"{_BASE}/chitchat/v1/group/list", headers=h, timeout=_TIMEOUT)
        data = resp.json().get("data") or []
        return [{"group_id": str(g.get("id") or g.get("group_id") or ""),
                 "name": str(g.get("name") or g.get("group_name") or ""),
                 "role": str(g.get("user_role") or g.get("role") or "")}
                for g in data if (g.get("id") or g.get("group_id"))]
    except Exception:  # noqa: BLE001 - 探针类调用,失败即空
        logger.exception("迅雷群列表获取失败")
        return []


def group_records(group_id, count: int = _PAGE_SIZE, record_id: int = 0,
                  direction: int = 0) -> list[dict]:
    """拉一个群的消息(服务端返回**新→旧**)。失败返回空表。

    `record_id` 是游标:不传返回最新一页;传某个消息 id 可前后翻(`direction` 0/1)。
    """
    h = _headers()
    if not h or not str(group_id):
        return []
    params: dict = {"group_id": str(group_id), "count": count}
    if record_id:
        params["record_id"] = record_id
        params["direction"] = direction
    try:
        resp = requests.get(f"{_BASE}/chitchat/group/records", headers=h, params=params,
                            timeout=_TIMEOUT)
        return resp.json().get("records") or []
    except Exception:  # noqa: BLE001
        logger.exception("迅雷群消息获取失败 group=%s", group_id)
        return []


def join_group(group_id) -> dict:
    """按群号加群(`POST /chitchat/v1/group/join`)。

    **已在群里**也返回 ok(`newly_joined=false`)——所以这个接口可以无脑重放,不用先查。
    ⚠️ `group_id` **必须传数字**:传字符串服务端报 `code 201 请求参数错误`(实测)。
    """
    h = _headers()
    if not h:
        return {"status": "failed", "message": "未配置迅雷凭据(需先扫码登录)"}
    try:
        num = int(str(group_id).strip())
    except (ValueError, TypeError):
        return {"status": "failed", "message": f"群号不是数字:{group_id}"}
    try:
        resp = requests.post(f"{_BASE}/chitchat/v1/group/join", headers=h, timeout=_TIMEOUT,
                             json={"group_id": num})
        data = resp.json()
        if data.get("code") != 0:
            return {"status": "failed", "message": f"加群失败:{str(data)[:160]}"}
        return {"status": "ok", "group_id": str(num),
                "newly_joined": bool(data.get("newly_joined")),
                "message": "已加入" if data.get("newly_joined") else "已在群里"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("迅雷加群失败 group=%s", group_id)
        return {"status": "failed", "message": str(exc)[:200]}


# ---------------------------------------------------------------- 解析

def _parse_content(raw) -> dict:
    """`content` 是 JSON 字符串 → dict;解析不出来返回 `{"type": None, "data": {}}`。"""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"data": {}}
    except (ValueError, TypeError):
        return {"data": {}}


def _share_from_card(data: dict, record: dict, group_id: str, group_name: str) -> dict:
    """把一条分享卡归一化成入库结构(`share_id` 为空则视为无效)。"""
    share_id = str(data.get("share_id") or "")
    return {"message_id": str(record.get("id") or ""),
            "group_id": group_id, "group_name": group_name,
            "share_id": share_id,
            "origin_url": str(data.get("share_url") or f"https://pan.xunlei.com/s/{share_id}"),
            "title": str(data.get("title") or data.get("folder_name") or "")[:255],
            "sender": str(record.get("sender") or ""),
            "kind": str(data.get("kind") or "")[:16],
            "msg_time": _msg_time(record.get("created_at"))}


def extract_shares(records: list[dict], group_id: str, group_name: str = "") -> list[dict]:
    """从群消息里挑出**分享**(type 7 分享卡 + type 15 群文件库卡),按 `share_id` 去重。

    返回 `[{message_id, group_id, group_name, share_id, origin_url, title, sender, kind,
    msg_time}]`,顺序保持消息的"新→旧"。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        content = _parse_content(rec.get("content"))
        ctype = content.get("type")
        data = content.get("data") or {}
        if not isinstance(data, dict):
            continue
        cards: list[dict] = []
        if ctype == _SHARE_TYPE:
            cards = [data]                                  # 单条分享卡
        elif ctype == _LIBRARY_TYPE:
            cards = [f for f in (data.get("folders") or []) if isinstance(f, dict)]
        for card in cards:
            item = _share_from_card(card, rec, group_id, group_name)
            if not item["share_id"] or item["share_id"] in seen:
                continue
            seen.add(item["share_id"])
            out.append(item)
    return out


def _msg_time(raw) -> datetime | None:
    """群消息时间(unix 秒)→ datetime;非法值返回 None。"""
    try:
        return datetime.fromtimestamp(int(raw)) if raw else None
    except (ValueError, TypeError, OSError):
        return None


# ---------------------------------------------------------------- 采集入库

def sync_group_shares(session, user_id: int, group_ids: list[str] | None = None,
                      settings=None) -> dict:
    """扫一轮群消息,**把没见过的分享登记成 pending**(不转存)。

    `group_ids` 为空 = 账号所在的全部群。返回 `{"status", "groups", "new"}`。
    """
    if not _headers():
        return {"status": "no_cred", "groups": 0, "new": 0}
    groups = list_groups()
    if group_ids:
        want = {str(g) for g in group_ids}
        groups = [g for g in groups if g["group_id"] in want] or \
            [{"group_id": str(g), "name": ""} for g in group_ids]
    if not groups:
        return {"status": "empty", "groups": 0, "new": 0}

    known = set(session.scalars(select(XunleiGroupShare.share_id).where(
        XunleiGroupShare.user_id == user_id)).all())
    new_count = 0
    for group in groups:
        gid, gname = group["group_id"], group.get("name") or ""
        for item in extract_shares(group_records(gid), gid, gname):
            if item["share_id"] in known:
                continue
            known.add(item["share_id"])
            session.add(XunleiGroupShare(
                user_id=user_id, group_id=item["group_id"], group_name=item["group_name"],
                message_id=item["message_id"], share_id=item["share_id"],
                origin_url=item["origin_url"][:300], title=item["title"],
                sender=item["sender"], kind=item["kind"], msg_time=item["msg_time"]))
            session.flush()                     # ⚠️ 见 cross_accounts 的教训:同轮去重靠它
            new_count += 1
    session.commit()
    logger.info("迅雷群采集:%d 个群 → 新登记 %d 条分享", len(groups), new_count)
    return {"status": "ok", "groups": len(groups), "new": new_count}


# ---------------------------------------------------------------- 转存闸门

# **泛化大包**的警戒词。群里动辄几十 TB 的正是这类合集 —— 2026-10-02 实测翻过车:
# 自动转存把「【全网最齐】游戏软件资源合集」搬进用户盘,一把顶到 126%,之后所有转存
# 都 `file_space_not_enough`。名字里带这些词的一律**不自动搬、只把链推给人**。
BULK_WORDS = ("合集", "大全", "资源包", "资源库", "宝库", "全部", "整合", "整理",
              "分类", "最齐", "最全", "全集", "日更", "每日", "更新")


def is_bulk_resource(name: str) -> bool:
    """是不是"泛化大包"(名字里带合集/大全/最全…)。

    ⚠️ **为什么只能按名字判体量**:分享详情里**文件夹的 `size` 一律是 0**,而分享接口
    也不给展开子目录(实测 `parent_id`/`file_id` **都被忽略**,永远返回分享根)——
    所以**拿不到真实体积**。名字是唯一便宜且能生效的判据。

    (抖音那边的取词清洗用的是同一份词表,见 `douyin_leads._to_search_word`。)
    """
    return any(w in (name or "") for w in BULK_WORDS)


_UNSET = object()          # 区分"没传 ratio"与"传了 None(= 不知道)"


def admit_transfer(name: str, cred: dict | None = None, settings=None,
                   ratio: float | None | object = _UNSET) -> tuple[bool, str, bool]:
    """转存准入检查:**两道闸门**都过了才放行。

    ① **盘级**:使用率 ≥ `xunlei_transfer_max_usage_ratio`(默认 0.9)→ 整批不搬 ——
       这是这次翻车的直接原因,也是唯一能兜住"资源包到底多大"的办法;
    ② **名字**:命中泛化大包词 → 不搬(见 `is_bulk_resource`)。

    `ratio` 可由调用方**预先算好传进来**(整批共用),省得每条都去打一次配额接口;
    **显式传 `None` 表示"不知道"**(探针失败)→ 放行,别把探测失败误判成盘满。

    返回 `(放行?, 原因, 可重试?)`。⚠️ **两者的终态不同,不能一刀切**:
      - 盘满 → **可重试**:调用方**不该**把它标成终态,否则用户清理完空间还得人工重新排队;
      - 泛化大包 → **不可重试**:策略性不搬,标终态留痕即可。
    """
    if ratio is _UNSET:
        from app.services import xunlei_transfer as xt

        ratio = xt.quota_ratio(cred)
    limit = float(getattr(settings, "xunlei_transfer_max_usage_ratio", 0.9) or 0.9)
    if ratio is not None and ratio >= limit:
        return False, f"盘快满了(已用 {ratio * 100:.0f}%,阈值 {limit * 100:.0f}%),先清理再搬", True
    if is_bulk_resource(name):
        return False, "泛化大包(名字含合集/大全/最全…),体积不可控,只推链不搬", False
    return True, "", False


# ---------------------------------------------------------------- 限量转存

def transfer_pending(session, user_id: int, limit: int = 5, settings=None) -> dict:
    """把 pending 的群分享**限量转存**到我方盘,回填我方分享链。

    ⚠️ **转存前过闸门**(`admit_transfer`):盘满了 / 命中泛化大包 → 标 `skipped` 只留痕,
    **不搬**。2026-10-02 实测:没有闸门时自动转存把大合集搬进盘,空间顶到 126%,
    之后所有转存都 `file_space_not_enough`。

    返回 `{"status", "picked", "ok", "failed", "skipped", "items"}`;`items` 供飞书推送。
    """
    from app.services import xunlei_transfer as xt

    rows = session.scalars(
        select(XunleiGroupShare).where(
            XunleiGroupShare.user_id == user_id,
            XunleiGroupShare.status == "pending",
        ).order_by(XunleiGroupShare.msg_time.desc()).limit(limit)).all()
    ratio = xt.quota_ratio()               # 只查一次,整批共用(别每条都打一次配额)
    ok_items: list[dict] = []
    failed = skipped = 0
    for row in rows:
        allowed, why, retryable = admit_transfer(row.title, settings=settings, ratio=ratio)
        if not allowed:
            if retryable:
                # ⚠️ 盘满:**整批停下,行保持 pending** —— 清理出空间后下一轮自动继续,
                # 若标成 skipped,用户清完还得人工重新排队(那是把方便留给了代码、麻烦留给人)
                logger.info("迅雷群分享暂停转存(盘满):%s", why)
                session.commit()
                return {"status": "disk_full", "picked": 0, "ok": 0, "failed": 0, "skipped": 0,
                        "message": why, "items": []}
            row.status, row.message = "skipped", why[:200]      # 策略性不搬 → 终态留痕
            skipped += 1
            logger.info("迅雷群分享跳过转存 %s:%s", row.title, why)
            continue
        out = xt.transfer_and_share(row.origin_url)
        if out.get("status") == "ok":
            row.status, row.our_url = "ok", out.get("share_url") or ""
            row.pass_code, row.fid, row.message = out.get("code") or "", out.get("fid") or "", ""
            ok_items.append({"title": row.title, "group_name": row.group_name,
                             "share_url": row.our_url, "code": row.pass_code})
        else:
            msg = out.get("message") or ""
            if xt.is_space_error(msg):
                # **第二层兜底**:闸门靠配额探针,探针失效会漏;真撞上"空间不足"时也要
                # 把它当**可重试**处理 —— 行**保持 pending**,整批停下,清出空间自动继续。
                row.message = msg[:200]
                session.commit()
                logger.warning("迅雷群分享转存遇空间不足,本轮停止:%s", msg[:80])
                return {"status": "disk_full", "picked": len(rows), "ok": len(ok_items),
                        "failed": failed, "skipped": skipped,
                        "message": "转存返回空间不足,本轮停止(清理出空间后会继续)", "items": ok_items}
            # ⚠️ 单条失败**不重试到底**:标 failed 留痕,避免每轮都拿它空转。
            row.status, row.message = "failed", msg[:200]
            failed += 1
            logger.warning("迅雷群分享转存失败 %s:%s", row.title, row.message)
    session.commit()
    return {"status": "ok", "picked": len(rows), "ok": len(ok_items),
            "failed": failed, "skipped": skipped, "items": ok_items}


def list_group_shares(session, user_id: int, status: str = "", limit: int = 200) -> list[dict]:
    """群分享列表(新→旧),供 API/前端展示。"""
    stmt = select(XunleiGroupShare).where(XunleiGroupShare.user_id == user_id)
    if status:
        stmt = stmt.where(XunleiGroupShare.status == status)
    rows = session.scalars(
        stmt.order_by(XunleiGroupShare.msg_time.desc().nullslast(),
                      XunleiGroupShare.id.desc()).limit(limit)).all()
    return [{"group_id": r.group_id, "group_name": r.group_name, "title": r.title,
             "origin_url": r.origin_url, "our_url": r.our_url, "pass_code": r.pass_code,
             "status": r.status, "message": r.message, "kind": r.kind,
             "msg_time": r.msg_time.strftime("%Y-%m-%d %H:%M") if r.msg_time else ""}
            for r in rows]


# ---------------------------------------------------------------- 推送

def push_new_shares(items: list[dict], settings) -> bool:
    """转存成功的资源推**管理员群**(与 `xunlei_sync.push_new_resources` 同一出口)。"""
    if not items:
        return False
    webhook = (getattr(settings, "feishu_webhook_admin", "") or
               getattr(settings, "feishu_webhook", ""))
    if not webhook:
        return False

    from app.services.feishu_client import FeishuClient

    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content":
        f"迅雷群组新转存 **{len(items)}** 个资源,已生成我方分享链:"}}]
    for it in items:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"📦 **{it['title']}**{' · 来自「' + it['group_name'] + '」' if it.get('group_name') else ''}"
            f"\n{it['share_url']}"}})
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text",
                                                      "content": f"📥 迅雷群资源 · {len(items)} 个"}},
            "elements": elements}
    try:
        return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(card)
    except Exception:  # noqa: BLE001
        logger.exception("迅雷群资源推送失败")
        return False


# ---------------------------------------------------------------- 定时入口

def xunlei_group_tick(settings=None) -> int:
    """定时:先采集(登记 pending),再限量转存。返回本轮转存成功数。"""
    from app.db import get_session_local
    from app.db.models import User
    from config.settings import get_settings

    settings = settings or get_settings()
    if not getattr(settings, "xunlei_group_enabled", True):
        return 0
    limit = int(getattr(settings, "xunlei_group_transfer_limit", 5) or 0)
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                sync_group_shares(db, uid, settings=settings)
                if limit:
                    out = transfer_pending(db, uid, limit=limit, settings=settings)
                    total += out.get("ok", 0)
                    push_new_shares(out.get("items") or [], settings)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("迅雷群采集失败 user=%s", uid)
    finally:
        db.close()
    return total
