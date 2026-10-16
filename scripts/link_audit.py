"""**链接体检**:我方的分享链**打不打得开**、**有没有货不对版**。

用户口径(2026-10-16):「检查一下链接资源是否都是对齐的有没有货不对版的情况,以及链接打开的情况」。

## 两道判据**必须分开报**
合在一起就再也分不清"链死了"和"链被配错了" —— 而这两件事的修法完全不同:

    ① **能不能打开** → 按盘探活(都只读):
         夸克 `sharepage/token` + `sharepage/detail`;迅雷 `GET /drive/v1/share`;
         百度 分享页校验 + 取分享元信息。
    ② **对不对得上** → 取分享里**顶层的名字**,与库里那条资源名做 `match_key` 归一化后
       **互含**比对 ⇒ 对不上就是**货不对版**
       (本仓最贵的那类:2026-10-14 抖音那条链就出过「点开拿到另一个资源」)。

⚠️ 判不了的**单独报「待人工看」** —— 不许默认判对(那是假绿),也不许默认判错(那是假红)。
⚠️ 探活有**请求成本**(夸克/百度都会记账),所以默认**抽样**、并在请求之间留间隔。

用法:
    python scripts/link_audit.py                 # 每个来源抽样 20 条
    python scripts/link_audit.py --n 50 --gap .8
    python scripts/link_audit.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

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
        return {"alive": True, "names": [str(x.get("file_name") or "") for x in items][:5]}

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
            return {"alive": True, "names": [str(f.get("name") or "")
                                             for f in (d.get("files") or [])][:5]}

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
    # 一条都对不上 ⇒ 把**分享里真实的名字**带出来,让人一眼判(判据是启发式的,别装成权威)
    return "货不对版", f"{match_key(names[0])[:30]} | 库里:{match_key(title)[:24]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20, help="每个来源抽样多少条(默认 20)")
    ap.add_argument("--gap", type=float, default=1.0, help="每次探活之间的间隔秒(默认 1)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default="data/platform.db")
    ap.add_argument("--sources", default="", help="只跑这些来源(逗号分隔,子串匹配);默认全跑")
    a = ap.parse_args()
    only = [x for x in (a.sources or "").split(",") if x]

    con = sqlite3.connect(a.db)
    pr = Prober()
    rows: list[dict] = []
    for label, table, name_col, link_col in SOURCES:
        if only and not any(o in label for o in only):
            continue
        q = (f"select {name_col}, {link_col} from {table} "
             f"where coalesce({link_col},'') like '%http%' order by id desc limit ?")
        try:
            got = con.execute(q, (a.n,)).fetchall()
        except sqlite3.Error as exc:
            print(f"⚠️ {label}: 查不动({exc})")
            continue
        for title, raw in got:
            url = _first_url(raw)
            if not url:
                continue
            p = pr.probe(url)
            verdict, why = _verdict(str(title or ""), p)
            rows.append({"source": label, "title": str(title or "")[:44], "url": url,
                         "platform": p.get("platform") or "", "verdict": verdict,
                         "why": str(why)[:44]})
            time.sleep(max(0.0, a.gap))
    con.close()

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    from collections import Counter

    by_v = Counter(r["verdict"] for r in rows)
    print(f"共探 {len(rows)} 条 | " + " | ".join(f"{k} {v}" for k, v in by_v.most_common()))
    for tag in ("打不开", "货不对版", "空分享", "待人工看"):
        bad = [r for r in rows if r["verdict"] == tag]
        if not bad:
            continue
        print(f"\n=== {tag}({len(bad)})===")
        for r in bad[:12]:
            print(f"  [{r['source']}/{r['platform']}] {r['title']}")
            print(f"      实际顶层: {r['why']}")
            print(f"      {r['url'][:88]}")
    ok = by_v.get("对齐", 0)
    print(f"\n对齐 {ok}/{len(rows)}" + ("" if rows else " (没抽到样本)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
