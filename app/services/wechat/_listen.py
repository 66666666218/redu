"""监听轮:书架粗筛、额度熔断、微信读书采集、批次轮换、主循环。"""

from app.db.models import (WechatArticle, WechatBenchmark, WechatPanLink)

from app.db.tx import savepoint

from app.services.reader_platform_client import PlatformError, ReaderPlatformClient

from app.services.tenant_base import _base, _record_run

from app.services.weread_client import WereadAuthError, WereadClient, WereadError, build_mp_url
from app.services import weread_budget   # 额度判据 + 跨路径共享熔断(单一事实来源)

from app.services.werss_client import WerssClient

from config.settings import Settings

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update

from sqlalchemy.exc import IntegrityError, OperationalError, PendingRollbackError

from sqlalchemy.orm import Session

import json

import re

import uuid

import zlib

from app.services.wechat._text import _MY_LINK_RE, _parse_time
from app.services.wechat._source import _cookie_fingerprint, feed_biz
from app.services.wechat._enrich import _insert_new_articles


_LISTEN_CURSOR_KEY = "wechat_listen_cursor_{uid}"
from app.utils import get_logger

logger = get_logger(__name__)
from app.services import wechat_monitor as _root  # 兼容 monkeypatch:可替换名经门面运行时查找

def _advance_listen_cursor(session: Session, user_id: int) -> int:
    """读并推进监听批次游标(存 system_config,所有入口共享同一轮转序列)。

    返回本次应使用的 batch_index;副作用是游标 +1(先取后增,从 0 起)。
    并发场景:监听轮自身有"同一用户不并发"的时长锁(见 run_wechat_listen),
    游标读写实际串行,无需额外加锁。
    """
    from app.db.models import SystemConfig

    key = _LISTEN_CURSOR_KEY.format(uid=user_id)
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
    current = int(row.value) if row and str(row.value).isdigit() else 0
    if row is None:
        session.add(SystemConfig(key=key, value=str(current + 1)))
    else:
        row.value = str(current + 1)
    session.commit()
    return current
# 额度判据的**单一事实来源**在 `app/services/weread_budget.py`(2026-10-05 搬过去)。
# ⚠️ 搬家的原因:额度是**跨模块的公共资源** —— 监听、同步、续期、体检验活、探针脚本
# 全在用同一个账号的额度,而判据原来住在这个 1200 行、监听专用的文件里,
# 别的路径嫌重干脆不 import ⇒ `_sync` 那边**根本没判过额度**。
# 这里保留名字只为兼容既有调用方与测试(值本身不再在此定义)。
_WEREAD_QUOTA_MARKS = weread_budget.QUOTA_MARKS
_COVER_QUOTA_TRIP = 3
_DORMANT_MISS = 7      # 连续 N 轮确认未发文 → 沉睡降频(与前端"沉睡"口径一致)
_DORMANT_SKIP = 3      # 沉睡号每 3 轮参与 1 轮
_BATCH_MAX = 75        # 单轮额度安全线(75 号 ≈150 请求;微信读书会话预算实测 160~200)
_BATCH_MIN = 8


def _select_listen_batch(session: Session, user_id: int, all_rows: list,
                         settings, batch_index: int | None = None,
                         batch_size: int | None = None) -> tuple[list, str]:
    """自适应分批 + 权重分层(2026-10-01,为号池扩建准备)。返回 (本轮号列表, batch_pos)。

    - **批大小自适应**:clamp(ceil(有效池/定点数), 8, 75)——池子涨了自动调,每天尽量
      全覆盖;超 75 时天然"多日轮转"(每天 4×75=300 号,池 400 两天轮完一遍)。
    - **权重分层**:沉睡号(miss_count≥7)每 3 轮只参与 1 轮——额度花在"有产出的号"上。
    - 显式 batch_size/batch_index(测试/特殊用途)保持旧语义,不叠分层。
    - settings.wechat_listen_batch_size: 0=自适应;非 0=固定批(旧);负数=全量。
    """
    N = len(all_rows)
    if batch_size is not None:
        eff = batch_size
        if eff <= 0 or N <= abs(eff):
            return all_rows, ""
        n_groups = N // abs(eff) + (1 if N % abs(eff) else 0)
        idx = batch_index or 0
        start = idx % n_groups
        return (all_rows[start * abs(eff):(start + 1) * abs(eff)],
                f" batch={start + 1}/{n_groups}(size={abs(eff)},cursor={idx})")

    cfg = getattr(settings, "wechat_listen_batch_size", 0)
    if cfg < 0:                       # 逃生门:负数 = 全量
        return all_rows, ""
    if batch_index is None:
        batch_index = _advance_listen_cursor(session, user_id)

    active = [b for b in all_rows if (b.miss_count or 0) < _DORMANT_MISS]
    dormant = [b for b in all_rows if (b.miss_count or 0) >= _DORMANT_MISS]
    dormant_pick = dormant[batch_index % _DORMANT_SKIP::_DORMANT_SKIP] if dormant else []
    pool = active + dormant_pick

    K = 4  # 每日定点数(WECHAT_LISTEN_HOURS);每日覆盖预期 = K × B
    if cfg and cfg > 0:               # 固定批(旧行为):在全量池上轮转
        B = cfg
        pool = all_rows
        dormant_pick = []
    else:                             # 自适应:按有效池定批
        B = max(_BATCH_MIN, min(_BATCH_MAX, -(-len(pool) // K)))
    if B <= 0 or len(pool) <= B:
        return pool, (f" all(size={len(pool)})" if len(pool) < N else "")

    n_groups = len(pool) // B + (1 if len(pool) % B else 0)
    start = batch_index % n_groups
    rows = pool[start * B:(start + 1) * B]
    extra = f",active={len(active)},dormant={len(dormant)}/{_DORMANT_SKIP}轮" if dormant else ""
    return rows, f" batch={start + 1}/{n_groups}(size={B},cursor={batch_index}{extra})"


def _is_weread_quota_error(exc: BaseException) -> bool:
    """-2014(频率额度)/ -2041(会话列表预算耗尽):同属"再问也不给,还会把风控加深"。

    判据在 `weread_budget`(单一事实来源),这里只是**兼容入口** ——
    新增代码直接用 `weread_budget.is_quota_error`。
    """
    return weread_budget.is_quota_error(exc)


#: App 路"登录态失效"类错误的标记 —— 这一类**等多久都不会自己好,要人动手**,
#: 所以它才值得推飞书(限流/网络那类会自愈,推了只会变成噪音)。
_APP_AUTH_MARKERS = ("-2010", "-2012", "登录", "凭据", "token", "accessToken")


def _app_auth_needs_human(exc: BaseException) -> bool:
    """App 路的失败**是不是"要人去模拟器里重登"**那一种。

    ⚠️ 分这一刀的理由和 `chain_health._classify_source_error` 完全一致:
    **只有"等多久都不会自己好"的才打扰人** —— 限流/网络抖动推飞书,只会训练人忽略告警。
    实测(2026-10-06):这一类断了 8 天没人知道,而修法只是"在雷电里打开微信读书 App"
    (用户打开后 token `jXHu1v8j`→`Xph3xEtt`,8/8 个号立刻全通)。
    """
    text = f"{type(exc).__name__}: {exc}"
    return any(m in text for m in _APP_AUTH_MARKERS)
_BAN_MARKERS = (("此账号已被屏蔽", "账号封禁"), ("该内容已被发布者删除", "作者删除"),
                ("此内容因违规无法查看", "违规处理"))
def _detect_ban_reason(page_text: str) -> str:
    """识别微信封文/封号页并提取违规类别;正常页面返回 ''。

    微信公开页**不区分投诉人**——只给「由用户投诉并经平台审核」+ 违规类别;
    但类别含「版权/商标/专利」即走了知识产权通道,普通用户很少走,批量出现
    就是版权方清扫的典型特征(2026-09-28 霸王茶姬杯贴文实测:同类资源多号
    接连被封,盘商分享链同步被批量投诉 41031)。
    """
    if not page_text:
        return ""
    for marker, note in _BAN_MARKERS:
        if marker in page_text:
            ip = ("版权" in page_text or "商标" in page_text or "专利" in page_text)
            return f"{note}·{'侵权投诉(版权/商标/专利)' if ip else '平台规范'}"
    return ""
# ---------------------------------------------------------- 列表额度:按"最久没轮到"挑
# `/web/mp/articles`(**带精确阅读数**)有**会话额度**:2026-10-04 实测 —— 续期后从
# 书架第 1 个号问起,前 29 个全成功、第 30 个被 `-10100` 挡。而一轮有 75 个号
# ⇒ 全问必然只有前 29 个拿得到阅读数,后面的还白挨一次熔断。
# 所以列表**每轮只问 `_LIST_WINDOW` 个号**,其余号只取 cover(不花额度)。
#
# ⚠️ **为什么不是"按序号切窗口 + 游标右移"(第一版写法,同日改掉)**:
# 本轮的号列表**不是固定的全量** —— 先被 `_select_listen_batch` 按批轮转切成子集
# (142 号池 → 每轮 36 个),再被书架闸门 `rows = sorted(rows, key=tier)` **按每轮都可能变的
# 次序重排**。在"内容与次序都在变"的列表上按小标切窗口,覆盖是**碰巧**而不是保证 ——
# 模拟 5 次:全覆盖要 16~22 轮(4~5.5 天),而"游标右移"的写法让它看起来像 1.4 天。
# ⇒ 改成**按"上次问到列表的时间"排序,取最久没轮到的 N 个**:
#   与批子集、与排序**都无关**,全覆盖有闭式上界 `ceil(池/批) × ceil(批/窗口)`
#   —— 142 号池、批 36、窗口 25 ⇒ `4 × 2 = 8` 轮 ≈ **2 天**(实测 5 个随机种子都是 8 轮)。
#   而"按序号 + 游标右移"那种写法实测要 16~22 轮,**却让人误以为只要 1.4 天**。
# 记号存 `system_config` 的 JSON(与 `weread_shelf_marks_<uid>` 同一套路)。
_LIST_WINDOW = 25            # 实测额度 29,取 25 留余量
_LIST_MARKS_KEY = "weread_list_marks_{uid}"


def _list_key(b) -> str:
    """列表轮转用的**号身份**:`weread_book_id`。

    ⚠️ 不能用自增 `id`(更不能用序号):`rows` 每轮都是**按批轮转切出来的子集**、
    又被书架闸门重排过,序号的含义每轮都在变。
    """
    return str(getattr(b, "weread_book_id", "") or "")


def _list_marks(session: Session, user_id: int) -> dict[str, float]:
    """每个号**上次真正问过列表**的时间(epoch 秒)。读不出来就当空(全都"从没问过")。"""
    import json

    from app.db.models import SystemConfig

    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == _LIST_MARKS_KEY.format(uid=user_id)))
    try:
        data = json.loads(row.value) if row and row.value else {}
    except (TypeError, ValueError):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(data, dict)} if data else {}


def _list_attempts(stats: dict) -> int:
    """本轮**真去问过列表**的次数(成功/失败都算,`weread_list_skipped`=没问过,不算)。

    记号必须按"**问没问过**"记、而不是"**问没问成**" —— 后者会让"额度只够前 10 个、
    窗口 25 个"这种情形卡死:没问成的号永远排在队头,而队头永远是同一批(次键是 bookId),
    于是 bookId 靠后的号**永远进不来**。按"问过"记 ⇒ 每个被挑中的号都轮到过,
    失败的下个周期再来。这正是"轮转"该有的性质。
    """
    return sum(stats.get(k, 0) for k in
               ("weread_list_ok", "weread_list_app_ok", "weread_list_off_new", "weread_list_off"))


def _acct_brief(pool, stats: dict | None) -> str:
    """运行记录里的「各账号答了几个号」:`weread:18/weread#2:0`。

    ⚠️ **单账号时返回空串**(调用方据此整段不写)—— 运行记录的 detail 是被人和守卫
    逐字比对的,只配了一枚账号时它必须与旧版**逐字相同**(2026-10-18 测试当场逼出来的)。
    """
    keys = list(getattr(pool, "keys", []) or [])
    if len(keys) <= 1:
        return ""
    by = (stats or {}).get("weread_list_ok_by_acct") or {}
    return "/".join(f"{k}:{int(by.get(k, 0))}" for k in keys)


def _list_windows(session: Session, user_id: int, rows: list, *, extra: int = 0,
                  window: int | None = None) -> tuple[set[str], set[str]]:
    """一轮里**两条路各自的窗口**:`(网页窗口, App 额外窗口)`。

    ⚠️ **一份次序切两段**(2026-10-10):两条路**共用同一个"最久没轮到"的排序**,
    只是网页吃前 `_LIST_WINDOW` 个、App 再吃**紧跟其后的 `extra` 个**。
    ⇒ 好处是"下一个最该被问的号"永远由**同一个次序**决定,不会出现两条路各自
    算一套"谁最久没轮"、结果互相抢或同时漏掉某批号(那种"两处各写一遍、迟早飘"的事本仓吃过)。

    **为什么要给 App 开"窗口外"名额**:`/web/mp/articles` 有**会话额度(实测 29)**,
    所以每轮只问 25 个号 ⇒ 206 个有 bookId 的号要 **8~20 轮**才轮到一次,
    实测**精确阅读数覆盖率只有 44%**。而 App 路(`/book/articles`)**是另一套鉴权与配额**,
    且 2026-10-10 起已改成**纯 HTTP、不需要模拟器** ⇒ 它可以去补网页吃不到的那些号。

    ⚠️ **App 的配额上限是未知的**,所以这里**不默认放开**:`extra=0` = 与旧行为完全一致。
    加量请走 `wechat_listen_app_extra`,并**盯 `runs.detail` 里的 `app_fail`/`app_blocked`**
    (只成功不失败才往上加)—— 网页路那次被账号级拦了**一周**,代价不对称。
    """
    if not rows:
        return set(), set()
    # ⚠️ 先滤空键:它们既不可能被问列表,也不该占用窗口名额(见 `_list_window` 的说明)
    keyed = [b for b in rows if _list_key(b)]
    if not keyed:
        return set(), set()
    marks = _list_marks(session, user_id)
    # 次序里带 bookId 做次键:并列时结果**确定**(否则同一批号的顺序每次不同,难复现)
    ordered = sorted(keyed, key=lambda b: (marks.get(_list_key(b), 0.0), _list_key(b)))
    # ★ 窗口宽度可配(2026-10-18):**微信读书账号池**有 N 个账号就有 N 份额度
    #   (见 `app/services/weread_accounts.py`)⇒ 窗口应开成 `25 × N`。
    #   不传 = `_LIST_WINDOW`(旧行为逐字一致)。
    width = _LIST_WINDOW if window is None else max(_LIST_WINDOW, int(window))
    web = {_list_key(b) for b in ordered[:width]}
    app = {_list_key(b) for b in ordered[width:width + max(0, int(extra))]}
    return web, app


def _list_window(session: Session, user_id: int, rows: list) -> set[str]:
    """本轮**允许问列表**的号(bookId 集合)—— 取"最久没轮到"的那 `_LIST_WINDOW` 个。

    ⚠️ 键用 `weread_book_id`(**不是序号**):序号会随批子集/重排漂移,bookId 不会。
    从没问过的记 0 ⇒ 排最前,所以新加的号第一轮就能拿到阅读数。

    ⚠️⚠️ **没有 bookId 的号必须排除在排队之外**(2026-10-08 修)—— 这一处曾让
    **阅读数整条链静默死了两天**,而所有告警都指向别处。机制:
    它们 `_list_key(b)` 是空串、记号里也没有(取 0.0 ⇒ 排最前),于是**窗口的 25 个名额
    全被这些"永远用不上窗口"的号占住**,`{...}` 又把 25 个空串收敛成**一个元素 `{""}`**;
    而真正有 bookId 的号 `_list_key(b) in {""}` **恒为 False** ⇒ 窗口对它们**恒为空**。
    后果是**两条路一次都没被调用过**(不是被拦、不是失效):运行记录已经写着
    `weread_list(ok=0 app=0 off=0 skipped=79)` —— `ok/app/off 全 0 而 skipped 满员`
    就是"谁都没问"的指纹(被额度挡会是 `off>0`,所以从计数就能与风控区分开)。
    实测那天的库:263 个号里 **57 个 bookId 为空**,而有 bookId 的 **139 个全被挡在窗外**。
    同一处再核一遍:`_mark_listed` 本来就过滤空键(`if k`),说明"空键不是有效号"
    这件事在写入侧早已成立,漏的只是**读取侧的排队**。

    本函数**只返回网页窗口**(是 `_list_windows(extra=0)` 的第一段);App 的额外窗口
    由调用方用 `_list_windows(extra=wechat_listen_app_extra)` 取。
    """
    return _list_windows(session, user_id, rows, extra=0)[0]


def _mark_listed(session: Session, user_id: int, keys: set[str]) -> None:
    """把"这些号**这一轮真问到列表了**"记下来(epoch 秒),下轮好把机会让给别人。

    ⚠️ **只记号、不推游标**:没问到的号(不在窗口里 / 被额度挡下 / 被平台源抢先消费)
    记号**保持旧值** ⇒ 下一轮它仍然排在队头,不会因为"别人前进"而被永久轮空。
    这正是第一版"游标右移 + 被挡就不推进"要花力气防的那件事,现在由数据结构本身保证。
    """
    import json
    import time

    from app.db.models import SystemConfig

    keys = {str(k) for k in keys if k}
    if not keys:
        return
    marks = _list_marks(session, user_id)
    now = time.time()
    marks.update({k: now for k in keys})
    key = _LIST_MARKS_KEY.format(uid=user_id)
    payload = json.dumps(marks, ensure_ascii=False)
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
    if row:
        row.value = payload
    else:
        session.add(SystemConfig(key=key, value=payload))


def _append_listed(items: list, rows, cutoff) -> None:
    """把"列表"查到的新文并进 `items`(网页路与 App 路**共用**这段)。

    两条路的响应形状不同(网页 `flatten_mp_articles` 出 `create_time`,
    App 路在调用处已归一成同名字段),但**过滤与落字段的规则必须一致** ——
    写成两份迟早会飘(本仓最不缺的就是这个)。
    """
    for it in rows:
        ts = it.get("create_time") or 0
        pub = datetime.fromtimestamp(ts) if ts else None
        if pub and pub < cutoff:
            continue
        items.append({"title": it["title"], "url": build_mp_url(it["original_id"]),
                      "read_num": it.get("read_num"), "like_num": it.get("like_num"),
                      "publish_at": pub, "review_id": str(it.get("review_id") or "")})


def _weread_app_keys(session: Session, user_id: int) -> list[str]:
    """已配置的 **App 凭据** key,顺序稳定:`weread_app` → `weread_app#2` → …。

    「多开」用:一个微信读书账号 = 一台雷电实例 = 一枚 App 凭据
    (见 `weread_app_token.platform_for`:网页 `weread#2` ↔ App `weread_app#2`)。

    ⚠️ 枚举逻辑**只在 `weread_app_token.app_keys` 一处** —— 定时续期要用同一份,
    两处各写一遍就会重演今晚那个漏点(定时只续了基础那枚)。
    """
    from app.services import weread_app_token as wat

    return wat.app_keys(session, user_id)


class _WereadAppPool:
    """多开:**每个微信读书账号一枚 App 凭据**,按轮转分配请求。

    - **单凭据时与旧行为逐字一致**(只打那一枚,异常也原样冒泡);
    - 多凭据时把整轮的 App 窗口轮转到各账号 —— 额度是**按账号**记的
      (2026-10-18 实测:一轮硬打 80+ 发就把一个号打进 `-2041`),所以 N 个号 ≈ N 份预算;
    - 某一枚报错(**登录态或额度**)就换下一枚;**全挂才抛**,绝不吞成"这个号没发文"。

    ★★ **2026-10-19:额度熔断按账号分开**(`app_list@weread_app#2`)。
    以前整个池共用一个 `app_list` scope:1 号被 `-2041` 挡下后,外层
    `weread_budget.call` 在调用池**之前**就抛 `Blocked` ⇒ **2 号根本没机会被试**。
    这正是"明明有两个号,整轮却 `app=0`"的直接原因。现在 pool 自己按账号记熔断,
    一个号被挡,下一个号照常发。
    """

    #: 告诉调用方"本对象的额度由自己按账号管",别再在外面套一层共享 scope。
    scoped_budget = True

    def __init__(self, items: list) -> None:
        norm: list[tuple[str, object]] = []
        for i, it in enumerate(items):
            #: 旧测试/旧调用只传 client ⇒ 给一个稳定的默认 key(第一枚=基础账号)。
            if isinstance(it, tuple) and len(it) == 2:
                key, client = it
            else:
                key, client = ("weread_app" if i == 0 else f"weread_app#{i + 1}"), it
            norm.append((str(key), client))
        self._clients = norm
        self._i = 0
        #: 本轮已确认**用不了**的账号(额度/登录态/网络)—— 不在一发上反复撞。
        #: ⚠️ 这一轮内有效(pool 每轮新建),下一轮会重新试 —— 与 `weread_budget` 的
        #:   30 分钟冷静期不同:这里只是"别在同一次轮询里连撞 N 次"。
        self._dead: set[int] = set()
        self._last_error: BaseException | None = None

    def __len__(self) -> int:
        return len(self._clients)

    @staticmethod
    def _scope(key: str) -> str:
        """某个 App 账号的额度 scope。基础账号保持 `app_list`(历史熔断键不变)。"""
        return (weread_budget.SCOPE_APP if key == "weread_app"
                else f"{weread_budget.SCOPE_APP}@{key}")

    def articles(self, book_id: str, *, session=None, user_id: int = 1,
                 allow_probe: bool = False, **kw):
        n = len(self._clients)
        alive = [i for i in range(n) if i not in self._dead]
        if not alive:
            # 全用不了:抛上一次的错(不改异常语义),而不是死等
            if self._last_error is not None:
                raise self._last_error
            raise RuntimeError("没有可用的 App 凭据")
        for off in range(len(alive)):
            idx = alive[(self._i + off) % len(alive)]
            key, client = self._clients[idx]
            try:
                if session is None:
                    #: 测试/裸调用:不碰额度表,行为与旧版一致。
                    out = client.articles(book_id, **kw)
                else:
                    out = weread_budget.call(
                        session, user_id, lambda c=client: c.articles(book_id, **kw),
                        what=f"app/book/articles[{key}]", scope=self._scope(key),
                        allow_probe=allow_probe)
            except Exception as exc:  # noqa: BLE001 - 换下一枚账号(与账号池同一条纪律)
                self._dead.add(idx)
                self._last_error = exc
                continue
            self._i = (idx + 1) % n
            return out
        raise self._last_error if self._last_error is not None else RuntimeError("App 调用失败")


def _weread_app_client(session: Session, user_id: int):
    """取本租户的 **App 侧**客户端(不配就返回 `None`,不报错)。

    ⚠️ 返回 `None` 是**正常分支**(没配过 App 凭据),不是失败 —— 调用方据此跳过兜底。
    ⚠️ 凭据是 JSON(`scripts/weread_app_login.py` 写的),**解析失败要能说出来**:
    静默当成"没配"会让"token 坏了"表现为"阅读数又是 0",正是本仓最怕的那种。

    **自愈**:`accessToken` 会随 App 会话轮换,轮换后即失效。这里挂上
    `weread_app_token.refresh_with_wake` 作回调 —— 遇到登录态错误时**自动重取并重试一次**,
    不用人记得去跑脚本(那是靠不住的)。

    ★ **多账号**(2026-10-18):按 `weread_app` / `weread_app#2` / … 各建一枚,
    返回 `_WereadAppPool`。2026-10-19 起**单枚也返回池**(池内只有一枚,行为不变)——
    因为额度熔断要按账号分开记,统一走池才不会漏掉那一层。
    """
    try:
        from app.services.weread_app_client import WereadAppClient
        from app.services import weread_app_token as wat

        clients = []
        for key in _weread_app_keys(session, user_id):
            blob = wat.load(session, user_id, platform=key)
            if not blob:
                continue
            #: 这枚凭据属于哪台模拟器(多开时**必须**按它取,否则会读到别人的库)。
            serial = str(blob.get("adb_serial") or "")

            def _reget(_key=key, _serial=serial):
                """重取凭据;返回 `(token, vid, profile)` 或 `None`(取不到就 None,不抛)。"""
                # ⚠️ **走"唤醒+验活"那条,不是裸 refresh**(2026-10-07):
                # 裸 refresh 只是"重取一次、拿到**同一个值**、再失败"——**空转**。
                # 实测 App 长时间不动时数据库里就是旧 token,而 `am start` 唤醒它之后
                # 才会写新的有效值(实测 `3sg_FVG7` 失败 → 唤醒后 `HbVEHrVJ` 连试 4 次全通)。
                out = wat.refresh_with_wake(session, user_id, serial=_serial, platform=_key)
                if not out.get("ok"):
                    logger.warning("App 凭据自愈失败(%s,两条路都没成):%s",
                                   _key, str(out.get("reason"))[:120])
                    return None
                # ★ 第三项是**身份档**(2026-10-10):纯 HTTP 自愈铸出来的是 eink 身份的 token,
                #   客户端必须跟着换头,否则下一次调用仍然 `-2012`。
                return out["accessToken"], out["vid"], out.get("profile") or ""

            clients.append((key, WereadAppClient(
                blob.get("accessToken") or "", blob.get("vid") or "",
                on_auth_error=_reget, profile=blob.get("profile") or "")))
        if not clients:
            return None
        #: 单枚也走池:额度 scope 按账号分开(`app_list` / `app_list@weread_app#2`)。
        return _WereadAppPool(clients)
    except Exception as exc:  # noqa: BLE001 - 不能因为兜底坏了就停掉整轮监听
        logger.warning("App 侧凭据不可用(跳过列表兜底):%s", str(exc)[:120])
        return None


def _weread_collect(user_id: int, b: WechatBenchmark, weread: WereadClient,
                    session: Session, stats: dict | None = None,
                    breaker: dict | None = None, shelf_ts: str | int | None = None,
                    banned_out: dict[str, str] | None = None, list_allow: bool = True,
                    app_client=None, app_allow: bool | None = None, pool=None
                    ) -> tuple[list[WechatArticle], bool]:
    """微信读书单号采集:**cover 最新一篇(始终可用)+ 窗口内的号才拉精确列表**。

    ⚠️ `/web/mp/articles`(**带精确 `readNum`**)是**活的**,但它有**会话额度**:
    2026-10-04 实测 —— 续期后从书架**第 1 个号**问起,**前 29 个全部成功,第 30 个**
    回 `-10100` 被挡。而一轮有 75 个号 ⇒ **全问必然只有前 29 个拿得到阅读数**。
    ⇒ 所以列表**只对本轮"轮转窗口"里的号**要(`list_allow`,见 `_list_window`),
    其余号只取 cover;游标轮转,让每个号迟早轮到。方案见
    `doc/阅读数与迅雷盘-结论与方案-2026-10-04.md` 第三节。

    ⚠️ **这里曾写了两个月的错话,记下来警示**:旧注释说该接口"仅在会话续期后初期可用"
    (后来又改判"已废弃"),2026-10-04 白天据此把它判死 —— 但那次"四层测试"用的号
    **全都排在额度之后**,属于**没控制变量的证伪**。当天傍晚从**第 1 个号**重测:
    前 29 个全通、精确阅读数到手。
    **"只有 cover"就意味着同一天发第 2、3 篇会被最新一篇顶掉、永久漏采**(两轮之间最长 8h),
    这正是"近 24h 必须全推"的唯一真实缺口。
    `stats` 记账可枚举性(见调用方),不可枚举又采到新文时必须暴露给运维,不能假装全覆盖。
    近3天过滤。**阅读/点赞以列表自带值为准**(cover 不带 readNum)。
    `shelf_ts` = 书架粗筛拿到的 lastChapterCreateTime:cover 文章就是该号最新一篇,
    书架时间戳即它的发布时间——此前 cover 路径 publish_at 恒空,卡片时效/补推窗口/
    采样窗口全都吃不到,现在每轮监听顺手补上(2026-09-27)。

    第二个返回值 = **源这一轮有没有正面回答过这个号**(cover 给出一篇可看的文 / 列表枚举成功)。
    调用方据此决定 `miss_count` 加不加:+1 的含义是"确认当天没发文",
    cover 空响应且列表又挂了的"什么都不知道"不能算进去——否则一次风控就把正常号
    刷成"连续 N 轮未发文"的沉睡号,运营会去删本来在发的号。
    """
    from app.services.weread_client import WereadClient as _WC

    cutoff = datetime.now() - timedelta(days=3)
    items = []
    # 主路径:cover 最新一篇(始终可用);发布时间用书架时间戳盖(封面文=最新一篇)
    # ★ 账号池(2026-10-18):cover 不花列表额度,但它**依赖账号的登录态** ——
    #   基础账号的 cookie 在两次续期之间有窗口会失效(`-2012 登录超时`),
    #   池里还有别的账号时就不必整轮等续期(续期失败还会把本号记成 failed)。
    # ★★ 2026-10-18:**cover 登录态失效不许让整个号判死**。
    #    cover 走的是**网页 cookie**,列表(App 路)是**另一套鉴权**;网页 cookie 在
    #    两次续期之间会有一段 `-2012`(今天就有一段)。原来的写法让 cover 先抛
    #    `WereadAuthError` ⇒ 调用方直接去续期(还可能撞冷却)⇒ **唯一能用的 App 路根本没被调用**
    #    ⇒ 整轮 `new=0`、阅读数全 0。现在:cover 失败只记下来,**继续走列表**;
    #    只有列表也没答上时,才把那个登录态错抛回去(续期仍然照旧触发)。
    cover_auth: Exception | None = None
    try:
        item = (pool.latest_article(b.weread_book_id) if pool is not None
                else weread.latest_article(b.weread_book_id))
    except WereadAuthError as exc:
        cover_auth, item = exc, None
        if not getattr(_weread_collect, "_cover_auth_warned", False):
            _weread_collect._cover_auth_warned = True
            logger.info("cover 登录态失效(%s)—— 本轮改由列表路(App/网页)回答", str(exc)[:80])
    cover_ok = bool(item and item.get("url"))
    if cover_ok:
        items.append({"title": item["title"], "url": item["url"],
                      "publish_at": _shelf_ts_to_dt(shelf_ts),
                      "review_id": str(item.get("review_id") or "")})
    # 备选:mp/articles 近期列表(含精确阅读/点赞;被限权时静默跳过)
    # ⚠️ 这个接口是**稀缺额度**:一轮 81 个号各问一次就是 81 次,微信读书按会话/IP 记账,
    # 队头那十几个号把额度吃光后,后面的号必然连 cover 一起被 -2014 挡掉(2026-09-27 实测:
    # 同 IP 连打 7 次 cover 即回 -2014)。所以一旦本轮被额度类错误挡下,`breaker` 就合闸,
    # 剩余号不再问列表——省下来的额度留给"每个号至少问得动 cover"。
    listed = False
    list_via = ""          # "web" / "app" —— 两条路要**分开计数**,见文件末的记账块
    # 两道闸:① `list_allow` —— 本号在不在本轮的**轮转窗口**里(额度只有 ~29 个号,
    # 而一轮 75 个;不在窗口里的号只取 cover,把额度留给别人,下轮轮到它);
    # ② `breaker["list_off"]` —— 本轮已经被额度挡过一次,剩余号一律不再问。
    list_skipped = (not list_allow) or bool(breaker is not None and breaker.get("list_off"))
    if not list_skipped:
        try:
            # ★ **账号池**(2026-10-18):一直打同一枚账号,它被 `-2041` 限掉之后整条链归零
            # (10-11 起连续 8 天 `ok=0`)。池里只有一个账号时,`ask_list` 与下面那句旧写法
            # **逐字等价**(同一个 scope `web_list`、同一个 `allow_probe=False`、同一个异常)。
            if pool is not None:
                payload, _acct = pool.ask_list(
                    session, user_id, b.weread_book_id,
                    what=f"mp/articles({b.nickname})")
                if stats is not None and len(pool) > 1:
                    by = stats.setdefault("weread_list_ok_by_acct", {})
                    by[_acct] = by.get(_acct, 0) + 1
            else:
                payload = weread_budget.call(
                    session, user_id, lambda: weread.mp_articles(b.weread_book_id),
                    what=f"mp/articles({b.nickname})")
            _append_listed(items, _WC.flatten_mp_articles(payload), cutoff)
            listed, list_via = True, "web"
        except weread_budget.Blocked as exc:
            # ⚠️ **熔断中 = 我们主动没发请求**,不是"这个源坏了"。合闸后本轮不再问。
            # 与"发出去了被拒"必须分开 —— 前者是保护、后者才是风控信号。
            if breaker is not None:
                breaker["list_off"] = True
            logger.debug("%s", exc)
        except Exception as exc:  # noqa: BLE001 - 限权/废弃不影响 cover 主路径
            # -2041 = **这个微信读书账号**在该接口上被限(账号级,2026-10-18 三种上下文复验);
            # 每进程只记一次,避免每账号刷屏。换号能否解决见 `weread_accounts`。
            if not getattr(_weread_collect, "_mp_articles_warned", False):
                _weread_collect._mp_articles_warned = True
                logger.warning("mp/articles 不可用(%s),全部账号仅用 cover 最新一篇", exc)
            if breaker is not None and _is_weread_quota_error(exc):
                breaker["list_off"] = True
    # **App 侧兜底**(2026-10-05):网页 `/web/mp/articles` 已被账号级拦截 `-2041`
    # (控制变量实验:刚续期 1 分钟内、三种上下文、8 个号全挂),而 App 的
    # `i.weread.qq.com/book/articles` **同源同数据、带精确 readNum,且没被拦**。
    # ⇒ 网页路拿不到时改问 App 路,把**已经断了一周的阅读数**接回来。
    # ⚠️ 只在网页路没成功时走(App 有自己的配额,未知上限 —— 不无谓地双倍消耗);
    # ⚠️ **记账要分路**(`weread_list_app_ok` 而不是 `weread_list_ok`):合在一起就再也看不出
    #    "网页路到底恢复了没" —— 而"能不能恢复"决定这条兜底是临时的还是永久的。
    # ⚠️ **这里改过一次,记下来免得改回去**(2026-10-05 同一天内):
    #    第一版写的是 `not list_skipped`(审计建议),理由是"窗口外的号别去打 App"。
    #    **那个理由对了一半,但用错了变量**:`list_skipped` 是
    #    `(不在窗口) or (本轮网页已熔断)` 两个条件的**或** —— 用它挡 App,
    #    就等于"网页一被挡,唯一还能用的 App 也一起停" ⇒ **兜底在最需要它的时候失效**。
    #    正确的窗口纪律来自 `list_allow`(这个号在不在本轮窗口),而 Web 的熔断
    #    **不应该**连累 App(App 是另一套鉴权与配额)。
    # ⚠️ 用 **`list_allow`(在不在本轮窗口)**,不是 `list_skipped`:
    #    `list_skipped` 把两件事混成一个布尔 —— ⒜"这个号不在窗口里"(该省额度)
    #    和 ⒝"网页本轮已熔断"。用 ⒝ 去挡 App 是**错的**:App 是另一套鉴权与配额,
    #    **网页恰好被挡的时候,正是 App 该顶上的时候**(兜底的全部价值就在这)。
    #    窗口纪律照旧(⒜ 仍然省),但不再让网页的熔断连累唯一还能用的那条路。
    #    (这是 `test_listen_counts_quiet_rounds_on_weread_path` 与额度收窄一起逼出来的。)
    # ⚠️ **用 `app_allow`,不是 `list_allow`**(2026-10-10):两条路的窗口现在是**分开**的 ——
    # 网页窗口吃前 `_LIST_WINDOW` 个(App 不必重复去问,省它的配额),
    # 而 App 还额外吃紧跟其后的 `wechat_listen_app_extra` 个(那些号网页**吃不到**,
    # 正是覆盖率卡在 44% 的原因)。不传 `app_allow` 时退化成 `list_allow`(旧行为)。
    _app_ok = bool(list_allow if app_allow is None else app_allow)
    if not listed and _app_ok and app_client is not None:
        try:
            # ⚠️ **scope=app**:App 是另一套鉴权与配额,网页被 `-2041` 拦了它照样能取 ——
            # 这正是兜底的价值。熔断按通道分开记,别让网页的挡牵连唯一还能用的这条路。
            if getattr(app_client, "scoped_budget", False):
                #: ★ 2026-10-19:池内**按账号**分别记熔断(`app_list` / `app_list@weread_app#2`)。
                #: 以前整个池共用一个 scope:1 号被 `-2041` 挡下后,外层 `call` 在调用池
                #: **之前**就抛 `Blocked` ⇒ 2 号根本没机会 —— 这就是"两个号却整轮 app=0"。
                rows = app_client.articles(
                    b.weread_book_id, session=session, user_id=user_id,
                    allow_probe=not listed)
            else:
                #: 测试替身/裸客户端没有 `scoped_budget` ⇒ 保持旧的共享 scope 行为。
                rows = weread_budget.call(
                    session, user_id, lambda: app_client.articles(b.weread_book_id),
                    what=f"app/book/articles({b.nickname})", scope=weread_budget.SCOPE_APP,
                    allow_probe=not listed)
            # ⚠️ **不再翻译字段**:App 端产出的形状已与网页端 `flatten_mp_articles` 逐字一致
            # (2026-10-05 收敛)—— 之前这里有一层 `publish_at→create_time` 的映射,
            # 那正是"两处同构、迟早飘一个"的来源。
            _append_listed(items, rows, cutoff)
            listed, list_via = True, "app"
        except weread_budget.Blocked as exc:
            logger.debug("%s", exc)
            #: **被熔断挡下也要计数**(2026-10-10):它与"发出去了被拒"是两件事
            #: (前者是我们主动不发、保护账号),但**都必须看得见** —— 否则"App 路整轮没动"
            #: 在运行记录里跟"这一轮不需要问 App"长得一模一样。
            if stats is not None:
                stats["weread_list_app_blocked"] = stats.get("weread_list_app_blocked", 0) + 1
        except Exception as exc:  # noqa: BLE001 - 兜底也失败 = 本号没答案,但不影响 cover
            # ⚠️⚠️ **兜底也断了必须看得见**(2026-10-06 实测)。这里原来打的是 `debug`,
            # 而生产日志级别是 INFO ⇒ **App 兜底全断,日志里一个字都没有**。
            # 代价:这条路(10-05 为"把断了一周的阅读数接回来"而加)**一次都没成功过**,
            # 也没有任何人知道 —— 每天 17 轮静默失败,看起来和"今天没人发文章"一样。
            # 每轮只喊一次(142 个号会刷屏),并把它记进 `wr_stats` 供运行记录统计。
            # ⚠️ **2026-10-10 补:光有"喊一次"也不够** —— 那个标志只让日志打一行,
            # 而**运行记录里 `app=` 只统计成功**,于是"App 路 22 轮里 21 轮全是 0"
            # 这种事实,在报告里看着和"这一轮不需要问 App"完全一样。
            # ⇒ 现在**失败也计数**,汇总行进 detail。
            if stats is not None:
                stats["weread_list_app_fail"] = stats.get("weread_list_app_fail", 0) + 1
                #: ★ **把错误码写进运行记录**(2026-10-22):`-2012`(token/身份档失效,
                #: 该开模拟器重取)与 `-2041`(额度,该等)修法正相反,原来都只记成 `app_fail+1`。
                if not stats.get("weread_list_app_err"):
                    stats["weread_list_app_err"] = weread_budget.error_mark(exc)
            if not (stats or {}).get("weread_app_fail_logged"):
                if stats is not None:
                    stats["weread_app_fail_logged"] = 1
                logger.warning("App 列表兜底失败(%s):%s —— **App 这条路当前是断的**,阅读数只剩网页路",
                               b.nickname or b.weread_book_id, str(exc)[:140])
                # ⚠️⚠️ **光写日志还是没人看见** —— 运营者不读日志。
                #
                # ★★★ **必须先分"额度"与"登录态"两刀**(2026-10-13 实测定性)。
                # 判据不靠猜:同一枚新铸 token 去问,/shelf/sync 与 /user/notebooks **回 200**
                # (认证是过的),只有 `/book/articles` 回 `-2041` ⇒ 接口的顺序是
                # **先校认证、后校额度**,能走到额度这一步就**反证了认证通过**。
                # 不分的代价(2026-10-11~13 真实发生的):真因是 `-2041`,
                # 报出来的却是 `-2012 登录超时` —— 因为 `WereadAppClient.articles()`
                # 在自愈拿不到新 token 时**重抛的是原始错误**,于是告警把人引向
                # "开雷电模拟器重登",而那只既修不了、还多一次登录自证(风控更重)。
                try:
                    from app.services.alert_service import notify_incident

                    if _is_weread_quota_error(exc):
                        # 额度/端点级被挡 ⇒ **不是登录问题**,正确处置是"别动"。
                        # ⇒ 落**站内告警**(`push_feishu=False`,与"要不要自建 WeRSS"同类):
                        #   它是"要不要换号 / 要不要降频"的长期决策项,不是"现在去动手"
                        #   的即时事件 —— 丢进飞书群只会训练人忽略告警。
                        # 为什么不能干脆静默:这正是本仓最忌的**假绿灯** ——
                        # "精确阅读数全断"会在报告里长得像"今天没人发文章"(10-06 已经吃过一次)。
                        notify_incident(
                            session, user_id, "wechat",
                            "🟠 微信读书 App 路被挡(-2041),精确阅读数无来源",
                            "App 兜底路(`i.weread.qq.com/book/articles`)回**额度/端点级**错误,\n"
                            "⚠️ **这不是登录态失效** —— 别开雷电、别无谓重登。实测(2026-10-13):\n"
                            "  · 纯 HTTP 铸 token **成功**(`refreshToken` 有效,`RefreshTokenExpired=0`);\n"
                            "  · 同一枚新 token 问 `/shelf/sync`、`/user/notebooks` ⇒ **HTTP 200**;\n"
                            "  · 只有 `/book/articles` ⇒ `-2041`(换书、换身份档都一样)⇒ 端点级。\n"
                            "⇒ 精确阅读数当前**没有任何来源**:网页 `/web/mp/articles` 早被账号级\n"
                            "`-2041` 拦(2026-10-04 起),App 这条路自 **2026-10-11 07:52** 起同样被挡。\n"
                            "需要决策:换个微信读书账号出新会话,或接受「只能拿到最新一篇」。",
                            push_feishu=False)   # 长期决策项 ⇒ 落站内,不丢进飞书群
                    elif _app_auth_needs_human(exc):
                        # 真·登录态类 ⇒ **要人动手**(等多久都不会自己好),该推飞书。
                        notify_incident(
                            session, user_id, "wechat",
                            "🟠 微信读书 App 登录态失效,精确阅读数断了",
                            "App 兜底路(阅读数的最后一条路)取不到数据。\n"
                            "⚠️ **先分是不是额度类**:若日志里是 `-2041/-2014` ⇒ 那是端点级被挡,\n"
                            "**不要开模拟器**(实测无效),见同时落下的那条站内告警。\n"
                            "确认是登录态类(`-2012/-2010`)再照下面走 ——\n"
                            "⚠️ **2026-10-10 起续期已是纯 HTTP**(签名 `sha256(timestamp+deviceId+random)`,"
                            "无需模拟器),所以先看运行记录里的 `app_fail` 与自愈日志:\n"
                            "  · 若报「HTTP 铸 token 失败」⇒ 多半是 **`refreshToken` 本身过期/被吊销**,\n"
                            "    那才需要**开一次**雷电模拟器读账号库重取(用 "
                            "`scripts/weread_app_token.py`),**不必像以前那样一直开着**;\n"
                            "  · 若报「身份不配套 / -2012」⇒ token 与客户端身份档对不上(见 "
                            "`EINK_HEADERS` 的注释):纯 HTTP 铸出来的**只能是 eink 档**,\n"
                            "    必须配 `profile=\"eink\"` 去调,拿安卓 App 的头一定回 `-2012`。")
                except Exception:  # noqa: BLE001 - 告警失败不影响监听
                    logger.debug("App 路失效告警推送失败", exc_info=True)
    # 正文:先直抓 mp.weixin.qq.com(不占微信读书配额),**抓空了再用这篇的 reviewId
    # 走微信读书转发页**。此前这里只传 fetch_content=True,把 cover/列表白拿的 reviewId 丢了,
    # 于是直抓被风控的那 26% 正文永远为空 → 盘链认不出 → 飞书卡片整片"—"而员工以为号没发资源
    # (本机库实测 369 篇里 97 篇正文空、97 篇全部无盘链;「同步文章」走 mp_content 就没这问题)。
    rid_of = {it["url"]: it["review_id"] for it in items if it.get("review_id")}

    def _resolve(title: str, url: str = "") -> str:
        body = _root.fetch_article_content(url)
        reason = _detect_ban_reason(body)
        if reason:
            # 封文/封号页不当正文入库(污染检索与分析),状态带出去给卡片标 ⛔
            if banned_out is not None and url:
                banned_out[url] = f"{title[:24]}·{reason}"
        elif body:
            return body
        rid = rid_of.get(url) or ""
        if not rid:
            return ""
        alt = weread.mp_content(rid)   # 惰性回退:直抓成功就绝不追打转发页(2s 节流=风控暴露)
        reason = _detect_ban_reason(alt)
        if reason:
            if banned_out is not None and url:
                banned_out[url] = f"{title[:24]}·{reason}"
            return ""
        return alt

    # require_pan=False:不再丢弃无盘链文——"标题不含网盘词"≠"没价值",
    # 此前这道闸把 15 个对标号 10 天的新文全部静默丢弃(用户看到"停更在 9.7"的根因)
    got = _insert_new_articles(session, user_id, b, items, source="listen",
                               content_resolver=_resolve, require_pan=False)
    if banned_out and got:
        for r in got:
            if r.url in banned_out:
                # 沿用 41031 的标记位语义:卡片网盘列认「原文失效」显示 ⛔,免得员工白点尸体链
                r.my_pan_urls = f"⚠️原文失效({banned_out[r.url]}),未转存"
    if stats is not None:
        if list_via == "app":
            # **单独一个键**:和 `weread_list_ok` 合在一起,就再也看不出"网页路恢复了没"
            key = "weread_list_app_ok"
        elif listed:
            key = "weread_list_ok"
        elif got:
            # 列不出却采到新文 = 同日其它篇**未知丢失**,必须进铁律告警,不能因为
            # "本轮被熔断没问列表"就把它记成无害的 off
            key = "weread_list_off_new"
        elif list_skipped:
            key = "weread_list_skipped"
        else:
            key = "weread_list_off"
        stats[key] = stats.get(key, 0) + 1
    # ★ **两条都没答上、且 cover 是登录态错** ⇒ 抛回去让调用方续期(与旧行为一致)。
    #   区别只有一处:列表(App)**答上了就不抛** —— 那正是修复的目的
    #   (网页 cookie 过期不该连累唯一能出阅读数的 App 路)。
    if cover_auth is not None and not listed:
        raise cover_auth
    return got, (cover_ok or listed)


def _listen_lock_key(user_id: int) -> str:
    return f"wechat_listen_running_{user_id}"
_SHELF_REVIEW_KEYS = ("reviewId", "review_id", "lastReviewId", "latestReviewId", "mpReviewId")
_SHELF_TS_KEYS = ("lastChapterCreateTime", "updateTime")
def _shelf_slot(book_id: str, every: int) -> int:
    """每号在"强制问询"轮转里的固定槽位(bookId 散列:与排序无关,增删号不影响别人的节奏)。"""
    return zlib.crc32(str(book_id).encode("utf-8")) % max(1, every)
def _bump_shelf_round(session: Session, user_id: int) -> int:
    """粗筛轮计数器(system_config):强制问询按"轮数+号槽位"轮转,返回自增前的轮数。"""
    from app.db.models import SystemConfig

    key = f"weread_shelf_round_{user_id}"
    row = session.get(SystemConfig, key)
    try:
        cur = int(str(row.value)) if row and row.value else 0
    except ValueError:
        cur = 0
    val = str(cur + 1)
    if row is None:
        session.add(SystemConfig(key=key, value=val, updated_at=datetime.now()))
    else:
        row.value = val
        row.updated_at = datetime.now()
    return cur
def _load_shelf_marks(session: Session, user_id: int) -> dict[str, str]:
    """书架水位(JSON 落 system_config):每号上次「问过且答上」时看到的信号值。"""
    from app.db.models import SystemConfig

    row = session.get(SystemConfig, f"weread_shelf_marks_{user_id}")
    try:
        data = json.loads(row.value) if row and row.value else {}
    except ValueError:
        data = {}
    return {str(k): str(v) for k, v in data.items()}
def _save_shelf_marks(session: Session, user_id: int, updates: dict[str, str]) -> int:
    """水位前移(写入 system_config JSON),返回前移的号数。

    只收「问过且答上了」的号是水位机制的安全底线:问都没问成(额度熔断/请求失败)
    就前移,等于把没采到的那篇永久记成"见过"——下一轮书架值不变就跳过,漏推再也补不回来。
    """
    from app.db.models import SystemConfig

    if not updates:
        return 0
    key = f"weread_shelf_marks_{user_id}"
    marks = _load_shelf_marks(session, user_id)
    marks.update(updates)
    val = json.dumps(marks, ensure_ascii=False)
    row = session.get(SystemConfig, key)
    if row is None:
        session.add(SystemConfig(key=key, value=val, updated_at=datetime.now()))
    else:
        row.value = val
        row.updated_at = datetime.now()
    return len(updates)
def _shelf_ts_to_dt(value) -> datetime | None:
    """书架 lastChapterCreateTime(unix 秒)→ 发布时间;离谱值一律 None(宁缺勿错)。"""
    try:
        n = int(str(value))
    except (TypeError, ValueError):
        return None
    if not 1_500_000_000 <= n <= 4_000_000_000:
        return None
    return datetime.fromtimestamp(n)


def _needs_reads_24h(session: Session, b: WechatBenchmark, gate: dict,
                     cutoff: datetime) -> bool:
    """这个号近 **24h** 有没有新文?有才值得花列表额度(精确阅读数)。

    ★ 2026-10-20 用户口径:「**新文优先,24 小时之内的都算新文**」。
    判据按"最便宜 → 最贵":
      ① 书架信号 `lastChapterCreateTime` ≥ 24h 前 ⇒ 有更新,直接查;
      ② 信号是旧的 ⇒ 再查库(免费列表可能已经先入库);近 24h 入库/发布也算新;
      ③ 信号缺失/认不出 ⇒ **查**(判不了就不判死 —— 宁可多一次,也别让新文永远没读数)。
    """
    sig = (gate or {}).get("signals", {}).get(b.weread_book_id)
    dt = _shelf_ts_to_dt(sig)
    if dt is not None and dt >= cutoff:
        return True
    if dt is None:
        #: 书架没这个号/字段认不出 ⇒ 不能证明它旧,查一次。
        return True
    #: 书架说旧 ⇒ 查库兜底:免费列表/同步可能已经把新文写进来了。
    try:
        n = session.scalar(
            select(func.count()).select_from(WechatArticle).where(
                WechatArticle.benchmark_id == b.id,
                or_(WechatArticle.publish_at >= cutoff,
                    and_(WechatArticle.publish_at.is_(None),
                         WechatArticle.created_at >= cutoff)),
            )
        ) or 0
        return bool(n)
    except Exception:  # noqa: BLE001 - 查库失败就保守地查(宁可多一次)
        return True


def _shelf_gate_plan(session: Session, user_id: int, rows: list[WechatBenchmark],
                     weread: WereadClient | None, settings: Settings) -> dict:
    """书架粗筛(降频主刀):1 次书架请求回答「哪些号自上次问到之后没有更新」。

    判据(2026-09-27 线上实勘后定稿):书架条目的 lastChapterCreateTime == 该号水位
    (上次「问过且答上」时看到的同一字段值,JSON 落 system_config)→ 服务端没更新过
    这个号的最新章节,cover 只会吐我们已入库的那篇旧文,问它纯属白问。量化收益
    (本机 9.22 快照):每轮 81 次 cover 只有 4~20 个号真吐新文,其余 60~77 次白问
    正是烧光会话额度、让真有文的号被 -2014 挡掉的元凶。

    与首版(reviewId 比对文章 URL)的差异:线上实勘证明书架**不带 reviewId**,
    时间戳没有"可从库里反解"的对应物,水位必须自己持久化;由此多出一条铁律——
    **水位只对问过且答上的号前移**(_save_shelf_marks),问都没问成就前移等于把
    没采到的那篇永久记成"见过",那是本机制唯一会造永久漏推的错。

    失效保护,任何一环不确定都整门停用(代价只是白付 1 次书架请求):
    - 配置关停 / 客户端没有 shelf_entries(测试假件)/ 书架请求失败 → 逐号照旧;
    - 条目里认不出信号字段(名单见 _SHELF_REVIEW_KEYS/_SHELF_TS_KEYS)→ 跳过集为空;
    - 水位匹配不搞永久豁免:每号每 `weread_shelf_gate_every` 轮强制真问一次 cover,
      "书架字段滞后"这种最坏错判的盲区被压到 ≤K 轮(K=4 → 每号每天至少被真问一次)。

    返回:{ok, skip(本轮可跳过的 bookId 集), force(到期必须真问的), tier(排序权重:
    0=有更新/无水位优先问,1=强制问询,2=证实没更新), signals(每号本轮信号值), reason}。
    """
    plan: dict = {"ok": False, "skip": set(), "force": set(), "tier": {},
                  "signals": {}, "reason": ""}
    if not getattr(settings, "weread_shelf_gate", True) or weread is None:
        plan["reason"] = "disabled"
        return plan
    shelf_entries = getattr(weread, "shelf_entries", None)
    if not callable(shelf_entries):
        plan["reason"] = "no_shelf_entries"
        return plan
    try:
        entries = shelf_entries()
    except Exception as exc:  # noqa: BLE001 - 书架挂了不能连累监听:退化为逐号问
        plan["reason"] = type(exc).__name__
        logger.warning("书架粗筛请求失败,本轮退化为逐号问(%s)", exc)
        return plan
    signals: dict[str, str] = {}
    for it in entries or []:
        bid = str(it.get("bookId") or "")
        if not bid:
            continue
        sig = ""
        for k in _SHELF_REVIEW_KEYS:      # reviewId 优先:文章身份,判据最强
            v = str(it.get(k) or "").strip()
            if v:
                sig = v
                break
        if not sig:
            for k in _SHELF_TS_KEYS:      # 实勘:线上只有时间戳可用(已自证)
                if it.get(k) not in (None, ""):
                    sig = str(it[k])
                    break
        if sig:
            signals[bid] = sig
    plan["signals"] = signals
    if not signals:
        plan["reason"] = "no_signal_field"
        return plan
    marks = _load_shelf_marks(session, user_id)
    every = max(1, int(getattr(settings, "weread_shelf_gate_every", 4) or 4))
    rnd = _bump_shelf_round(session, user_id)
    for b in rows:
        sig = signals.get(b.weread_book_id)
        mark = marks.get(b.weread_book_id)
        if sig and mark and (sig == mark or sig.endswith("_" + mark)):
            if (rnd + _root._shelf_slot(b.weread_book_id, every)) % every == 0:
                plan["force"].add(b.weread_book_id)
                plan["tier"][b.id] = 1     # 保险单:排在"可能有更新"的号后面问
            else:
                plan["skip"].add(b.weread_book_id)
                plan["tier"][b.id] = 2
        else:
            plan["tier"][b.id] = 0         # 有更新/首轮无水位/书架没这号:优先问
    plan["ok"] = True
    return plan
def _acquire_listen_slot(session: Session, user_id: int, settings: Settings) -> str | None:
    """抢占"这一用户的一轮监听"执行权(跨进程/跨 worker 的时长锁)。抢到给令牌,抢不到给 None。

    `claim_schedule` 的乐观锁只保护**抢占那一刻**(比较 last_run_at),而一轮监听要跑几分钟:
    期间用户再点一次「立即监听」、或后台 `retry_failed_runs` 撞上在跑的定时轮,两轮就会并行
    扫同一批号——同一篇新文发两张卡、微信读书密度翻倍招风控
    (2026-09-26 第九轮审计)。标记落在 `system_config`(整库可见),超过 TTL 视为持有进程已死,
    允许后来者接管:宁可重跑一轮,也不能因没解锁而把该用户的监听永久锁死。
    """
    from app.db.models import SystemConfig

    key = _listen_lock_key(user_id)
    now = datetime.now()
    stale_before = now - timedelta(minutes=max(1, int(settings.wechat_listen_lock_ttl_minutes or 20)))
    token = uuid.uuid4().hex
    if session.get(SystemConfig, key) is None:
        try:
            with savepoint(session):     # 插入冲突=别人刚抢到,只撤销这一段
                session.add(SystemConfig(key=key, value=token, updated_at=now))
                session.flush()
        except IntegrityError:
            return None
        session.commit()
        return token
    res = session.execute(
        update(SystemConfig).where(
            SystemConfig.key == key,
            or_(SystemConfig.value == "", SystemConfig.updated_at.is_(None),
                SystemConfig.updated_at < stale_before),
        ).values(value=token, updated_at=now))
    session.commit()
    return token if res.rowcount else None
def _release_listen_slot(session: Session, user_id: int, token: str) -> None:
    """解锁:只删自己那把令牌(接管过的后来者令牌不同,不会被误删)。"""
    from app.db.models import SystemConfig

    try:
        with savepoint(session):
            session.execute(delete(SystemConfig).where(
                SystemConfig.key == _listen_lock_key(user_id), SystemConfig.value == token))
            session.flush()
        session.commit()
    except Exception:  # noqa: BLE001 - 解锁失败只能等 TTL 过期,不能盖掉本轮结果
        logger.exception("释放监听在跑标记失败 user=%s(该用户在标记过期前不能再起一轮)", user_id)
def run_wechat_listen(session: Session, user_id: int, settings: Settings | None = None,
                      weread: WereadClient | None = None,
                      platform: ReaderPlatformClient | WerssClient | None = None, push: bool = True,
                      batch_index: int | None = None, batch_size: int | None = None) -> dict:
    """监听一轮(外层是"同一用户不并发"的时长锁,内层 `_listen_round` 才是采集本体)。

    所有入口——手动「立即监听」、调度器定时轮、管理端失败重试——都走这里,所以防重只需要
    在这一处做实。被挡住的一轮返回 `skipped/running`,不产生采集副作用。
    """
    st = _base(settings)
    token = _acquire_listen_slot(session, user_id, st)
    if token is None:
        _record_run(session, user_id, "wechat_listen", "skipped",
                    "running(上一轮监听尚未结束)")
        session.commit()
        logger.warning("公众号监听跳过 user=%s:上一轮仍在执行", user_id)
        return {"platform": "wechat", "status": "skipped", "reason": "running"}
    try:
        return _root._listen_round(session, user_id, settings=settings, weread=weread,
                             platform=platform, push=push, batch_index=batch_index,
                             batch_size=batch_size)
    finally:
        #: ⚠️ **失败后先恢复 session 再解锁**(2026-10-19):`database is locked` 会让
        #: session 进入「必须 rollback」状态,而 `_release_listen_slot` 也要写库 ⇒
        #: 解锁失败 ⇒ 该用户在整个 TTL 内**不能再起一轮**(04:02 就是这么被锁死的)。
        #: 只在 `is_active=False`(确实处在失败事务)时回滚,正常轮次的未提交写不受影响。
        if not session.is_active:
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                logger.debug("释放监听标记前回滚失败", exc_info=True)
        _release_listen_slot(session, user_id, token)
def _listen_round(session: Session, user_id: int, settings: Settings | None = None,
                  weread: WereadClient | None = None,
                  platform: ReaderPlatformClient | WerssClient | None = None, push: bool = True,
                  batch_index: int | None = None, batch_size: int | None = None) -> dict:
    """监听一轮:纯免费源——免费列表(WeRSS/读书平台)→ 微信读书(cover/列表)→ 新文入库推飞书。

    (2026-09-29 用户决策:dajiala 收费链整体摘除,免费源不可用即如实记 failed/partial。)
    全部数据源不可用才返回 `skipped`。
    """
    settings = _base(settings)
    # 开工先补上一轮的欠推:飞书抖动/超时/进程被杀留下的"已入库未推送"文章在这一轮补上。
    # 关在保存点里——补推本身炸了不能伤到本轮采集(与后处理同一条原则)。
    repushed = 0
    try:
        with savepoint(session):
            repushed = repush_unpushed(session, user_id, settings)
    except Exception:  # noqa: BLE001 - 补推失败不影响本轮监听
        logger.exception("补推上轮欠推失败 user=%s", user_id)
        #: ⚠️⚠️ **必须 rollback**(2026-10-19 04:02/08:02 的实际事故):补推里的
        #: `database is locked` 会把 session 标成「必须回滚」,不恢复的话后面**采集第一步**
        #: 就抛 `PendingRollbackError`,整轮监听被一条旁路任务带崩(而且释放"在跑"标记也失败)。
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 - 回滚本身失败只能记下来,别盖住原异常
            logger.debug("补推失败后回滚会话失败", exc_info=True)
    all_rows = session.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.active.is_(True))
        .order_by(WechatBenchmark.id)).all()
    if not all_rows:
        # 跳过也记运维记录:否则后台"没有公众号情况",无从判断是没加号还是没跑
        _record_run(session, user_id, "wechat_listen", "skipped", "no_benchmarks(未添加对标号)")
        session.commit()
        return {"platform": "wechat", "status": "skipped", "reason": "no_benchmarks"}
    # 自适应分批 + 权重分层(2026-10-01,随号池扩建自动适配;见 _select_listen_batch):
    # 批大小按池子规模自适应、沉睡号降频;cursor=原始游标值(重试路径据此重跑同一组)。
    rows, batch_pos = _select_listen_batch(session, user_id, all_rows, settings,
                                           batch_index=batch_index, batch_size=batch_size)
    cookie = _root._weread_cookie(session, user_id, settings)
    if not cookie:
        _record_run(session, user_id, "wechat_listen", "skipped",
                    "no_source(无微信读书 Cookie,免费列表源未配置或不可用)")
        session.commit()
        return {"platform": "wechat", "status": "skipped", "reason": "no_source"}
    # ★ **微信读书账号池**(2026-10-18):精确阅读数唯一来源 `/web/mp/articles` 的额度
    #   **按账号**记,且会被账号级 `-2041` 限死(10-11 起连续 8 天 `ok=0`,而同一份
    #   刚续期的 cookie 下 shelf/cover 全 200)。所以要"很全的阅读数",只能多账号。
    #   只配了一枚时,**池里就是这一枚**,与旧行为逐字一致。
    from app.services import weread_accounts as _wa

    # ⚠️ **调用方显式注入了客户端 ⇒ 不建池**(测试替身/手动指定):那条路的每个请求都该
    #   打到注入的那个对象上;建池会让它绕过替身去打真网络(第一次跑测试就是这么红的)。
    pool = None if weread is not None else _wa.build(session, user_id, settings).with_base(cookie)

    plat = platform or _root._platform_client(settings, session=session, user_id=user_id)
    # 书架粗筛(1 次书架请求换"谁没更新"的答案):把每轮 60~77 次白问省下来。
    # 任何不确定(字段认不出/书架挂了/配置关停)都整门停用,退化为逐号问
    # (判据与三重失效保护见 _shelf_gate_plan;量化依据见 doc/operations.md §9.2)。
    weread = weread or (_root.WereadClient(cookie) if cookie else None)
    gate = _shelf_gate_plan(session, user_id, rows, weread, settings)
    if gate["ok"]:
        # 稳定排序把"可能有更新/无水位"的号排到队头:额度/熔断真触发时,稀缺的请求
        # 先花在最可能吐新文的号上(书架说没更新、仅到强制问询期的号殿后)
        rows = sorted(rows, key=lambda b: gate["tier"].get(b.id, 0))
    now = datetime.now()
    new_rows: list[WechatArticle] = []
    failed = 0
    # 微信读书"近期列表"可枚举性统计:决定"近24h全推"能不能兑现(见 _weread_collect)
    wr_stats: dict = {}
    # 额度熔断:微信读书按会话/IP 给请求记账,一轮 162 次(81 号 × cover+列表)远超它的容忍线。
    # 被额度类错误(-2014/-2041)挡下后继续让剩余号逐个去撞,只会把风控加深、让队尾整轮挨饿,
    # 所以这里合闸:列表先停,连续 _COVER_QUOTA_TRIP 个号 cover 也挡不下就整源停。
    breaker: dict = {"list_off": False, "cover_quota_fails": 0, "off": False}
    # ⚠️ **跨路径共享的额度熔断**(2026-10-05):`breaker` 是**轮内**的 ——
    # 它管得住本轮,管不住"同步那条路也被挡了""上一轮被挡过"。
    # 这里查一次共享状态(轮级只查一次,不是每号一次)。
    # ⚠️ **只合 `list_off`,不合 `off`**:共享熔断记的是**列表预算**(`mp/articles`),
    # 而实测 `-2041` 打的是列表的额度 —— 同一时刻 **cover 与 shelf 都好好的**。
    # 把 cover 一起停掉 = 每个号连"当天有没有发文"都答不上,那才是真瞎。
    # (这条是 `test_listen_counts_quiet_rounds_on_weread_path` 当场逼出来的。)
    try:
        _blk = weread_budget.blocked_until(session, user_id)
        if _blk is not None:
            breaker["list_off"] = True
            logger.info("微信读书列表额度熔断中(至 %s):本轮不问列表,cover 照常",
                        _blk.strftime("%H:%M"))
    except Exception:  # noqa: BLE001 - 查不到就当没熔断(宁可多打一次,也别把整轮卡死)
        logger.debug("读共享额度熔断失败", exc_info=True)
    #: ★★ 2026-10-20 **新文优先**(用户口径:「24 小时之内的都算新文」):
    #: 列表额度不再按"轮转窗口"平均分,而是**只花在有新文的号上**。
    #: 旧窗口(`_list_windows` / `wechat_listen_app_extra`)保留函数供体检/测试使用,
    #: 但本轮不再用它决定"问不问列表" —— 判据见 `_needs_reads_24h`。
    cutoff24 = datetime.now() - timedelta(hours=24)
    # **App 侧客户端**(2026-10-05):整轮只建一次;没配就 None,兜底自动跳过。
    # 兜底存在的理由:网页 `/web/mp/articles` 被账号级拦截,而它是**精确阅读数**的唯一来源
    # (见 weread_app_client.py 头注释)。不建这一步,阅读数就永远是 0。
    app_client = _weread_app_client(session, user_id)
    listed_keys: set[str] = set()      # 本轮**真问到**列表的号 → 轮末记号(问不到的不记)
    quota_skipped = 0
    no_free_source = 0
    marks_advance: dict[str, str] = {}   # 本轮「问过且答上」的号 → 书架信号值(轮末前移水位)
    banned: dict[str, str] = {}          # 本轮被封/被删的文章 url → 标题·违规类别(清扫预警用)
    for b in rows:
        used = False
        # ⓪ 免费全量列表(自建 WeRSS 或 wewe-rss 兼容的读书平台):biz 是源认识的形态才首选
        feed_id = feed_biz(b)
        if plat and feed_id:
            try:
                # ★ **横向补阅读数**(2026-10-14):`mp_articles` 一见到"有内容"的源就 return,
                # 而**只有自研 wemp(后台 appmsgpublish)那一路带阅读数**(WeRSS 结构里没有
                # 这个字段)⇒ 只要 WeRSS 活着,wemp 永远问不到,卡片上就永远是 `—`。
                # 所以这条首选路改用 `mp_articles_with_reads`:列表仍以第一个答上话的源为准,
                # 其余源只用来补 read_num/like_num(详见 `_source._merge_counts`)。
                # 单源客户端 / 测试替身没有这个方法 ⇒ 原样退回 `mp_articles`,行为逐字不变。
                # ★ **统一入口**:`ask_platform` 既是"横向补阅读数"的优先逻辑所在,
                # 也是**熔断**所在(单源时裸客户端没有熔断 ⇒ 一层要兜住;见它的说明)。
                raw_items = _root.ask_platform(plat, feed_id, page=1, limit=20,
                                               stats=wr_stats)
                # ⚠️ **阅读/点赞必须往下带**(2026-10-14 修):这里原来只取 title/url/时间,
                # 把 `read_num` / `like_num` **白丢掉** —— 于是哪怕源(自研 wemp 后台那一路)
                # 答上了阅读数,`_insert_new_articles` 收到的也是 0,卡片上照样是 `—`。
                # 而 `_insert_new_articles` **本来就支持**(`preset_read`,且只在"旧值 0、
                # 新值 >0"时回填),所以这是纯丢数据、不是没能力。取不到就是 None ⇒ 与旧行为逐字一致。
                norm = [{"title": it["title"], "url": it["url"],
                         "read_num": it.get("read_num"), "like_num": it.get("like_num"),
                         "publish_at": _parse_time(it.get("publish_at_raw"))} for it in raw_items]
                if norm:
                    used = True
                    b.miss_count = 0
                    b.last_item_at = now
                    got = _insert_new_articles(session, user_id, b, norm, source="listen",
                                               fetch_content=True, require_pan=False)
                    if got:
                        new_rows.extend(got)
                else:
                    # 首页就空 ≠ 这个号今天没发文:更可能是这个订阅在源里不存在/还没抓到
                    # (WeRSS 里没加、或它的 weread 模式被限权)。不能当采集成功——否则 ① 的 cover
                    # 也被挤掉,该号整轮失明还表现为"连续 N 轮未发文"。留给后续源回答。
                    logger.warning("免费列表 %s 返回空(biz=%s),本轮交由后续源", b.nickname, feed_id)
            except PlatformError as exc:
                logger.warning("读书平台监听 %s 失败,降级后续源:%s", b.nickname or b.biz, exc)
        # ① 微信读书(免费):对标号已关联 bookId 且有 Cookie;登录失效时自动续期重试一次
        wr_eligible = (not used) and bool(cookie) and bool(b.weread_book_id)
        if not used and not wr_eligible and not plat:
            no_free_source += 1  # 免费列表源缺失+无 bookId 的手动号:本轮结构性失明,计入运维记录
        if wr_eligible and breaker["off"]:
            # 整源已停:本号这一轮"什么都不知道",不能算 answered(故不动 miss_count),
            # 但必须计数暴露——否则运维记录会长得跟"81 个号都问过了、只是没新文"一样。
            quota_skipped += 1
        elif wr_eligible and b.weread_book_id in gate["skip"]:
            # 书架水位未变:cover 只会吐库里已有的那篇旧文,这一跳省的是纯白问、不是盲区。
            # 语义与"cover 答了但没新文"对齐(miss_count+1、置 used 视为本号已有答案);
            # 书架字段若滞后,强制问询轮(每号每 K 轮)会把错判纠回来。
            wr_stats["weread_cover_shelf_skipped"] = wr_stats.get("weread_cover_shelf_skipped", 0) + 1
            used = True
            b.miss_count = (b.miss_count or 0) + 1
        elif wr_eligible:
            #: ★ 24h 新文闸门(2026-10-20):没有新文就不花列表额度,只取 cover。
            need_reads = _needs_reads_24h(session, b, gate, cutoff24)
            if not need_reads:
                wr_stats["weread_list_new_skipped"] = \
                    wr_stats.get("weread_list_new_skipped", 0) + 1
            try:
                weread = weread or _root.WereadClient(cookie)
                _seen = _list_attempts(wr_stats)
                got, answered = _weread_collect(user_id, b, weread, session,
                                                stats=wr_stats, breaker=breaker,
                                                shelf_ts=gate["signals"].get(b.weread_book_id),
                                                banned_out=banned,
                                                #: ★ 2026-10-20 用户口径「新文优先」:
                                                #: **有 24h 新文就查列表**(不再受轮转窗口限制);
                                                #: 没有新文的号只取 cover —— 这是省额度
                                                #: 的主刀,窗口只负责给"判不了/首次见到"的号兜底。
                                                list_allow=need_reads,
                                                app_client=app_client,
                                                app_allow=need_reads,
                                                pool=pool)
                if _list_attempts(wr_stats) > _seen:
                    listed_keys.add(_list_key(b))          # 问过就记号(成败都算,见 _list_attempts)
                # 只有免费源真答了才算"本号已被消费":答不上按 failed 计,如实暴露。
                used = answered
                if answered and gate["signals"].get(b.weread_book_id):
                    # 答上了才准前移水位(问都没问成就前移=把没采到的篇永久记成"见过")
                    marks_advance[b.weread_book_id] = gate["signals"][b.weread_book_id]
                if got:
                    new_rows.extend(got)
                    b.miss_count = 0
                    b.last_item_at = now
                elif answered:
                    # cover 报得出"最新一篇是哪篇",库里也确有这篇 → 当天确实没发文。
                    # 此前微信读书路径从不 +1(只有平台分支加),而它是 81 个号
                    # 唯一的源 → 前端"连续 N 轮未发文"永远空着,号停更和源挂了分不清。
                    b.miss_count = (b.miss_count or 0) + 1
                else:
                    # 免费源没答上(cover 空 + 列表挂):dajiala 摘除后没有第二双腿,
                    # 必须计 failed 如实暴露——旧版此处静默留给付费兜底,兜底没了就是纯盲区。
                    failed += 1
            except WereadAuthError as exc:
                logger.warning("微信读书登录态失效(用户 %s):%s;尝试自动续期", user_id, exc)
                refreshed = _root.refresh_weread_cookie(session, user_id, settings)
                if refreshed.get("status") == "success":
                    cookie = refreshed["cookie"]
                    # ⚠️ **旧注释说"续期=新会话、列表额度重算"—— 2026-10-18 实测证伪**:
                    #    就是刚续期 1 分钟的活 cookie,三种上下文打 `/web/mp/articles`
                    #    仍然 `-2041`。所以这里**不再指望**续期把额度换回来;
                    #    列表能不能问,由 `pool` 按账号逐个判(见 `weread_accounts`)。
                    pool = pool.with_base(cookie)
                    breaker.update(list_off=False, cover_quota_fails=0, off=False)
                    try:
                        _seen = _list_attempts(wr_stats)
                        got, answered = _weread_collect(user_id, b, _root.WereadClient(cookie), session,
                                                        stats=wr_stats, breaker=breaker,
                                                        shelf_ts=gate["signals"].get(b.weread_book_id),
                                                        banned_out=banned,
                                                        list_allow=need_reads,
                                                        app_client=app_client,
                                                        app_allow=need_reads,
                                                        pool=pool)
                        if _list_attempts(wr_stats) > _seen:
                            listed_keys.add(_list_key(b))
                        used = answered  # 答上了就消费掉本号(答不上按 failed 计,如实暴露)
                        if answered and gate["signals"].get(b.weread_book_id):
                            marks_advance[b.weread_book_id] = gate["signals"][b.weread_book_id]
                        if got:
                            new_rows.extend(got)
                            b.miss_count = 0
                            b.last_item_at = now
                        elif answered:
                            b.miss_count = (b.miss_count or 0) + 1
                        else:
                            failed += 1  # 续期后仍没答上:无兜底,如实计失败
                    except WereadError as exc2:
                        failed += 1
                        logger.warning("微信读书续期后仍失败 %s:%s", b.nickname or b.weread_book_id, exc2)
                else:
                    # 续期失败:本号本轮记失败,如实暴露(无付费兜底可降级)
                    # 指纹必须在清空 cookie 前算——否则 md5("")[:6]="d41d8c" 恒定,
                    # 冷却键永远撞同一个假指纹,通知无法随用户换新 Cookie 而重置。
                    fp = _cookie_fingerprint(cookie)
                    cookie = ""
                    failed += 1
                    # 即时提醒用户更新 Cookie(6h 冷却,不刷屏)
                    from app.services.alert_service import notify_incident
                    notify_incident(
                        session, user_id, "wechat",
                        "🟠 微信读书 Cookie 已过期,请更新[" + fp + "]",
                        "自动续期失败。请在浏览器登录 weread.qq.com 后 F12 复制 Cookie,"
                        "粘贴到「Cookie 管理」页 weread 平台(或发给我更新)。"
                        "粘贴前确认串里有「wr_rt=」——缺它自动续期无从下手,十几小时必过期。"
                        "⚠️ 若服务器出口 IP 刚变过(容器重建/加了代理/换网络),同一把 Cookie 也会立刻被判"
                        "登录超时(会话与出口 IP 绑定,2026-09-27 实测)——那就先固定出口再重扫,"
                        "否则新 Cookie 一样当场死",
                        settings=settings)
            except WereadError as exc:
                failed += 1
                logger.warning("微信读书监听 %s 失败:%s", b.nickname or b.weread_book_id, exc)
                if _is_weread_quota_error(exc):
                    breaker["cover_quota_fails"] += 1
                    if breaker["cover_quota_fails"] >= _COVER_QUOTA_TRIP:
                        breaker["off"] = True
                        logger.warning("微信读书连续 %d 个号回额度类错误,本轮剩余号停采(别再加深风控)",
                                       breaker["cover_quota_fails"])
                else:
                    breaker["cover_quota_fails"] = 0
    # 列表轮转记号:只记**这一轮真去问过列表**的号(成败都记,见 `_list_attempts`)。
    # 没轮到的号记号不变 ⇒ 下一轮仍排在最前面,不会被"别人前进"顶掉。
    _mark_listed(session, user_id, listed_keys)
    # 水位前移(只收问过且答上的号,铁律见 _save_shelf_marks):写丢=下轮重问一遍,无害
    try:
        advanced = _save_shelf_marks(session, user_id, marks_advance)
    except Exception:  # noqa: BLE001 - 水位回写失败不能连累本轮采集与推送
        logger.exception("书架水位回写失败 user=%s", user_id)
        advanced = 0
    if banned:
        # 版权清扫预警(2026-09-28 霸王茶姬杯贴文实测引出):同轮 ≥2 篇被投诉下架 = 批量
        # 维权特征,值得让群里知道(同类资源盘链可能连带失效);单篇只落站内不刷群。
        # 微信封禁页不区分投诉人,但类别「版权/商标/专利」即知识产权通道——普通用户
        # 很少走,批量出现即为版权方清扫的典型特征,文案里如实写「疑似」。
        from app.services.alert_service import notify_incident

        cats = "、".join(sorted({v.split("·")[-1] for v in banned.values()}))
        if len(banned) >= 2:
            notify_incident(
                session, user_id, "wechat",
                f"⚠️ 疑似版权清扫:本轮 {len(banned)} 篇文章被投诉下架",
                f"违规类别:{cats}。微信封禁页只标『由用户投诉并经平台审核』,不区分投诉人,"
                "但知识产权类别的批量投诉基本是版权方维权。同类资源的盘链可能被连带投诉、"
                "陆续失效——群里看到可用的 🔴 转存链尽快保存,别等卡变 ⛔。"
                "命中:" + "; ".join(list(banned.values())[:5]),
                settings=settings, push_feishu=True)
        else:
            notify_incident(
                session, user_id, "wechat",
                "⚠️ 1 篇文章被投诉下架(仅站内记录)",
                f"违规类别:{cats}。命中:{list(banned.values())[0]}",
                settings=settings, push_feishu=False)
    # 后处理(回填/采样/转存/共振)整段关在保存点里:它炸了只撤销自己那半截写,
    # 本轮已采到的新文照样 commit + 推飞书(回落原文)。此前它是裸调用,
    # 一次转存异常会连带 `_record_run`/`_push_listen` 全部跳过(第八轮审计)。
    replacements: dict[int, list[tuple[str, str, str]]] = {}
    # ⚠️⚠️ **做后处理之前,先把新文落盘**(2026-10-07 实测代价,很贵):
    # 后处理(回填/采样/转存/共振)是本轮**最容易炸**的一段,而它原来跑在**同一个
    # 未提交事务**里。会话一旦进了失败状态(某处吞了异常没回滚),
    # **`savepoint` 自己就会抛 `PendingRollbackError`** ⇒ **整段后处理全废**。
    # 实测 20:08 那轮:210 个号、70 篇新文、转存一条没做
    # ⇒ 卡片全是「⏳待转存」、阅读数全是「—」,**而这一轮记的是 success**。
    # 先 commit 有两个好处:
    #   ① 采到的文**立刻 durable**,后处理再炸也丢不了;
    #   ② 之后才**敢回滚**那个坏掉的事务 —— 于是后处理**仍然能跑**,而不是陪葬。
    session.commit()
    if not session.is_active:
        logger.warning("监听:后处理前 DB 会话已进入失败状态(某处吞了异常没回滚),"
                       "先回滚再做后处理 user=%s —— ⚠️ 这行日志出现说明**上游有吞异常的点**",
                       user_id)
        session.rollback()

    enrich_failed = ""
    # ★★ **撞 SQLite 写锁时回滚重试一次**(2026-10-21)。
    # 实测 04:05 / 14:05 两轮:`_enrich` 里 `UPDATE wechat_articles SET my_pan_urls=…`
    # 撞 `database is locked`,会话进入「**待回滚**」状态后 ——
    # `Session.is_active` **仍是 True**(上面那道预检拦不住),而 `savepoint()` 入口
    # 直接抛 `PendingRollbackError` ⇒ 整段后处理(转存/复用/阅读数)陪葬,
    # 运行记录只留一句 `⚠️enrich_failed=…`。
    # 锁是**瞬时**的(SQLite 单写者),所以先回滚再重试一次,比直接放弃划算。
    # ⚠️ 只对**锁**重试;别的异常重试只会把同一份失败再做一遍。
    for _attempt in (1, 2):
        try:
            with savepoint(session):
                replacements = _root._enrich_new_articles(session, user_id, settings, new_rows)
            enrich_failed = ""
            break
        except Exception as exc:  # noqa: BLE001 - 转存炸了也要推(标题回落原文)
            # ⚠️ **别只写日志**:外面看到的是"轮次 success、卡片照常推送",
            # 而后处理可能已经废了一半(2026-10-05 就是转存全被回滚、卡片显示"⏳待转存")。
            # 所以**必须进运行记录**,否则运维永远只能靠翻日志偶然撞见。
            _locked = isinstance(exc, PendingRollbackError) or (
                isinstance(exc, OperationalError) and "database is locked" in str(exc))
            if _locked and _attempt == 1:
                logger.warning("监听后处理撞 SQLite 写锁,回滚后重试一次 user=%s:%s",
                               user_id, str(exc)[:120])
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001 - 回滚都失败就别再硬试
                    logger.exception("监听后处理撞锁后回滚失败 user=%s", user_id)
                replacements = {}
                continue
            logger.exception("监听后处理失败 user=%s(本轮推送回落原文)", user_id)
            replacements = {}
            enrich_failed = type(exc).__name__
            break

    session.commit()

    # 判定:全部账号失败=failed;部分失败=partial(即便采到新文,故障也要暴露)。
    # 此前 `not failed or new_rows` 优先级等于 `(not failed) or new_rows`,
    # 只要采到 1 篇新文就把"全部账号挂了"也记成 success 掩盖故障
    # quota_skipped(整源熔断后根本没问过的号)同样是"没监控到",不能记 success:
    # 否则 81 号只问了 12 个的一轮会长得跟"81 号都问过了、只是没新文"一模一样。
    if failed == len(rows):
        status = "failed"
    elif failed or quota_skipped or enrich_failed:
        # ⚠️ **后处理失败要降级成 partial**(2026-10-07):它原来只在 detail 里带一句
        # `⚠️enrich_failed=…`,而 `status` 照旧是 **success** ⇒ 体检按 status 判读时
        # **看不见它**。实测代价:转存全没做、阅读数全丢,而所有指标都是绿的。
        status = "partial"
    else:
        status = "success"
    detail = f"accounts={len(rows)} new={len(new_rows)} failed={failed}{batch_pos}"
    if quota_skipped:
        detail += f" quota_skipped={quota_skipped}"
    if no_free_source:
        detail += f" no_free_source={no_free_source}"
    # biz 里躺着源认不出的形态(历史上 add_benchmark 写过 base64 __biz):⓪ 分支按"没配"处理
    # 所以号不会失明,但免费全量列表也就没接上。不点名出来,运维只会以为"配了 WeRSS 就该全推"。
    miskeyed = [str(b.nickname or b.id) for b in rows if (b.biz or "").strip() and not feed_biz(b)]
    if miskeyed:
        detail += f" biz_bad_shape({len(miskeyed)})"
    if repushed:
        detail += f" repushed={repushed}"   # 补推的上一轮欠推数,写进运维记录而非只进日志
    # ★ **横向补阅读数的账**(2026-10-14):阅读列全 `—` 时,这四个数一眼分得出性质 ——
    #   src=0           横向源根本没被问到(没配/冷却中/凭据死)
    #   src>0 from=0    **问到了、它没带数** ⇒ 大概率 `wemp_client._READ_KEYS` 字段名不对
    #   unmatched>0     两边都有数、**键对不上**(短链 vs 长链,见 `_source.article_keys`)
    # 不写出来的话,"阅读补没补上、为什么没补上"就只能靠猜(与 weread_list 那条同一教训)。
    if wr_stats.get("reads_src") or wr_stats.get("reads_from") or wr_stats.get("reads_unmatched"):
        detail += (f" reads(src={wr_stats.get('reads_src', 0)}"
                   f" from={wr_stats.get('reads_from', 0)}"
                   f" via_title={wr_stats.get('reads_via_title', 0)}"
                   f" unmatched={wr_stats.get('reads_unmatched', 0)})")
    enumerable = wr_stats.get("weread_list_ok", 0)
    off_new = wr_stats.get("weread_list_off_new", 0)
    if wr_stats:
        # list_off 必须写进运维记录:"只采到 cover 最新一篇"时同日其它篇是**未知丢失**,
        # 不能让它和"该号今天真的只发了一篇"长得一样(与 -2014 假象、全败标 success 同族)。
        # list_skipped = 本轮列表已被额度熔断挡下、这些号根本没被问过(见 breaker)。
        detail += (f" weread_list(ok={enumerable} app={wr_stats.get('weread_list_app_ok', 0)}"
                   # ★ **App 路的失败/熔断也要写出来**(2026-10-10):只统计成功的话,
                   # 「App 路整轮没动」与「这轮不需要问 App」看起来一模一样 ——
                   # 我们正是因此**漏看了这条路连续几周 `app=0`**。
                   f" app_fail={wr_stats.get('weread_list_app_fail', 0)}"
                   #: ★ 首个错误码(2026-10-22):`-2012`=token/身份档失效(开模拟器重取),
                   #: `-2041`=额度(等)。没有就不写,保持旧版 detail 逐字不变。
                   + (f" app_err={wr_stats.get('weread_list_app_err')}"
                      if wr_stats.get("weread_list_app_err") else "")
                   + f" app_blocked={wr_stats.get('weread_list_app_blocked', 0)}"
                   # ★ **按账号分开记**(2026-10-18):池里"谁还能答"是"要不要再加一个号"
                   #   的唯一依据;合成一个数就又看不出是新号在扛、还是老号自己活了。
                   #   ⚠️ 单账号时这里是空串 —— detail 必须与旧版逐字一致(见 `_acct_brief`)。
                   + (f" accts={_acct}" if (_acct := _acct_brief(pool, wr_stats)) else "")
                   + f" off={wr_stats.get('weread_list_off', 0)}"
                   f" off_with_new={off_new} skipped={wr_stats.get('weread_list_skipped', 0)}"
                   f" new_skip={wr_stats.get('weread_list_new_skipped', 0)})")
    if gate["ok"]:
        # `skip` 是**计划**跳过数,`saved` 是**实际**跳过数(wr_stats 里那个计数)。
        # ⚠️ 加 `saved` 是因为 2026-10-05 审计发现 `weread_cover_shelf_skipped`
        # **只写不读** —— 书架闸门的价值(省了多少次 cover 请求)运维根本看不到,
        # 于是"它到底有没有在省"只能靠猜。**只写不读的计数等于没有。**
        detail += (f" shelf(signals={len(gate['signals'])} skip={len(gate['skip'])}"
                   f" saved={wr_stats.get('weread_cover_shelf_skipped', 0)}"
                   f" force={len(gate['force'])} adv={advanced})")
    elif gate["reason"] not in ("", "disabled", "no_shelf_entries"):
        # 试过但没用上:no_signal_field=条目认不出信号字段(拿 probe 的字段表回来对名单),
        # 异常类名=书架请求本身挂了(本轮已自动退化为逐号问)
        detail += f" shelf(off={gate['reason']})"
    if banned:
        detail += f" banned={len(banned)}"
    if enrich_failed:
        # 后处理(转存/复用)整段被回滚 ⇒ 这一轮的"待转存"是**故障**不是"还没轮到我方转存"。
        # 不点名的话,它在运行记录里跟正常轮次长得一样。
        detail += f" ⚠️enrich_failed={enrich_failed}"
    _record_run(session, user_id, "wechat_listen", status, detail)
    session.commit()
    if push and new_rows:
        _push_listen(session, user_id, settings, new_rows, replacements)
        try:
            with savepoint(session):
                _burst_scan(session, user_id, settings, new_rows)
        except Exception:  # noqa: BLE001 - 爆点扫描失败不伤主流程
            session.rollback()
            logger.exception("爆点扫描失败 user=%s", user_id)
    if off_new and push:
        # 兑现"近24h全部推送"要靠列表枚举;只要还有号列不出来又采到了新文,就必须点名而不是安静少推。
        # 但点名落在**站内告警**:这是"要不要自建 WeRSS/要不要充值"的长期决策,不是员工群里
        # 该刷的东西——用户 2026-09-26 定的口径:飞书只推文章与 Cookie 提醒。
        from app.services.alert_service import notify_incident
        notify_incident(
            session, user_id, "wechat",
            "⚠️ 微信读书只能拿到最新一篇,同日其它篇可能漏推",
            f"本轮列不出却采到新文的号:{off_new}(可枚举 {enumerable} / 共 {len(rows)})。"
            "微信读书 mp/articles 是**按会话/IP 记额度**的稀缺接口:一把刚建立的会话能列"
            "(2026-09-27 实测:刚续期的会话回 -2014『额度』而非会话老化后的 -2041『不下发』),"
            "被 81 个号 × 每轮 2 次的密度耗尽后就整轮列不出。监听因此退化为 cover 最新一篇:"
            "两轮之间(最长 8h)同一号发多篇时,前面的那几篇顶不掉也补不回来。"
            "要真正兑现『近24h全推』:① 把列表额度花在少数号上(错峰分批/按号轮转枚举,"
            "见 §9.2 密度账);② 自建 WeRSS 的 web/app 模式(要求你有一个自己的公众号后台身份,没有就不能用——"
            "它的 weread_mp 模式会原样继承本接口的额度限制)。详见 doc/operations.md §4g/§9.2"
            + (f";另有 {len(miskeyed)} 个号的 biz 不是源认识的形态:{('、'.join(miskeyed[:5]))}"
               if miskeyed else ""),
            settings=settings, push_feishu=False)
    if quota_skipped:
        # "本轮根本没问到"是密度问题,不是"号没发文"——必须单独点名(按口径落站内,不刷飞书)。
        # 不点名的话,运营看到的只有"今天怎么又只有几个号推",而事实是"其余号被源挡在门外"。
        from app.services.alert_service import notify_incident
        notify_incident(
            session, user_id, "wechat",
            f"🟠 微信读书额度耗尽,本轮 {quota_skipped} 个号未采到",
            f"连续 {breaker['cover_quota_fails']} 个号被 -2014/-2041 挡回后本轮熔断,"
            f"剩余 {quota_skipped} 个号(共 {len(rows)})这一轮没有数据,下一轮会重新尝试。"
            f"根因是单轮请求密度({len(rows)} 号 × cover+列表 ≈ {len(rows) * 2} 次/轮、"
            "4 轮/天)超出该会话的容忍线。可选缓解:调度器启用错峰分批"
            "(2026-09-29 起已默认启用:每批 wechat_listen_batch_size=36)、"
            "拉长 WereadClient.min_gap(现 2s)。详见 doc/operations.md §9.2",
            settings=settings, push_feishu=False)
    out: dict = {"platform": "wechat", "status": status, "accounts": len(rows),
                 "new": len(new_rows), "failed": failed}
    if quota_skipped:
        out["weread_quota_skipped"] = quota_skipped
    if repushed:
        out["repushed"] = repushed
    if wr_stats:
        out["weread_list"] = {k: v for k, v in wr_stats.items()}
    if gate["ok"]:
        out["weread_shelf"] = {"signals": len(gate["signals"]), "skip": len(gate["skip"]),
                               "force": len(gate["force"]), "advanced": advanced}
    if banned:
        out["banned"] = len(banned)
    if miskeyed:
        out["biz_bad_shape"] = miskeyed[:10]
    return out
def _burst_scan(session: Session, user_id: int, settings: Settings,
                rows: list[WechatArticle]) -> int:
    """免费爆点检测(2026-09-30,dajiala 付费采样摘除后的轻量替代)。

    数据源:微信读书 cover/列表自带的**站内阅读数**(免费,随监听一并入库,零额外请求)。
    判定:新文阅读数 ≥ 同号近 14 天文章阅读中位数 × `wechat_burst_median_mult`
    且 ≥ `wechat_burst_min_reads` → 🔥 爆点卡推公众号群(建议员工立即跟进)。
    每篇只报一次(冷却键=article_id,7 天);基线不足 3 篇宁缺毋滥。返回发送篇数。
    """
    if not rows or not settings.wechat_burst_min_reads:
        return 0
    from app.services.alert_service import feishu_alert_gate
    from app.services.feishu_client import FeishuClient
    from app.services.feishu import webhook_for

    webhook = webhook_for(settings, "wechat")
    if not webhook:
        return 0
    now = datetime.now()
    client = FeishuClient(webhook, settings.feishu_secret)
    sent = 0
    for r in rows:
        if not r.benchmark_id or (r.read_num or 0) < settings.wechat_burst_min_reads:
            continue
        if not feishu_alert_gate(session, user_id, "burst_free", f"burst:{r.id}",
                                 24 * 7, f"站内阅读{r.read_num}"):
                continue  # 这篇 7 天内已报过
        vals = sorted(v for v in session.scalars(select(WechatArticle.read_num).where(
            WechatArticle.user_id == user_id, WechatArticle.benchmark_id == r.benchmark_id,
            WechatArticle.id != r.id, WechatArticle.read_num > 0,
            WechatArticle.created_at >= now - timedelta(days=14))).all() if v)
        if len(vals) < 3:
                continue  # 基线不足,宁缺毋滥
        median = vals[len(vals) // 2]
        if r.read_num < median * settings.wechat_burst_median_mult:
                continue
        mine = [x for x in (r.my_pan_urls or "").splitlines() if x.strip()]
        lines = ["🔥 爆点苗头 · 建议立即跟进改写",
                 "🔴 " + r.title[:40],
                 f"📊 站内阅读 {r.read_num}(同号中位数 {median} 的 "
                 f"{r.read_num / max(median, 1):.0f} 倍)"]
        if mine:
            lines.append("📦 我的链接: " + mine[0])
        lines.append(r.url)
        if client.send(chr(10).join(lines)):
            sent += 1
    return sent


def repush_unpushed(session: Session, user_id: int, settings: Settings | None = None) -> int:
    """补推:近 N 小时入库、却从未成功推上飞书的文章(铁律的最后一道兜底)。

    "监控号近 24h 发的文章必须全部到飞书"是产品铁律,但推送从来没有事实记录:
    `run_wechat_listen` 先 commit 新文、再发卡,卡片发送失败只留一行 exception 日志,
    那批文章就永久停在库里,而且和"根本没发文"长得一模一样(第八轮审计遗留的最后一格)。
    现在 `_push_listen` 只在**真的送达**时才落 `pushed_at`,这里把 `pushed_at IS NULL`
    的窗口内文章重新走一次发卡(标题带 ⏰补推,员工能分辨这是迟到的卡)。

    每轮监听开头先补上一轮的欠账,所以不需要额外的定时作业;窗口外的文章不再补
    (与铁律同界,免得翻旧账刷屏)。
    """
    settings = _base(settings)
    cutoff = datetime.now() - timedelta(hours=max(1, int(settings.wechat_repush_window_hours or 24)))
    limit = max(1, int(settings.wechat_repush_limit or 100))
    rows = list(session.scalars(select(WechatArticle).where(
        WechatArticle.user_id == user_id,
        WechatArticle.pushed_at.is_(None),
        WechatArticle.source.in_(("listen", "sync")),
        WechatArticle.created_at >= cutoff,
        # 发布时间也要在窗口内:「同步文章」被封顶窗口砍掉的 24h 之前的历史补采文
        # 按设计是"留给补转存队列、不再补推"的,只按入库时间筛会把它们翻出来刷屏。
        or_(WechatArticle.publish_at.is_(None), WechatArticle.publish_at >= cutoff),
    ).order_by(WechatArticle.id).limit(limit)).all())
    if not rows:
        return 0
    pushed = _push_listen(session, user_id, settings, rows, None, repush=True)
    if pushed:
        logger.warning("补推完成 user=%s:窗口内欠推 %d 篇,本次送达 %d 篇", user_id, len(rows), pushed)
    if pushed < len(rows):
        # 补推又没推完 = 飞书侧持续故障或积压超过单轮上限——必须点名,不能安静少推。
        # 而这种"飞书本身坏了"的告警发飞书更是发不出去(或被同一故障吞掉),只落站内。
        from app.services.alert_service import notify_incident

        notify_incident(
            session, user_id, "wechat",
            "⚠️ 公众号文章补推仍未送达,飞书推送可能持续故障",
            f"近 {settings.wechat_repush_window_hours} 小时内有 {len(rows)} 篇从未成功推送,"
            f"本轮补推只送达 {pushed} 篇(单轮上限 {limit} 篇)。"
            "请检查飞书机器人 webhook 是否被移除/关键词/IP 白名单拦截,或被封禁群。"
            "剩余欠推会在下一轮监听继续补,超过窗口后不再补。",
            settings=settings, push_feishu=False)
    return pushed
def _my_pan_link_from_history(my_pan_urls: str) -> tuple[str, str]:
    """从已持久化的 `my_pan_urls` 里取第一条可用我方链 → (干净 URL, 提取码),没有给 ("", "")。

    过去这里写死 `startswith("https://pan.quark.cn/s/")`,只认夸克;而百度转存成功落库的是
    `https://pan.baidu.com/s/xxx (提取码 abcd) [百度]` —— 于是**非本轮的卡片**(⏰补推、
    同步重推)上百度文一律退回公众号原文,网盘列却还标着盘商名,员工点开才发现是文章。
    用盘链正则取 URL 本体,顺带把 `(自分享)`/`[百度]`/提取码这些附属标记留在外面。
    """
    for line in (my_pan_urls or "").splitlines():
        m = _MY_LINK_RE.search(line)
        if m:
            c = re.search(r"提取码\s*([0-9A-Za-z]{4})", line)
            return m.group(0), (c.group(1) if c else "")
    return "", ""
def _collapse_repeats(rows: list) -> list[tuple[object, int]]:
    """同一 **(账号, 标题)** 只留**最新**那条,并带上"这轮一共发了几次"。

    ⚠️ 为什么要收(用户 2026-10-14 报「显示也有问题」):实测有些号**每天在发同名文章**
    (`xx风味笔记` / `现在就出发` 这类系列天天重发),卡片逐条列出 ⇒ **同一个标题连着出现
    2~3 次**,读的人第一反应是「卡片重复了 / 去重坏了」。
    ⚠️ **入库去重没问题**:这些行的 URL 与 `article_keys()` 三级候选键**各不相同**(是不同文章),
    所以这里收的是**显示**,不是数据 —— 别顺手去改去重。
    ⚠️ 只按 **(账号, 标题)** 合并:`标题带「最新版」后缀`的那种是**另一篇**,不许并(会丢信息)。

    返回 `[(文章, 次数)]`,**保序**(按首次出现)。
    """
    best: dict[tuple[str, str], list] = {}
    order: list[tuple[str, str]] = []
    for r in rows:
        k = (str(getattr(r, "author", "") or ""), str(getattr(r, "title", "") or ""))
        cur = best.get(k)
        if cur is None:
            best[k] = [r, 1]
            order.append(k)
            continue
        cur[1] += 1
        # 留更新的那条(先 publish_at —— 它才是内容自身时刻;都没有就留先到的)
        t_new = getattr(r, "publish_at", None) or getattr(r, "created_at", None)
        t_old = getattr(cur[0], "publish_at", None) or getattr(cur[0], "created_at", None)
        if t_new and t_old and t_new > t_old:
            cur[0] = r
    return [(best[k][0], best[k][1]) for k in order]

def _our_share_keys(session: Session, user_id: int) -> set[str]:
    """本租户**已生成过的我方分享链**(URL 本体集合)。

    ★ 2026-10-20 用户口径「根据网盘来判别」:别的号**转发我们的链**时，
    该文章的 `my_pan_urls` 是空的 —— 只看 `my_pan_urls` 会漏掉这种。
    所以从全库 `my_pan_urls` 里抽出 URL 本体，和文章的 `pan_urls` 比。
    """
    out: set[str] = set()
    try:
        rows = session.execute(
            select(WechatArticle.my_pan_urls).where(
                WechatArticle.user_id == user_id,
                WechatArticle.my_pan_urls != "",
            )
        ).scalars()
        for blob in rows:
            for line in (blob or "").splitlines():
                m = _MY_LINK_RE.search(line)
                if m:
                    out.add(m.group(0))
    except Exception:  # noqa: BLE001 - 判别失败就当没有(照常推),别挡推送
        logger.debug("收集我方分享链失败,按没有处理", exc_info=True)
    return out


def _owned_share_keys(session: Session, user_id: int) -> set[str]:
    """`pan_owned_shares` 里登记的**我方自有分享链**(多夸克账号的并集)。

    ★ 2026-10-20:用户口径「看夸克名字来分辨」—— 名字只能人工看,**判据必须是分享 id**。
    这张表由 `pan_owned_shares.refresh` 每天 04:15 从各账号的
    `/share/mypage/detail` 拉下来,覆盖两类 `my_pan_urls` 漏掉的:
    **App 里手工建的分享**、**同行转发我们的链**(对方文章 `my_pan_urls` 为空)。
    """
    try:
        from app.services.pan_owned_shares import owned_urls

        return owned_urls(session, user_id)
    except Exception:  # noqa: BLE001 - 登记表读不到就回落旧判据,别挡推送
        logger.debug("读取我方自有分享登记失败,按没有处理", exc_info=True)
        return set()


def _push_listen(session: Session, user_id: int, settings: Settings, rows: list[WechatArticle],
                 replacements: dict[int, list[tuple[str, str, str]]] | None = None,
                 repush: bool = False) -> int:
    """新文推公众号专属飞书群(column_set 网格卡片:公众号/文章/网盘/阅读 四列对齐)。

    标题超链接优先级:本轮转存链(带提取码)> 已持久化的我的转存链 > 原文;
    未配专属群则回落总群;推送失败不影响采集结果。
    不设免打扰窗口:飞书是员工查看新发文的唯一入口(平台只有运营者可见),
    任何时段采到的文章都照常全量推送。

    返回值=成功送达的篇数。**送达的卡片里那些文章会盖上 `pushed_at`**,没盖上的
    (整卡发送失败/异常)就是"采到了却从没到过飞书",由 `repush_unpushed` 下一轮补推
    ——此前这类文章只留下一行 logger.exception,静默永久丢失(2026-09-26 第八轮审计)。
    `repush=True` 时卡标题带 ⏰补推,让员工/运营分得清这是迟到的那批。
    """
    from app.services.feishu import _col_set_row, _md_safe, webhook_for
    from app.services.feishu_client import FeishuClient

    wh = webhook_for(settings, "wechat")
    main_wh = settings.feishu_webhook
    targets = list(dict.fromkeys(filter(None, [wh, main_wh])))  # 去重保序
    if not targets:
        return 0
    replacements = replacements or {}
    #: ★ 2026-10-20 用户口径:「我们发公众号的网盘资源不要推送进飞书」。
    #: 判定(任一命中):本轮有新转存链 / 历史 `my_pan_urls` 有真实我方链 /
    #: 作者在自营号名单且文章带盘链。命中 ⇒ 盖 `pushed_at`(不再补推)但**不发飞书**。
    if getattr(settings, "wechat_push_skip_own_pan", True) and rows:
        own_names = {x.strip() for x in
                     str(getattr(settings, "own_account_names", "") or "").split(",")
                     if x.strip()}
        own_keys = _our_share_keys(session, user_id) | _owned_share_keys(session, user_id)

        def _is_ours(r: WechatArticle) -> bool:
            if replacements.get(r.id):
                return True                       # 本轮刚转存出来的就是我们的盘
            link, _code = _my_pan_link_from_history(r.my_pan_urls)
            if link:
                return True                       # 历史已持久化的我方链
            #: 别的号转发我们的链:my_pan_urls 为空,但 pan_urls 里就是我方 share id。
            for line in (r.pan_urls or "").splitlines():
                m = _MY_LINK_RE.search(line)
                if m and m.group(0) in own_keys:
                    return True
            has_pan = bool((r.pan_urls or "").strip() or (r.my_pan_urls or "").strip())
            return bool(has_pan and own_names and (r.author or "") in own_names)

        suppressed = [r for r in rows if _is_ours(r)]
        if suppressed:
            session.execute(update(WechatArticle).where(
                WechatArticle.id.in_([r.id for r in suppressed])
            ).values(pushed_at=datetime.now()))
            session.commit()
            logger.info("公众号推送:跳过 %d 篇(我方网盘资源,不推飞书):%s",
                        len(suppressed),
                        "、".join((r.title or "")[:18] for r in suppressed[:5]))
            _ids = {r.id for r in suppressed}
            rows = [r for r in rows if r.id not in _ids]
    # 免打扰过滤后可能清空(理论上上面已 return,这里再兜一层,避免推空卡)
    if not rows:
        return 0
    from collections import OrderedDict

    # 重复资源计数:一次 GROUP BY 批量查本轮全部文章(循环内逐篇 COUNT 是 N+1)。
    # 不再只算 rows[:20]——全量推送下每篇都要有 🔥xN 标记。
    dup_counts: dict[str, int] = {}
    pan_of = {r.id: next((x.strip() for x in (r.pan_urls or "").splitlines() if x.strip()), "")
              for r in rows if r.pan_urls}
    if pan_of:
        for pan_url, cnt in session.execute(
                select(WechatPanLink.pan_url, func.count()).where(
                    WechatPanLink.pan_url.in_(set(pan_of.values())),
                    WechatPanLink.user_id == user_id,  # 🔥xN 只数本租户监听,别把他号的重复算进来
                ).group_by(WechatPanLink.pan_url)).all():
            dup_counts[pan_url] = int(cnt)


    def _render_article(r: WechatArticle, repeat: int = 1) -> dict:
        rep = replacements.get(r.id) or []
        code = ""
        my_link = False          # 标题链接是否指向"我方转存链"(False=点进去是公众号原文)
        if rep:
            link, code = rep[0][1], rep[0][2]
            my_link = True
        else:
            # 只展示我方网盘链接(本轮转存链 > 历史我方链);绝不回落到别人的盘链——
            # 未转存时点标题打开公众号原文。历史 my_pan_urls 常自带提取码,拆出来明文显示。
            link, code = _my_pan_link_from_history(r.my_pan_urls)
            if link:
                my_link = True
            else:
                link = r.url
        # 重复资源标记: 同盘链已被其他文章推过 → 🔥N(同行都在发的确认级资源)
        hot = ""
        first_pan = pan_of.get(r.id, "")
        if first_pan:
            dup = max(0, dup_counts.get(first_pan, 0) - (1 if first_pan in pan_of.values() else 0))
            if dup:
                hot = f"🔥x{dup + 1} "
        title = _md_safe(r.title)
        shown = title[:26] + ("…" if len(title) > 26 else "")
        # 时效标注:发布日=今天不标(监听主打就是刚发的);带发布时间的旧文(补采/同步/
        # 迟到补推)标上日期——盘链时效短,员工该对"点开可能已失效"有预期
        if r.publish_at and r.publish_at.date() < datetime.now().date():
            shown += f" ·{r.publish_at:%m-%d}"
        if repeat > 1:
            # 「这轮发了几次」—— 同名重发是真实存在的(有些号每天重发同一篇),
            # 收成一行但**把次数说出来**,免得被读成"只发了一次"
            shown += f" ×{repeat}"
        q_badge = ""
        if r.read_num >= 500:
            q_badge = "🔴爆 "
        elif r.read_num >= 100:
            q_badge = "⭐热 "
        elif r.quality >= 6:
            q_badge = "⭐优 "
        elif r.quality <= 2 and r.pan_types:
            q_badge = "⚠️疑 "
        # href 只放干净链接(旧实现把" (提取码 xxxx)"拼进 URL → 链接点不开),提取码明文附后
        # ★★ **2026-10-16:原文独立成列**(用户报「虽然有链接了但**没办法看对标的作品**」)。
        #    原来整行**只有一处可点**,而它在有网盘链时指向的是**我方链**
        #    ⇒ 员工**永远进不去原文**,没法核对"对标作品到底长什么样、值不值得跟"。
        #    现在与抖音卡**同形**(那边是「作品」→网盘、「▶视频」→视频,**两个独立格**):
        #      「文章」→ 取资源(我方链);「原文」→ 看内容(公众号原文)。
        #    ⚠️ 有链时「文章」**不再回落到原文** —— 否则同一行两个格指向同一处,
        #       看起来像"这里有两篇文章"。
        _titled = f"{hot}{q_badge}{shown}"
        article_md = (f"[{_titled}]({_md_safe(link)})" if (link and my_link) else _titled) \
            + (f" 🔑{code}" if code else "")
        # 「原文」列:对标作品的入口。**永远指向公众号原文**(不因有没有转存而变)。
        orig_md = f"[原文]({_md_safe(r.url)})" if r.url else "—"
        # 网盘列同时交代"点标题会去哪":🔴=我方转存链;⏳=有源链但还没转好(点进去是原文);
        # ⛔=对方分享已被封,永远转不了。员工不必点开才发现进的是公众号文章。
        types = _md_safe(r.pan_types)[:8] if r.pan_types else ""
        if my_link:
            pan = f"🔴{types}" if types else "🔴我方链"
        elif "41031" in (r.my_pan_urls or ""):
            pan = f"⛔源失效{types}"
        elif "原文失效" in (r.my_pan_urls or ""):
            pan = "⛔原文失效"   # 原文被投诉下架(账号封禁/作者删除),链接是尸体别点
        elif (r.pan_urls or "").strip():
            pan = f"⏳待转存{types}"
        else:
            pan = types or "—"
        # 阅读数来源:微信读书 cover/列表自带站内阅读(免费,2026-09-30 起);
        # dajiala 采样摘除后 traffic_at 恒空,不能再当显示条件(否则永远显示"—")
        #
        # ★ **2026-10-18 去掉 `*` 后缀**(用户当场报「刚补推的阅读数后面都带*」):
        #   它原来标的是「dajiala 采样口径」,而 dajiala **2026-09-29 已摘除**;
        #   `traffic_at` 现在的语义是 **`read_trend` 最近一次采到读数的时刻**
        #   (`read_trend.observe` 每观察到一次就写 now)⇒ **每篇被观察过的文章都会带 `*`**,
        #   员工读到的是"这个数是估算的",而它本来是**精确**的 `readNum`。
        #   ⇒ 读数只显示数字,不加任何口径符号。
        read = str(r.read_num) if r.read_num else "—"
        return _col_set_row([
            (_md_safe(r.author)[:10] or "—", 3), (article_md, 6), (orig_md, 2),
            (pan, 2), (read, 2),
        ])

    # 按账号分组(员工视角要一眼看清"哪个号发了哪些"),同号内资源文/高阅读优先。
    # ★ 同一(账号,标题)**收敛成一行**(2026-10-14,用户报「显示也有问题」):
    #   有些号每天重发同名文章 ⇒ 卡片上同一标题连着出现 2~3 次。**入库没问题**(URL/键都不同),
    #   收的是显示,并把次数标在标题后面(`×3`)。
    collapsed = _collapse_repeats(list(rows))
    groups: "OrderedDict[str, list[tuple[WechatArticle, int]]]" = OrderedDict()
    for r, n in sorted(collapsed, key=lambda t: (t[0].author or "", not t[0].pan_types,
                                                  -(t[0].read_num or 0))):
        groups.setdefault(r.author or "未知账号", []).append((r, n))
    # 展开成 (账号, 文章, 次数) 序列,分页时按账号标题切段
    seq: list[tuple[str, WechatArticle, int]] = [
        (author, r, n) for author, arts in groups.items() for r, n in arts
    ]

    # LLM 叙事层:盘链文优先交给大模型解读(失败/未配 key 静默降级,不影响推送)
    ai_elements: list[dict] = []
    if settings.deepseek_api_key:
        try:
            from app.services.llm_client import narrate_articles

            top = sorted(rows, key=lambda r: (not r.pan_types, -(r.read_num or 0)))[: settings.llm_narrate_limit]
            ctx = [{"title": r.title, "summary": (r.content or "")[:200],
                    "pan_types": r.pan_types} for r in top]
            reading = narrate_articles(settings.deepseek_base_url, settings.deepseek_api_key,
                                       settings.deepseek_model, ctx)
            if reading:
                # LLM 输出同样是"外部可控文本":员工群卡片里不能原样塞 markdown 链接/
                # `<at>`(提示注入 → 钓鱼链、@所有人)。逐行走 _md_safe,保留换行排版。
                safe_reading = chr(10).join(_md_safe(line) for line in reading.splitlines())
                ai_elements = [
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md",
                        "content": "🤖 **AI 解读**" + chr(10) + safe_reading[:1500]}},
                ]
        except Exception:  # noqa: BLE001 - 叙事失败不影响推送
            logger.exception("LLM 叙事失败 user=%s", user_id)

    # 全量分页:每卡最多 20 篇(飞书卡片有体积上限),超出部分继续发卡而非"见平台列表"。
    per_card = 20
    chunks = [seq[i:i + per_card] for i in range(0, len(seq), per_card)]
    total_pages = len(chunks)
    delivered: list[int] = []   # 成功送达那一页所盖的 article id → 收尾统一落 pushed_at
    for page_idx, chunk in enumerate(chunks):
        elements: list[dict] = [
            # ⚠️ 这段是**外部可见文案**:措辞按用户口径去掉了会暴露内部动作的字眼
            # (2026-10-02 "不要暴露我")——不再提"转存链 / 识别到的 / 补采 / 投诉"。
            {"tag": "note", "elements": [{"tag": "plain_text",
                "content": "点文章标题看原文 · 网盘列=该文带的网盘链接,"
                           "—=这篇没带(仍照常推) · 阅读未采样为 —"
                           " · 标题后 ·MM-DD=发文日期(旧文链接可能已失效)"
                           " · ⛔=原文已被下架,别点"}]},
        ]
        if page_idx == 0:
            n_pan = sum(1 for r in rows if (r.pan_urls or "").strip())
            elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                "content": f"本轮共 {len(rows)} 篇新发文(按账号分组),"
                           f"其中 {n_pan} 篇带网盘资源"}]})
        elements.append(_col_set_row(
            [("**公众号**", 3), ("**文章**", 6), ("**原文**", 2),
             ("**网盘**", 2), ("**阅读**", 2)], grey=True))
        last_author: str | None = None
        for author, r, n in chunk:
            if author != last_author:  # 换账号插入一行账号标题;账号跨卡时下一页重出标题
                elements.append({"tag": "div", "text": {"tag": "lark_md",
                    "content": f"**📢 {_md_safe(author)} · {len(groups[author])} 篇**"}})
                last_author = author
            elements.append(_render_article(r, repeat=n))
        if page_idx == 0 and ai_elements:
            elements.extend(ai_elements)
        card_title = f"📡 公众号监听 · 新发文 {len(rows)} 篇"
        if repush:
            # 补推卡必须自报身份:员工看到的是一张迟到的卡,不是"这轮又发了新的"
            card_title = "⏰ 补推 · " + card_title
        if total_pages > 1:
            card_title += f" · {page_idx + 1}/{total_pages}"
        sent_any = False
        for target in targets:
            try:
                if FeishuClient(target, settings.feishu_secret).send_card({
                    "config": {"wide_screen_mode": True},
                    "header": {"template": "blue", "title": {"tag": "plain_text",
                        "content": card_title}},
                    "elements": elements,
                }):
                    sent_any = True
            except Exception:  # noqa: BLE001 - 推送失败不影响采集结果
                logger.exception("公众号监听飞书推送失败 user=%s page=%s", user_id, page_idx)
        # 任一目标群收下即算这一页送达(专属群+总群双推是设计如此;补推时会整批重发,
        # 好的那个群可能再收一次——重复一张卡远好于员工永远没看到这篇)。
        if sent_any:
            delivered.extend(r.id for _a, r, _n in chunk)   # chunk 现在带"次数"
            # ★ **推送台账**(2026-10-16):这一页真的送到了 ⇒ 记进台账。
            #   板块日结/周结(`board_digest`)读的就是它 —— 客户抱怨"信息太多"的正是这个板块。
            #   ⚠️ **按"页"记一次,不按"目标群"记**:双推(专属群 + 总群)是设计如此,
            #   记两遍会把日结的「推了几条」直接翻倍(台账的语义是"有东西送到了")。
            try:
                from app.services import push_ledger

                push_ledger.record(session, user_id, "wechat", [
                    {"item_key": f"art{r.id}", "title": r.title, "url": r.url or "",
                     "author": r.author or "",
                     # 有我方链就记我方链(客户点开能直接取),没有就留空(别记别人的盘链)
                     "pan_url": ((replacements.get(r.id) or [("", "", "")])[0][1]
                                 or _my_pan_link_from_history(r.my_pan_urls)[0] or ""),
                     "metrics": {"read_num": r.read_num} if r.read_num else {}}
                    for _a, r, _n in chunk])
            except Exception:  # noqa: BLE001 - 旁路,绝不许它挡住下面 pushed_at 的落库
                logger.warning("公众号卡片入台账失败(不影响推送)", exc_info=True)
    if delivered:
        #: ⚠️ 台账写失败会把 session 打成「必须 rollback」(2026-10-19 的锁冲突)。
        #: 不先恢复的话这一批 `pushed_at` 落不下去 ⇒ 下一轮重复补推同一批。
        if not session.is_active:
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                logger.debug("落 pushed_at 前回滚会话失败", exc_info=True)
        session.execute(update(WechatArticle).where(WechatArticle.id.in_(delivered))
                        .values(pushed_at=datetime.now()))
        session.commit()
    return len(delivered)
