"""盘内**重复资源**只读报告(2026-10-07)。**不删任何东西**。

## 判据从哪来(以及为什么换了口径)
用户口径原本是「重复 = 同一资源多份;老 = 早于 2026-10-01」。实测后发现"老"**算不出来**:
迅雷盘上所有文件的 `created_time` / `original_create_time` **全是入库那一天**,
没有区分度 —— 那是"什么时候搬进来的",不是"这资源有多老"。
所以本脚本**只做重复**(它现在算得出来),老的判据留给你重新定。

## 重复怎么判
用 `resource_library.core_resource_name`(**今天做跨平台共振时建的那层归一化**)——
盘里同一份资源常有一堆写法:「xxx.pdf」/「xxx(1).pdf」/「xxx 完整版.pdf」,
按原始名比对会把它们当成三份,按归一化名才认得出是一份。

用法:`python scripts/pan_dedupe_report.py [深度]`
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import xunlei_transfer as xt  # noqa: E402
from app.services.resource_library import core_resource_name  # noqa: E402

MAX_DEPTH = int(sys.argv[1]) if len(sys.argv) > 1 else 3
BUDGET = 400          # API 调用上限:扫不完就说扫不完,别跑飞


def _strip_copy_suffix(name: str) -> str:
    """去掉「(1)」「(2)」这类**复制副本**后缀 —— 那是"同一份存了两次"最典型的样子。

    ⚠️ 只在**末尾**剥,且要求括号里是纯数字:「（先保存再下载）」这种说明性括号**不动**。
    """
    import re as _re

    return _re.sub(r"\s*[（(]\s*\d+\s*[)）]\s*$", "", str(name or "")).strip()


#: 说明类**目录名**的痕迹 —— 每个资源包里都带一份,不是重复
_NOTICE_WORDS = ("必看", "解压方法", "使用帮助", "说明", "教程", "注意", "公告")


def _is_notice(name: str) -> bool:
    return any(w in str(name) for w in _NOTICE_WORDS)


def main() -> int:
    calls = {"n": 0}
    files: list[dict] = []
    dirs: list[dict] = []          # 资源包(文件夹)—— 去重的判据落在这一层,见下面注释

    def ls(pid: str) -> list[dict]:
        if calls["n"] >= BUDGET:
            return []
        calls["n"] += 1
        try:
            return xt.list_all_files(pid, page_size=200, max_pages=20)
        except Exception as exc:  # noqa: BLE001 - 单目录失败不拖垮整轮
            print(f"   ⚠️ 列 {pid[:10]} 失败: {str(exc)[:60]}")
            return []

    def walk(pid: str, depth: int, path: str) -> None:
        if depth > MAX_DEPTH or calls["n"] >= BUDGET:
            return
        for x in ls(pid):
            name = str(x.get("name") or "").strip()
            if not name:
                continue
            if "folder" in str(x.get("kind")):
                kids = ls(str(x.get("id")))
                dirs.append({"id": str(x.get("id")), "name": name,
                             "n": len(kids), "path": f"{path}/{name}"})
                walk(str(x.get("id")), depth + 1, f"{path}/{name}")
            else:
                files.append({"id": str(x.get("id")), "name": name,
                              "size": int(x.get("size") or 0), "path": f"{path}/{name}"})

    print(f"扫盘(深度上限 {MAX_DEPTH},API 预算 {BUDGET} 次)…")
    walk("", 1, "")
    print(f"扫到 {len(files)} 个文件,用了 {calls['n']} 次调用"
          f"{'(**撞预算,没扫完**)' if calls['n'] >= BUDGET else ''}\n")
    if not files:
        return 0

    # ⚠️⚠️ **判据在「资源包(文件夹)」这一层,不是文件层**(2026-10-07 实测踩到):
    # 第一版按**文件名**去重,报出来的"重复"绝大多数是
    #   【必看.jpg】×5、【解压方法看这里.JPG】×3、【使用帮助及新版软件.jpeg】×8
    # —— 那些**不是重复,是每个资源包各自带的说明书**!按文件删会把**每个包都弄坏**。
    # 真正的重复是**整个资源包被存了两份**(实测:`白泽的梦` 与 `白泽的梦(1)`)。
    # ⇒ 只比**文件夹**,并排掉说明类目录名。
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for d in dirs:
        k = core_resource_name(_strip_copy_suffix(d["name"]))
        if k and not _is_notice(k):
            groups[k].append(d)
    dups = {k: v for k, v in groups.items() if len(v) > 1}

    print(f"=== 重复的**资源包(文件夹)**:{len(dups)} 组 ===")
    for k, v in sorted(dups.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  【{k[:44]}】×{len(v)}")
        for x in sorted(v, key=lambda y: y["path"]):
            print(f"     {x['path'][:86]}   ({x['n']} 项)")
    print()
    print("⚠️ **文件层不去重**:每个资源包里都有一份「必看 / 解压方法 / 使用帮助」——")
    print("   它们文件名相同,但**各自属于那个包**,删掉等于把每个包都弄坏。")
    print("   上面报的是**整包重复**(同一份资源存了两次),删掉多余那份才安全。")
    print()
    print("   ⚠️ 本脚本**只读**。要删请另行确认 —— 删之前必须看清每组**留哪一份**,")
    print("      以及「名字像但其实是两份不同资源」的误判(归一化不是万能的)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
