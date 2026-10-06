"""数据源选择/Cookie 解析/对标号 CRUD/书架导入/微信读书续期(彼此咬合,故同模块)。"""

from app.db.models import (User, WechatArticle, WechatBenchmark, WechatPanLink, WechatRewrite, WechatTrafficSample)

from app.services.reader_platform_client import PlatformError, ReaderPlatformClient

from app.services.tenant_base import _base

from app.services.weread_client import WereadAuthError, WereadError
from app.services import weread_budget   # 额度统一入口:续期成功要解熔断(新会话=新额度账)

from app.services.werss_client import WerssClient

from config.settings import Settings, get_settings

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select

from sqlalchemy.orm import Session

import re



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
    """自研 appmsgpublish 客户端(凭据自持 `system_config[wemp_cred_{uid}]`,**加密存储**)。

    2026-09-30:WeRSS 同类项目有停维前科,列表源不能赌单一开源项目存活——
    该客户端按公开接口合同独立实现,与 WeRSS 互备。无凭据返回 None。

    读凭据统一走 `app.services.wemp_cred`(单一事实源:健康页/录入脚本也用它,
    免得改一处漏一处)。那边解不开时返回空 dict,这里就当"未配置"降级到下一个源。
    """
    from app.services.wemp_cred import load as _load_wemp_cred
    from app.services.wechat.wemp_client import WempClient

    cred = _load_wemp_cred(session, user_id)
    if not cred.get("cookie") or not cred.get("token"):
        return None
    return WempClient(cred["cookie"], cred["token"])


def configured_backends(settings: Settings, session=None,
                        user_id: int | None = None) -> list[tuple[str, object]]:
    """当前**配好了的**列表源后端,按优先级排列(顺序 = 择源顺序,别顺手调)。

    ⚠️ **为什么要把这份清单单独暴露出来**(2026-10-06):`MultiSourceClient` 会把某个源的
    失效吞成"换下一个源",外面**只看得到最后那个异常** —— 分不出是谁死的。
    实测代价:`wemp`(自研兜底,本该是"WeRSS 挂了"的保险)自 10-02 起每轮报
    `200003 会话失效`,**连着 4 天没人知道**,因为没人能单独问它一句。
    体检要能**逐个验活**,就得先能拿到"逐个"。

    ⚠️ 还有一层:`wemp` 的凭据要 `session` 才读得到(system_config),所以体检调用时
    必须把 session 传进来,否则**这一源会被静默漏掉**,体检反而给出假绿。
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
    return backends


def _platform_client(settings: Settings, session=None, user_id: int | None = None):
    """免费全量列表的数据源客户端;各家合同一致(都提供 `mp_articles`),按配置择一。

    优先级:WeRSS(自建成熟,含 free_publish 降级) → **自研 WempClient(兜底,凭据自持)**
    → wewe-rss 兼容"读书平台"。都没配返回 None。
    传了 session+user_id 才会考虑自研兜底(凭据存 system_config,与用户绑定)。
    """
    backends = configured_backends(settings, session, user_id)
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
def _subscribe_by_name(plat: object, nickname: str) -> str:
    """把公众号名补进 WeRSS 订阅池,返回订阅 id(即 `biz`);拿不准一律空串,不猜。

    先查已有订阅(`find_feed_biz_by_name`):命中就零副作用返回——**这一步很关键**,
    因为 `add_feed` 会顺带排一次历史抓取,对已在池子里的号重复调用纯属浪费上游配额。
    没订阅才去搜全量号:只在**规范化名精确相等**时订阅(搜狗给的候选名就是公众号名,
    形近号(「XX说」vs「XX説」)一律不认);重名歧义也走空串,交人工。
    """
    if not hasattr(plat, "search_mp"):
        # ⚠️ **绝不能静默返回空串**(2026-10-05 审计揪出的潜在失效):
        # 返回空串会被上层写成「WeRSS 未搜到该号或重名歧义」—— 而真相是
        # **"当前列表源根本没有按名解析能力"** ⇒ **两句提示的方向完全相反**:
        #   一个说"这个号不存在"(去别处找号),一个说"我们查不了"(去修配置)。
        # 触发条件今天不成立(**`add_benchmark_by_name` 调 `_platform_client(settings)`
        # 不传 session ⇒ 只拿得到裸 `WerssClient`,它有 `search_mp`**),但**一旦配上
        # `reader_platform`,`_platform_client` 就会返回 `MultiSourceClient`(不转发
        # `search_mp`)⇒ 按名加号会**无声失效**,而且提示还把人往错方向带。**
        # 所以这里必须抛:让 hint 说"解析能力缺失",而不是"号没搜到"。
        raise PlatformError(
            f"当前列表源({type(plat).__name__})不支持按公众号名解析(缺 search_mp)"
            "⇒ 无法把号名换成订阅 id;检查 WeRSS 配置,或改用「文章链接」方式加号")
    existing = find_feed_biz_by_name(plat, nickname)
    if existing:
        return existing
    want = _norm_mp_name(nickname)
    hits = [h for h in plat.search_mp(nickname, limit=10)  # type: ignore[attr-defined]
            if _norm_mp_name(h.get("nickname", "")) == want]
    if len(hits) != 1:
        return ""
    feed = plat.add_feed(hits[0]["nickname"], hits[0]["fakeid"],  # type: ignore[attr-defined]
                         avatar=hits[0].get("avatar", ""), intro=hits[0].get("intro", ""))
    fid = str(feed.get("id") or "")
    return fid if fid.startswith(_FEED_BIZ_PREFIX) else ""
def add_benchmark_by_name(session: Session, user_id: int, nickname: str, note: str = "",
                          settings: Settings | None = None) -> dict:
    """候选无文章链接时按公众号名加号:补进 WeRSS 订阅 → 拿到 biz,直接可监听。

    与 `add_benchmark` 的分工:那条路拿**文章链接**当锚点(锚点空就没法查重),这条拿**号名**。
    候选号正是"有名字、没链接"的形态——搜狗结果链接是 `/link?url=` 二次跳转(且要执行
    JS 拼接才拿得到,见 `sogou_weixin`),所以我们从没存下来过。补齐订阅即等价于手动加号:
    `biz` 一落地,监听 ⓪ 分支下一轮就会走 WeRSS 列表源抓取。

    返回 {"id", "nickname", "biz", "listenable", "created", "hint"}。
    """
    name = (nickname or "").strip()
    if not name:
        raise ValueError("公众号名不可为空")
    dup = session.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.nickname == name))
    if dup is not None:
        listenable = bool(feed_biz(dup) or dup.weread_book_id)
        return {"id": dup.id, "nickname": dup.nickname, "biz": dup.biz, "created": False,
                "listenable": listenable,
                "hint": "" if listenable else "已存在同名对标号,但仍无可监听标识"}
    settings = settings or get_settings()
    plat = _root._platform_client(settings)
    biz, hint = "", ""
    if plat is None:
        hint = "未配置列表源(WeRSS),已建号但暂无监听能力"
    else:
        try:
            biz = _subscribe_by_name(plat, name)
        except PlatformError as exc:  # 订阅失败不挡建号:号先留着,下次收录会重试
            logger.info("候选订阅 WeRSS 失败:%s", exc)
            hint = f"订阅列表源失败:{exc}"
        if not biz and not hint:
            hint = "WeRSS 未搜到该号或重名歧义,已建号但暂无监听能力"
    row = WechatBenchmark(user_id=user_id, nickname=name[:128], ghid="", biz=biz[:64],
                          # WeRSS 的订阅 id 是 `MP_WXS_<base64解码(fakeid)>`,与微信读书的
                          # bookId **同一编号**(老号 142 个当初就是拿 book_id 当 feed id 导进去的,
                          # 2026-10-01 又用 mp_cover 在未关注的号上验过:返回号名/头像/最新一篇)。
                          # 填上它,该号立刻能走微信读书链路被监听——否则光有 biz 而 WeRSS 抓不到东西
                          # (上游限流中)时,这个号就是个哑号。
                          weread_book_id=biz[:64],
                          anchor_url="", note=(note or "").strip()[:255])
    session.add(row)
    session.commit()
    return {"id": row.id, "nickname": row.nickname, "biz": row.biz, "created": True,
            "listenable": bool(biz), "hint": hint}
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
#: 续期**失败**时立的旗子 —— 「恢复通知」的前提条件(见 `refresh_weread_cookie`)。
_RENEWAL_FAILED_KEY = "weread_renewal_failed_{uid}"


def _renewal_failed_since(session: Session, user_id: int) -> str:
    """续期是否处于"失败未恢复":返回失败时间;空串 = **没有失败要恢复**。"""
    from app.db.models import SystemConfig

    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == _RENEWAL_FAILED_KEY.format(uid=user_id)))
    return str(row.value or "") if row else ""


def _mark_renewal_failed(session: Session, user_id: int) -> None:
    """续期失败 → 立旗。**恢复通知的前提是这个旗子,而不是时间冷却**。"""
    from app.db.models import SystemConfig

    key = _RENEWAL_FAILED_KEY.format(uid=user_id)
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
    if row is None:
        session.add(SystemConfig(key=key, value=datetime.now().isoformat()))
    else:
        row.value = datetime.now().isoformat()


def _clear_renewal_failed(session: Session, user_id: int) -> None:
    """**恢复通知真发出去了**才落旗 —— 没发成就不落,下一轮还会想发。"""
    from app.db.models import SystemConfig

    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == _RENEWAL_FAILED_KEY.format(uid=user_id)))
    if row is not None:
        session.delete(row)


def refresh_weread_cookie(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    """微信读书 Cookie 续期:长效 wr_rt → 新短效 wr_skey,并回写 Cookie 管理。

    wr_skey 短效且轮换(续期后旧 skey 很快 -2012),故续期成功**必须回写**;
    全局 WEREAD_COOKIE(.env)无法回写文件,统一落到平台内「weread」Cookie
    (读取优先级:平台内 > 全局,下次监听即用新值)。
    renewal 失败后进入 2h 冷却(频控风控锁定期内反复撞只会延长封锁)。
    返回 {status: success|skipped|failed, reason?, verified, cookie?}。
    """
    from app.db.models import SystemConfig
    from app.services.cookie_store import set_cookie

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
        # ⚠️ **续期成功 = 新会话 = 新的额度账** ⇒ 解除额度熔断(2026-10-05)。
        # 不解除的话,上一把会话吃光额度时记下的冷静期会**继续压着新会话**,
        # 于是"刚换到一把好会话,却因为旧账被停半小时" —— 白白浪费掉最鲜的那段窗口。
        weread_budget.clear(session, user_id)
        return {"status": "success", "verified": False, "cookie": new_cookie}
    set_cookie(session, user_id, "weread", new_cookie)
    weread_budget.clear(session, user_id)   # 新会话 = 新额度账(见上一条注释)
    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == _RENEWAL_COOLDOWN_KEY.format(uid=user_id)))
    if row:
        session.delete(row)
    # 打"会话初期"标记:新 Cookie 的 mp/articles 列表接口仅在会话初期可用,
    # 此时自动触发一轮全量 sync 把各对标号停更期间的历史文章补齐
    # ( cover 只出最新一篇,停更号的历史列表平时拿不到——2026-09-18 诊断)
    #
    # ⚠️ **开关关着就别打标记**(2026-10-04 修):消费这个标记的 `run_full_sync_if_pending`
    # 默认关停(实测"renewal 后打 81×2 的补采炸弹会数小时内打穿全部会话额度"),
    # 而这里**每次续期成功都无条件写** ⇒ 两件事:
    #   ① 标记**永远不清**,库里常驻一行垃圾(今天 17:48 又写了一行);
    #   ② 埋雷:哪天把开关打开,会**立刻**对全部 142 个号打一轮列表、把当时那把会话的
    #      额度烧光 —— 而监听现在是按"最久没轮到"轮转的,额度被抽干后后面的号全退到
    #      cover,**轮转节奏直接被打乱**。
    # 所以:开关关着就不写标记(要补采的人本来就得先打开开关)。
    if getattr(settings or get_settings(), "weread_fullsync_on_renewal", False):
        flag_key = f"weread_fullsync_pending_{user_id}"
        flag = session.scalar(select(SystemConfig).where(SystemConfig.key == flag_key))
        if flag:
            flag.value = datetime.now().isoformat()
        else:
            session.add(SystemConfig(key=flag_key, value=datetime.now().isoformat()))
        session.commit()
        logger.info("微信读书 Cookie 已续期并验证通过(用户 %s),已标记全量补采", user_id)
    else:
        logger.info("微信读书 Cookie 已续期并验证通过(用户 %s);全量补采开关关闭,不打标记",
                    user_id)
    # 恢复确认(对齐闲鱼"✅采集已恢复",2026-09-30):失败告警发过,恢复也得说一声——
    # 否则群里"续期失败"的旧告警变成孤魂,用户看到旧告警+新推送并存会误判(实测困惑)
    #
    # ⚠️⚠️ **2026-10-06 修:只有"真的失败过"才发**。原来这里是"续期成功就发",而续期
    # **每 6 小时成功一次**、冷静期恰好也设成 6 小时 ⇒ **系统完全健康也天天推"恢复正常"**
    # (实测约 2 次/天;用户直接来问"为什么今天还在提醒我",而其实一次失败都没发生过)。
    # 这条通知自己写着"此前如有「续期失败」告警,以本条为准",却**从不检查此前失败过没有**——
    # **没有失败就报恢复,等于把告警变成噪音,而噪音会训练人忽略整块告警**
    # (与"恒为 0 的档位""假红会训练人忽略整份报告"同一条教训)。
    # 判据改成旗子:失败时立(`_mark_renewal_failed`)、**发出去才落**(`_clear_renewal_failed`)。
    try:
        if _renewal_failed_since(session, user_id):
            from app.services.alert_service import feishu_alert_gate
            from app.services.feishu_client import FeishuClient, webhook_for as _wf

            _st = settings or get_settings()
            _hook = _wf(_st, "wechat")
            if _hook and feishu_alert_gate(session, user_id, "weread_renewal_ok",
                                           f"renewal_ok:{user_id}", 6, "续期成功,监听恢复"):
                FeishuClient(_hook, _st.feishu_secret).send(
                    "✅ 微信读书 Cookie 已自动续期,监听恢复正常——此前如有「续期失败」告警,以本条为准")
                _clear_renewal_failed(session, user_id)
                session.commit()
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
            # ★ **立旗**:这一轮确有失败 ⇒ 之后某轮续期成功时,才轮到"恢复通知"出场。
            # 没这面旗子,"恢复通知"就只能在不知道有没有失败的情况下凭时间瞎发(2026-10-06 修)。
            for _uid, _ in failed:
                _mark_renewal_failed(db, _uid)
            try:
                db.commit()
            except Exception:  # noqa: BLE001 - 立旗失败不该挡住真正的失败告警
                logger.exception("续期失败旗子落库失败(恢复通知可能漏发或误发)")
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


def retire_dormant_benchmarks(session, user_id: int, settings=None) -> list[str]:
    """死号清理(v2.6.0,用户口径:一星期没发文就取消监控)。返回被停用号昵称。

    **安全阀(关键)**:先确认近期监听链路是活的(近 N 天有 wechat_listen 成功记录)——
    否则 Cookie 故障期间所有号的 last_item_at 都不动,会被整体误判成"死号"一起停掉。
    停用即 active=False(不监听);号复活需人工恢复(前端可见"已停用"状态)。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import or_

    from app.db.models import RunRecord, WechatBenchmark

    st = settings or get_settings()
    days = int(getattr(st, "wechat_dormant_retire_days", 7) or 0)
    if days <= 0:
        return []
    cutoff = datetime.now() - timedelta(days=days)
    # 安全阀:链路不活不清理
    alive = session.scalar(select(func.count()).select_from(RunRecord).where(
        RunRecord.user_id == user_id, RunRecord.kind == "wechat_listen",
        RunRecord.status.in_(("success", "partial")),
        RunRecord.started_at >= cutoff)) or 0
    if not alive:
        logger.warning("死号清理跳过 user=%s:近 %s 天无成功监听(疑似链路故障,防误杀)", user_id, days)
        return []
    rows = session.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.active.is_(True),
        or_(WechatBenchmark.last_item_at < cutoff,
            and_(WechatBenchmark.last_item_at.is_(None),
                 WechatBenchmark.created_at < cutoff)))).all()
    names = []
    for b in rows:
        b.active = False
        names.append(str(b.nickname or b.id))
    if names:
        session.commit()
    return names


def retire_dormant_tick_all_users(settings=None) -> int:
    """每日死号清理入口(05:30,在 4 点定点监听之后判断);停用则站内汇总通知。"""
    from config.settings import get_settings as _gs
    from app.db import get_session_local
    from app.db.models import User

    st = settings or _gs()
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                names = retire_dormant_benchmarks(db, uid, st)
                if names:
                    from app.services.alert_service import notify_incident

                    days = int(getattr(st, "wechat_dormant_retire_days", 7) or 7)
                    notify_incident(db, uid, "wechat",
                                    f"🧹 死号清理:{len(names)} 个对标号连续 {days} 天无发文,已停监控",
                                    "停用名单:" + "、".join(names[:12])
                                    + ("…" if len(names) > 12 else "")
                                    + "。号若复活,可在「公众号监听」页手动重新启用。",
                                    settings=st, push_feishu=False)
                    db.commit()
                    total += len(names)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("死号清理失败 user=%s", uid)
    finally:
        db.close()
    return total
