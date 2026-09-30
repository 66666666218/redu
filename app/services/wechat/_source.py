"""数据源选择/Cookie 解析/对标号 CRUD/书架导入/微信读书续期(彼此咬合,故同模块)。"""

from app.db.models import (FeishuAlert, User, WechatArticle, WechatBenchmark, WechatCandidate,
                           WechatPanLink, WechatRewrite, WechatTrafficSample)

from app.services.reader_platform_client import PlatformError, ReaderPlatformClient

from app.services.tenant_base import _base, _record_run

from app.services.weread_client import WereadAuthError, WereadClient, WereadError, build_mp_url

from app.services.werss_client import WerssClient

from config.settings import Settings, get_settings

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update

from sqlalchemy.orm import Session

import re

from app.services.wechat._text import extract_article_meta


_FEED_BIZ_PREFIX = "MP_WXS_"
from app.utils import get_logger

logger = get_logger(__name__)
from app.services import wechat_monitor as _root  # 兼容 monkeypatch:可替换名经门面运行时查找

def feed_biz(b: WechatBenchmark) -> str:
    """`biz` 列里**免费列表源真认得**的那个形态;认不出就当没配。

    同一列历史上被两种值写过:`add_benchmark` 从文章页 `__biz` 解出的 base64(`MjM5...==`),
    和平台/WeRSS 返回的订阅 id(`MP_WXS_*`)。只有后者能被 `mp_articles` 用。
    这不是洁癖:⓪ 分支拿错形态去调,源会**正常返回空列表而不是报错**,于是 `used` 被置真、
    ① 微信读书 cover 也被跳过,该号整轮静默失明并在前端表现成"连续 N 轮未发文"。
    """
    value = (b.biz or "").strip()
    return value if value.startswith(_FEED_BIZ_PREFIX) else ""
def find_feed_biz_by_name(plat: object, nickname: str) -> str:
    """按公众号名在 WeRSS 里找一个订阅 id;拿不准(0 或 >1)一律返回空串,不猜。

    用上游的 `kw` 搜索而不是翻全量索引:加号是交互式请求,翻 20 页不值。重名如果都来自
    同一个号,规范化名相等后仍会是多个 → 交给 `scripts/werss_backfill_biz.py` 让人去改订阅名。
    """
    name = _norm_mp_name(nickname)
    if not name or not hasattr(plat, "list_feeds"):
        return ""
    try:
        feeds = plat.list_feeds(kw=nickname, limit=100)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - 加号不因查订阅失败而变红
        logger.info("加号时查 WeRSS 订阅失败(忽略):%s", exc)
        return ""
    hits = [str(f.get("id") or "").strip() for f in feeds
            if _norm_mp_name(f.get("mp_name", "")) == name]
    hits = [h for h in hits if h.startswith(_FEED_BIZ_PREFIX)]
    return hits[0] if len(hits) == 1 else ""
def nudge_werss(biz: str, settings: Settings | None = None) -> dict:
    """催 WeRSS 立刻去上游抓一次这个订阅(加号/回填后的最后一公里,别等它自己的定时)。

    只对 WeRSS 生效(读书平台没有对应接口),且**只认 `MP_WXS_*`**。不抛异常:
    WeRSS 的 60s 节流或上游失败都只记日志——它自己的抓取任务迟早补上。
    """
    plat = _root._platform_client(settings or get_settings())
    value = (biz or "").strip()
    if not hasattr(plat, "refresh_mp") or not value.startswith(_FEED_BIZ_PREFIX):
        return {"nudged": False, "reason": "not_werss_or_bad_biz"}
    return {"nudged": plat.refresh_mp(value), "reason": ""}
class MultiSourceClient:
    """免费列表源的多后端故障切换组合器(2026-10-01 抗停维升级)。

    按优先级排列后端(WeRSS → 自研 Wemp → 读书平台),`mp_articles` 依次尝试:
    某后端抛 PlatformError(限流/会话/网络)即切下一个,并**进程内短期熔断**
    (同源失败后 10 分钟内跳过,避免每号每页都撞一遍已死的源)。
    空列表不算失败(正常"没有更多文章");全失败抛最后一个异常——
    监听轮 ⓪ 分支的 `except PlatformError` 降级逻辑照常衔接。
    """

    _COOLDOWN_SEC = 600

    def __init__(self, backends: list[tuple[str, object]]) -> None:
        self._backends = [(n, b) for n, b in backends if b is not None]
        self._fail_until: dict[str, float] = {}

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self._backends]

    def _cooling(self, name: str) -> bool:
        import time as _time
        return self._fail_until.get(name, 0) > _time.time()

    def _trip(self, name: str) -> None:
        import time as _time
        self._fail_until[name] = _time.time() + self._COOLDOWN_SEC

    def mp_articles(self, mp_id: str, page: int = 1, limit: int = 20) -> list[dict]:
        last_exc: Exception | None = None
        for name, be in self._backends:
            if self._cooling(name):
                continue
            try:
                items = be.mp_articles(mp_id, page=page, limit=limit)
                if items:
                    return items
                # 空列表:该源正常答"没有"——但可能它没订阅这个号而别家订阅了,
                # 继续问下一源,谁有数据用谁(都不问"空"就直接返回会漏)
                last_exc = last_exc or None
            except PlatformError as exc:
                self._trip(name)
                logger.warning("列表源[%s]失败,切换下一源:%s", name, str(exc)[:120])
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        return []

    def list_feeds(self, kw: str = "", limit: int = 100) -> list[dict]:
        """订阅搜索转发:任一后端有该能力即可用(WeRSS 的 `kw` 搜索在加号路径要用)。"""
        for name, be in self._backends:
            fn = getattr(be, "list_feeds", None)
            if fn is None or self._cooling(name):
                continue
            try:
                return fn(kw=kw, limit=limit)
            except PlatformError:
                self._trip(name)
        return []

    def refresh_mp(self, mp_id: str, end_page: int = 1) -> bool:
        """催抓取逐个后端尝试(有该方法的);全无返回 False。"""
        for name, be in self._backends:
            fn = getattr(be, "refresh_mp", None)
            if fn is None or self._cooling(name):
                continue
            try:
                return bool(fn(mp_id, end_page=end_page))
            except PlatformError:
                self._trip(name)
        return False


def _wemp_client(session, user_id: int):
    """自研 appmsgpublish 客户端(凭据自持 system_config[wemp_cred_{uid}])。

    2026-09-30:WeRSS 同类项目有停维前科,列表源不能赌单一开源项目存活——
    该客户端按公开接口合同独立实现,与 WeRSS 互备。无凭据返回 None。
    """
    import json as _json

    from app.db.models import SystemConfig
    from app.services.wechat.wemp_client import WempClient

    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == f"wemp_cred_{user_id}"))
    if not row or not row.value:
        return None
    try:
        cred = _json.loads(row.value)
    except ValueError:
        return None
    if not cred.get("cookie") or not cred.get("token"):
        return None
    return WempClient(cred["cookie"], cred["token"])


def _platform_client(settings: Settings, session=None, user_id: int | None = None):
    """免费全量列表的数据源客户端;各家合同一致(都提供 `mp_articles`),按配置择一。

    优先级:WeRSS(自建成熟,含 free_publish 降级) → **自研 WempClient(兜底,凭据自持)**
    → wewe-rss 兼容"读书平台"。都没配返回 None。
    传了 session+user_id 才会考虑自研兜底(凭据存 system_config,与用户绑定)。
    """
    backends: list[tuple[str, object]] = []
    if settings.wechat_werss_url and settings.wechat_werss_ak and settings.wechat_werss_sk:
        backends.append(("werss", WerssClient(settings.wechat_werss_url,
                                              access_key=settings.wechat_werss_ak,
                                              secret_key=settings.wechat_werss_sk)))
    if session is not None and user_id is not None:
        wc = _wemp_client(session, user_id)
        if wc is not None:
            backends.append(("wemp", wc))
    if settings.wechat_reader_platform_url and settings.wechat_reader_token:
        backends.append(("reader_platform", ReaderPlatformClient(
            settings.wechat_reader_platform_url,
            token=settings.wechat_reader_token, vid=settings.wechat_reader_vid)))
    if not backends:
        return None
    if len(backends) == 1:
        return backends[0][1]  # 单源直接返回(保持既有行为与 mock 友好)
    return MultiSourceClient(backends)
def add_benchmark(session: Session, user_id: int, url: str, nickname: str = "",
                  note: str = "", settings: Settings | None = None) -> dict:
    """贴一篇文章长链即加号(不产生 API 调用);配了 key 时顺手解析昵称/ghid。"""
    settings = settings or get_settings()
    url = (url or "").strip()
    if not url.startswith("http"):
        raise ValueError("请粘贴公众号文章链接(mp.weixin.qq.com/...)")
    dup = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.anchor_url == url))
    if dup:
        raise ValueError("该文章链接对应的对标号已存在")
    ghid = ""
    biz = ""
    # 解析优先级:读书平台/WeRSS(免费)→ 文章页直抓(免费);失败不挡加号
    plat = _root._platform_client(settings)
    if plat:
        try:
            mp = plat.resolve_mp(url) if hasattr(plat, "resolve_mp") else None
            if mp is None:
                raise PlatformError("当前列表源不支持链接解析")
            biz = mp["mp_id"]
            nickname = nickname or mp["name"]
        except PlatformError as exc:
            logger.info("读书平台解析公众号失败:%s", exc)
    if not biz or not nickname:
        meta = _root.extract_article_meta(url)
        # 文章页解出的是 base64 `__biz`,**不是**列表源认识的订阅 id,写进 biz 只会让 ⓪ 分支
        # 拿着它去撞空列表并挤掉微信读书 cover(见 feed_biz)。昵称照取,biz 不落地。
        nickname = nickname or meta.get("name", "")
    # WeRSS 没有"链接→公众号"接口,所以昵称是它那边唯一的线索:同名订阅已存在就直接接上列表源,
    # 免得运营者为一个新号再跑一遍回填脚本(接上后由接口在后台催 WeRSS 抓一次)。
    if not biz and nickname:
        biz = find_feed_biz_by_name(plat, nickname)
    row = WechatBenchmark(user_id=user_id, nickname=(nickname or "未命名").strip()[:128],
                          ghid=ghid, biz=biz[:64], anchor_url=url[:500],
                          note=(note or "").strip()[:255])
    session.add(row)
    session.commit()
    return {"id": row.id, "nickname": row.nickname, "ghid": row.ghid,
            "biz": row.biz, "anchor_url": row.anchor_url}
def list_benchmarks(session: Session, user_id: int) -> list[dict]:
    rows = session.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id).order_by(WechatBenchmark.id.desc())).all()
    out = []
    for r in rows:
        art_count = session.scalar(
            select(WechatArticle.id).where(WechatArticle.user_id == user_id,
                                           WechatArticle.benchmark_id == r.id).limit(1))
        out.append({
            "id": r.id, "nickname": r.nickname, "ghid": r.ghid, "biz": r.biz,
            "weread_book_id": r.weread_book_id, "anchor_url": r.anchor_url,
            "note": r.note, "active": bool(r.active), "miss_count": r.miss_count,
            "last_item_at": r.last_item_at.isoformat(sep=" ", timespec="seconds") if r.last_item_at else None,
            "has_articles": art_count is not None,
        })
    return out
def _norm_mp_name(value: str) -> str:
    return re.sub(r"\s+", "", (value or "")).strip().lower()
def werss_feed_index(plat: object) -> dict[str, list[str]]:
    """WeRSS 订阅按名称归组:{规范化名称: [feed_id, ...]}(重名的都留着,好让调用方判定歧义)。

    每页 ≤100(上游 `le=100`),翻页上限 20 页 = 2000 个订阅——远超我们的用量,
    真到上限也宁可少翻页也别把监听拖成几分钟。
    """
    index: dict[str, list[str]] = {}
    page, size = 0, 100
    while page < 20:
        feeds = plat.list_feeds(limit=size, offset=page * size)  # type: ignore[attr-defined]
        for f in feeds:
            key = _norm_mp_name(f.get("mp_name", ""))
            if key and f.get("id"):
                index.setdefault(key, []).append(str(f["id"]))
        if len(feeds) < size:
            break
        page += 1
    return index
def match_biz_from_werss(session: Session, user_id: int,
                         settings: Settings | None = None, apply: bool = False) -> dict:
    """按公众号名称把 WeRSS 的订阅 id 回填进 `biz` 列(接上免费全量列表的最后一公里)。

    现有对标号绝大多数是从微信读书书架导入的:只有 `weread_book_id`,没有 `biz`,
    而监听 ⓪ 分支的门槛正是 `plat and b.biz`——不回填就一行也不会多推。
    两边唯一共同的信息是**号的名字**,所以只能按名称匹配,并且必须把"重名/找不到"如实报出来:
    猜一个填上去,后果下一整轮监听都在把别人的文章当这个号的推给员工。

    `apply=False` 只出计划不落库(默认),`apply=True` 才写。已是可用形态的 `biz` 不动,
    但**源认不出的旧值**(历史上写进去的 base64 `__biz`)按空处理并就地纠正——它没有任何消费者,
    留着只会让 ⓪ 分支永远跳过这个号。
    """
    settings = settings or get_settings()
    plat = _root._platform_client(settings)
    if plat is None or not hasattr(plat, "list_feeds"):
        raise ValueError("未配置 WeRSS(WECHAT_WERSS_URL/AK/SK),无法按名称回填订阅 id")
    index = werss_feed_index(plat)
    rows = session.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id).order_by(WechatBenchmark.id)).all()
    matched: list[dict] = []
    ambiguous: list[dict] = []
    missing: list[str] = []
    already = 0
    for r in rows:
        if feed_biz(r):
            already += 1
            continue
        stale = (r.biz or "").strip()   # 非空但形态不对 = 待纠正的旧值
        hits = [h for h in index.get(_norm_mp_name(r.nickname), []) if h.startswith(_FEED_BIZ_PREFIX)]
        if len(hits) == 1:
            matched.append({"id": r.id, "nickname": r.nickname, "biz": hits[0],
                            **({"was": stale} if stale else {})})
            if apply:
                r.biz = hits[0][:64]
        elif len(hits) > 1:
            # 重名:让运营者在 WeRSS 后台把订阅名改成可区分的(如加后缀),或人工指定 biz
            ambiguous.append({"id": r.id, "nickname": r.nickname, "candidates": hits})
        else:
            missing.append(r.nickname)
    if apply and matched:
        session.commit()
    return {"matched": len(matched), "applied": bool(apply and matched),
            "already": already, "ambiguous": ambiguous, "missing": missing,
            "detail": matched}
def remove_benchmark(session: Session, user_id: int, benchmark_id: int) -> None:
    """删除对标号并级联清理其关联数据(pan_links/采样点/文章),防止孤儿行堆积。

    用户 API 文案明确"已同步文章保留"指向的是**通用文章库**;
    这里删的是 benchmark_id 直接关联的行——对标号删除后这些数据无法再归属。
    """
    row = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.id == benchmark_id))
    if row is None:
        raise KeyError("对标账号不存在")
    arts = session.scalars(select(WechatArticle.id).where(
        WechatArticle.user_id == user_id, WechatArticle.benchmark_id == benchmark_id)).all()
    if arts:
        session.execute(delete(WechatPanLink).where(WechatPanLink.article_id.in_(arts)))
        session.execute(delete(WechatTrafficSample).where(WechatTrafficSample.article_id.in_(arts)))
        # 改写稿挂在文章下,文章删了不留行就是指向空文章的孤儿(大 Text 永久堆积)
        session.execute(delete(WechatRewrite).where(WechatRewrite.article_id.in_(arts)))
        session.execute(delete(WechatArticle).where(WechatArticle.id.in_(arts)))
    session.delete(row)
    session.commit()
def set_benchmark_active(session: Session, user_id: int, benchmark_id: int, active: bool) -> None:
    row = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.id == benchmark_id))
    if row is None:
        raise KeyError("对标账号不存在")
    row.active = bool(active)
    session.commit()
def _is_privileged(session: Session, user_id: int) -> bool:
    """是否可回退到运营者全局资源(weread Cookie 等,admin 专属)。"""
    user = session.get(User, user_id)
    return user is not None and user.role == "admin"
def _quark_cookie(session: Session, user_id: int, settings: Settings) -> str:
    """夸克 Cookie:优先用户在平台内配置的「quark」,其次全局 QUARK_COOKIE。

    与 baidupan 对齐(网盘转存是"往 Cookie 主人的盘里写"的个人动作):
    只挂 .env 全局值时,运营者改一次要重启容器,多用户也没法各用自己的盘。
    """
    from app.services.cookie_store import get_cookie

    return (get_cookie(session, user_id, "quark") or settings.quark_cookie or "").strip()
def _weread_cookie(session: Session, user_id: int, settings: Settings) -> str:
    """微信读书 Cookie:优先用户在平台内配置的「weread」,其次全局 WEREAD_COOKIE。

    这是**监听取数**通道:微信读书账号可查任意公开公众号的文章列表,全局运营者
    Cookie 作为共享抓取凭据是所有租户监听正常工作的基础,故此处允许普通用户回退
    全局。暴露"运营者关注了哪些号"的书架类操作另用 _weread_cookie_for_shelf 收口。
    """
    from app.services.cookie_store import get_cookie

    return (get_cookie(session, user_id, "weread") or settings.weread_cookie or "").strip()
def _weread_cookie_for_shelf(session: Session, user_id: int, settings: Settings) -> str:
    """书架/导入/续期通道用的 Cookie:普通用户**只能用自己的**「weread」。

    与监听通道分开(2026-09-22 审计):
    - `weread_shelf`/`import_benchmarks_from_shelf` 走全局 Cookie 会把运营者账号
      "关注的全部公众号 + bookId"整份返回给任意注册用户(横向信息泄露),import 还会
      把它批量写进调用者的对标号库;
    - `refresh_weread_cookie` 走全局 Cookie 续期后,轮换出的新 skey 经 set_cookie 落到
      调用者自己的 UserCookie 行,而 .env 里的全局旧值被微信读书作废 → 一击打穿运营者
      共享凭据,连带所有依赖全局兜底的租户监听集体失效。
    故这三处不得回退全局,普通用户未自配即视为无 Cookie。
    """
    from app.services.cookie_store import get_cookie

    own = (get_cookie(session, user_id, "weread") or "").strip()
    if own:
        return own
    if not _is_privileged(session, user_id):
        return ""
    return (settings.weread_cookie or "").strip()
def weread_shelf(session: Session, user_id: int, settings: Settings | None = None) -> list[dict]:
    """列出微信读书书架上的公众号(导入预览;需先在微信读书 App 内关注目标号)。"""
    settings = _base(settings)
    cookie = _weread_cookie_for_shelf(session, user_id, settings)
    if not cookie:
        raise ValueError("未配置微信读书 Cookie(平台 Cookie「weread」或 WEREAD_COOKIE)")
    return _root.WereadClient(cookie).shelf()
def import_benchmarks_from_shelf(session: Session, user_id: int,
                                 settings: Settings | None = None) -> dict:
    """微信读书书架一键导入:MP_WXS_* 条目 → 对标号(免费,自动关联 weread_book_id)。"""
    settings = _base(settings)
    cookie = _weread_cookie_for_shelf(session, user_id, settings)
    if not cookie:
        return {"status": "skipped", "reason": "no_cookie"}
    books = _root.WereadClient(cookie).shelf()
    created = updated = 0
    for book in books:
        bid, name = book["book_id"], book["name"]
        row = session.scalar(select(WechatBenchmark).where(
            WechatBenchmark.user_id == user_id,
            or_(WechatBenchmark.weread_book_id == bid,
                WechatBenchmark.nickname == (name or "未命名"))))
        if row is None:
            session.add(WechatBenchmark(user_id=user_id, nickname=(name or "未命名")[:128],
                                        weread_book_id=bid[:64], note="微信读书书架导入"))
            created += 1
        elif not row.weread_book_id:
            row.weread_book_id = bid[:64]
            updated += 1
    session.commit()
    return {"status": "success", "shelf": len(books), "created": created, "updated": updated}
def _cookie_fingerprint(cookie: str) -> str:
    """Cookie 短指纹(前 6 位 md5):告警 key 携带它,换新 Cookie 后冷却自动重置——
    否则新 Cookie 的第一次死亡会被旧 Cookie 时期的同标题告警冷却拦住(2026-09-16 实测)。"""
    import hashlib

    return hashlib.md5(str(cookie or "").encode()).hexdigest()[:6]
_RENEWAL_COOLDOWN_KEY = "weread_renewal_cooldown_{uid}"
_RENEWAL_COOLDOWN_MIN = 120  # renewal 失败后的冷却:实测连续撞会触发微信读书 renewal 频控,
def _renewal_cooldown_until(session: Session, user_id: int) -> datetime | None:
    from app.db.models import SystemConfig

    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == _RENEWAL_COOLDOWN_KEY.format(uid=user_id)))
    if row and row.value:
        try:
            return datetime.fromisoformat(row.value)
        except ValueError:
            return None
    return None
def refresh_weread_cookie(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    """微信读书 Cookie 续期:长效 wr_rt → 新短效 wr_skey,并回写 Cookie 管理。

    wr_skey 短效且轮换(续期后旧 skey 很快 -2012),故续期成功**必须回写**;
    全局 WEREAD_COOKIE(.env)无法回写文件,统一落到平台内「weread」Cookie
    (读取优先级:平台内 > 全局,下次监听即用新值)。
    renewal 失败后进入 2h 冷却(频控风控锁定期内反复撞只会延长封锁)。
    返回 {status: success|skipped|failed, reason?, verified, cookie?}。
    """
    from app.db.models import SystemConfig
    from app.services.cookie_store import get_cookie, set_cookie

    settings = _base(settings)
    # 冷却优先于 Cookie 检查:锁定期内连解析都不做(避免每分钟监听自救反复撞频控)
    cooldown_until = _renewal_cooldown_until(session, user_id)
    if cooldown_until and datetime.now() < cooldown_until:
        return {"status": "skipped", "reason": "renewal_cooldown",
                "retry_after": cooldown_until.isoformat(sep=" ", timespec="seconds")}
    # 续期只作用于"调用者自己的 Cookie":走全局续期会把轮换出的新 skey 落到调用者
    # 自己行、作废 .env 里的全局值,一击打穿运营者共享凭据(见 _weread_cookie_for_shelf)。
    cookie = _weread_cookie_for_shelf(session, user_id, settings)
    if not cookie:
        return {"status": "skipped", "reason": "no_cookie"}
    if "wr_rt=" not in cookie:
        return {"status": "skipped", "reason": "no_rt"}
    new_cookie = _root.WereadClient(cookie).refresh_skey()
    if not new_cookie:
        # 失败:进入冷却,停止风控锁定期内的反复撞(每分钟监听自救 × 6h tick 会加剧封锁)
        row = session.scalar(select(SystemConfig).where(
            SystemConfig.key == _RENEWAL_COOLDOWN_KEY.format(uid=user_id)))
        until = datetime.now() + timedelta(minutes=_RENEWAL_COOLDOWN_MIN)
        if row:
            row.value = until.isoformat()
        else:
            session.add(SystemConfig(key=_RENEWAL_COOLDOWN_KEY.format(uid=user_id),
                                     value=until.isoformat()))
        session.commit()
        return {"status": "failed", "reason": "renewal_failed"}
    # 先验证再回写:续期后仍 -2012/-2010 说明登录态整体过期(wr_rt 也失效),
    # 此时回写的新值同样无效,不能报"已续期"误导用户——直接失败让用户重新登录。
    try:
        _root.WereadClient(new_cookie).shelf()
    except WereadAuthError as exc:
        # 能换出 skey 但验证即死 → renewal 接口正处于频控期(换出的即刻作废),
        # 与换不出同样需要冷却,否则每分钟监听自救继续撞,延长封锁
        logger.warning("微信读书续期后仍登录失效(用户 %s):%s;进入续期冷却", user_id, exc)
        row = session.scalar(select(SystemConfig).where(
            SystemConfig.key == _RENEWAL_COOLDOWN_KEY.format(uid=user_id)))
        until = datetime.now() + timedelta(minutes=_RENEWAL_COOLDOWN_MIN)
        if row:
            row.value = until.isoformat()
        else:
            session.add(SystemConfig(key=_RENEWAL_COOLDOWN_KEY.format(uid=user_id),
                                     value=until.isoformat()))
        session.commit()
        return {"status": "failed", "reason": "expired"}
    except WereadError as exc:  # 非登录问题(风控/接口异常):保留续期结果但标注未验证
        logger.warning("微信读书续期后书架验证异常(非登录问题,用户 %s):%s", user_id, exc)
        set_cookie(session, user_id, "weread", new_cookie)
        return {"status": "success", "verified": False, "cookie": new_cookie}
    set_cookie(session, user_id, "weread", new_cookie)
    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == _RENEWAL_COOLDOWN_KEY.format(uid=user_id)))
    if row:
        session.delete(row)
    # 打"会话初期"标记:新 Cookie 的 mp/articles 列表接口仅在会话初期可用,
    # 此时自动触发一轮全量 sync 把各对标号停更期间的历史文章补齐
    # ( cover 只出最新一篇,停更号的历史列表平时拿不到——2026-09-18 诊断)
    flag_key = f"weread_fullsync_pending_{user_id}"
    flag = session.scalar(select(SystemConfig).where(SystemConfig.key == flag_key))
    if flag:
        flag.value = datetime.now().isoformat()
    else:
        session.add(SystemConfig(key=flag_key, value=datetime.now().isoformat()))
    session.commit()
    logger.info("微信读书 Cookie 已续期并验证通过(用户 %s),已标记全量补采", user_id)
    # 恢复确认(对齐闲鱼"✅采集已恢复",2026-09-30):失败告警发过,恢复也得说一声——
    # 否则群里"续期失败"的旧告警变成孤魂,用户看到旧告警+新推送并存会误判(实测困惑)
    try:
        from app.services.alert_service import feishu_alert_gate
        from app.services.feishu_client import FeishuClient, webhook_for as _wf

        _st = settings or get_settings()
        _hook = _wf(_st, "wechat")
        if _hook and feishu_alert_gate(session, user_id, "weread_renewal_ok",
                                       f"renewal_ok:{user_id}", 6, "续期成功,监听恢复"):
            FeishuClient(_hook, _st.feishu_secret).send(
                "✅ 微信读书 Cookie 已自动续期,监听恢复正常——此前如有「续期失败」告警,以本条为准")
    except Exception:  # noqa: BLE001 - 恢复确认是锦上添花,失败不影响续期结果
        logger.debug("续期恢复确认推送失败(不影响续期)", exc_info=True)
    return {"status": "success", "verified": True, "cookie": new_cookie}
_RENEWAL_FAIL_TEXT = {
    "renewal_failed": "renewal 换不出新 wr_skey(wr_rt 已失效,或正处于频控锁定期)",
    "expired": "换出了新 wr_skey 但书架验证仍报登录失效(登录态整体过期,需重新扫码)",
    "no_rt": "Cookie 里根本没有 wr_rt,自动续期无从下手(这份 Cookie 十几小时必过期)",
    "exception": "续期流程抛异常(见服务端日志)",
}
def weread_refresh_tick(settings: Settings | None = None) -> int:
    """定时续期:wr_skey 短效且轮换制,有效期内主动换新则永不过期(兜底是 wr_rt,约 30 天)。

    失败(wr_rt 整体过期/续期被拒)必须即时推飞书——否则要等监听断掉才发现,
    用户感知就是"Cookie 过期好快"。提醒自带冷却,不刷屏。
    返回续期成功的账号数;单用户失败不影响其余。
    """
    from app.db import get_session_local
    from app.db.models import User
    from app.services.alert_service import notify_incident

    settings = settings or get_settings()
    db = get_session_local()()
    total = 0
    failed: list[tuple[int, str]] = []
    try:
        users = db.scalars(select(User.id).where(User.enabled.is_(True)).order_by(User.id)).all()
        for uid in users:
            try:
                out = _root.refresh_weread_cookie(db, uid, settings=settings)
                if out.get("status") == "success":
                    total += 1
                elif out.get("status") == "failed":
                    failed.append((uid, str(out.get("reason") or "")))
                elif out.get("reason") == "no_rt":
                    # 缺 wr_rt 是"续期根本没起跑",旧逻辑当 skipped 静默放过 → 运营者以为
                    # 自动续期在守着,实际这份 Cookie 十几小时必死(监听随后断源)。
                    failed.append((uid, "no_rt"))
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("微信读书续期失败 user=%s", uid)
                failed.append((uid, "exception"))
    finally:
        if failed:
            try:
                detail = "; ".join(f"用户{u}:{_RENEWAL_FAIL_TEXT.get(r, r) or '未知原因'}" for u, r in failed)
                from app.services.cookie_store import get_cookie

                fp = _cookie_fingerprint(get_cookie(db, failed[0][0], "weread") or "")
                notify_incident(
                    db, failed[0][0], "wechat",
                    f"🟠 微信读书 Cookie 自动续期失败,请重新复制[{fp}]",
                    f"{detail}。监听即将断源。"
                    "请在浏览器登录 weread.qq.com → F12 → 网络 复制完整 Cookie,"
                    "更新到「Cookie 管理」页 weread 平台。"
                    "粘贴前先确认串里有「wr_rt=」(约 30 天,自动续期只认它;缺它十几小时就死)。"
                    "复制后尽量不要再在该浏览器使用微信读书——浏览器会自己轮换 wr_skey,"
                    "把服务端这份顶失效(这是 Cookie『过期快』的主因)。",
                    settings=settings)
            except Exception:  # noqa: BLE001 - 提醒失败不影响续期结果
                logger.exception("微信读书续期失败提醒推送异常")
        db.close()
    if total:
        logger.info("微信读书 Cookie 定时续期完成:%d 个账号", total)
    return total
