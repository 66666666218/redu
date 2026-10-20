"""我方**自有分享链**登记表(2026-10-20)。

## 它解决什么
用户口径:「以后我们发公众号的网盘资源不要推送进飞书」「如果是我们的网盘就不要推」。
判"是不是我们的链"此前只有三条启发式:

1. 本轮刚转存(`replacements`);
2. 文章自己的 `my_pan_urls`;
3. 全库 `my_pan_urls` 里出现过的 URL。

它们共同的前提是:**这条链在本项目里被转存/生成过**。但有两类会漏:
· 我们在 App 里**手工**建的分享(没走转存流程);
· 同行/对标号**转发了我们的链**,而那篇文章的 `my_pan_urls` 是空的。

本模块把「每个我方网盘账号**自己创建过哪些分享**」拉下来(夸克
`/share/mypage/detail`,一页 50、按创建时间倒序),按 **`pwd_id`(URL 路径段)**
登记成一张表;推送前拿文章的盘链来查表。**多账号也成立** —— 表是各账号的并集,
链由 `quark#2` 建的、转存走的是 `quark`,照样认得出。

## 为什么不按"名字"判(用户提出过这个思路)
保存时能看见资源名,夸克也会提示「不能保存自己的文件」—— 这两条**都是对的观察**,
但都不能当判据:
· **名字**:同名资源满街都是,同行转存后连文件名都不改;按名字判会把**别人的链**
  当成我们的 ⇒ 整类文章不再推飞书,而且**没有任何报错**(静默少推最难发现)。
· **41017「禁止转存自己的分享」**:只在**当前账号就是分享者**时才会出现。
  我们不止一个夸克号,`quark` 去转 `quark#2` 的分享会**成功**,不会报 41017
  ⇒ 它只能当**实时补充信号**(命中就登记),不能当唯一判据。
⇒ 名字只进 `title` 供人工排查;归属只认 `pwd_id`。

## 刷新策略
· 首次:全量翻页(实测 `quark` 号 2000+ 条、约 41 页,一次约 40 秒);
· 之后:按创建时间**增量** —— 倒序翻到"比库里最新一条还老"就停,通常 1 页;
· 由调度作业每天 04:15 跑(`pan_owned_shares`),不在推送路径里打接口。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 夸克分享链 → `pwd_id`(URL 路径段,也就是归属判据)
_QUARK_SHARE_RE = re.compile(r"https?://pan\.quark\.cn/s/([0-9A-Za-z]+)")

#: 翻页上限(与 `quark_dup.SHARE_PAGE_CAP` 同量级):40 页 × 50 = 2000 条。
#: ⚠️ **翻到上限仍没到底就抛**,不能交半份 —— 半份会让"我们的链"漏判成"别人的链"。
PAGE_SIZE = 50
PAGE_CAP = 400

#: 单次写库批量(2000+ 条一次性 flush 会长时间占用 SQLite 写锁)
_COMMIT_EVERY = 200


def pwd_id_of(url_or_id: str) -> str:
    """从分享链/分享 id 里取 `pwd_id`;取不到返回空串(不猜)。"""
    s = str(url_or_id or "").strip()
    if not s:
        return ""
    m = _QUARK_SHARE_RE.search(s)
    if m:
        return m.group(1)
    # 只给路径段/内部 id 的调用方(如 41017 分支):纯字母数字才收
    return s if re.fullmatch(r"[0-9A-Za-z]+", s) else ""


def normalize_url(url_or_id: str) -> str:
    """规范化成 `https://pan.quark.cn/s/<pwd_id>`(带 `?pwd=` 的一律去掉)。"""
    pwd = pwd_id_of(url_or_id)
    return f"https://pan.quark.cn/s/{pwd}" if pwd else ""


def _to_ms(raw: Any) -> int:
    """平台时间戳 → 毫秒。夸克实测是毫秒;兼容万一返回秒的账号(小于 1e11 视为秒)。"""
    try:
        n = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return n if n >= 100_000_000_000 else n * 1000


def _ms(dt: datetime | None) -> int:
    if not dt:
        return 0
    try:
        return int(dt.timestamp() * 1000)
    except (OSError, OverflowError, ValueError):
        return 0


def fetch_quark_shares(qt: Any, *, since_ms: int = 0,
                       page_cap: int = PAGE_CAP) -> list[dict]:
    """拉一个夸克账号**自己创建过的分享**(倒序)。

    `since_ms > 0` 时只回"比它新"的条目:倒序翻页,遇到整页都不新于它就停。
    ⚠️ 翻到 `page_cap` 仍未到底且没有可停的增量边界 ⇒ 抛错,不交半份。
    """
    out: list[dict] = []
    for page in range(1, page_cap + 1):
        d = qt._request("GET", "/1/clouddrive/share/mypage/detail",
                        api="https://drive-pc.quark.cn",
                        params={"share_id": "", "_page": page, "_size": PAGE_SIZE,
                                "_order": "created_at:desc"})
        lst = list((d.get("data") or {}).get("list") or [])
        if not lst:
            return out
        page_ms: list[int] = []
        for a in lst:
            created_ms = _to_ms(a.get("created_at"))
            if created_ms:
                page_ms.append(created_ms)
            if since_ms and created_ms and created_ms <= since_ms:
                continue                      # 增量:比库里的还老,已经有了
            pwd = str(a.get("pwd_id") or "") or pwd_id_of(str(a.get("share_url") or ""))
            if not pwd:
                continue
            out.append({
                "pwd_id": pwd,
                "share_id": str(a.get("share_id") or ""),
                "url": normalize_url(pwd),
                "title": str(a.get("title") or ""),
                "first_fid": str(a.get("first_fid") or ""),
                "status": int(a.get("status") or 0),
                "created_at": (datetime.fromtimestamp(created_ms / 1000)
                               if created_ms else None),
                "created_ms": created_ms,
            })
        if len(lst) < PAGE_SIZE:
            return out                       # 短页 = 到底
        if since_ms and page_ms and max(page_ms) <= since_ms:
            return out                       # 整页都比库里旧 ⇒ 后面只会更旧
    raise RuntimeError(
        f"分享名单翻到 {page_cap} 页仍未到底(已收 {len(out)} 条),"
        "拒绝用**不完整**的名单做归属判据")


def _existing(session: Session, user_id: int, platform: str) -> dict[str, Any]:
    from app.db.models import PanOwnedShare

    rows = session.scalars(select(PanOwnedShare).where(
        PanOwnedShare.user_id == user_id,
        PanOwnedShare.platform == platform,
    )).all()
    return {str(r.pwd_id): r for r in rows}


def refresh(session: Session, user_id: int = 1, *, full: bool = False,
            refs: list[Any] | None = None) -> dict:
    """把一个用户**所有夸克账号**的自有分享同步进 `pan_owned_shares`。

    返回 `{accounts, fetched, added, updated, errors}`。
    单个账号失败只记错误、不拖垮其它账号(下次再补)。
    """
    from app.db.models import PanOwnedShare
    from app.services import pan_accounts
    from app.services.quark_transfer import QuarkTransfer

    if refs is None:
        refs = pan_accounts.accounts(session, "quark", user_id=user_id)
    stats = {"accounts": 0, "fetched": 0, "added": 0, "updated": 0, "errors": 0}
    book = _existing(session, user_id, "quark")
    for ref in refs:
        ck = pan_accounts.cookie_of(ref, session)
        if not ck:
            logger.warning("自有分享登记:%s 无可用 Cookie,跳过", ref.label)
            stats["errors"] += 1
            continue
        stats["accounts"] += 1
        last_ms = 0
        if not full:
            same = [r for r in book.values() if str(r.account_key) == str(ref.cookie_key)]
            last_ms = max((_ms(r.created_at) for r in same), default=0)
        try:
            items = fetch_quark_shares(QuarkTransfer(ck), since_ms=last_ms)
        except Exception as exc:  # noqa: BLE001 - 单账号失败不挡其它账号
            logger.warning("自有分享登记:%s 拉取失败(%s: %s)",
                           ref.label, type(exc).__name__, str(exc)[:120])
            stats["errors"] += 1
            continue
        stats["fetched"] += len(items)
        now = datetime.now()
        done = 0
        for it in items:
            row = book.get(it["pwd_id"])
            if row is None:
                row = PanOwnedShare(user_id=user_id, platform="quark",
                                    pwd_id=it["pwd_id"])
                session.add(row)
                book[it["pwd_id"]] = row
                stats["added"] += 1
            else:
                stats["updated"] += 1
            row.account_key = str(ref.cookie_key)
            row.share_id = it["share_id"] or row.share_id
            row.url = it["url"] or row.url
            row.title = it["title"] or row.title
            row.first_fid = it["first_fid"] or row.first_fid
            row.status = it["status"]
            row.created_at = it["created_at"] or row.created_at
            row.refreshed_at = now
            done += 1
            if done % _COMMIT_EVERY == 0:
                session.commit()
        session.commit()
        logger.info("自有分享登记:%s 拉到 %d 条(累计新 %d / 更新 %d)",
                    ref.label, len(items), stats["added"], stats["updated"])
    return stats


def record_owned(session: Session, user_id: int, url_or_id: str, *,
                 account_key: str = "", title: str = "", commit: bool = True) -> bool:
    """补登记一条**已确认属于我们**的分享(脚本/手工用;日常由 `refresh` 全量并入)。

    幂等:同一 `pwd_id` 已存在就只刷新 `refreshed_at`。返回是否落库。
    """
    from app.db.models import PanOwnedShare

    pwd = pwd_id_of(url_or_id)
    if not pwd:
        return False
    try:
        row = session.scalar(select(PanOwnedShare).where(
            PanOwnedShare.user_id == user_id,
            PanOwnedShare.platform == "quark",
            PanOwnedShare.pwd_id == pwd,
        ))
        if row is None:
            row = PanOwnedShare(user_id=user_id, platform="quark", pwd_id=pwd,
                                url=normalize_url(pwd), title=(title or "")[:500],
                                account_key=account_key, refreshed_at=datetime.now())
            session.add(row)
        else:
            row.refreshed_at = datetime.now()
            if account_key:
                row.account_key = account_key
            if title:
                row.title = title[:500]
        if commit:
            session.commit()
        return True
    except Exception:  # noqa: BLE001 - 登记失败不该挡转存/推送
        logger.warning("自有分享实时登记失败:%s", pwd, exc_info=True)
        return False


def owned_urls(session: Session, user_id: int = 1, platform: str = "quark") -> set[str]:
    """该用户**全部我方自有分享链**(规范化 URL 集合)。查询失败返回空集(不挡推送)。"""
    from app.db.models import PanOwnedShare

    try:
        rows = session.scalars(select(PanOwnedShare.url).where(
            PanOwnedShare.user_id == user_id,
            PanOwnedShare.platform == platform,
            PanOwnedShare.url != "",
        )).all()
        return {str(x) for x in rows if x}
    except Exception:  # noqa: BLE001 - 判别失败就当没有(照常推),别挡推送
        logger.debug("读取自有分享登记失败,按没有处理", exc_info=True)
        return set()


def refresh_tick() -> None:
    """调度入口:每天 04:10 增量刷新;首次自动全量。"""
    from app.db.database import get_session_local
    from app.services.tenant_base import _record_run
    from config.settings import get_settings

    settings = get_settings()
    if not getattr(settings, "pan_owned_shares_enabled", True):
        return
    db = get_session_local()()
    try:
        stats = refresh(db, 1)
        detail = (f"accounts={stats['accounts']} fetched={stats['fetched']} "
                  f"new={stats['added']} upd={stats['updated']} err={stats['errors']}")
        _record_run(db, 1, "pan_owned_shares",
                    "success" if not stats["errors"] else "partial", detail)
        db.commit()
        logger.info("自有分享登记完成:%s", detail)
    except Exception as exc:  # noqa: BLE001 - 定时作业不能把调度器带崩
        logger.exception("自有分享登记失败")
        try:
            db.rollback()
            _record_run(db, 1, "pan_owned_shares", "failed", str(exc)[:300])
            db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()
