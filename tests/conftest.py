"""pytest 共用 fixture。"""
import pytest

from config.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """测试用 Settings:跳过 .env,使用受控阈值。"""
    return Settings(
        _env_file=None,
        is_dev=True,
        use_proxy=False,
        top_n=5,
        growth_threshold=0.3,
        request_delay_seconds=0.0,
        xianyu_request_delay=0.0,
    )


@pytest.fixture(autouse=True)
def _no_shutdown_marker(monkeypatch):
    """测试**不许**往仓库的 `data/` 写「优雅关停」标记。

    ⚠️ **为什么**(2026-10-08 发现):

    1. 4 处测试用 `with TestClient(create_app()) as c:` —— 退出时走 lifespan 的关停,
       于是 `app.platform._mark_graceful_shutdown()` 把 `data/last_shutdown.txt`
       写成**当前时间**;
    2. 而 `scripts/win/notify_restart.py` 的判据是「标记 **< 180 秒** ⇒ 计划内重启,
       **跳过告警**」。

    ⇒ **跑完测试的 3 分钟内,服务真崩了也不会推飞书**(重启照常,只是没人告诉你)。
    这正是本仓最在意的那一类 —— **告警没响 = 假成功**。

    ⚠️ 生产行为**不变**(人主动停机时写标记是对的);这里只是让**测试**别去写它。
    端到端守卫见 `tests/test_shutdown_marker.py` —— 那条测试与"怎么修"无关,
    它直接跑一次真实 lifespan 看标记有没有被动过。
    """
    import app.platform as platform

    monkeypatch.setattr(platform, "_mark_graceful_shutdown", lambda: None)
