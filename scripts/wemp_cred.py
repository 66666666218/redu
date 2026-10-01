"""公众号后台凭据录入(2026-10-01):让自研 WempClient 真正跑起来的那一步。

**背景**:`app/services/wechat/wemp_client.py`(直连 `appmsgpublish`,能拿某号的**全量文章列表**)
9-30 就写好了,协议正确、异常也接进了监听降级链——**一直只缺有效凭据**。而现有的取数链路
(微信读书 cover)每个号每次只能拿**最新 1 篇**,还老撞 `-2041` 限流。

**凭据从哪来**:注册一个微信公众号(订阅号即可)→ 浏览器登录 mp.weixin.qq.com,需要两样:
  - `cookie`:F12 → Network → 任意请求 → Request Headers → Cookie(整条复制)
  - `token` :登录后地址栏里的 `?token=xxxxxxxx`(每次登录变,有效期通常几天)

**用法**:
    # 1) 手动录入(从浏览器复制)
    python scripts/wemp_cred.py --cookie "..." --token "..."

    # 2) 从 WeRSS 的 wx.lic 提取(先在 WeRSS 界面扫码登录过)
    python scripts/wemp_cred.py --from-werss

    # 3) 只看看当前存的凭据还能不能用
    python scripts/wemp_cred.py --check

录入后会**立刻打一次探针**(拉一个真实对标号的文章列表),结果直接告诉你:
凭据有效能拿几篇 / 被限流(200013) / 会话失效(200003)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import init_db  # noqa: E402
from app.db import get_session_local  # noqa: E402
from app.db.models import SystemConfig, User, WechatBenchmark  # noqa: E402
from app.services.wechat.wemp_client import (  # noqa: E402
    WempAuthError, WempClient, WempError, WempRateLimited,
)
from sqlalchemy import select  # noqa: E402

KEY = "wemp_cred_{uid}"
WERSS_LIC = Path("D:/werss/data/wx.lic")


def load_cred(db, user_id: int) -> dict:
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == KEY.format(uid=user_id)))
    if not row or not row.value:
        return {}
    try:
        return json.loads(row.value)
    except ValueError:
        return {}


def save_cred(db, user_id: int, cookie: str, token: str) -> None:
    key = KEY.format(uid=user_id)
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == key))
    value = json.dumps({"cookie": cookie.strip(), "token": token.strip()}, ensure_ascii=False)
    if row is None:
        db.add(SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.commit()


def parse_lic(path: Path) -> dict:
    """解析 WeRSS 的 wx.lic。

    它不是标准 JSON/Python 字面量,而是 `键: '值'` 每行一条(YAML 风格、值带单引号),
    所以先按行切再逐行剥引号——比套用任何解析器都稳。
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip("'\"").strip()
    return out


def extract_from_lic(path: Path) -> tuple[str, str]:
    """从 wx.lic 取 (cookie, token)。取不到就抛,让调用方给一句人话。"""
    lic = parse_lic(path)
    cookie = lic.get("cookie", "")
    token = lic.get("token", "") or lic.get("token_data", "")
    if not cookie or not token:
        raise WempError(f"{path} 里没有 cookie/token(该文件里出现的是:{sorted(lic)[:8]})")
    return cookie, token


def probe(db, user_id: int, cookie: str, token: str) -> int:
    """拿真实对标号打一枪,返回拿到的文章数;异常原样抛给调用方分类。"""
    bm = db.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id, WechatBenchmark.biz != "").limit(1))
    if bm is None:
        print("⚠ 库里还没有可试的对标号,跳过探针(凭据已保存,监听轮会用它)")
        return -1
    mp_id = bm.biz if bm.biz.startswith("MP_WXS_") else bm.weread_book_id
    items = WempClient(cookie, token).mp_articles(mp_id, page=1, limit=20)
    print(f"✓ 探针成功:「{bm.nickname}」拿到 {len(items)} 篇")
    for it in items[:3]:
        print(f"    · {it['title'][:44]}")
    return len(items)


def main() -> int:
    ap = argparse.ArgumentParser(description="公众号后台凭据录入/校验")
    ap.add_argument("--cookie", help="公众号后台 Cookie(整条)")
    ap.add_argument("--token", help="后台 URL 里的 token")
    ap.add_argument("--from-werss", action="store_true", help=f"从 {WERSS_LIC} 提取")
    ap.add_argument("--lic", default=str(WERSS_LIC), help="wx.lic 路径")
    ap.add_argument("--check", action="store_true", help="只校验已存凭据")
    ap.add_argument("--user", type=int, default=1, help="用户 id(默认 1)")
    args = ap.parse_args()

    init_db()
    db = get_session_local()()
    try:
        if db.scalar(select(User).where(User.id == args.user)) is None:
            print(f"✗ 用户 {args.user} 不存在")
            return 1

        if args.check:
            cred = load_cred(db, args.user)
            if not cred:
                print("✗ 还没录入凭据(见本文件头部的用法)")
                return 1
            cookie, token = cred.get("cookie", ""), cred.get("token", "")
        elif args.from_werss:
            try:
                cookie, token = extract_from_lic(Path(args.lic))
            except WempError as exc:
                print(f"✗ {exc}")
                return 1
            print(f"从 {args.lic} 取到 cookie({len(cookie)} 字符) / token({len(token)} 字符)")
            save_cred(db, args.user, cookie, token)
            print("✓ 已保存")
        elif args.cookie and args.token:
            cookie, token = args.cookie, args.token
            save_cred(db, args.user, cookie, token)
            print(f"✓ 已保存 cookie({len(cookie)}) / token({len(token)})")
        else:
            ap.print_help()
            return 1

        try:
            probe(db, args.user, cookie, token)
            print("\n下一步:等下一轮监听(4/8/14/20 点)自动走 ⓪ 分支;想立刻看效果可跑一次「立即监听一轮」")
        except WempRateLimited:
            print("⚠ 凭据有效,但**被频率限制(200013)** —— 换个新注册的号会干净很多,或等配额恢复")
        except WempAuthError as exc:
            print(f"✗ {exc}\n  → 重新登录 mp.weixin.qq.com,把新的 cookie/token 再录一次")
        except WempError as exc:
            print(f"✗ 探针失败:{exc}")
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
