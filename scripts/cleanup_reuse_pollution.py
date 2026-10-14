"""清掉**三盘互通复用**留下的错链(2026-10-14 事故的残留)。

## 事故是什么
`pan_discovery.already_have()` 原来**没有专指度护栏**:它拿标题里切出来的**通用短语**
(`无删减完整版` / `百度网盘资源免费下载安装`)回库匹配,而库里又**存着这些贴吧帖本身**
⇒ 命中它们、把**它们那条已经写错的 `our_url`** 又抄一遍 ⇒ **越滚越错**。
实测:`discovered_pan_links` 里 **428 条复用行,201 条最终指向同一份 PDF**
(一条《人生大事》的贴吧帖点开是「高性价比人生指南」)。客户卡片上就是这么露出去的。

## 判据(关键:**不用那个已被污染的字段**)
对每条复用行:
  ① 从标题里取**专指名**(`_specific_hint`,通用短语一律不算);
  ② 到**独立来源**里查「这条 `our_url` 到底是谁的链」——
     `wechat_articles.my_pan_urls`(监听/转存链写的)与 `xunlei_resources.share_url`(口令/扫盘写的),
     这两处都不是复用路径写的,所以没被这次污染;
  ③ 命中才算"这条链真属于这个资源" ⇒ **保留**;否则 ⇒ **清空 `our_url` 并打回 `pending`**
     (让发现链**重新正经转存一次** —— 修好的护栏不会再错配)。

⚠️ **只清我们自己写错的那一列**:`origin_url`/`title`/`author` 一个不碰。
⚠️ 清空前把值备份到 `data/_reuse_pollution_backup.json`(万一要翻旧账)。
⚠️ **不删行**:打回 `pending` 的行会被 `pan_discovery` 当"存量待办"继续处理(受它自己的额度)。

## 跑法
    python scripts/cleanup_reuse_pollution.py                  # 默认 dry-run:只报告
    python scripts/cleanup_reuse_pollution.py --apply          # 真清
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.db import get_session_local  # noqa: E402
from app.db.models import DiscoveredPanLink, WechatArticle, XunleiResource  # noqa: E402


def _independent_links(session, user_id: int) -> dict[str, list[str]]:
    """`我方链 → 它在独立来源里的标题们`(**不含**复用路径写的那一列)。"""
    out: dict[str, list[str]] = defaultdict(list)
    for link, title in session.execute(select(WechatArticle.my_pan_urls, WechatArticle.title)
                                       .where(WechatArticle.user_id == user_id)).all():
        for line in str(link or "").splitlines():
            u = line.strip().split(" ")[0]
            if u.startswith("http") and title:
                out[u].append(str(title))
    for link, title in session.execute(select(XunleiResource.share_url, XunleiResource.name)
                                       .where(XunleiResource.user_id == user_id)).all():
        u = str(link or "").strip()
        if u.startswith("http") and title:
            out[u].append(str(title))
    return out


# ---------------------------------------------------------------- 第二遍:按"链的权威主人"清

def ownership_pass(db, user_id: int, apply: bool = False) -> dict:
    """**按链的权威主人**清:一条链如果有"自洽主人"(`origin_url == our_url`,或文章的
    `pan_urls` 里就有它),那主人标题里的**专指名**就是这条链的权威名;**别的行**若用
    对不上的名字认领同一条链 ⇒ 错配 ⇒ 清空并打回 `pending`。

    ⚠️ 为什么不能"一刀切":标题里切出来的词**未必是资源名**(群消息里全是「先存后下」「全网最齐」
    这种推广词)。所以**只对"有权威主人"的链判定**,主人不明的链一律**不判定**(保守)。
    实测 736 条链里只有 **24 条**有权威主人 —— 判据收紧到这个程度才敢动数据。
    """
    from app.services.pan_discovery import _resource_name_hints, _specific_hint
    from app.db.models import XunleiGroupShare

    def _names(t: str) -> set:
        return {n for n in _resource_name_hints(t or "") if _specific_hint(n)}

    claims: dict[str, list] = defaultdict(list)
    all_claims: dict[str, list] = defaultdict(list)      # 含"主人候选"来源
    for row in db.scalars(select(XunleiGroupShare).where(
            XunleiGroupShare.user_id == user_id,
            XunleiGroupShare.our_url.isnot(None), XunleiGroupShare.our_url != "")).all():
        key = str(row.our_url).strip()
        item = (row, _names(row.title), str(row.origin_url or "").strip() == key)
        claims[key].append(item)
        all_claims[key].append(item)
    # ⚠️ **主人可能来自别处**:一条链的"权威名"要连**公众号文章**(`pan_urls` 里就有它)
    #    与**发现链**(`origin_url` 就是它)一起算 —— 只在本表里找主人会漏掉大半(实测 10 → 0)。
    for i, t, pu in db.execute(select(WechatArticle.id, WechatArticle.title,
                                      WechatArticle.pan_urls)
                               .where(WechatArticle.user_id == user_id)).all():
        for u in {x.strip() for x in str(pu or "").split() if x.startswith("http")}:
            all_claims[u].append((None, _names(t), True))
    for i, t, o in db.execute(select(DiscoveredPanLink.id, DiscoveredPanLink.title,
                                     DiscoveredPanLink.origin_url)
                              .where(DiscoveredPanLink.user_id == user_id)).all():
        if t:
            all_claims[str(o or "").strip()].append((None, _names(t), True))
    owners = {u: set().union(*[c[1] for c in cs if c[2] and c[1]]) for u, cs in all_claims.items()}
    clear: list = []
    for u, cs in claims.items():
        auth = owners.get(u) or set()
        if not auth:
            continue                       # 主人不明 ⇒ 不判定(宁可漏清也不误清)
        for row, names, own in cs:
            if (own and names & auth) or (names & auth):
                continue
            clear.append((row, list(auth)[:3], list(names)[:3]))
    print(f"有权威主人的链 {sum(1 for v in owners.values() if v)} 条;判定错配的行 **{len(clear)}** 条")
    for row, auth, names in clear[:10]:
        print(f"  id{row.id} {(row.title or '')[:26]!r} 链尾={str(row.our_url)[-22:]}")
        print(f"      权威名={auth} 这行的名={names}")
    if apply:
        backup = []
        for row, auth, names in clear:
            backup.append({"id": row.id, "title": row.title, "ours": row.our_url})
            row.our_url = ""
            row.status = "pending"
            row.message = "清掉错配的我方链(2026-10-14 归属审计);等重新正经转存"
        db.commit()
        Path("data/_reuse_pollution_backup.json").write_text(
            json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")
        print("已清空并打回 pending(备份同上一遍的备份文件)")
    return {"cleared": len(clear)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", type=int, default=1)
    ap.add_argument("--apply", action="store_true", help="真清(默认只报告)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ownership", action="store_true",
                    help="第二遍:按「链的权威主人」清错配(只动群分享那一列)")
    args = ap.parse_args()

    from app.services.pan_discovery import _specific_hint, _title_claims
    from app.services.pan_discovery import _resource_name_hints

    db = get_session_local()()
    try:
        if args.ownership:
            ownership_pass(db, args.user, apply=args.apply)
            return 0
        rows = list(db.scalars(select(DiscoveredPanLink).where(
            DiscoveredPanLink.user_id == args.user,
            DiscoveredPanLink.message.like("库里已有%"),
            DiscoveredPanLink.our_url.isnot(None), DiscoveredPanLink.our_url != "",
        ).order_by(DiscoveredPanLink.id)))
        todo = rows[:args.limit] if args.limit else rows
        print(f"复用行 {len(rows)} 条,本轮检查 {len(todo)} 条"
              f"({'只报告' if not args.apply else '**真清**'})")
        indep = _independent_links(db, args.user)
        print(f"独立来源里可核对的链:{len(indep)} 条")

        keep = clear = 0
        samples: list[str] = []
        backup: list[dict] = []
        for r in todo:
            names = [n for n in _resource_name_hints(r.title or "") if _specific_hint(n)]
            titles = indep.get(str(r.our_url or "").strip(), [])
            ok = any(_title_claims(t, n) for n in names for t in titles) if names and titles else False
            if ok:
                keep += 1
                continue
            clear += 1
            backup.append({"id": r.id, "origin_url": r.origin_url, "title": r.title,
                           "ours": r.our_url, "message": r.message})
            if len(samples) < 8:
                samples.append(f"  id{r.id} 标题:{(r.title or '')[:34]!r}\n"
                               f"        错配的链:{r.our_url}"
                               f"({('独立来源里没这条链' if not titles else '独立来源里的标题都不含该资源名')})")
            if args.apply:
                r.our_url = ""
                r.status = "pending"
                r.message = "清掉错配的复用链(2026-10-14);等发现链重新正经转存"
        if args.apply:
            db.commit()
            Path("data/_reuse_pollution_backup.json").write_text(
                json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n可核对(#独立来源里有这条链且标题点名):{keep}\n"
              f"要清掉(错配):{clear}")
        if samples:
            print("\n样例:")
            print("\n".join(samples))
        if args.apply:
            print("\n已清空并打回 pending;备份在 data/_reuse_pollution_backup.json")
        else:
            print("\n[dry-run] 什么都没写;要真清加 --apply")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
