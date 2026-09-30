"""前端构建同步守卫:源码变更后必须重建,否则后端挂载的是旧产物(2026-10-01)。

失败时的修复:`python scripts/build_frontend.py --build`(或 cd frontend && npm run build)。
"""
import pytest


def test_frontend_spa_built_from_current_src() -> None:
    from scripts.build_frontend import is_synced

    ok, why = is_synced()
    assert ok, (
        f"前端产物与源码不同步:{why}。"
        "运行 `python scripts/build_frontend.py --build` 重新构建后再提交"
    )
