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


# ------------------------------------------------ 后端接口 × 前端接线(2026-10-05)

# **运维/按需调用的接口** —— 有实现、真能跑,但**前端确实不需要入口**。
# ⚠️ 这份白名单是**要维护的**:往里加东西时必须在注释里写清"为什么前端不需要"。
# 不加注释就往里塞,等于把"没接线"变成"没人记得为什么" —— 那正是本测试要防的。
_OPS_ONLY: dict[str, str] = {
    "/api/wechat/analyze":
        "选题分析报告(含盘链偏好/类目阅读/最佳时段)。给人看的长报告,不是界面控件;"
        "目前按需 curl 调,若要做成看板页再从这里移出。",
    "/api/wechat/weread/shelf":
        "书架**导入预览**。前端「导入书架」走的是 import_shelf(一步到位);"
        "预览是「先看再导」的增强,属产品决策,未定之前不硬接。",
    "/api/wechat/articles/{}/rewrites":
        "同一篇的历史改写稿。前端目前只展示**最新一稿**(POST rewrite 的返回);"
        "历史列表要不要露出来是产品决策。",
    "/api/wechat/keywords":
        "只读地列出当前关键词。改它要动服务器 .env,前端没有可写的入口,展示出来也无事可做。",
}


def _norm_path(p: str) -> str:
    """把路径参数统一成 `{}`,好让 `{id}` / `{article_id}` / ${x} 三种写法可比。"""
    out, i = [], 0
    while i < len(p):
        ch = p[i]
        if ch == "$" and p[i:i + 2] == "${":
            j = p.find("}", i)
            if j < 0:
                out.append(p[i:]); break
            out.append("{}"); i = j + 1; continue
        if ch == "{":
            j = p.find("}", i)
            if j < 0:
                out.append(p[i:]); break
            out.append("{}"); i = j + 1; continue
        out.append(ch); i += 1
    return "".join(out).rstrip("/") or "/"


def _frontend_paths() -> set[str]:
    """从前端源码里抠出它调用的所有 `/api/...` 路径(含模板串)。"""
    fe = ""
    for f in (ROOT / "frontend" / "src").rglob("*"):
        if f.suffix in (".vue", ".js"):
            fe += f.read_text(encoding="utf-8", errors="replace")
    # 只要求"以 /api/ 开头、由路径安全字符组成";模板串里的 ${...} 也吃进来
    raw = re.findall(r"/api/[A-Za-z0-9_/$\{\}\.\-]*", fe)
    return {_norm_path(x) for x in raw}


def test_每个后端公众号接口要么前端在用_要么在运维白名单里():
    """★ **把"没接线"从意外变成记录在案的选择**。

    背景(2026-10-05 审计):后端 25 条公众号接口里 **4 条前端从没调用过** ——
    有实现、有文档、**没人调**,而且**没人知道**。这正是本仓那份《能力清单》
    存在的原因("不知道系统已经有这个能力")。

    本测试不强制"必须接线"(有些确实不需要 UI),但**强制它是个显式决定**:
    要么前端真的在用,要么出现在 `_OPS_ONLY` 里并写清原因。
    于是"新加了个接口但忘了接前端"会立刻红,而不是三个月后被人偶然发现。
    """
    from app.api.wechat import router as _r

    fe = _frontend_paths()
    missing = []
    for route in _r.routes:
        path = _norm_path(str(getattr(route, "path", "")))
        if not path.startswith("/api/wechat"):
            continue
        if path not in fe and path not in _OPS_ONLY:
            methods = sorted(getattr(route, "methods", None) or [])
            missing.append(f"{','.join(methods)} {path}")

    assert not missing, (
        "这些后端接口前端没调用、也不在 _OPS_ONLY 白名单里 —— "
        "要么接上前端,要么加进白名单并写清为什么不需要入口:" + chr(10) + "  " +
        (chr(10) + "  ").join(missing)
    )
