# -*- coding: utf-8 -*-
"""**我方分享链的存活判据**(2026-11-24):哪条链客户点得开、哪条已经死了。

## 它解决什么

实测(`2026-11-24`):`quark`(夸克1号)**整个账号的分享被平台封禁** ——
2026-04 到 11-23 逐月抽样 16 条分享**全部**回
`41031 分享者用户封禁链接查看受限`,而账号本身是好的(配额、列目录都正常)。
该账号名下 **6596 条**自有分享;近 7 天 `push_ledger` 命中 **901 次**、
公众号 `my_pan_urls` **1477 条** ⇒ **客户点开的绝大多数是死链**。

代码里早有一条"`41031` = 终态"的判据,但它只加在**源链**侧
(`app/services/wechat/_enrich.py` 把源分享判死出队);**没有任何一处看我们自己发的链** ——
于是 `resource_library`、`reuse_if_have`、`already_have`、卡片渲染
还在把同一批死链反复复用、反复推。

## 判据只有一条,出处只有一处

**`link_audit` 的一手探活**。探到 `alive=False` 且错误是**终态**才写死:
- 夸克 `41031`(分享者封禁);
- 迅雷 `share_status=EXPIRED / SENSITIVE_RESOURCE / PASS_CODE_EMPTY`;
- 百度/夸克/迅雷的 `41011 分享地址已失效` 那一族(复用 `pan_discovery._DEAD_LINK_HINTS`)。

⚠️⚠️ **「限流 / 超时 / 探不动」绝不写死** —— `link_audit.probe()` 已经把这类归到
`alive=None`(「待人工看」),这里只处理 `alive is False`。拿"我们没看见"当"链坏了"
会把好链判死,那是本仓那条「没看到≠不存在」的正面违反。

## 存储:按**分享**记一份,不往 5 张业务表各加一列

我方链散在 `wechat_articles.my_pan_urls`(一行多链)/ `xunlei_group_shares.our_url` /
`discovered_pan_links.our_url` / `douyin_leads.our_url` / `xunlei_resources.share_url`。
同一条分享会被**多个标题复用**(实测一条挂过 68 个不同标题)⇒ 逐表加 `dead` 列
必然"一处先修、另一处照旧"。存活是**分享本身**的属性 ⇒ `pan_link_health` 一张表。

## 账号级封禁:`disabled` 标记 + 归并查询

`41031` 是**整号**性质,逐条登记 6596 行既慢又会在新增分享时漏掉。
⇒ 两段式:
1. 命中 41031 时**把该账号在池里标 `disabled`**(`pan_accounts.disable_account`),
   于是它**不再被选去转存**;
2. `dead_share_keys()` **动态归并**:`pan_owned_shares` 里凡属停用账号的分享,
   一律算死 —— 不需要物化任何行,新分享也自动覆盖。

## 自愈

再探到 `alive=True` ⇒ 清掉 `dead_at`(申诉解封/重建分享后不必手工改库)。
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 夸克分享链 → 路径段(`pwd_id`,与该盘 API 里的 `pwd_id` 是同一个东西)
_QUARK_SESHARE_RE = re.compile(r"pan\.quark\.cn/s/([0-9A-Za-z]+)")
#: 百度分享链 → `surl`(路径段,含开头的 `1`)
_BAIDU_SHARE_RE = re.compile(r"pan\.baidu\.com/s/([0-9A-Za-z_-]+)")
#: 迅雷分享链 → `share_id`(路径段)
_XUNLEI_SHARE_RE = re.compile(r"pan\.xunlei\.com/s/([0-9A-Za-z_-]+)")

#: **账号级封禁**的措辞。命中 ⇒ 这条错误不是"某一链坏了",而是"这个号发什么都会坏"。
_ACCOUNT_BAN_HINTS = ("41031", "分享者用户封禁", "分享者被封", "get_share_user_banned")


def platform_of(url: str) -> str:
    u = (url or "").lower()
    for plat, token in (("quark", "quark.cn"), ("baidupan", "baidu.com"),
                        ("xunlei", "xunlei.com")):
        if token in u:
            return plat
    return ""


def share_key_of(url: str) -> tuple[str, str]:
    """分享链 → `(盘, 稳定身份)`;认不出返回 `("", "")`(**不猜**)。

    身份取的是**路径段**而不是整条 URL:`?pwd=` / 末尾标记 / `(自分享)` 这些
    附属物在库里各写各的(实测 `my_pan_urls` 存过 `https://pan.quark.cn/s/xxx (自分享)`),
    拿整条 URL 当键会让同一条分享登记成好几个身份。
    """
    raw = str(url or "")
    for plat, rx in (("quark", _QUARK_SESHARE_RE), ("baidupan", _BAIDU_SHARE_RE),
                     ("xunlei", _XUNLEI_SHARE_RE)):
        m = rx.search(raw)
        if m:
            return plat, m.group(1)
    return "", ""


def is_account_ban(err: str) -> bool:
    """这条探活错误是不是**账号级封禁**(整号发什么都打不开)?见 `_ACCOUNT_BAN_HINTS`。"""
    text = str(err or "")
    return any(h in text for h in _ACCOUNT_BAN_HINTS)


#: **链本身已死**的平台原话 —— 命中才算死,其余一律留活(见模块头注的警告)。
#: ⚠️ 只收**平台明确表态**的措辞。「解析不出 share_id」「没配百度凭据」这种是**我们的问题**,
#: 收进来就会把好链判死(本仓那条「证伪也要控制变量」)。
_DEAD_HINTS = (
    "41031", "分享者用户封禁", "分享者被封", "get_share_user_banned",
    "41011", "分享地址已失效", "链接失效", "分享不存在", "分享已取消", "分享已被删除",
    "share_status=EXPIRED", "share_status=SENSITIVE_RESOURCE",
    "share_status=PASS_CODE_EMPTY", "SENSITIVE_RESOURCE",
)


def is_terminal_dead(err: str) -> bool:
    """这条链的失败是**终态**(重试一万次也不会好)吗?

    ⚠️ 判据只认**平台原话**(见 `_DEAD_HINTS`);限流/超时/我们自己的配置问题
    **一个都不收**。`link_audit.probe()` 已经把限流那类归到 `alive=None`,
    这里再兜一层是为了防止有人把 `alive=False` 的其它来源接进来。
    """
    text = str(err or "")
    return bool(text) and any(h in text for h in _DEAD_HINTS)


def account_of_share(session: Session, user_id: int, platform: str,
                     share_key: str) -> str:
    """这条分享是**哪个账号**创建的?(查 `pan_owned_shares`;查不到返回空串)

    只有夸克有这张登记表。账号级封禁(41031)靠它把"一条链打不开"升级成
    "这个号发什么都打不开"—— 否则 6596 条要逐条探活才拦得住。
    """
    from app.db.models import PanOwnedShare

    if platform != "quark" or not share_key:
        return ""
    try:
        return str(session.scalar(select(PanOwnedShare.account_key).where(
            PanOwnedShare.user_id == user_id,
            PanOwnedShare.platform == "quark",
            PanOwnedShare.pwd_id == share_key).limit(1)) or "")
    except Exception:  # noqa: BLE001 - 认不出账号就只判这一条链
        return ""


def dead_share_keys(session: Session, user_id: int = 1) -> set[tuple[str, str]]:
    """**当前已判死**的分享身份集合 `{(盤, share_key)}`。

    两个来源并起来:
    1. `pan_link_health.dead_at` 非空的(逐条探出来的);
    2. **停用账号名下的全部分享**(账号级封禁,见模块头注)。

    ⚠️ 读库失败一律返回**空集**(= 什么也不拦)。这是"别把死链给客户"的旁路,
    不许变成"库一抖就整站没有链接"的新故障源 —— 与 `owned_urls()` 同一取舍。
    """
    from app.db.models import PanLinkHealth, PanOwnedShare

    out: set[tuple[str, str]] = set()
    try:
        rows = session.execute(select(PanLinkHealth.platform, PanLinkHealth.share_key)
                               .where(PanLinkHealth.user_id == user_id,
                                      PanLinkHealth.dead_at.isnot(None))).all()
        out |= {(str(p), str(k)) for p, k in rows if p and k}
    except Exception:  # noqa: BLE001 - 读不到就当没有,别拦客户
        logger.debug("读取我方链存活登记失败,按没有处理", exc_info=True)

    try:
        from app.services import pan_accounts as _A

        disabled = _A.disabled_account_keys(session, "quark", user_id=user_id)
        if disabled:
            rows = session.scalars(select(PanOwnedShare.pwd_id).where(
                PanOwnedShare.user_id == user_id,
                PanOwnedShare.platform == "quark",
                PanOwnedShare.account_key.in_(sorted(disabled)))).all()
            out |= {("quark", str(k)) for k in rows if k}
    except Exception:  # noqa: BLE001 - 同上
        logger.debug("归并停用账号的自有分享失败,按没有处理", exc_info=True)
    return out


class DeadLinks:
    """一次查询、多次判定的**只读索引**(推送/检索路径里每行都要问一次)。

    ⚠️ 故意做成"取快照"而不是实时查:一轮推送里库不会变,
    逐行查库会把这个旁路变成性能问题(本仓那条"写点抱住慢活"的教训)。
    """

    __slots__ = ("_keys",)

    def __init__(self, session: Session | None = None, user_id: int = 1, *,
                 keys: set[tuple[str, str]] | None = None) -> None:
        self._keys = keys if keys is not None else (
            dead_share_keys(session, user_id) if session is not None else set())

    def is_dead(self, url: str) -> bool:
        """这条链**已知是死的**吗?认不出盘的链一律返回 False(不拦)。"""
        plat, key = share_key_of(url)
        return bool(plat and key and (plat, key) in self._keys)

    def __bool__(self) -> bool:
        return bool(self._keys)


#: 会话备忘键:本会话里**已 `add()` 但还没 flush** 的登记行,按 `(user, plat, key)` 索引。
_PENDING_KEY = "pan_link_health.pending_rows"


def _pending(session: Session) -> dict[tuple[int, str, str], object]:
    """本会话里**已 add 但尚未 flush** 的登记行。

    ## 为什么需要它(2026-11-26 修的缺陷)
    会话是 `autoflush=False`(见 `db.database.get_session_local`),`session.add()` 之后
    **`select()` 看不见那一行**。而**同一条分享被多个标题复用是常态**
    (实测一条挂过 68 个标题)⇒ `link_audit` 一轮里会对同一 share_key 调多次 `mark_dead`
    ⇒ 每次都"查不到 → 新建" ⇒ commit 时撞
    `UNIQUE constraint failed: pan_link_health.user_id/platform/share_key`
    ⇒ **整批回滚,当天所有死链登记全丢**,日志里只有一条 WARN。
    实测:11-25、11-26 两天的 link_audit 各自丢掉全部登记(表里只剩 11 行,
    而夸克1号一个账号就有 6596 条死分享)。

    ⚠️ **不用 `session.flush()` 把它们逼出来**:一轮体检是逐条探活的网络慢活,
    flush 会把 **SQLite 写锁抱过整段慢活**(本仓那条"新加的写点会把锁抱到慢活结束")。
    内存里记一笔就够 —— 落库仍然是外层 `commit()` 的事。
    """
    memo = session.info.get(_PENDING_KEY)
    if memo is None:
        memo = {}
        session.info[_PENDING_KEY] = memo
    return memo


def mark_dead(session: Session, user_id: int, url: str, reason: str, *,
              account_key: str = "", commit: bool = False) -> bool:
    """登记一条**已一手探实**的死链。幂等:已死只更新原因与时刻。返回是否落库。"""
    from sqlalchemy.orm import object_session

    from app.db.models import PanLinkHealth

    plat, key = share_key_of(url)
    if not plat or not key:
        return False
    try:
        ident = (user_id, plat, key)
        memo = _pending(session)
        row = memo.get(ident)
        if row is not None and object_session(row) is not session:
            #: 上一轮 rollback 把它 expunge 了 —— 备忘里的是僵尸,丢掉重查
            memo.pop(ident, None)
            row = None
        if row is None:
            row = session.scalar(select(PanLinkHealth).where(
                PanLinkHealth.user_id == user_id, PanLinkHealth.platform == plat,
                PanLinkHealth.share_key == key))
        now = datetime.now()
        if row is None:
            row = PanLinkHealth(user_id=user_id, platform=plat, share_key=key,
                                checked_at=now)
            session.add(row)
            memo[ident] = row
        row.url = str(url or "")[:500]
        row.dead_at = now
        row.reason = str(reason or "")[:200]
        if account_key:
            row.account_key = str(account_key)[:64]
        if commit:
            session.commit()
        return True
    except Exception:  # noqa: BLE001 - 登记失败只影响"下次还拦不拦",不许挡业务流程
        logger.warning("登记死链失败:%s", str(url)[:80], exc_info=True)
        return False


def mark_alive(session: Session, user_id: int, url: str, *, commit: bool = False) -> bool:
    """探到**活着** ⇒ 清掉死亡标记(**自愈**)。没有登记行时什么都不做。

    ⚠️ 不新建行:健康链是绝大多数(1356 条),给每条都写一行只会把表撑大而没有任何判据价值。
    """
    from app.db.models import PanLinkHealth

    plat, key = share_key_of(url)
    if not plat or not key:
        return False
    try:
        row = session.scalar(select(PanLinkHealth).where(
            PanLinkHealth.user_id == user_id, PanLinkHealth.platform == plat,
            PanLinkHealth.share_key == key, PanLinkHealth.dead_at.isnot(None)))
        if row is None:
            return False
        row.dead_at = None
        row.reason = ""
        row.checked_at = datetime.now()
        if commit:
            session.commit()
        logger.info("我方链复活:%s(%s)", str(url)[:70], plat)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("清除死链标记失败:%s", str(url)[:80], exc_info=True)
        return False


__all__ = ["DeadLinks", "dead_share_keys", "share_key_of", "platform_of",
           "is_account_ban", "is_terminal_dead", "account_of_share",
           "mark_dead", "mark_alive"]