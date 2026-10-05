"""微信读书**额度**的单一事实来源 + 跨路径共享熔断(2026-10-05)。

## 为什么要有它 —— 这个模块是对着"同一类错误反复出现"建的

**症状**:同一个账号的额度,**被 6 条彼此不知道的路径一起花**:

| 路径 | 打什么 | 位置 |
|---|---|---|
| 监听轮 | cover / 网页列表 / App 列表 | `wechat/_listen.py::_weread_collect` |
| 文章同步 | 网页列表 | `wechat/_sync.py::_fetch` |
| 推荐全量同步 | 同上(走 sync) | `wechat/_ticks.py::run_full_sync_if_pending` |
| 续期 | `renewal` + `shelf` | `wechat/_source.py::refresh_weread_cookie` |
| 链路体检验活 | `shelf` | `chain_health._weread_alive` |
| 探针脚本 | 列表/书架 | `scripts/probe_weread_list.py` 等 |

**它们各自写各自的熔断**,于是:
- 监听轮里合了闸,**同步那条路照打不误** —— 闸门形同虚设;
- 2026-09-29 那次网页列表被账号级拦 `-2041`,**没有任何一条路径知道"别人已经被挡了"**,
  继续按原节奏打,**把风控越打越深**。

**判据也散着**:`_WEREAD_QUOTA_MARKS` 定义在 1200 行的 `_listen.py` 里,
别的路径要用就得 import 那个重模块 ⇒ 于是 `_sync` 那边**干脆没判额度**。

## 这个模块提供什么

1. **判据唯一**:`is_quota_error` / `is_auth_error` —— 全仓只此一份;
2. **跨路径共享的熔断**:被挡一次就写库(`system_config`),**所有路径**在熔断期内
   **连请求都不发**(而不是发出去再失败 —— 失败本身就会加深风控);
3. **`call()` 统一入口**:所有打微信读书的地方都经它,于是"被挡"永远是个**显式信号**,
   不可能再被某条路径悄悄吞成空结果(本仓那条母题)。

⚠️ **为什么不放在 `_listen.py` 里**:那个文件 1200 行、是监听专用的;
额度是**跨模块的公共资源**,判据与熔断不该住在某一个调用方家里 ——
住进去的结果就是这次这样:别的调用方嫌重、干脆不 import。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

# 额度类错误码 —— **全仓唯一一份**。含义:
#   `-2014` 频率额度 / `-2041` 会话列表预算耗尽或被拦 / `-10100` 列表额度边界
QUOTA_MARKS = ("-2014", "-2041", "-10100")
# 登录态类:处置是"去重新取凭据/续期",与额度**完全不同**,必须分得开
AUTH_MARKS = ("-2012", "-2010")

_BLOCK_KEY = "weread_blocked_{uid}_{scope}"
# ⚠️ **熔断要按通道分开**(2026-10-05):网页(cookie)与 App(accessToken)**是两套
# 独立的鉴权与配额** —— 网页被 `-2041` 拦了,App 那边照样能取(App 兜底的价值就在这);
# 反过来同理。**合成一个 key 的话,"网页被挡"会连带把唯一还能用的 App 路也停掉。**
# ⚠️ **粒度是"端点类",不是"通道"**(2026-10-05 定):实测 `-2041` 打的是**列表预算**,
# 同一时刻 `shelf` 与 cover **都好好的** ⇒ 拿列表的错误去停 cover 会**误伤一大片**
# (cover 是每个号每轮的保底源,停了就等于整轮瞎)。所以按"哪个接口的额度"分开记,
# 而不是按"哪个通道"。今天是这条测试当场把它逼出来的。
SCOPE_WEB_LIST = "web_list"    # 网页 /web/mp/articles(会话列表预算)
SCOPE_APP_LIST = "app_list"    # App i.weread.qq.com/book/articles(另一套鉴权/配额)
# 向后兼容别名(默认通道 = 网页列表)
SCOPE_WEB = SCOPE_WEB_LIST
SCOPE_APP = SCOPE_APP_LIST
DEFAULT_BLOCK_MIN = 30          # 被挡后默认冷静多久


def is_quota_error(exc: BaseException | str) -> bool:
    """是不是"再问也不给、还会把风控加深"的额度类错误。"""
    text = str(exc)
    return any(m in text for m in QUOTA_MARKS)


def is_auth_error(exc: BaseException | str) -> bool:
    """是不是登录态问题(该去续期/重取凭据,而不是等冷静)。"""
    text = str(exc)
    return any(m in text for m in AUTH_MARKS)


class Blocked(RuntimeError):
    """熔断中 —— **请求根本没发出去**。

    单独一类,好让调用方把"我们主动不发"与"发出去了被拒"分开:
    前者是保护措施(不该报警),后者才是风控信号(该报警)。
    """


def blocked_until(session: Session, user_id: int,
                  scope: str = SCOPE_WEB) -> datetime | None:
    """当前熔断到什么时刻;没熔断返回 `None`。`scope` 见文件头(两套鉴权分开计)。"""
    from app.db.models import SystemConfig

    try:
        row = session.scalar(select(SystemConfig).where(
            SystemConfig.key == _BLOCK_KEY.format(uid=user_id, scope=scope)))
        if not row or not row.value:
            return None
        until = datetime.fromisoformat(str(row.value))
        return until if until > datetime.now() else None
    except Exception:  # noqa: BLE001 - 读不到就当没熔断(宁可多打一次,也别把整轮卡死)
        logger.debug("读额度熔断状态失败", exc_info=True)
        return None


def note_blocked(session: Session, user_id: int, *, why: str,
                 minutes: int = DEFAULT_BLOCK_MIN, scope: str = SCOPE_WEB) -> None:
    """记一次"被额度挡下" ⇒ **所有路径**在接下来 N 分钟内都不再打这个账号。

    ⚠️ **必须写库而不是存内存**:写内存只有当前进程/当前路径知道,
    而额度是账号级的、被多条路径共享 —— 这正是之前熔断失效的原因。
    """
    from app.db.models import SystemConfig

    until = datetime.now() + timedelta(minutes=minutes)
    try:
        row = session.scalar(select(SystemConfig).where(
            SystemConfig.key == _BLOCK_KEY.format(uid=user_id, scope=scope)))
        if row:
            row.value = until.isoformat()
        else:
            session.add(SystemConfig(key=_BLOCK_KEY.format(uid=user_id, scope=scope),
                                     value=until.isoformat()))
        session.commit()
        logger.warning("微信读书额度被挡[%s](%s)⇒ 该通道全路径冷静到 %s", scope, why[:80],
                       until.strftime("%H:%M"))
    except Exception:  # noqa: BLE001 - 记不上就算了,不能连带采集
        logger.warning("记额度熔断失败:%s", why[:80], exc_info=True)


def clear(session: Session, user_id: int, scope: str = SCOPE_WEB) -> None:
    """手动解除熔断(凭据续期/重取之后调 —— 换了一把会话,额度是新账)。"""
    from app.db.models import SystemConfig

    try:
        row = session.scalar(select(SystemConfig).where(
            SystemConfig.key == _BLOCK_KEY.format(uid=user_id, scope=scope)))
        if row:
            row.value = ""
            session.commit()
    except Exception:  # noqa: BLE001
        logger.debug("清除额度熔断失败", exc_info=True)


def call(session: Session, user_id: int, fn: Callable[[], Any], *,
         what: str = "", block_min: int = DEFAULT_BLOCK_MIN,
         scope: str = SCOPE_WEB) -> Any:
    """**打一次微信读书接口的统一入口**。

    - 熔断中 ⇒ 抛 `Blocked`,**请求根本不发**(省额度、不加深风控);
    - 被额度挡 ⇒ 记熔断(全路径生效)后**照实抛出**(不吞成空 —— 吞掉就是假成功);
    - 登录态错 ⇒ 原样抛(交调用方续期;**不**记额度熔断,处置完全不同)。

    ⚠️ `Blocked` 与"调用失败"必须能被上层分开:前者是**我们主动不发**(保护),
    后者是**发出去了被拒**(风控信号,该报警)。
    """
    until = blocked_until(session, user_id, scope)
    if until is not None:
        raise Blocked(f"{what or '微信读书'}:额度熔断中(至 {until:%H:%M}),本次不发请求")
    try:
        return fn()
    except BaseException as exc:
        if is_quota_error(exc):
            note_blocked(session, user_id, why=f"{what}:{exc}", minutes=block_min,
                         scope=scope)
        raise


def snapshot(session: Session, user_id: int) -> dict:
    """给链路体检/运维看的一眼状态(**两条通道各报各的**)。"""
    out = {}
    for scope in (SCOPE_WEB_LIST, SCOPE_APP_LIST):
        until = blocked_until(session, user_id, scope)
        out[scope] = {"blocked": until is not None,
                      "until": until.isoformat(sep=" ", timespec="seconds") if until else ""}
    return out
