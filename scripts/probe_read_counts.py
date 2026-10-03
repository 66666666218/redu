"""公众号阅读数来源体检 + 后台 `appmsgpublish` 的 `read_num` 验证。

**为什么有这个脚本**(2026-10-04):阅读数已**全平台免费断供**,逐条实测见
`doc/外部接口速查.md §3.2`。唯一还没定论的免费候选是**公众号后台 `appmsgpublish`** ——
它的 `appmsgex[*]` 里到底有没有**别人号**的 `read_num`,必须拿一份**有效后台凭据**
打一枪才知道。本脚本就是那一枪,兼作"现状说明书"。

**为什么不是一次性的**:凭据是会过期的,结论也会变。与其把"已断供"写死进文档当事实,
不如留一个随时能重跑、能自己给出判据的命令。

用法:
    python scripts/probe_read_counts.py            # 只读体检:现状 + 探现有凭据
    python scripts/probe_read_counts.py --dump     # 凭据有效时,把 appmsgex 字段全 dump 出来

凭据从哪来(见 `scripts/wemp_cred.py`):
    python scripts/wemp_cred.py --from-werss       # 从本机 WeRSS 的 wx.lic 提取
    python scripts/wemp_cred.py --cookie "..." --token "..."   # 从浏览器 F12 复制
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.db.database import get_session_local, init_db  # noqa: E402
from app.db.models import User, WechatBenchmark  # noqa: E402
from app.services.wemp_cred import load as load_wemp_cred  # noqa: E402
from app.services.wechat.wemp_client import WEMP_BASE, _UA  # noqa: E402

# 实测结论(2026-10-04,证据见 doc/外部接口速查.md §3.2)
_ROADS = [
    ("微信读书 /web/mp/articles", "永久废弃", "续期 success+verified 后立刻再拉,书架前 5 个号仍全部 -2041"),
    ("微信读书 /api/mp/cover", "无该字段", "返回体只有 avatar/name/title/pic/reviewId/template/coverBoxInfo"),
    ("微信读书 /web/review/list", "全空/404", "listType 1~9 均为 0 条; /web/mp/notes 404"),
    ("文章页 HTML", "空占位符", "阅读数是服务端 '' * 1,全靠前端 getappmsgext 补"),
    ("/mp/getappmsgext 裸打", "静默空", "无 key/appmsg_token 时 ret:0 但响应里没有 appmsgstat"),
    ("i.weread.qq.com /mp/list", "要 App 凭据", "接口存在(401 而非 404),但 web 的短 wr_skey 不认"),
    ("WeRSS", "无此能力", "5 种采集模式里 read_num 只在那条死接口上;生产入口写死 0"),
    ("dajiala 付费", "已废弃", "2026-09-29 用户决定不充值(¥0.06/篇)"),
    ("公众号后台 appmsgpublish", "**待验证**", "本脚本要回答的就是这一条"),
]


def print_status(db) -> int:
    """打印现状 + 库里阅读数的实际情况;返回可试目标号数。"""
    print("=" * 74)
    print("阅读数来源现状(2026-10-04 逐条实测,证据见 doc/外部接口速查.md §3.2)")
    print("=" * 74)
    for name, verdict, why in _ROADS:
        print(f"  {name:26s} {verdict:10s} {why}")

    total, nonzero, lo, hi = db.execute(text(
        "select count(*), sum(case when read_num > 0 then 1 else 0 end),"
        " min(created_at), max(created_at) from wechat_articles")).one()
    print(f"\n库里 wechat_articles:{total} 篇,其中 read_num>0 的 {nonzero or 0} 篇")
    if nonzero:
        rng = db.execute(text(
            "select min(created_at), max(created_at) from wechat_articles where read_num > 0")).one()
        print(f"  这些非零行全部入库于 {rng[0]} ~ {rng[1]} —— 与'两条源几乎同一天断'吻合")
    print(f"  最早/最新文章入库:{lo} ~ {hi}")

    targets = db.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == 1, WechatBenchmark.active == 1).limit(1)).all()
    return len(targets)


def probe_appmsggex(db, dump: bool) -> int:
    """用现有 wemp 凭据打一枪,回答"别人号的 appmsgex 里有没有 read_num"。"""
    cred = load_wemp_cred(db, 1)
    if not cred.get("cookie") or not cred.get("token"):
        print("\n✗ 还没有公众号后台凭据,无法验证。录入方式:")
        print("    python scripts/wemp_cred.py --from-werss          (从本机 WeRSS 的 wx.lic 提取)")
        print('    python scripts/wemp_cred.py --cookie "..." --token "..."   (浏览器 F12 复制)')
        return 2

    bm = db.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == 1, WechatBenchmark.active == 1,
        WechatBenchmark.biz != "").limit(1))
    if bm is None:
        print("\n✗ 库里没有可试的对标号,跳过")
        return 2

    mp_id = bm.biz if bm.biz.startswith("MP_WXS_") else bm.weread_book_id
    fakeid = base64.b64encode(mp_id.replace("MP_WXS_", "").encode()).decode()
    print(f"\n>>> 用「{bm.nickname}」({mp_id})试探(appmsgpublish 拉**别人号**的文章列表)")
    try:
        resp = requests.get(
            f"{WEMP_BASE}/cgi-bin/appmsgpublish",
            params={"sub": "list", "sub_action": "list_ex", "begin": "0", "count": "5",
                    "fakeid": fakeid, "token": cred["token"],
                    "lang": "zh_CN", "f": "json", "ajax": "1"},
            headers={"User-Agent": _UA, "Referer": f"{WEMP_BASE}/cgi-bin/appmsg",
                     "Cookie": cred["cookie"], "X-Requested-With": "XMLHttpRequest"},
            timeout=20)
        msg = resp.json()
    except requests.RequestException as exc:
        print(f"✗ 请求失败:{type(exc).__name__}")
        return 1
    except ValueError:
        print("✗ 响应非 JSON(会话可能已失效)")
        return 1

    base = msg.get("base_resp") or {}
    ret = base.get("ret")
    if ret == 200003:
        print("✗ 会话失效(200003)—— 凭据过期,重新录入后再跑(见 doc/operations.md §7c)")
        return 1
    if ret == 200013:
        print("⚠ 被频率限制(200013)—— 凭据有效但此刻拿不到,稍后再试")
        return 1
    if ret != 0:
        print(f"✗ 接口报错 ret={ret}:{base.get('err_msg', '')}")
        return 1

    publish = json.loads(msg.get("publish_page") or "{}")
    for block in publish.get("publish_list") or []:
        info = json.loads(block.get("publish_info") or "{}")
        for art in info.get("appmsgex") or []:
            keys = sorted(art.keys())
            print(f"\n✓ 拿到 appmsgex,共 {len(keys)} 个字段:")
            print("   ", ", ".join(keys))
            hit = [k for k in keys if "read" in k.lower() or "like" in k.lower()]
            if hit:
                print(f"\n  >>> **有阅读/点赞类字段**:{hit}")
                for k in hit:
                    print(f"      {k} = {art[k]!r}")
                print("  → 免费阅读数可行!接下来把 `wemp_client.mp_articles` 扩成也返回 read_num")
            else:
                print("\n  >>> **没有** read_num/read_num 类字段 —— 后台这条路也不给"
                      "别人号的阅读数,免费源到此为止")
            if dump:
                print("\n  完整字段:")
                for k in keys:
                    print(f"    {k:22s} = {str(art[k])[:80]}")
            return 0
    print("✗ publish_page 里没有 appmsgex")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="阅读数来源体检 / 后台 read_num 验证")
    ap.add_argument("--dump", action="store_true", help="凭据有效时 dump appmsgex 全部字段")
    args = ap.parse_args()

    init_db()
    db = get_session_local()()
    try:
        if db.scalar(select(User).where(User.id == 1)) is None:
            print("✗ 用户 1 不存在")
            return 1
        print_status(db)
        return probe_appmsggex(db, dump=args.dump)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
