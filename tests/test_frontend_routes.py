"""`api.js` × 后端路由 一致性守卫(2026-10-03)。

补的是**前两道守卫中间的缝**:`test_frontend_api_contract.py` 管 `api.js` ↔ Vue 视图,
`test_api_doc_routes.py` 管 `doc/API.md` ↔ 后端路由 —— 没人管 `api.js` ↔ 后端路由。
于是"视图调的方法确实在 `api.js` 里导出着,但那个方法指向一个**不存在的路由**"能一路放行,
用户点下去只拿到 404(2026-10-03 的「刷新阅读量」按钮就是这个形态)。

解析细节见 `scripts/check_frontend_routes.py` 的模块说明。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_frontend_routes as cfr  # noqa: E402


def test_every_api_js_call_hits_a_real_route() -> None:
    """`api.js` 里每个 `req(METHOD, path)` 都要命中后端真实注册的路由(含方法)。"""
    bad = cfr.reconcile()
    assert not bad, (
        "frontend/src/api.js 调用了后端不存在的路由(用户点下去会 404):\n  "
        + "\n  ".join(bad)
        + "\n修法:改 api.js 的路径,或补上后端路由 —— 别把这条测试整个跳过。"
    )


def test_parser_handles_the_four_shapes_in_api_js() -> None:
    """解析器必须认得出本项目实际用到的 4 种写法(否则守卫会静默放行)。"""
    cases = {
        "'/api/cookies'": "/api/cookies",
        # 模板串里的 ${} → 路径参数
        "`/api/wechat/articles/${id}/rewrite`": "/api/wechat/articles/{}/rewrite",
        # 中段拼接 + 尾段拼接
        "'/api/watch/' + section + '/analytics'": "/api/watch/{}/analytics",
        "'/api/watch/' + section": "/api/watch/{}",
        # 查询串要截断;末尾的请求体实参**不能**被当成路径参数
        "'/api/xianyu/market?days=' + (days || 30)": "/api/xianyu/market",
        "'/api/auth/login', body": "/api/auth/login",
        # 三元里再拼查询串,也不该污染路径
        "'/api/admin/users' + (q ? '?q=' + encodeURIComponent(q) : '')": "/api/admin/users",
    }
    for expr, want in cases.items():
        assert cfr._norm(cfr._to_path(expr) or "") == want, f"{expr} → 解析错了"


def test_parser_flags_a_fabricated_dead_route() -> None:
    """反向验证:守卫本身也得被守卫 —— 塞个假路由进去,解析器要能报出来。"""
    real = cfr.backend_routes()
    assert cfr._norm("/api/definitely-not-a-real-route") not in real
    assert cfr._norm(cfr._to_path("'/api/wechat/traffic/refresh', o") or "") not in real
