# -*- coding: utf-8 -*-
"""前端构建工具:内容哈希校验 + 一键构建(2026-10-01)。

**为什么**:前端源码改了但忘记 `npm run build` 时,后端挂载的仍是旧产物——
页面行为与源码不符,排查极易跑偏(典型症状:改了文案/字段但线上没变化)。
本脚本把 `frontend/src` 的内容哈希写进构建产物 `BUILD_INFO.json`,
`tests/test_frontend_build_sync.py` 在测试时比对该哈希,不同步即失败并提示重建。

用法:
    python scripts/build_frontend.py            # 校验模式:只报告是否同步(退出码 0/1)
    python scripts/build_frontend.py --build    # 一键构建:跑 npm build + 写 BUILD_INFO
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"
SPA = ROOT / "app" / "static" / "spa"
INFO = SPA / "BUILD_INFO.json"
_EXT = {".vue", ".js", ".ts", ".css", ".json", ".html"}


def src_hash() -> str:
    """frontend/src 下全部源文件的内容哈希(路径+内容,稳定排序)。"""
    h = hashlib.sha256()
    files = sorted(p for p in SRC.rglob("*") if p.is_file() and p.suffix in _EXT)
    for p in files:
        h.update(p.relative_to(SRC).as_posix().encode("utf-8"))
        h.update(b"\x00")
        h.update(p.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()


def write_info() -> None:
    import datetime

    SPA.mkdir(parents=True, exist_ok=True)
    INFO.write_text(json.dumps({
        "src_hash": src_hash(),
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")


def is_synced() -> tuple[bool, str]:
    """产物与源码是否同步;(是否同步, 原因)。"""
    if not INFO.exists():
        return False, "缺少 BUILD_INFO.json(旧构建产物)"
    try:
        recorded = json.loads(INFO.read_text(encoding="utf-8")).get("src_hash", "")
    except ValueError:
        return False, "BUILD_INFO.json 损坏"
    current = src_hash()
    if recorded != current:
        return False, f"源码已变更(src {current[:8]} vs 产物 {recorded[:8]})"
    return True, "同步"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 非交互环境无 reconfigure
        pass
    do_build = "--build" in sys.argv
    if do_build:
        print("运行 npm run build ...")
        r = subprocess.run("npm run build", cwd=str(ROOT / "frontend"), shell=True)
        if r.returncode != 0:
            print("npm build 失败", file=sys.stderr)
            return r.returncode
        write_info()
        print("构建完成,BUILD_INFO 已更新")
        return 0
    ok, why = is_synced()
    print(("前端产物与源码同步 ✓" if ok else f"前端产物**不同步**:{why}"))
    if not ok:
        print("修复:cd frontend && npm run build,或 python scripts/build_frontend.py --build")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
