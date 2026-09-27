"""一次只读请求判定:微信读书**书架**里到底有没有"这个号今天有没有更新"的信号。

为什么要它:监听一轮要向微信读书问 81 号 × 2 次(cover + 列表 ≈ 162 次),而会话级额度
会被开头十几个号烧光(`/web/mp/articles` -2041 → -2014,见 `doc/operations.md` §9.2),
症状就是"只有 id 靠前的几个号能推多篇"。如果 `/web/shelf/sync` 的每个 `books[]` 条目
自带"最新文章时间/reviewId/未读条数",一轮就能压成 **1 次书架 + 只问命中的号**,
密度从 162 掉到 `1 + 2N`。

现有代码判不了这件事:`WereadClient.shelf()` 只保留 `bookId` 和标题,其余字段全部丢弃,
所以**没人看过原始 payload**。本脚本走同一个客户端、同一套请求头(生产发什么就探什么),
只打印**字段名 + 时间戳/计数类字段的值**,不输出 Cookie、不输出文章标题、不写库、不换凭据。

用法(在**线上那台有活 Cookie 的机器**上跑,和定点监听隔开几十分钟以上再做,
探测与生产共用同一把账号的会话额度):

    python scripts/probe_weread_shelf.py --user 1

Cookie 交接(2026-09-27 增):Cookie 也可以不经对话/不落库——把整串 Cookie 存进一个
**已被 gitignore 的文件**(如 `data/weread_cookie.tmp.txt`,一行纯文本),然后:

    python scripts/probe_weread_shelf.py --user 1 --cookie-file data/weread_cookie.tmp.txt

脚本只读该文件、不回显内容、不写库;跑完由调用方立即删除。注意 Cookie 必须复制自
**与运行脚本的机器同一网络出口**的浏览器(会话与出口 IP 绑定,换网络当场 -2012)。

判定口径(脚本自己会说):
- 出现形如 `updateTime`/`lastReviewId`/`unreadCnt` 且**跨号取值不同**、时间戳落在近几天
  → 书架可当"谁更新了"的粗筛,值得改造监听轮;
- 只有 `addTime`/`createTime` 这类**加书架时间**(全部停在关注那天)→ 书架不能降频,
  这条路到此为止,别再猜。

字段含义不靠第二次探测自证:脚本读本地库两套基线与候选时间戳逐号对照——
**主基线 = 文章自带 publish_at(库里最新一篇的发布时间,dajiala/列表路才有)**,
与候选字段同为"发布时刻"语义,不受断采、补采干扰:字段若真是最新文章发布时间,
它只会等于或晚于我们存过的最新一篇,旧超 1 天的号超出噪声即"字段跟不上发文"。
入库时间基线(created_at)只作背景:断采让它整体过期、补采让它晚于发布,
两个方向都会制造假不符(2026-09-27 首跑即栽在这里:基线停在 9.20,差点把
真信号 `lastChapterCreateTime` 误判成"加书架时间")。对不上则退出码 3,
意思是"字段有名无实,别拿它筛号"——用错判据的代价是漏推,比现在的限流严重。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")   # 微信号名带中文,GBK 控制台会 UnicodeEncodeError

from app.db.database import get_session_local  # noqa: E402
from app.services import wechat_monitor  # noqa: E402
from app.services.weread_client import WereadClient  # noqa: E402
from config.settings import get_settings  # noqa: E402

# 名字里带这些词段的字段,才可能是"有没有更新"的信号,值得看值;其余只看类型。
_SIGNAL_HINTS = ("time", "date", "review", "unread", "new", "count", "cnt", "num",
                 "last", "latest", "update", "notice", "read")


def _kind(value) -> str:
    if isinstance(value, dict):
        return f"dict({len(value)})"
    if isinstance(value, list):
        return f"list({len(value)})"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return f"str({len(value)})"
    return type(value).__name__


def _as_epoch(value) -> dt.datetime | None:
    """把像是 unix 秒/毫秒的整数转成日期,便于人眼判断"停在哪天"。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    n = int(value)
    for seconds in ([n] if 1_500_000_000 <= n <= 4_000_000_000 else
                    [n // 1000] if 1_500_000_000_000 <= n <= 4_000_000_000_000 else []):
        try:
            return dt.datetime.fromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _read_cookie_file(path: str) -> str:
    """读 Cookie 文件(一行纯文本);不回显内容,读不到/为空都只说结果不说原因细节。"""
    from pathlib import Path

    text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="探测书架 /web/shelf/sync 每号条目带不带更新信号")
    parser.add_argument("--user", type=int, default=1, help="租户 id(取其微信读书 Cookie)")
    parser.add_argument("--cookie-file", default="",
                        help="从该文件读 Cookie(一行纯文本,须在 gitignore 内)代替本地库;用完即删")
    parser.add_argument("--samples", type=int, default=4,
                        help="打印几个号的信号字段取值(以 bookId 标识,不打印标题)")
    args = parser.parse_args()

    settings = get_settings()
    db = get_session_local()()
    try:
        cookie = (_read_cookie_file(args.cookie_file) if args.cookie_file
                  else wechat_monitor._weread_cookie(db, args.user, settings))
        last_seen = _last_new_article_by_book(db, args.user)
        last_pub = _last_publish_by_book(db, args.user)
    finally:
        db.close()
    if not cookie:
        print("取不到微信读书 Cookie(weread):线上没配就先别探")
        return 1
    print(f"本地对照基线:发布时间基线(publish_at){len(last_pub)} 号 / "
          f"入库时间基线(参考){len(last_seen)} 号 —— 用于自证字段含义,不需第二次探测")

    try:
        # 走生产的 _get(同参数、同请求头):探的就是线上实际收到的那份返回
        payload = WereadClient(cookie)._get(
            "/web/shelf/sync", {"userVid": "", "synckey": 0, "lectureSynckey": 0})
    except Exception as exc:  # noqa: BLE001 - 探测脚本:只报异常类型/消息,不外传凭据
        print(f"书架请求失败:{type(exc).__name__}: {exc}")
        print("(若为 -2012/-2010,说明线上 Cookie 已死或出口 IP 变过,本轮不作数)")
        return 2

    books = payload.get("books") or []
    mp = [b for b in books if str(b.get("bookId") or "").startswith("MP_WXS_")]
    print(f"书架顶层键: {sorted(payload.keys())}")
    print(f"books[] 共 {len(books)} 条,其中公众号(MP_WXS_) {len(mp)} 条")
    if not mp:
        print("结论:书架里没有公众号条目 → 无从判定")
        return 0

    # 字段名并集 + 每个字段在多少号上非空、值是什么类型
    stats: dict[str, dict] = {}
    for item in mp:
        bid = str(item.get("bookId") or "")
        for k, v in item.items():
            s = stats.setdefault(k, {"kinds": set(), "present": 0, "samples": {},
                                     "epochs": [], "distinct": set()})
            s["kinds"].add(_kind(v))
            if v not in (None, "", 0, [], {}):
                s["present"] += 1
            if k != "bookId":
                # 判定"跨号是否不同"要看全量(只看前 4 个号会把差别看成一致);
                # 只留截断后的 repr 且封顶,142 号也不会撑爆内存。
                if len(s["distinct"]) <= 60:
                    s["distinct"].add(repr(v)[:40])
                if len(s["samples"]) < args.samples:
                    s["samples"][bid] = v
            ep = _as_epoch(v) if not isinstance(v, str) else _as_epoch(_num(v))
            if ep:
                s["epochs"].append(ep)

    print(f"\n{'字段':<22}{'类型':<18}{'非空号数':>8}  说明")
    signal_keys: list[str] = []
    for k in sorted(stats):
        s = stats[k]
        hint = ""
        if s["epochs"]:
            hint = f"时间戳范围 {min(s['epochs']):%Y-%m-%d} ~ {max(s['epochs']):%Y-%m-%d}"
        elif k == "reviewId" or "review" in k.lower():
            hint = "reviewId 类:可直接判断有无新文章"
        print(f"{k:<22}{'/'.join(sorted(s['kinds'])):<18}{s['present']:>8}  {hint}")
        if any(h in k.lower() for h in _SIGNAL_HINTS) and k != "bookId":
            signal_keys.append(k)

    print(f"\n候选『谁更新了』字段: {signal_keys or '(无)'}")
    for k in signal_keys:
        s = stats[k]
        print(f"  {k}: 跨号取值是否不同 = {len(s['distinct']) > 1}"
              f"(取值数 {min(len(s['distinct']), 60)}{'+' if len(s['distinct']) > 60 else ''}/"
              f"{len(mp)} 号)  样本 "
              f"{ {bid[-8:]: repr(v)[:34] for bid, v in list(s['samples'].items())[:args.samples]} }")

    today = dt.datetime.now().strftime("%Y-%m-%d")

    def _decisive(k: str) -> bool:
        """字段能不能当粗筛用:跨号取值不同,且(时间戳落在今天 或 本身就是 reviewId)。"""
        s = stats[k]
        if len(s["distinct"]) <= 1:
            return False
        if s["epochs"] and max(s["epochs"]).strftime("%Y-%m-%d") == today:
            return True
        # reviewId 变了 = 最新文章换了,不需要它是今天的时间戳
        return "review" in k.lower()

    usable = [k for k in signal_keys if _decisive(k)]
    dated = [k for k in usable if stats[k]["epochs"]]
    reviewish = [k for k in stats if "review" in k.lower() and stats[k]["present"]]
    if not reviewish:
        print("\n书架每号条目**不带 reviewId 类字段**(原设想的『reviewId 水位比对』没有比对对象)。"
              "粗筛要走时间戳水位:按号存「上次见到的最新发布时间」,发文才变、问过才前移;"
              "⚠️ 时间戳不含标题,命中的号仍要各调一次 cover 拿文章本体。")
    # 存原始 payload 供离线分析(data/ 已 gitignore;含标题,勿外传/勿提交)
    try:
        with open(os.path.join("data", "weread_shelf_payload.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        print("原始 payload 已存 data/weread_shelf_payload.json(本地/gitignored,含标题勿外传)")
    except OSError:
        pass
    # 自证:主基线 = 库里最新发布时间(publish_at,与字段同为发布时刻,不受断采/补采干扰);
    # 入库时间基线只作背景——断采让它过期、补采让它晚于发布,两个方向都制造假不符。
    corr = _correlate(mp, dated, last_pub, last_seen, args.samples)
    if usable:
        ok_keys = [k for k, v in corr.items() if v["ok"]]
        print(f"\n结论:**可用** —— {usable}"
              f"({('「' + '、'.join(ok_keys) + '」通过 publish_at 基线自证') if ok_keys else '形状像但自证未通过,先别接入门'})。"
              "监听轮可改成「1 次书架筛出有更新的号 → 只问这些号」,一轮密度从 162 降到 1+2N。")
        if not dated:
            print("注意:只有 reviewId 类字段时**不能只看它** —— 书架不含文章标题,命中的号仍要各调一次 cover"
                  "(省的是「没发文的那些号的白问」,不是省掉 cover),"
                  "且落地要按号存「上次见到的 reviewId」当水位。")
        if corr and not ok_keys:
            print("对照校验:**不通过** —— 字段与「库里最新发布时间」对不上(旧超 1 天的号过多),"
                  "它多半是加书架/笔记同步时间。**先别改监听轮**,把上面这张表带回来重新判。")
            return 3
        if corr:
            print("对照校验:**通过** —— 见上表。")
        else:
            print("对照校验:**本轮没做成**(没有带 publish_at 的文章可对照) → "
                  "结论只是「字段长得像」,别直接照它改监听轮。")
        return 0
    if signal_keys:
        print("结论:有名字像信号的字段,但**没有一个同时满足「跨号取值不同」+「时间戳落在今天或就是 reviewId」**"
              " —— 全停在同一天/同一值的大概率是加书架时间或笔记同步时间,"
              "不能当「谁今天更新了」用(上面「时间戳范围」那一列就是判据)。")
        return 0
    print("结论:书架每号条目只有书名/封面这类静态字段,不带更新信号 → 这条降频路走不通,"
          "不要为此改监听轮;降密度只剩「额度该轮到哪些号」那条人工待决(第 22 条)。")
    return 0


def _last_new_article_by_book(db, user_id: int) -> dict[str, dt.datetime]:
    """每个 weread_book_id → 该号最近一次「我们看见它发文」的入库时间(参考基线)。

    ⚠️ 它只能当**参考**:① 监听断采期间基线整体过期;② dajiala/同步会把**历史旧文**
    补采入库,入库时间天然晚于发布时间——拿它当真基线会把"字段=发布时间"误判成
    "书架比我们旧"。精确基线是 `_last_publish_by_book`(文章自带 publish_at)。
    """
    from sqlalchemy import func, select

    from app.db.models import WechatArticle, WechatBenchmark

    rows = db.execute(
        select(WechatBenchmark.weread_book_id, func.max(WechatArticle.created_at))
        .join(WechatArticle, WechatArticle.benchmark_id == WechatBenchmark.id)
        .where(WechatBenchmark.user_id == user_id, WechatBenchmark.weread_book_id != "")
        .group_by(WechatBenchmark.weread_book_id)).all()
    out: dict[str, dt.datetime] = {}
    for book_id, when in rows:
        if not book_id or not when:
            continue
        out[str(book_id)] = when if isinstance(when, dt.datetime) else dt.datetime.fromisoformat(str(when))
    return out


def _last_publish_by_book(db, user_id: int) -> dict[str, dt.datetime]:
    """每个 weread_book_id → 库里最新一篇的**发布时间**(publish_at,dajiala/列表路自带)。

    这是判定"字段是不是发布时间"的**主基线**:两边的语义都是发布时刻,不受断采、
    不受补采时点干扰——字段若是真的,它只会等于或晚于我们存过的最新一篇,
    (差 = 断采窗口里漏掉的新文),旧超 1 天才算"字段跟不上发文"。
    """
    from sqlalchemy import func, select

    from app.db.models import WechatArticle, WechatBenchmark

    rows = db.execute(
        select(WechatBenchmark.weread_book_id, func.max(WechatArticle.publish_at))
        .join(WechatArticle, WechatArticle.benchmark_id == WechatBenchmark.id)
        .where(WechatBenchmark.user_id == user_id, WechatBenchmark.weread_book_id != "",
               WechatArticle.publish_at.is_not(None))
        .group_by(WechatBenchmark.weread_book_id)).all()
    out: dict[str, dt.datetime] = {}
    for book_id, when in rows:
        if not book_id or not when:
            continue
        out[str(book_id)] = when if isinstance(when, dt.datetime) else dt.datetime.fromisoformat(str(when))
    return out


def _correlate(mp: list[dict], keys: list[str], last_pub: dict[str, dt.datetime],
               last_seen: dict[str, dt.datetime], samples: int) -> dict[str, dict]:
    """候选时间戳字段与本地「最新发布时间」基线的吻合度,顺带打印对照表。

    判定(主基线 = publish_at):真若是"最新文章发布时间",字段只会 **等于或晚于**
    我们存过的最新一篇(晚出的部分 = 断采窗口里漏掉的新文),旧超 1 天的号超出
    少量噪声即"字段跟不上发文",不能当粗筛。入库时间(created_at)基线只作参考:
    监听断采会让它整体过期、补采会让它晚于发布,两个方向都会制造假不符。
    """
    if not keys:
        return {}
    if not last_pub:
        print("\n(本地没有带 publish_at 的文章,主基线做不成 → 结论只能算「字段长得像」,不算验过)")
        return {}
    report: dict[str, dict] = {}
    for k in keys:
        pairs = []
        for item in mp:
            bid = str(item.get("bookId") or "")
            raw = item.get(k)
            field = _as_epoch(raw) if not isinstance(raw, str) else _as_epoch(_num(raw))
            pub = last_pub.get(bid)
            if field and pub:
                pairs.append((bid, field, pub))
        if not pairs:
            print(f"\n[{k}] 与 publish_at 基线没有重叠的号(书架 bookId 与库里对不上?)→ 无法对照")
            continue
        # 字段若 = 最新文章发布时间:lag = 存过的最新发布 - 字段,正常 ≤ 0(字段更新)
        # 或 ≈ 0(没发新文);正得离谱(书架比我们还旧超 1 天)的号超出噪声就不是发布时间。
        lags = sorted((pub - field).total_seconds() / 86400 for _, field, pub in pairs)
        diffs = sorted(abs(x) for x in lags)
        median_diff = diffs[len(diffs) // 2]
        staler = sum(1 for x in lags if x > 1)   # 书架比"存过的最新发布"还旧超 1 天
        fresher = sum(1 for x in lags if x < -0.02)  # 书架比我们新(断采窗口漏掉的新文,合理)
        # 能证伪「字段=发布时间」的只有"书架比我们还旧"这一个方向:监听断采期间漏掉的
        # 发文会让字段**整体比库里新**(中位数被它抬到好几天,方向合理,不是不符)——
        # 2026-09-27 首跑把这点当成不符,差点枪毙真信号,所以中位数只展示、不判生死。
        ok = staler <= max(2, len(pairs) // 20)
        report[k] = {"ok": ok, "n": len(pairs), "median_diff": median_diff,
                     "staler": staler, "fresher": fresher}
        print(f"\n[{k}] 可对照 {len(pairs)} 个号 | 相差中位数 = {median_diff:.2f} 天(断采漏文会抬高,仅展示)"
              f" | 书架比我们旧超 1 天 = {staler}(唯一可证伪方向,超出即不可用)"
              f" | 书架比我们新 = {fresher}")
        print("  样本(号 / 书架字段 / 本地最新发布): "
              + "  ".join(f"{b[-6:]}:{f:%m-%d %H:%M}/{p:%m-%d %H:%M}"
                          for b, f, p in sorted(pairs, key=lambda x: x[1], reverse=True)[:samples]))
        print(f"  → {'吻合:该字段就是/跟着最新文章发布时间走' if ok else '不吻合:别拿它当粗筛判据'}")
        # 参考基线(入库时间)的旧口径,只打印不判:断采/补采都会制造假不符
        pairs_seen = [(b, f, last_seen[b]) for b, f, _ in pairs if b in last_seen]
        if pairs_seen and last_seen:
            lags_seen = sorted((s - f).total_seconds() / 86400 for _, f, s in pairs_seen)
            stale_seen = sum(1 for x in lags_seen if x < -1)  # 书架比入库旧超 1 天(旧口径的 staler)
            print(f"  (参考:入库时间基线 {len(pairs_seen)} 号,书架旧超 1 天 = {stale_seen}"
                  "——含补采旧文/断采过期,只作背景不参与判定)")
    return report


def _num(value):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    sys.exit(main())
