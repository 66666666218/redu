"""抖音推广线索(2026-10-02,用户提供的判据)。

**要解决的问题**:抖音上的网盘推广号,标题里会多出一段**与视频内容无关**的文字——
常见形式是**书名号包裹**,而 `《…》` 里就是**迅雷的分享口令**(用户口径,2026-10-02):
进迅雷搜索框搜「白泽的梦」「三岁分享」就能搜到资源。所以它不只是"这人像推广号"的信号,
**本身就是可用的资源入口**。
实测(拿"diplay车机互联"搜 13 条):

    ★《白泽的梦》diplay软件下载教程 支持安卓苹果车机互联…     ← 命中
    ★《三岁分享》#diplay车机互联#carplay 一个软件实现车机互联,不用盒子 ← 命中
      不用加盒子,一个车机软件就可以实现无线CarPlay              ← 未命中
      Diplay如何安装#用车小常识 #Carplay #比亚迪 #iphone        ← 未命中
      …(其余 9 条同样都是直接的内容描述)

**格式差异一眼可辨**:命中那 2 条以《…》开头,其余全是直接描述内容。这是用户给的判据。

**为什么只推线索、不自动收号**:本项目装的 MediaCrawler 是作者的**教学版**,账号信息被
刻意脱敏(昵称 `睡***着`、user id 是 sha256 截断不可逆、主页链接不采集),**拿不到是谁**;
而**视频链接是完整的**——推给运营点开就能看到作者,人工补最后一步即可。
(账号层能全自动的只有知乎/B站,见 `cross_accounts.py`。)

判据:`标题以 《…》 开头`。它比"内容里有网盘链"更早命中——因为抖音的链本来就不在公开层
(见 `doc/pan-promotion-channels.md` §七)。
"""
from __future__ import annotations

import re

from sqlalchemy import select

from app.utils import get_logger

logger = get_logger(__name__)

# 标题里所有《…》(不限位置)——口令可能出现在开头(`《白泽的梦》diplay…`),
# 也可能嵌在句中(`苹果安卓手车互联更新《玩车不求人》新版本`)。
_ANY_BRACKET_RE = re.compile(r"《([^》]{1,20})》")
# 标题**以**《…》开头 —— 最强的信号(分享者把自己的品牌名顶在最前面)。
_LEAD_RE = re.compile(r"^《([^》]{1,20})》\s*")


def _matches_nickname(mark: str, masked_name: str) -> bool:
    """《》里的名字是否就是**本账号自己**(用脱敏昵称校验)。

    MediaCrawler 的脱敏规则是"首尾各留 1 字、中间打星"(`玩车不求人` → `玩***人`),
    所以**首尾字都对得上**就足以认定《》里写的是这个号的品牌名 —— 那是口令。
    反之 `My Dearest` vs `汶***汝` 对不上,说明它只是视频内容(剧名),不该收。
    """
    if not mark or not masked_name or "*" not in masked_name:
        return False
    return mark[0] == masked_name[0] and mark[-1] == masked_name[-1]


def _lead_mark(text: str, masked_name: str = "") -> str:
    """从标题里取出**口令候选**;没有《…》则返回空串。

    ⚠️ **不要用"《》内容 = 账号名"来判定** —— 用户 2026-10-02 明确指出:
    "账号名称跟关键词并没有特殊关联,只是这个凑巧了"。也就是说 `《玩车不求人》`
    碰巧等于昵称,但**一般情况两者无关**,拿昵称做判据会漏。

    所以这里只做一件事:**把标题里的《…》摘出来当候选**。
    排序用的优先级(不影响"收不收",只影响先后):
      ① 标题**以**《…》开头 —— 分享者把品牌名顶在最前;
      ② 《》内容与**本账号脱敏昵称首尾吻合** —— 碰巧同名时的高置信信号;
      ③ 其余(《》嵌在句中的)。
    最终"是不是真口令"由人看 —— 线索本来就是推给人判断的。
    """
    m = _LEAD_RE.match(text)
    if m:
        return m.group(1)[:20]
    for cand in _ANY_BRACKET_RE.findall(text):
        return cand[:20]          # 不限位置:句中也可能藏口令
    return ""


def _lead_rank(text: str, masked_name: str) -> int:
    """线索排序优先级(越小越靠前):开头《》 0 / 与昵称吻合 1 / 其它 2。"""
    if _LEAD_RE.match(text):
        return 0
    for cand in _ANY_BRACKET_RE.findall(text):
        if _matches_nickname(cand, masked_name):
            return 1
    return 2


def find_leads(keywords: list[str], limit: int = 30) -> list[dict]:
    """搜抖音 → 挑出标题带 `《…》` 前缀的推广线索。

    返回 `[{mark, title, url, keyword}]`;`mark` 是《》里那段 —— **迅雷分享口令**(可直接去迅雷搜)。
    ⚠️ 会**开浏览器**(MediaCrawler),一次几分钟 —— 只该低频跑。

    关键词**一次性全给** MediaCrawler(它的 CLI 吃整个列表,逐词调用等于反复开关浏览器)。
    """
    from app.services import mediacrawler_source as mc

    ok, why = mc.available()
    if not ok:
        logger.info("抖音线索:MediaCrawler 不可用(%s)", why)
        return []
    if not keywords:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for h in mc.crawl("douyin", keywords):
        text = (h.get("snippet") or "").strip()
        url = (h.get("url") or "").strip()
        if not url or url in seen:
            continue          # 拿不到视频链 / 同一个视频(多词命中)去重
        name = h.get("name") or ""
        mark = _lead_mark(text, name)
        if not mark:
            continue          # 标题里没有《…》 → 不是线索
        seen.add(url)
        out.append({"mark": mark, "title": text[:120], "url": url,
                    "keyword": h.get("keyword", ""),
                    "_rank": _lead_rank(text, name)})
    out.sort(key=lambda x: x["_rank"])      # 强信号排前面(开头《》> 与昵称吻合 > 其它)
    for x in out:
        x.pop("_rank", None)
    return out[:limit]


def apply_kouling(leads: list[dict], session, user_id: int, settings) -> list[dict]:
    """把线索里《…》包的口令**真的变成资源**(2026-10-02):解析 → 转存入库 / 加群。

    **为什么默认开**:此前这条链断在"口令 → 分享 id",线索只能推给人、由人去 App 里搜。
    现在 `xunlei_kouling` 把口令直接解成**网盘分享链**(可直接转存)或**群邀请**(加群后由
    群采集轮收),所以"抖音发现 → 加进网盘 → 转存"可以真的全自动。

    **两道闸门**:① 每轮最多真转存 `douyin_leads_transfer_limit` 条(转存慢且占盘),
    超出的**只解析**、把结果写进卡片由人决定;② 已经转存过的词(`xunlei_resources` 里
    `parent_name="口令解析"` 的那些)直接跳过,不重复搬。
    """
    from app.services import xunlei_kouling as kk

    if not getattr(settings, "douyin_leads_auto_transfer", True):
        for ld in leads:
            ld["kouling"] = {"kind": "off"}
        return leads
    budget = int(getattr(settings, "douyin_leads_transfer_limit", 3) or 0)
    already = kk.known_koulings(session, user_id)
    for ld in leads:
        mark = (ld.get("mark") or "").strip()
        if not mark:
            continue
        if mark in already:
            ld["kouling"] = {"kind": "share", "status": "already"}
            continue
        info = kk.resolve(mark)
        if info["kind"] == kk.KIND_NONE:
            ld["kouling"] = {"kind": "none"}
            continue
        if info["kind"] == kk.KIND_GROUP:
            res = kk.ingest(session, user_id, mark)
            ld["kouling"] = {"kind": "group", "status": res.get("status"),
                             "group_id": info["group_id"]}
            continue
        if budget <= 0:
            ld["kouling"] = {"kind": "share", "status": "over_budget",
                             "share_url": info["share_url"]}
            continue
        budget -= 1
        res = kk.ingest(session, user_id, mark)
        ld["kouling"] = {"kind": "share", "status": res.get("status"),
                         "our_url": res.get("our_url") or "",
                         "message": res.get("message") or ""}
    return leads


def _kouling_line(ld: dict) -> str:
    """把解析/转存结果渲染成卡片上的一行(让运营一眼看出这条线索值不值钱)。"""
    info = ld.get("kouling") or {}
    kind, status = info.get("kind"), info.get("status")
    if kind == "share" and status == "ok":
        return f"✅ **已自动转存**,我方链:{info.get('our_url') or ''}"
    if kind == "share" and status == "already":
        return "✅ 之前已转存过"
    if kind == "share" and status == "over_budget":
        return f"⏸ 本轮转存额度用完,未搬(原链 {info.get('share_url') or ''})"
    if kind == "share" and status == "failed":
        return f"⚠️ 转存失败:{info.get('message') or ''}"
    if kind == "group":
        return "👥 指向**群组**,已加群(群里的资源由群采集自动收)"
    if kind == "none":
        return "· 未解析出资源(可能只是剧名/普通词)"
    if kind == "off":
        return "· 自动转存已关闭,仅作线索"
    return ""


def push_leads(leads: list[dict], settings) -> bool:
    """把线索推飞书。

    **推管理员群**(未配则回落总群):这是"谁在发资源"的运营线索、给运营自己看的,
    不是给客户的内容素材 —— 与告警同属内部信息。
    """
    if not leads:
        return False
    webhook = (getattr(settings, "feishu_webhook_admin", "") or
               getattr(settings, "feishu_webhook", ""))
    if not webhook:
        logger.info("抖音线索:未配飞书 webhook,跳过推送")
        return False

    from app.services.feishu_client import FeishuClient

    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content":
        f"抖音上标题带《…》前缀的推广视频 **{len(leads)}** 条。\n"
        "《…》里就是**迅雷口令** —— 已自动解析:能解的**已转存进你的盘**并生成我方分享链,"
        "指向群组的已加群;点视频链接能看到作者(账号被工具脱敏,需人工确认)。"}}]
    for ld in leads:
        line = _kouling_line(ld)
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"**《{ld['mark']}》**\n{ld['title']}\n"
            + (f"{line}\n" if line else "")
            + f"[▶ 打开视频]({ld['url']})"}})
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "purple", "title": {"tag": "plain_text",
                                                   "content": f"🎯 抖音推广线索 · {len(leads)} 条"}},
        "elements": elements,
    }
    try:
        return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(card)
    except Exception:  # noqa: BLE001 - 推送失败不该影响采集
        logger.exception("抖音线索推送失败")
        return False


def douyin_leads_tick(settings=None) -> int:
    """定时:按资源库的词搜抖音 → 推推广线索。返回线索条数。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User
    from app.services.cross_accounts import _keywords_from_library

    settings = settings or get_settings()
    if not getattr(settings, "douyin_leads_enabled", True):
        return 0
    top = int(getattr(settings, "douyin_leads_keywords", 3) or 3)
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                kws = _keywords_from_library(db, uid, top)
                if not kws:
                    continue
                leads = find_leads(kws)
                total += len(leads)
                if leads:
                    # 口令 → 资源(分享链直接转存入库 / 群则加群),结果一并写进卡片
                    apply_kouling(leads, db, uid, settings)
                    push_leads(leads, settings)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                logger.exception("抖音线索失败 user=%s", uid)
    finally:
        db.close()
    return total
