# -*- coding: utf-8 -*-
"""微信读书**账号池** —— 精确阅读数的唯一线性扩容手段(2026-10-18)。

## 为什么要有它(证据,不是猜测)
精确阅读数(`readNum`)在全仓只有一个来源:`GET /web/mp/articles`。它的额度是
**按微信读书账号**记的,而且会被**账号级**挡掉:

- 2026-10-09/10-10(好日子):单账号一轮 `ok=12~19`、`off=0`,一天 4 轮能成功 58 次;
- 2026-10-11 起:`ok=0`,同 IP 下 `shelf`/`mp/cover` 全 200(认证是好的),
  只有列表接口回 `-2041` ⇒ **认证之后才校额度**,所以断在额度上;
- 2026-10-18 复验:**刚续期 1 分钟的活 cookie** + 三种请求上下文
  (首页 Referer / 无 Referer / 阅读器页) **全部 `-2041`**
  (`scripts/probe_weread_list.py`) ⇒ 不是"调用上下文不对",是**这个账号被限**。

⇒ **续期换不来额度**(2026-10-18 实测定性;旧注释里"续期=新会话、额度重算"是错的,
`_listen` 那条注释已一并订正)。既然如此,唯一的扩容方式就是**再加一个微信读书账号** ——
每个账号各记各的额度,`N` 个账号 ≈ `N × 25` 个号/轮。

## 实现取舍(与 `pan_accounts` 同源,但不共用池配置)
- **存储直接复用 `cookie_store` 的「平台#序号」形态**(`weread` / `weread#2` / …,
  `set_cookie` 早就允许;< 见 `cookie_store.set_cookie`)⇒ **不新增表、不新增配置项**,
  加号的动作就是"往 `weread#2` 存一枚 cookie"。
- **单账号时行为与从前逐字一致**:基础账号 `weread` 的额度 scope **仍叫 `web_list`**,
  请求参数、异常类型、熔断键都不变 —— 加进来只会让"被挡之后还有别人可问"。
- 池里**只存引用**(key),凭据仍旧只在 `cookie_store` 的加密行里。

## 一个账号被挡时会发生什么
`ask_list` 按「先用没被挡的」排序逐个试;**全被挡**才抛 `Blocked`(与旧行为一致,
调用方照旧合闸)。某个账号答上了 ⇒ 本轮立刻能用,不必等它的 30 分钟冷静期。
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.services import weread_budget
from app.utils import get_logger

logger = get_logger(__name__)

#: 基础账号名(与历史上唯一的那一枚同名,保证 scope 向后兼容)
BASE_KEY = "weread"
#: 单轮每账号最多问多少个号。与 `_listen._LIST_WINDOW` 同一量级(实测额度 29,留余量)。
#: ⚠️ 这是**上限不是目标**:池里只有它自己时就是旧行为,池里多一个号也不会让它超发。
PER_ACCOUNT_ROUND = 25


def account_keys(session: Session, user_id: int) -> list[str]:
    """已配置的账号 key,顺序稳定:`weread` → `weread#2` → `weread#3` …

    ⚠️ 只认 `weread` 与 `weread#N` 两种形态 —— `weread_app` 是**另一套鉴权**
    (accessToken,见 `cookie_store.PLATFORMS`),不能混进来当列表账号用。
    """
    from app.db.models import UserCookie

    try:
        rows = session.execute(
            select(UserCookie.platform).where(
                UserCookie.user_id == user_id,
                or_(UserCookie.platform == BASE_KEY,
                    UserCookie.platform.like(BASE_KEY + "#%")),
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001 - 读不到就当"只有基础账号",不挡链路
        logger.debug("读微信读书账号列表失败", exc_info=True)
        return [BASE_KEY]

    def _order(p: str) -> tuple[int, str]:
        suffix = p.split("#", 1)[1] if "#" in p else "1"
        return (int(suffix) if suffix.isdigit() else 999, p)

    keys = sorted(set(rows), key=_order)
    return keys or [BASE_KEY]


def web_scope(key: str) -> str:
    """某账号的**列表额度** scope。

    ⚠️ 基础账号**必须**仍然是 `web_list`(与 `weread_budget.SCOPE_WEB_LIST` 逐字相同)——
    改名的后果是历史熔断状态失联,下一轮会对着一个"以为没被挡"的账号重打一次。
    """
    return weread_budget.SCOPE_WEB_LIST if key == BASE_KEY else f"{weread_budget.SCOPE_WEB_LIST}@{key}"


def is_blocked(session: Session, user_id: int, key: str) -> bool:
    return weread_budget.blocked_until(session, user_id, web_scope(key)) is not None


class WereadPool:
    """一轮一个实例。持有 `[(key, cookie)]`,并按"先用没被挡的"发列表请求。"""

    def __init__(self, entries: list[tuple[str, str]]) -> None:
        self.entries: list[tuple[str, str]] = [(k, c) for k, c in entries if c]
        self._clients: dict[str, object] = {}
        #: 本轮每个账号已经**答上**的号数(上限 `PER_ACCOUNT_ROUND`)。
        self.used: dict[str, int] = {}
        #: 本轮已确认**登录态坏**的账号(cover 用;见 `latest_article`)。
        self._cover_dead: set[str] = set()
        #: 上一次 cover 的登录态错误(全部账号都坏时原样抛出,不改异常语义)。
        self._cover_last: BaseException | None = None

    # ---- 基本 ----
    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def keys(self) -> list[str]:
        return [k for k, _ in self.entries]

    def with_base(self, cookie: str) -> "WereadPool":
        """用调用方解析出的基础 cookie **覆盖**池里的 `weread` 条目。

        为什么不让 `build` 自己取:基础账号那条的历史取值顺序在
        `_source._weread_cookie`(库 → `.env` 兜底 → 续期后的新值),而且测试会替换它。
        这里只做"把已经解析好的那一枚放进池里的第一位",**不改取值顺序** ⇒ 单账号时
        池里那唯一一条就是从前那个 cookie,一字不差。
        """
        cookie = (cookie or "").strip()
        if not cookie:
            return self
        rest = [(k, c) for k, c in self.entries if k != BASE_KEY]
        return WereadPool([(BASE_KEY, cookie)] + rest)

    def cookie_of(self, key: str) -> str:
        for k, c in self.entries:
            if k == key:
                return c
        return ""

    def client(self, key: str):
        """按 key 建客户端(**整轮复用**,与旧版"整轮只建一次"一致)。

        ⚠️ 走**门面** `wechat_monitor.WereadClient` 取类,不直接 import `weread_client` ——
        本仓的测试替身都是 `monkeypatch.setattr(wechat_monitor, "WereadClient", …)`,
        直接 import 会让池**绕过替身去打真网络**(2026-10-18 第一次跑测试就是这么红的)。
        """
        from app.services import wechat_monitor as _root

        if key not in self._clients:
            self._clients[key] = _root.WereadClient(self.cookie_of(key))
        return self._clients[key]

    def window_size(self) -> int:
        """本轮列表窗口应开多大:`单账号 25 × 可用账号数`。

        可用 = 至少还有一轮额度的账号。被挡的账号不占窗口 —— 否则"2 个账号、其中 1 个
        被挡"时窗口开成 50,而实际只有一个账号能答 ⇒ 一半的号白等一轮(仍不会出错,
        只是轮转变慢)。
        """
        return PER_ACCOUNT_ROUND * max(1, len(self.entries))

    # ---- 列表(唯一吃额度的动作) ----
    def ordered_keys(self, session: Session, user_id: int) -> list[str]:
        """排序:**没被挡的在前**,被挡的在后(仍排进去 —— 半开探测靠 `weread_budget.call`
        自己判,不在这里提前排除,免得两处各写一份"能不能问")。

        另外把"本轮已经用满 `PER_ACCOUNT_ROUND` 的账号"排到最后:它再答就超发了。
        """
        def _rank(key: str) -> tuple[int, int]:
            full = 1 if self.used.get(key, 0) >= PER_ACCOUNT_ROUND else 0
            blocked = 1 if is_blocked(session, user_id, key) else 0
            return (blocked, full)

        return sorted(self.keys, key=_rank)

    def ask_list(self, session: Session, user_id: int, book_id: str,
                 what: str = "") -> tuple[dict, str]:
        """按池顺序问列表;返回 `(payload, 答上话的账号 key)`。

        **单账号时与旧行为逐字一致**(同一个 scope、同一个异常、同一个 `allow_probe=False`)。
        多账号时的语义:一个账号被挡 **不** 终止本轮 —— 换下一个继续;
        全部被挡才把最后一个异常抛出去(调用方照旧合闸)。
        """
        errors: list[BaseException] = []
        for key in self.ordered_keys(session, user_id):
            if self.used.get(key, 0) >= PER_ACCOUNT_ROUND:
                continue                      # 这个账号本轮的份额已满,别超发
            client = self.client(key)
            try:
                payload = weread_budget.call(
                    session, user_id, lambda c=client: c.mp_articles(book_id),
                    what=f"{what or 'mp/articles'}[{key}]", scope=web_scope(key))
            except BaseException as exc:      # noqa: BLE001 - 任何失败都先换下一枚账号
                # ⚠️ **登录态错也要换号**(2026-10-18):账号 A 的 cookie 过期(`-2012`)
                #    并不代表账号 B 不行 —— 那正是"多一个号"最该顶上的时刻。
                #    第一版只对额度错换号,结果基础账号一过期整轮就红(被自己的测试逼出来的)。
                errors.append(exc)
                if weread_budget.is_quota_error(exc):
                    continue                      # 记在它自己的 scope 上,换下一个
                if weread_budget.is_auth_error(exc):
                    continue                      # 这个号的登录态坏了,换下一个
                # 其它(网络/解析失败):换号同样合理,但**不要**把它当成"账号坏了"
                continue
            self.used[key] = self.used.get(key, 0) + 1
            return payload, key
        if errors:
            # 抛哪个很关键:调用方是按**类型**决定处置的(登录态 ⇒ 续期;额度 ⇒ 合闸)。
            # 因此**登录态错优先**(它有一件明确要人做的事),否则抛最后一个。
            auth = [e for e in errors if weread_budget.is_auth_error(e)]
            raise (auth[0] if auth else errors[-1])
        raise weread_budget.Blocked(f"{what or '微信读书列表'}:所有账号本轮份额已满,本次不发请求")

    # ---- 最新一篇(cover,不花列表额度) ----
    def latest_article(self, book_id: str) -> dict | None:
        """cover 最新一篇。额度外,但仍然依赖**账号的登录态** ⇒ 也要能换号。

        - 第一个账号答了 ⇒ 用它;
        - 只在 **登录态类** 错误上换号(额度错误与 cover 无关,**不**拿它换号);
        - 全挂 ⇒ 抛最后一个登录态错误(调用方照旧去续期)。
          注意:上游是 `WereadClient.latest_article`,`mp_cover` 空时它会抛
          `WereadError("公众号暂无文章…")` —— 那不是登录态问题,**原样上抛**,别换号。
        """
        from app.services.weread_client import WereadAuthError

        last: BaseException | None = None
        # **本轮已确认登录态坏的账号不再重试**:cover 是"每个号每轮都打一次"的高频动作,
        # 不记住的话,基础账号一过期就会让**每个号**都多打一发注定失败的请求(整轮成本翻倍)。
        # ⚠️ 全部账号都坏时**直接抛上一次的错**,不要用 `or self.keys` 把它们放回来重试 ——
        # 那等于"记住"白记(2026-10-18 当场发现:网页 cookie 过期时 122 个号各打了一发 -2012)。
        keys = [k for k in self.keys if k not in self._cover_dead]
        if not keys:
            if self._cover_last is not None:
                raise self._cover_last
            return None
        for key in keys:
            try:
                return self.client(key).latest_article(book_id)
            except WereadAuthError as exc:
                last = exc
                self._cover_last = exc
                self._cover_dead.add(key)
                logger.debug("cover 账号 %s 登录态失效,换下一个", key)
                continue
        if last is not None:
            raise last
        return None


def build(session: Session, user_id: int, settings=None) -> WereadPool:
    """按库里已配置的账号建池;**永远至少有 1 个条目**(基础账号,含 `.env` 兜底)。

    与 `_source._weread_cookie` 同一套取值顺序(库优先、`.env` 兜底),
    所以"只配了一枚"时,池里那唯一的一条**就是**从前那个 cookie。
    """
    from app.services.cookie_store import get_cookie

    keys = account_keys(session, user_id)
    entries: list[tuple[str, str]] = []
    for key in keys:
        cookie = (get_cookie(session, user_id, key) or "").strip()
        if not cookie and key == BASE_KEY and settings is not None:
            cookie = (getattr(settings, "weread_cookie", "") or "").strip()
            if cookie.startswith("#"):      # `.env` 里的注释行,不是 cookie
                cookie = ""
        if cookie:
            entries.append((key, cookie))
    if len(entries) > 1:
        logger.info("微信读书账号池:%d 个账号(%s),本轮列表窗口可开到 %d",
                    len(entries), ", ".join(k for k, _ in entries),
                    PER_ACCOUNT_ROUND * len(entries))
    return WereadPool(entries)
