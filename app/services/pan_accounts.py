"""**网盘账号池**(2026-10-14,用户口径:「一个网盘账号内存不足增加多个账号补上」)。

## 它解决什么
一个网盘账号**满了**,整条转存链就停摆(实测 2026-10-11:迅雷 91% → 98.8%,群转存整批停)。
用户的解法是"**再加一个账号**"。这个模块就是那个"加"的落点:

    池里**挑最空的**那个 → 它满了就**换下一个** → 全满才真的停

⚠️ **单账号时行为与从前逐字一致**(池里没配就拿"默认账号" = 旧的
`cookie_store.get_cookie(uid, platform)`)—— 所以这次上线**不改变现有链路的行为**,
只是把"满了就停"换成"满了先看看还有没有别人"。

## 与小红书账号池的关系(别造第二套)
`app/services/xhs_accounts.py` 早有一套(discover/pick/report/cookie_of + 健康/冷却),
**那套的取舍照抄**:按健康度择一、失败要报回池子、拿不到凭据不猜。区别只有两点:
  · 这里的"健康"是**空间**(`last_ratio`,越小越空);
  · 网盘的凭据是 **`cookie_store` 里的加密 JSON**(不像小红书那样存在池文件里)。
⇒ 所以池里**只存引用**(`cookie_key`),**绝不在 system_config 里放明文凭据**。

## 存储
`system_config['pan_account_pool']` = `{platform: [{label, user_id, cookie_key, last_ratio, …}]}`
(`cookie_key` 就是 `cookie_store` 的平台名 —— 第二个账号用 `xunlei#2` 这种带后缀的名字)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

_POOL_KEY = "pan_account_pool"
#: 盘满后的**冷静期** —— 满了的账号短时间内别再探(省一次接口,也免得反复撞风控)
FULL_COOLDOWN_MIN = 30

#: ★ **百度账号登录态探活**(2026-10-31):`baidupan#2` / `#3` 的 cookie 早已失效,
#: 但在池里它们"最空"(各 ~5TiB) ⇒ **永远排第一** ⇒ 每个百度动作都吃
#: `errno=-6`,而**主号明明是好的**(能列目录,只是超额 43 倍)。实测当天:
#: `pan_retention` 的百度分支连续多轮「列目录失败(errno=-6 /)」。
#: ⇒ 取号时先探一次活(缓存 30 分钟),**死的跳过并打日志**,别让坏凭据挡住好的。
_ALIVE_TTL_S = 1800
_alive_cache: dict[str, tuple[float, bool]] = {}
_BAIDU_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0")


def _alive_key(cookie: str) -> str:
    """缓存键 = cookie 的哈希(**不拿明文当键**,免得凭据进内存 dump/日志)。"""
    import hashlib

    return hashlib.sha1(str(cookie or "").encode("utf-8")).hexdigest()


def _baidu_alive(cookie: str) -> bool:
    """这个百度 cookie 还能用吗?`/api/quota` 的 `errno==0` 才算活。

    ⚠️ **探活失败(网络/超时)不判死** —— 否则一次抖动会把好号踢出池子,
    那是"因为查不到所以判死"的老毛病(见 `absence-of-evidence` 那条教训)。
    """
    import time

    import requests

    key = _alive_key(cookie)
    hit = _alive_cache.get(key)
    now = time.time()
    if hit and now - hit[0] < _ALIVE_TTL_S:
        return hit[1]
    try:
        r = requests.get("https://pan.baidu.com/api/quota",
                         params={"checkfree": 1, "checkexpire": 1},
                         headers={"User-Agent": _BAIDU_UA, "Cookie": str(cookie),
                                  "Referer": "https://pan.baidu.com/"}, timeout=12)
        ok = r.json().get("errno") == 0
    except Exception:  # noqa: BLE001 - 探不到 ≠ 死了
        logger.debug("百度账号探活失败(网络),暂不判死", exc_info=True)
        return True
    _alive_cache[key] = (now, bool(ok))
    return bool(ok)


@dataclass
class AccountRef:
    """池里的一个账号。`is_default` = 没配池时那个隐式默认账号(旧行为)。"""
    label: str
    platform: str
    cookie_key: str
    user_id: int = 1
    last_ratio: float | None = None
    last_check_at: str = ""
    full_until: str = ""
    note: str = ""
    #: ★★ 2026-11-20:`last_ratio` 是不是**从兜底 limit 算出来的**(迅雷四个号的 limit
    #: 全是 10 GiB ⇒ 同一个盘读成 1640 / 12 / 102 / 75)。这种比例**只能用来排序**,
    #: **不能当"满"** —— 拿它判满就是连挡 4 天、453 条 pending 的那种停摆。
    #: 判据出处: `xunlei_transfer.limit_looks_like_fallback`(单一出处,别在这另写一份)。
    ratio_suspect: bool = False
    is_default: bool = False
    tried: list[str] = field(default_factory=list)
    #: ★★ 2026-11-24:**停用**。实测夸克1号整号被平台封禁(所有分享回 `41031`),
    #: 而它的配额/列目录都正常 ⇒ "盘满/登录态失效"那两套判据**都拦不住它**,
    #: 它会被一直选去转存、把自己发的新分享也变成死链。
    #: 停用 = **不选它转存**(`refs_for`/`cookies_for` 过滤),但它名下的链**仍按账号级
    #: 封禁判死**(见 `pan_link_health.dead_share_keys`)。
    #: ⚠️ 与 `full_until` 的区别:那是**临时的**(30 分钟冷静期),这是**要有动作才恢复**的。
    disabled: bool = False
    disabled_reason: str = ""

    @property
    def known_full(self) -> bool:
        """**读得懂地知道它满了**吗 —— `ratio_suspect` 的比例**不算**(那是坏 limit 算的)。

        ★★ 2026-11-20:择一与换号都该问这个,而不是直接比 `last_ratio`。
        兜底 limit 下 `last_ratio` 可能是 1640,直接比会把**整池都判满**(实测连挡 4 天)。
        """
        return (not self.ratio_suspect) and self.last_ratio is not None

    @property
    def usable_hint(self) -> float:
        """择一排序键:**越小越该先挑**。满了的排到最后(不是排除 —— 全满时还得知道是谁满)。

        ⚠️⚠️ **"未知"必须排在最前,不是最后**(2026-10-16 修)。原来写的是
        `1.0 if self.last_ratio is None`,而实测到的比例是 `0.9x` ——
        **0.9x < 1.0 ⇒ "实测 90% 满的老号"排在"刚加、还没探过的新号"前面**
        ⇒ 与"挑最空的"**正好相反**,而且**新加的号永远轮不到**
        (用户口径「一个盘满了增加多个账号补上」—— 加进来的那个号就是最该先试的)。
        ⇒ 未知 = **乐观**(先试它):试错的代价只是浪费一次转存,而漏试的代价是**整个多账号白加**。
        """
        if self.full_until and self.full_until > datetime.now().isoformat():
            return 2.0
        return 0.0 if self.last_ratio is None else float(self.last_ratio)


def _load(session: Session) -> dict:
    from app.db.models import SystemConfig

    try:
        row = session.scalar(select(SystemConfig).where(SystemConfig.key == _POOL_KEY))
        if row and row.value:
            data = json.loads(str(row.value))
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 池读坏了就当"没配"⇒ 回落默认账号,不挡链路
        logger.warning("网盘账号池读取失败(按未配置处理)", exc_info=True)
    return {}


def _save(session: Session, data: dict) -> bool:
    from app.db.models import SystemConfig

    try:
        row = session.scalar(select(SystemConfig).where(SystemConfig.key == _POOL_KEY))
        payload = json.dumps(data, ensure_ascii=False)
        if row:
            row.value = payload
        else:
            session.add(SystemConfig(key=_POOL_KEY, value=payload))
        session.commit()
        return True
    except Exception:  # noqa: BLE001 - 存不上只影响"下次择一",不该炸掉转存
        logger.warning("网盘账号池写入失败", exc_info=True)
        return False


def default_cookie_key(platform: str) -> str:
    """没配池时的默认账号(= 旧行为读的那一枚)。"""
    return platform


def default_ref(platform: str, *, user_id: int = 1) -> AccountRef:
    """**默认账号** = 没配池时那一枚(旧行为的落点)。只此一处构造它。"""
    return AccountRef(label=f"{platform}(默认)", platform=platform,
                      cookie_key=default_cookie_key(platform), user_id=user_id,
                      is_default=True)


def accounts(session: Session, platform: str, *, user_id: int = 1) -> list[AccountRef]:
    """该平台的账号列表。**池里没配 ⇒ 返回一个隐式"默认账号"**(单账号、行为不变)。"""
    pool = _load(session).get(platform)
    if not pool:
        return [default_ref(platform, user_id=user_id)]
    out: list[AccountRef] = []
    for item in pool:
        if not isinstance(item, dict) or not item.get("cookie_key"):
            continue
        out.append(AccountRef(label=str(item.get("label") or item["cookie_key"]),
                              platform=platform, cookie_key=str(item["cookie_key"]),
                              user_id=int(item.get("user_id") or user_id),
                              last_ratio=item.get("last_ratio"),
                              last_check_at=str(item.get("last_check_at") or ""),
                              full_until=str(item.get("full_until") or ""),
                              note=str(item.get("note") or ""),
                              ratio_suspect=bool(item.get("ratio_suspect")),
                              disabled=bool(item.get("disabled")),
                              disabled_reason=str(item.get("disabled_reason") or "")))
    return out or [default_ref(platform, user_id=user_id)]


def disabled_account_keys(session: Session, platform: str, *,
                          user_id: int = 1) -> set[str]:
    """该平台**被停用**的账号 `cookie_key` 集合(**全平台**,不只池里)。

    唯一调用方是 `pan_link_health.dead_share_keys`(账号级封禁 ⇒ 名下分享全算死)。
    单独给一个函数是因为:"哪些号被停用"必须从**一处**读 —— 池是唯一事实源。
    """
    return {r.cookie_key for r in accounts(session, platform, user_id=user_id)
            if r.disabled and not r.is_default}


def ordered(session: Session, platform: str, *, user_id: int = 1) -> list[AccountRef]:
    """**该按什么顺序试** —— 最空的在前;满的排最后(全满时也要能说出是谁满)。

    ★ 2026-11-24:**停用的排最后**(不排除 —— 全停用时调用方要能说出"全停了",
    而不是看到"没配账号",那是两种完全不同的故障,修法也不同)。
    """
    return sorted(accounts(session, platform, user_id=user_id),
                  key=lambda r: (1 if r.disabled else 0, r.usable_hint, r.label))


def cookie_of(ref: AccountRef, session: Session) -> str:
    """该账号的凭据(cookie_store 里的加密 JSON;取不到返回空串 —— **不猜**)。"""
    from app.services.cookie_store import get_cookie

    return get_cookie(session, ref.user_id, ref.cookie_key) or ""


def report(session: Session, ref: AccountRef, *, ratio: float | None = None,
           full: bool | None = None, why: str = "",
           full_minutes: int | None = None, suspect: bool | None = None) -> None:
    """把**这一号的探测结果**写回池子(择一就靠它;`full` 只对这种账号加冷静期)。

    ⚠️ 默认账号(`is_default`)**不写池** —— 单账号部署时池子是空的,别凭空写出一条
    "配置",那会让人以为池配过了。

    `full_minutes` 可覆盖默认冷静期(`FULL_COOLDOWN_MIN`=30 分钟)。
    ★ 2026-10-24:**"空间满"会自行缓解(清理即可),"文件数满"不会** ——
    后者是账号级长期配额(迅雷 5 万),30 分钟后又会被挑中白撞一次。
    ⇒ 调用方对文件数满传 `full_minutes=24*60`,一天试一次就够。
    """
    if ref.is_default:
        return
    data = _load(session)
    items = data.get(ref.platform)
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict) or str(item.get("cookie_key")) != ref.cookie_key:
            continue
        if ratio is not None:
            item["last_ratio"] = round(float(ratio), 4)
        #: ★★ 2026-11-20:**必须把"这个比例可不可信"一起记下来** —— 池里只有数字时,
        #: 400 分钟后的另一次择一会把它原样当"用了多少"(实测整池 1640%/1217%/… 全被判满)。
        #: `suspect=None`(调用方没说)⇒ **保留原标记**,别顺手清掉。
        if suspect is not None:
            item["ratio_suspect"] = bool(suspect)
        item["last_check_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        if full is True:
            item["full_until"] = (datetime.now() + timedelta(
                minutes=full_minutes or FULL_COOLDOWN_MIN)).isoformat(sep=" ", timespec="seconds")
        elif full is False:
            item["full_until"] = ""
        if why:
            item["note"] = why[:120]
        break
    _save(session, data)


def disable_account(session: Session, platform: str, cookie_key: str, *, why: str = "",
                    user_id: int = 1) -> bool:
    """**停用一个账号** —— 不再选它转存。返回是否改到了。

    ★★ 为什么必须有这个动作(2026-11-24 实测):`quark` 整个账号被平台封禁,
    所有分享回 `41031`;可它**配额正常、列目录正常、登录态也没失效** ——
    现有的两套护栏(盘满 `full_until` / 百度登录态 `_baidu_alive`)都**看不见**这种状态,
    于是它会被一直选中去转存,**它新建的分享同样打不开**,死链源源不断。

    ⚠️ 不是 `report(full=True)`:那个 30 分钟就过期,而账号封禁**不会自己好**。
    ⚠️ 恢复要么重新登录/申诉后手工改池,要么由 `enable_account()` 显式放行 ——
    **不要**在探活失败时自动恢复(那等于把刚判死的号又放回去)。
    """
    data = _load(session)
    items = data.get(platform)
    if not isinstance(items, list):
        items = []
    #: ⚠️ 判据是「**池里有没有这一条**」,不是「`cookie_key` 看着像不像默认账号」。
    #: `quark` 这个 key **既可能是没配池时的默认账号,也可能是池里实打实登记的那个号**
    #: (本机实测就是后者:label「夸克1」、cookie_key `quark`)—— 拿字符串比会把
    #: **真正被封的那个号**挡在门外,而那正是这条护栏存在的唯一理由。
    for item in items:
        if isinstance(item, dict) and str(item.get("cookie_key")) == str(cookie_key):
            item["disabled"] = True
            item["disabled_reason"] = str(why or "")[:120]
            item["last_check_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
            _save(session, data)
            logger.warning("账号池:**停用** %s/%s(%s)", platform, cookie_key, why[:80])
            return True
    #: 池里没有这一条 = 单账号部署(池是空的)或 key 写错了。
    #: ⚠️ **不凭空写一条"配置"**:那会让人以为池配过了,与 `report()` 里 `is_default`
    #: 的处理同一理由。调用方要停它请先把池配上。
    logger.warning("账号池:拒绝停用 %s/%s(池里没有这个账号)", platform, cookie_key)
    return False


def enable_account(session: Session, platform: str, cookie_key: str, *,
                   user_id: int = 1) -> bool:
    """**恢复**一个被停用的账号(申诉解封/重登之后走这里)。返回是否改到了。"""
    data = _load(session)
    items = data.get(platform)
    if not isinstance(items, list):
        return False
    for item in items:
        if isinstance(item, dict) and str(item.get("cookie_key")) == str(cookie_key):
            if not item.get("disabled"):
                return False
            item["disabled"] = False
            item["disabled_reason"] = ""
            _save(session, data)
            logger.info("账号池:**恢复** %s/%s", platform, cookie_key)
            return True
    return False


def add_account(session: Session, platform: str, label: str, cookie: str, *,
                cookie_key: str = "", user_id: int = 1) -> str:
    """**登记一个新账号**(用户给一枚新 cookie 时走这里)。返回它的 `cookie_key`。

    - cookie 存进 `cookie_store`(**加密**,与既有平台同一套存储);
    - 池里只留**引用**;
    - 同一个 `label` 已存在 ⇒ 覆盖它的 cookie(改密码/重登后重登记),不新建一条。
    """
    from app.services.cookie_store import set_cookie

    data = _load(session)
    items = data.setdefault(platform, [])
    exist = next((x for x in items if isinstance(x, dict) and x.get("label") == label), None)
    key = cookie_key or (str(exist.get("cookie_key")) if exist else
                         (default_cookie_key(platform) if not items else
                          f"{default_cookie_key(platform)}#{len(items) + 1}"))
    set_cookie(session, user_id, key, cookie)
    if exist is not None:
        exist["cookie_key"] = key
        exist["user_id"] = user_id
    else:
        items.append({"label": label, "user_id": user_id, "cookie_key": key,
                      "last_ratio": None, "last_check_at": "", "full_until": "", "note": ""})
    _save(session, data)
    logger.info("网盘账号池:登记 %s/%s(cookie_key=%s)", platform, label, key)
    return key


__all__ = ["AccountRef", "accounts", "ordered", "cookie_of", "report", "add_account", "default_ref",
           "default_cookie_key", "FULL_COOLDOWN_MIN", "cookies_for", "refs_for", "cookie_for",
           "scoped_path", "disable_account", "enable_account", "disabled_account_keys"]


# ---------------------------------------------------------------- 按池取号(统一入口)

def cookies_for(session, platform: str, *, user_id: int = 1) -> list[tuple[str, str]]:
    """★ **按池取号**:返回 `[(label, cookie), …]`,**最空的在前**。

    池里**没配** ⇒ 只返回默认 key 那一枚 ⇒ **单账号行为逐字不变**。
    取不到 cookie 的账号**跳过**(不让一枚坏凭据挡住后面的)。

    ⚠️⚠️ **为什么必须集中在这里**(2026-10-16 用户口径「先跑通多账户轮换」):
    取这三个盘 cookie 的地方原本**八处各写一遍** —— 夸克 5 处
    (`wechat/_source._quark_cookie` / `pan_auto_clean` / `pan_discovery` /
    `quark_kouling` / `hotspot_agent`)、百度 3 处
    (`pan_auto_clean` / `pan_discovery` / `wechat/_enrich`)——**没有一处看池**
    ⇒ **账号登记了也不会被用**,"满了自动换下一个"是句空话。
    ⇒ 接池只需接这一处;调用方要"换下一个"就拿整个列表循环(见 `xunlei_group` 的择一循环)。

    ⚠️ **只给 `label` 的代价**(2026-11-13):要用 `scoped_path()` 按账号分家文件的
    调用方需要的是 **`cookie_key`**(路径就按它命名),拿 `label`(中文显示名)去
    还原 `cookie_key` 是**猜**——所以那种场景请用 `refs_for()`。
    """
    return [(r.label or r.cookie_key, ck) for r, ck in refs_for(
        session, platform, user_id=user_id)]


def refs_for(session, platform: str, *, user_id: int = 1) -> list[tuple[AccountRef, str]]:
    """★ 与 `cookies_for` 同一套择一/过滤,但**连同 `AccountRef` 一起给出**。

    需要"按账号分家"的调用方(清理快照 / fid 索引 / 缓存文件)必须用这个 ——
    文件名要用 `ref.cookie_key`,不能拿 `label` 去反推(`scoped_path` 的约定就是
    `cookie_key`,与 `pan_discovery._fid_store_for` 同一套;拿错就等于同一个号
    有两份索引,`FID_CHANGED` 那个坑会以另一种形式回来)。
    """
    from app.services.cookie_store import get_cookie

    out: list[tuple[AccountRef, str]] = []
    seen: set[str] = set()
    for r in ordered(session, platform, user_id=user_id):
        try:
            ck = str(get_cookie(session, r.user_id or user_id, r.cookie_key) or "").strip()
        except Exception:  # noqa: BLE001 - 解不开某一枚不该挡住后面的
            logger.debug("取账号 %s 的 cookie 失败", r.cookie_key, exc_info=True)
            ck = ""
        if not ck or ck in seen:
            continue
        seen.add(ck)
        out.append((r, ck))
    #: ★★ 2026-11-24:**停用的号不参与转存**(账号级封禁,见 `disable_account`)。
    #: ⚠️ **全停用时原样返回** —— 与下面百度那段同一取舍:调用方看到"没凭据"会把
    #: "账号被封/停用"误诊成"没配账号",而这两件事的修法完全不同。
    if out and any(r.disabled for r, _ in out):
        live = [(r, ck) for r, ck in out if not r.disabled]
        if live:
            skipped = [r.label for r, _ in out if r.disabled]
            logger.warning("账号池里有 %d 个账号**已停用**,已跳过:%s", len(skipped), skipped)
            out = live
        else:
            logger.error("账号池里 %s 的账号**全部停用**(%s)—— 仍原样返回,"
                         "请检查是被封还是配置错", platform,
                         [r.label for r, _ in out])
    #: ★ 百度:把**登录态已死**的账号召回后面(2026-10-31 实测:`baidupan#2/#3` 失效,
    #: 却因"最空"永远排第一 ⇒ 每个百度动作都 errno=-6,而主号是好的)。
    #: ⚠️ **只在还有活号时**才滤掉死号:全死就原样返回 —— 否则调用方会看到"没凭据",
    #: 把"cookie 过期"误诊成"没配置账号"。
    if platform == "baidupan" and len(out) > 1:
        alive = [(r, ck) for r, ck in out if _baidu_alive(ck)]
        if alive and len(alive) < len(out):
            #: ⚠️ `AccountRef` 是 dataclass(**不可哈希**),不能塞进 set 比 ——
            #: 这里按 `cookie_key`(它就等于账号身份)比。2026-11-13 我第一版写的就是
            #: `set(alive的ref)`,只要池里 ≥2 个百度号就 `TypeError` ⇒ 取号整条挂掉。
            _alive = {r.cookie_key for r, _ in alive}
            dead = [r.label for r, _ in out if r.cookie_key not in _alive]
            logger.warning("百度账号池里有 %d 个登录态失效,已跳过:%s", len(dead), dead)
            out = alive
    return out


def cookie_for(session, platform: str, *, user_id: int = 1) -> str:
    """按池取号的**单个**版本(现在最该用的那一个)。取不到回空串 —— 调用方自己回落到 `.env`。"""
    got = cookies_for(session, platform, user_id=user_id)
    return got[0][1] if got else ""


def scoped_path(base: str, cookie_key: str, *, platform: str) -> str:
    """把**共享的**缓存/索引文件路径**按账号分家**;默认号原样返回(单账号行为不变)。

    ★ 为什么必须有这一处(2026-11-13):这类文件的键都是**账号内**的标识 ——
    `quark_fid_store` 里是目录 fid,`quark_dup_cache` 里是家名+fid。
    两个号共用一份会互相作废:

    * fid_store:A 号的「目录不存在」墓碑(`invalidate_dir`)会把 B 号正确的映射删掉
      (2026-11-11 实测,见 `pan_discovery._fid_store_for`);
    * 扫描快照:两个号都有 `redian监听` 这个家,**同名不同 fid** ⇒
      `check_home` 每轮都判 `FID_CHANGED` ⇒ 两边轮流把对方的快照作废,谁都不收敛。
    """
    import os

    base = str(base or "")
    key = str(cookie_key or "")
    if not base or not key or key == platform:
        return base
    stem, ext = os.path.splitext(base)
    return f"{stem}.{key.replace('#', '_')}{ext}"
