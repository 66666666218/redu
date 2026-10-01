"""候选对标号:标题实体/内容词挖掘、搜索发现、站内推送。"""

from app.utils import get_logger

logger = get_logger(__name__)
from app.services import wechat_monitor as _root  # 兼容 monkeypatch:可替换名经门面运行时查找

from app.db.models import (FeishuAlert, User, WechatArticle, WechatBenchmark, WechatCandidate,
                           WechatPanLink, WechatRewrite, WechatTrafficSample)

from app.services.feishu_client import is_quiet_hours

from app.services.sogou_weixin import search_articles as sogou_search_articles

from app.services.tenant_base import _base, _record_run

from config.settings import Settings, get_settings

from sqlalchemy import and_, delete, func, or_, select, update

from sqlalchemy.orm import Session

import re



_TERM_STOPWORDS = ("链接", "入口", "获取", "教程", "分享", "合集", "全套", "更新", "最新",
                   "直达", "自取", "完整版", "白嫖", "点击", "关注", "原文", "公众号", "爆火")
_PUNCT_RE = re.compile(r"[^\w]+")     # 标点→空格(分段用,\w 含中文/字母/数字)
_HAS_CJK = re.compile(r"[一-鿿]")     # 片段需含 ≥2 个汉字,滤掉纯数字/英文碎片
def _overlap(a: str, b: str) -> int:
    """两片段最长公共子串长度(去重叠冗余用,串长 ≤6,暴力可)。"""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best
_ENTITY_RE = re.compile(r"[《【](.*?)[》】]")
_IMPORT_MAX_TRIES = 3   # 自动收录失败(号建了但补不进 WeRSS 订阅)的重试上限,试满转 dismissed
def mine_title_entities(titles: list[str], top: int = 6) -> list[str]:
    """从标题的书名号《》/【】标记中提取实体名(游戏/测试/资料名),作为首选搜索词。

    引流号标题套路:实体名必然被标记突出(《乡村晋升录》《花少2》【附链接】),
    实体名即业务对象——比滑窗碎片精准得多,出现 1 次就值得搜。
    """
    from collections import Counter

    counter: Counter[str] = Counter()
    for raw in titles or []:
        for name in _ENTITY_RE.findall(str(raw or "")):
            name = name.strip(" -—|")
            if (2 <= len(name) <= 20 and len(_HAS_CJK.findall(name)) >= 1
                    and not any(stop in name for stop in _TERM_STOPWORDS)):
                counter[name] += 1
    return [name for name, _n in sorted(counter.items(), key=lambda x: -x[1])][:top]
def mine_title_terms(titles: list[str], top: int = 6) -> list[str]:
    """从已入库文章标题挖高频内容词(段内 4~6 字滑窗,剔除营销泛词),供候选发现当搜索词。

    同类引流号的标题套路高度一致("花少2人格测试""乡镇晋升录"等),滑窗频次
    天然聚出内容词。要点:① 按标点分段滑窗,不产生跨词碎片;② 片段须含
    ≥2 个汉字(滤"2026"类);③ 与已选片段重叠 ≥3 字的冗余片段剔除。
    """
    from collections import Counter

    counter: Counter[str] = Counter()
    for raw in titles or []:
        for seg in _PUNCT_RE.sub(" ", str(raw or "")).split():
            if len(seg) < 4:
                continue
            for size in (4, 5):
                for i in range(max(0, len(seg) - size + 1)):
                    frag = seg[i:i + size]
                    if any(stop in frag for stop in _TERM_STOPWORDS):
                        continue
                    if len(_HAS_CJK.findall(frag)) < 2:
                        continue
                    if frag[0].isdigit() or frag[-1].isdigit():
                        continue  # 数字粘边的窗口是跨界碎片(如"2人格测""测试20")
                    counter[frag] += 1
    ranked = [(frag, n) for frag, n in counter.items() if n >= 2]
    ranked.sort(key=lambda x: (-x[1], len(x[0])))  # 同频优先短词(4 字词粒度最稳,防跨界碎片占位)
    picked: list[str] = []
    for frag, _n in ranked:
        if any(frag in p or p in frag or _overlap(frag, p) >= 3 for p in picked):
            continue
        picked.append(frag)
        if len(picked) >= top:
            break
    return picked
def discover_candidates(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    """一轮候选对标号发现:标题画像词+配置词 → 搜狗搜文章 → 按公众号名去重入库 → 推飞书。

    与现有对标号同名、或已在候选表(new 态)的跳过;搜狗验证码连续拦截 2 词即收手。
    免费(搜狗免账号);候选需人工在微信读书内关注 + 书架导入后成为正式对标号。
    """
    settings = _base(settings)
    terms = [t.strip() for t in (settings.candidate_search_terms or "").split(",") if t.strip()]
    titles = session.scalars(select(WechatArticle.title).where(
        WechatArticle.user_id == user_id).order_by(WechatArticle.created_at.desc()).limit(200)).all()
    # 首选:标题标记实体(《游戏名》《测试名》,最精准);兜底:滑窗高频内容词
    for term in [*mine_title_entities(list(titles), top=settings.candidate_mine_terms),
                 *mine_title_terms(list(titles), top=settings.candidate_mine_terms)]:
        if term not in terms:
            terms.append(term)
    terms = terms[: settings.candidate_max_terms] or ["网盘资源"]

    known = set(session.scalars(select(WechatBenchmark.nickname).where(
        WechatBenchmark.user_id == user_id)).all())
    seen = set(session.scalars(select(WechatCandidate.name).where(
        WechatCandidate.user_id == user_id, WechatCandidate.status == "new")).all())
    new_rows: list[WechatCandidate] = []
    blocked = 0
    for term in terms:
        res = _root.sogou_search_articles(term)
        if res["blocked"]:
            blocked += 1
            if blocked >= 2:
                logger.warning("搜狗验证码连续拦截,候选发现提前收手(用户 %s)", user_id)
                break
            continue
        for it in res["items"]:
            name = it["name"]
            if not name or name in known or name in seen:
                continue
            seen.add(name)
            new_rows.append(WechatCandidate(user_id=user_id, name=name, title=it["title"],
                                            url=str(it.get("url") or "")[:600],
                                            title_ts=it.get("published_at"), term=term[:64]))
    if new_rows:
        session.add_all(new_rows)
        session.commit()
        # LLM 评级:判定"资源号/营销号/无关"+优先级,写入候选 note 供人工参考
        if settings.deepseek_api_key:
            try:
                from app.services.llm_client import rank_candidates
                payload = [{"name": c.name, "title": c.title} for c in new_rows]
                ranked = rank_candidates(settings.deepseek_base_url, settings.deepseek_api_key,
                                         settings.deepseek_model, payload[:15])
                if ranked:
                    by_name = {r["name"]: r for r in ranked}
                    for c in new_rows:
                        r = by_name.get(c.name)
                        if r:
                            c.term = (f"{c.term}|LLM:{r['verdict']}({r['priority']})")[:64]
                    session.commit()
                    kept = sum(1 for r in ranked if r.get("verdict") == "资源号")
                    # 资源号排前,并按优先级排序 → 推送时用户先看到值得关注的
                    new_rows.sort(key=lambda c: (
                        0 if "资源号" in (c.term or "") and "高" in (c.term or "") else
                        1 if "资源号" in (c.term or "") else 2, c.id))
                    logger.info("LLM 候选评级:%d/%d 判定为资源号", kept, len(ranked))
            except Exception:  # noqa: BLE001 - 评级失败不影响候选入库
                logger.exception("LLM 候选评级失败")
        _push_candidates(session, user_id, settings, new_rows)
    _record_run(session, user_id, "wechat_candidates", "success",
                f"terms={len(terms)} new={len(new_rows)} blocked={blocked}")
    session.commit()
    return {"platform": "wechat", "status": "success", "terms": terms,
            "new": len(new_rows), "blocked": blocked}
def _push_candidates(session: Session, user_id: int, settings: Settings,
                     rows: list[WechatCandidate]) -> None:
    """候选清单推公众号专属飞书群(column_set 网格卡片:公众号/代表文章/来源词 三列对齐)。"""
    from app.services.feishu import _col_set_row, _md_safe, webhook_for
    from app.services.feishu_client import FeishuClient

    wh = webhook_for(settings, "wechat")
    main_wh = settings.feishu_webhook
    targets = list(dict.fromkeys(filter(None, [wh, main_wh])))  # 去重保序
    if not targets:
        return
    # 免打扰时段(默认 23~8 点):候选号没有盘链/阅读量概念(那两字段属于文章),
    # 不存在"紧急",一律延后到白天推——此前误用了文章的 pan_types/read_num 过滤,
    # 直接 AttributeError 炸掉整轮监听的候选推送(2026-09-14 实测)。
    if _root.is_quiet_hours(settings):
        logger.info("免打扰时段,延迟推送 %d 条候选号", len(rows))
        return
    elements: list[dict] = [
        {"tag": "note", "elements": [{"tag": "plain_text",
            "content": "手机微信读书搜索关注该号 → 监听页「从微信读书书架导入」即自动进监听"}]},
        _col_set_row([("**公众号**", 3), ("**代表文章**", 7), ("**来源词**", 2)], grey=True),
    ]
    for r in rows[:20]:
        title = _md_safe(r.title)
        ts = r.title_ts.strftime("%m-%d") if r.title_ts else ""
        shown = title[:30] + ("…" if len(title) > 30 else "")
        llm_tag = ""
        if "LLM:资源号(高)" in (r.term or ""):
            llm_tag = "🔴高优先 "
        elif "资源号" in (r.term or ""):
            llm_tag = "🟢资源号 "
        elements.append(_col_set_row([
            ((llm_tag + _md_safe(r.name))[:14] or "—", 3),
            (f"《{shown}》{f' ({ts})' if ts else ''}", 7),
            (_md_safe(r.term)[:8] or "—", 2),
        ]))
    if len(rows) > 20:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text",
            "content": f"…另有 {len(rows) - 20} 个,见平台候选列表"}]})
    for target in targets:  # 主群 + 专属群都推(此前只发 wh,主群永远收不到候选)
        try:
            FeishuClient(target, settings.feishu_secret).send_card({
                "config": {"wide_screen_mode": True},
                "header": {"template": "blue", "title": {"tag": "plain_text",
                    "content": f"🔍 候选对标号 · 新发现 {len(rows)} 个"}},
                "elements": elements,
            })
        except Exception:  # noqa: BLE001 - 推送失败不影响采集结果
            logger.exception("候选对标号飞书推送失败 user=%s", user_id)
def candidate_discover_tick(settings: Settings | None = None) -> int:
    """每日定时:为所有(有对标号的)用户发现一轮同类候选号。返回新增候选数。"""
    from app.db import get_session_local
    from sqlalchemy import func as sa_func

    settings = settings or get_settings()
    db = get_session_local()()
    total = 0
    try:
        users = db.scalars(select(User.id).where(User.enabled.is_(True)).order_by(User.id)).all()
        for uid in users:
            has_bm = db.scalar(select(sa_func.count()).select_from(WechatBenchmark).where(
                WechatBenchmark.user_id == uid, WechatBenchmark.active.is_(True)))
            if not has_bm:
                continue
            try:
                out = discover_candidates(db, uid, settings=settings)
                total += out.get("new", 0)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("候选对标号发现失败 user=%s", uid)
    finally:
        db.close()
    if total:
        logger.info("候选对标号发现完成:新增 %d 个", total)
    return total
def list_candidates(session: Session, user_id: int) -> list[dict]:
    """候选列表(新→旧);`imported`=该名已是正式对标号(书架导入后自然闭环)。"""
    rows = session.scalars(select(WechatCandidate).where(
        WechatCandidate.user_id == user_id).order_by(WechatCandidate.id.desc()).limit(200)).all()
    known = set(session.scalars(select(WechatBenchmark.nickname).where(
        WechatBenchmark.user_id == user_id)).all())
    return [{"id": r.id, "name": r.name, "title": r.title, "term": r.term,
             "status": r.status, "imported": r.name in known,
             "title_ts": r.title_ts.isoformat(sep=" ", timespec="seconds") if r.title_ts else None,
             "discovered_at": r.discovered_at.isoformat(sep=" ", timespec="seconds")}
            for r in rows]
def set_candidate_status(session: Session, user_id: int, candidate_id: int, status: str) -> None:
    """更新候选状态(仅 new/dismissed);dismissed 后不再进入去重表,允许未来重新发现。"""
    if status not in ("new", "dismissed"):
        raise ValueError("非法状态")
    row = session.scalar(select(WechatCandidate).where(
        WechatCandidate.user_id == user_id, WechatCandidate.id == candidate_id))
    if row is None:
        raise KeyError("候选不存在")
    row.status = status
    session.commit()


def import_candidate(session, user_id: int, candidate_id: int, settings=None) -> dict:
    """一键收录候选为对标号(自动发现→人工/自动审核→直接进监听)。

    两条路,按候选有没有文章链接分流:
    - **有链接**:走 `add_benchmark`(与手动贴链同链路),能从链接解出号即可监听。
    - **无链接**(搜狗候选的常态):走 `add_benchmark_by_name`——按号名补进 WeRSS 订阅池,
      拿回 `MP_WXS_*` 写进 `biz`,下一轮监听 ⓪ 分支就会开始抓它的文章。

    历史注:本函数 v1 只会走第一条路,而搜狗结果从来不存链接(见 `sogou_weixin`),
    于是"收录"按钮自上线起对每一条候选都返回 no_url——v2.13.0 补上的正是第二条路。
    返回 {"status", "benchmark_id", "nickname", "listenable", "created", "hint"}。
    """
    from app.services import wechat_monitor as _wm

    cand = session.get(WechatCandidate, candidate_id)
    if cand is None or cand.user_id != user_id:
        return {"status": "not_found"}
    note = f"候选收录({cand.term})"
    listenable, hint, created = False, "", False
    if cand.url:
        try:
            row = _wm.add_benchmark(session, user_id, cand.url, nickname=cand.name,
                                    note=note, settings=settings)
        except ValueError:
            # 链接已存在:按昵称找回那条记录当"已收录"处理(旧行为,保持兼容)
            from sqlalchemy import select as _sel

            from app.db.models import WechatBenchmark as _WB

            ex = session.scalar(_sel(_WB).where(_WB.user_id == user_id,
                                                _WB.nickname == cand.name))
            if ex is None:
                return {"status": "failed", "hint": "该文章链接已存在但昵称对不上,请人工核对"}
            row = {"id": ex.id, "nickname": ex.nickname}
        bid = int(row["id"])
        from app.db.models import WechatBenchmark as _WB2

        saved = session.get(_WB2, bid)
        listenable = bool(saved and (saved.weread_book_id or saved.biz))
        hint = "" if listenable else "已建号,但暂无可监听标识——请在微信读书关注该号后点「从书架导入」补齐"
    else:
        out = _wm.add_benchmark_by_name(session, user_id, cand.name, note=note, settings=settings)
        bid, listenable, hint, created = out["id"], out["listenable"], out["hint"], out["created"]
    if listenable:
        cand.status = "imported"
    else:
        # 没收进监听能力就一律算"还没成":可能是 WeRSS 里搜不到它(号名带后缀/已被封)或
        # 上游临时抽风。前者重试多少次都没用,后者值得再试——所以给有限次机会,试满就收手,
        # 免得这类候选每天霸占 `candidate_auto_import_max` 的名额(2026-10-01)。
        # 注意判据是 listenable 而非 created:重试第二轮时对标号可能已由第一轮建出来了,
        # 若按 created 判会被误当"已收录"而永远停在无 biz 的半成品状态。
        cand.import_tries = (cand.import_tries or 0) + 1
        if cand.import_tries >= _IMPORT_MAX_TRIES:
            cand.status = "dismissed"
    session.commit()
    return {"status": "ok", "benchmark_id": bid, "nickname": cand.name,
            "listenable": listenable, "created": created, "hint": hint}


def _term_of(cand: WechatCandidate) -> str:
    """候选的来源词,剥掉 `|LLM:资源号(高)` 这层评级后缀(入库时拼在 term 尾巴上)。"""
    return (cand.term or "").split("|")[0].strip()
def _resource_accounts(session: Session, user_id: int, term: str, days: int = 90) -> int:
    """该来源词对应的资源在库里的**验证强度**(发过它的对标号数);查不动按 0 算。

    用"号数"而不是"命中与否"当判据,是因为词本身会骗人:`mine_title_terms` 的滑窗
    挖出过"可保存"这种泛词(不在停用词表里),拿它去 `search_resources` 一搜一大片,
    但那是"标题里恰好有这三个字",不是"同一个资源被反复要过"。同链多号同发才是需求坐实。
    """
    if len(term) < 2:
        return 0
    try:
        from app.services.resource_library import search_resources

        hits = search_resources(session, user_id, term, days=days, limit=5)
    except Exception:  # noqa: BLE001 - 资源库查询失败不该挡住收录
        logger.exception("候选收录:资源库查询失败 term=%s", term)
        return 0
    return max((int(h.get("accounts") or 0) for h in hits), default=0)
def select_importable(session: Session, user_id: int, settings: Settings | None = None,
                      limit: int = 0) -> list[dict]:
    """挑出值得收录的候选(2026-10-01 定的标准):

    ① **LLM 判为资源号**(`term` 里带 `LLM:资源号(...)`),或
    ② **它发的资源已被 ≥`candidate_auto_import_min_accounts` 个对标号验证过**——这条
       是针对"LLM 漏判但确实是同类号"的兜底:资源在库里被反复发过,说明需求坐实,
       发它的号就有跟的价值。

    返回 `[{cand, reason, accounts}]`,排序:资源号(高)→ 资源号 → 资源库验证强 → 弱。
    已 imported/dismissed 的不再入选;`limit`>0 时截断。
    """
    settings = _base(settings)
    rows = session.scalars(select(WechatCandidate).where(
        WechatCandidate.user_id == user_id,
        WechatCandidate.status == "new").order_by(WechatCandidate.id.desc())).all()
    acc_cache: dict[str, int] = {}   # 多个候选常共用同一个来源词,查询结果直接复用
    picked: list[tuple[int, int, int, WechatCandidate, str]] = []
    for c in rows:
        term = _term_of(c)
        if "资源号" in (c.term or ""):
            tier = 0 if "高" in (c.term or "") else 1
            picked.append((tier, 0, -c.id, c, "资源号"))
            continue
        if term not in acc_cache:
            acc_cache[term] = _resource_accounts(session, user_id, term)
        acc = acc_cache[term]
        if acc >= settings.candidate_auto_import_min_accounts:
            picked.append((2, -acc, -c.id, c, f"资源库{acc}号验证"))
    picked.sort(key=lambda x: x[:3])
    out = [{"cand": c, "reason": why, "accounts": -neg} for _t, neg, _i, c, why in picked]
    return out[:limit] if limit > 0 else out
def auto_import_candidates(session: Session, user_id: int, settings: Settings | None = None,
                           limit: int = 0) -> dict:
    """自动收录一轮:按标准挑候选 → 补进 WeRSS 订阅 → 建对标号 → 监听下一轮自动接上。

    数量闸门(`limit`,默认取 `candidate_auto_import_max`)是**必须的**:`add_feed` 会让
    上游立刻排一次该号的历史文章抓取,而 WeRSS 本来就在限流边缘,一口气灌几百个号会把
    抓取队列压在别人(以及我们自己已订阅的 113 个号)前面。

    返回 {"picked", "imported", "listenable", "items"}。
    """
    settings = _base(settings)
    cap = int(limit or settings.candidate_auto_import_max or 0)
    picks = select_importable(session, user_id, settings)
    items: list[dict] = []
    for p in picks:
        if cap and len(items) >= cap:
            break
        c = p["cand"]
        try:
            out = import_candidate(session, user_id, c.id, settings=settings)
        except Exception as exc:  # noqa: BLE001 - 单个失败不影响整轮
            session.rollback()
            logger.exception("候选自动收录失败 id=%s name=%s", c.id, c.name)
            items.append({"id": c.id, "name": c.name, "status": "error",
                          "reason": p["reason"], "hint": str(exc)[:120]})
            continue
        items.append({"id": c.id, "name": c.name, "status": out.get("status"),
                      "reason": p["reason"], "listenable": bool(out.get("listenable")),
                      "hint": out.get("hint", "")})
    listenable = sum(1 for it in items if it.get("listenable"))
    _record_run(session, user_id, "wechat_candidate_import", "success",
                f"picked={len(picks)} done={len(items)} listenable={listenable}")
    session.commit()
    logger.info("候选自动收录:选中 %d,处理 %d,可监听 %d(用户 %s)",
                len(picks), len(items), listenable, user_id)
    return {"picked": len(picks), "imported": len(items),
            "listenable": listenable, "items": items}
def candidate_import_tick(settings: Settings | None = None) -> int:
    """每日定时:为所有(有对标号的)用户自动收录候选号。返回本轮收录成功的号数。"""
    from app.db import get_session_local
    from sqlalchemy import func as sa_func

    settings = settings or get_settings()
    if not settings.candidate_auto_import:
        return 0
    db = get_session_local()()
    total = 0
    try:
        users = db.scalars(select(User.id).where(User.enabled.is_(True)).order_by(User.id)).all()
        for uid in users:
            has_bm = db.scalar(select(sa_func.count()).select_from(WechatBenchmark).where(
                WechatBenchmark.user_id == uid, WechatBenchmark.active.is_(True)))
            if not has_bm:
                continue
            try:
                out = auto_import_candidates(db, uid, settings=settings)
                total += out.get("imported", 0)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("候选自动收录失败 user=%s", uid)
    finally:
        db.close()
    return total
