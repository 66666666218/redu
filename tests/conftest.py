"""pytest 共用 fixture。"""
import pytest

from config.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """测试用 Settings:跳过 .env,使用受控阈值。"""
    return Settings(
        _env_file=None,
        is_dev=True,
        mock_index=True,
        use_proxy=False,
        top_n=5,
        min_heat=100,
        min_samples=3,
        growth_threshold=0.3,
        slope_threshold=0.0,
        data_dir="data/test",
    )
