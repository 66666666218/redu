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

判定口径(脚本自己会说):
- 出现形如 `updateTime`/`lastReviewId`/`unreadCnt` 且**跨号取值不同**、时间戳落在近几天
  → 书架可当"谁更新了"的粗筛,值得改造监听轮;
- 只有 `addTime`/`createTime` 这类**加书架时间**(全部停在关注那天)→ 书架不能降频,
  这条路到此为止,别再猜。
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


def main() -> int:
    parser = argparse.ArgumentParser(description="探测书架 /web/shelf/sync 每号条目带不带更新信号")
    parser.add_argument("--user", type=int, default=1, help="租户 id(取其微信读书 Cookie)")
    parser.add_argument("--samples", type=int, default=4,
                        help="打印几个号的信号字段取值(以 bookId 标识,不打印标题)")
    args = parser.parse_args()

    settings = get_settings()
    db = get_session_local()()
    try:
        cookie = wechat_monitor._weread_cookie(db, args.user, settings)
    finally:
        db.close()
    if not cookie:
        print("取不到微信读书 Cookie(weread):线上没配就先别探")
        return 1

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
    if usable:
        dated = [k for k in usable if stats[k]["epochs"]]
        print(f"结论:**可用** —— {usable}"
              f"{'(时间戳落在今天)' if dated else '(reviewId 类:要跟上次见到的值比对新旧)'},"
              "监听轮可改成「1 次书架筛出有更新的号 → 只问这些号」,一轮密度从 162 降到 1+2N。")
        print("但落地前必须做一次对照校验:拿一个已知今天发过文的号 + 一个已知停更的号,"
              "确认该字段区分得开。否则会把「今天发了」读成「没更新」而漏推,"
              "那是飞书全推铁律级的错(宁问多余一次,不能少问一次)。")
        return 0
    if signal_keys:
        print("结论:有名字像信号的字段,但**没有任何一个落在今天** —— 大概率是加书架时间或"
              "笔记同步时间,不能当「谁今天更新了」用。想确认就换个时间点再跑一次,"
              "看这些时间戳会不会随公众号发文而移动。")
        return 0
    print("结论:书架每号条目只有书名/封面这类静态字段,不带更新信号 → 这条降频路走不通,"
          "不要为此改监听轮;降密度只剩「额度该轮到哪些号」那条人工待决(第 22 条)。")
    return 0


def _num(value):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    sys.exit(main())
