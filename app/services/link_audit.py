"""我方分享链的**链接体检**(2026-10-16):打不打得开 + 有没有货不对版。

## 两道判据**必须分开报**
合在一起就分不清"链死了"和"链配错了" —— 而这两件事修法完全不同。
完整说明(含判据为什么是四条并列、为什么两边各取两种归一化、2026-10-16 首轮实测数字)
见 `doc/链接体检.md`;**逻辑主体在下面,`scripts/link_audit.py` 只是它的命令行壳。**

⚠️ 本模块**只读**:只调各盘"取分享详情"那类接口,不转存、不分享、不删。
⚠️ 每次探活都是**一次平台请求**(夸克要两次)⇒ 定时作业用**小样本**(见 `link_audit_tick`)。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

#: (来源名, 表, 名字列, 链接列, 盘) —— 盘只用于显示;真实判定按 URL 认
SOURCES: list[tuple[str, str, str, str]] = [
    ("抖音线索", "douyin_leads", "title", "our_url"),
    ("公众号", "wechat_articles", "title", "my_pan_urls"),
    ("迅雷群转存", "xunlei_group_shares", "title", "our_url"),
    ("盘链发现", "discovered_pan_links", "title", "our_url"),
    ("迅雷盘同步", "xunlei_resources", "name", "share_url"),
]
_URL_RE = re.compile(r"https?://[^\s()\[\]|]+")


def _platform(url: str) -> str:
    u = (url or "").lower()
    if "quark.cn" in u:
        return "quark"
    if "baidu.com" in u:
        return "baidu"
    if "xunlei.com" in u:
        return "xunlei"
    return ""


def _first_url(raw: str) -> str:
    """`my_pan_urls` 那种一行里带提取码/标记的,只取**第一个 URL**。"""
    m = _URL_RE.search(raw or "")
    return m.group(0) if m else ""


class Prober:
    """按盘探活 + 取顶层名字。**只读**:只调列目录/取详情那类接口。"""

    def __init__(self) -> None:
        self._quark = None
        self._xunlei_cred = None
        self._baidu_ck = ""

    def _q(self):
        if self._quark is None:
            from app.db import get_session_local
            from app.services import pan_accounts as A
            from app.services.quark_transfer import QuarkTransfer

            db = get_session_local()()
            try:
                ck = A.cookie_for(db, "quark", user_id=1) or ""
            finally:
                db.close()
            self._quark = QuarkTransfer(ck)
        return self._quark

    def quark(self, url: str) -> dict:
        q = self._q()
        sid, pwd = q._parse_share(url)
        stoken = q._get_stoken(sid, pwd)
        items = q._list_share_files(sid, stoken)
        _is_dir = lambda x: bool(x.get("dir"))                                   # noqa: E731
        _name = lambda x: x.get("file_name")                                     # noqa: E731
        only = _only_dir_name(items, _is_dir, _name)
        out: dict = {"alive": True,
                     "names": [str(_name(x) or "") for x in items][:5],
                     "only_dir_name": only}
        # ★ **夸克能下探**(2026-11-18):`_list_share_files` 有 `pdir_fid`
        # ⇒ 单一目录时再列一层,把真实内容带回来 —— 这样 `穿成炮灰后…→ 获取链接`
        # 那种「容器名 vs 真实资源」才判得出,**而不是只能挂在「待人工看」上等人**。
        # ⚠️ 迅雷没这条路(没有 `pdir_fid` 参数),那边只能继续靠容器名判。
        # ⚠️ **要逐层下探到底**:实测有 `古装大剧 / 分类整理 / 文字类` 这种**连着两层**
        #    都是收纳目录的;只下探一层会把「第二层容器名」当成真实内容,硬判成货不对版。
        containers: list[str] = [only] if only else []
        cur = items
        for _ in range(3):
            fid = str(next((x.get("fid") for x in cur if x.get("fid")), "") or "")
            if not (only and fid):
                break
            try:
                sub = q._list_share_files(sid, stoken, pdir_fid=fid)
            except Exception as exc:  # noqa: BLE001 - 下探失败不该把整条判成坏链
                logger.info("链接体检:夸克分享下探失败(%s)", type(exc).__name__)
                sub = []
            sub_names = [str(_name(x) or "") for x in sub if _name(x)]
            if not sub_names:
                break
            cur = sub
            nxt = _only_dir_name(sub, _is_dir, _name)
            if not nxt:
                #: 到这一层**不再是单一目录** ⇒ 这就是真实内容,下探到底了
                out["inner_names"] = sub_names[:5]
                break
            containers.append(nxt)
            only = nxt
        if containers:
            out["container_names"] = containers[:4]
        return out

    def xunlei(self, url: str) -> dict:
        import requests

        from app.services import xunlei_transfer as xt

        if self._xunlei_cred is None:
            self._xunlei_cred = xt._credentials(cookie_key="xunlei") or {}
        cred = self._xunlei_cred
        sid, pwd = xt._extract_share_id(url)
        if not sid:
            return {"alive": False, "err": "解析不出 share_id"}

        def _once() -> dict:
            h = xt._drive_headers(xt._fresh_cred(cred))
            d = xt._json(requests.get(f"{xt._API}/drive/v1/share", headers=h,
                                      timeout=xt._TIMEOUT,
                                      params={"share_id": sid, "pass_code": pwd, "limit": "100",
                                              "pass_code_token": "", "page_token": "",
                                              "thumbnail_size": "SIZE_SMALL"}))
            st = str(d.get("share_status") or "")
            if st and st != "OK":
                return {"alive": False, "err": f"share_status={st}"}
            if str(d.get("error") or "") and not d.get("files"):
                return {"alive": False, "err": str(d.get("error"))[:60]}
            files = d.get("files") or []
            return {"alive": True,
                    "names": [str(f.get("name") or "") for f in files][:5],
                    #: 迅雷的分享接口**不支持指定目录**(没有 `pdir_fid` 那样的参数),
                    #: 所以这里只能标记「顶层是单一目录」,**无法下探** ——
                    #: 判定会在 `_verdict` 里转成「容器名待核」,而不是硬判货不对版。
                    "only_dir_name": _only_dir_name(
                        files,
                        lambda f: "folder" in str(f.get("kind") or "").lower(),
                        lambda f: f.get("name"))}

        # ⚠️ **必须走 captcha 重试**(2026-10-16 实测踩到):直接裸 GET 会回 `captcha_invalid`,
        #    于是 30 条迅雷链**全被报成"打不开"** —— 而那是**我们这边**没带有效 captcha,
        #    与链接本身死活无关。`_with_captcha_retry` 会顺手铸一枚新的再试(它本来就是自愈路径)。
        return xt._with_captcha_retry(_once, cred)

    def baidu(self, url: str) -> dict:
        from app.db import get_session_local
        from app.services import pan_accounts as A
        from app.services.baidupan_transfer import BaiduPanClient

        if not self._baidu_ck:
            db = get_session_local()()
            try:
                self._baidu_ck = (A.cookie_for(db, "baidupan", user_id=1) or "").strip()
            finally:
                db.close()
        if not self._baidu_ck:
            return {"alive": None, "err": "没配百度凭据"}
        c = BaiduPanClient(self._baidu_ck)
        m = re.search(r"/s/1([A-Za-z0-9_-]+)", url or "")
        if not m:
            return {"alive": False, "err": "解析不出 surl"}
        pwd = ""
        pm = re.search(r"pwd=([A-Za-z0-9]+)", url or "")
        if pm:
            pwd = pm.group(1)
        c._verify(m.group(1), pwd)
        _uk, _sid, _tok, files = c._share_meta(m.group(1))
        return {"alive": True, "names": [str(f.get("server_filename") or "")
                                         for f in (files or [])][:5]}

    def probe(self, url: str) -> dict:
        plat = _platform(url)
        if not plat:
            return {"alive": None, "err": "认不出是哪个盘", "platform": ""}
        try:
            out = getattr(self, plat)(url)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {str(exc)[:70]}"
            # ⚠️⚠️ **不许把"我们这边被限流/超时"报成"这条链死了"**(2026-10-16):
            #   那两件事**修法相反**(一个等、一个要重做链),而假红会训练人忽略整份报告。
            #   判据只看**错误文本**(各盘自己的限流措辞),命中就转成「待人工看」。
            if any(k in msg.lower() for k in ("频率", "限流", "too many", "429", "超时",
                                              "timeout", "timed out", "connection",
                                              "风控", "频繁", "验证码", "captcha")):
                return {"alive": None, "platform": plat, "err": f"{msg}(疑似限流/网络)"}
            return {"alive": False, "platform": plat, "err": msg}
        out["platform"] = plat
        return out


def _lcs(a: str, b: str) -> int:
    """最长公共**子串**长度(不是子序列)—— 名字加装饰后仍会留下连续的真名。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


#: 这些字符 **`isalnum()` 为真**(它们是 CJK 里的"字母"),但**在资源名里就是分隔符** ——
#: 实测分享名写成 `S_死丨刑丨将丨至丨2026丨率丨FLAC`,`丨` 把"死刑将至"逐字切开。
_SEP_CHARS = "丨|·•・、,，。;；:：~≈·"


def _loose(s: str) -> str:
    """只去**标点/空白/emoji**,**保留括号里的字**。

    ⚠️ 为什么还要这么一路:`match_key` 会把 `【…】` **连内容一起剥掉**(那是"回库匹配"
    的规矩,有它的道理),可括号里的内容**经常正是资源名本身** ——
    实测 `乔迁之喜【邀请函】高清电子版` 被剥成 `乔迁之喜高清电子版`,
    于是分享名 `乔迁邀请函` 就**认不出来**了(而它俩显然是同一份)。
    """
    return "".join(ch for ch in str(s or "").lower()
                   if ch.isalnum() and ch not in _SEP_CHARS)


def _aligned(title: str, name: str) -> bool:
    """这条链说的**是不是**这个资源。

    ⚠️ **判据要容得下"装饰"**(2026-10-16 实测校准):真实世界的同一份资源,
    名字上会被加 emoji / 分隔符 / 年份 / 后缀 —— 第一版只做「归一化后互含」,
    结果把**明明对得上**的也算成"货不对版"(实测 `对齐 0/15`,而肉眼一看大半是对的):

        库里   《死刑将至》全集4K1080P高清…
        分享内 S_死丨刑丨将丨至丨2026丨率丨FLAC丨…     ← 被 `丨` 隔开了
        库里   1000套+优质PPT模版（免费下载/可修改）
        分享内 2025年1000套优质PPT模板                 ← 多了年份、简体/繁体不同

    ⇒ 三条**并列**的判据(满足一条即算对得上):
      ① 归一化后**互含**;
      ② **最长公共子串 ≥ 4 字**(名字被分隔符切开时仍能留下连续的一段);
      ③ **字集互包**:标题的字 ≥70% 出现在分享名里(**或**反过来,分享名的字 ≥80%
         出现在标题里)。两个方向都要,因为资源名与库里的标题**谁是"长"那一边不固定**:
          `乔迁之喜【邀请函】…`(长标题) vs `乔迁邀请函`(短分享名) ⇒ 只有**反向**那条能认出来。
    ⚠️ 两条字集判据都带**长度下限**(≥4 字),否则 `合集`/`资源` 这种短词会乱命中
    (本仓那条老教训:裸包含会让 `无删减完整版` 命中一大堆标题)。
    ⚠️ **判错的代价不对称**:把"对得上"判成"货不对版"只是多一条待人工看的行;
    反过来会让人**以为链是好的**。所以这里宁严不宽,**每条非对齐都带原文供人复核**。
    """
    from app.services.resource_library import match_key

    #: **两边各取两种归一化**:`match_key`(剥括号、剥盘商尾巴 —— 回库匹配的规矩)与
    #: `_loose`(只去标点/分隔符、**保留括号内容**)。任一组合成立即算对得上。
    #: ⚠️ 名字那边也必须过 `_loose` —— 实测分享名常写成 `S_死丨刑丨将丨至丨2026丨率丨FLAC`,
    #: 而 `match_key` **不去掉 `丨`**,于是"死刑将至"被逐字切开、LCS 只剩 1 个字。
    t_vars = {v for v in (match_key(title or ""), _loose(title or "")) if v}
    n_vars = {v for v in (match_key(name or ""), _loose(name or "")) if v}
    if not t_vars or not n_vars:
        return False
    for t in t_vars:
        for n in n_vars:
            if t in n or n in t:
                return True
            if _lcs(t, n) >= 4:
                return True
            st, sn = set(t), set(n)
            if len(st) >= 4 and len(st & sn) / len(st) >= 0.7:
                return True
            if len(sn) >= 4 and len(st & sn) / len(sn) >= 0.8:
                return True
    return False


def _only_dir_name(items: list, is_dir, get_name) -> str:
    """分享顶层**只有一条、且它是目录**时返回那个目录名;否则返回 `""`。

    为什么要它:实测一个迅雷分享的顶层就一个叫 `文字类` 的文件夹(真实内容在下一层),
    而它被 **6 个不同标题**复用 ⇒ 体检报 6 条「货不对版」,占当轮 11 条里的一半多。
    `文字类` / `古装大剧` / `分类整理` 这种词是**收纳目录**,不是资源名,
    拿它跟标题比**注定对不上** —— 那是判据的盲区,不是链坏了。

    ⚠️ 只认「**单一目录**」这一种形状:多条目、或单条但是文件,对不上就是真的对不上。
    """
    if len(items) != 1:
        return ""
    one = items[0]
    return str(get_name(one) or "") if is_dir(one) else ""


#: 「顶层是单一目录、且目录名与标题对不上」这一档的标记。**公开名**(命令行壳要按它分段)。
#: ★ **必须与「我们被限流」那种「待人工看」分开**(2026-11-18):后者进 `_BAD` 就是**假红**,
#: 而前者**恰恰可能是真事故**(`《阳光姐妹淘》→ 中医古籍` 就是这么一种形状)。
CONTAINER_MARK = "[容器名待核]"


def _verdict(title: str, probe: dict) -> tuple[str, str]:
    """→ (判定, 说明)。判定 ∈ {对齐, 货不对版, 空分享, 打不开, 待人工看}。"""
    if probe.get("alive") is False:
        return "打不开", str(probe.get("err") or "")
    if probe.get("alive") is None:
        return "待人工看", str(probe.get("err") or "")
    names = [n for n in (probe.get("names") or []) if n]
    if not names:
        return "空分享", "分享里没有顶层条目"
    from app.services.resource_library import match_key, title_claims

    for n in names:
        if _aligned(title, n) or title_claims(title, n) or title_claims(n, title):
            return "对齐", n
    #: 已下探拿到的**真实内容名**(目前只有夸克能下探)。对得上就直接是「对齐」——
    #: 容器名叫什么已经不重要了(实测 `穿成炮灰后…` 顶层是 `获取链接`,内容在下一层)。
    inner = [str(n) for n in (probe.get("inner_names") or []) if n]
    for n in inner:
        if _aligned(title, n) or title_claims(title, n) or title_claims(n, title):
            return "对齐", n
    # ★★ **顶层只有一个「分类目录」时,不能判货不对版**(2026-11-18 实测)。
    # 实测那个迅雷分享顶层就一个叫 `文字类` 的文件夹,真实内容在下一层 ——
    # 而它被 **6 个不同标题**(抖音线索 4 + 公众号 2)复用,于是体检里报 6 条「货不对版」,
    # 占了当轮 11 条里的一半多。名字像 `文字类` / `古装大剧` / `分类整理` 这种,
    # 是**收纳目录**不是资源名,拿它跟标题比注定对不上。
    # ⚠️ 判据只放行**单一目录**这一种:多条目或单文件时对不上,仍然是真的对不上。
    only = probe.get("only_dir_name")
    #: `only_dir_name` 是**顶层**那个目录名;`container_names` 是下探走到底的整条链
    #: (只有夸克会给,迅雷恒缺)。文案里印全链路,人才看得出"中间隔了几层收纳目录"。
    containers = [str(c) for c in (probe.get("container_names") or []) if c]
    if only:
        # ⚠️ **不能一律放过**:那样会把**真事故**也埋进「判不了」里。实测同一天里
        # 既有 `《麦穗与羊》→ 文字类`(容器名,判不出对错),也有
        # `《阳光姐妹淘》→ 中医古籍`(这条 2026-10-16 就确认过是**真错配**)。
        # ⇒ 先拿**容器名**跟标题比一次:对得上(说明标题说的就是这层目录)算对齐;
        #   对不上就把两边都写出来 —— 判不了 ≠ 没问题,文案必须让人能动手查。
        for c in (containers or [only]):
            if _aligned(title, c) or title_claims(title, c) or title_claims(c, title):
                return "对齐", c
        # ★ **下探过就不再是「判不了」** —— 内容名与容器名都对不上,那就是**货不对版**。
        # 实测 `穿成炮灰后：不是反派吗？怎么要抱抱` → 容器 `获取链接`,而里面也不是它;
        # 那条链被 100+ 标题共用。只按容器名判的话它永远停在「待人工看」。
        if inner:
            chain = "→".join(match_key(c)[:12] for c in containers) or match_key(only)[:18]
            return "货不对版", (f"{match_key(inner[0])[:26]} | 库里:{match_key(title)[:24]}"
                                f" | 容器:{chain}")
        # ⚠️⚠️ **这一档必须带标记且必须进告警**(2026-11-18):它落「待人工看」是为了不改
        # 五档判定的口径,而 `_BAD` 里刻意不含「待人工看」(那档还混着"我们被限流")。
        # 于是实测 11 条里 `《阳光姐妹淘》→ 中医古籍` 这条**真事故**被静默丢出了告警 ——
        # 与"静默失败=假成功"同族:报告写着"待人工看",实际谁也不会去看。
        chain = "→".join(match_key(c)[:12] for c in containers)
        return "待人工看", (f"{CONTAINER_MARK} 顶层是单一目录「{chain or match_key(only)[:24]}」,"
                            f"与标题不符 | 库里:{match_key(title)[:24]};需人工核对")
    # 一条都对不上 ⇒ 把**分享里真实的名字**带出来,让人一眼判(判据是启发式的,别装成权威)
    return "货不对版", f"{match_key(names[0])[:30]} | 库里:{match_key(title)[:24]}"


def check_share_matches(url: str, title: str, settings=None,
                        prober: "Prober | None" = None) -> tuple[bool | None, str]:
    """**这条链里装的是不是这个资源** → `(是否对得上, 说明)`;判不了给 `None`。

    ★★ **2026-10-16:给"转存之后、入库之前"用的一道闸** —— 实测出过一次**真事故**:
    别人的**原链内容是对的**(`602ff5fc5865` 里就是「三千年等一口将军气（更至77集）」),
    而**我们转存后新建的分享里却是另一个资源(中医古籍)**;那条链被当 `our_url` 用了 **69 次**,
    还经「回库复用」扩散到 **68 个不同标题** ⇒ **客户点开会拿到完全不相干的东西**。
    ⇒ **不管上游是哪条子路径**(口令解析 / 转存取错 fid / 盘商抽风),这一道核对都拦得住。

    ⚠️ 判不了(限流/超时/认不出盘)返回 `None` —— 调用方**不许当成"对不上"**去丢链
    (那是拿"我们探不动"当"资源有问题");**对上才放行、对不上才拦、判不了照旧放行并留痕**。
    """
    t = str(title or "").strip()
    if not t:
        return None, "没给标题,跳过核对"
    try:
        pr = prober or Prober()
        p = pr.probe(url)
    except Exception as exc:  # noqa: BLE001 - 探不动不该毁掉转存本身
        return None, f"核对探不动:{type(exc).__name__}"
    verdict, why = _verdict(t, p)
    if verdict == "对齐":
        return True, why
    if verdict in ("打不开", "空分享", "待人工看"):
        #: 这几档**不是**"内容对不上" —— 交回调用方按原逻辑处理(它们各有各的处置)
        return None, f"{verdict}:{why}"
    return False, why


def audit_rows(db: Any, user_id: int = 1, *, per_source: int = 15, gap: float = 0.6,
               sources: tuple[str, ...] = (), prober: "Prober | None" = None) -> list[dict]:
    """按来源抽样探活 + 判对齐。返回每条一行,**不写库、不推消息**。"""
    from sqlalchemy import text

    pr = prober or Prober()
    out: list[dict] = []
    for label, table, name_col, link_col in SOURCES:
        if sources and not any(s in label for s in sources):
            continue
        sql = text(f"select {name_col}, {link_col} from {table} "
                   f"where user_id = :uid and coalesce({link_col}, '') like '%http%' "
                   f"order by id desc limit :n")
        try:
            got = db.execute(sql, {"uid": user_id, "n": per_source}).fetchall()
        except Exception as exc:  # noqa: BLE001 - 单表查不动不该毁掉整轮体检
            logger.info("链接体检:%s 查不动(%s)", label, type(exc).__name__)
            continue
        for title, raw in got:
            url = _first_url(str(raw or ""))
            if not url:
                continue
            p = pr.probe(url)
            verdict, why = _verdict(str(title or ""), p)
            out.append({"source": label, "title": str(title or "")[:44], "url": url,
                        "platform": p.get("platform") or "", "verdict": verdict,
                        "why": str(why)[:44]})
            time.sleep(max(0.0, gap))
    return out


#: 出问题才算"要人看";这三档是**链本身**的问题
_BAD = ("打不开", "空分享", "货不对版")
#: ⚠️ 「待人工看」**整体不进 `_BAD`** —— 那一档大部分是「我们自己被限流/超时」,
#: 算进去就是 `_BAD` 最怕的**假红**(见 `probe()` 里那段)。
#: 但里面的「顶层单一目录且目录名与标题不符」是**另一个物种**:它是**链本身**的可疑形状,
#: 实测 `《阳光姐妹淘》→ 中医古籍` 就是**真错配**。⇒ 靠 `CONTAINER_MARK` 标记**只捞这一小类**。


def _needs_eyes(row: dict) -> bool:
    """这一行要不要进告警:确凿的坏链,**加上**「容器名与标题不符」这一小类。"""
    return row["verdict"] in _BAD or CONTAINER_MARK in str(row.get("why") or "")


def bad_groups(rows: list[dict]) -> list[tuple[str, list[str], str, int]]:
    """要人看的行**按链聚合** → `[(url, [问题类型...], 说明, 命中行数)]`,行数多的在前。

    ★ **为什么必须聚合**(2026-11-18 实测):一条链被 N 个标题复用是常态 ——
    实测 `VP6XqobV5vdtY7KuROqmhouKA1` 一个分享被 **6 个抖音/公众号标题**共用,
    告警里就成了 6 行一模一样的东西,而 `_audit` 的告警只发前 8 行 ⇒
    **其余链被挤出告警**,读起来像"只有这一条有问题"。按链聚一次,
    一个坏分享只占一行,「影响 N 个标题」反而成了它的严重度。
    """
    from collections import defaultdict

    grouped: dict[str, dict] = defaultdict(lambda: {"titles": [], "kinds": [], "why": ""})
    for r in rows:
        if not _needs_eyes(r):
            continue
        g = grouped[r["url"]]
        g["titles"].append(str(r.get("title") or "")[:26])
        kind = r["verdict"]
        if CONTAINER_MARK in str(r.get("why") or ""):
            kind = "容器名待核"
        if kind not in g["kinds"]:
            g["kinds"].append(kind)
        g["why"] = g["why"] or str(r.get("why") or "")
    #: ⚠️ 说明里**剥掉标记前缀** —— 标记已经进 `kinds` 了,两边都带会读成两件事。
    out = [(u, g["kinds"], g["why"].replace(CONTAINER_MARK, "").strip(" ,;"), len(g["titles"]))
           for u, g in grouped.items()]
    #: 影响面大的排前面 —— 一条链挂 N 个标题的,比单行坏链更该先看
    out.sort(key=lambda x: -x[3])
    return out


def summary(rows: list[dict]) -> str:
    """一行给人看:各判定几个。"""
    from collections import Counter

    c = Counter(r["verdict"] for r in rows)
    #: 「待人工看」里**分两小类报**:容器名不符是要人动手的,限流是我们自己等一等就好。
    #: ⚠️ 混成一个数字时,后者会把前者淹掉(实测 11 条待人工看里 6 条是容器名、其余是限流)。
    container = sum(1 for r in rows if CONTAINER_MARK in str(r.get("why") or ""))
    manual = c.get("待人工看", 0) - container
    return (f"抽{len(rows)} 对齐{c.get('对齐', 0)}/"
            + " ".join(f"{k}{c[k]}" for k in _BAD if c.get(k))
            + (f" 容器名待核{container}" if container else "")
            + (f" 待人工看{manual}" if manual else "")
            # ⚠️ **「全对齐」只许在每一条都真的对齐时说**(2026-11-18):原来按"没有 `_BAD`"
            # 算,于是「3 条对齐 + 3 条没探成(限流)」也印 **全对齐** —— 拿"我们没看见"
            # 冒充"看见是好的",正是本仓那条「没看到≠不存在」的反面。
            + (" **全对齐**" if rows and all(r["verdict"] == "对齐" for r in rows) else ""))


def link_audit_tick(settings=None, *, per_source: int = 0) -> int:
    """定时:小样本体检 + 记运行记录;有坏链就推**管理群**(内容卡不看这个,运维要看)。

    **样本故意小**(默认取 `settings.link_audit_per_source` = 6 ⇒ 约 30 次探活):
    这是**日常盯趋势**用的,要全量排查请手动跑 `python scripts/link_audit.py --n 50`。
    """
    from config.settings import get_settings

    from app.db import get_session_local
    from app.db.models import User
    from app.services.feishu_client import FeishuClient, webhook_for
    from app.services.tenant_base import _record_run

    settings = settings or get_settings()
    n = int(per_source or getattr(settings, "link_audit_per_source", 6) or 6)
    db = get_session_local()()
    bad_total = 0
    try:
        for (uid,) in db.execute(__import__("sqlalchemy").select(User.id)
                                 .where(User.enabled.is_(True))).all():
            rows = audit_rows(db, uid, per_source=n)
            s = summary(rows)
            groups = bad_groups(rows)
            bad_total += sum(g[3] for g in groups)
            detail = s + (f" | 例:{groups[0][0][-18:]}" if groups else "")
            _record_run(db, uid, "link_audit", "success" if rows else "failed", detail[:900])
            db.commit()
            if groups and getattr(settings, "link_audit_alert", True):
                hook = webhook_for(settings, "admin")
                if hook:
                    #: ⚠️ **按链聚合后再截断**(见 `bad_groups`) —— 按行截断时,
                    #: 一条被 6 个标题共用的分享会占满 8 行里的大半,把别的链挤掉。
                    lines = "\n".join(
                        f"· {'/'.join(k)} 影响{n}个标题 | {why[:34]}\n  {url[:74]}"
                        for url, k, why, n in groups[:8])
                    FeishuClient(hook, getattr(settings, "feishu_secret", "")).send(
                        f"🔗 **链接体检**:{s}\n{lines}\n"
                        f"⚠️ 只是抽样;复跑:`python scripts/link_audit.py --n 50`")
    except Exception:  # noqa: BLE001 - 体检自己崩了不该影响调度
        logger.exception("链接体检作业失败")
        db.rollback()
    finally:
        db.close()
    return bad_total
