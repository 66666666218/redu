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
    is_default: bool = False
    tried: list[str] = field(default_factory=list)

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
                              note=str(item.get("note") or "")))
    return out or [default_ref(platform, user_id=user_id)]


def ordered(session: Session, platform: str, *, user_id: int = 1) -> list[AccountRef]:
    """**该按什么顺序试** —— 最空的在前;满的排最后(全满时也要能说出是谁满)。"""
    return sorted(accounts(session, platform, user_id=user_id),
                  key=lambda r: (r.usable_hint, r.label))


def cookie_of(ref: AccountRef, session: Session) -> str:
    """该账号的凭据(cookie_store 里的加密 JSON;取不到返回空串 —— **不猜**)。"""
    from app.services.cookie_store import get_cookie

    return get_cookie(session, ref.user_id, ref.cookie_key) or ""


def report(session: Session, ref: AccountRef, *, ratio: float | None = None,
           full: bool | None = None, why: str = "") -> None:
    """把**这一号的探测结果**写回池子(择一就靠它;`full` 只对这种账号加冷静期)。

    ⚠️ 默认账号(`is_default`)**不写池** —— 单账号部署时池子是空的,别凭空写出一条
    "配置",那会让人以为池配过了。
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
        item["last_check_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        if full is True:
            item["full_until"] = (datetime.now() + timedelta(minutes=FULL_COOLDOWN_MIN)
                                  ).isoformat(sep=" ", timespec="seconds")
        elif full is False:
            item["full_until"] = ""
        if why:
            item["note"] = why[:120]
        break
    _save(session, data)


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
           "default_cookie_key", "FULL_COOLDOWN_MIN"]


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
    """
    from app.services.cookie_store import get_cookie

    out: list[tuple[str, str]] = []
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
        out.append((r.label or r.cookie_key, ck))
    return out


def cookie_for(session, platform: str, *, user_id: int = 1) -> str:
    """按池取号的**单个**版本(现在最该用的那一个)。取不到回空串 —— 调用方自己回落到 `.env`。"""
    got = cookies_for(session, platform, user_id=user_id)
    return got[0][1] if got else ""
