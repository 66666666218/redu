"""API 文档 × 真实路由 一致性守卫(2026-10-03)。

**为什么需要**:`doc/API.md` 里有一整段(§1.1–§5)写着 **9 条根本不存在的 `/api/v1/*` 路径** ——
是 v1.0 设计稿残留,实现时全改了名却没人回头改文档。这类漂移的害处很具体:**照文档写代码必错**,
而且不报错、不告警(你按 `/api/v1/runs` 发请求,只会拿到 404 然后怀疑自己)。

所以这里把"文档提到的路径"与"后端真实注册的路由"做**双向对照**(路径参数名归一化后比较)。

两处**已知且已注明**的例外(见 API.md §1.1 的对照表):
  ① §1.1–§5 的 9 条旧路径 —— 保留是为了留对照关系,文档里已明示"不存在、别照着写";
  ② `/api/v1/wx/*` —— 那是**外部服务 WeRSS** 的接口,不是我们的路由。
除这两类外,文档里再出现对不上的路径 → 本测试直接红。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_MD = ROOT / "doc" / "API.md"

# ① §1.1–§5 的旧路径(v1.0 设计稿残留,API.md 里已列对照表)
_LEGACY = {
    "/api/v1/trends/latest", "/api/v1/alerts/latest", "/api/v1/runs", "/api/v1/runs/latest",
    "/api/v1/xianyu/hot", "/api/v1/xianyu/runs", "/api/v1/xianyu/daily",
    "/api/v1/douhot/trends", "/api/v1/douhot/runs",
}
# ② 外部服务(WeRSS)的接口前缀 —— 不是我们的路由
_EXTERNAL_PREFIXES = ("/api/v1/wx/",)


def _norm(path: str) -> str:
    """路径参数名归一化:文档写 `{id}`、代码写 `{article_id}`,是同一个路由。"""
    return re.sub(r"\{[^}]*\}", "{}", path).rstrip("/") or "/"


def _real_routes() -> set[str]:
    from app.api import all_routers

    out: set[str] = set()
    for router in all_routers:
        for route in router.routes:
            p = getattr(route, "path", "")
            if p.startswith("/api"):
                out.add(_norm(p))
    return out


def _documented_paths() -> set[str]:
    doc = API_MD.read_text(encoding="utf-8")
    # 文档里的路径都写成反引号包起来的形式(`/api/...`)—— 只认这种,避免把散文里的词当路径
    raw = set(re.findall(r"`(/api/[A-Za-z0-9_\-{}/.]*[A-Za-z0-9_\-{}])`", doc))
    return {_norm(p) for p in raw if not p.endswith((".md", ".py"))}


def test_api_doc_paths_all_exist_as_real_routes() -> None:
    """文档提到的每个 `/api/...` 都要在后端真实注册(已知旧路径与外部服务除外)。"""
    real = _real_routes()
    doc = _documented_paths()
    legacy = {_norm(p) for p in _LEGACY}
    missing = sorted(
        p for p in doc
        if p not in real and p not in legacy
        and not any(p.startswith(pre) for pre in _EXTERNAL_PREFIXES)
    )
    assert not missing, (
        "doc/API.md 提到了后端不存在的路由(照着它写代码会 404):\n  "
        + "\n  ".join(missing)
        + "\n修法:改文档路径,或在测试的 _LEGACY/_EXTERNAL_PREFIXES 里按需登记并说明原因。"
    )


def test_legacy_paths_are_still_absent_and_documented_as_such() -> None:
    """那 9 条旧路径**必须仍然不存在**,且文档里仍明确写着"不存在" —— 防止有人当 bug"修回来"。

    它们已经改名成真实路径(见 §1.1 对照表)。若哪天它们真的被注册了,说明有人照旧文档加了路由,
    那是把错误固化;这条会拦住。
    """
    real = _real_routes()
    for p in _LEGACY:
        assert _norm(p) not in real, f"{p} 被注册了 —— 它是旧设计稿的路径,不该复活"
    doc = API_MD.read_text(encoding="utf-8")
    assert "一条都不存在" in doc and "别照着它们写代码" in doc, "§1.1 的废弃说明被删了?"


def test_guard_would_catch_drift() -> None:
    """反向验证:随便塞一个不存在的路径进去,上面那条守卫**要能红**。

    (守卫本身也得被守卫 —— 否则解析写坏了会静默放行。)
    """
    real = _real_routes()
    fake = _norm("/api/definitely-not-a-real-route")
    assert fake not in real
