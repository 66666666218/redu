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


class XunleiGroupError(RuntimeError):
    """迅雷**群组接口**硬失败(HTTP 非 200 / 网络异常)。

    ⚠️ **为什么必须与"群里没有新分享"分开**(2026-10-03 全项目 A 类排查):此前
    `list_groups` / `group_records` 把所有失败吞成 `[]`,而 `xunlei_group_tick` 记
    `success(群0 新0 转存0)` —— **凭据失效时整条群链静默停摆**,运行记录里跟"群里今天
    真没新资源"长得一模一样。这是本项目已修 6 次的静默失败(闲鱼/知乎/MediaCrawler/
    迅雷扫盘/跨平台号/这里)同型问题的第 7 例。
    **判据**:群列表为空是正常的(账号没加群);但**请求失败**不是。
    """


def list_groups() -> list[dict]:
    """账号所在的全部群:`[{group_id, name, role}]`。**硬失败抛 `XunleiGroupError`**。

    没配凭据时仍返回 `[]`(调用方 `sync_group_shares` 会先查 `_headers()` 再决定 ——
    与 `xunlei_transfer.list_files` 同一口径)。
    """
    h = _headers()
    if not h:
        return []
    try:
        resp = requests.get(f"{_BASE}/chitchat/v1/group/list", headers=h, timeout=_TIMEOUT)
        if resp.status_code != 200:
            raise XunleiGroupError(f"群列表 HTTP {resp.status_code}: {resp.text[:100]}")
        data = resp.json().get("data") or []
        return [{"group_id": str(g.get("id") or g.get("group_id") or ""),
                 "name": str(g.get("name") or g.get("group_name") or ""),
                 "role": str(g.get("user_role") or g.get("role") or "")}
                for g in data if (g.get("id") or g.get("group_id"))]
    except XunleiGroupError:
        raise
    except Exception as exc:  # noqa: BLE001 - 包成自己的异常类型,好让调用方区分
        logger.exception("迅雷群列表获取失败")
        raise XunleiGroupError(f"群列表失败:{type(exc).__name__}: {str(exc)[:100]}") from exc


def group_records(group_id, count: int = _PAGE_SIZE, record_id: int = 0,
                  direction: int = 0) -> list[dict]:
    """拉一个群的消息(服务端返回**新→旧**)。**硬失败抛 `XunleiGroupError`**。

    `record_id` 是游标:不传返回最新一页;传某个消息 id 可前后翻(`direction` 0/1)。
    ⚠️ 之前这里失败返回 `[]`,于是"这个群拉不到消息"被当成"这个群今天没消息"。
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
        if resp.status_code != 200:
            raise XunleiGroupError(f"群消息 HTTP {resp.status_code}: {resp.text[:100]}")
        return resp.json().get("records") or []
    except XunleiGroupError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("迅雷群消息获取失败 group=%s", group_id)
        raise XunleiGroupError(f"群 {group_id} 消息失败:{type(exc).__name__}") from exc


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

# 漏消息时的补翻上限(页)。一页 `_PAGE_SIZE` 条消息,3 页 = 60 条。
# 设上限是为了:接口若返回异常页(比如一直不回我们见过的分享),不至于无限翻下去打爆接口。
_MAX_CATCHUP_PAGES = 3


def _records_with_catchup(group_id: str, gname: str, known_share_ids: set[str]) -> list[dict]:
    """拉一个群的消息,**必要时往回补翻几页**;返回合并后的记录(可能有重复,入库靠 share_id 去重)。

    ⚠️ **为什么需要**(2026-10-03 全项目审查):`/chitchat/group/records` 每页只回
    `_PAGE_SIZE`(20)条,而旧实现**只拉最新一页** —— 于是"两次采集之间消息数超过 20 条"
    的那段时间(停机、连续失败、或群突然刷屏)里发出的分享**会被永久跳过**,且**不报错**:
    下一次采集照样报 `新0`,运行记录一片绿。这正是本项目反复修的那类"静默丢失"。

    **实测风险量级**(2026-10-03):最活跃的群约 1 条消息/27 分钟,20 条 ≈ **9 小时**;
    而当天上午迅雷群链**恰好连续失败 8 小时**(02:40~10:40)——
    **离"丢消息"只差 2 小时**,不是纯理论。

    **判据**:拿**库里已有的 `share_id`** 当水位线 —— 这一页里只要出现**一个见过的分享**,
    就说明已经接上历史、不必再翻。稳态下(每轮都能看到上一轮的分享)**不会多花任何请求**;
    只有"整页都是新分享"这个真·漏消息信号出现时才往回翻。`direction=1` = 往更旧翻(实测确认)。
    """
    page = group_records(group_id)
    merged = list(page)
    for _ in range(_MAX_CATCHUP_PAGES - 1):
        shares = extract_shares(page, group_id, gname)
        if not shares or any(s["share_id"] in known_share_ids for s in shares):
            break                                   # 已接上水位线(或这页没有分享),不必再翻
        ids = [r.get("record_id") or r.get("id") for r in page]
        oldest = min((i for i in ids if i), default=None)
        if not oldest:
            break
        try:
            page = group_records(group_id, record_id=oldest, direction=1)
        except XunleiGroupError as exc:
            # 补翻失败**不拖垮整轮**:最新一页已经拿到手,顶多少补几条历史。
            logger.warning("迅雷群 %s 补翻失败(不影响本页):%s", group_id, exc)
            break
        if not page:
            break
        merged.extend(page)
    return merged


def sync_group_shares(session, user_id: int, group_ids: list[str] | None = None,
                      settings=None) -> dict:
    """扫一轮群消息,**把没见过的分享登记成 pending**(不转存)。

    `group_ids` 为空 = 账号所在的全部群。返回 `{"status", "groups", "new"}`。
    """
    if not _headers():
        return {"status": "no_cred", "groups": 0, "new": 0, "message": "未配迅雷凭据",
                "failed_groups": []}
    try:
        groups = list_groups()
    except XunleiGroupError as exc:
        # ⚠️ **不能当成"这个账号没加群"**:群列表拉不到 = 凭据/网络问题,群里其实有货。
        # 旧实现返回空表 → tick 记 `success(群0 新0)`,整条群链静默停摆(2026-10-03 修)。
        session.rollback()
        logger.warning("迅雷群列表失败:%s", exc)
        return {"status": "failed", "groups": 0, "new": 0, "failed_groups": [],
                "message": str(exc)[:200]}
    if group_ids:
        want = {str(g) for g in group_ids}
        groups = [g for g in groups if g["group_id"] in want] or \
            [{"group_id": str(g), "name": ""} for g in group_ids]
    if not groups:
        return {"status": "empty", "groups": 0, "new": 0, "failed_groups": []}

    known = set(session.scalars(select(XunleiGroupShare.share_id).where(
        XunleiGroupShare.user_id == user_id)).all())
    new_count = 0
    failed_groups: list[str] = []
    for group in groups:
        gid, gname = group["group_id"], group.get("name") or ""
        try:
            # ⚠️ 走带补翻的版本:只拉最新一页会在"消息超过 20 条的空档"里**静默丢分享**
            records = _records_with_catchup(gid, gname, known)
        except XunleiGroupError as exc:
            # 单个群拉不到**不该中断整轮**,但**必须计数**:全群失败时"被挡住"与
            # "群里今天真没新分享"在运行记录里长得一样(2026-10-03 修)。
            failed_groups.append(gname or gid)
            logger.warning("迅雷群 %s 消息拉取失败:%s", gid, exc)
            continue
        for item in extract_shares(records, gid, gname):
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
    if failed_groups and len(failed_groups) == len(groups):
        # **全部群都拉不到** → 不是"今天群里没新分享",是链路坏了。必须冒出去让 tick 记 failed。
        raise XunleiGroupError(f"{len(groups)} 个群全部拉取失败(如 {failed_groups[0]})")
    logger.info("迅雷群采集:%d 个群 → 新登记 %d 条分享(失败群 %d)",
                len(groups), new_count, len(failed_groups))
    return {"status": "ok", "groups": len(groups), "new": new_count,
            "failed_groups": failed_groups}


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


def _gate_limit(settings=None) -> float:
    """盘级预闸门的阈值(`usage/limit`)。

    **单一事实源**:闸门判定(`admit_transfer`)与"**闸门失准**"自检(`transfer_pending`)共用它 ——
    否则改阈值只改一处,自检就会拿错门槛、把正常挡下报成失准(或反过来漏报)。
    """
    return float(getattr(settings, "xunlei_transfer_max_usage_ratio", 0.9) or 0.9)


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
    limit = _gate_limit(settings)
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
    # 本轮**跳过但没标终态**的条数:单个资源比剩余空间大(盘没满,是这一个包太大)。
    # 与 `skipped` 分开记 —— `skipped` 是"永远搬不了、已标终态",这个是"以后还能搬"。
    too_large = 0
    too_large_need: int | None = None      # 这些大包里**最大的**那个要多少(留痕用)
    too_large_free: int | None = None      # 当时盘上还剩多少
    first_err = ""          # 第一条失败原因 —— 带出去让运行记录**可照做**(见 tick 的状态判定)
    for row in rows:
        allowed, why, retryable = admit_transfer(row.title, settings=settings, ratio=ratio)
        if not allowed:
            if retryable:
                # ⚠️ 盘满:**整批停下,行保持 pending** —— 清理出空间后下一轮自动继续,
                # 若标成 skipped,用户清完还得人工重新排队(那是把方便留给了代码、麻烦留给人)
                logger.info("迅雷群分享暂停转存(盘满):%s", why)
                session.commit()
                return {"status": "disk_full", "picked": 0, "ok": 0, "failed": 0, "skipped": 0,
                        "quota_ratio": ratio, "quota_limit": _gate_limit(settings),
                        "gate_mismatch": False,      # 预闸门**按设计**挡下的,不是失准
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
            if xt.is_own_share_error(msg):
                # **我们自己发的分享**:转存自己必然失败,重试也是白试 → 直接终态
                row.status, row.message = "skipped", "这是我们自己的分享(转存自己的文件),不搬"
                skipped += 1
                logger.info("迅雷群分享是自己的分享,跳过:%s", row.title)
                continue
            if xt.is_dead_share_error(msg):
                # **分享本身已死**(分享者被封/过期/取消):永远转不了 → 直接终态,别再当失败
                row.status, row.message = "skipped", "分享已失效(分享者被封/过期/取消),转不了"
                skipped += 1
                logger.info("迅雷群分享已失效,跳过:%s", row.title)
                continue
            if xt.is_space_error(msg):
                # 🔎 **先分两种**(2026-10-04 用户口径:「不一定是搬不动,有没有可能是一次搬太多」):
                #   ⒜ **单个资源比剩余空间大** —— 盘上明明还有 6.04 TiB,只是这一个包要 6.88 TiB。
                #      ⇒ **只跳过这一条,整批照常继续**。它自己**保持 pending**(清出空间后仍可搬),
                #        但**不许拖停别人的转存** —— 之前那版让 1 个大包把 30 条全卡住,是放大伤害。
                #   ⒝ **盘真的要满** ⇒ 整批停下(清空间后自动继续),并触发「闸门失准」自检。
                if xt.space_insufficient(out):
                    row.message = msg[:200]            # 保持 pending:可重试,只是本轮不搬
                    too_large += 1
                    _need = out.get("required_size")
                    if isinstance(_need, int) and (too_large_need is None or _need > too_large_need):
                        too_large_need, too_large_free = _need, out.get("free_size")
                    logger.warning("迅雷群分享跳过(单个资源比剩余空间大,整批继续):%s | %s",
                                   row.title, msg[:100])
                    continue
                # **第二层兜底**:闸门靠配额探针,探针失效会漏;真撞上"空间不足"时也要
                # 把它当**可重试**处理 —— 行**保持 pending**,整批停下,清出空间自动继续。
                row.message = msg[:200]
                session.commit()
                logger.warning("迅雷群分享转存遇空间不足,本轮停止:%s", msg[:80])
                # 🔎 **闸门失准自检**(2026-10-04):转存已经说了"空间不足",那就回头看
                # **配额探针当时怎么说** —— 探针若报"没到阈值"(闸门本来会放行),两者就是矛盾的:
                # 说明那道预闸门**挡不住这件事**。实测:配额 **79.9%**(阈值 90%)、转存照样
                # `file_space_not_enough`,一路连撞 30 多轮,而闸门每次都放行。
                # 这里**不额外打接口** —— `ratio` 是本次批量开头算好的,直接用。
                # 探针拿不到(None)就**不下结论**,别把"不知道"报成"失准"。
                gate = _gate_limit(settings)
                return {"status": "disk_full", "picked": len(rows), "ok": len(ok_items),
                        "failed": failed, "skipped": skipped, "too_large": too_large,
                        "quota_ratio": ratio, "quota_limit": gate,
                        "gate_mismatch": ratio is not None and ratio < gate,
                        "required_size": out.get("required_size"),
                        "free_size": out.get("free_size"),
                        "message": "转存返回空间不足,本轮停止(清理出空间后会继续)",
                        "items": ok_items}
            # ⚠️ 单条失败**不重试到底**:标 failed 留痕,避免每轮都拿它空转。
            row.status, row.message = "failed", msg[:200]
            failed += 1
            first_err = first_err or msg[:160]
            logger.warning("迅雷群分享转存失败 %s:%s", row.title, row.message)
    session.commit()
    return {"status": "ok", "picked": len(rows), "ok": len(ok_items),
            "failed": failed, "skipped": skipped, "too_large": too_large,
            "required_size": too_large_need, "free_size": too_large_free,
            "message": first_err, "items": ok_items}


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
    """转存成功的群资源推飞书 —— **客户群**(主群)。

    版式与公众号推送**一致**(2026-10-02 用户口径"格式按照公众号的格式"):
    **四列网格** —— 资源 / 我方分享链,不靠空格对齐。

    ⚠️ **目的地(2026-10-03 用户口径)**:"**内容是给用户看的,推客户群就行;管理群职责是
    接受维护的信息**。" 这张卡是**内容**(资源 + 可直接用的链),所以走**主群** ——
    此前推管理员群是错的。运营侧要看的是另一件事(哪条被闸门挡下/失败),那走 `push_ops_note`。
    """
    if not items:
        return False
    webhook = str(getattr(settings, "feishu_webhook", "") or "").strip()
    if not webhook:
        return False

    from app.services.feishu._cards import _col_set_row, _md_safe, strip_others
    from app.services.feishu_client import FeishuClient

    brand = (getattr(settings, "brand_name", "") or "").strip()
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content":
        # **结论先行**(用户口径"推送是为了用户更好总结"):先说"这批是什么、能直接干什么",
        # 再上列表 —— 光甩一张表,看的人还要自己数、自己猜能不能用。
        f"**本批 {len(items)} 个资源,已全部转存并生成分享链,点开即用。**\n"
        f"已归入「{getattr(settings, 'xunlei_transfer_parent', '') or '最全文件'}」。"}},
        # ⚠️ **不列来源群**:那是**别人的群名**,卡片是发到客户群看的(2026-10-02 用户口径
        # "不要带别人的关键词")—— 露出别人的群等于把人往别人那儿送
        _col_set_row([("**资源**", 6), ("**链接**", 4)], grey=True)]
    for it in items:
        elements.append(_col_set_row([
            (_md_safe(strip_others(it.get("title") or "")), 6),
            (f"[▶ 打开]({_md_safe(it.get('share_url') or '')})"
             + (f" 🔑{_md_safe(it.get('code') or '')}" if it.get("code") else ""), 4)]))
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text",
                                                      "content": f"📥 {brand + ' · ' if brand else ''}"
                                                                 f"新资源 · {len(items)} 个"}},
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
            from app.services.tenant_base import _record_run

            try:
                got = sync_group_shares(db, uid, settings=settings)
                out = transfer_pending(db, uid, limit=limit, settings=settings) if limit else {}
                total += out.get("ok", 0)
                push_new_shares(out.get("items") or [], settings)
                # ⚠️ 写运行记录:否则健康页**看不到这条链**(2026-10-02 补)
                # ⚠️ **按 status 区分**(2026-10-03 修):旧实现无条件记 `success`,
                # 于是"群列表拉不到(凭据失效)"被记成"群0 新0 转存0",跟"今天群里真没新资源"
                # 长得一模一样。`no_cred`/`failed` 都不算成功。
                st = str(got.get("status") or "")
                t_status = str(out.get("status") or "")
                t_ok = int(out.get("ok", 0) or 0)
                t_bad = int(out.get("failed", 0) or 0)
                note = (f"群{got.get('groups', 0)} 新{got.get('new', 0)} "
                        f"转存{t_ok} 跳过{out.get('skipped', 0)} 失败{t_bad}")
                if got.get("failed_groups"):
                    note += f" 失败群{len(got['failed_groups'])}"
                # ⚠️ **"单个资源太大"必须留痕,否则会被读成"今天群里真没这条"**(2026-10-04):
                # 它**不标终态**(行仍 pending,清出空间后还能搬),但确实没搬成 ——
                # 运行记录里不写,运维就只知道"转存 N 条",不知道有 N 条被这样跳过。
                if out.get("too_large"):
                    from app.services import xunlei_transfer as xt
                    _sz = (f"(最大那个要 {xt.human_bytes(out.get('required_size'))}"
                           f"/当时剩 {xt.human_bytes(out.get('free_size'))};"
                           if out.get("required_size") is not None else "(")
                    note += f" 单个太大跳过{out['too_large']}{_sz}未标终态,清空间后可搬)"
                # ⚠️ **转存失败必须反映到状态里**(2026-10-04 修):旧实现只看 `got['status']`,
                # 于是"凭据失效导致 5 条转存**全挂**"被记成 `success(转存0)` ——
                # 和"今天群里真没新资源"长得**一模一样**。实测:迅雷 refresh_token 失效、
                # 转存全失败,而作业每 20 分钟照报 success,**断了好几天没人知道**
                # (采集只用缓存凭据、不需要新 token,所以只有转存这一步会暴露)。
                why = str(out.get("message") or "")[:140]
                if st in ("failed", "no_cred"):
                    _record_run(db, uid, "xunlei_group", "failed",
                                f"{st}: {str(got.get('message') or '')[:140]} {note}")
                elif t_status == "disk_full":
                    # 盘满 → 整批停下(行保持 pending,清空间后自动继续)。**但这不是"成功"**:
                    # 它意味着"有货但搬不进去",不报出来就会以为一切正常。
                    _mm, _rr = bool(out.get("gate_mismatch")), out.get("quota_ratio")
                    tag = (f" ⚠️闸门失准(配额只报 {_rr * 100:.0f}%,闸门本该放行)"
                           if _mm and _rr is not None else "")
                    _record_run(db, uid, "xunlei_group", "failed",
                                f"盘满暂停: {why} {note}{tag}")
                    # 🔔 **闸门失准 → 单独告警**(2026-10-04 用户要求加)。
                    # 预闸门本该在"盘满"之前就把整批挡住;若它**放行了**、转存却仍被迅雷挡回
                    # 「空间不足」,那就是**闸门自身失准** —— 只看运行记录只知道"盘满",
                    # 看不出"那道闸门其实没起作用",于是会一直误以为"到 90% 才会停"。
                    # ⚠️ **进到这里的一定不是"单个资源太大"**(那种已经在上面 `continue` 掉了,
                    # 只跳过自己、不拖停整批):所以这里是**真的**盘况与闸门对不上。
                    # ⚠️ 标题**不含数字**:冷却门按标题去重,带数字就每次都算新告警、每轮刷屏
                    # (与 `disk_guard` 同一条教训)。
                    if _mm:
                        from app.services import alert_service

                        alert_service.notify_incident(
                            db, uid, "pan", "迅雷盘闸门失准:配额说还有空间,转存却报空间不足",
                            f"配额探针报 **{_rr * 100:.1f}%**(闸门阈值 {out.get('quota_limit', 0) * 100:.0f}%)"
                            f"= 闸门本该放行,但转存被迅雷挡回「空间不足」。"
                            f"⚠️ 已排除「单个资源太大」那种(那种只会跳过它自己,不会走到这条告警)"
                            f"→ 疑似**真实停摆点比阈值低**,需人工看一眼盘况(未必是容量到顶)。"
                            f"原始:{why}",
                            settings=settings, push_feishu=True)
                        db.commit()
                elif t_bad and not t_ok:
                    _record_run(db, uid, "xunlei_group", "failed", f"转存全失败: {why} {note}")
                elif t_bad:
                    _record_run(db, uid, "xunlei_group", "partial", f"部分转存失败: {why} {note}")
                else:
                    _record_run(db, uid, "xunlei_group", "success", note)
                db.commit()
                # 🔔 **内部版 → 管理群**(2026-10-03 用户口径:"内容推客户群,管理群推真正的内部消息"):
                # 内容卡(`push_new_shares`)已去客户群,但"**哪个群拉不到 / 哪条被闸门挡下 /
                # 哪条转存失败**"是**维护信息** —— 运营得知道,客户不该知道。
                # ⚠️ **只在真有需处理项时才推**:否则每 20 分钟一条,又变噪音被无视。
                ops_bits = []
                if got.get("failed_groups"):
                    ops_bits.append(f"{len(got['failed_groups'])} 个群拉取失败")
                if out.get("skipped"):
                    ops_bits.append(f"{out['skipped']} 条被闸门挡下(泛化大包/盘快满)")
                if out.get("failed"):
                    ops_bits.append(f"{out['failed']} 条转存失败")
                if ops_bits:
                    from app.services import alert_service

                    alert_service.notify_incident(
                        db, uid, "pan", "迅雷群采集:本轮有需处理的项",
                        "；".join(ops_bits) + f"(本轮成功转存 {out.get('ok', 0)} 条)",
                        settings=settings, push_feishu=True)
                    db.commit()
            except Exception as exc:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("迅雷群采集失败 user=%s", uid)
                _record_run(db, uid, "xunlei_group", "failed", str(exc)[:200])
                db.commit()
    finally:
        db.close()
    return total
