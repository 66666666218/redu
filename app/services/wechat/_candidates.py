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
    from app.db.models import User
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
    """一键收录候选为对标号(v2.6.0,用户审核流转:自动发现→人工确认→直接收录)。

    用候选的代表文章链接走 add_benchmark(与手动贴链同链路):能解析出
    biz(WeRSS 有该订阅)即可直接监听;解析不到时仍建号(锚点留着),
    并提示"还需在微信读书关注后导入"才能走 book_id 链路。
    返回 {"status", "benchmark_id", "nickname", "listenable", "hint"}。
    """
    from app.services import wechat_monitor as _wm

    cand = session.get(WechatCandidate, candidate_id)
    if cand is None or cand.user_id != user_id:
        return {"status": "not_found"}
    if not cand.url:
        return {"status": "no_url", "hint": "该候选缺文章链接(旧数据),请在「添加对标号」粘贴其文章链接"}
    try:
        row = _wm.add_benchmark(session, user_id, cand.url, nickname=cand.name,
                                note=f"候选收录({cand.term})", settings=settings)
    except ValueError:
        # 已存在(同名/同链接):按昵称找到并存为收录态
        from sqlalchemy import select as _sel

        from app.db.models import WechatBenchmark as _WB

        ex = session.scalar(_sel(_WB).where(_WB.user_id == user_id, _WB.nickname == cand.name))
        if ex is None:
            return {"status": "failed", "hint": "该链接已存在但昵称对不上,请人工核对"}
        cand.status = "imported"
        session.commit()
        return {"status": "ok", "benchmark_id": ex.id, "nickname": ex.nickname,
                "listenable": bool(ex.weread_book_id or ex.biz),
                "hint": "" if (ex.weread_book_id or ex.biz) else "还需在微信读书关注后导入,才能走书架监听"}
    cand.status = "imported"
    session.commit()
    from app.db.models import WechatBenchmark as _WB2

    row2 = session.get(_WB2, row["id"])
    listenable = bool(row2 and (row2.weread_book_id or row2.biz))
    return {"status": "ok", "benchmark_id": row["id"], "nickname": row["nickname"],
            "listenable": listenable,
            "hint": "" if listenable else "已建号,但暂无可监听标识——请在微信读书关注该号后点「从书架导入」补齐"}
