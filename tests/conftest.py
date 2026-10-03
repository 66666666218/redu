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
