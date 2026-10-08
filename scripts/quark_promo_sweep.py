"""按**强模式**清扫别人的引流文件(2026-10-08)。**默认 dry-run。**

## 为什么是"按模式搜索",不是"递归扫描"
递归扫描每个资源包(深度 ≥2)在真盘上要**几十上百次** `list_dir`(实测 19 个家 584 次调用、
811 秒),而且**只能扫到扫过的深度** —— 实测扫完两层之后,`切勿卸载网盘` 这类模板 txt
盘上仍有 **96 份**(它们藏在每个包更深的子目录里)。

而**搜索接口按子串匹配文件名,一次调用就能拿全**(不受深度限制):
13 个强模式各搜一次 ≈ 13 次调用 → 拿到全部副本。这是"按内容找"与"按结构找"的差别。

## 范围怎么圈:靠 `file_struct.sec_source == 'share_save'`
搜索结果**不带路径**,所以"这个文件是不是在我们转存的资源包里"没法直接判。
但每条结果带 `file_struct`:`sec_source == 'share_save'` 说明它**是转存回来的** ——
正好是我们搬的资源包内,而**排除了用户自己上传/另存的**东西(那些不该由我们判)。
这是**结构判据,不是名字判据**,所以比"按目录名猜"可靠。

## 三条硬保护(与 `quark_dup` 同一套)
1. 我方已发出的分享链指着的那份(`first_fid`)**绝不删**;
2. 我们自己的 `简介.doc` **绝不删**;
3. **弱模式一律不碰** —— 只做**强模式**(名字本身就是网盘/加群指令)。
   ⚠️ 弱模式(`教程`/`注意`/`说明`…)在这份盘上会**系统性误判**
   (实测 `高中英语151组最容易拼错的单词，考场上一定要注意！.docx` 是真资源),
   详见 `app/services/quark_dup.py` 里 `classify` 的那段。

用法:
    python scripts/quark_promo_sweep.py            # 只看(默认)
    python scripts/quark_promo_sweep.py --yes      # 真删(进回收站)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app.services.quark_dup import (  # noqa: E402
    PROTECT_NAMES,
    STRONG_PATTERNS,
    is_promo_name,
    protected_fids,
)


def collect(qt) -> tuple[list[dict], list[dict]]:
    """→ `(要删的, 被保护而跳过的)`。**只读。**"""
    prot_fids, prot_names, n_shares = protected_fids(qt)
    print(f"我方分享 {n_shares} 条,保护 {len(prot_fids)} 个 fid、{len(prot_names)} 个名字")
    seen: dict[str, dict] = {}
    skipped: list[dict] = []
    for pat in STRONG_PATTERNS:
        try:
            hits = qt.search_files(pat, size=200)
        except Exception as exc:                    # noqa: BLE001 - 一种模式搜不到不该中断整轮
            print(f"  ⚠️ 搜「{pat}」失败:{type(exc).__name__}")
            continue
        n = 0
        for h in hits:
            nm = str(h.get("file_name") or "")
            # ⚠️ 用 **is_promo_name**(剥掉括号标注再比),不是 `pat in nm`:
            # 实测真课件叫 `课时…ppt【公众号dc008免费分享】.pptx`、真资源叫
            # `黄一鸣曝王S聪聊天记录【先保存才能看】.rar` —— 按整名匹配会把资源本体删掉。
            if h.get("dir") or pat not in nm or not is_promo_name(nm):
                continue
            # 结构判据:只动**转存回来**的文件(排除用户自己上传/另存的)
            fs = h.get("file_struct") or {}
            if str(fs.get("sec_source") or "") != "share_save":
                continue
            fid = str(h.get("fid") or "")
            if not fid:
                continue
            n += 1
            if fid in prot_fids or nm in prot_names or nm in PROTECT_NAMES:
                skipped.append({"name": nm, "fid": fid, "why": "被分享链或简介名单保护"})
                continue
            seen[fid] = {"name": nm, "fid": fid, "size": int(h.get("size") or 0), "why": pat}
        if n:
            print(f"  强模式「{pat}」→ {n} 个")
    return list(seen.values()), skipped


def main() -> int:
    from app.db import get_session_local
    from app.services.cookie_store import get_cookie
    from app.services.quark_dup import apply_plan
    from app.services.quark_transfer import QuarkTransfer
    from config.settings import get_settings

    yes = "--yes" in sys.argv
    s = get_settings()
    db = get_session_local()()
    try:
        ck = get_cookie(db, 1, "quark") or getattr(s, "quark_cookie", "") or ""
    finally:
        db.close()
    qt = QuarkTransfer(ck, fid_store=getattr(s, "quark_fid_store", "") or None)

    items, skipped = collect(qt)
    print(f"\n=== 计划删除 {len(items)} 个引流文件,"
          f"约 {sum(x['size'] for x in items) / 2 ** 20:.1f} MiB ===")
    for x in sorted({x["name"] for x in items}):
        print(f"   ✂ {x[:70]}")
    if skipped:
        print(f"\n🛡 被保护而跳过 {len(skipped)} 个,例如:{[x['name'][:24] for x in skipped[:5]]}")

    if not yes:
        print("\n(dry-run:什么都没删。要真删加 --yes —— 全部进回收站)")
        return 0
    out = apply_plan(qt, {"delete": items})
    print(f"\n✅ 已删 {out['deleted']} 个(约 {out['bytes'] / 2 ** 20:.1f} MiB),全部进**回收站**可捞回")
    if out["failed"]:
        print(f"⚠️ {len(out['failed'])} 批失败:{out['failed'][:3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
