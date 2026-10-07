"""「优雅关停标记」不被测试污染的守卫(2026-10-08)。

## 背景
`data/last_shutdown.txt` 是**看门狗区分「人主动停机」与「崩溃」**的凭据:
`scripts/win/notify_restart.py` 见到标记**小于 180 秒**就判定"计划内重启"、**跳过告警**。

而测试里 4 处 `with TestClient(create_app()) as c:` 退出时会走 lifespan 关停,
把这个标记**写成当前时间** ⇒ **跑完测试的 3 分钟内服务真崩了,飞书不会响**。

这正是本仓最在意的那类毛病:**告警没响,却没人知道它没响**。

## 这两条测试分别在守什么
1. **端到端**:真的跑一次 lifespan,看仓库里的标记**有没有被动过**。
   它与"用什么办法修"**无关** —— 换任何修法它都照样有效,所以不怕将来重构。
2. **反面对照**:别把功能本身弄坏 —— 真实现必须**照旧会写标记**。
   ⚠️ 这里用的是**模块导入时**抓住的原始函数对象:conftest 的 autouse fixture
   只替换 `app.platform` 上的**属性**,不影响我手里这份引用。
"""
from pathlib import Path

# ⚠️ **必须在模块导入时就抓住真实现** —— 此时 conftest 的 autouse fixture 还没跑。
# 放进制里就晚了(那时拿到的是被替换过的空操作)。
from app.platform import _mark_graceful_shutdown as _REAL_MARK

REPO_MARKER = Path(__file__).resolve().parents[1] / "data" / "last_shutdown.txt"


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def test_跑一次真实_lifespan_不会动仓库里的关停标记():
    """★ **端到端**守卫:真正跑一次 `create_app()` 的 lifespan(含关停)。

    判据是标记的 mtime **不变**。测试期间**总在**跑 lifespan,所以一条正常的
    `pytest` 调用若污染了它,这里就会红 —— 不需要谁记得去点这个测试。
    """
    from fastapi.testclient import TestClient

    from app.platform import create_app

    before = _mtime(REPO_MARKER)
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
    after = _mtime(REPO_MARKER)

    assert after == before, (
        "测试往仓库的 data/last_shutdown.txt 写了「优雅关停」标记 ⇒ "
        "`notify_restart.py` 会在接下来的 180 秒里把**真崩溃**当成计划内重启、**不推告警**。"
        "检查 tests/conftest.py 的 _no_shutdown_marker fixture 是否还在生效。"
    )


def test_真实现仍然会写标记(tmp_path, monkeypatch):
    """反面对照:别为了修测试把功能本身弄坏 —— 真实现必须照写。

    换个 cwd 再调,**写进 tmp 而不是仓库**(`_mark_graceful_shutdown` 用的是
    相对路径 `data/`)。
    """
    monkeypatch.chdir(tmp_path)
    _REAL_MARK()
    written = tmp_path / "data" / "last_shutdown.txt"
    assert written.exists(), "真实现不写标记了?那看门狗就分不清'人停的'和'崩的'了"
    # 形态:ISO 8601 秒级时间戳(notify_restart 不解析它,只比 mtime;但别改成别的形状)
    text = written.read_text(encoding="utf-8")
    assert len(text) == 19 and text[4] == "-" and text[13] == ":", f"时间戳形状变了:{text!r}"
