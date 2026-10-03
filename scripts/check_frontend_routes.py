"""前端 `api.js` 路径 × 后端真实路由 一致性检查(2026-10-03)。

用法:
    python scripts/check_frontend_routes.py     # 退出码 1 = 有对不上的调用

**为什么需要**:这是前两道守卫中间的那个缝。
  - `tests/test_frontend_api_contract.py` 管的是 **`api.js` ↔ Vue 视图**(方法有没有人用);
  - `tests/test_api_doc_routes.py` 管的是 **`doc/API.md` ↔ 后端路由**(文档别写错路径);
  - **没人管 `api.js` ↔ 后端路由**。
于是"视图调用了 `api.js` 里导出过的方法,而那个方法指向一个**根本不存在的路由**"这个组合,
两道守卫都放行 —— 用户点下去只会拿到一个 404,而代码里看不出来。

**它是怎么被发现的**(2026-10-03 全项目审查):公众号页那个「刷新阅读量(¥0.06/篇)」按钮,
`api.wechatTrafficRefresh` → `POST /api/wechat/traffic/refresh` —— **后端没有这条路由**。
dajiala 阅读采样 2026-09-29 已废弃(`scheduler.py` 里 `traffic_tick` 停用),按钮没跟着摘,
正是闲鱼那个"扫了不生效却显示成功"按钮的翻版。

**解析难点**:`api.js` 的路径是 JS 表达式拼出来的,不是字面量。要处理:
  - 模板串 `` `/api/wechat/articles/${id}/rewrite` ``      → `/api/wechat/articles/{}/rewrite`
  - 拼接   `'/api/watch/' + section + '/analytics'`        → `/api/watch/{}/analytics`
  - 查询串 `'/api/xianyu/market?days=' + (days || 30)`     → `/api/xianyu/market`
  - 请求体 `req('POST', '/api/auth/login', body)`          → 末尾那个 `body` **不是**路径变量
判据:`+` 拼上来的变量**只有紧跟在 `/` 之后**才算路径参数,否则是请求体实参。
(这条判据是踩着坑定下来的:先按"是变量就补 `{}`"写,把 `req('POST','/api/x', o)` 的 `o`
也当成了路径参数,一次报出 36 处假阳性。)
"""
from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_JS = ROOT / "frontend" / "src" / "api.js"

_LIT = re.compile(r"""^\s*(['"`])(.*)\1\s*$""", re.S)
_HOLE = "\x00"  # 模板串里的 `${...}` 占位(不能直接用 `{}`,会被下面的归一化再动一遍)


def backend_routes() -> dict[str, set[str]]:
    """后端真实注册的 `/api/*` 路由 → 支持的方法集合。"""
    sys.path.insert(0, str(ROOT))
    from app.api import all_routers

    out: dict[str, set[str]] = {}
    for router in all_routers:
        for route in router.routes:
            p = getattr(route, "path", "")
            if not p.startswith("/api"):
                continue
            out.setdefault(_norm(p), set()).update(
                m for m in (getattr(route, "methods", None) or []) if m != "HEAD")
    return out


def _norm(path: str) -> str:
    """归一化:去掉查询串、路径参数名、尾斜杠。"""
    path = path.strip().split("?")[0]
    path = path.replace(_HOLE, "{}")
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return path.rstrip("/") or "/"


def _split_top(expr: str) -> list[str]:
    """按**顶层**(括号外)的 `+` 与 `,` 切分 JS 表达式,字符串内部的运算符不算。"""
    parts, buf, depth, i = [], "", 0, 0
    while i < len(expr):
        c = expr[i]
        if c in "'\"`":  # 整段字面量原样抄走(含转义)
            quote, j, buf = c, i + 1, buf + c
            while j < len(expr) and expr[j] != quote:
                if expr[j] == "\\":
                    buf += expr[j:j + 2]
                    j += 2
                    continue
                buf += expr[j]
                j += 1
            buf += expr[j] if j < len(expr) else ""
            i = j + 1
            continue
        depth += (c in "([{") - (c in ")]}")
        if depth == 0 and c in "+,":
            if buf.strip():
                parts.append(buf)
            buf = ""
            i += 1
            continue
        buf += c
        i += 1
    if buf.strip():
        parts.append(buf)
    return parts


def _to_path(expr: str) -> str | None:
    """从 `req(METHOD, <expr>)` 的 `<expr>` 里解析出请求路径(归一化前)。"""
    segs: list[tuple[str, str]] = []
    for part in _split_top(expr):
        m = _LIT.match(part)
        # 模板串里的 ${...} 先换成占位符,免得嵌套引号干扰
        segs.append(("lit", re.sub(r"\$\{[^}]*\}", _HOLE, m.group(2))) if m else ("var",))
    start = next((n for n, s in enumerate(segs)
                  if s[0] == "lit" and s[1].lstrip().startswith("/api")), None)
    if start is None:
        return None
    path = segs[start][1]
    for n in range(start + 1, len(segs)):
        if "?" in path:  # 查询串开始 → 后面的都不算路径
            break
        if segs[n][0] == "var":
            if not path.endswith("/"):  # 不跟在 '/' 后的变量是请求体实参,不是路径参数
                break
            path += _HOLE
        else:
            path += segs[n][1]
    return path


def api_js_calls(src: str) -> list[tuple[str, str]]:
    """扫出 `api.js` 里所有 `req('METHOD', ...)` 调用的 (方法, 路径)。"""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"\breq\(\s*'(\w+)'\s*,\s*", src):
        depth, j = 1, m.end()
        while j < len(src) and depth:  # 括号配平,取到 req(...) 的完整实参
            depth += (src[j] == "(") - (src[j] == ")")
            j += 1
        path = _to_path(src[m.end():j - 1])
        if path:
            out.append((m.group(1), path))
    return out


def reconcile() -> list[str]:
    """返回所有"对不上后端"的调用说明(空列表 = 全部一致)。"""
    real = backend_routes()
    src = API_JS.read_text(encoding="utf-8")
    bad, seen = [], set()
    for method, raw in api_js_calls(src):
        shown = raw.replace(_HOLE, "${}")
        key = (method, _norm(raw))
        if key in seen:
            continue
        seen.add(key)
        normed = key[1]
        if normed not in real:
            near = difflib.get_close_matches(normed, real, n=2, cutoff=0.55)
            hint = ("最接近的是 " + "、".join(near)) if near else "没有任何相近路由"
            bad.append(f"{method:<7} {shown:<48} ← 路由不存在({hint})")
        elif method not in real[normed]:
            bad.append(f"{method:<7} {shown:<48} ← 路径在,但它只支持 "
                       f"{sorted(real[normed])}")
    return bad


def main() -> int:
    bad = reconcile()
    if not bad:
        print(f"OK:frontend/src/api.js 的调用与后端路由一致({len(api_js_calls(API_JS.read_text(encoding='utf-8')))} 处)")
        return 0
    print(f"发现 {len(bad)} 处 api.js 调用了后端不存在的路由(用户点下去会 404):")
    for line in bad:
        print("  " + line)
    print("\n修法:改 api.js 的路径,或补上后端路由 —— 别把这个测试列进白名单了事。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
