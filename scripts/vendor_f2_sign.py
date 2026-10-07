"""把 f2 的抖音签名算法**重新移植**进 `app/services/douyin_sign/`(2026-10-07)。

## 什么时候用它
上游 f2 更新了 `abogus.py` / `xbogus.py`(抖音改版 → 签名跟进),我们要跟着搬:

    python scripts/vendor_f2_sign.py            # 按下面钉住的提交重新移植
    python scripts/vendor_f2_sign.py --sha <新提交>   # 先看上游更新到哪了,再搬

搬完**务必**:
1. 跑 `python -m pytest tests/test_douyin_sign.py -q` —— 金标/哈希变了会红,这是**要你确认**;
2. 本脚本会把新的"算法体 sha256"打出来,把它抄进 `tests/test_douyin_sign.py` 的
   `_BODY_SHA256` 与 `app/services/douyin_sign/NOTICE.md`;
3. 在 CHANGELOG 写清**为什么搬**(上游 commit 说了什么)。

## 为什么要有这个脚本
第一次移植是我敲的一次性命令 —— 那种东西**下一个人复现不了**。搬第三方代码要么别搬,
要么搬得可复现:来源、提交、改动范围、哈希,四样都得留下。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "app" / "services" / "douyin_sign"

REPO = "Johnserf-Seed/f2"
#: 钉住的提交 —— 换它之前先读 `--sha` 拉到的上游 commit message
SHA = "f6be8c0ffba9a127075bbeafe4838716650b6325"
#: 上游路径 → 本仓文件名
FILES = {
    "f2/utils/crypto/bytedance/abogus.py": "abogus.py",
    "f2/utils/crypto/bytedance/xbogus.py": "xbogus.py",
}

BANNER = '''# =============================================================================
# **逐字移植**自 f2 —— https://github.com/{repo}
#   上游路径: {up}
#   上游提交: {sha}  ({date})
#   许可证:   Apache License 2.0(见同目录 NOTICE.md)
# 本仓的**唯一改动**: 加了这个横幅。算法体**一字未动**。
# ⚠️ 改这个文件 = 改签名。动之前先读 `doc/抖音纯协议-链路拆解.md` 的
#    「怎么自修」一节,并跑 `python -m pytest tests/test_douyin_sign.py -q`。
# =============================================================================
'''
BANNER_RULE = "# " + "=" * 77
UA = {"User-Agent": "curl/8"}


def _fetch(url: str, tries: int = 3) -> bytes:
    last: Exception | None = None
    for _ in range(tries):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=30).read()
        except Exception as exc:  # noqa: BLE001 - 网络抖动要重试,别的错误照抛
            last = exc
            time.sleep(2)
    raise RuntimeError(f"下载失败 {url}:{type(last).__name__}: {last}")


def _commit_date(sha: str) -> str:
    """取提交日期(只为写进横幅;取不到就留空,不因此让移植失败)。"""
    try:
        import json
        d = json.loads(_fetch(f"https://api.github.com/repos/{REPO}/commits/{sha}",
                              tries=1).decode("utf-8"))
        return str(d["commit"]["committer"]["date"])[:10]
    except Exception:  # noqa: BLE001
        return "?"


def _strip_upstream_header(body: str) -> str:
    """去掉上游首行的 `# path: f2/...`(那是 f2 仓库内的路径,搬过来已无意义)。"""
    lines = body.splitlines(keepends=True)
    if lines and lines[0].startswith("# path:"):
        lines = lines[1:]
        while lines and lines[0].strip() == "":
            lines = lines[1:]
    return "".join(lines)


def _body_sha(text: str) -> str:
    """算法体哈希:换行归一化 + 去掉横幅。供测试与 NOTICE 引用。"""
    norm = text.replace("\r\n", "\n")
    return hashlib.sha256(norm.split(BANNER_RULE + "\n")[-1].encode()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="重新移植 f2 的抖音签名算法")
    ap.add_argument("--sha", default=SHA, help="上游提交(默认用脚本里钉住的那个)")
    ap.add_argument("--dry-run", action="store_true", help="只下载比对,不写文件")
    args = ap.parse_args()

    date = _commit_date(args.sha)
    print(f"来源 {REPO} @ {args.sha} ({date})\n")
    PKG.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    hashes: dict[str, str] = {}

    for up, name in FILES.items():
        raw = _fetch(f"https://raw.githubusercontent.com/{REPO}/{args.sha}/{up}")
        text = _strip_upstream_header(raw.decode("utf-8"))
        out = BANNER.format(repo=REPO, up=up, sha=args.sha, date=date) + text
        hashes[name] = _body_sha(out)
        dst = PKG / name
        old = dst.read_text(encoding="utf-8").replace("\r\n", "\n") if dst.exists() else ""
        same = old == out
        if not same and old:
            changed.append(name)
        if not args.dry_run:
            dst.write_text(out, encoding="utf-8", newline="\n")
        print(f"  {name:12s} {'未变' if same else '**已更新**'}  {len(out)} 字符")

    lic = _fetch(f"https://raw.githubusercontent.com/{REPO}/{args.sha}/LICENSE").decode("utf-8")
    if not args.dry_run:
        (PKG / "LICENSE.f2.txt").write_text(lic, encoding="utf-8", newline="\n")

    print("\n算法体 sha256(抄进 tests/test_douyin_sign.py 的 _BODY_SHA256 与 NOTICE.md):")
    for name, h in hashes.items():
        print(f'    "{name}": "{h}",')
    if changed:
        print(f"\n⚠️ 变了的是:{'、'.join(changed)} —— 去翻上游这次的 commit message,"
              f"确认它**是有意改的**,再更新金标;金标变了不等于签名错了。")
    if args.dry_run:
        print("\n(--dry-run:没有写任何文件)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
