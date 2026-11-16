"""重建**已过期的迅雷分享链**(2026-11-16)。

## 背景

`xunlei_transfer` 原本把 `expiration_days` 写死成 `"7"` ⇒ 我们发出去的每一条迅雷链
7 天后必然失效(`xunlei_resources` 实测 1886/1886 全部过期)。2026-11-14 已改成永久,
**但存量不会自愈** —— 迅雷没有"给已建链改有效期"的接口,只能**重新建一条**。

用户口径(2026-11-16):「**只重建能救的那批**」——
`xunlei_resources` 里源文件还在盘上的约 1/3;其余源文件已消失的标记放弃,不再对外给。
`xunlei_group_shares` 那批源文件基本没了(fid 存活 0/20),要救只能回群原链重搬,
不属于本脚本职责。

## 判据

- **源文件在不在**:对每个 `fid` 以各账号身份 `GET /drive/v1/files/<id>`,**三态**
  (`exists` / `not_found` / `unknown`)。⚠️ 不复用 `_folder_exists` —— 它把"认不出"
  一律当**存在**(那是给"转存落点"用的保守取舍,用在这里会把不存在当成能救)。
- **只对 `exists` 建链**;`unknown` 单列一档,**不建链**(下一轮再试)。
- 建链走 `xt.share_files()` ⇒ 自动是**永久**(`SHARE_EXPIRATION_DAYS`)。

## 两条纪律

- **探测与写库分开**:先在内存里跑完所有外部请求,最后**一次性** `UPDATE`。
  这个仓栽过"循环里写一次 + 调外部接口,抱着 SQLite 写锁 156 秒"。
- **dry-run 默认**;`--apply` 才真建链、真写库。

用法:
    python scripts/xunlei_relink.py                    # 只探活 + 出计划(不建链、不写库)
    python scripts/xunlei_relink.py --apply            # 真建链 + 写库
    python scripts/xunlei_relink.py --apply --limit 20 # 先小批试
    python scripts/xunlei_relink.py --resume           # 复用上次探活结果(不重探)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

_CACHE = "data/xunlei_relink_probe.json"


def _probe(xt, fid: str, cred: dict) -> str:
    """三态探活:`exists` / `not_found` / `unknown:<原因>`。

    ⚠️ **不能复用 `_folder_exists`**:它对任何"认不出"的结果都返回 True(当存在),
    那是"转存落点"场景的保守取舍;这里的用途相反 —— 把不存在判成存在会让后续
    `share_files` 白跑一趟(虽是响的,但整套计划会虚高)。

    ⚠️⚠️ **判据只认 HTTP 状态码 + `error` 字段,不许猜响应体里的条目字段**
    (2026-11-17 实测):真实的 200 响应体是
    `{"kind": "drive#folder", "id": …, "name": "H 回乡BL44", …}` —— **没有 `file` 键**。
    11-14 有个一次性作业用 `'在' if r.get('file') else '无'` 判存活 ⇒ 恒为假 ⇒
    它打印的「源文件=无」**连"存在"都表达不出来**(那份输出还被误当成证据用了两天)。
    """
    import requests

    def _once() -> str:
        r = requests.get(f"{xt._API}/drive/v1/files/{fid}",
                         headers=xt._drive_headers(xt._fresh_cred(cred)),
                         timeout=xt._TIMEOUT)
        d = xt._json(r)               # captcha_invalid 会在这里抛 → 走重试
        if r.status_code == 200:
            return "exists"
        if r.status_code == 404 or str(d.get("error") or "") == "file_not_found":
            return "not_found"
        return f"unknown:{r.status_code}:{str(d.get('error') or '')[:24]}"

    try:
        return xt._with_captcha_retry(_once, cred)
    except Exception as exc:  # noqa: BLE001 - 探不到 ≠ 不存在,但也不能当存在
        return f"unknown:{type(exc).__name__}"


def _load_cache(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="真建链并写库(默认只出计划)")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条(0=全部)")
    ap.add_argument("--gap", type=float, default=1.2, help="每次外部请求之间的间隔秒数")
    ap.add_argument("--resume", action="store_true", help="复用上次探活结果,不重新探")
    ap.add_argument("--cache", default=_CACHE)
    a = ap.parse_args()

    from sqlalchemy import select, update

    from app.db import get_session_local
    from app.db.models import XunleiResource
    from app.services import pan_accounts as pa
    from app.services import xunlei_transfer as xt

    db = get_session_local()()
    try:
        rows = db.execute(
            select(XunleiResource.id, XunleiResource.fid, XunleiResource.name)
            .where(XunleiResource.user_id == 1,
                   XunleiResource.fid != "",
                   XunleiResource.share_url.like("%pan.xunlei.com%"))
            .order_by(XunleiResource.id)).all()
        if a.limit:
            rows = rows[:a.limit]
        print(f"待处理 {len(rows)} 条(xunlei_resources 里带我方迅雷链的行)")

        keys = [r.cookie_key for r in pa.accounts(db, "xunlei", user_id=1)]
        creds = {k: v for k, v in ((k, xt._credentials(cookie_key=k)) for k in keys) if v}
        if not creds:
            print("没有可用凭据 —— 先扫码登录")
            return 2
        #: 默认号优先(fid 与转存它的号绑定,大多数存量是默认号转的)。
        #: ⚠️ **但绝不是"全部在默认号上"**(2026-11-16 订正):全量探活实测
        #: `exists` 678 条里 **668 在 `xunlei`、10 在 `xunlei#2`**;受控复核那 10 条,
        #: **只有 `xunlei#2` 说 exists、其余三号全 `not_found`** —— 即"只在某一个号上",
        #: 少问一个号就会把它误判成源已丢。
        #: 下面的循环只在 `exists` 时 break;`not_found` **会继续问下一个号**,
        #: 所以"默认号优先"只影响**请求顺序**,不改变判死口径(四号全否才判 not_found)。
        order = ["xunlei"] + [k for k in creds if k != "xunlei"]

        cache = _load_cache(a.cache) if a.resume else {}
        probe: dict[str, dict] = {}
        t0 = time.time()
        for i, (rid, fid, name) in enumerate(rows, 1):
            hit = cache.get(str(fid))
            if not hit:
                hit = {"state": "not_found", "who": ""}
                for k in order:
                    one = _probe(xt, fid, creds[k])
                    if one == "exists":
                        hit = {"state": "exists", "who": k}
                        break
                    if one.startswith("unknown"):
                        hit = {"state": one, "who": k}
                    #: ⚠️ 不再在**账号之间** sleep:一次裸请求实测 0.78s,而四个号各睡 1.2s
                    #: 会把单条探活从 ~3s 拖到 ~16s ⇒ 全量 1886 条要 8 小时。
                    #: 限流靠**条与条之间**的间隔(`--gap`)就够 —— 那是真正的请求节奏。
                time.sleep(a.gap)
            probe[str(fid)] = hit
            if i % 25 == 0 or i == len(rows):
                _save_cache(a.cache, probe)
                el = time.time() - t0
                print(f"  探活 {i}/{len(rows)}  已用 {el:.0f}s")

        tally: dict[str, int] = {}
        for hit in probe.values():
            key = "exists" if hit["state"] == "exists" else (
                "not_found" if hit["state"] == "not_found" else "unknown")
            tally[key] = tally.get(key, 0) + 1
        print(f"探活结果:{tally}")
        alive = [(rid, fid, name, probe[str(fid)]["who"]) for rid, fid, name in rows
                 if probe[str(fid)]["state"] == "exists"]
        print(f"可重建 {len(alive)} 条(源文件还在盘上)")

        if not a.apply:
            for rid, fid, name, who in alive[:15]:
                print(f"  [{who}] {name[:46]!r}")
            if len(alive) > 15:
                print(f"  …另外 {len(alive) - 15} 条")
            dead_n = sum(1 for h in probe.values() if h["state"] == "not_found")
            print(f"另有 {dead_n} 条源文件已不在盘上(--apply 时会标 status='dead',不再对外给)")
            print("\n[dry-run] 没有建链、没有写库。加 --apply 才真动。")
            return 0

        # ---- 建链(全部在内存里,不碰数据库) ----
        done: list[tuple[int, str, str]] = []      # (row_id, share_url, pass_code)
        failed: list[tuple[int, str, str]] = []    # (row_id, name, message)
        for i, (rid, fid, name, who) in enumerate(alive, 1):
            try:
                out = xt.share_files([fid], cred=creds[who])
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
                out = {"status": "failed", "message": f"{type(exc).__name__}"}
            if out.get("status") == "ok" and out.get("share_url"):
                done.append((rid, out["share_url"], out.get("code") or ""))
            else:
                failed.append((rid, name[:40], str(out.get("message") or "")[:80]))
            if i % 10 == 0 or i == len(alive):
                print(f"  建链 {i}/{len(alive)}  成功 {len(done)} 失败 {len(failed)}")
            time.sleep(a.gap)

        # ---- 一次性写库(不在循环里写) ----
        #: 源文件 `not_found` 的行标 `status='dead'`:它们的链**永远修不好**(没有源就没有
        #: 分享)。留着 `share_url` 是为了留痕 —— 读取端会按 `status` 跳过,不再对外给。
        #: ⚠️ `unknown` 的行**不标** —— 探不到 ≠ 不存在,下一轮再试。
        #: ⚠️ **建链失败的行也要标 dead**(2026-11-16 实测撞到一条):源文件在盘上,但迅雷
        #: 拒绝分享(`share_illegal_files`,违规内容)。它同样是"链永远修不好",
        #: 留着旧链 = 资源库继续回答一条打不开的链。
        dead_ids = [rid for rid, fid, _n in rows if probe[str(fid)]["state"] == "not_found"]
        dead_ids += [rid for rid, _n, _w in failed if rid not in dead_ids]
        if done:
            db.execute(update(XunleiResource), [
                {"id": rid, "share_url": url[:500], "pass_code": code[:32]}
                for rid, url, code in done])
        if dead_ids:
            db.execute(update(XunleiResource)
                       .where(XunleiResource.id.in_(dead_ids))
                       .values(status="dead"))
        if done or dead_ids:
            db.commit()
        print(f"\n✅ 已重建 {len(done)} 条;标 dead {len(dead_ids)} 条;建链失败 {len(failed)} 条")
        for _rid, nm, why in failed[:10]:
            print(f"  ✗ {nm!r} {why}")
        if len(failed) > 10:
            print(f"  …另外 {len(failed) - 10} 条失败")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
