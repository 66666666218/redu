# -*- coding: utf-8 -*-
"""微信读书**账号池**的命令行入口:列/加/删/验。

## 什么时候会用到它
精确阅读数(`readNum`)只有一个来源 `GET /web/mp/articles`,而它的额度**按账号**记、
并且会被**账号级** `-2041` 限死(2026-10-18 证据链见 `app/services/weread_accounts.py`)。
一旦手上这枚账号被限,阅读数就整体归零 —— 唯一能扩容的办法是**再加一个微信读书账号**。

## 怎么拿到一枚新 cookie(人只做这一步)
1. 浏览器无痕窗口打开 <https://weread.qq.com/>,用**另一个微信号**扫码登录;
2. F12 → Network → 任一 `weread.qq.com` 请求 → 复制请求头里的 `Cookie` 整串;
3. `python scripts/weread_accounts.py add --key weread#2 --cookie "<粘贴>"`
   (也可以 `--cookie-file cookie.txt`,或省略 `--cookie` 让它从标准输入读)。

脚本会**先验证再保存**:`shelf`(认证)+ `/web/mp/articles`(额度)各打一次只读请求。
不通过就不写库 —— 存一枚被限的凭据只会让下一轮多一次必然失败的请求。

## 用法
    python scripts/weread_accounts.py list          # 看现有账号 + 熔断状态
    python scripts/weread_accounts.py add --key weread#2 --cookie "<...>"
    python scripts/weread_accounts.py probe --key weread#2   # 只验不打库
    python scripts/weread_accounts.py rm --key weread#2

⚠️ 全程**不打印 cookie**(只打印长度与掩码),也不改基础账号的续期逻辑。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")   # 结论里带 →,GBK 控制台会 UnicodeEncodeError

from sqlalchemy import text  # noqa: E402

from app.db.database import get_session_local  # noqa: E402
from app.services import wechat_monitor  # noqa: F401,E402  (先入门面,避免循环导入)
from app.services.cookie_store import delete_cookie, get_cookie, set_cookie  # noqa: E402
from app.services.weread_accounts import (  # noqa: E402
    BASE_KEY, account_keys, build, web_scope,
)
from app.services.weread_client import WereadAuthError, WereadClient  # noqa: E402


def _mask(cookie: str) -> str:
    return f"len={len(cookie)} {cookie[:8]}…" if cookie else "(空)"


def _sample_book_id(session, user_id: int) -> str:
    return str(session.execute(text(
        "select weread_book_id from wechat_benchmarks "
        "where user_id=:u and weread_book_id like 'MP_WXS_%' and active=1 limit 1"),
        {"u": user_id}).scalar() or "")


def probe(cookie: str, book_id: str) -> tuple[bool, str]:
    """两发只读请求:`shelf`(认证)+ `mp/articles`(额度)。返回 (能不能用来拿阅读数, 说明)。"""
    c = WereadClient(cookie)
    try:
        n = len(c.shelf_entries())
    except WereadAuthError as exc:
        return False, f"登录态无效:{str(exc)[:90]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"shelf 失败:{type(exc).__name__}: {str(exc)[:90]}"
    if not book_id:
        return True, f"登录态正常(书架 {n} 个号);没有可试的 bookId,未验列表额度"
    try:
        payload = c.mp_articles(book_id, 0, 20)
        items = c.flatten_mp_articles(payload)
    except WereadAuthError as exc:
        return False, f"列表接口登录态无效:{str(exc)[:90]}"
    except Exception as exc:  # noqa: BLE001
        return False, (f"**登录态正常**(书架 {n} 个号),但列表接口不可用:"
                       f"{str(exc)[:110]} ⇒ 这枚账号拿不到精确阅读数(多半是额度被限)")
    reads = sum(1 for it in items if int(it.get("read_num") or 0) > 0)
    return True, f"shelf {n} 个号;列表 {len(items)} 篇(带阅读数 {reads} 篇)⇒ **可用于精确阅读数**"


def cmd_list(session, user_id: int) -> int:
    pool = build(session, user_id)
    print(f"微信读书账号池(用户 {user_id}):{len(pool)} 个")
    for key in account_keys(session, user_id):
        cookie = get_cookie(session, user_id, key) or ""
        blocked = ""
        try:
            from app.services import weread_budget

            until = weread_budget.blocked_until(session, user_id, web_scope(key))
            blocked = f" 熔断至 {until:%m-%d %H:%M}" if until else ""
        except Exception:  # noqa: BLE001
            blocked = ""
        print(f"  - {key:<12} {_mask(cookie)}{blocked}")
    if len(pool) <= 1:
        print("\n⚠️ 只有 1 个账号 —— 它被 -2041 限掉之后阅读数就会整体归零。"
              "\n   加一个:`python scripts/weread_accounts.py add --key weread#2 --cookie \"<...>\"`")
    return 0


def _read_cookie(args) -> str:
    if args.cookie:
        return args.cookie.strip()
    if args.cookie_file:
        with open(args.cookie_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    print("把 Cookie 整串粘进来,回车结束:", file=sys.stderr)
    return sys.stdin.readline().strip()


def cmd_add(session, user_id: int, args) -> int:
    key = (args.key or "").strip()
    if not key.startswith("weread") or key.startswith("weread_app"):
        print("❌ `--key` 必须是 `weread` 或 `weread#2` / `weread#3` …")
        return 2
    if key == BASE_KEY and not args.force:
        print("❌ 覆盖基础账号会**换掉正在用的那枚**;确认要换就加 `--force`。\n"
              "   加一个新账号请用 `--key weread#2`。")
        return 2
    cookie = _read_cookie(args)
    if not cookie:
        print("❌ 没读到 cookie")
        return 2
    ok, why = probe(cookie, _sample_book_id(session, user_id))
    print(("✅ " if ok else "❌ ") + why)
    if not ok and not args.force:
        print("   未写库(加 `--force` 可以强行存下,但通常没有意义)")
        return 1
    set_cookie(session, user_id, key, cookie)
    print(f"已保存到 `{key}`({_mask(cookie)})")
    return 0


def cmd_probe(session, user_id: int, args) -> int:
    cookie = _read_cookie(args) if (args.cookie or args.cookie_file) else (
        get_cookie(session, user_id, args.key or BASE_KEY) or "")
    if not cookie:
        print(f"❌ `{args.key or BASE_KEY}` 没有凭据")
        return 2
    ok, why = probe(cookie, _sample_book_id(session, user_id))
    print(("✅ " if ok else "❌ ") + why)
    return 0 if ok else 1


def cmd_rm(session, user_id: int, args) -> int:
    key = (args.key or "").strip()
    if not key or key == BASE_KEY:
        print("❌ 不会删基础账号(要换请 `add --key weread --force`)")
        return 2
    delete_cookie(session, user_id, key)
    print(f"已删除 `{key}`")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="微信读书账号池(精确阅读数扩容)")
    ap.add_argument("--user", type=int, default=1)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    p_add = sub.add_parser("add")
    p_add.add_argument("--key", default="weread#2")
    p_add.add_argument("--cookie", default="")
    p_add.add_argument("--cookie-file", default="")
    p_add.add_argument("--force", action="store_true")

    p_probe = sub.add_parser("probe")
    p_probe.add_argument("--key", default=BASE_KEY)
    p_probe.add_argument("--cookie", default="")
    p_probe.add_argument("--cookie-file", default="")

    p_rm = sub.add_parser("rm")
    p_rm.add_argument("--key", required=True)

    args = ap.parse_args()
    session = get_session_local()()
    try:
        if args.cmd == "list":
            return cmd_list(session, args.user)
        if args.cmd == "add":
            return cmd_add(session, args.user, args)
        if args.cmd == "probe":
            return cmd_probe(session, args.user, args)
        return cmd_rm(session, args.user, args)
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
