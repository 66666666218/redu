# -*- coding: utf-8 -*-
"""推送台账:某个群那天**真的收到了什么** —— 日结/周结的单一数据源(2026-10-16)。

## 为什么要有它

用户口径:「**有的客户反馈有点信息太多看不过来,能否加上每日总结昨天…再加一个周结…
每个板块都配置上**」。要按板块做总结,先得能回答「**昨天那个板块推出去了什么**」。
本仓三处现成数据都答不了:

  · **业务表**(文章/线索/热榜…)只记「**采到了**」 —— 采到但没发出去的
    (被去重掉、那个板块没配群、卡片发送失败)也在表里 ⇒ 拿它当日结,会把
    **客户从没见过的东西**算成「发过了」(本仓最忌讳的那类"看起来做了");
  · `resource_presence`(小红书/快手/贴吧/B站/知乎)那张卡**从来不落库**
    ⇒ 那 5 个板块在库里**什么都没有**;
  · `notification_logs` 只记 `notify_incident` 的**事件级告警**(采集失败/停摆),
    记的是「哪里坏了」,不是「发了什么」。

⇒ 于是加这一张**只增不改**的台账:**推卡的那一刻**顺手记一行。

## 纪律(每条都对着本仓踩过的坑)

1. **一次推送一行,写的时候不聚合**(同一资源推了 3 轮 = 3 行)—— 聚合放到读的时候,
   窗口计数与「谁被反复推」才有干净语义(见 `window()` 的 `pushes`)。
2. **记不上不许挡住推送**:台账是**旁路**(推卡才是主业)⇒ 写失败只 warning +
   用 SAVEPOINT 兜住,**绝不**让一次台账写入把推送事务连坐。
   ⚠️ 但**不许静默** —— 失败必须留日志,否则「今天没内容」与「台账没写进去」
   在日结上长得一模一样。
3. **一条坏不牵连这一批的其余条目**:逐条 try,坏的记 warning 跳过。
4. 时间戳一律取 `datetime.now()`(**本地**),窗口也是本地零点 ——
   与运行记录/周报同一口径(远程那份 MySQL 的 `NOW()` 是 UTC,这里是本机进程,别混)。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 各推送点 item 的**形状本来就不同**,但它们**本来就带着这些信息**。
#: 与其在台账里再造一套 DTO(每个推送点都要适配一遍),不如把已知的键**按优先级**认下来。
#:
#: ★ **`desc` 是 2026-11-18 补的**,而且是**抖音那两路唯一的标题**:
#:   `demand_watch.discover_latest`(最新大瓜)与求1型视频的条目只有 `desc`(视频标题),
#:   没有一个 `title/name` —— 实测近 7 天 `section='douyin'` 的台账 **30 行里 24 行 title 为空**,
#:   `item_key` 全退化成 aweme_id ⇒ 日结/周结那张表里那批资源**没有名字可显示**。
#:   ⚠️ 放**末位**:有 `title/name` 时必须优先用它们(`desc` 是"描述",不是全仓的标题主键)。
_TITLE_KEYS = ("title", "name", "resource", "work", "word", "desc")
_KEY_KEYS = ("item_key", "key", "uid", "aweme_id", "note_id", "id")
_URL_KEYS = ("url", "origin_url", "source_url", "work_url")
_PAN_KEYS = ("my_link", "our_url", "pan_url", "share_url")
#: **源链**(原作者分享的那条)—— 判重身份的首选,见 `pan_id_of`。
#: ⚠️ 与 `_PAN_KEYS` 分开:`pan_url` 那一列必须留我方链(日结要客户能点),
#: 而判重认的是"同一份资源",原作者的链才是它的稳定身份。
_SRC_PAN_KEYS = ("src_pan_url", "source_pan_url", "origin_pan_url", "pan_src_url")
_AUTHOR_KEYS = ("author", "bench_author", "nickname")
#: 有些推送点把互动数**摊平**在 item 上(`douyin_leads` 的 `share_count` 就是),一并收进 metrics
_FLAT_METRIC_KEYS = ("play_count", "liked_count", "collected_count", "comment_count",
                     "share_count", "read_num")

#: 各键的取值长度上限(表字段就这么宽;超了要**截断**而不是让 INSERT 抛)
_LIMITS = {"section": 32, "item_key": 255, "title": 255, "url": 500, "pan_url": 500,
           "pan_id": 255, "pan_type": 16, "author": 64, "metrics": 255}

#: 盘链身份判定时**要保留**的查询参数:只有提取码会改变"能不能打开",
#: 其余(`utm_*`/`entry`/`share_token` 之类)同一份资源的写法差异很大,算噪声。
_PAN_KEEP_QS = ("pwd", "password", "code", "passcode")

#: ★★ **"又火了"的绝对增量下限,按板块**(2026-11-25 标定,`hot_rise_min_abs` 的默认来源)。
#:
#: 为什么比例不够用:各板块的热度口径量级差 **4 个数量级** —— `kuaishou` 的播放量
#: 中位 12.7 万、`bilibili` 1.8 万,而 `tieba` 的 `total_replay_num` 中位是 **2**、
#: `zhihu` 的 `comment_count` 中位是 **2**。在个位数基数上,"涨 30%" = 多一个回复,
#: 那不是"资源火了",是同一条又被顶了一下。
#:
#: 标定方法(30 天台账 8553 行,生产口径重放 —— 盘链身份优先、`item_key` 兜底,
#: 按 `(section, 身份)` 比前后两次)。**先过 30% 比例闸,再在候选池内部定增量档**:
#:   · 看**噪音的天花板**(涨幅 <5% 那批的增量):`wechat` 最大 7、`xiaohongshu` 19、
#:     `weibo_peer` 16 —— 门槛取它的若干倍以上;
#:   · 看**候选池的分位**:每个值都标了它落在 p 几,以及切掉/保留各多少条;
#:   · 逐条抽查:`被切掉的必须全是小量级`、`被保留的必须都像真爆`。
#:
#: ⚠️ 这些档位是**分位数选择**,不是"天然断点"(只有 `tieba` 有干净空档:
#:   候选池在 +17 与 +72 之间是空的)。所以每一行都写了"为什么不是相邻那一档"。
#: ⚠️ `zhihu`/`kuaishou`/`douyin`/`xunlei_group` **不在表里 = 0 = 不启用**:
#:   · `zhihu` 的热度是「同资源内容数」,过 30% 的 15 条**全部增量 <20**(p50=1)——
#:     加门槛会一条不剩,那是把链关死;
#:   · `kuaishou` 的候选池 p10 已达 **29929**(播放量中位 12.7 万,涨 30% 本身就是
#:     大几万起)⇒ 比例闸已经挡干净,再补绝对门槛只多挡 3 条,答不上"为什么是一万"。
#: ⚠️ 改这张表**必须重跑回放**(`scripts/backtest_hot_rise.py`),别凭手感调。
HOT_RISE_MIN_ABS = {
    #: 播放量。候选池(过 30%)204 条、p25=7427;10000 切 62 条,被切的最大 +9759
    #: 是「免费二维码生成器 / 700+中文字体」这类**工具帖**(有量但不是爆款),
    #: 保留的最小 +10361。取 2000(切 20)会放过那批工具帖;取 50000 会切掉 129 条。
    "bilibili": 10000,
    #: 点赞。噪音天花板 19,500 是它的 26 倍;候选池 129 条、p50=360。
    #: 切 76 条,被切最大 +494(一折春完结 / 会议记录模板),保留最小 +581。
    #: 取 100(切 34)会放过 +86(月亮查询)这种;取 2000(切 108)只剩 21 条。
    "xiaohongshu": 500,
    #: 阅读数。噪音天花板 **7**,50 是它的 7 倍;候选池 428 条(最大)、p75=42。
    #: 切 334 条,被切最大 +48(七宗罪测试 / 霸王茶姬杯贴 —— 48 阅读就是几十户人家)。
    #: 取 20(切 260)会放过 +19;取 100(切 380)只剩 48 条,太窄。
    "wechat": 50,
    #: 点赞。噪音天花板 16,50 是它的 3 倍;候选池 95 条、p50=41。
    #: 切 55 条,被切最大 +48,保留最小 +51。取 20 只切 32 条(+19 会漏过)。
    "weibo_peer": 50,
    #: 回复数(该板块 `total_replay_num` 中位就是 **2**)。候选池 38 条,
    #: **+17 与 +72 之间是干净空档** ⇒ 20 落在空档里,只放行 +72/+108/+990/+995 这 4 条。
    #: 取 5 会放进 +4(1→5)那种"多一个人";取 50 切掉的行数与 20 完全相同(空档)。
    "tieba": 20,
    #: 点赞。候选池 26 条(样本少)、p50=324。500 切 15 条,被切最大 +359(维尼小熊),
    #: 保留最小 +911。取 50 只切 3 条(+34 豆花空间会漏过);取 5000 只剩 3 条(全是麦穗)。
    "douyin_peer": 500,
}


def _first(d: dict, keys: tuple[str, ...]) -> str:
    """按优先级取第一个**非空**值(空串/None 都算没给)。"""
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _has_src_key(it: dict) -> bool:
    """条目**显式带了源链键**吗(哪怕值是空串)?

    只有**公众号**那条链会这么做(它手上同时有"我方转存链"和"原作者的源链")。
    别的板块给的 `pan_url` 本来就是别人的源链 —— 对它们加标题判据会**漏挡**。
    """
    if any(k in it for k in _SRC_PAN_KEYS):
        return True
    link = it.get("link")
    return isinstance(link, dict) and any(k in link for k in _SRC_PAN_KEYS)


def _pan_id_of(it: dict) -> str:
    """条目的**判重身份**:源链优先,没有源链才用我方链。

    ★★ 2026-11-19(实测驱动的取舍)。判重判的是「**这是不是同一份资源**」,
    而这两条链的可靠性**方向相反**:

      · **源链**(`src_pan_url` 之类)是"原作者分享的那一份"。同一个源链 ⇒ 同一份资源,
        这条判据**零误伤**(近 3 天 2011 行带盘数据重放:按它挡 1142 行,标题冲突 0)。
        代价是**压不住副本**:5 个号各转存一份同一 PDF 时源链各不相同。
      · **我方链**是我们**自己转存出来的**。它在理论上更强(多号转存同一份会收敛到一条),
        但实测**不成立**:310 条我方链里 **67 条(21.6%)挂着 >1 个不同源链** ——
        `7728b469a574` 在资源库里登记的是「儿童涂色填色模板大全」,台账上却挂着
        公考答题卡 / 作文稿纸 / 2027 日历三份**毫不相干**的东西(`_enrich` 的
        「资源级复用」按标题近似匹配,把不同资源错配到了同一条链)。
        ⇒ 按我方链判重会**挡掉真实的新资源**(72h 实测 282 行),而客户的判据是
        「**带网盘链接 + 最新**」——**丢资源比重复推一遍严重得多**。

    ⇒ 所以身份取**源链优先**;`drop_recent(our_chain_check_title=True)` 那边再叠一道
    "我方链命中 **且** 标题同源"的辅助判据(见 `_same_resource_title`),把副本也压住,
    但**只在标题确认同源时才压**。

    ## 为什么"有没有给 `src_pan_url`"就是那个开关

    只有**公众号**这条链的 `pan_url` 是**我们自己转存出来的**。别的链
    (抖音对标号正文直链 / 资源库的 `pan_url` / 发现链)给的**本来就是别人的源链**,
    它本身就是资源身份,不该再过标题判据 —— 无条件加标题判据会让它们**漏挡**
    (实测:抖音对标号"同一份资源换作品 id"立刻不再被挡)。

    ⇒ 判据:**条目带了 `src_pan_url` 这个键**(哪怕值是空串)就说明调用方明确区分了
    「我方链 / 源链」,身份**只认源链**(没有源链就给 `""`,交给标题判据兜);
    否则身份就用 `pan_url`(它本来就是资源身份)。`_pan_of` 的 `_PAN_KEYS` 顺序
    保证「我方链优先」,那个语义只对公众号成立,所以只能由调用方显式告诉这里。

    返回 `pan_key()` 归一后的串(认不出给 `""`)。
    """
    if _has_src_key(it):
        link = it.get("link") if isinstance(it.get("link"), dict) else {}
        return pan_key(_first(it, _SRC_PAN_KEYS)) or pan_key(_first(link, _SRC_PAN_KEYS))
    return pan_key(_pan_of(it))


def _same_resource_title(a: str, b: str) -> bool:
    """两个标题是不是**同一份资源**(判据复用 `resource_library.same_resource`)。

    ⚠️ 只用于**判重**(要不要再推一遍),不用于给客户出链 —— 那仍走
    `title_claims` 那套严格判据(见 `same_resource` 自己的头注)。
    判不了(空标题 / 判据模块不可用)一律按**同一份**处理:判重是旁路,
    多挡一次只是少推一遍,而放行会刷屏。
    """
    x, y = str(a or "").strip(), str(b or "").strip()
    if not x or not y:
        return True
    if x == y:
        return True
    try:
        from app.services.resource_library import same_resource

        return bool(same_resource(x, y))
    except Exception:  # noqa: BLE001 - 判据不可用就当同一份(与"判不了按同一份"一致)
        logger.info("判重的标题比较不可用(按同一份处理)", exc_info=True)
        return True


def _norm_item(it: dict, section: str) -> dict[str, Any] | None:
    """一条 item → 台账行(认不出任何东西时返回 `None`,由调用方跳过)。"""
    from app.services.douyin_leads import _pan_name      # 盘名的**单一出处**(别抄第二份)

    title = _first(it, _TITLE_KEYS)
    url = _first(it, _URL_KEYS)
    #: ⚠️ **`resource_presence` 那条链的原帖链在 `sample_urls`(list)里** ——
    #: 它没有 `url` 这个键。2026-10-16 拿真形状渲染预览时**当场抓到**:不认它,
    #: 日结卡上「对标作品」整列恒 `—`(而数据一直都在)。
    #: 只取**能打开的第一条**;`sample_urls` 里可能有空串(源没给链)。
    if not url and isinstance(it.get("sample_urls"), (list, tuple)):
        url = next((str(u).strip() for u in it["sample_urls"] if str(u or "").strip()), "")
    pan_url = _first(it, _PAN_KEYS)
    #: ⚠️ presence 那种把链藏在 `link: {...}` 里的形状(它每行都这么给)
    link = it.get("link") if isinstance(it.get("link"), dict) else {}
    if link:
        pan_url = pan_url or _first(link, _PAN_KEYS)
        url = url or _first(link, _URL_KEYS)
    if not title and not url and not pan_url:
        return None                       #: 什么都没有 ⇒ 这行记了也没法在日结里显示
    item_key = _first(it, _KEY_KEYS) or title or url
    #: 热度:优先现成的 `metrics` dict,再把**摊平**在 item 上的互动数补进去
    raw = it.get("metrics") if isinstance(it.get("metrics"), dict) else {}
    metrics = {k: int(v) for k, v in raw.items() if isinstance(v, (int, float)) and int(v) > 0}
    for k in _FLAT_METRIC_KEYS:
        v = it.get(k)
        if isinstance(v, (int, float)) and int(v) > 0 and k not in metrics:
            metrics[k] = int(v)
    mj = json.dumps(metrics, ensure_ascii=False) if metrics else ""
    row = {
        "section": section[: _LIMITS["section"]],
        "item_key": item_key[: _LIMITS["item_key"]],
        "title": title[: _LIMITS["title"]],
        "url": url[: _LIMITS["url"]],
        "pan_url": pan_url[: _LIMITS["pan_url"]],
        #: 判重身份(源链优先;见 `_pan_id_of` 的取舍)。**没有就留空** ——
        #: 空串让读取端回落到 `pan_url`,而不是把"没盘链"当成一种身份。
        "pan_id": _pan_id_of(it)[: _LIMITS["pan_id"]],
        "pan_type": (_pan_name(pan_url) or "")[: _LIMITS["pan_type"]],
        "author": _first(it, _AUTHOR_KEYS)[: _LIMITS["author"]],
        "metrics": mj[: _LIMITS["metrics"]],
    }
    return row


def _key_of(it: dict) -> str:
    """条目的**身份**,必须与 `_norm_item` 的 `item_key` 一致(去重要按同一把尺子)。"""
    return (_first(it, _KEY_KEYS) or _first(it, _TITLE_KEYS) or _first(it, _URL_KEYS))[:255]


def pan_key(url: str) -> str:
    """盘链的**资源身份**(归一化后的字符串;认不出来返回 `""`)。

    ## 为什么不能用原串比

    同一条盘链在不同来源里的写法会有差异(`https://` vs `http://`、末尾 `/`、
    `pan.quark.cn/s/xxx?entry=...` 这类渠道参数)。原串比 ⇒ 同一个资源被当成几个,
    **去重就漏了**。反过来,只剩**提取码**类参数会改变"能不能打开",必须保留。

    ## 为什么不只看 `#fid`

    夸克/百度的分享链身份就在 path 里(`/s/<id>`);迅雷在 `/s/<id>`。取
    `host + path` 已经足够区分资源,再拼提取码就是全部可辨信息。

    ⚠️ **保留原协议无关**:`http`/`https` 指同一份资源。
    """
    u = str(url or "").strip()
    if not u:
        return ""
    try:
        from urllib.parse import parse_qsl, urlencode, urlsplit

        parts = urlsplit(u)
        host = (parts.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = (parts.path or "").rstrip("/")
        if not host or not path:
            return ""
        keep = sorted((k.lower(), v) for k, v in parse_qsl(parts.query or "", keep_blank_values=True)
                      if k.lower() in _PAN_KEEP_QS)
        return f"{host}{path}" + (f"?{urlencode(keep)}" if keep else "")
    except Exception:  # noqa: BLE001 - 解析不了的当"认不出",退回按原条目不参与盘链去重
        return ""


def _pan_of(it: dict) -> str:
    """条目身上的盘链(`_PAN_KEYS` 与 `link` 兜底,与 `_norm_item` 同一套认法)。"""
    pan = _first(it, _PAN_KEYS)
    if not pan:
        link = it.get("link") if isinstance(it.get("link"), dict) else {}
        pan = _first(link, _PAN_KEYS) if link else ""
    return pan


def group_of(settings, section: str) -> tuple[str, ...]:
    """与 `section` 落在**同一个飞书群**的板块集合(含自己;认不出就是 `(section,)`)。

    ## 为什么 section 不等于群

    `drop_recent` 的去重范围一直是 `section`,理由写在它的头注里:「不同板块的群
    不是一批人,跨板块去重会把别的群饿死」。**但那个前提对 tieba/zhihu 不成立** ——
    实测(2026-11-18)两者配的是**同一个专属群**(webhook 尾部同为 `a062026f`)。
    近 7 天这两个 section 有 **10 个盘链在同一个群里各推一遍、涉及 166 行**
    (占它们带盘行的 37.6%),而 `item_key` **跨板块重复 0 条** ⇒ 内容级去重
    根本抓不到,只有盘链闸能抓,偏偏闸又被 section 隔开了。

    ## 判据:只认**显式配成同一个专属群**的

    ⚠️ **不许拿 `webhook_for()` 比**:它在没配专属群时会**回落主群** ⇒ 所有没配
    专属群的板块都会被判成"同群",那时按群去重会把它们的**内容互相挡掉**(灾难)。
    所以只比 `platform_webhook()`(专属群;未配返回空串),空的一律各管各的。
    """
    from app.services.feishu_client import _SECTION_WEBHOOK, platform_webhook

    if not section:
        return ()
    mine = platform_webhook(settings, section) if settings is not None else ""
    if not mine:
        return (section,)
    same = [section]
    for other in _SECTION_WEBHOOK:
        if other != section and platform_webhook(settings, other) == mine:
            same.append(other)
    return tuple(sorted(same))


def recent_keys(session: Session, user_id: int, section: str,
                hours: float = 24, sections: tuple[str, ...] | None = None) -> set[str]:
    """这个板块最近 N 小时**已经推过**的 `item_key` 集合(`hours<=0` = 空集)。

    `sections` 非空时按**那一组板块**查(见 `group_of`:同群的板块要共用一道闸)。
    """
    if session is None or hours <= 0:
        return set()
    from sqlalchemy import select

    from app.db.models import PushLedger

    scope = tuple(sections or (section,))
    cutoff = datetime.now() - timedelta(hours=float(hours))
    return set(session.scalars(select(PushLedger.item_key).where(
        PushLedger.user_id == user_id,
        PushLedger.section.in_(scope),
        PushLedger.pushed_at >= cutoff)).all())


def recent_pan_map(session: Session, user_id: int, section: str,
                   hours: float = 24,
                   sections: tuple[str, ...] | None = None) -> tuple[set[str], dict[str, str]]:
    """窗口内已推过的 **①源链身份集合 ②我方链身份 → 当时那行的标题**。

    ★★ 2026-11-19:判重从「只用我方链」改成**双判据**,对应 `_pan_id_of` 的取舍 ——
      · `srcs`   :**源链**身份。命中就挡(同一份资源,零误伤);
      · `my_to_title`:**我方链**身份 → 上次推它时的标题。命中**还要**过
        `_same_resource_title` 才挡,因为实测 21.6% 的我方链挂着多个不同源链
        (`_enrich` 的资源级复用错配)⇒ 光看链会把不同资源互相挡掉。

    ⚠️ 库里的 `pan_id` 是上线后才有值;存量行(空串)回落到 `pan_url` ——
    `pan_url` 只记我方链,**没有源链信息**,所以那批行只能走我方链那一档
    (带上标题判据,不会重演 282 行误伤)。
    ⚠️ 库里存的是**原串**,这里逐条过 `pan_key()` 归一(管道/末尾斜杠/渠道参数)。
    """
    if session is None or hours <= 0:
        return set(), {}
    from sqlalchemy import select

    from app.db.models import PushLedger

    scope = tuple(sections or (section,))
    cutoff = datetime.now() - timedelta(hours=float(hours))
    rows = session.execute(select(PushLedger.pan_id, PushLedger.pan_url,
                                  PushLedger.title).where(
        PushLedger.user_id == user_id,
        PushLedger.section.in_(scope),
        PushLedger.pushed_at >= cutoff)).all()
    srcs: set[str] = set()
    my_to_title: dict[str, str] = {}
    for pan_id, pan_url, title in rows:
        myk = pan_key(pan_url or "")
        if myk:
            #: 同一我方链多行时,**后写的覆盖** = 最近一次那行的标题(与本函数语义一致)
            my_to_title[myk] = (title or "")
        #: 身份 = `pan_id`(源链优先),存量行/非公众号链回落到 `pan_url`。
        #: ⚠️ **一律入 `srcs`**:它就是"这份资源的身份",命中即挡。
        key = (pan_id or "").strip() or myk
        if key:
            srcs.add(key)
    return srcs, my_to_title


def recent_pan_keys(session: Session, user_id: int, section: str,
                    hours: float = 24,
                    sections: tuple[str, ...] | None = None) -> set[str]:
    """窗口内已推过的**盘链身份**集合(源链 ∪ 我方链,只做聚合统计用)。

    ⚠️ **不要拿它当判重依据** —— 它把两条可靠性完全不同的链混在一起,
    丢掉了我方链那档需要的标题判据(见 `recent_pan_map` 与 `_pan_id_of`)。
    """
    srcs, my_to_title = recent_pan_map(session, user_id, section, hours, sections)
    return srcs | set(my_to_title)


def last_pan_hot(session: Session, user_id: int, section: str, hours: float,
                 sections: tuple[str, ...] | None = None, hot_of=None) -> dict[str, float]:
    """窗口内**每个盘链**最近一次被推出去时的热度 → `{pan_key: hot}`。

    ★★ 2026-11-19:`hot_rise_only` 的基准原来只按 `item_key` 取(`last_metrics`),
    而 `item_key` 是**内容**身份 —— 同一份资源被不同的号各发一遍时它每次都变
    (`art{id}` / 各平台各自的 id)。于是**盘链闸挡下来的那次,永远查不到"上次的热度"**,
    判定退到"没法证明它涨了"、或者(改动前)按 0 算 ⇒ 任何正数都放行。

    盘链闸判的是「**同一份资源**」⇒ 基准必须按**同一份资源**取(见 `pan_key`)。
    """
    if session is None or hours <= 0 or hot_of is None:
        return {}
    from sqlalchemy import select

    from app.db.models import PushLedger

    scope = tuple(sections or (section,))
    cutoff = datetime.now() - timedelta(hours=float(hours))
    rows = session.scalars(select(PushLedger).where(
        PushLedger.user_id == user_id,
        PushLedger.section.in_(scope),
        PushLedger.pushed_at >= cutoff,
    ).order_by(PushLedger.pushed_at.asc(), PushLedger.id.asc())).all()
    out: dict[str, float] = {}
    for r in rows:                 # 升序遍历 ⇒ 后写的覆盖 = **最近一次**那条
        #: 身份与 `recent_pan_map` 同一把尺子:`pan_id`(源链优先),存量行回落 `pan_url`。
        k = (r.pan_id or "").strip() or pan_key(r.pan_url or "")
        if not k:
            continue
        try:
            m = json.loads(r.metrics or "{}")
        except ValueError:
            m = {}
        if not isinstance(m, dict):
            continue
        try:
            out[k] = _num(hot_of({"metrics": m, **m}))
        except Exception:  # noqa: BLE001 - 单行算不出来就跳过(基准缺失 = 挡,与下面同一口径)
            continue
    return out


def cross_section_seen(session: Session, user_id: int, section: str,
                       items: list[dict], hours: float = 72,
                       sections: tuple[str, ...] | None = None) -> dict[str, list[str]]:
    """★★ **跨板块已推过**(2026-11-25,档3):这份资源在**别的板块**被推过吗 → `{盘链身份: [板块…]}`。

    ## 它补的是哪个盲区

    `drop_recent` 的盘链闸只认**本 section**(不同板块的群不是一批人 —— 那是故意的)。
    于是同一份资源在一个板块推过之后,**换到另一个板块再推一遍完全不受拦**:
    近 7 天带盘 **1106 行**里,同一盘链在 **≥2 个板块**出现的有 **94 个盘链**;
    逐行按时间序算,**41.0%** 的带盘行是「它已经在别的板块推过了」。

    ## 为什么是**标注**,不是**拦**

    用户拍板(2026-11-25):「做降级标注」。直接静默丢会与客户的判据冲突 ——
    「**丢资源比重复推一遍严重得多**」(见 `_pan_id_of`);而每组人确实只在自己的群里,
    别的群收到过不等于这组人收到过。所以:**照推**,但卡面标出这件事,
    让客户自己决定这条要不要再点(也让我们事后能看出资源池是不是太集中)。

    ## 口径

    · **不看本板块的历史** —— 「跨板块」才算;本板块内的重复是 `drop_recent` 的事。
    · 窗口默认 **72h**,与 `push_pan_dedupe_hours` 同一个数:再久以前的推送对客户已经不算
      "同一条了"(而它与判重共用一把尺子,才不会出现"判重放过、标注却说推过"的矛盾)。
    · 身份与 `recent_pan_map` **同一把尺子**:`pan_id`(源链优先)∪ `pan_key(pan_url)` ——
      ⚠️ 必须**两个都查**:存量行没有 `pan_id`,而新行两个都有;只查一个会半边瞎。
    · 返回的板块名按**首次出现时间**排序(`drop_recent` 的调用方直接拿去渲染)。

    ⚠️ **没有盘链的条目不会出现在结果里** —— 没链就无从判"同一份资源"。
    """
    if session is None or not items or hours <= 0:
        return {}
    from sqlalchemy import select

    from app.db.models import PushLedger

    scope = tuple(sections or (section,))
    cutoff = datetime.now() - timedelta(hours=float(hours))
    rows = session.execute(
        select(PushLedger.section, PushLedger.pan_id, PushLedger.pan_url)
        .where(PushLedger.user_id == user_id,
               PushLedger.section.notin_(scope),
               PushLedger.pushed_at >= cutoff)
        .order_by(PushLedger.pushed_at.asc(), PushLedger.id.asc())).all()
    by_pan: dict[str, list[str]] = {}
    for sec, pan_id, pan_url in rows:
        sec = (sec or "").strip()
        for k in ((pan_id or "").strip(), pan_key(pan_url or "")):
            if not k or not sec:
                continue
            secs = by_pan.setdefault(k, [])
            if sec not in secs:
                secs.append(sec)
    out: dict[str, list[str]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        #: 与本函数查出来的键同一套(源链优先 ∪ 归一后的我方链)
        for k in (_pan_id_of(it), pan_key(_pan_of(it))):
            if k and k in by_pan:
                out[k] = by_pan[k]
    return out


def annotate_cross_section(items: list[dict], seen: dict[str, list[str]]) -> int:
    """把 `cross_section_seen` 的结果挂到条目上(`it["_cross_sections"]`)→ 标注条数。

    **改的是入参列表里的 dict**(原地),调用方直接拿去渲染。返回标注了几条 ——
    运行记录要报这个数,否则「这轮有没有跨板块重复」在日志里看不见。
    ⚠️ 同一条目命中两个身份(源链 + 我方链)时取**并集**,不是后覆盖:
    它们说的是同一份资源,合并才是"它去过哪些板块"。
    """
    n = 0
    for it in items or []:
        if not isinstance(it, dict):
            continue
        secs: list[str] = []
        for k in (_pan_id_of(it), pan_key(_pan_of(it))):
            for s in seen.get(k, []) if k else []:
                if s not in secs:
                    secs.append(s)
        if secs:
            it["_cross_sections"] = secs
            n += 1
    return n


def cross_section_note(secs: list[str]) -> str:
    """跨板块标注的**唯一文案出处**(各推送卡共用,别各写一句)。

    ⚠️ 口径:写「**已在 N 个板块推荐**」,不写"重复"——客户看到的是一次**提示**,
    不是一次认错;也不写板块英文键以外的内部术语(板块名走 `board_label`)。
    ⚠️ 卡片**标题**里不许带数字(那是冷却键的原料),这条文案只进正文,不进标题。
    """
    names = [s for s in (secs or []) if s]
    if not names:
        return ""
    from app.services.board_digest import board_label

    return "🔥 已在 %d 个板块推荐(%s)" % (
        len(names), " · ".join(board_label(s) or s for s in names))


def peak_metric(metrics) -> float:
    """一组互动指标里**最高的那个数字**(`{"liked_count": 53, "play_count": "1.2万"}` → 53)。

    给 `drop_recent` 的 `hot_of` 用:`metrics` 的键因平台而异(点赞/播放/评论/转发),
    取「最高的那项」当"这个资源有多热"的**粗代理** —— 与 `_heat()` 同一套思路。
    ⚠️ 值可能是 `"1.2万"` 这种**字符串**(各源格式不一),必须过 `_num()` 再比;
    直接 `max()` 会拿 str 与 int 比较、在 py3 里当场 `TypeError`。
    """
    if not isinstance(metrics, dict):
        return 0.0
    return max((_num(v) for v in metrics.values()), default=0.0)


def last_metrics(session: Session, user_id: int, section: str,
                 hours: float = 24,
                 sections: tuple[str, ...] | None = None) -> dict[str, dict]:
    """窗口内**已被推过的条目** → 它**上一次**记进台账的 `metrics` dict。

    ★★ 2026-11-18(档2 前置):`drop_recent` 的 `hot_of/hot_min` 判的是**绝对值**
    —— 而"同资源内容数"这个绝对值在 presence 链上**恒远高于门槛**:
    实测同一份资源的内容数是 20~99,而 `push_hot_bypass_count` 默认 **3**
    ⇒ 那道"流量好就放行"的口子**永远成立**,盘链去重实际全部失效。
    后果(实测近 30 天):知乎把同一份资源推了 **162 次**、B站 61 次、贴吧 25 次。

    正确的语义是「**比上次推它时更火了**」,不是「绝对值高」——
    所以要把上一轮的热度读回来做比较。键与 `item_key` 一致(去重按同一把尺子)。
    """
    if session is None or hours <= 0:
        return {}
    from sqlalchemy import select

    from app.db.models import PushLedger

    scope = tuple(sections or (section,))
    cutoff = datetime.now() - timedelta(hours=float(hours))
    rows = session.scalars(select(PushLedger).where(
        PushLedger.user_id == user_id,
        PushLedger.section.in_(scope),
        PushLedger.pushed_at >= cutoff,
    ).order_by(PushLedger.pushed_at.asc(), PushLedger.id.asc())).all()
    out: dict[str, dict] = {}
    for r in rows:                     # 升序遍历 ⇒ 后写的覆盖 = **最近一次**那条
        key = r.item_key or r.title or r.url or f"#{r.id}"
        try:
            m = json.loads(r.metrics or "{}")
        except ValueError:
            m = {}
        if isinstance(m, dict):
            out[key] = m
    return out


def _num(v) -> float:
    """把 `metrics` 里的值(可能是 "1.2万"/int/str)转成数字;转不了按 0。"""
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v or "").strip().replace(",", "")
    if not t:
        return 0.0
    mult = 1.0
    if t.endswith("万"):
        mult, t = 10000.0, t[:-1]
    elif t.endswith("亿"):
        mult, t = 100000000.0, t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return 0.0


def _rise_min_abs_of(settings, section: str) -> float:
    """这个板块的**绝对增量下限**(见 `HOT_RISE_MIN_ABS` 与 `hot_rise_min_abs`)。

    顺序:**配置覆盖** > 代码标定表 > `0`(不启用)。
    `push_hot_rise_min_abs` 的格式是 `板块:数值,板块:数值` —— 只为**不改代码就能调**
    而存在(标定表在代码里,是因为它带着数据出处;配置里再抄一份必然飘)。
    ⚠️ 解析失败**不抛**:一个写错的配置不该让整条推送链挂掉 ——
    退回标定值(那才是默认行为),但要 `warning` 出来,别让"配了没生效"静默过去。
    """
    raw = str(getattr(settings, "push_hot_rise_min_abs", "") or "").strip() if settings else ""
    if not raw:
        return float(HOT_RISE_MIN_ABS.get(section, 0))
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, val = part.partition(":")
        if not sep:
            continue
        if name.strip() != section:
            continue
        try:
            return float(val.strip())
        except ValueError:
            logger.warning("push_hot_rise_min_abs 里 %r 的值解析不出数字 ⇒ 用标定值", part)
            return float(HOT_RISE_MIN_ABS.get(section, 0))
    return float(HOT_RISE_MIN_ABS.get(section, 0))


def drop_recent(session: Session, user_id: int, section: str, items: list[dict],
                settings=None, hours: float | None = None,
                hot_of=None, hot_min: float = 0,
                pan_hours: float | None = None,
                hot_rise_only: bool = False,
                hot_rise_pct: float | None = None,
                hot_rise_min_abs: float | None = None,
                by_group: bool = False,
                in_batch: bool = True) -> tuple[list[dict], int]:
    """按**推送台账**去掉最近已经推过的条目 → `(保留, 去掉的条数)`。

    ★ 2026-10-29 数据驱动的决定:12 天台账里同一 item 在 **24 小时内重推**的比例
      —— B站 **53%**、抖音 **62%**、微博 43%、小红书/快手 38%、贴吧 33%。
      这既是"群信息杂乱"的主因,也在白烧 AI 筛选的钱(同一句话反复判)。
    ⇒ 推送前统一按 `push_dedupe_hours`(默认 24h,0=关)去重,**同一板块内**才去重
      (不同板块的群不是一批人,跨板块去重会把别的群饿死)。

    ## ★★ 盘链级去重(`pan_hours`,2026-11-17)

    `item_key` 是**内容的身份**(那条视频/那篇文章),但客户拿到手的是**那份资源**。
    同一份资源会被不同号、不同文章各推一遍 —— 近 7 天实测:带盘推送 1979 行只涉及
    **378 个不同盘链**(81% 的行是重复),最极端的 5 个盘链被 **7 个板块各推一遍**。

    ⇒ 再加一道**按盘链身份**的闸:同一板块内,同一个盘链身份在 `pan_hours`
      小时内已经推过就不推。默认继承 `push_dedupe_hours`,可用
      `push_pan_dedupe_hours` 单独调(**它比内容去重该更长**:同一份资源隔一天再推
      对客户仍然是同一条)。

    ## ★★ 盘链身份的口径(2026-11-19 改):源链优先,我方链要过标题判据

    原来只有一把尺子(`pan_url`,也就是**我方链**)。实测它**不唯一标识资源**:
    310 条我方链里 67 条(21.6%)挂着 >1 个不同源链 —— `7728b469a574` 在资源库里
    登记的是「儿童涂色填色模板大全」,台账上却挂着公考答题卡 / 作文稿纸 / 2027 日历
    三份毫不相干的东西(`_enrich` 的「资源级复用」按标题近似匹配错配了链)。
    拿它判重,72h 重放会**挡掉 282 行真实的不同资源** —— 而客户的判据是
    「**带网盘链接 + 最新**」,**丢资源比重复推一遍严重得多**。

    ⇒ 现在两条判据并行(见 `_pan_id_of` / `recent_pan_map`):
      ① **源链身份**(`pan_id`,源链优先)命中 ⇒ 挡。同一源链就是同一份资源,**零误伤**;
      ② **我方链身份**命中 ⇒ **还要**标题过 `_same_resource_title` 才挡。这一档用来
         压住"5 个号各转存一份同一 PDF"的副本(它们源链各不相同),但只在标题确认
         同源时生效,不会重演上面那 282 行。

    ⚠️ **两道闸共用 `hot_of` 的放行口径**:流量真的爆了的,重复也要推。
    ⚠️ 没有盘链的条目(身份为空)只走内容去重 —— 没盘链就无从判"同一份资源"。

    ## ★★ `hot_rise_only`(2026-11-18,档2 前置)

    默认 `False` = 旧行为(绝对门槛)。**但在 presence 那条链上绝对门槛是个笑话**:
    实测同一份资源的"同资源内容数"是 20~99,而门槛默认 **3** ⇒ 每次判重都命中豁免,
    盘链去重**一次都没生效过**(近 30 天知乎把同一份资源推了 162 次)。

    `True` = 只有当 `hot_of(item)` **严格大于上一轮推它时的热度**才放行。
    这才是「**又火了**」的正确语义 —— 热度原地不动、或者只是绝对值高,都不该再推一遍。
    比较基准取 `last_metrics()`(窗口内该条目**最近一次**的 metrics)。

    ## ★★ `hot_rise_pct`(2026-11-19):只认「涨了」还不够,涨多少得有个下限

    `hot_rise_only` 上线后实测(7 天台账逐条重放,按 `(section, item_key)` 比前后两次):

      · bilibili 里 99 次"涨了"的重复推送,**涨幅全在 10% 以内**;贴吧 32 次里一次
        涨幅超过 50% 都没有。典型:`pan.quark.cn/s/8fa4f71f7430` 一天推 4 次,
        播放 `99893 → 100194 → 100382 → 100957`(每次 +0.1%~0.6%),`6473d35f2aba`
        也是 `466 → 468 → 471 → 478`。这些是**同一个老资源被同一批账号反复顶到**
        (同资源内容数在涨),不是"资源火了"。
      · 真爆的涨幅是另一个量级:同窗口有 `play_count 17160 → 226451 → 1001939`。

    ⇒ 涨幅必须**同时**满足 `hot-prev > 0` 与 `(hot-prev)/prev >= hot_rise_pct`。
    默认值由 `push_hot_rise_pct`(**30%**)给出;显式传 `0` = 关掉**比例**这一档。
    ⚠️ 比例与**绝对增量**(`hot_rise_min_abs`)是**两个独立开关**:
    传 `hot_rise_pct=0` 只是不再要求百分比,**绝对下限仍按 `hot_rise_min_abs` 生效**。
    要完全退回"涨了就算"的老行为,**两个都得传 0** —— 否则会撞上本仓那族
    「看着有逃生门、其实没打开」。
    ⚠️ 基准 `prev<=0`(上次没有互动数据)时按"**没法证明它涨了**"处理 ⇒ 挡;
    原来那种把 prev 当 0、于是任何正数都放行的写法,正是本参数要堵的口子。

    ## ★★ `hot_rise_min_abs`(2026-11-25):**比例尺在小量级板块上是错的**

    按 30 天台账逐条重放(生产口径:盘链身份优先、`item_key` 兜底),把"涨幅 ≥30%"
    放行的那批再按**绝对增量**分档,**双峰不再是"涨/没涨",而是"涨了几个数"**:

      · `tieba` 过 30% 的 38 条里 **34 条增量 <20**(典型 `1→16`、`2→16`、`14→19`)——
        贴吧的 `total_replay_num` 中位数就 **2**,涨一个回复就是 +50%;
      · `wechat` 过 30% 的 428 条里,**35.6% 增量 ≤5**(典型 `2→3`、`1→2`)——
        公众号阅读数涨 3 个也算"又火了",而这条链的热度基数常在个位数;
      · `weibo_peer` 7 天内的噪音(涨幅 <5%)绝对增量 p99 = **7**、最大 **16**。

    反过来,同一批里真正的爆量差着好几个数量级:`bilibili 218676 → 2947509`、
    `kuaishou 421962 → 4218509`、`xiaohongshu 106 → 17000`。
    ⇒ **只抬比例阈值会把它们一起挡掉**(`277239→497704` 只涨 80% 但增量 22 万,
    是 bilibili 排前几的真爆),所以这里加的是**绝对增量下限**,不是更高的百分比。

    `None` ⇒ **按板块**查 `HOT_RISE_MIN_ABS`(表里没有 = 0 = 不启用这一档);
    显式传 `0` 同样是关掉。各板块的热度口径量级差 4 个数量级
    (`kuaishou` 播放百万 vs `tieba` 回复 2),一个全局常量不可能同时对。
    实测这一档在 30 天台账上多挡 **576 行(6.7%)**,其中 `wechat` 334 行、
    `xiaohongshu` 76 行、`bilibili` 62 行、`weibo_peer` 55 行、`tieba` 34 行、
    `douyin_peer` 15 行;`kuaishou` 不在表里(比例闸已挡干净)。

    ## ★★ `by_group`(2026-11-18)

    `False` = 去重范围是**本 section**(旧行为,大多数板块各管各的群,这是对的)。
    `True` = 范围放大到**与本 section 同一个飞书群的那几个 section**(见 `group_of`)。

    起因:`tieba` 与 `zhihu` 配的是**同一个专属群** ⇒ 客户在同一个群里看到
    「贴吧推一遍、知乎再推一遍」,而 section 级去重管不到。实测近 7 天有
    **10 个盘链在同一群里被两板块各推、涉及 166 行**(`item_key` 跨板块重复 0 条,
    ⇒ 只有盘链闸抓得到)。

    ⚠️ **别把 `by_group` 一律打开**:不同群之间跨板块去重会把别的群饿死 ——
    那正是 section 级去重的原始理由。只在**确认同群**时用(`group_of` 只认
    **显式配成同一个专属群**的;回落主群的那些不算)。

    ## ★★ `in_batch`(2026-11-19):同一批内部要不要互相挡

    `True`(默认)= 同一批里两条指向同一份资源,后一条也挡。**公众号那条链必须关掉**:
    产品硬要求是「监控号近 24h 发的文章**一篇不落**全到飞书」,而 `_enrich` 的
    「资源级复用」会把同一号连发的系列文(标题只差一位数字、**各自是不同的资源**)
    判成同一份、复用同一条我方链 —— 实测一次同步 4 篇新文被批内闸吃掉 3 篇。
    真正的刷屏来自**跨轮次的重复**(同一份资源三天推 17 次),那部分是 `seen_pan`
    (台账历史)管的,与本开关无关。
    """
    if settings is not None and hours is None:
        hours = float(getattr(settings, "push_dedupe_hours", 24) or 0)
    if hours is None:
        hours = 24.0
    if pan_hours is None:
        pan_hours = float(getattr(settings, "push_pan_dedupe_hours", hours)
                          if settings is not None else hours)
    #: ★ 涨幅下限:只在 `hot_rise_only` 分支用得到。`None` ⇒ 读配置(默认 30%);
    #: 显式传 `0` ⇒ 退回旧的「涨了就算」(调用方明确要放宽时才这么写)。
    #: ⚠️ `or 0.30` 会把显式传的 `0` 也吞成 30% —— 逃生门靠的是 `is None` 判断,不是 `or`。
    if hot_rise_pct is None:
        hot_rise_pct = (float(getattr(settings, "push_hot_rise_pct", 0.30))
                        if settings is not None else 0.30)
    #: ★★ 绝对增量下限(2026-11-25):`None` ⇒ **按板块**查表(`HOT_RISE_MIN_ABS`)。
    #: ⚠️ 显式传 `0` = 关掉这一档(与 `hot_rise_pct=0` 同样的逃生门语义)。
    #: ⚠️ 判据是 `is None` 而不是 `or` —— `or` 会把显式传的 `0` 吞成表里的值。
    if hot_rise_min_abs is None:
        hot_rise_min_abs = float(_rise_min_abs_of(settings, section))
    if not items:
        return [], 0
    #: 去重范围:默认本 section;`by_group` 时放大到**与本 section 同一个飞书群**的那几个
    #: (⚠️ `group_of` 只认**显式配成同一个专属群**的 —— 回落主群的不算,否则没配群的
    #: 板块会被判成"同群"、互相挡内容)。
    scope: tuple[str, ...] = (section,)
    if by_group and settings is not None:
        scope = group_of(settings, section) or (section,)
    #: ⚠️ **`session=None` 不等于"什么都不能判"**:同一批内部的盘链重复不依赖库。
    seen: set[str] = set()
    seen_pan: set[str] = set()          #: 源链身份(命中即挡,零误伤)
    seen_my: dict[str, str] = {}        #: 我方链身份 → 上次推它时的标题
    if session is not None:
        if float(hours) > 0:
            seen = recent_keys(session, user_id, section, float(hours), sections=scope)
        if float(pan_hours or 0) > 0:
            seen_pan, seen_my = recent_pan_map(session, user_id, section,
                                               float(pan_hours), sections=scope)
    if float(hours) <= 0:
        seen = set()               #: 内容去重关掉时,库里那份也不该生效
    #: ★★ **重复但"流量好"的仍要推**(2026-11-05 用户:「如果是重复的资源但是流量好也需要推出来」)。
    #: 只看时间窗会误杀"同一资源突然爆了"——那正是客户最想知道的。
    #: `hot_of(item)` 由各板块给(点赞/转发/同资源内容数…),`hot_min` 为门槛;
    #: 命中就**放行**并打标记,卡片上可以标 🔥 复推。
    kept: list[dict] = []
    _batch_pans: set[str] = set()
    #: `hot_rise_only` 的比较基准:窗口内该条目**上一次**推出去时的热度。
    #: 只在需要时才查(默认路径不付这份开销)。
    prev_metrics: dict[str, dict] = {}
    prev_hot_by_pan: dict[str, float] = {}
    if hot_rise_only and hot_of is not None and session is not None:
        #: 取**两个窗口里更长的那个** —— 盘链闸(默认 72h)比内容闸(默认 24h)长,
        #: 若按 24h 查基准,会出现"盘链判它是重复的、却查不到它上次的热度"⇒ 退回放行。
        _win = max(float(pan_hours or 0), float(hours or 0)) or 24.0
        prev_metrics = last_metrics(session, user_id, section, _win, sections=scope)
        #: ★★ 2026-11-19:**按盘链也要有基准**。实测公众号那条链上,同一份资源被
        #: 不同号各发一遍 ⇒ 每次的 `item_key` 都不一样(`art{id}`) ⇒ 按内容取基准
        #: 永远查不到"上次同一份资源的热度",于是盘链闸一挡、判定就走到"没法证明涨了"
        #: ⇒ 每小时重复进一次。盘链闸本来判的就是「**同一份资源**」,基准也必须按盘链取。
        prev_hot_by_pan = last_pan_hot(session, user_id, section, _win,
                                       sections=scope, hot_of=hot_of)
    #: 我方链那一档的批内记录:身份 → 本批第一次推它时的标题(判据同 `seen_my`)
    _batch_my: dict[str, str] = {}
    for it in items:
        key = _key_of(it)
        dup_item = key in seen
        title = _first(it, _TITLE_KEYS)
        #: ★★ **双判据**(2026-11-19,见 `_pan_id_of` 里的实测):
        #:   ① `pid`(源链优先)命中 ⇒ 同一份资源,**直接挡**;
        #:   ② 我方链命中 **且标题确认同源** 才挡 —— 我方链有 21.6% 挂着不同资源,
        #:      光看链会把书法字帖各年级、公考/作文/日历这种**真实的不同资源**互相挡掉。
        pid = _pan_id_of(it)
        pk = pan_key(_pan_of(it))
        #: 条目显式给了 `src_pan_url` 键 ⇒ 调用方区分了「我方链 / 源链」(只有公众号那样做)
        #: ⇒ 这两条链的可靠性不同,必须分档判(见 `_pan_id_of` 头注)。
        split_chains = _has_src_key(it)
        #: ★ 第一档:**真正的源链**命中 ⇒ 同一份资源,直接挡(零误伤)。
        #: ⚠️ `pid != pk` 这个条件不能省:源链为空时 `_pan_id_of` 会退回我方链,
        #: 那时 `pid == pk`,它是**我方链身份**,必须交给下面那档过标题判据。
        src_pid = pid if (pid and pid != pk) else ""
        dup_pan = bool(src_pid) and src_pid in seen_pan
        if not dup_pan and in_batch and src_pid and src_pid in _batch_pans:
            dup_pan = True
        if not split_chains and pk:
            #: 没区分两条链的板块(抖音对标号 / 资源库 / 发现链):`pan_url` **本来就是**
            #: 别人的源链、就是资源身份 ⇒ 直接挡(加标题判据会让它们漏挡)。
            dup_pan = dup_pan or pk in seen_pan or (in_batch and pk in _batch_pans)
        #: ★ 第二档(**我方链**):同一份资源被几个号各转存一份时源链各不相同(压不住),
        #: 但我方链会收敛到同一条。这一档**必须**标题确认「确实是同一份」才挡 ——
        #: 实测我方链有 21.6% 挂着不同资源,光看链会重演 282 行丢资源。
        if not dup_pan and pk:
            #: 库里那档:同一我方链上次推的标题,和这条**是不是同一份**
            prev_title = seen_my.get(pk)
            if prev_title is not None and _same_resource_title(title, prev_title):
                dup_pan = True
            #: 本批那档:同一条我方链的系列文**不许互相挡**(见 `in_batch` 的头注)
            elif in_batch and pk in _batch_my and _same_resource_title(title, _batch_my[pk]):
                dup_pan = True
        if not dup_item and not dup_pan:
            kept.append(it)
            if pid:
                _batch_pans.add(pid)
            if not split_chains and pk:
                _batch_pans.add(pk)
            if pk and pk not in _batch_my:
                _batch_my[pk] = title
            continue
        if hot_of is not None and float(hot_min or 0) > 0:
            try:
                hot = _num(hot_of(it))
                if hot_rise_only:
                    #: ★ **只认"涨了"**:绝对值再高,只要没超过上次记的那个数,就还是同一条。
                    #: 比较基准也过**同一个 `hot_of`** —— 把上次的 metrics 包成 item 形状喂进去,
                    #: 两种取法(读 `it["metrics"]` / 读摊平字段)都能对上,口径不会各飘各的。
                    prev = prev_metrics.get(key) or {}
                    prev_hot = _num(hot_of({"metrics": prev, **prev})) if prev else 0
                    #: ★★ 2026-11-19:本次是**盘链闸**挡下来的 ⇒ 基准也要取**同一份资源**
                    #: 上次的热度(按 `pan_key`)。按 item_key 取会让"同一资源换了篇文章"
                    #: 永远拿不到基准(见 `last_pan_hot` 头注)。
                    #: ⚠️ **`dup_item` 优先**:同一条内容再推一遍时,基准就取它**自己**
                    #: 上次的热度(即使那行没记盘链)。只有"内容没推过、但这份资源推过"
                    #: 时才退到按盘链取基准 —— 别让后者盖掉前者。
                    if dup_pan and pk and not dup_item:
                        prev_hot = prev_hot_by_pan.get(pk, 0.0)
                    #: ★★ 2026-11-19:**涨多少也得有下限**。实测"涨了"的重复里
                    #: bilibili 99 次涨幅 <10%(播放 99893→100194 这种平台自然波动)、
                    #: 贴吧 32 次一次都没超 50% —— 它们不是"资源火了",是**同一条又来了一遍**。
                    #: `prev_hot<=0` ⇒ 没有可比基准 ⇒ 挡(原来按 0 算,任何正数都放行)。
                    if prev_hot <= 0:
                        continue
                    if hot <= prev_hot:
                        continue
                    if float(hot_rise_pct or 0) > 0 and \
                            (hot - prev_hot) / prev_hot < float(hot_rise_pct):
                        continue
                    #: ★★ 2026-11-25:**比例之外还要看绝对增量**。个位数基数上"涨 30%"
                    #: 只是多一个回复(`tieba` 6→23 / `wechat` 2→3),那不是又火了。
                    #: 只抬比例会把"涨 80% 但增量 22 万"的真爆一起挡掉,所以卡的是增量。
                    if float(hot_rise_min_abs or 0) > 0 and \
                            (hot - prev_hot) < float(hot_rise_min_abs):
                        continue
                elif hot < float(hot_min):
                    continue
                it["_dedupe_hot"] = True
                kept.append(it)
                if pid:
                    _batch_pans.add(pid)
                if pk and pk not in _batch_my:
                    _batch_my[pk] = title
            except Exception:  # noqa: BLE001 - 取热度失败就按"已推过"处理
                continue
    return kept, len(items) - len(kept)


def record(session: Session, user_id: int, section: str, items: list[dict],
           when: datetime | None = None) -> int:
    """把「这一轮**真的推出去**的东西」记进台账。返回写入条数。

    ⚠️ **由推送点自己调用**(它手上才有"推了哪几条");不要试图在 `FeishuClient`
    那一层做 —— 那里只有渲染好的卡片 JSON,从中反解出行结构比每个点加一行更脆。

    ⚠️ **写失败会让调用方的 session 进入「必须 rollback」状态**(锁冲突时)。
    本函数按纪律**不吞掉调用方未提交的数据**,所以这里只撤销保存点、返回 0;
    **调用方必须在继续用这个 session 之前 `rollback()`** —— 监听轮已按此修
    (`_listen_round` 的补推兜底、`_push_listen` 的落库前自愈),见 2026-10-19 的事故。
    """
    from app.db.models import PushLedger
    from app.db.tx import savepoint

    if not items or not section:
        return 0
    ts = when or datetime.now()
    rows: list[PushLedger] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            norm = _norm_item(it, section)
        except Exception as exc:  # noqa: BLE001 - 单条坏不许牵连整批
            logger.warning("推送台账:某条条目解析失败已跳过(%s): %s",
                           type(exc).__name__, str(exc)[:100])
            continue
        if norm:
            rows.append(PushLedger(user_id=user_id, pushed_at=ts, **norm))
    if not rows:
        return 0
    try:
        #: SAVEPOINT:台账写失败**只撤销这几行**,不许把调用方尚未提交的推送数据一起抹掉
        with savepoint(session):
            session.add_all(rows)
            session.flush()
    except Exception as exc:  # noqa: BLE001 - 旁路失败只留痕,绝不挡住推送
        logger.warning("推送台账写入失败(%s: %s)—— 本批 %d 条未入账,"
                       "当天的日结会少这一部分(这是**已知会显形**的代价,不是静默)",
                       type(exc).__name__, str(exc)[:110], len(rows))
        return 0
    logger.info("推送台账:%s 记入 %d 条", section, len(rows))
    return len(rows)


def _heat(metrics: str) -> int:
    """热度排序用的**粗**代理值(只有排序用途,不做展示)。

    ⚠️ 把播放量与点赞相加在量纲上确实不严谨 —— 但这里只回答「**同一板块内谁更值得看**」,
    不对客户暴露这个数,也不参与任何结算(结算走 `conversion.estimate`)。
    """
    try:
        m = json.loads(metrics or "{}")
    except ValueError:
        return 0
    if not isinstance(m, dict):
        return 0
    try:
        return sum(int(v) for v in m.values() if isinstance(v, (int, float)))
    except (TypeError, ValueError):
        return 0


def window(session: Session, user_id: int, section: str, since: datetime,
           until: datetime, top: int = 0) -> dict:
    """读一个窗口 → 按**资源身份**聚合后的清单。

    返回 `{"rows", "resources", "repeats", "items": [...]}`:
      · `rows` = 窗口内**推送行数**(同一资源推 3 轮就是 3);
      · `resources` = **不同资源个数**(聚合后)—— 两者都给,是因为
        「推了 30 行」与「有 30 个资源」是**两件事**,而客户抱怨的正是前者;
      · `repeats` = 被推 ≥2 次的那种资源有几个(等于「刷屏了多少」);
      · `items`:有互动数据的按**热度降序**排在前;**没有数据的按首次出现顺序沉底**。
    """
    from app.db.models import PushLedger

    rows = session.scalars(
        select(PushLedger).where(
            PushLedger.user_id == user_id,
            PushLedger.section == section,
            PushLedger.pushed_at >= since,
            PushLedger.pushed_at < until,
        ).order_by(PushLedger.pushed_at.asc(), PushLedger.id.asc())
    ).all()
    agg: dict[str, dict] = {}
    for r in rows:
        key = r.item_key or r.title or r.url or f"#{r.id}"
        g = agg.get(key)
        if g is None:
            agg[key] = {
                "item_key": r.item_key, "title": r.title, "url": r.url,
                "pan_url": r.pan_url, "pan_type": r.pan_type, "author": r.author,
                "metrics": r.metrics, "pushes": 1,
                "first": r.pushed_at, "last": r.pushed_at,
            }
            continue
        g["pushes"] += 1
        g["last"] = r.pushed_at
        #: 后来的行**只补空位**,不覆盖 —— 先出现的那份信息通常是"第一次发现它"时的原话
        for f in ("title", "url", "pan_url", "pan_type", "author", "metrics"):
            if not g.get(f) and r.__getattribute__(f):
                g[f] = r.__getattribute__(f)
    items = list(agg.values())          #: 字典保序 = **首次出现顺序**(rows 已按 pushed_at asc 取)
    #: ★★ 2026-10-26 **排序改成热度优先**(用户口径:「每日总结里按热度排序,让用户更好的观看」)。
    #: 原来是 `pushes` 主导 —— 但 `pushes` 是**我们自己推送了几次**(自家行为),
    #: 不是"这件事有多火";客户翻日结想看的是「昨天什么在火」,不是「我们重复推了谁」。
    #:
    #: ★ 同日订正(用户口径):**有数据的按热度排,没数据的按原顺序排**。
    #: 贴吧/部分源本来就没有点赞播放(`_heat` 恒 0)—— 把它们也丢进 heat 排序里比,
    #: 等于拿"没有的指标"编出一个次序(先被 `pushes` 决定、再被标题拼音决定),
    #: 那个次序**没有任何含义**。所以:热的排前面按热度降序;**没有数据的整体沉底,
    #: 内部保持首次出现的顺序**(谁先被发现谁在前)。
    hot = [g for g in items if _heat(str(g.get("metrics") or "")) > 0]
    cold = [g for g in items if _heat(str(g.get("metrics") or "")) <= 0]
    hot.sort(key=lambda g: (-_heat(str(g.get("metrics") or "")),
                            -int(g["pushes"]), str(g.get("title") or "")))
    items = hot + cold
    out = {"rows": len(rows), "resources": len(items),
           "repeats": sum(1 for g in items if int(g["pushes"]) > 1), "items": items}
    if top:
        out["items"] = items[:top]
    return out


def cross_section_summary(session: Session, user_id: int, since: datetime,
                          until: datetime, min_sections: int = 2,
                          top: int = 0) -> dict:
    """★★ **跨板块全景**(2026-11-18,档2):同一份**资源**(盘链)被几个板块各推了几次。

    ## 它回答的问题

    `window()` / `boards_with_content()` 都是**单板块**视角 —— 于是
    「同一份盘链在 8 个群里各推一遍」在内部**完全看不见**(每个板块的日结都只说自己
    那一份,看起来都很正常)。实测近 7 天:带盘 1910 行 / 382 个盘链,其中
    **82 个盘链跨 ≥2 个板块、涉及 1063 行**(最极端的一个进了 8 个板块)。

    ## 为什么只在**内部**用,不拿去合并客户群

    不同板块的群**不是一批人**(这是 `drop_recent` 只按 section 去重的原始理由),
    跨群合并会把别的群饿死。跨板块重复的正确用途有两条:
      · **同群的**几个板块(tieba/zhihu)⇒ 走 `drop_recent(by_group=True)` **真去重**;
      · **不同群的** ⇒ 只做**内部可见**(运营据此判断资源池是不是太集中),
        客户群照常各推各的。

    ## ⚠️ 没有盘链的行不参与聚合

    "同一份资源"的判据只能是**盘链身份**(`pan_key`)。`item_key` 是**内容**身份:
    实测跨板块重复 **0 条**(不同平台抄的是同一份资源,标题各不相同)——
    拿它聚合会得到一张恒空的表。

    返回 `{"rows","pan_rows","pans","cross","cross_rows","by_section"}`:
      · `rows` = 窗口内台账总行;`pan_rows` = 其中**带盘**的行;
      · `pans` = 不同盘链数;`cross` = 跨 ≥`min_sections` 个板块的盘链清单
        (按板块数降序、行数降序);`cross_rows` = 这些盘链占了多少行;
      · `by_section` = 各板块的 `{"rows","pans"}`(**pans 是去重后的**,行数会大于它)。
    """
    from app.db.models import PushLedger

    rows = session.scalars(
        select(PushLedger).where(
            PushLedger.user_id == user_id,
            PushLedger.pushed_at >= since,
            PushLedger.pushed_at < until,
        ).order_by(PushLedger.pushed_at.asc(), PushLedger.id.asc())
    ).all()
    agg: dict[str, dict] = {}
    by_section: dict[str, dict] = {}
    pan_rows = 0
    for r in rows:
        sec = r.section or ""
        s = by_section.setdefault(sec, {"rows": 0, "pans": set()})
        s["rows"] += 1
        k = pan_key(r.pan_url or "")
        if not k:
            continue
        pan_rows += 1
        s["pans"].add(k)
        g = agg.get(k)
        if g is None:
            agg[k] = g = {
                "pan_key": k, "title": r.title or r.item_key or "",
                "pan_type": r.pan_type or "", "url": r.url or "",
                "pan_url": r.pan_url or "", "sections": {},
                "total": 0, "first": r.pushed_at, "last": r.pushed_at,
            }
        g["sections"][sec] = g["sections"].get(sec, 0) + 1
        g["total"] += 1
        g["last"] = r.pushed_at
        #: 后来的行只补空标题 —— 与 `window()` 同一口径(先出现的那条是"第一次发现它"时的原话)
        if not g["title"]:
            g["title"] = r.title or r.item_key or ""
    cross = [g for g in agg.values() if len(g["sections"]) >= int(min_sections or 2)]
    cross.sort(key=lambda g: (-len(g["sections"]), -g["total"], g["title"]))
    out = {
        "rows": len(rows), "pan_rows": pan_rows, "pans": len(agg),
        #: ⚠️ `cross_total` / `cross_rows` 必须**在 `top` 截断之前**算好 ——
        #: 截断后 len(cross) 只是"列了几条",拿它当"有多少个"会把规模说小。
        "cross": cross, "cross_total": len(cross),
        "cross_rows": sum(g["total"] for g in cross),
        "by_section": {k: {"rows": v["rows"], "pans": len(v["pans"])}
                       for k, v in by_section.items()},
    }
    if top:
        out["cross"] = cross[:int(top)]
    return out


def boards_with_content(session: Session, user_id: int, since: datetime,
                        until: datetime) -> dict[str, int]:
    """窗口内**有台账的板块** → 各有多少行。

    ⚠️ 日结的板块清单**从这里来**,不写死在配置里:`section` 取的是**落地的那个群**,
    所以「每个板块都配上」= 那个群有内容就自动有日结;没内容的日子**不发空卡**。
    """
    from app.db.models import PushLedger

    rows = session.execute(
        select(PushLedger.section, PushLedger.id).where(
            PushLedger.user_id == user_id,
            PushLedger.pushed_at >= since,
            PushLedger.pushed_at < until,
        )
    ).all()
    out: dict[str, int] = {}
    for sec, _ in rows:
        if sec:
            out[sec] = out.get(sec, 0) + 1
    return out
