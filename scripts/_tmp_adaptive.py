# -*- coding: utf-8 -*-
"""自适应分批+权重分层:为号池扩建准备的轮换算法(2026-10-01)。用后即删。"""
from pathlib import Path

# ① settings:默认改 0=自适应
f = Path("config/settings.py")
t = f.read_text(encoding="utf-8")
old = "    wechat_listen_batch_size: int = 36  # 监听轮每批号数(2026-09-29 用户决策:142 号分组轮换,4 定点×36=144 每天轮一遍;每批≈72 请求远低于会话额度红线;0=回退全量)"
new = "    wechat_listen_batch_size: int = 0  # 监听轮每批号数:0=自适应(2026-10-01,按池子规模自动分批+沉睡号降频,扩建无需手调);非 0=固定批(旧行为);负数=回全量"
assert t.count(old) == 1
t = t.replace(old, new)
f.write_text(t, encoding="utf-8")
print("① settings 自适应默认")

# ② _listen.py:算法函数 + 接入
f = Path("app/services/wechat/_listen.py")
t = f.read_text(encoding="utf-8")

anchor = "def _renewal_cooldown"
if "def _select_listen_batch" not in t:
    fn = '''_DORMANT_MISS = 7      # 连续 N 轮确认未发文 → 沉睡降频(与前端"沉睡"口径一致)
_DORMANT_SKIP = 3      # 沉睡号每 3 轮参与 1 轮
_BATCH_MAX = 75        # 单轮额度安全线(75 号 ≈150 请求;微信读书会话预算实测 160~200)
_BATCH_MIN = 8


def _select_listen_batch(session: Session, user_id: int, all_rows: list,
                         settings, batch_index: int | None = None,
                         batch_size: int | None = None) -> tuple[list, str]:
    """自适应分批 + 权重分层(2026-10-01,为号池扩建准备)。返回 (本轮号列表, batch_pos)。

    - **批大小自适应**:clamp(ceil(有效池/定点数), 8, 75)——池子涨了自动调,每天尽量
      全覆盖;超 75 时天然"多日轮转"(每天 4×75=300 号,池 400 两天轮完一遍)。
    - **权重分层**:沉睡号(miss_count≥7)每 3 轮只参与 1 轮——额度花在"有产出的号"上。
    - 显式 batch_size/batch_index(测试/特殊用途)保持旧语义,不叠分层。
    - settings.wechat_listen_batch_size: 0=自适应;非 0=固定批(旧);负数=全量。
    """
    N = len(all_rows)
    if batch_size is not None:
        eff = batch_size
        if eff <= 0 or N <= abs(eff):
            return all_rows, ""
        n_groups = N // abs(eff) + (1 if N % abs(eff) else 0)
        idx = batch_index or 0
        start = idx % n_groups
        return (all_rows[start * abs(eff):(start + 1) * abs(eff)],
                f" batch={start + 1}/{n_groups}(size={abs(eff)},cursor={idx})")

    cfg = getattr(settings, "wechat_listen_batch_size", 0)
    if cfg < 0:                       # 逃生门:负数 = 全量
        return all_rows, ""
    if batch_index is None:
        batch_index = _advance_listen_cursor(session, user_id)

    active = [b for b in all_rows if (b.miss_count or 0) < _DORMANT_MISS]
    dormant = [b for b in all_rows if (b.miss_count or 0) >= _DORMANT_MISS]
    dormant_pick = dormant[batch_index % _DORMANT_SKIP::_DORMANT_SKIP] if dormant else []
    pool = active + dormant_pick

    K = 4  # 每日定点数(WECHAT_LISTEN_HOURS);每日覆盖预期 = K × B
    if cfg and cfg > 0:               # 固定批(旧行为):在全量池上轮转
        B = cfg
        pool = all_rows
        dormant_pick = []
    else:                             # 自适应:按有效池定批
        B = max(_BATCH_MIN, min(_BATCH_MAX, -(-len(pool) // K)))
    if B <= 0 or len(pool) <= B:
        return pool, (f" all(size={len(pool)})" if len(pool) < N else "")

    n_groups = len(pool) // B + (1 if len(pool) % B else 0)
    start = batch_index % n_groups
    rows = pool[start * B:(start + 1) * B]
    extra = f",active={len(active)},dormant={len(dormant)}/{_DORMANT_SKIP}轮" if dormant else ""
    return rows, f" batch={start + 1}/{n_groups}(size={B},cursor={batch_index}{extra})"


'''
    assert t.count(anchor) >= 1
    t = t.replace(anchor, fn + anchor, 1)

old_seg = '''    # 错峰分批(2026-09-29 用户决策:142 号分组轮换,一轮只测少数):
    # 未显式传 batch_size 时按 settings.wechat_listen_batch_size(默认 36)轮转——
    # 4/8/14/20 四定点 × 36 号 = 144 ≥ 142,每天恰好全覆盖一遍;
    # batch_index 未传时用 system_config 游标(wechat_listen_cursor_{uid})自动推进,
    # 调度/手动/重试三入口零改动即共享同一轮转序列。batch_size=0 关闭轮转回全量。
    rows = all_rows
    batch_pos = ""
    effective_bs = batch_size if (batch_size and batch_size > 0) else settings.wechat_listen_batch_size
    if effective_bs > 0 and len(all_rows) > effective_bs:
        n_groups = len(all_rows) // effective_bs + (1 if len(all_rows) % effective_bs else 0)
        if batch_index is None:
            batch_index = _advance_listen_cursor(session, user_id)
        start = batch_index % n_groups
        rows = all_rows[start * effective_bs:(start + 1) * effective_bs]
        # cursor=原始游标值(未取模):重试路径据此重跑**同一个失败组**,而不是推进到下一组
        batch_pos = f" batch={start + 1}/{n_groups}(size={effective_bs},cursor={batch_index})"'''
new_seg = '''    # 自适应分批 + 权重分层(2026-10-01,可随号池扩建自动适配;见 _select_listen_batch):
    # 批大小按池子规模自适应、沉睡号降频;cursor=原始游标值(重试路径据此重跑同一组)。
    rows, batch_pos = _select_listen_batch(session, user_id, all_rows, settings,
                                           batch_index=batch_index, batch_size=batch_size)'''
assert t.count(old_seg) == 1, "旧分批段未命中"
t = t.replace(old_seg, new_seg)
f.write_text(t, encoding="utf-8")
print("② 算法已接入")
