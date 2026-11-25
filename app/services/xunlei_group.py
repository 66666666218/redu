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
import re
from datetime import datetime, timedelta

import requests
from sqlalchemy import func, or_, select

from app.db.models import XunleiGroupShare
from app.utils import get_logger
from app.services.chain_delivery import mark_newly_moved

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
        # ★★ **每个群单独提交** —— 原实现是「循环里 `flush()` + **所有群跑完后**才
        # `commit()` 一次」,而**每轮迭代的开头都要发网络请求**(`_records_with_catchup`
        # 拉群消息)⇒ 写事务**横跨了后面所有群的 HTTP 往返**,写锁实测被持 **22~45 秒**
        # (2026-10-13 由 `app.db.database` 的写事务看门狗点名:栈就落在这两行)。
        # 单写者 sqlite 下后果是真的:当天 370 次「写事务超时」;仅一次重启之后就
        # `database is locked` 42 次、**作业心跳写入失败 10 次**(作业会被误判成"没跑")、
        # 调度作业执行失败 4 次。⇒ **慢活(HTTP)一律不许留在事务里。**
        # ⚠️ 同轮去重靠的是上面的 `known`(Python 集合),**不需要**让事务一直开着。
        # (同类 bug 在 `wechat/_enrich.py`、`pan_discovery.py` 2026-10-08 已修,这是第三处。)
        session.commit()
    # 兜底:某个群 `continue`(拉取失败)或一个群都没新增时,上面那行不会执行;
    # 这里收尾关掉仍开着的(只读)事务 —— 与原实现「循环后必有一次 commit」一致。
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


# ---------------------------------------------------------------- 招揽话术闸门
#
# ★ 为什么需要它(2026-11-25):客户群里**真的收到过**「视频帮发，壹天三位数」「注意！避免和谐
#   看这里」这类**招揽/广告帖**。链路是:迅雷群里的广告帖被**真转存**了 ⇒ 进
#   `resource_library.recent_source_names()` 的词表 ⇒ 被 `library_search_word()` 当成资源名 ⇒
#   五个 presence 板块 + 抖音线索 + 知乎一起拿它去搜 ⇒ 搜回广告本身 ⇒ 推给客户。
#
# ⚠️ **不能复用 `quark_dup.is_promo_name`**:那是按**盘内文件名**写的判据(「切勿卸载」
#   「后续资源会同步更新」),实测 9 个群标题样本只命中 1 个 —— 群分享标题是另一套话术。
#
# 判据靠 **9141 条真标题回放标定**(已转存的迅雷群标题 + 近全量推送标题 + 公众号标题):
# 判死 44 条、20 条「必须放过的真资源」全部零误杀。三个层次:
#   · `_AD_STRICT_*`  真资源名不可能含的**招揽词/微信号** ⇒ 一票否决;
#   · `_AD_B_STRICT`  只在**标题主体**(剥括号 + 去前导)成立 —— 「先保存 防河蟹」挂在**真剧名**
#     后面是正常后缀(`强娶（更至42集）先保存 防河蟹`),必须放过;
#   · `_AD_FILLER`    纯口水/语气/类别词 —— 削掉它们之后**什么都不剩**才算"整名就是广告"。
#     (这正是「🥩🥩速存🚗」与「斗破苍穹剧场版 高清4K（速存）」的分界线。)
_AD_PAREN = re.compile(r"[（(【\[][^)）】\]]*[)）】\]]")
_AD_LEAD = re.compile(r"^[\s\W\d_.\U0001F300-\U0001FAFF☀-➿️]+")
_AD_PUNCT = re.compile(r"[\s\W\d_.]+", re.UNICODE)

#: 真资源名不可能含 —— 招揽词本身
_AD_STRICT = ("帮发", "帮發", "招代发", "代发兼职", "代发助手", "代发日入", "代发赚",
              "三位数", "叁位数", "日入过", "日入百", "日入千", "每天多杯奶茶")
#: 弱词:必须与「钱/量」语境**同现**才成立(`高分韩剧《末路兼职》` 只有「兼职」,不是招揽)
_AD_WEAK = ("代发", "代發", "兼职", "真实有效", "招人", "招代理", "招募")
_AD_MONEY = ("三位数", "叁位数", "日入", "赚米", "一天", "每天", "元", "¥", "工资", "佣金",
             "零撸", "撸")
#: 整名就是某个弱词(`兼职` 这种)
_AD_WEAK_EXACT = ("兼职", "代发", "代發", "招人", "招代理", "招募", "帮发")
#: 微信号形态(`+v❤ Zy2343cray` / `➕ Zy2343cray`)—— 真资源名不会有
_AD_WECHAT = re.compile(
    r"(?:[➕＋]\s*[A-Za-z][A-Za-z0-9]{5,})"
    r"|(?:[+＋]\s*[vV抖微薇][❤💖]?\s*[A-Za-z][A-Za-z0-9]{5,})")
#: B 组:剥括号后仍在主体里 → 看"削干净之后还剩不剩资源名"
_AD_B_STRICT = ("防失联", "防河蟹", "防和谐", "避免和谐", "容易和谐", "防丢失",
                "不走丢", "收藏不迷路", "大汇总", "保存到自己网盘")
#: 兜底:没有 B 组强标记,但整名只剩这些裸大包词(`🥩🥩速存🚗` / `10.24限时大饭饭 速存`)
_AD_FALLBACK = ("速存", "大汇总", "防失联", "防和谐", "防河蟹", "防丢失", "不走丢")
#: 口水/语气/类别词(削完剩下什么才是资源名)
_AD_FILLER = (
    "注意", "看这里", "请看这里", "这里", "速存", "加群", "进群", "加",
    "不迷路", "不麋鹿", "先转存再观看", "先保存", "先转存", "重复进", "不要重复",
    "不可抗因素", "无", "不要", "最近", "迅雷", "热门", "电视剧", "综艺", "电影", "影视",
    "一定要", "必须", "记得", "大家", "请", "然后", "再", "和", "与", "及", "都", "也", "就",
    "第一时间", "持续", "每日", "每天都", "来看", "更多",
    "热播", "漫剧", "交流", "获取", "更新", "以防", "链接", "失效",
    "备用", "后续", "好物", "回家地址", "类", "等", "个文件", "在", "这",
    "裙", "Q裙", "群", "被", "奶茶", "违规", "速度", "查", "解散", "换", "进",
    "很", "多", "强制", "同时", "作为", "又", "了", "png", "jpg", "jpeg", "pdf",
    "Q", "限时", "饭饭", "浮力", "夜宵", "目前", "自取", "喜欢", "顶一贴",
    "扣", "哈", "宝们", "赏", "整理不易",
)


def _ad_residue(text: str) -> str:
    """把招揽话术/口水词削掉,剩下的就是"资源名"(空/1 字 = 没有资源名)。"""
    s = text
    for w in sorted(set(_AD_B_STRICT) | set(_AD_FILLER), key=len, reverse=True):
        s = s.replace(w, " ")
    return _AD_PUNCT.sub("", s)


def is_solicit_title(title: str) -> bool:
    """标题是否是**招揽/加群/引流话术**(而不是一份真资源)。

    用法:凡是**把标题当资源名用**的地方(搜索词、推送卡片标题)都要过它 ——
    这类帖子的`kind` 也是 `drive#folder`(真分享目录),**不能按 kind 区分**。
    """
    raw = str(title or "")
    if not raw.strip():
        return False
    if any(f in raw for f in _AD_STRICT):
        return True
    if any(f in raw for f in _AD_WEAK) and any(m in raw for m in _AD_MONEY):
        return True
    if _AD_WECHAT.search(raw):
        return True
    if _AD_PUNCT.sub("", raw) in _AD_WEAK_EXACT:
        return True

    body = _AD_LEAD.sub("", _AD_PAREN.sub("", raw))
    body_ns = re.sub(r"\s+", "", body)
    if not body_ns:
        # 整名都在括号里(如「🔥防失联+👗不麋鹿💕」剥完只剩表情)→ 退回原名判
        body_ns = _AD_PUNCT.sub("", _AD_LEAD.sub("", raw))
        if not body_ns:
            return False

    hit_b = [f for f in _AD_B_STRICT if f in body_ns]
    if not hit_b:
        # 兜底档:裸大包词 + 削干净什么都不剩
        if any(f in body_ns for f in _AD_FALLBACK) and len(_ad_residue(body_ns)) <= 1:
            return True
        # B 词只在**括号里**(「Q裙漫剧交流（防失联）」):剥括号看不见,但主体削完也不剩东西
        if any(f in raw for f in _AD_B_STRICT) and len(_ad_residue(body_ns)) <= 1:
            return True
        return False

    # B 词在主体里:先看它**前面**是不是真作品名(`强娶（更至42集）先保存 防河蟹`)。
    # ⚠️ 但「🔥热门剧 防失联」的「剧」是泛词 —— 削掉泛词后什么都不剩 ⇒ 仍是招揽。
    first = min(body_ns.index(f) for f in hit_b)
    pre = _ad_residue(body_ns[:first])
    if any(w in body_ns[:first] for w in ("集", "剧", "季", "部", "话", "章")):
        if len(pre) > 1 and pre not in ("都在", "都在这"):
            return False
    # 削干净之后还有真名 ⇒ 真资源;什么都不剩 ⇒ 整名就是招揽
    return len(_ad_residue(body_ns)) <= 1


_UNSET = object()          # 区分"没传 ratio"与"传了 None(= 不知道)"

#: **源链直推**的窗口判据(2026-11-23)。窗口内已直推过的行不再推 ——
#: 借用 `push_pan_dedupe_hours` 而不是另开一个旋钮:同一份资源隔多久该重推一次,
#: 全网只有一个答案(见 `push_ledger.drop_recent` 的盘链闸)。
def _source_push_cutoff(settings) -> datetime | None:
    """窗口起点;`None` = 这个窗口关掉了(`push_pan_dedupe_hours<=0`)。"""
    hours = float(getattr(settings, "push_pan_dedupe_hours", 72) or 72)
    return datetime.now() - timedelta(hours=hours) if hours > 0 else None


def _source_only_item(row, reason: str = "") -> dict:
    """空间受阻的群分享 → **源链直推**条目(2026-11-23)。

    ⚠️ `share_url` 放的是**群主原链**(别人的分享),不是我们的。卡片靠 `source_only`
    这个标记逐行标注「源链,未转存」,别让客户以为我们搬过。
    提取码复用 `pan_discovery._source_pass_code`(**单一出处**:URL 里的 `?pwd=` +
    正文里的「提取码:xxxx」)—— 取不到就空着,**不猜**。

    `_row_id` / `_reason` 是**内部键**(卡片逐字段取,不会渲染出来):推送成功后由
    `mark_source_pushed` 按 id 回写 `source_pushed_at` —— 为什么不能在这里就标,
    见那个函数的头注(手动接口只转存不推送 / 发送失败都会变成"标了没推")。
    """
    from app.services.pan_discovery import _source_pass_code

    return {"title": row.title or "", "group_name": row.group_name or "",
            "share_url": row.origin_url or "",
            "code": _source_pass_code({"title": row.title or "",
                                       "origin_url": row.origin_url or ""}),
            "source_only": True,
            #: ⚠️ 私有键:API 层要剥掉(见 `api/xunlei.py`),别让它成为对外契约的一部分。
            "_row_id": row.id, "_reason": reason}


def mark_source_pushed(session, items: list[dict]) -> int:
    """推送**真的成功之后**,把源链行标成"已推过"。返回标记条数。

    ★★ **为什么标记必须落在推送点**(2026-11-23,第一版写法当场废弃):
      ① **手动接口 `POST /api/xunlei/transfer` 只转存、不推送** —— 在那里标,等于把行
         "锁"进 72h 窗口,而客户一条都没收到;
      ② 飞书 **webhook 没配 / 发送失败**时同样:标了没推。
    ⇒ 「谁真的发出去了,谁才标记」。行仍保持 `pending`(空间出来还要补搬),
    这里**只写 `source_pushed_at` 与消息**,不改 `status`。

    只处理 `items` 里**带 `_row_id` 的**(即已经过 `drop_recent` 过滤、真进了卡片的那些);
    被台账挡掉的不在 `items` 里,自然不会标记。
    """
    pairs = [(int(it["_row_id"]), str(it.get("_reason") or ""))
             for it in items
             if it.get("source_only") and it.get("_row_id") is not None]
    if not pairs:
        return 0
    want = {rid: why for rid, why in pairs}
    marked = 0
    for row in session.scalars(
            select(XunleiGroupShare).where(XunleiGroupShare.id.in_(list(want)))).all():
        row.source_pushed_at = datetime.now()
        why = want.get(row.id) or ""
        #: ⚠️ 消息必须写明「未转存」—— 否则运维看到原来那句「空间不足」会以为仍然没推。
        row.message = (f"未转存,已按源链推送:{why}" if why else "未转存,已按源链推送")[:200]
        marked += 1
    session.commit()
    return marked


def _source_push_budget(session, user_id: int, settings) -> int:
    """今天还剩几条**源链直推**名额(0 = 这条路关掉 / 今天已用完)。

    ★ **为什么要日额度**(2026-11-23):这条链每 20 分钟跑一轮(一天 72 轮),
    按"每轮 N 条"配额度会变成**一天几百条**别人的旧链刷客户群。按**天**算才对得上
    用户口径「盘满时直接推送」——要的是"别停摆",不是"把积压一次倒完"。
    ⚠️ 窗口内已推过的行不再占名额(它们仍是 `pending`,否则每天推的都是同样那几条)。
    """
    limit = int(getattr(settings, "xunlei_group_source_push_limit", 15) or 0)
    if limit <= 0:
        return 0
    day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    used = session.scalar(select(func.count()).select_from(XunleiGroupShare).where(
        XunleiGroupShare.user_id == user_id,
        XunleiGroupShare.source_pushed_at.is_not(None),
        XunleiGroupShare.source_pushed_at >= day_start)) or 0
    return max(0, limit - int(used))


def _gate_limit(settings=None) -> float:
    """盘级预闸门的阈值(`usage/limit`)。

    **单一事实源**:闸门判定(`admit_transfer`)与"**闸门失准**"自检(`transfer_pending`)共用它 ——
    否则改阈值只改一处,自检就会拿错门槛、把正常挡下报成失准(或反过来漏报)。
    ⚠️ 兜底值跟着 `settings` 一起改(2026-10-11 降到 **0.90**:为「一批冲过阈值」留缓冲)——两个数不一致就违背了
    上面那句"单一事实源":将来真走到兜底分支时会**悄悄退回旧门槛**。
    """
    return float(getattr(settings, "xunlei_transfer_max_usage_ratio", 0.90) or 0.90)


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
    #: ★★ **招揽/加群话术不搬**(2026-11-25):这类帖的 `kind` 也是 `drive#folder`,
    #: 体积常常只有几十 KB —— 闸门靠体积/泛词一个都挡不住。而一旦搬进来,它就会顺着
    #: 「已转存 → `recent_source_names` 词表 → `library_search_word`」这条链变成搜索词,
    #: 再被五个板块搜回来推给客户(客户确实收到过「视频帮发,壹天三位数」)。
    #: 同属**策略性不搬**(不可重试):它不是资源,搬一百次也不会变成资源。
    if is_solicit_title(name):
        return False, "招揽/加群话术(不是资源),不搬也不推", False
    return True, "", False


# ---------------------------------------------------------------- 限量转存

def _transfer_with_account_fallback(session, user_id: int, row, chosen):
    """转存一条群资源;**遇到「转存文件数达上限」就换下一个账号**。返回 `(out, 实际账号)`。

    ## 为什么必须单独处理(2026-10-24 实测)
    多账号池是按**空间**择一的(最空的优先),而迅雷的「转存文件数达上限(5 万)」
    **不影响空间比例** ⇒ 空间探针永远看不出它,每一轮都会挑同一个号、撞同一个错。
    当天群转存连续 **7 轮 partial**,detail 全是
    `share_upgrade_to_vip_super_file_count / error_code=9`。

    处置:
      1. 把该号标成 `full`(带 120 分钟冷静期,`pan_accounts.ordered` 会把它排到最后);
      2. 依次换池里**未满**的号重试同一资源;
      3. 全池都达上限 ⇒ 返回**第一条**错误(不吞,也不假装成功)。
    """
    from app.services import pan_accounts
    from app.services import xunlei_transfer as xt

    out = xt.transfer_and_share(row.origin_url, cookie_key=chosen.cookie_key)
    if out.get("code") != "file_count_limit":
        return out, chosen
    pan_accounts.report(session, chosen, full=True, why="转存文件数达上限(5 万)",
                          full_minutes=24 * 60)
    logger.warning("迅雷账号 %s 转存文件数达上限(5 万),换下一个账号重试", chosen.label)
    first = out
    gate = _gate_limit()
    for ref in pan_accounts.ordered(session, "xunlei", user_id=user_id):
        if ref.cookie_key == chosen.cookie_key or ref.is_default:
            continue
        if ref.full_until and ref.full_until > datetime.now().isoformat():
            continue                      # 已在冷静期(空间满或文件数满)
        #: ★★ 2026-10-24 修:换号时**必须跳过空间已满的号**。
        #: 实测当天三个主号都撞了文件数上限,换号逻辑落到了 `xunlei#2`
        #: —— 那是**免费 10 GiB 小号(已用 10253%)**,它的 `full_until` 早已过期,
        #: 于是被当成可用落点,"转存成功"了一条小资源。
        #: 空间闸门(`_gate_limit`,与 `admit_transfer` 同一事实源)对**换号**同样适用。
        #: ★★ 2026-11-20:这里**刻意不再探一次配额** —— 换号发生在**每条资源**上,
        #: 探针一加就回到"每条各打一次"的老毛病(见 `_QUOTA_TTL` 的说明)。
        #: 用池里记的 `known_full`(已把兜底 limit 算出来的比例排除掉,见
        #: `pan_accounts.AccountRef.known_full`):真超额的跳过,不可信的放它去试 ——
        #: 试不成由下面的空间不足分支接住并换下一个。
        if ref.known_full and float(ref.last_ratio) >= gate:
            logger.info("迅雷换号:跳过 %s(空间已用 %.0f%% ≥ 闸门 %.0f%%)",
                        ref.label, float(ref.last_ratio) * 100, gate * 100)
            continue
        retry = xt.transfer_and_share(row.origin_url, cookie_key=ref.cookie_key)
        if retry.get("code") == "file_count_limit":
            pan_accounts.report(session, ref, full=True, why="转存文件数达上限(5 万)",
                    full_minutes=24 * 60)
            continue
        #: ★★ 2026-11-20:这一号**真的装不下**(一手证据,不是配额探针的推测)⇒ **继续换
        #: 下一个**,别拿着第一个空间不足就收工 —— 池里还有空号时,换号是唯一能让这条
        #: 资源搬成的路。⚠️ **不标 full**:`space_insufficient` 也可能是"这一个包太大"
        #: (盘还有余量),给它 30 分钟冷静期会把本来能搬的小包也一起挡掉;排序本来就会
        #: 把更满的号排后面,下一轮自然先挑别的号。
        if xt.space_insufficient(retry) or xt.is_space_error(str(retry.get("message") or "")):
            logger.info("迅雷换号:%s 转存报空间不足,继续换下一个", ref.label)
            continue
        if retry.get("status") == "ok":
            logger.info("迅雷账号 %s 文件数已满,改用 %s 转存成功", chosen.label, ref.label)
            return retry, ref
        #: ⚠️ 换号也没成功(失效分享/凭据坏了这类)⇒ 把**这一号**的结果交出去,
        #: 别再往下试:那类错误与账号无关,换多少个号都一样白撞。
        logger.info("迅雷账号 %s 文件数已满,换到 %s 仍失败:%s",
                    chosen.label, ref.label, str(retry.get("message") or "")[:80])
        return retry, ref
    return first, chosen


def transfer_pending(session, user_id: int, limit: int = 5, settings=None) -> dict:
    """把 pending 的群分享**限量转存**到我方盘,回填我方分享链。

    ⚠️ **转存前过闸门**(`admit_transfer`):盘满了 / 命中泛化大包 → 标 `skipped` 只留痕,
    **不搬**。2026-10-02 实测:没有闸门时自动转存把大合集搬进盘,空间顶到 126%,
    之后所有转存都 `file_space_not_enough`。

    返回 `{"status", "picked", "ok", "failed", "skipped", "items"}`;`items` 供飞书推送。
    """
    from app.services import xunlei_transfer as xt

    # ★★ **过期分诊**(2026-10-29 用户「先做 1+2」):pending 里积了 **11,312 条**、
    # 最老到 08-30,而每轮只搬几条 ⇒ 队列永远清不完,还让"待办 11k"看起来像工作量。
    # 先把**超过 `xunlei_group_pending_expire_days`(默认 14 天)**的统一标 `skipped`,
    # 打标不删(留痕:为什么没搬)。⚠️ `msg_time` 为 NULL 的不动(判不了就别当过期)。
    expire_days = int(getattr(settings, "xunlei_group_pending_expire_days", 14) or 0)
    expired = 0
    if expire_days > 0:
        _cut = datetime.now() - timedelta(days=expire_days)
        stale = list(session.scalars(select(XunleiGroupShare).where(
            XunleiGroupShare.user_id == user_id,
            XunleiGroupShare.status == "pending",
            XunleiGroupShare.msg_time.is_not(None),
            XunleiGroupShare.msg_time < _cut)))
        for _r in stale:
            _r.status, _r.message = "skipped", f"过期未搬(超过 {expire_days} 天,只搬新鲜的)"[:200]
        if stale:
            session.commit()
            expired = len(stale)
            logger.info("迅雷群:过期分诊 %d 条 pending → skipped(>%d 天)", expired, expire_days)

    # ★ **新鲜优先**(同日):只挑最近 `xunlei_group_transfer_max_age_days`(默认 7 天)的;
    # 更老的永远轮不到,留给上面的分诊处理。判不了时间(NULL)的照旧放行。
    age_days = int(getattr(settings, "xunlei_group_transfer_max_age_days", 7) or 0)
    _stmt = select(XunleiGroupShare).where(
        XunleiGroupShare.user_id == user_id,
        XunleiGroupShare.status == "pending",
    )
    if age_days > 0:
        _stmt = _stmt.where(or_(
            XunleiGroupShare.msg_time.is_(None),
            XunleiGroupShare.msg_time >= datetime.now() - timedelta(days=age_days)))
    rows = session.scalars(
        _stmt.order_by(XunleiGroupShare.msg_time.desc()).limit(limit)).all()
    # ★★ **多账号择一:满了就换下一个**(2026-10-14 用户口径:「一个网盘账号内存不足
    #   增加多个账号补上」)—— 池里挑**最空的**;它满了**换下一个**;**全满才停**
    #   (与从前"一个满就停"相比是**严格推广**)。⚠️ 没配池 ⇒ 池只返回一个"默认账号"
    #   ⇒ **单账号行为与从前逐字一致**。配额**按账号探**(探失败 = None ⇒ 放行,与既有取舍一致)。
    # ⚠️⚠️ **这里刻意不新增返回路径**:全满时**照旧**让下面行循环里那句 `admit_transfer`
    #   去判"盘满",走**原来那条** `disk_full` 返回(它带着 `gate_mismatch` 等既有键 ——
    #   再写一份返回就多一处会飘的地方,本仓为这个栽过很多次)。
    from app.services import pan_accounts

    limit_ratio = _gate_limit(settings)
    chosen = pan_accounts.default_ref("xunlei", user_id=user_id)
    ratio = None
    tried: list[str] = []
    #: ⚠️ `ratio is None` 有三种成因,**别混**:①池里一个可用凭据都没有(⇒ 回落默认账号)
    #: ②选中了号、但它的读数不可信(⇒ **就用它**,放行到"转存自己报空间不足"那一层)
    #: ③全池都满了(⇒ ratio 是最后一个已满号的比例,照旧由 `admit_transfer` 判盘满)。
    _picked = False
    _cred_seen = False
    _refs = pan_accounts.accounts(session, "xunlei", user_id=user_id)
    if len(_refs) == 1 and _refs[0].is_default:
        # ★ **没配池(单账号)= 走老路径,逐字不变**:一次探针、一个账号、一条凭据都不预读。
        #   这条分支存在的意义就是"上线多账号**不改变**现有部署的行为"(也正因为如此,
        #   既有的那 10 条用例一行都不用改)。
        ratio = xt.quota_ratio()
    else:
        # ★★ **多账号择一:满了就换下一个**(2026-10-14 用户口径:「一个网盘账号内存不足
        #   增加多个账号补上」)—— 池里挑**最空的**;它满了**换下一个**;**全满才停**
        #   (与"一个满就停"相比是**严格推广**)。配额**按账号探**(探失败 = None ⇒ 放行,
        #   与既有取舍一致)。⚠️ **刻意不新增返回路径**:全满时照旧让下面行循环里那句
        #   `admit_transfer` 去判盘满,走**原来那条** `disk_full` 返回(带着 `gate_mismatch`
        #   等既有键 —— 再写一份返回就多一处会飘的地方)。
        for _ref in pan_accounts.ordered(session, "xunlei", user_id=user_id):
            # ⚠️ 凭据走 `xt._credentials`(**与转存那一步同一个读法**),别在这里自己解析
            #    cookie_json —— 两处各读一次,迟早一个认一个不认。
            cred = xt._credentials(settings, cookie_key=_ref.cookie_key)
            if not cred:
                continue                     # 没凭据 ⇒ 不猜,跳过这个账号
            _cred_seen = True
            #: ★★ 2026-11-20:兜底 `limit` 算出来的比例**不能当"满"**(实测四个号全报 10 GiB
            #: ⇒ 同一块盘读成 1640/1217/10249/7467,闸门连挡 4 天)。判据只有一处
            #: `limit_looks_like_fallback`;命中时闸门按"不知道"处理,让转存自己报
            #: `file_space_not_enough`(一手证据)。原始读数照样写回池,外加 `suspect` 标记。
            _info = xt.quota_info(cred, cookie_key=_ref.cookie_key)
            #: `suspect=None` = **这次没读到数**(探针失败)⇒ 别去动池里原有的可信度标记。
            _suspect = xt.limit_looks_like_fallback(_info) if _info else None
            _raw = _info.get("ratio")
            #: 读数不可信 ⇒ 闸门按"不知道"放行(与探针失败同一条纪律)。
            _r = None if _suspect else _raw
            if _r is None:
                #: ★ 两种 `None` 都在这里:①探针失败 ②读数不可信(兜底 limit)。
                #: **两种都选中它** —— 「不知道 ≠ 满」,放到"转存自己报空间不足"那一层去判。
                #: 原始读数照样写回池(1640 的号下一轮排到后面),同时记下 `suspect`。
                chosen, ratio, _picked = _ref, None, True
                pan_accounts.report(session, _ref, ratio=_raw, full=False, suspect=_suspect)
                break
            if _r < limit_ratio:
                chosen, ratio, _picked = _ref, _r, True
                pan_accounts.report(session, _ref, ratio=_r, full=False, suspect=False)
                break
            pan_accounts.report(session, _ref, ratio=_r, full=True, suspect=False,
                                why=f"已用 {_r * 100:.0f}%")
            #: ★ 2026-11-20:**全满时要把"满"带进闸门**。`chosen/ratio` 原来只在
            #: "选中了能用的号"时赋值 ⇒ 全池都满时 `ratio` 还是 `None` ⇒
            #: `admit_transfer` 判"不知道"**放行**,一路搬到 `_transfer_with_account_fallback`
            #: 拿 `chosen`(默认账号,占位)去撞 —— 实测全满那轮把默认号又写了一遍。
            #: 正确做法:记下最后一个已满号的比例(最满的排在最后 ⇒ 它最像"还差多少"),
            #: 让**原来那条** `admit_transfer` 的盘满分支去判(不新增返回路径)。
            chosen, ratio = _ref, _r
            tried.append(f"{_ref.label} 已用 {_r * 100:.0f}%")
        if not _picked and not _cred_seen:
            # 池里有账号但**一个都取不到凭据** ⇒ 回落默认账号 + 老探针,让下游用它**自己的**
            # 错误说清(缺凭据/失效)—— ⚠️"没有凭据" ≠ "盘满",别说错是哪种坏。
            chosen = pan_accounts.default_ref("xunlei", user_id=user_id)
            ratio = xt.quota_ratio()
            logger.info("迅雷多账号:池里没有可用凭据 ⇒ 回落默认账号(按老路径继续)")
        if tried:
            logger.info("迅雷多账号:候选都满了(%s)⇒ 按盘满处理", ";".join(tried))
    ok_items: list[dict] = []
    failed = skipped = 0
    # 本轮**跳过但没标终态**的条数:单个资源比剩余空间大(盘没满,是这一个包太大)。
    # 与 `skipped` 分开记 —— `skipped` 是"永远搬不了、已标终态",这个是"以后还能搬"。
    too_large = 0
    # ★ **"单个资源太大"跳过的那些,要把名字留下来**(2026-10-07 用户口径:
    #   「如果内存不够了,也需要把名称来告诉用户**而不是停摆**」)。
    # 原来只 `logger.warning` 一行 —— **日志没人看**,用户只知道"今天没搬成",
    # 不知道"是哪几个、因为要多大"。实测那 44 条跳过告警就是这么沉掉的。
    too_large_names: list[str] = []
    too_large_need: int | None = None      # 这些大包里**最大的**那个要多少(留痕用)
    too_large_free: int | None = None      # 当时盘上还剩多少
    first_err = ""          # 第一条失败原因 —— 带出去让运行记录**可照做**(见 tick 的状态判定)
    # ★★ **盘满直推**(2026-11-23,用户口径「盘容量满时进行直接推送不需要再进行转存」)。
    # 空间受阻的行不再一条不推地留在队里,而是**跳过转存、把群主原链直接推给客户**。
    # ⚠️ 行仍保持 `pending`(空间出来要补搬)⇒ 靠 `source_pushed_at` 防重复推。
    source_items: list[dict] = []
    source_only = 0
    source_skipped = 0            #: 窗口内已推过 / 今日额度用完 —— **不是**"没这条"
    _src_cutoff = _source_push_cutoff(settings)
    _src_budget = _source_push_budget(session, user_id, settings)

    def _try_source_push(row, reason: str) -> bool:
        """这一行能不能**按源链推**?能就标好并收进 `source_items`。"""
        nonlocal source_only, source_skipped, _src_budget
        #: ⚠️ **本批已经搬成的行不算候选**:它已经有我方链了,再推一遍源链是重复。
        if str(row.status or "") != "pending":
            return False
        #: ★★ **招揽话术的源链也不推**(2026-11-25):盘满时走的是"直接给群主原链"这条路,
        #: 它**绕过了 `admit_transfer`**(那不是转存闸门)⇒ 不在这里再挡一次,广告帖会从
        #: 另一条路照推给客户。判据与转存闸门同源(`is_solicit_title`)。
        if is_solicit_title(row.title):
            source_skipped += 1
            return False
        if _src_budget <= 0:
            source_skipped += 1
            return False
        #: ⚠️ **窗口内推过的不再推**:它们仍是 `pending`(每轮都在队首),不排除的话
        #: 今天的名额全被同几条吃掉,后面几百条永远轮不到(与 pan_discovery 同一个病)。
        pushed = row.source_pushed_at
        if _src_cutoff is not None and pushed is not None and pushed >= _src_cutoff:
            source_skipped += 1
            return False
        source_items.append(_source_only_item(row, reason))
        source_only += 1
        _src_budget -= 1
        return True

    for row in rows:
        allowed, why, retryable = admit_transfer(row.title, settings=settings, ratio=ratio)
        if not allowed:
            if retryable:
                # ⚠️ 盘满:**整批停下,行保持 pending** —— 清理出空间后下一轮自动继续,
                # 若标成 skipped,用户清完还得人工重新排队(那是把方便留给了代码、麻烦留给人)
                # ★★ 但**推**不必等盘:**链本身是好的**,客户点开就能拿(2026-11-23)。
                # 剩下没推的照旧留在队里,空间出来先补搬。
                _pushed = sum(1 for r in rows if _try_source_push(r, why))
                logger.info("迅雷群分享暂停转存(盘满):%s;本轮直推源链 %d 条", why, _pushed)
                session.commit()
                return {"status": "disk_full", "picked": 0, "ok": 0, "failed": 0, "skipped": 0,
                        "expired": expired, "quota_ratio": ratio, "quota_limit": _gate_limit(settings),
                        "gate_mismatch": False,      # 预闸门**按设计**挡下的,不是失准
                        "source_only": source_only, "source_skipped": source_skipped,
                        "message": why, "items": source_items}
            row.status, row.message = "skipped", why[:200]      # 策略性不搬 → 终态留痕
            skipped += 1
            logger.info("迅雷群分享跳过转存 %s:%s", row.title, why)
            continue
        # ★ **三盘互通**(用户口径:「所有资源都走三盘互通」):群里这条资源
        # **别的盘已经有了** ⇒ 直接复用那条链,**不再搬一份**(省盘空间、省一次转存)。
        from app.services.pan_discovery import reuse_if_have

        _have = reuse_if_have(session, user_id, row.title)
        if _have and _have.get("my_link"):
            _had = bool(str(row.our_url or ""))
            row.status, row.our_url = "ok", str(_have["my_link"])
            mark_newly_moved(row, _had)  # **首次搬成**(复用已有链也算搬成)
            row.message = "三盘互通:复用已有链,未重复转存"
            ok_items.append({"title": row.title, "group_name": row.group_name,
                             "share_url": row.our_url, "code": ""})
            logger.info("迅雷群分享:三盘互通命中,复用已有链 %s", row.title)
            continue
        # ★ 2026-10-24:文件数达上限时**换下一个账号**(见 helper 的说明)
        out, chosen = _transfer_with_account_fallback(session, user_id, row, chosen)
        # ↑ 落在**选中的那个账号**的盘上(见上面择一那段)
        if out.get("status") == "ok":
            _had = bool(str(row.our_url or ""))
            row.status, row.our_url = "ok", out.get("share_url") or ""
            mark_newly_moved(row, _had)  # **首次搬成时刻**(2026-10-07)
            row.pass_code, row.fid = out.get("code") or "", out.get("fid") or ""
            # ★ **功能降级要留在行上**(2026-10-16):原来恒写 "" ⇒ 2 号盘没有「监控简介」时,
            #   新链静默地没有宣传简介,而运行记录与列表页**全是绿的**。
            #   (简介缺失不是转存失败 —— 状态照旧 ok,只是把降级写在 message 里。)
            row.message = str(out.get("intro_note") or "")
            ok_items.append({"title": row.title, "group_name": row.group_name,
                             "share_url": row.our_url, "code": row.pass_code})
        else:
            msg = out.get("message") or ""
            if out.get("code") == "file_count_limit":
                #: ★ 2026-10-24:给出**能照做**的下一步 —— 这条不是代码能修的,
                #: 是账号侧的配额(迅雷普通账号转存文件数上限 5 万)。
                msg += (" —— **所有可用迅雷账号的转存文件数都已达上限**;"
                        "恢复转存需:升级超级会员 / 清理盘内文件 / 再扫码加一个迅雷号")
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
                    too_large_names.append(str(row.title or "")[:60])
                    # ★ **搬不进 ≠ 推不了**(2026-11-23):这条的源链是好的,客户点开就能拿。
                    # 原来它只被记进「单个太大跳过N」,一条都不推 —— 实测每轮都有 5~6 条
                    # 这样沉在队里(最大那个要 175 GiB),而客户那边整天看不到更新。
                    _try_source_push(row, msg)
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
                # ★★ **盘满直推**(2026-11-23):这一批搬不进了,但**链是好的** ——
                # 把本批剩下的(以及后面还没轮到的)pending 按源链推出去,别让客户干等。
                for _r in rows:
                    _try_source_push(_r, msg)
                session.commit()
                return {"status": "disk_full", "picked": len(rows), "ok": len(ok_items),
                        "failed": failed, "skipped": skipped, "too_large": too_large,
                        "expired": expired,
                        "too_large_names": too_large_names,
                        "source_only": source_only, "source_skipped": source_skipped,
                        "blocked_names": [str(r.title or "")[:60] for r in rows
                                          if r.status == "pending"][:10],
                        "quota_ratio": ratio, "quota_limit": gate,
                        "gate_mismatch": ratio is not None and ratio < gate,
                        "required_size": out.get("required_size"),
                        "free_size": out.get("free_size"),
                        "message": "转存返回空间不足,本轮停止(清理出空间后会继续)",
                        "items": ok_items + source_items}
            # ⚠️ 单条失败**不重试到底**:标 failed 留痕,避免每轮都拿它空转。
            row.status, row.message = "failed", msg[:200]
            failed += 1
            first_err = first_err or msg[:160]
            logger.warning("迅雷群分享转存失败 %s:%s", row.title, row.message)
    session.commit()
    return {"status": "ok", "picked": len(rows), "ok": len(ok_items),
            "failed": failed, "skipped": skipped, "too_large": too_large,
            "expired": expired,
            #: ★ **直推的条数单独报**(2026-11-23):它**没转存**、不花转存额度,
            #: 混进 `ok` 会让「转存 N 条」这个数再次骗人(与 pan_discovery 同一条纪律)。
            "source_only": source_only, "source_skipped": source_skipped,
            "required_size": too_large_need, "free_size": too_large_free,
            "message": first_err, "items": ok_items + source_items}


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

def push_new_shares(items: list[dict], settings, session=None, user_id: int = 0) -> bool:
    """群资源推飞书 —— **客户群**(主群)。已转存的给**我方链**,盘满未转存的给**源链**。

    版式与公众号推送**一致**(2026-10-02 用户口径"格式按照公众号的格式"):
    **四列网格** —— 资源 / 链接,不靠空格对齐。

    ⚠️ **目的地(2026-10-03 用户口径)**:"**内容是给用户看的,推客户群就行;管理群职责是
    接受维护的信息**。" 这张卡是**内容**(资源 + 可直接用的链),所以走**主群** ——
    此前推管理员群是错的。运营侧要看的是另一件事(哪条被闸门挡下/失败),那走 `push_ops_note`。

    ★★ **两种链必须分开说**(2026-11-23,盘满直推)。`pan_discovery` 那条链早就这么做了:
       · 已转存 ⇒ 链是**我们的**,标「已转存」;
       · 盘满未转存 ⇒ 链是**别人群的源链**,标「源链,未转存」。
    混成一句「已全部转存」就是**硬编码的假话**,客户会以为我们搬过。
    ⚠️ **不列来源群名**(2026-10-02):那是别人的群名,露出去等于把人往别人那儿送。
    ⚠️⚠️ **仍然不单开「原文」列**(2026-10-16 用户拍板):这一张的"原文"就是别人的群分享链,
    与公众号/快手那种"内容帖"不是一类东西。盘满直推的源链**必须**落在「链接」这一列里
    (它正是客户要的那份东西),但**逐行标注 + 头部说明**要写清楚来源性质。
    """
    if not items:
        return False
    webhook = str(getattr(settings, "feishu_webhook", "") or "").strip()
    if not webhook:
        return False
    # ★ **AI 价值筛选**(2026-10-29,用户口径「每个板块都加上 AI 筛选」)。
    # 价值口径:「有用的也就是意味着**资源发的多、转存人数多**」;群资源这一批已经
    # 全部转存成功,模型只判"这份资源是否值得客户现在看"。失败/没配 key ⇒ 全推。
    from app.services import ai_filter

    _ai = ai_filter.filter_items(
        "xunlei", items, settings,
        text_of=lambda it: str(it.get("title") or ""))
    if not _ai.items:
        return False
    items = _ai.items
    #: ★★ **招揽话术最后一道闸**(2026-11-25):这条链的入口有两条(`transfer_pending` 的
    #: 已转存行 + 盘满时的源链直推),两条都已在源头挡过;但 AI 筛选是**旁路**的
    #: (`ai_filter` 失败时**全放行**),而"盘满直推"还有手动接口那条路 —— 与其赌
    #: 每个入口都记得调,不如在**唯一出口**再挡一次(客户收到的就是这里发出去的)。
    _n_ad = 0
    _kept = []
    for it in items:
        if is_solicit_title(str(it.get("title") or "")):
            _n_ad += 1
            continue
        _kept.append(it)
    if _n_ad:
        logger.info("迅雷群资源:挡下 %d 条招揽/加群话术(不推给客户)", _n_ad)
    items = _kept
    if not items:
        return False

    #: ★ **推送去重**(2026-11-23):这条链此前**一处都没接**台账(全仓只有
    #: `push_ledger.record` 零处、`drop_recent` 零处)⇒ 盘满直推后同样的源链
    #: 会每轮重推。去重口径与其它板块一致:同 section 内 24h / 同盘链 72h。
    #: ⚠️ **没有盘链的条目只走内容去重**(`_pan_id_of` 认不出就给空串)。
    from app.services import push_ledger

    items, _dup = push_ledger.drop_recent(session, user_id, "xunlei_group", items,
                                          settings) if session is not None and user_id else (items, 0)
    if not items:
        return False
    #: ★★ **跨板块标注**(2026-11-25,用户拍板「做降级标注」):这份资源在**别的板块**
    #: 72h 内推过就标出来,**照推不拦** —— 用户口径「丢资源比重复推一遍严重得多」。
    #: 身份 = `share_url`(`_PAN_KEYS` 里 `my_link`/`our_url`/`pan_url` 之后的兜底,
    #: 这条链的转存行与源链行都落在它上面)⇒ 与 `drop_recent` **同一把尺子**,
    #: 只是范围换成了"别的 section"。本板块内的重复已由上面那道闸管掉。
    _n_xsec = 0
    try:
        if session is not None and user_id:
            _n_xsec = push_ledger.annotate_cross_section(
                items, push_ledger.cross_section_seen(session, user_id, "xunlei_group", items))
    except Exception:  # noqa: BLE001 - 标注是旁路,查库失败照旧全推(与台账同一条纪律)
        logger.warning("迅雷群跨板块标注失败(本批照常推)", exc_info=True)
    if _n_xsec:
        logger.info("迅雷群资源:%d 条已在其他板块推荐过(照推,卡面标注)", _n_xsec)

    from app.services.feishu._cards import _col_set_row, _md_safe, strip_others
    from app.services.feishu_client import FeishuClient

    brand = (getattr(settings, "brand_name", "") or "").strip()
    #: ★★ **两种链分开说**(2026-11-23):混成一句「已全部转存」是硬编码的假话。
    _n_src = sum(1 for it in items if it.get("source_only"))
    _n_mine = len(items) - _n_src
    _parent = getattr(settings, "xunlei_transfer_parent", "") or "最全文件"
    if not _n_src:
        _head = (f"**本批 {_n_mine} 个资源,已全部转存并生成分享链,点开即用。**\n"
                 f"已归入「{_parent}」。")
    elif not _n_mine:
        _head = (f"**本批 {_n_src} 个资源,网盘空间不足、本轮未转存,直接给源链"
                 f"(点开同样能取)。**\n这些是群主原分享链,不是我们的转存链。")
    else:
        _head = (f"**本批 {len(items)} 个资源:{_n_mine} 条已转存成我方分享链、"
                 f"**{_n_src} 条因网盘空间不足未转存**(给的是群主源链,点开同样能取)。**\n"
                 f"已转存的已归入「{_parent}」。")
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content":
        # **结论先行**(用户口径"推送是为了用户更好总结"):先说"这批是什么、能直接干什么",
        # 再上列表 —— 光甩一张表,看的人还要自己数、自己猜能不能用。
        _head
        + (f"\n**{_ai.summary()}**:已挡掉纯广告 / 无具体资源 / 过时的那些。"
           if _ai.summary() else "")
        + (f"\n已跳过 {_dup} 条 {int(getattr(settings, 'push_dedupe_hours', 24) or 0)} 小时内推过的。"
           if _dup else "")
        #: 顶部只报"几条",板块明细跟着每行走(与其它板块同一版式)。
        + (f"\n其中 {_n_xsec} 条已在其他板块推荐过(照推,明细见各行)。"
           if _n_xsec else "")}},
        # ⚠️ **不列来源群**:那是**别人的群名**,卡片是发到客户群看的(2026-10-02 用户口径
        # "不要带别人的关键词")—— 露出别人的群等于把人往别人那儿送。
        # ⚠️⚠️ **也不加「原文」列**(2026-10-16 用户拍板「**不露**」):这一张的"原文"
        # 与公众号/快手那几张**不是一类东西** —— 那边的原文是**内容帖**(文章/视频),
        # 这里的原文**就是别人的群分享链本身** ⇒ 露出去等于**把客户直接送到别人那儿**,
        # 与上面那条口径同源。⇒ 全网卡片统一成"有原文有网盘"时,**这一张是例外,别顺手改**。
        # ★ 有源链行时列名必须中性 ——「链接」这一列里可能放的是**别人**的链,
        # 叫「我方分享链」就是撒谎(与 pan_discovery 同一处取舍)。
        _col_set_row([("**资源**", 6), (("**可用链**" if _n_src else "**链接**"), 4)],
                     grey=True)]
    for it in items:
        _tail = ""
        if it.get("source_only"):
            #: ⚠️ **逐行标「源链未转存」**:表头改成中性了,但客户仍要能一眼分出
            #: 手里这条是**我们的**还是**别人**的 —— 别人的链哪天被撤掉,我们的不会。
            _tail = " (源链,未转存)"
        elif _n_src:
            _tail = " (已转存)"
        #: ★★ 跨板块标注(2026-11-25):写进**链接格**。客户要判断的是"这条要不要再点",
        #: 而资源名本身不带这个信息(同一份资源换到别的板块,名字一模一样)。
        _xnote = push_ledger.cross_section_note(it.get("_cross_sections") or [])
        elements.append(_col_set_row([
            (_md_safe(strip_others(it.get("title") or "")), 6),
            (f"[▶ 打开]({_md_safe(it.get('share_url') or '')})" + _tail
             + (f" 🔑{_md_safe(it.get('code') or '')}" if it.get("code") else "")
             + (f" · {_md_safe(_xnote)}" if _xnote else ""), 4)]))
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text",
                                                      "content": f"📥 {brand + ' · ' if brand else ''}"
                                                                 f"新资源 · {len(items)} 个"}},
            "elements": elements}
    try:
        ok = FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(card)
    except Exception:  # noqa: BLE001
        logger.exception("迅雷群资源推送失败")
        return False
    if ok and session is not None:
        #: ★★ **真的发出去了才标"已推过"**(2026-11-23)。`transfer_pending` 只负责
        #: **挑出候选**,标记落在推送点 —— 手动接口只转存不推、webhook 没配/发送失败时,
        #: 在那里标就等于把行"锁"进 72h 窗口而客户一条没收到(见 `mark_source_pushed`)。
        #: ⚠️ 台账去重挡掉的那些**不在 `items` 里**,自然不会被误标。
        try:
            mark_source_pushed(session, items)
        except Exception:  # noqa: BLE001 - 卡片已发出,标记失败不该向上抛
            logger.warning("迅雷群源链直推标记失败(内容已发出)", exc_info=True)
            session.rollback()
    if ok and session is not None and user_id:
        #: **台账落 section=`xunlei_group`**(落地的那个群就是主客户群,不是别的板块)。
        #: ⚠️ 写失败只警告不抛出:内容已经发出去了,记不上台账不该让调用方以为推送失败。
        try:
            push_ledger.record(session, user_id, "xunlei_group", items)
        except Exception:  # noqa: BLE001
            #: ⚠️ **必须 rollback**:`record` 在锁冲突时会让 session 进入"必须回滚"状态,
            #: 不收拾的话调用方紧接着的 `_record_run` 会一起失败(本仓 2026-10-19 栽过)。
            logger.warning("迅雷群推送台账写入失败(内容已发出)", exc_info=True)
            session.rollback()
    return ok


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
                #: 本轮起点:`push_new_shares` 里才标"已推过"(见 `mark_source_pushed`),
                #: 之后数 `source_pushed_at >= 这个时刻` 才是**真的推出去几条** ——
                #: `out["source_only"]` 只是**挑出的候选**,webhook 没配 / 发送失败 /
                #: 台账挡掉时那个数会虚高。
                _round_at = datetime.now()
                out = transfer_pending(db, uid, limit=limit, settings=settings) if limit else {}
                total += out.get("ok", 0)
                push_new_shares(out.get("items") or [], settings, session=db, user_id=uid)
                _src_pushed = int(db.scalar(
                    select(func.count()).select_from(XunleiGroupShare).where(
                        XunleiGroupShare.user_id == uid,
                        XunleiGroupShare.source_pushed_at.is_not(None),
                        XunleiGroupShare.source_pushed_at >= _round_at)) or 0)
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
                #: ★★ **盘满直推单独报**(2026-11-23):它**没转存**、不花转存额度,
                #: 混进「转存 N」会让这个数再次骗人(与 `pan_discovery` 同一条纪律)。
                #: ⚠️ 报**真的推出去**的(`_src_pushed`),不是 `out["source_only"]`
                #: (那只是候选 —— webhook 没配时一条都没发,报候选数就是假绿灯)。
                #: `source_skipped` = 窗口内已推过 / 今日额度用完 —— **不是**"没这条资源"。
                if _src_pushed:
                    note += f" **盘满直推源链{_src_pushed}**"
                if out.get("source_skipped"):
                    note += f"(今日额度用完/窗口内已推 {int(out['source_skipped'])})"
                if out.get("expired"):
                    #: 过期分诊了多少条要写出来 —— 否则"队列怎么突然少了 7000 条"没人解释得了。
                    note += f" 过期{int(out.get('expired') or 0)}"
                if got.get("failed_groups"):
                    note += f" 失败群{len(got['failed_groups'])}"
                # ⚠️ **"单个资源太大"必须留痕,否则会被读成"今天群里真没这条"**(2026-10-04):
                # 它**不标终态**(行仍 pending,清出空间后还能搬),但确实没搬成 ——
                # 运行记录里不写,运维就只知道"转存 N 条",不知道有 N 条被这样跳过。
                if out.get("too_large"):
                    from app.services import xunlei_transfer as xt
                    #: ⚠️ `free_size` 为 None 时必须写「未知」而不是「?」(2026-11-18):
                    #: `?` 读起来像"某个数没渲染出来",而真相是**这个数不存在**
                    #: (limit 是兜底值时 `free_bytes()` 返回 None,见它自己的 docstring)。
                    _free = out.get("free_size")
                    _sz = (f"(最大那个要 {xt.human_bytes(out.get('required_size'))}"
                           f"/当时剩 {xt.human_bytes(_free) if _free is not None else '未知'};"
                           if out.get("required_size") is not None else "(")
                    _names = "、".join(out.get("too_large_names") or [])[:90]
                    note += (f" 单个太大跳过{out['too_large']}{_sz}未标终态,清空间后可搬;"
                             f"**是这几个:{_names}**)" if _names else
                             f" 单个太大跳过{out['too_large']}{_sz}未标终态,清空间后可搬)")
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
                    #: ★★ **盘满但已按源链推出去 ⇒ 不是"停摆"**(2026-11-23)。
                    #: 记 `partial` 而不是 `failed`:客户这一轮**确实拿到东西了**,
                    #: 只是没进我们的盘。⚖️ 但**也不能记 success** —— 盘真的满了,
                    #: 补搬仍然堵着,那是运维要处理的事。
                    #: ⚠️ 一条都没**真推出去**(额度用完 / webhook 没配)才记 `failed`。
                    _st = "partial" if _src_pushed else "failed"
                    _record_run(db, uid, "xunlei_group", _st,
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
                # ★ **空间不够没搬成的,把名字说出来**(2026-10-07 用户口径)。
                # 只报"跳过 N 条"等于没说 —— 用户没法判断"要不要清空间 / 清多少"。
                if out.get("too_large_names"):
                    _n = out["too_large_names"]
                    _free = out.get("free_size")
                    _sz = (f"(最大那条要 {xt.human_bytes(out.get('required_size'))},"
                           f"当时剩 {xt.human_bytes(_free) if _free is not None else '未知'})"
                           if out.get("required_size") is not None else "")
                    #: ★ **把"客户其实拿到了"说清楚**(2026-11-23):旧文案只说"没搬成",
                    #: 运营会以为资源没出去。盘满直推之后,这些**已经按源链推到客户群**了。
                    #: ⚠️ 用**真的推出去**的 `_src_pushed`(不是候选数 `source_only`)。
                    ops_bits.append(
                        f"**{len(_n)} 个资源因空间不够没搬成**{_sz}:"
                        + "、".join(_n[:8]) + ("…" if len(_n) > 8 else "")
                        + (f"(其中 {_src_pushed} 条**已按源链推给客户**,仍待补搬)"
                           if _src_pushed else "(**本轮未推**,空间出来才搬)"))
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
