"""链路体检(CLI 薄壳)—— 实现在 `app/services/chain_health.py`。

2026-10-05 从脚本**挪进 services**:因为要把它**挂进调度**(每天推管理群),
而作业只在 `app/services/` 里注册。脚本这边保留一个薄壳,方便手动跑。

跑法:
    python scripts/chain_health.py           # 人读
    python scripts/chain_health.py --json    # 机器读
    python scripts/chain_health.py --push    # 立刻推一次管理群(测通路)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.services.chain_health import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
