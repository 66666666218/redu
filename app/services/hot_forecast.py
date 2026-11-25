# -*- coding: utf-8 -*-
"""**爆款预判**:从三源热榜的形状 + 抖音趋势,提前一天判「明天会炸的**资源型**热点」(2026-11-24)。

## 用户口径与那条被量出来的差距

> 「我们监控的麦穗和羊**最早 15 号就看到了**,但 **17 号才开始监控到发网盘**的。
> 那我们能否根据这个**自己提前预判适合做网盘的热点**、自己写呢?**16 号发我们就会占很大先机**」

拿远程库 + 抖音热点宝逐条核对的事实(**不是估的**):

| 时刻 | 发生了什么 | 出处 |
|---|---|---|
| 11-15 13:05 | 贴吧「麦穗与羊原帖作者回应真实性」**#2** | 远程 `hot_source_items` |
| 11-15 15:05 | 贴吧「**《麦穗与羊》凄美爱情故事原文**」**#3** | 同上 |
| 11-15 15:35 | 百度「**文章《麦穗与羊》爆火 作者发声**」**#26**(17:11 升到 #6) | 远程 `baidu_hot_items` |
| **11-15 全天** | 抖音热点宝「麦穗与羊」**3092 万**,是自身基线的 **109 倍** | 热点宝 `hot_search` 日序列 |
| 11-17 00:38 | 我们才拿到第一条盘链 | `discovered_pan_links` |

⇒ **热榜侧在 11-15 下午就已经三源齐报,我们晚了 35 小时。**
11-16 08:49 跑一次这个判据,会**精确报出麦穗**(回放实测,卡片上写「单日 3093 万 / 87 倍基线」),
比第一张带链卡早 **40 小时** —— 正是用户要的那个"先机"。

## 为什么现有链路都抓不到(逐条对照过)

| 链路 | 判据 | 差在哪 |
|---|---|---|
| `hot_breakout` | 本机 `hot_source_items` + `《作品名》` | 本机那张表只镜像**每源最新一轮**(`remote_pull` 刻意不搬历史)⇒ 11-15 那批**根本不在本机**;且麦穗是**没书名号**的标题 |
| `promo_forecast` | 头 24h 攒够 **4 个推手** | 那是"同行**已经**入局",跟风信号 |
| `hotspot_agent` / `early_agent` | 热榜 → LLM 选题 | 只跑远程,且本机没有入口 |
| `hot_cycle.carrier_words` | 热点宝"形态词"(回应/官宣…) | 实测麦穗的形态词 **11-18 16:50** 才出现 —— 那时事件早已成型 |

## 判据(每一档都拿 11-15~11-23 的真实库回放过,不是拍的)

**① 资源形态 —— 三源标题里出现「有东西可交付」的词**

    原文|全文|完整版|全集|原稿|小说|文档|资料包|下载|漫画|短剧|剧集|剧本|PDF|连载|番外|大结局|完整视频|合集|文章

11-15 全天三源去重 753 条,命中 **11 条**(麦穗排 #1 和 #3)。
⚠️ 早先那版词表里还有 `资源/手写/情感/故事`,它们把 `BLG拿资源输团战`、`泛黄手写信`、
`马天宇承认不需要任何情感链接` 都拉了进来 —— **宽词是噪音源,已剔除**。

**② 抖音趋势确认 —— 它真的在起量,而不是一个一直在榜的品类词**

对候选的**作品名**去热点宝定向查(`hot_search(keyword=...)`,榜外词也能查),
取出「词 ≈ 作品名」的那一族,截到**昨天**(今天那一格是不完整的),判:

    昨天单日 ≥ 500 万  ∧  昨天 / 自身历史最低 ≥ 10 倍  ∧  ≥100 万的天数 ≥ 2

回放实测:11-16 麦穗 3093 万 / 30x / 5 天 ⇒ **当天唯一命中**;
11-17 麦穗 5741 万 / 56x;11-23 多出一个「双帝之战」15375 万 / 56x(**全是真作品**)。
「谷雨」2828 万却只有 **1 倍**基线 ⇒ 它是常驻短剧,不是"要炸" ⇒ 正确挡掉。
⚠️ **同类词取最优**:词族里 `麦穗与羊`/`麦穗与羊原视频`/`麦穗与羊原文` 都指向同一事件,
只留末值最大的那个;纯品类词(`小说/短剧/合集/…`)直接丢。

### ⚠️ 倍数阈值 30 → 10(2026-11-25 放宽,用户决定「观察几天」)

**上面那句「87x」是事后用完整序列算的,决策当时看不到。** 按线上口径
(只截到昨天)重放,麦穗 11-16 那一刻只有 **30.0 倍** —— 恰好贴着阈值边。
于是这个"锚点事件"在真实运行里其实是**勉强过线**,而 11-24~11-25 连续多轮
`趋势确认 0 ⇒ 卡内 0` 就是同一把尺子的另一面。

`scripts/backtest_hot_forecast.py` 把 10 天、44 个形态作品名逐个查热点宝重放:

| 阈值 | 命中 | 变化 |
|---|---|---|
| 30x(原) | 7/10 天 | — |
| 20x | 8/10 天 | +亚马尔澄清函(2932 万/24x) |
| **10x(现行)** | **8/10 天** | **+麦穗 11-16(3093 万/30.0x)** |
| 5x | 8/10 天 | 与 10x **逐字相同**,零新增噪音 |

⇒ 取 10x(**不是 5x**:两者结果一样,10x 更保守、留安全边际)。
**真正的护栏是 `min_last` 而不是倍数** —— 噪音候选离 10x 极远:
`丑狐` 1.3x、`我的有罪之人` 1.7x、`谷雨` 1.0x、`吧友锐评GTA6` 1.1x;
其余靠别的条件挡(「纸质」850x 但有量天 0;「第二十八年春」12.9x 但末值仅 160 万)。
⇒ **再放宽倍数没有收益,要提召回只能动 `min_last` 或形态词表**,那必须重跑回测。

**③ AI 判「值不值得做网盘」**

`llm_client.judge_value` 是现成的尺子(系统提示词就是「资源发的多、转存人数多」)。
实测 8 条里判对了 6 条:
`大结局删改版全集` 8 分 · `爱情故事原文` 7 分 · `行业话题` 4 分 · `数据新闻` 3 分 · `剧本争议` 3 分。
⇒ 低于 `hot_forecast_min_score`(默认 6)的不报。

**④ 我们还没动手**

复用 `hot_breakout` 的语料口径(台账 + 公众号,`peer_hours` 窗口,按归一标题裸包含):
已有我方链 ⇒ 不报;库内已有同行在推 ⇒ 不报(那是跟风,不是先手)。

**为什么不用「整条标题跨源 ≥2 平台」**:见 `hot_breakout` 头注的反例 ——
各平台标题措辞完全不同(`《麦穗与羊》凄美爱情故事原文` vs `文章《麦穗与羊》爆火 作者发声`),
**只有作品名是一致的**。按整条标题聚类会**同时漏掉麦穗和斗破**。

## 产出:预警卡 + **备好物料**(用户 2026-11-24 拍板「两者都要」)

卡片进**管理群**(运营情报,不进客户群),逐条带上:
量级 / 倍数 / 热榜名次 / AI 分数与理由 / **库内同行数** / 物料路径。
物料两份:
  · **PDF 资料** —— `gossip_material.gather` 采真实素材 → `compose_gossip_doc` → `gossip_doc.render_pdf`;
  · **公众号推文稿** —— `llm_client.draft_article`(标题 ×3 + 正文 + 回复关键词)。

⚠️ **本轮绝不碰上传**:夸克两个号一个 99.99999%、一个 163.8%,`data/` 是本机落点。
   `scripts/gossip_publish.py`(上传+建链+发群)**不在这里调用**。
⚠️ 素材为空 ⇒ **拒绝生成**(`compose_gossip_doc` 的底线:不许模型凭记忆编)。

## 纪律(与 `hot_breakout` / `promo_forecast` 同)

· **慢活之前不抱 SQLite 写事务**:远程查询、热点宝、LLM、素材采集全在**本机 session 之外**
  (本仓在 `resource_presence` 上栽过:抱着写锁跑 181 秒,别的作业全 `database is locked`)。
· **发送成功之后才落冷却行**(pysqlite 上先占位再发,一次抖动就吃掉整条提醒)。
· **每一级漏斗都进运行记录**,否则「今天没预判」与「作业看不见数据」在库里长得一样。
· **热点宝不可达时降级不静默**:只用「形态 + 名次 ≤ 10」并**在卡上写明趋势未确认**。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from app.utils import get_logger

logger = get_logger(__name__)

#: **资源形态词** —— 出现它说明「这条热点里有一份东西可以交付」。
#: ⚠️ 宽词(`资源/手写/情感/故事`)已剔除,回放噪音实测见模块头注 ①。
FORM_RE = re.compile(
    r"(原文|全文|完整版|全集|原稿|小说|文档|资料包|下载|漫画|短剧|剧集|剧本|"
    r"PDF|连载|番外|大结局|完整视频|合集|文章)")

_BRACKET_RE = re.compile(r"《([^》]{2,30})》")

#: 纯品类词 —— 热点宝里 `小说`/`短剧`/`合集` 这种词天天有量,和作品名不是一回事。
GENRE_STOP: frozenset[str] = frozenset({
    "小说", "短剧", "漫画", "剧集", "合集", "影片", "电影", "资料", "资源", "原文",
    "全文", "完整版", "全集", "剧本", "文档", "下载", "番外", "大结局", "视频", "文章",
})

#: 去掉标题里的动作/前缀词,只为把「作品名」抽干净(`文章《X》…` 的 `文章`)
_LEAD_RE = re.compile(r"^(文章|视频|网传|曝|热传|分享|盘点)\s*[:：]?\s*")
_PUNCT_RE = re.compile(r"[\s\u3000:：,，。!！?？\"'“”‘’、·—\-|/]+")


def _seed_of(title: str) -> str:
    """一条热榜标题 → **它说的那个东西的名字**。

    优先取最后一个 `《…》`(中文标题里主作品在后面,与 `hot_breakout._primary_name` 同一条理由);
    没有书名号就剥掉形态词/前缀/标点 —— `亚马尔澄清函全文` → `亚马尔澄清函`,
    `双帝之战大结局删改` → `双帝之战删改`(热点宝里能查到 `双帝之战`)。
    """
    names = _BRACKET_RE.findall(str(title or ""))
    if names:
        return names[-1].strip()
    t = FORM_RE.sub("", str(title or ""))
    t = _LEAD_RE.sub("", t)
    return _PUNCT_RE.sub("", t)[:24]


def _cover(word: str, seed: str) -> float:
    """`word` 是不是在说 `seed` 这个东西(1.0 = 是,0.0 = 不是)。

    **双向包含都算**:`麦穗与羊` ⊂ `《麦穗与羊》原稿传递`;反过来
    `麦穗与羊原视频`(词)也含 `麦穗与羊`(seed)—— 那正是同一事件的另一种说法。
    ⚠️ 不能用「二元组覆盖率」或低阈值 LCS:`音乐合集` 与 `GTA6BUG合集` 能蹭到 0.5,
    而 `麦穗与羊辟谣` 与 `《辟谣箴言》系列漫画` 也能蹭到 0.33 —— 都是不同的东西。
    """
    w, s = str(word or "").strip(), str(seed or "").strip()
    if not w or not s:
        return 0.0
    if w in s or s in w:
        return 1.0
    return 0.0


# ---------------------------------------------------------------- 三个数据源

def remote_candidates(settings, since: datetime, until: datetime) -> list[dict]:
    """远程三源(`百度/微博/贴吧`)**按标题去重**后的全部条目(只读,不碰本机库)。

    返回 `[{title, srcs, rank, first}]`。

    ⚠️ 为什么**直连远程**而不是读本机:本机 `baidu_hot_items` **0 行**、`weibo_hot_items`
    停在 11-06(52 行),而 `hot_source_items` 只有每源最新一轮(`remote_pull` 刻意不搬历史,
    否则每轮搬运量从 1.3k 涨到 33k)。**11-15 那批只存在于远程库。**
    ⚠️ 远程同一份榜单在多个 `user_id` 下都有(实测贴吧 5 个 user 各 1043 行),
    所以 SQL 里 `GROUP BY title` 去重,否则同一事件会被数成 5 条。
    ⚠️ `rank` 是 MySQL 保留字,**必须反引号**。
    ⚠️ 引擎用完要 `dispose()` **并关掉 SSH 通道**(照 `remote_pull._remote_query`)。
    """
    from sqlalchemy import text

    from app.services.remote_sync import _engine

    sql = text(
        "SELECT title, GROUP_CONCAT(DISTINCT src) AS srcs, MIN(rk) AS rk, MIN(t) AS t "
        "FROM ("
        "  SELECT title, '百度' AS src, `rank` AS rk, captured_at AS t FROM baidu_hot_items "
        "   WHERE captured_at BETWEEN :a AND :b"
        "  UNION ALL "
        "  SELECT title, '微博' AS src, `rank` AS rk, captured_at AS t FROM weibo_hot_items "
        "   WHERE captured_at BETWEEN :a AND :b"
        "  UNION ALL "
        "  SELECT title, '贴吧' AS src, `rank` AS rk, captured_at AS t FROM hot_source_items "
        "   WHERE source = 'tieba' AND captured_at BETWEEN :a AND :b"
        ") x GROUP BY title")
    eng = _engine("", settings)
    try:
        with eng.connect() as conn:
            rows = conn.execute(sql, {"a": since, "b": until}).all()
    finally:
        eng.dispose()
        tr = getattr(eng, "_rs_transport", None)
        if tr is not None:
            try:
                tr.close()
            except Exception:  # noqa: BLE001 - 关通道失败不影响本轮结果
                pass
    out: list[dict] = []
    for title, srcs, rk, t in rows:
        out.append({"title": str(title or ""), "srcs": str(srcs or ""),
                    "rank": int(rk or 0), "first": t})
    return out


def douhot_confirm(cookie: str, settings, seed: str, cutoff: str) -> dict | None:
    """热点宝定向查 `seed` → **确认它正在起量**;不可达/没确认返回 None。

    `cutoff` = `YYYYMMDD`,序列只取到这天(**今天那格不完整,不能用来判断**)。
    """
    from app.services.douhot_client import DouhotClient, DouhotError

    min_last = int(getattr(settings, "hot_forecast_min_last", 5_000_000) or 5_000_000)
    min_ratio = float(getattr(settings, "hot_forecast_min_ratio", 30.0) or 30.0)
    min_points = int(getattr(settings, "hot_forecast_min_points", 2) or 2)
    try:
        raw = DouhotClient(cookie, settings).hot_search(
            limit=20, date_window=24, sub_type=3001, keyword=seed)
    except DouhotError as exc:
        logger.warning("爆款预判:热点宝查询失败 seed=%s: %s", seed, exc)
        raise
    best = None
    for it in raw or []:
        word = str(it.get("key_word") or "").strip()
        if not word or word.strip("#") in GENRE_STOP:
            continue
        if _cover(word, seed) < 0.99:
            continue
        vals = [int(p["value"]) for p in it.get("trends") or []
                if str(p.get("date") or "") <= cutoff and p.get("value") is not None]
        if len(vals) < 4:
            continue
        last, low = vals[-1], min(vals)
        if last < min_last:
            continue
        ratio = (last / low) if low > 0 else 0.0
        n_big = sum(1 for v in vals if v >= 1_000_000)
        ok = ratio >= min_ratio and n_big >= min_points
        cand = {"word": word, "last": last, "ratio": round(ratio, 1),
                "days": len(vals), "points": n_big, "ok": ok}
        if best is None or (cand["ok"], cand["last"]) > (best["ok"], best["last"]):
            best = cand
    return best if (best and best["ok"]) else None


def judge_values(settings, items: list[dict]) -> dict[str, dict]:
    """AI 判「值不值得做网盘」→ `{seed: {score, why}}`(失败返回空 dict,不挡发卡)。"""
    from app.services import llm_client

    api_key = str(getattr(settings, "deepseek_api_key", "") or "")
    if not api_key or not items:
        return {}
    payload = [{"i": i, "text": f"{it['seed']} | {it['title'][:60]} | {'/'.join(it['srcs'])}"}
               for i, it in enumerate(items)]
    got = llm_client.judge_value(
        str(getattr(settings, "deepseek_base_url", "https://api.deepseek.com")), api_key,
        str(getattr(settings, "deepseek_model", "deepseek-chat")),
        "网盘热点预判", payload, timeout=int(getattr(settings, "ai_filter_timeout", 45) or 45),
        extra="判断标准:这条热点能不能变成一份**别人愿意转存的资源**"
              "(原文/全文/全集/完结合集/资料文档)。纯资讯、行业数据、人物八卦 = 低分。")
    if not got:
        return {}
    out: dict[str, dict] = {}
    for d in got:
        i = int(d.get("i", -1))
        if 0 <= i < len(items):
            out[items[i]["seed"]] = {"score": int(d.get("score") or 0),
                                     "why": str(d.get("why") or "")[:60]}
    return out


# ---------------------------------------------------------------- 主判据

def detect(settings, *, now: datetime | None = None, cutoff: str = "",
           candidates: list[dict] | None = None,
           confirms: dict[str, dict | None] | None = None,
           scores: dict[str, dict] | None = None,
           degraded: bool | None = None,
           stats: dict | None = None) -> list[dict]:
    """找出「明天会炸、且我们还没动手」的资源型热点(纯读 + 出网;**不碰本机库**)。

    每个可注入参数都是给测试/回放用的:`candidates` 传三源原始条目、
    `confirms` 传热点宝确认结果、`scores` 传 AI 分值、`degraded` 强制降级档;
    传了就**完全不触网**。

    返回 `[{seed, title, srcs, rank, douhot, score, why}]`,按 (AI 分, 单日量) 降序。
    ⚠️ **不在这里截 top** —— 调用方要先剔「已有我方链 / 同行已入局」再截,
    否则前 3 条都被人推过时,第 4 条本来干净的也会被一起丢掉。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    now = now or datetime.now()
    lookback = float(getattr(settings, "hot_forecast_lookback_hours", 24) or 24)
    #: ⚠️ **今天那一格不完整**(当天还在累计)⇒ 趋势只判到**昨天**为止,
    #: 否则 08 点跑出来的是"半天数据",倍数整条失真。
    cutoff = cutoff or (now - timedelta(days=1)).strftime("%Y%m%d")
    used: dict = {}

    if candidates is None:
        candidates = remote_candidates(settings, now - timedelta(hours=lookback), now)
    used["rows"] = len(candidates)

    #: ① 资源形态 → 按 seed 归并(同一事件的两种说法只留一条,名次取最好的)
    groups: dict[str, dict] = {}
    n_form = 0
    for c in candidates:
        title = str(c.get("title") or "")
        if not FORM_RE.search(title):
            continue
        n_form += 1
        seed = _seed_of(title)
        if not seed or seed in GENRE_STOP:
            continue
        g = groups.get(seed)
        if g is None:
            groups[seed] = g = {"seed": seed, "title": title,
                                "srcs": {s for s in str(c.get("srcs") or "").split(",") if s},
                                "rank": int(c.get("rank") or 0)}
        else:
            g["srcs"] |= {s for s in str(c.get("srcs") or "").split(",") if s}
            if int(c.get("rank") or 0) and (not g["rank"] or int(c["rank"]) < g["rank"]):
                g.update({"rank": int(c["rank"]), "title": title})
    used["form"] = n_form
    used["seeds"] = len(groups)

    #: ② 抖音趋势确认(热点宝不可达 ⇒ 降级成「形态 + 名次 ≤10」并在卡上写明)
    if confirms is None:
        from app.services.hot_cycle import read_cookie

        cookie = read_cookie(settings)
        confirms = {}
        down = ""
        if cookie:
            for seed in list(groups):
                try:
                    confirms[seed] = douhot_confirm(cookie, settings, seed, cutoff)
                except Exception as exc:  # noqa: BLE001 - 单条失败不挡其余
                    logger.warning("爆款预判:热点宝不可达(%s)", type(exc).__name__)
                    down = type(exc).__name__
                    confirms[seed] = None
                    break
        else:
            down = "未配置热点宝 Cookie"
        used["douhot_down"] = down
    else:
        used.setdefault("douhot_down", "")
    used["douhot_ok"] = sum(1 for v in confirms.values() if v)
    #: **降级档** = 热点宝整条路不可用(Cookie 没配/查询全挂)。
    #: 此时不再要求趋势确认,只收「已经很靠前」的,并在卡上写明「趋势未确认」。
    #: ⚠️ 单条查得到但没达标(如「谷雨」1 倍基线)**不算降级** —— 那是判据把噪声挡掉了。
    if degraded is not None:
        used["douhot_down"] = used.get("douhot_down") or ("测试强制降级" if degraded else "")
    is_degraded = bool(used.get("douhot_down"))

    rank_top = int(getattr(settings, "hot_forecast_degraded_rank_top", 10) or 10)
    cands: list[dict] = []
    for seed, g in groups.items():
        conf = confirms.get(seed)
        if conf is not None:
            cands.append({**g, "douhot": conf})
        elif is_degraded and 0 < g["rank"] <= rank_top:
            cands.append({**g, "douhot": None})
    used["confirmed"] = len(cands)

    #: ③ AI 判价值
    if scores is None:
        scores = judge_values(settings, [{"seed": c["seed"], "title": c["title"],
                                          "srcs": sorted(c["srcs"])} for c in cands])
    min_score = int(getattr(settings, "hot_forecast_min_score", 6) or 6)
    out: list[dict] = []
    n_low = 0
    for c in cands:
        sc = scores.get(c["seed"])
        if sc is None:
            #: AI 没判(没 key/失败)⇒ 放行(与 `ai_filter` 纪律 1 同:失败不静默丢)
            out.append({**c, "score": None, "why": ""})
            continue
        if int(sc["score"]) < min_score:
            n_low += 1
            continue
        out.append({**c, "score": int(sc["score"]), "why": sc.get("why", "")})
    used["ai_low"] = n_low
    out.sort(key=lambda x: (-(x["score"] if x["score"] is not None else min_score),
                            -(x["douhot"]["last"] if x["douhot"] else 0)))
    used["cand"] = len(out)
    if stats is not None:
        stats.update(used)
    return out


# ---------------------------------------------------------------- 备料(不碰上传)

def prepare(settings, item: dict, *, user_id: int = 1) -> dict:
    """给一条预判**备好两份物料**:PDF 资料 + 公众号推文稿。返回 `{"pdf","article","note"}`。

    ⚠️ **不调用 `scripts/gossip_publish.py`** —— 夸克两个号都满了(99.99999% / 163.8%),
    用户 2026-11-24 拍板「暂不碰上传」。文件只落本机 `data/hot_forecast/`。
    ⚠️ 素材为 0 ⇒ **两份都不生成**(`compose_gossip_doc` 的底线:不许模型凭记忆编)。
    """
    from app.services import gossip_doc, gossip_material, llm_client

    api = str(getattr(settings, "deepseek_api_key", "") or "")
    if not api:
        return {"pdf": "", "article": "", "note": "未配置 deepseek key"}
    title = str(item.get("title") or "")
    seed = str(item.get("seed") or "")
    out_dir = Path(str(getattr(settings, "hot_forecast_material_dir", "data/hot_forecast")))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    base = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", seed)[:30].strip("_") or "hot"
    base = f"{base}_{stamp}"
    res: dict = {"pdf": "", "article": "", "note": ""}

    #: ① 素材 —— 百度资讯 + 微博(抖音 CDP 时好时坏,默认关,配置可开)
    got = gossip_material.gather(
        seed or title, user_id=user_id, limit=8,
        use_douyin=bool(getattr(settings, "hot_forecast_use_douyin", False)))
    counts = got.get("counts") or {}
    if not str(got.get("text") or "").strip():
        return {**res, "note": f"素材 0 条({counts or got.get('fails')})⇒ 拒绝生成"}

    #: ② PDF 资料
    try:
        md = llm_client.compose_gossip_doc(
            str(getattr(settings, "deepseek_base_url", "https://api.deepseek.com")), api,
            str(getattr(settings, "deepseek_model", "deepseek-chat")),
            title=seed or title, sources=str(got["text"]))
        if md and "免责声明" in md:
            pdf = out_dir / f"{base}.pdf"
            gossip_doc.render_pdf(md, pdf)
            res["pdf"] = str(pdf)
        elif md:
            res["note"] = "模型没写免责声明小节 ⇒ 不出 PDF"
    except Exception as exc:  # noqa: BLE001 - 备料失败不挡预警卡
        logger.warning("爆款预判:PDF 生成失败 %s", seed, exc_info=True)
        res["note"] = f"PDF 失败:{type(exc).__name__}"

    #: ③ 公众号推文稿(公众号正文不放外链 ⇒ wechat_seo=True,文末引导回复关键词)
    try:
        art = llm_client.draft_article(
            str(getattr(settings, "deepseek_base_url", "https://api.deepseek.com")), api,
            str(getattr(settings, "deepseek_model", "deepseek-chat")),
            hotspot=f"{seed}(热榜:{'/'.join(sorted(item.get('srcs') or []))}"
                    f"#{item.get('rank')};抖音单日 {_wan(item)} )",
            resource=f"{seed} 的原文/全文整理", wechat_seo=True)
        if art and art.get("content"):
            path = out_dir / f"{base}_推文.md"
            lines = ["# 备选标题"] + [f"- {t}" for t in art.get("titles") or []]
            if art.get("keyword"):
                lines.append(f"\n回复关键词:{art['keyword']}")
            lines.append("\n---\n" + str(art["content"]))
            path.write_text("\n".join(lines), encoding="utf-8")
            res["article"] = str(path)
    except Exception as exc:  # noqa: BLE001 - 同上
        logger.warning("爆款预判:推文稿生成失败 %s", seed, exc_info=True)
        res["note"] = (res["note"] + " / " if res["note"] else "") + \
            f"推文稿失败:{type(exc).__name__}"
    return res


def _wan(item: dict) -> str:
    d = item.get("douhot")
    return f"{d['last'] / 1e4:.0f} 万" if d else "未确认"


# ---------------------------------------------------------------- 卡片 / 冷却

def _line_of(a: dict) -> str:
    """一条 → 卡片里的 3~4 行。**前两行必须能让人决定要不要动手**。"""
    d = a["douhot"]
    if d:
        trend = (f"📈 抖音热点宝 **{d['last'] / 1e4:.0f} 万**/单日"
                 f" · 是自身基线 **{d['ratio']:.0f} 倍** · 有量 {d['days']} 天")
    else:
        trend = "📈 抖音趋势 **未确认**(热点宝不可达,仅凭热榜形态)"
    srcs = "/".join(sorted(a["srcs"])) or "—"
    lines = [f"**{a['seed'][:40]}**",
             trend,
             f"🔥 热榜 {srcs} #{a['rank']} · 标题「{a['title'][:50]}」"]
    if a.get("score") is not None:
        lines.append(f"🤖 AI 价值 {a['score']}/10:{a['why'] or '—'}")
    if a.get("materials"):
        m = a["materials"]
        if m.get("pdf"):
            lines.append(f"📄 物料已备:{m['pdf']}")
        if m.get("article"):
            lines.append(f"✍️ 推文稿已备:{m['article']}")
        if m.get("note") and not (m.get("pdf") or m.get("article")):
            lines.append(f"⚠️ 物料未生成:{m['note']}")
    if a.get("peer_authors") is not None:
        n = a["peer_authors"]
        lines.append("👥 库内同行 **0 人** · 我们还没有自有链(先手窗口)"
                     if n == 0 else f"👥 库内同行 {n} 人")
    return chr(10).join(lines)


def _render(alerts: list[dict], nl: str, min_last: int = 5_000_000,
            min_ratio: float = 10.0) -> str:
    """**一轮一张汇总卡** → 飞书纯文本(管理群)。

    ⚠️ `min_ratio` 必须由调用方传入 **settings 里的实际值**:卡片上写的判据
    与 `douhot_confirm` 真正用的阈值必须是同一个数 —— 写死 30 倍的话,
    2026-11-25 放宽到 10 倍之后卡面会撒谎,运营按卡面复算会觉得判据对不上。
    """
    lines = [f"🔮 **爆款预判** · {len(alerts)} 条「适合做网盘、还没人动手」的热点"]
    for i, a in enumerate(alerts, 1):
        lines.append("")
        lines.append(f"{i}. " + _line_of(a))
    lines.append("")
    lines.append("— 判据:标题有**资源形态**(原文/全集/大结局…)+ 抖音搜索量 ≥"
                 f"{min_last / 1e4:.0f} 万且 ≥{min_ratio:g} 倍自身基线。"
                 "⚠️ 这是**预判**不是确认;「同行 0 人」只代表**我们库内**。"
                 "⚠️ 物料只落本机 `data/hot_forecast/`,**未上传**(盘满)。")
    return nl.join(lines)


def _recently_alerted(session, user_id: int, key: str, cooldown_hours: float) -> bool:
    """**纯读**预检:这条在冷却期内发过没有(语义同 `hot_breakout`)。"""
    from sqlalchemy import select

    from app.db.models import FeishuAlert

    row = session.scalar(select(FeishuAlert).where(
        FeishuAlert.section == "hot_forecast",
        FeishuAlert.user_id == user_id,
        FeishuAlert.title == key[:200]))
    if row is None:
        return False
    return (datetime.now() - row.alerted_at) < timedelta(hours=float(cooldown_hours))


def _mark_alerted(session, user_id: int, key: str, cooldown_hours: float,
                  reason: str) -> None:
    """**发送成功之后**才落冷却行。写失败只影响去重,不抛。"""
    from app.services.alert_service import feishu_alert_gate

    try:
        feishu_alert_gate(session, user_id, "hot_forecast", key, cooldown_hours, reason)
        session.commit()
    except Exception:  # noqa: BLE001 - 冷却没落盘只影响去重
        logger.warning("爆款预判冷却门写入失败 %s", key, exc_info=True)
        session.rollback()


def _record(session, user_id: int, status: str, detail: str) -> None:
    """运行记录失败不挡业务。"""
    try:
        from app.services.tenant_base import _record_run

        _record_run(session, user_id, "hot_forecast", status, detail)
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.warning("爆款预判运行记录失败 user=%s", user_id, exc_info=True)


def _enrich_peer(alerts: list[dict], user_id: int) -> None:
    """给每条补「库内同行数 / 已有我方链」(复用 `hot_breakout` 的语料口径)。

    ⚠️ **必须先补完再发卡**:没有这两项,卡片就分不清「先手」和「跟风」——
    而这个功能的全部价值在先手。读本机库是**纯读且很快**,不构成长事务。
    """
    from app.db import get_session_local
    from app.services.hot_breakout import _corpus, _library_rows, _peer_count
    from app.services.resource_library import match_key

    session = get_session_local()()
    try:
        now = datetime.now()
        rows = _library_rows(session, user_id, now - timedelta(hours=24), now)
        corpus = _corpus(rows)
        for a in alerts:
            a["peer_authors"], a["owned"] = _peer_count(corpus, match_key(a["seed"]))
    except Exception:  # noqa: BLE001 - 同行数只是加分项,读不到不挡预警
        logger.warning("爆款预判:库内同行数读取失败(按未知处理)", exc_info=True)
    finally:
        session.close()


def hot_forecast_tick(settings=None) -> int:
    """定时入口(每天 08:49 / 20:49):三源 + 抖音趋势 → 管理群 + 备料。返回发卡数。

    ⚠️ 时刻的两个理由:
      · **08:49** —— 覆盖「昨天白天到现在」的全部榜单,让"昨天下午起来的东西"
        在第二天上班就有卡(麦穗 11-15 下午三源齐报 ⇒ 11-16 早上就该报);
      · **20:49** —— 覆盖当天白天起来的东西,不必等到第二天。
      · 不落 `:00/:30` 网格、不与任何定点作业同分钟(`test_job_stagger` 是硬守卫)。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    if not bool(getattr(settings, "hot_forecast_enabled", True)):
        return 0
    from sqlalchemy import select

    from app.db import get_session_local
    from app.db.models import User
    from app.services.feishu_client import FeishuClient, webhook_for

    hook = webhook_for(settings, "admin")
    if not hook:
        return 0
    min_last = int(getattr(settings, "hot_forecast_min_last", 5_000_000) or 0)
    #: 卡面上写的倍数必须与 `douhot_confirm` 实际用的一致(理由见 `_render` 头注)
    min_ratio = float(getattr(settings, "hot_forecast_min_ratio", 10.0) or 10.0)

    #: ① **全部慢活都在本机 session 之外**:远程查询 → 热点宝 → AI 判定
    stats: dict = {}
    try:
        alerts = detect(settings, stats=stats)
    except Exception:  # noqa: BLE001 - 远端不可达等,记失败不抛
        logger.exception("爆款预判检测失败")
        session = get_session_local()()
        try:
            _record(session, 1, "failed", "检测异常(远程/热点宝/AI 之一)")
        finally:
            session.close()
        return 0

    client = FeishuClient(hook, getattr(settings, "feishu_secret", ""))
    cooldown = float(getattr(settings, "hot_forecast_cooldown_hours", 20) or 20)
    nl = chr(10)
    sent = 0
    session = get_session_local()()
    try:
        uids = [u for (u,) in session.execute(
            select(User.id).where(User.enabled.is_(True))).all()]
        for uid in uids:
            fresh: list[dict] = []
            n_cool = 0
            for a in alerts:
                #: 冷却键 = 作品名(**不含日期**):同一事件第二天不重复报,
                #: 但必须**发送成功才落**(失败下一轮原样重试)
                key = f"forecast:{a['seed'][:80]}"
                try:
                    if _recently_alerted(session, uid, key, cooldown):
                        n_cool += 1
                        continue
                except Exception:  # noqa: BLE001 - 读不到冷却就照发(宁可多报)
                    logger.warning("爆款预判冷却预检失败 %s(按未发过处理)", key,
                                   exc_info=True)
                    session.rollback()
                fresh.append(a)
            #: ② 库内同行数(纯读,必须是发卡前补齐)+ 剔掉「我们已有链 / 同行已入局」
            #: ⚠️ 这两级**必须在截 top 之前**做,否则前 3 条都被推过时会把后面干净的挤掉
            n_owned = 0
            n_peer = 0
            if fresh:
                _enrich_peer(fresh, uid)
                max_peer = int(getattr(settings, "hot_forecast_max_peer_authors", 0) or 0)
                n_owned = sum(1 for a in fresh if a.get("owned"))
                before = len(fresh)
                fresh = [a for a in fresh
                         if not a.get("owned") and (a.get("peer_authors") or 0) <= max_peer]
                n_peer = before - n_owned - len(fresh)
                fresh = fresh[:int(getattr(settings, "hot_forecast_top", 3) or 3)]
                #: ⚠️ 上面的冷却预检会隐式开一个读事务。**备料是几分钟级的慢活**
                #: ⇒ 先把事务结束掉(WAL 下读不挡写,但不该抱着陈旧快照跨过几轮别人写入)。
                session.commit()
            #: ③ 备料(**在本机 session 之外**跑,慢活不许抱锁)
            if fresh and bool(getattr(settings, "hot_forecast_materials", True)):
                budget = int(getattr(settings, "hot_forecast_materials_top", 2) or 0)
                for a in fresh[:budget]:
                    a["materials"] = prepare(settings, a, user_id=uid)
            #: ④ 发卡 + **成功后才逐条落冷却**
            n_sent = 0
            if fresh and client.send(_render(fresh, nl, min_last, min_ratio)):
                sent += 1
                n_sent = len(fresh)
                for a in fresh:
                    d = a.get("douhot")
                    _mark_alerted(session, uid, f"forecast:{a['seed'][:80]}", cooldown,
                                  f"{a['rank']}名/{_wan(a)}"
                                  + (f"/{d['ratio']:.0f}x" if d else "/未确认"))
            _record(session, uid, "success",
                    f"三源{stats.get('rows', 0)}行 "
                    f"形态{stats.get('form', 0)} "
                    f"作品{stats.get('seeds', 0)} "
                    f"趋势确认{stats.get('douhot_ok', 0)} "
                    f"降级候选{stats.get('confirmed', 0) - stats.get('douhot_ok', 0)} "
                    f"AI低分{stats.get('ai_low', 0)} ⇒ "
                    f"冷却{n_cool} 已有链{n_owned} 同行已入局{n_peer} 卡内{n_sent}"
                    + (f" 热点宝故障:{stats['douhot_down']}"
                       if stats.get("douhot_down") else ""))
            break               #: 远程数据只有一份(user 1 的镜像),多租户会重复发
    finally:
        session.close()
    return sent