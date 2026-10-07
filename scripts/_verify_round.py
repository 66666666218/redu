"""抓一轮监听的验证证据(临时诊断脚本,随时可删)。"""
import io
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402

from app.db import get_session_local  # noqa: E402

db = get_session_local()()
print("=== ① 最近一轮监听 ===")
r = db.execute(text("SELECT substr(started_at,1,16),status,substr(coalesce(detail,''),1,160) "
                    "FROM runs WHERE kind='wechat_listen' ORDER BY started_at DESC LIMIT 1")).first()
print(f"   {r[0]} {r[1]}" if r else "   (无记录)")
print(f"   {r[2]}" if r else "")
print()
print("=== ② 待转存补上了吗 ===")
for t, mu in db.execute(text(
        "SELECT substr(title,1,30), substr(coalesce(my_pan_urls,''),1,24) FROM wechat_articles "
        "WHERE created_at > datetime('now','-2 days') AND coalesce(pan_urls,'')<>'' "
        "ORDER BY created_at DESC LIMIT 6")).all():
    print(f"   {str(t)[:30]:32} {mu or '**还是空**'}")
print()
r = db.execute(text("SELECT count(*), sum(read_num>0) FROM wechat_articles "
                    "WHERE created_at > datetime('now','-1 day')")).one()
print(f"=== ③ 阅读数(近1天):入库 {r[0]} 篇,有阅读数 {r[1]} 篇 ===")
print()
L = []
for b in io.open("data/app.log", "rb").read().split(b"\n"):
    for e in ("utf-8", "gbk", "latin-1"):
        try:
            L.append(b.decode(e))
            break
        except Exception:
            continue
lock = [l[:19] for l in L if "database is locked" in l and l[:10] == "2026-10-07"
        and l[:16] >= "2026-10-07 20:0"]
print(f"=== ④ 20:0x 之后的 database is locked:{len(lock)} 次 ===")
for l in lock[:5]:
    print("   ", l)
ev = [l[:19] for l in L if "App 登录态失效" in l or "凭据自愈" in l or "唤醒 App" in l]
print(f"=== ⑤ 阅读数自愈痕迹:{ev[-3:] or '无'} ===")
db.close()
