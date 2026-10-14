# -*- coding: utf-8 -*-
"""小红书:**统一账号池**(2026-10-12)。

## 为什么要有它
之前多账号轮换只活在**页面渲染**那条路里(`xhs_page_source` 自己一套游标 + "需验证"名单),
而**协议路**(现在的主路)只读**单枚** cookie(`cookie_store` 或 `.env`)——
两条路各管各的,健康状态不互通。后果:

- 协议路撞上 `-104`/`-100` 时**没有下一个账号可换**,只能整轮失败;
- 页面路好不容易标记出的"这号别用了",协议路**不知道**,照撞。

本模块把它统一成**一个账号池 + 一台状态机**,两条路都从这里取号、都往这里报结果。

## 状态机(每一种都对应一个**不同的修法**,不能混)
| 状态 | 触发 | 修法 | 冷却 |
|---|---|---|---|
| `healthy` | 取到数 | 继续用 | 0 |
| `guest` | `web_session` 前缀 `03` | **重登**(游客档拿不到数) | 5 分钟 |
| `expired` | `-100 登录已过期` | 刷新 cookie / 重登 | 15 分钟 |
| `restricted` | `-104` / `300011` | **等解封**(改代码没用) | 6 小时 |
| `need_verify` | 页面跳安全验证 | 人过一次验证 | 12 小时 |
| `rate_limited` | `请勿频繁操作` | **等**(重试会推后冷却) | 3 小时 |
| `unknown` | 还没用过 | 试一次 | 0 |

⚠️ **`guest` 与 `restricted` 必须分开**:前者是"压根没登录"(重登就好),
后者才可能是账号被限。混在一起就会把"没登录"误读成"被风控"——
本仓在这句上栽过好几天。

## 账号来源(自动发现)
1. `data/xhs_session_<档案>.json` —— `tools/xhs_browser_login.py` 导出的**成套**凭据(cookie+ssk)
2. **cookie 库**里各租户的 `xiaohongshu` 条目
3. `.env` 的 `XHS_WEB_SESSION`(**兜底**,标 `env`)

## 用法
```python
from app.services import xhs_accounts as A

# —— 协议路:只要"有 cookie 串"的来源 ——
ref = A.pick(kinds=A.COOKIE_KINDS)   # 挑一个当前最该用的
if ref is None: ...                  # 全在冷却里
ck = A.cookie_of(ref)                # 拿它的 cookie 串
A.report(ref, A.STATE_RESTRICTED, "code=-104")   # 用完把结果报回去

# —— 页面路:按**档案目录**进出(见 `report_profile`) ——
A.report_profile("data/xhs_2", A.STATE_RATE_LIMITED, "请勿频繁操作")
A.cooling_profiles()                 # 现在哪些档案在冷却(带时刻的退避)
A.profile_cooldowns()                # {档案: 剩余分钟}(给体检/看板)
A.harmful_cooldowns()                # 只挑"越试越糟"的那种(全都在里面时**不该硬试**)
A.need_verify_profiles()             # 哪些档案要人去过一次验证
```
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

# ------------------------------------------------------------------ 状态
STATE_HEALTHY = "healthy"
STATE_GUEST = "guest"
STATE_EXPIRED = "expired"
STATE_RESTRICTED = "restricted"
STATE_NEED_VERIFY = "need_verify"
STATE_RATE_LIMITED = "rate_limited"
STATE_UNKNOWN = "unknown"

ALL_STATES = (STATE_HEALTHY, STATE_GUEST, STATE_EXPIRED, STATE_RESTRICTED,
              STATE_NEED_VERIFY, STATE_RATE_LIMITED, STATE_UNKNOWN)

#: 各状态的**冷却时长(秒)**。0 = 不限用(只是可能取不到数)。
#: ⚠️ 这些数字是"别越撞越糟"的护栏,不是精确科学;`restricted`/`rate_limited`
#: 取偏长是**故意的**——本仓有过"在已风控的号上一小时跑好几轮测试"的教训。
COOLDOWN_S: dict[str, int] = {
    STATE_HEALTHY: 0,
    STATE_UNKNOWN: 0,
    STATE_GUEST: 300,            # 5 分钟:够人去重登,又不至于把号晾死
    STATE_EXPIRED: 900,          # 15 分钟
    STATE_RATE_LIMITED: 3 * 3600,
    STATE_RESTRICTED: 6 * 3600,
    STATE_NEED_VERIFY: 12 * 3600,
}

#: 需要**人动手**的状态(供告警用)
NEEDS_HUMAN = (STATE_GUEST, STATE_EXPIRED, STATE_RESTRICTED, STATE_NEED_VERIFY)

#: **「重试会把事情弄坏」**的状态 —— 它们进了冷却就**绝不能靠"硬试一轮"绕过**。
#: ⚠️ 这是本仓用真账号换来的教训:**在已被风控的账号上反复重试,冷却会被推后**
#: (服务端按账号/IP 记账 ⇒ 越试越久)。所以只有这几种状态才值得"宁可不试"。
#: 反过来 `need_verify`(要人去过一次)**不在**这里:硬试一轮虽然没用,但**不会更糟**。
HARMFUL_STATES = (STATE_RATE_LIMITED, STATE_RESTRICTED)

_KEY = "xhs_account_pool"

#: 池里**浏览器档案**那类账号的 key 前缀(后接**按仓库根解析的绝对路径**)。
#: 为什么单独一个前缀:档案身份就是"一个目录",而它跟 cookie 库里那种
#: "某租户的一条 cookie"是**两种东西** —— 前缀一分开,`pick(kinds=...)` 就能
#: 精确地"协议路只取有 cookie 的、页面路只取有档案的",不会再互相污染状态。
PROFILE_PREFIX = "profile:"

#: **能直接拿到 cookie 串**的来源 —— 协议路只该用这些。
#: ⚠️ `profile_dir`(纯浏览器档案)**不在其中**:它只有登录态在 Chromium 里,
#: `cookie_of()` 对它返回空 —— 让协议路取到它 = 每轮白报一次失败、
#: 还把"这个档案"标脏(它其实没坏,只是**协议路用不了**)。这是本仓很容易踩的坑。
COOKIE_KINDS = ("session_file", "cookie_store", "env")

#: **有浏览器档案**的来源 —— 页面渲染路只该用这些。
BROWSER_KINDS = ("profile_dir", "session_file")


@dataclass
class AccountRef:
    """池里一个账号的**引用**(身份 + 怎么取它的 cookie)。"""

    key: str                      #: 归一化身份(去重用)
    label: str = ""               #: 给人看的名字
    kind: str = "unknown"         #: `session_file` / `cookie_store` / `env`
    profile: str = ""             #: 浏览器档案(有就带上,便于"去重登")
    user_id: int = 0              #: cookie 库里的租户(>0 才有意义)
    path: str = ""                #: session 文件路径(kind=session_file 时)
    state: str = STATE_UNKNOWN
    detail: str = ""
    ok: int = 0
    fail: int = 0
    last_used: float = 0.0
    cooldown_until: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def available(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.cooldown_until

    def as_row(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in
             ("key", "label", "kind", "profile", "user_id", "path", "state",
              "detail", "ok", "fail", "last_used", "cooldown_until")}
        d["extra"] = self.extra
        return d


# ------------------------------------------------------------------ 持久化

def _load_raw() -> dict[str, dict]:
    try:
        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import SystemConfig

        with get_session_local()() as db:
            row = db.scalar(select(SystemConfig).where(SystemConfig.key == _KEY))
            if row is None or not row.value:
                return {}
            got = json.loads(row.value)
            if not isinstance(got, dict):
                return {}
            return _merge_aliases(got)             #: 见 `_canon` —— 孪生身份共用状态
    except Exception:  # noqa: BLE001 - 读不到就当空的,不该让采集停摆
        logger.debug("读账号池失败(当空处理)", exc_info=True)
        return {}


def _save_raw(data: dict[str, dict]) -> bool:
    try:
        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import SystemConfig

        blob = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with get_session_local()() as db:
            row = db.scalar(select(SystemConfig).where(SystemConfig.key == _KEY))
            if row is None:
                db.add(SystemConfig(key=_KEY, value=blob))
            else:
                row.value = blob
            db.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.debug("写账号池失败(状态本轮不持久化)", exc_info=True)
        return False


# ------------------------------------------------------------------ 发现

def _root() -> Path:
    return Path(__file__).resolve().parents[2]


#: 会话文件命名(见 `tools/xhs_browser_login.py`):`data/xhs_session_<档案名>.json`
_SESSION_PREFIX = "xhs_session_"


def _canon(key: str) -> str:
    """★ **同一个账号的两种身份 ⇒ 归到一个 key**(2026-10-12 修)。

    一个物理账号在池里有**两个入口**,状态却是**各记各的**:

      ① `file:<data/xhs_session_<prof>.json>`(kind=`session_file`)—— 协议路用
      ④ `profile:<data/<prof>>`(kind=`profile_dir`)        —— 页面路用

    命名规则(登录脚本定的)保证 ① 就是 ④ 那枚账号导出来的凭据 ⇒ **它们是同一个号**。

    ## 不修会怎样(这就是换号对照实验里踩到的)
    页面路把 `data/xhs_new` 打成 `need_verify`(账号级风控,冷却 712 分),
    可协议路看到的孪生兄弟还是 `unknown` ⇒ 照取不误、照发请求。
    而记忆里那条教训是:**在已被风控的号上重试,冷却会被推后** ⇒ 等于自己把号往死里踩。

    ⚠️ 之所以拿 **`profile:` 当正主**:档案目录是**本体**(登录态住在 Chromium 里),
    会话 json 只是它导出来的**快照**(每次重登会被覆盖)。拿快照当主键,下次重登就失忆了。

    只在**档案目录确实存在**时才归并 —— 否则一个纯导出的会话文件(没有配对档案)
    仍按自己的 key 记,不会莫名其妙并到别人名下。
    """
    k = str(key or "")
    if not k.startswith("file:"):
        return k
    fp = Path(k[len("file:"):])
    prof = fp.stem.replace(_SESSION_PREFIX, "", 1)
    if not prof or prof == fp.stem:
        return k                                   #: 不是这套命名 ⇒ 不归并
    try:
        d = (_root() / "data" / prof).resolve()
    except Exception:  # noqa: BLE001
        return k
    if not d.is_dir():
        return k                                   #: 没有配对档案 ⇒ 各记各的
    return f"{PROFILE_PREFIX}{d}"


def _merge_aliases(data: dict[str, dict]) -> dict[str, dict]:
    """把**旧数据里的别名行**并到正主上(一次性迁移,幂等)。

    规则:正主行已存在 ⇒ **正主说了算**,别名行丢掉(它多半是归并前的旧快照);
    正主行还没有 ⇒ 别名行**整个搬过去**(保住历史冷却,别让人白等)。
    """
    out: dict[str, dict] = {}
    for k, row in data.items():
        c = _canon(k)
        if c == k:
            out[k] = row
            continue
        if c in out:                               #: 正主已在这一批里 ⇒ 丢别名
            continue
        out.setdefault(c, row)                     #: 正主还没 ⇒ 搬过去
    return out


def discover(settings=None) -> list[AccountRef]:
    """**自动发现**所有可用账号(不读它们的 cookie,只登记身份)。"""
    if settings is None:
        from config.settings import get_settings
        settings = get_settings()

    out: list[AccountRef] = []
    seen: set[str] = set()

    def _add(ref: AccountRef) -> None:
        """★ 按**规范化 key** 去重 —— 同一个账号的两个入口只登记一次。

        ⚠️ 为什么必须在这里就并掉:凭据文件(`file:<会话 json>`)与浏览器档案
        (`profile:<档案目录>`)是**同一个物理账号的两个入口**(见 `_canon`)。
        若两条都登记:
          · 体检里同一个档案**出现两遍**(「✅ 可直接用:2 个 —— 档案 xhs_4, 档案 xhs_4」);
          · 冷却/状态会被**数两遍**(「只在等 2 个」其实是同一个号在等)。
        ⇒ 谁先来谁占位,后来的那条**并入**先到那条的身份(状态本来就共用一张表)。
        """
        if not ref.key:
            return
        ck = _canon(ref.key)
        if ck in seen:
            return
        seen.add(ck)
        out.append(ref)

    #: ① 成套凭据文件(浏览器登录导出的,最靠谱) —— 优先
    for fp in sorted((_root() / "data").glob("xhs_session_*.json")):
        prof = fp.stem.replace("xhs_session_", "")
        _add(AccountRef(key=f"file:{fp.resolve()}", label=f"档案 {prof}",
                        kind="session_file", profile=f"data/{prof}", path=str(fp)))

    #: ② cookie 库里各租户的条目
    try:
        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import UserCookie

        with get_session_local()() as db:
            rows = db.scalars(select(UserCookie).where(
                UserCookie.platform == "xiaohongshu")).all()
            for r in rows:
                _add(AccountRef(key=f"user:{r.user_id}", label=f"租户 {r.user_id}",
                                kind="cookie_store", user_id=int(r.user_id)))
    except Exception:  # noqa: BLE001
        logger.debug("读 cookie 库账号失败", exc_info=True)

    #: ③ `.env` 兜底(**排最后**:它常是一枚"孤儿"——没有配对 ssk)
    if str(getattr(settings, "xhs_web_session", "") or "").strip():
        _add(AccountRef(key="env", label=".env(兜底)", kind="env"))

    #: ④ 配置了但还没登录的档案也登记上(便于"提示去登它")
    raw = str(getattr(settings, "xhs_browser_profiles", "") or "").strip()
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            p = Path(part)
            p = p if p.is_absolute() else _root() / p
            _add(AccountRef(key=f"profile:{p.resolve()}", label=f"档案 {p.name}",
                            kind="profile_dir", profile=str(p)))

    #: 合并已存的健康状态
    saved = _load_raw()
    for ref in out:
        s = saved.get(_canon(ref.key))             #: 孪生身份读同一份状态(见 `_canon`)
        if not s:
            continue
        for k in ("state", "detail", "ok", "fail", "last_used", "cooldown_until"):
            if k in s:
                setattr(ref, k, s[k])
        ref.extra = s.get("extra") or {}
    return out


# ------------------------------------------------------------------ 选号

def pick(settings=None, *, exclude: set[str] | None = None,
         kinds: tuple[str, ...] | None = None,
         now: float | None = None) -> AccountRef | None:
    """挑一个**当前最该用**的账号。

    排序(**先按状态好坏,再按"最久没用"**):
      healthy > unknown > guest/expired > need_verify/rate_limited/restricted
    冷却中的一律跳过。全都在冷却里 ⇒ 返回 `None`(调用方据此"整轮跳过"而不是硬撞)。

    `kinds`:只要某几类来源(如协议路传 `COOKIE_KINDS`、页面路传 `BROWSER_KINDS`)。
    传 `None` = 不筛。⚠️ **筛掉之后若一个都不剩,也回 `None`** —— 调用方据此报
    "没有可用凭据"而不是硬撞一个取不出 cookie 的东西。

    ⚠️ **`unknown` 排在 `guest` 前面**是有意的:没用过的号值得试一次,
    而已知是游客档的号再试也是白试。
    """
    now = now or time.time()
    refs = discover(settings)
    cands = [r for r in refs
             if r.key not in (exclude or set()) and r.available(now)
             and (kinds is None or r.kind in kinds)]
    if not cands:
        return None

    #: ★ 账号级封禁:一组孪生档案里**任何一张**还红着 ⇒ 整组都不能用
    #: (见 `account_state_map` —— 风控按账号记账,不按档案目录记账)
    blocked = {t for t, g in account_state_map(settings, now, refs=refs).items()
               if g["blocked"]}
    if blocked:
        kept = [r for r in cands if _tag_of_ref(r) not in blocked]
        if not kept:
            logger.warning(
                "小红书账号池:候选 %d 个全部属于**已被风控的账号**(账号段 %s)"
                " ⇒ 本轮不取号。**不要**靠换档案/重登绕过去 —— 那会给同一个号记账",
                len(cands), ", ".join(sorted(t[6:] for t in blocked))[:120])
            return None
        if len(kept) != len(cands):
            logger.info("小红书账号池:跳过 %d 个孪生档案(与风控中的账号是同一个号)",
                        len(cands) - len(kept))
        cands = kept

    cands.sort(key=lambda r: (_STATE_RANK.get(r.state, 9), r.last_used or 0))
    return cands[0]


# ------------------------------------------------------------------ 报结果

def report(ref: AccountRef | str, state: str, detail: str = "", *,
           settings=None) -> None:
    """把**一次使用的结果**报回池里 —— 状态机与冷却都在这里生效。

    `ref` 可以是 `AccountRef` 或它的 `key`(方便调用方只留一个字符串)。
    """
    key = _canon(ref.key if isinstance(ref, AccountRef) else str(ref))
    if not key:
        return
    if state not in ALL_STATES:
        logger.warning("账号池:未知状态 %r(按 unknown 记)", state)
        state = STATE_UNKNOWN

    data = _load_raw()
    row = data.get(key) or {}
    now = time.time()
    row["state"] = state
    row["detail"] = str(detail)[:300]
    row["ok"] = int(row.get("ok", 0)) + (1 if state == STATE_HEALTHY else 0)
    row["fail"] = int(row.get("fail", 0)) + (0 if state == STATE_HEALTHY else 1)
    row["last_used"] = now
    cd = COOLDOWN_S.get(state, 0)
    #: 好结果**清掉**旧冷却;坏结果才按状态设冷却
    row["cooldown_until"] = now + cd if cd else 0.0
    if isinstance(ref, AccountRef):
        row["label"] = ref.label
        row["kind"] = ref.kind
        row["extra"] = ref.extra
        #: `path` / `user_id` 一并存:只存在于库里、当前配置已查不到的行(孤儿)
        #: 要靠它们才能还原成能取到 cookie 的引用(见 `_ref_from_row`)。
        row["path"] = ref.path
        row["user_id"] = int(ref.user_id or 0)
    data[key] = row
    _save_raw(data)

    if state == STATE_HEALTHY:
        logger.info("小红书账号池:%s → 健康", key)
    else:
        logger.warning("小红书账号池:%s → %s(%s;冷却 %d 分钟)", key, state,
                       str(detail)[:80], cd // 60)


def from_error(kind: str, msg: str = "") -> str:
    """从协议路抛出的错误(`XhsProtocolError.kind`)映射到账号池状态。

    这是"把两条路的判据对齐"的那一层 —— 免得每个调用点各写一遍 if。
    """
    k = str(kind or "").lower()
    if k == "restricted":
        return STATE_RESTRICTED
    if k == "need_login":
        #: ⚠️ `need_login` 既可能是"游客档"也可能是"过期" —— 看文案区分:
        #: 说"已过期"是 expired,否则按 guest(要靠重登而不是刷新)
        return STATE_EXPIRED if "过期" in msg else STATE_GUEST
    if k in ("rate_limited", "busy"):
        return STATE_RATE_LIMITED
    if k == "sign":
        return STATE_UNKNOWN       # 签名问题不是账号问题,别赖到账号上
    return STATE_UNKNOWN


def cookie_of(ref: AccountRef, settings=None, session=None) -> str:
    """取这个账号的 cookie 串。取不到回空(**不抛异常**,让调用方换下一个)。"""
    try:
        if ref.kind == "session_file" and ref.path:
            got = json.loads(Path(ref.path).read_text(encoding="utf-8"))
            ck = got.get("cookies") or {}
            return "; ".join(f"{k}={v}" for k, v in ck.items() if v)
        if ref.kind == "cookie_store" and ref.user_id:
            from app.db import get_session_local
            from app.services.cookie_store import get_cookie

            if session is not None:
                return str(get_cookie(session, ref.user_id, "xiaohongshu") or "")
            with get_session_local()() as db:
                return str(get_cookie(db, ref.user_id, "xiaohongshu") or "")
        if ref.kind == "env":
            if settings is None:
                from config.settings import get_settings

                settings = get_settings()
            return str(getattr(settings, "xhs_web_session", "") or "")
    except Exception:  # noqa: BLE001
        logger.debug("取账号 cookie 失败:%s", ref.key, exc_info=True)
    return ""


def ssk_of(ref: AccountRef) -> bytes:
    """取这个账号的 `ssk`(与 cookie 必须同源)。取不到回空。"""
    try:
        if ref.kind == "session_file" and ref.path:
            got = json.loads(Path(ref.path).read_text(encoding="utf-8"))
            b64 = str(got.get("ssk") or "")
            if b64:
                import base64

                return base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        logger.debug("取账号 ssk 失败:%s", ref.key, exc_info=True)
    return b""


def _session_meta(ref: AccountRef) -> dict:
    """读 `kind=session_file` 那份 JSON 的**元数据**(不抛异常,读不到回空 dict)。"""
    if ref.kind != "session_file" or not ref.path:
        return {}
    try:
        got = json.loads(Path(ref.path).read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001
        logger.debug("读会话存档失败:%s", ref.key, exc_info=True)
        return {}


def ua_of(ref: AccountRef) -> str:
    """★ 取这个账号**登录时的真实 UA**(2026-10-12)。

    为什么必须按**账号**取,而不是全局一个常量:
    会话是**某个浏览器**签发的 —— 用 Edge 154 登的号,采集时却自称 Chrome 138,
    服务端直接判"登录设备 ≠ 调用设备"(实测 `300011 当前账号存在异常`)。
    所以指纹必须**跟着凭据走**,与 `cookie_of` / `ssk_of` 同源。

    读不到(老存档还没重登过)回空串 —— 调用方应退回
    `xhs_fingerprint.DEFAULT_UA`,**不要**在这里兜底(免得两边各兜一份、口径打架)。
    """
    return str(_session_meta(ref).get("ua") or "").strip()


def egress_of(ref: AccountRef) -> str:
    """取这个账号**登录时记下的出口 IP**(供 `tools/xhs_egress.py` 比对"是不是同一个出口")。"""
    return str(_session_meta(ref).get("egress_ip") or "").strip()


def hints_of(ref: AccountRef) -> dict[str, str]:
    """取这个账号**登录时抓的 `sec-ch-ua*` 客户端提示头**。

    与 `ua_of` 同理:真实浏览器发请求时一定带这组头,而我们原来一个都不发 ⇒
    自称浏览器却拿不出浏览器该有的头。⇒ 同样**从登录浏览器抓来存着,采集时原样带**。

    抓不到/老存档 ⇒ 回 `{}`,调用方**不发**(宁可缺,也不编一份)。
    """
    got = _session_meta(ref).get("client_hints")
    if not isinstance(got, dict):
        return {}
    return {str(k): str(v) for k, v in got.items() if k and v}


def fingerprint_of(ref: AccountRef, session=None) -> tuple[str, dict[str, str]]:
    """★ 这个账号的 **`(ua, sec-ch-ua*)`** —— **存档优先,库里兜底**。

    ## 为什么需要"兜底"这一半(2026-10-14 定位)
    `ua_of` / `hints_of` 只认 `data/xhs_session_<档案>.json` 那种**成套存档**。
    而**租户那条路**(cookie 写进加密库的那条,也正是池里唯一标记可用的那个)
    过去**没有任何地方记指纹** ⇒ 拿到的 UA 是空串 ⇒ 协议路退回硬编码的
    `DEFAULT_UA`(`Chrome/138`)。今晚这枚会话是 **Edge** 签发的(证据:cookie 里
    带着 `pglt-edgeChromium-*`),发 `Chrome/138` 正是本项目已定性过的
    「登录设备 ≠ 调用设备」(`300011`)—— 只是从存档那条路换到了租户这条路。

    ⇒ 登录/导出时把指纹**跟着 cookie 一起存**(`save_fp`),这里**先读账号自己的存档**,
    存档里没有(租户那种)才回落到按用户存的这份。**优先级只此一处写死**,别在调用点各判一次。
    """
    ua, hints = ua_of(ref), hints_of(ref)
    if ua or hints or session is None:
        return ua, hints
    got = load_fp(session, ref.user_id)
    return str(got.get("ua") or ""), dict(got.get("hints") or {})


def _fp_key(user_id: int) -> str:
    return f"xhs_fp_{user_id}"


def save_fp(db, user_id: int, ua: str, hints: dict | None = None) -> None:
    """把**这份凭据所属浏览器**的指纹与 cookie **成对**存起来(存 `system_config`)。

    ⚠️ 与 `xhs_xys.save_ssk` 同一条规矩:**每一次重登/重导都要覆盖写**。
    只写一半(cookie 换了、指纹还是上一个浏览器的)比不写更糟 ——
    它会让"登录设备 ≠ 调用设备"从一个**能被发现的空值**变成一份**看起来合理的错值**。
    ⚠️ 空 UA **不写**:宁可让协议路按"没有指纹"处理(它会退回常量并留痕)。
    """
    ua = str(ua or "").strip()
    if not ua:
        return
    from sqlalchemy import select

    from app.db.models import SystemConfig

    payload = json.dumps({"ua": ua, "hints": dict(hints or {})}, ensure_ascii=False)
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == _fp_key(user_id)))
    if row is None:
        db.add(SystemConfig(key=_fp_key(user_id), value=payload))
    else:
        row.value = payload


def load_fp(session, user_id: int) -> dict:
    """读回 `{"ua": str, "hints": {}}`;**读不到回 `{}`**(不抛 —— 调用方自己兜)。"""
    try:
        from sqlalchemy import select

        from app.db.models import SystemConfig

        row = session.scalar(select(SystemConfig).where(SystemConfig.key == _fp_key(user_id)))
        if row is not None and row.value:
            got = json.loads(str(row.value))
            if isinstance(got, dict):
                return got
    except Exception:  # noqa: BLE001 - 读不到就当没有
        logger.debug("读小红书指纹失败(按没有处理)", exc_info=True)
    return {}


# ------------------------------------------------------------------ 档案(浏览器路径)
#: 页面渲染路(`xhs_page_source`)是**按目录**工作的 —— 它要的不是 cookie 串,
#: 而是一个**能开起来的浏览器档案目录**。下面这几个函数就是那一路进出池子的门:
#: 把"一个档案目录"映射成池里的 key、报结果、以及读"哪些档案在冷却/要人处理"。
#: 这样**两条路共用一台状态机**成为事实,而不是只有文档里写着。

def profile_key(profile) -> str:
    """把一个**档案目录**映射成池里的 key(`profile:<按仓库根解析的绝对路径>`)。

    ⚠️ 相对路径按**仓库根**解析 —— 与 `discover()` 来源④**同一套**;
    否则"配置里写的 `data/xhs_2`"与"调用方手上的 `Path('data/xhs_2')`"
    会算成两个身份(取决于进程 CWD),状态就串不起来了。
    """
    p = Path(profile)
    if not p.is_absolute():
        p = _root() / p
    try:
        p = p.resolve()
    except Exception:  # noqa: BLE001 - 归不了就当原样,别让记录本身崩
        pass
    return PROFILE_PREFIX + str(p)


def report_profile(profile, state: str, detail: str = "") -> None:
    """把**页面渲染路**一次使用的结果报回池里(与协议路写的是同一张表)。"""
    p = Path(profile)
    report(AccountRef(key=profile_key(profile), label=f"档案 {p.name}",
                      kind="profile_dir", profile=str(profile)), state, detail)


def account_tag(cookie_or_ws: str) -> str:
    """★ 从 cookie 串(或直接的 `web_session` 值)抽出**账号标识**。

    ## 为什么要这个(2026-10-12 用一次失败验证换来的)
    以为"换了号",其实**登的还是同一个号** —— 这种事光看档案名**看不出来**
    (`xhs_2` 与 `xhs_3` 是两个目录、`a1` 设备指纹也不同、会话也是新签发的,
    看起来完全是"另一个号")。可小红书的 `web_session` **中间那段编码了账号**:

        xhs_2   : 040069 b7366765643da33 4f0f9384b0899c555
        xhs_3   : 040069 b7366765643da32 bbaf9384b46d6fa68   ← 同一段 ⇒ **同一个号**
        xhs_new : 040069 8eff879e57bfcf1f f7f9384b95bb1885   ← 不同段 ⇒ 另一个号

    后果非常实在:**多账号轮换是本项目的核心降险手段**(风控是账号级的)。
    若池里几个"账号"其实是同一个号,轮换**一点用都没有**,
    而且会让人得出"换号也救不回来 ⇒ 是 IP 问题"这种**错误结论** —— 实际上根本没换。

    ⚠️ 只认 `04` 前缀(真登录);游客档 `03` 没有稳定标识 ⇒ 返回空(宁可不说,也别乱猜)。
    """
    v = str(cookie_or_ws or "")
    ws = v
    if "=" in v:                                   #: 传进来的是整串 cookie
        for part in v.split(";"):
            k, _, val = part.partition("=")
            if k.strip() == "web_session":
                ws = val.strip()
                break
    else:
        ws = v.strip()
    if len(ws) < 20 or not ws.startswith("04"):
        return ""
    return ws[:20]                                 #: 前 20 位:版本号 + 账号段


def account_groups(settings=None) -> dict[str, list[str]]:
    """`{账号标识: [档案…]}` —— **只返回"一个标识对应多个档案"的组**。

    空字典 = 池里每个档案都是**不同**的号(健康状态)。
    非空 = 列出的这几组是**同一个号被登记成了多个身份** ⇒ 轮换对它们无效。
    """
    from collections import defaultdict

    groups: dict[str, list[str]] = defaultdict(list)
    for r in discover(settings):
        try:
            ck = cookie_of(r)
        except Exception:  # noqa: BLE001 - 某一个取不到 cookie 不该让体检崩
            continue
        tag = account_tag(ck)
        if tag:
            groups[tag].append(r.label or r.key)
    return {t: xs for t, xs in groups.items() if len(xs) > 1}


def _tag_of_ref(r: AccountRef) -> str:
    """安全取某个引用的**账号标识**;取不到就返回空(不因此让选号/体检崩)。"""
    try:
        return account_tag(cookie_of(r))
    except Exception:  # noqa: BLE001 - 单个号取不到 cookie 不该拖垮整轮
        return ""


#: 状态从好到坏(数字越大越糟)—— 聚合"一组孪生档案"时取**最坏**的那个
_STATE_RANK = {STATE_HEALTHY: 0, STATE_UNKNOWN: 1, STATE_GUEST: 2,
               STATE_EXPIRED: 3, STATE_NEED_VERIFY: 4, STATE_RATE_LIMITED: 5,
               STATE_RESTRICTED: 6}


def account_state_map(settings=None, now: float | None = None,
                      refs: list[AccountRef] | None = None) -> dict[str, dict]:
    """★ **账号级**(不是档案级)的状态聚合:`{账号标识: {...}}`。

    ## 为什么必须有这一层(2026-10-12,体检自己报出来的)
    池里 7 条登记,用 `account_tag()` 一归并 ⇒ **其实只有 2 个真账号**:

        b7366765643da3 ← 档案 xhs_2, 档案 xhs_3
        b84bab7b8e6e46 ← 档案 xhs_4, 租户 1, .env(兜底)

    而**风控是账号级的,状态机却是档案级记的** ⇒ 出现这种荒唐结果:

        xhs_2 被打成 `need_verify`(要人动手) ⇒ 孪生兄弟 xhs_3 仍写着 `healthy`
        ⇒ `pick()` 高高兴兴挑中 xhs_3 ⇒ **拿那个正被风控的号又发了一次请求**

    这正是「**一个号都活不过一天**」的**机制性原因**:我们以为在轮换降险,
    其实是在**反复戳同一个号**;而在已被风控的号上重试**会把冷却往后推**
    (见 `HARMFUL_STATES`),于是越戳越糟 —— 单体看每次都是"新档案、新会话、新指纹",
    账号维度看却是"同一个号一分钟前刚被判异常,又来了"。

    聚合规则:**取这一组里最坏的状态**,冷却取**最晚的那个时刻**
    (保守方向:宁可少用一个号,也不要把人推进"越试越久"的循环)。
    """
    from collections import defaultdict

    now = now or time.time()
    acc: dict[str, dict] = {}
    for r in (refs if refs is not None else discover(settings)):
        tag = _tag_of_ref(r)
        if not tag:
            continue                                   #: 游客档/取不到 ⇒ 不参与归并
        g = acc.get(tag)
        if g is None:
            g = acc[tag] = {"state": STATE_UNKNOWN, "cooldown_until": 0.0,
                            "labels": [], "keys": [], "bad_keys": []}
        g["labels"].append(r.label or r.key)
        g["keys"].append(r.key)
        if _STATE_RANK.get(r.state, 9) >= _STATE_RANK.get(g["state"], 9):
            g["state"] = r.state                       #: 取最坏
        g["cooldown_until"] = max(g["cooldown_until"], float(r.cooldown_until or 0))
        #: 这一条**自己**就坏着吗?(要人动手 或 还在有害冷却里)
        if r.state in NEEDS_HUMAN or (
                r.state in HARMFUL_STATES and (r.cooldown_until or 0) > now):
            g["bad_keys"].append(r.key)

    for g in acc.values():
        g["cooling"] = g["cooldown_until"] > now
        g["needs_human"] = g["state"] in NEEDS_HUMAN
        #: 「越试越糟」且**冷却还没过** ⇒ 现在碰它会把冷却推后
        g["harmful"] = g["state"] in HARMFUL_STATES and g["cooling"]
        g["blocked"] = bool(g["needs_human"] or g["harmful"])
        #: ★ **连坐 ≠ 自己坏了**:只有"自己没坏、却被同组的孪生兄弟带封"才是连坐。
        #:   之前 `blocked_by_account` 把"自己正在冷却"也算进来 ⇒ 体检把它标成
        #:   「连坐停用」,而它其实只是"只需等"(分类被污染,下一步动作就错了)。
        g["shadowed_keys"] = ([k for k in g["keys"] if k not in g["bad_keys"]]
                              if g["blocked"] else [])
    return acc


def real_account_count(settings=None,
                       refs: list[AccountRef] | None = None) -> tuple[int, int]:
    """`(真实账号数, 登记条数)` —— 孪生档案(登的是同一个号)**只算一个**。

    体检第一行原来写「账号池:N 个」⇒ 让人以为手上有 N 个号可以轮换。
    **`N` 是档案数,不是账号数**;取不到账号标识的(游客档等)保守地各算一个。
    """
    refs = refs if refs is not None else discover(settings)
    tags: set[str] = set()
    untagged = 0
    for r in refs:
        t = _tag_of_ref(r)
        if t:
            tags.add(t)
        else:
            untagged += 1
    return len(tags) + untagged, len(refs)


def note_relogin(profile) -> None:
    """★ **人已经重登了 ⇒ 把"要人动手"的旧记号摘掉**(2026-10-12)。

    ## 为什么必须有这一下
    `need_verify` / `expired` / `guest` 这几种状态**没有"到点自愈"** ——
    按设计只能靠人来清(它们要的就是"人去重登/过验证")。
    可**之前没有任何一处会在重登成功后自动清它**:

        人跑了 `xhs_browser_login.py` → 会话真的活了 → 池子**还写着** expired
        ⇒ 轮换继续跳过它 ⇒ 体检上还是一盏红灯
        ⇒ 除非那人**恰好知道**还要再跑一次 `tools/xhs_accounts.py reset`

    这就是本仓记过一次的**「单向陷阱」**:记号**只写得进、摘不掉**
    (与 `xhs_page_source._NEED_KEY` 那段是同一个病,只是换了个门)。

    ⇒ 正解:**"人把要求做的事做完了"这件事本身就是清记号的时机**。
    重登成功 ⇒ 直接把这一号放回 `unknown`(清冷却),下一轮自然会被重新试用。

    ⚠️ 之所以回 `unknown` 而不是 `healthy`:重登只说明**会话活了**,
    还没真的取到过数;让下一轮去证明它,别在这儿提前发奖状。
    """
    key = profile_key(profile)
    data = _load_raw()
    row = data.get(key)
    if not row:
        return
    if str(row.get("state") or "") not in NEEDS_HUMAN:
        return                                     #: 本来就没事 ⇒ 不打扰
    old = row.get("state")
    data[key] = {**row, "state": STATE_UNKNOWN, "cooldown_until": 0.0,
                 "detail": f"重登成功,解除「{old}」", "last_used": time.time()}
    _save_raw(data)
    logger.info("小红书账号池:%s → %s(人已重登,解除「%s」)", key, STATE_UNKNOWN, old)


def need_verify_profiles() -> set[str]:
    """池里处于**「需人工过验证/重登」**的档案(**绝对路径**集合)。

    给页面路的轮换用:这些号直接跳过,不继续烧风控。
    """
    return {k[len(PROFILE_PREFIX):] for k, row in _load_raw().items()
            if k.startswith(PROFILE_PREFIX) and row.get("state") == STATE_NEED_VERIFY}


def profile_cooldowns(now: float | None = None) -> dict[str, int]:
    """`{档案绝对路径: 剩余冷却分钟}` —— 给体检/看板用(**不依赖 `discover()`**)。

    ⚠️ **不依赖 `discover()` 是有意的**:体检手上的档案可能**不在**当前配置里
    (改过配置、或多个实例共用一个库),那时按 `discover()` 组出来的快照**查不到它**
    ⇒ 会**漏报**("有号在冷却"这件事就看不见了)。冷却本身是**记在库里的**,
    直接读库最全。
    """
    now = now or time.time()
    out: dict[str, int] = {}
    for k, row in _load_raw().items():
        if not k.startswith(PROFILE_PREFIX):
            continue
        left = float(row.get("cooldown_until") or 0) - now
        if left > 0:
            out[k[len(PROFILE_PREFIX):]] = max(1, int(left // 60))
    return out


def cooling_profiles(now: float | None = None) -> set[str]:
    """池里**当前还在冷却**的档案(**绝对路径**集合)。

    ★ 这一条补的是**本仓一个长期空档**:页面路原先只有 `need_verify` 那个
    **没有时间戳**的标记,于是"频率风控"这类"等着就会好"的失败**没法按时长退避**
    —— 要么复用它(变成永远红)、要么干脆不复用(每轮重新撞)。
    现在冷却**带时刻**,池子会自己把它放出来。
    """
    return set(profile_cooldowns(now))


def harmful_cooldowns(now: float | None = None) -> dict[str, int]:
    """`{档案路径: 剩余分钟}`:处于**「越试越糟」**冷却里的档案(见 `HARMFUL_STATES`)。

    与 `profile_cooldowns()` 的区别:只挑**重试会把事情弄坏**的那两种状态
    (`rate_limited` / `restricted`)。调用方据此决定"**全都在这种冷却里时,不能硬试**"。
    """
    now = now or time.time()
    out: dict[str, int] = {}
    for k, row in _load_raw().items():
        if not k.startswith(PROFILE_PREFIX):
            continue
        if str(row.get("state") or "") not in HARMFUL_STATES:
            continue
        left = float(row.get("cooldown_until") or 0) - now
        if left > 0:
            out[k[len(PROFILE_PREFIX):]] = max(1, int(left // 60))
    return out


# ------------------------------------------------------------------ 视图

def _ref_from_row(key: str, row: dict) -> AccountRef:
    """把库里的一行还原成一个 `AccountRef`。

    ⚠️ **key 前缀比行里的 `kind` 权威**(2026-10-12 修):
    一行 `profile:<档案>` 可能是从 `file:<会话文件>` **归并**过来的(见 `_canon`),
    行里就还留着归并前的 `kind="session_file"`。要是信了它,体检会把同一枚
    `profile:` 行**当成一份有 cookie 的凭据**再去查一次 UA —— 而它没有 `path`,
    于是报出「存档里没有 UA」这种**假警报**(实际那份存档是有的)。
    """
    ref = AccountRef(key=key, label=str(row.get("label") or key),
                     kind=str(row.get("kind") or "unknown"),
                     state=str(row.get("state") or STATE_UNKNOWN),
                     detail=str(row.get("detail") or ""),
                     ok=int(row.get("ok") or 0), fail=int(row.get("fail") or 0),
                     last_used=float(row.get("last_used") or 0),
                     cooldown_until=float(row.get("cooldown_until") or 0))
    if key.startswith(PROFILE_PREFIX):
        ref.profile = key[len(PROFILE_PREFIX):]
        ref.kind = "profile_dir"                   #: key 说了算,不信行里的旧值
        ref.label = f"档案 {Path(ref.profile).name}"
    else:
        ref.path = str(row.get("path") or "")      #: 会话文件行:把路径带回来
        ref.user_id = int(row.get("user_id") or 0)
    return ref


def all_refs(settings=None) -> list[AccountRef]:
    """`discover()` **∪「库里记着、但当前配置已经查不到」**的账号。

    为什么需要:改过配置(或**多个实例共用一个库**)时,库里会留下**不属于**
    当前发现集合的记号。而"某个号解封了、想把它放出来"(`reset`)恰恰要能碰到它们
    —— 否则那些记号**永远清不掉**(与 `_pkey` 那条"单向陷阱"是同一族问题:
    只能写、不能摘)。
    """
    refs = discover(settings)
    seen = {r.key for r in refs}
    for key, row in _load_raw().items():
        if key not in seen:
            refs.append(_ref_from_row(key, row))
    return refs


def snapshot(settings=None) -> list[dict[str, Any]]:
    """给看板/CLI 用的账号池快照(带"能不能用、还要冷多久")。"""
    now = time.time()
    refs = discover(settings)
    #: ★ 只把**被孪生兄弟带封**的那些标成连坐;自己坏着的走 human/wait 分类
    shadowed = {k for g in account_state_map(settings, now, refs=refs).values()
                for k in g["shadowed_keys"]}
    rows = []
    for r in refs:
        tag = _tag_of_ref(r)
        rows.append({
            "key": r.key, "label": r.label, "kind": r.kind, "state": r.state,
            "detail": r.detail, "ok": r.ok, "fail": r.fail,
            "available": r.available(now),
            "cooldown_min": max(0, int((r.cooldown_until - now) // 60)),
            "needs_human": r.state in NEEDS_HUMAN,
            #: 账号段(空=取不到,如游客档)+ 是否被"孪生档案"的红状态连带封住
            "account": tag,
            "blocked_by_account": r.key in shadowed,
        })
    rows.sort(key=lambda x: (_STATE_RANK.get(x["state"], 9), -x["ok"]))
    return rows


#: 四分类 —— **互斥且穷尽**(见 `classify`)
CLS_USABLE = "usable"          #: 现在就能拿去用
CLS_WAIT = "wait"              #: 只需等,别动手
CLS_HUMAN = "human"            #: 要人去重登 / 过验证
CLS_SHADOWED = "shadowed"      #: 自己没问题,但登的账号被连坐了


def classify(row: dict) -> str:
    """★ 给一条登记**归个类** ⇒ `usable` / `wait` / `human` / `shadowed`。

    ⚠️ 为什么必须**互斥且穷尽**:
    之前 `summary()` 看 `available`、体检看"状态是不是 healthy/unknown",
    两边各判各的 ⇒ 同一份数据里出现「当前可用 2 个」与「🔴 一个都不能用」
    **两行互相打架**;而且被连坐的号在摘要里干脆**没被任何一类统计到**
    (1+0+4=5,实际 6 条)⇒ 一条"消失的账号"。
    ⇒ 记过的纪律:**一个结论只能有一个出处** ⇒ 全部走这一个函数。

    判定顺序(**最急的事优先**,因为那才是"下一步做什么"):
      ① `human`    —— 要人动手(重登 / 过验证):没人做就永远不会好
      ② `shadowed` —— 自己绿着,但**登的是同一个出事的号** ⇒ 碰它等于戳那号
      ③ `wait`     —— 冷却还没过,**只需等**(乱动反而推后)
      ④ `usable`   —— 剩下的:冷却已过、不要人、没连坐
    """
    st = str(row.get("state") or STATE_UNKNOWN)
    if st in NEEDS_HUMAN:
        return CLS_HUMAN
    if row.get("blocked_by_account"):
        return CLS_SHADOWED
    if st in HARMFUL_STATES and int(row.get("cooldown_min") or 0) > 0:
        return CLS_WAIT
    return CLS_USABLE if row.get("available") else CLS_WAIT


def row_usable(row: dict) -> bool:
    """这一条**现在能不能拿去用**?(= `classify() == usable`)

    ⚠️ 不在这里**另写一套判据** —— 否则又会和摘要/体检打架。
    """
    return classify(row) == CLS_USABLE


def summary(settings=None) -> str:
    """一行摘要(塞进日志/卡片)。"""
    rows = snapshot(settings)
    if not rows:
        return "小红书账号池:**空**(没有任何可用账号)"
    from collections import Counter

    c = Counter(r["state"] for r in rows)
    n_acc, n_rows = real_account_count(settings)
    #: ★ 四分类**互斥且穷尽** ⇒ 每一条登记都必然落在其中一类,且只落一类
    cls = Counter(classify(r) for r in rows)
    parts = ", ".join(f"{k}×{v}" for k, v in c.most_common())
    head = (f"小红书账号池:{n_rows} 条登记 / **实际 {n_acc} 个账号**({parts})"
            if n_acc != n_rows else f"小红书账号池:{n_rows} 个({parts})")
    return (f"{head};可用 {cls[CLS_USABLE]} / 只在等 {cls[CLS_WAIT]}"
            f" / 要人动 {cls[CLS_HUMAN]} / 连坐停用 {cls[CLS_SHADOWED]}")
