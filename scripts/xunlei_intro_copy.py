"""把「宣传简介」从一个迅雷号搬到另一个号(2026-10-16)。

## 为什么只能"建分享 → 转存"

迅雷侧**没有上传接口**,**跨账号也没有复制接口**(`batchCopy` 只在**同一账号内**,
见 `doc/三盘宣传简介.md`)⇒ 跨账号搬内容**唯一**可行的办法就是
「**甲**建一条分享 → **乙**转存」。

本脚本做的是**乙那一半**(纯协议,可自动化)。
**甲那一半**(在 App/网页里给「监控简介」目录点一下「分享」,把链接复制出来)
**只能人工,大约 10 秒** —— 这一家没有别的入口,别去找了。

## 用法

    # ① 预演(只读:拉分享详情,列出会搬哪些东西,不写盘)
    python scripts/xunlei_intro_copy.py "<分享链>" --target xunlei#2

    # ② 真搬
    python scripts/xunlei_intro_copy.py "<分享链>" --target xunlei#2 --yes

搬完会在目标账号的盘里得到一个**与分享里同名的目录**。脚本会**顺手复核**目标账号的
「转存落点目录」里到底有没有 `PAN_INTRO_XUNLEI_DIR` 配的那个目录 ——
**简介能不能挂上,取决于目标账号里有没有那个目录名**,所以这一步必须报出来
(而不是"搬完了"就完事:搬进去但名字不对 = 简介照样静默不挂)。
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")                                    # 允许 `python scripts/xxx.py`

from app.services import xunlei_transfer as xt  # noqa: E402


def _share_detail(h: dict, share_id: str, pass_code: str) -> dict:
    return xt._json(xt.requests.get(
        f"{xt._API}/drive/v1/share", headers=h, timeout=xt._TIMEOUT,
        params={"share_id": share_id, "pass_code": pass_code, "limit": "100",
                "pass_code_token": "", "page_token": "",
                "thumbnail_size": "SIZE_SMALL"}))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把一条分享里的内容搬进某个账号的盘(跨账号搬简介用)")
    ap.add_argument("share_url", help="甲建出来的分享链(https://pan.xunlei.com/s/xxx?pwd=yyyy)")
    ap.add_argument("--target", default="xunlei#2", help="转存到哪个账号(账号池的 key),默认 xunlei#2")
    ap.add_argument("--parent-id", default="", help="落点目录 id;默认根目录")
    ap.add_argument("--yes", action="store_true", help="真搬(不加则只预演,不写盘)")
    a = ap.parse_args(argv)

    cred = xt._credentials(cookie_key=a.target)
    if not cred:
        print(f"❌ 取不到账号 {a.target} 的凭据(先 `python scripts/xunlei_login.py --cookie-key {a.target}`)")
        return 2
    share_id, pass_code = xt._extract_share_id(a.share_url)
    if not share_id:
        print(f"❌ 解析不出 share_id:{a.share_url[:80]}")
        return 2

    def _once() -> int:
        h = xt._drive_headers(xt._fresh_cred(cred))
        detail = _share_detail(h, share_id, pass_code)
        if detail.get("share_status") not in ("OK", None):
            print(f"❌ 分享状态异常:{str(detail)[:200]}")
            return 2
        files = [f for f in (detail.get("files") or []) if f.get("id")]
        print(f"分享 {share_id}:{len(files)} 个顶层条目")
        for f in files:
            print(f"  · {f.get('kind')} | {f.get('name')}")
        if not files:
            print("❌ 分享里没有条目(是不是给错了链?)")
            return 2
        if not a.yes:
            print("\n(预演完成)真要搬请加 --yes —— 会在**目标账号**里真的多存一份。")
            return 0

        restore = xt._json(xt.requests.post(
            f"{xt._API}/drive/v1/share/restore", headers=h, timeout=xt._TIMEOUT,
            json={"parent_id": a.parent_id, "share_id": share_id,
                  "pass_code_token": detail.get("pass_code_token") or "",
                  "ancestor_ids": [], "specify_parent_id": True,
                  "file_ids": [f.get("id") for f in files]}))
        task_id = restore.get("restore_task_id") or ""
        if not task_id:
            print(f"❌ 转存失败:{str(restore)[:220]}")
            return 2
        task: dict = {}
        for _ in range(60):                                # 最多 60 × 2 秒
            task = xt._json(xt.requests.get(f"{xt._API}/drive/v1/tasks/{task_id}",
                                            headers=h, timeout=xt._TIMEOUT))
            if int(task.get("progress") or -1) == 100:
                break
            time.sleep(2)
        ids = xt._trace_file_ids(task)
        print(f"✅ 已搬到 {a.target}(progress={task.get('progress')},新增 {len(ids)} 个条目)")

        # ★ **搬完必须复核"名字对不对"** —— 简介能不能挂上,只看目标账号里有没有
        #   `PAN_INTRO_XUNLEI_DIR` 那个目录名;搬进去但名字不同 = 简介照样静默不挂。
        from config.settings import get_settings

        want = (getattr(get_settings(), "pan_intro_xunlei_dir", "") or "").strip()
        found = xt._find_folder_by_name(want, cred) if want else ""
        print(f"复核:目标账号里的「{want or '(未配)'}」目录 = {found or '**没找到**'}")
        if want and not found:
            print(f"⚠️ 搬是搬进来了,但**名字不是「{want}」**(或不在能搜到的地方)"
                  f" ⇒ 简介**仍然挂不上**:请在目标账号里把它改名/移动到「{want}」。")
        return 0

    try:
        return xt._with_captcha_retry(_once, cred)
    except xt._CaptchaExpired:
        print("❌ captcha 失效且自动续期失败 —— 先 `python scripts/xunlei_login.py --cookie-key "
              f"{a.target}` 刷一枚")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
