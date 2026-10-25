"""清理浏览器档案里的**缓存**,保留登录态(2026-10-26)。

    python scripts/clean_browser_caches.py            # 只看能回收多少(默认 dry-run)
    python scripts/clean_browser_caches.py --yes      # 真的删

## 为什么需要它

CDP 那条路每多一个平台就多一份 Edge 档案,而每份档案稳定吃 **300~500MB** ——
其中真正的"登录态"只有**几百 KB**(`Default/Network/Cookies`、`Local Storage`、
`Preferences`、`Login Data`),其余是:

| 目录 | 是什么 | 删了会怎样 |
|---|---|---|
| `Default/Cache`、`Default/Code Cache` | 网页 HTTP 缓存 / JS 编译缓存 | 下一轮重新下载,慢一点 |
| `component_crx_cache` | Edge 组件更新包(CRL、DRM…) | Edge 自动重下 |
| `ProvenanceData` | Edge 视觉搜索模型(`*.onnx`) | 自动重下 |
| `WidevineCdm` | DRM 模块 | 自动重下 |
| `Edge Wallet/Shopping/Entity Extraction/Subresource Filter` | Edge 自带功能数据 | 用不到,重下也无所谓 |

实测(2026-10-26):`xhs_cdp_profile` 991MB 里 `Default/Cache` 375MB + `Code Cache` 89MB;
三个档案合计 2.0GB,可回收约 **1.7GB**。

## 五条安全纪律

1. **默认 dry-run**,不加 `--yes` 一个文件都不删。
2. **只删白名单内的缓存目录**,白名单是**相对路径的精确匹配**(不是前缀、不是正则)——
   登录态那几个文件**不在白名单里**,所以不可能被误删。
3. **有浏览器正在用这个档案就跳过** —— 边跑边删档案会把它搞坏(宁可少清一个)。
4. **档案目录必须长得像"我们自己的档案"**:在 `data/` 下、且含 `Local State` 或 `Default/`,
   别把随便一个路径当档案清了。
5. ★ **常驻 CDP 档案默认不清**(`xhs_cdp_profile` / `_douyin_profile`):它们是
   "一直登着、模拟真人"的资产,用户要的是**连风险都不冒**;要清必须显式 `--include-kept`。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: **精确相对路径白名单** —— 只清这些;任何登录态文件都不在此列
_CACHE_PATHS = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "Default/GrShaderCache",
    "Default/Service Worker/CacheStorage",
    "component_crx_cache",
    "ProvenanceData",
    "WidevineCdm",
    "Edge Entity Extraction",
    "Edge Wallet",
    "Edge Shopping",
    "Subresource Filter",
    "OptimizationHints",
    "ShaderCache",
    "GrShaderCache",
)

#: 登录态所在 —— **只用于报告**,永远不会被删(不在白名单里)
_KEEP = ("Default/Network/Cookies", "Default/Local Storage", "Default/Preferences",
         "Default/Login Data", "Local State")

#: ★ **常驻 CDP 档案:默认一个字节都不清**(用户 2026-10-26 明确要求)。
#: 这两份是"一直登着、模拟真人"的资产 —— 虽然清缓存本身不会掉登录(夸克那份实测清完
#: 仍是登录态),但用户要的是**连这点风险都不冒**:把"绝不会被顺手清掉"变成代码里的默认,
#: 而不是靠每次记得加参数。要清必须显式 `--include-kept`。
_DEFAULT_KEEP = ("xhs_cdp_profile", "_douyin_profile")


class CleanError(RuntimeError):
    """清理失败(不静默)。"""


def _dir_mb(path: Path) -> float:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / (1024 * 1024)
    except Exception:  # noqa: BLE001 - 算不出来就报 0,别让报告挂掉
        return 0.0


def looks_like_profile(path: Path) -> bool:
    """这个目录像"我们自己的浏览器档案"吗(避免把任意路径当档案清掉)。

    ⚠️ 判据要**认浏览器自己的标记文件**,不能只看"有没有 Default 目录" ——
    实测 `data/_rootfs`(131MB,逆向环境里的 rootfs)就因为有个 `Default/` 被误认成档案。
    真正的档案一定有 `Local State`(Edge/Chrome 的档案级配置)或 `Default/Network/Cookies`。
    """
    if not path.is_dir():
        return False
    return (path / "Local State").is_file() or (path / "Default" / "Network" / "Cookies").is_file()


def in_use(path: Path) -> str:
    """有 Edge/Chrome 正在用这个档案吗?返回占用的命令行;没有就空串。

    ⚠️ **边跑边删会把档案搞坏** —— 宁可跳过这一个,也不要冒这个险。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" "
             "| Select-Object -ExpandProperty CommandLine) -join \"`n\""],
            capture_output=True, timeout=20)
        text = (out.stdout or b"").decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 - 查不到就**按占用处理**(保守)
        return "无法确认是否被占用(查询失败)"
    for line in text.splitlines():
        if str(path).lower() in line.lower():
            return line.strip()[:120]
    return ""


def plan(path: Path) -> list[tuple[Path, float]]:
    """一个档案里**会删哪些目录、各有多大**。"""
    out: list[tuple[Path, float]] = []
    for rel in _CACHE_PATHS:
        p = path / rel
        if p.exists():
            out.append((p, _dir_mb(p)))
    return out


def clean(path: Path, *, yes: bool) -> float:
    """清一个档案,返回回收的 MB。`yes=False` 只算不删。"""
    items = plan(path)
    total = sum(mb for _, mb in items)
    who = in_use(path)
    if who:
        print(f"  跳过(浏览器正在用):{who}")
        return 0.0
    for p, mb in sorted(items, key=lambda x: -x[1]):
        print(f"  {'删除' if yes else '可删'} {mb:7.1f} MB  {p.relative_to(path)}")
        if yes:
            shutil.rmtree(p, ignore_errors=True)
    kept = [p for p in _KEEP if (path / p).exists()]
    print(f"  保留(登录态):{', '.join(kept) or '(没找到,可能这个档案还没登录过)'}")
    return total


def discover(root: Path) -> list[Path]:
    """`data/` 下所有长得像浏览器档案的目录。"""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if looks_like_profile(p))


def select_profiles(profiles: list[Path], *, only: str = "",
                    include_kept: bool = False) -> tuple[list[Path], list[Path]]:
    """分流成 `(本次要处理的, 因"常驻档案"被跳过的)`。

    ⚠️ 常驻 CDP 档案(`_DEFAULT_KEEP`)默认**不清**,除非显式 `include_kept=True`。
    """
    targets: list[Path] = []
    kept: list[Path] = []
    for p in profiles:
        if only and only not in p.name:
            continue
        if not include_kept and p.name in _DEFAULT_KEEP:
            kept.append(p)
            continue
        targets.append(p)
    return targets, kept


def main() -> int:
    ap = argparse.ArgumentParser(description="清浏览器档案缓存(默认 dry-run)")
    ap.add_argument("--yes", action="store_true", help="真的删(不加只统计)")
    ap.add_argument("--root", default="data", help="档案所在根目录")
    ap.add_argument("--only", default="", help="只处理名字包含该串的档案")
    ap.add_argument("--include-kept", action="store_true",
                    help="连常驻 CDP 档案也清(默认跳过 xhs_cdp_profile / _douyin_profile)")
    args = ap.parse_args()

    root = Path(args.root)
    targets, kept = select_profiles(discover(root), only=args.only,
                                    include_kept=args.include_kept)
    for p in kept:
        print(f"跳过(常驻 CDP 档案,默认不清):{p.name}({_dir_mb(p):.0f} MB)")
    profiles = targets
    if not profiles:
        print(f"✗ {root} 下没有可处理的浏览器档案"
              f"(常驻档案都已跳过;要清它们加 --include-kept)")
        return 1

    total = 0.0
    for p in profiles:
        print(f"== {p}({_dir_mb(p):.0f} MB)")
        total += clean(p, yes=args.yes)
    verb = "已回收" if args.yes else "可回收"
    print(f"\n{verb} **{total:.0f} MB**(共 {len(profiles)} 个档案)")
    if not args.yes:
        print("这只是统计 —— 加 --yes 才会真的删。删完下一轮浏览器会自动重建缓存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
