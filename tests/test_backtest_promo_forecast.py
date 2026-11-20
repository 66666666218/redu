"""回测脚本 `scripts/backtest_promo_forecast.py` 的口径守卫。

它产出的是「**提前 24 小时锁定爆款**」这个判断的量化依据，会直接决定要不要
把某个资源搬进来。三类错误都会让结论反向：

① **前视偏差**：拿今天的自有链清单去判 30 天前的决策 ⇒ 「当时没有」全被判成
   「当时已有」⇒ 可动手量被系统性低估，而且数字变小看起来像"保守"而不像 bug;
② **右删失**：把昨天才出现的桶算进"没爆" ⇒ 精确率虚高;
③ **早窗口外的行混进来**：`early` 人数变成"总人数",规则失去意义。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import sys  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backtest_promo_forecast import _bucketize  # noqa: E402

T0 = datetime(2026, 11, 1, 10, 0, 0)

#: `_identity` 用的就是 `pan_key()`：它只归一 host/query，**路径大小写原样保留**。
#: 桶键必须用这个串,不能拿原始 URL(否则 `KeyError`,本测试第一版就栽在这)。
PK = "pan.quark.cn/s/AAA"


def _row(author, title, *, at=T0, pan_url="https://pan.quark.cn/s/AAA",
         section="bilibili", pan_type="夸克"):
    return SimpleNamespace(user_id=1, pan_url=pan_url, pan_id="", section=section,
                           author=author, title=title, pan_type=pan_type, pushed_at=at)


def _names(rows):
    return {(1, PK): ["作品a"]}


def test_早窗口内的人数只算窗口内的() -> None:
    rows = [_row("甲", "资源《作品A》"),
            _row("乙", "资源《作品A》", at=T0 + timedelta(hours=2)),
            _row("丙", "资源《作品A》", at=T0 + timedelta(hours=48))]
    b = _bucketize(rows, _names(rows), 24, {})
    key = (1, "作品a")
    assert len(b[key]["early"]) == 2, "第 3 个人在 48h 后才出现,不算早窗口"
    assert len(b[key]["all"]) == 3


def test_自有链在早窗口之后才拿到_不算当时已有() -> None:
    """★ 前视偏差：30 天后才搬的链,不能用来解释 30 天前"我们没缺"。

    判反了 ⇒ ③ 可动手量会被低估(而且结果看起来更"保守",不容易被发现)。
    """
    rows = [_row("甲", "资源《作品A》")]
    owned = {PK: T0 + timedelta(days=5)}
    b = _bucketize(rows, _names(rows), 24, owned)
    key = (1, "作品a")
    assert b[key]["early_own"] is False
    assert b[key]["early_src"] is not None, "当时没有自有链 ⇒ 源链要被记下来供搬运"


def test_自有链在窗口之前就有_算当时已有() -> None:
    rows = [_row("甲", "资源《作品A》")]
    owned = {PK: T0 - timedelta(days=3)}
    b = _bucketize(rows, _names(rows), 24, owned)
    key = (1, "作品a")
    assert b[key]["early_own"] is True
    assert b[key]["early_src"] is None, "已有自有链就不该再给源链(否则会被重复搬)"


def test_早窗口跨板块与打码标记() -> None:
    rows = [_row("甲", "《作品A》", section="bilibili"),
            _row("水***", "《作品A》", section="kuaishou"),
            _row("乙", "《作品A》", section="tieba")]
    b = _bucketize(rows, _names(rows), 24, {})
    key = (1, "作品a")
    assert b[key]["early_sections"] == {"bilibili", "kuaishou", "tieba"}
    assert b[key]["early_masked"] is True, "打码作者名要能标出来(它会让人数 ±1)"
    assert b[key]["early_bracketed"] is True