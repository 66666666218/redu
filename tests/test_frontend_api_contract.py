"""前端调用契约守卫:**视图里用的 `api.X(...)` 必须是 `api.js` 真的导出的方法**。

**为什么需要它**(2026-10-03):同一个坑**踩了两次** ——
  · 2026-10-01:`api.post(...)` 是 axios 风格调用,而 `api.js` 从来没有 `post` →
    扫码登录/生成文案/候选收录三个按钮一直抛 `TypeError`(修法:加 `post` 兼容层);
  · 2026-10-03 审查发现:**只修了 `post`,漏了 `get`** —— 12 处 `api.get(url, {params})`
    分布在 6 个页面(健康页/资源库/热榜/迅雷群/跨平台号/Cookies 扫码),全部整页失效,
    且已在生产构建里。两次的共同点:**调用方与导出方之间没有任何静态检查**。
  · 所以这里补上:视图里出现 `api.<name>(`,而 `api.js` 没导出 `<name>` → **测试直接红**。

⚠️ 本测试**静态解析**,不构建前端 —— 所以它能拦住"名字不存在",拦不住"名字对但用法错"
(比如 `{data}` 包装与否)。后者只能靠人。
"""
import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "src"
API_JS = FRONTEND / "api.js"


def _strip_comments(src: str) -> str:
    """去掉 `//` 与 `/* */` 注释 —— 否则注释里提到的 `api.get(...)` 会造成误报。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _exported_methods() -> set[str]:
    """从 `export const api = { ... }` 里抠出方法名(缩进两格的 `name: ` 形式)。"""
    src = _strip_comments(API_JS.read_text(encoding="utf-8"))
    m = re.search(r"export const api\s*=\s*\{(.*?)\n\}", src, re.S)
    assert m, "没找到 `export const api = {` —— api.js 结构变了,请更新本测试"
    names = set(re.findall(r"^\s{2}(\w+)\s*:", m.group(1), re.M))
    assert len(names) >= 20, f"只解析出 {len(names)} 个方法,明显不对(解析写坏了?)"
    return names


def _used_methods() -> dict[str, list[str]]:
    """扫前端源码里所有 `api.<name>(` 的用法,记下每个名字出现在哪些文件。"""
    used: dict[str, list[str]] = {}
    files = list(FRONTEND.rglob("*.vue")) + list(FRONTEND.rglob("*.js"))
    for f in files:
        if f == API_JS:
            continue
        src = _strip_comments(f.read_text(encoding="utf-8"))
        for name in re.findall(r"\bapi\.(\w+)\s*\(", src):
            used.setdefault(name, []).append(str(f.relative_to(FRONTEND.parent.parent)))
    return used


def test_frontend_only_calls_exported_api_methods() -> None:
    """视图里用的每个 `api.X` 都要在 `api.js` 里真的存在。"""
    exported = _exported_methods()
    used = _used_methods()
    missing = {n: files for n, files in used.items() if n not in exported}
    detail = "\n".join(f"  api.{n}() —— 被 {', '.join(sorted(set(f))[:3])} 调用" for n, f in missing.items())
    assert not missing, (
        "前端调了 api.js 没导出的方法(会整页抛 TypeError):\n" + detail +
        "\n修法:在 api.js 的 `export const api = {...}` 里补上,或改调用方用已有方法。"
    )


def test_api_get_compat_layer_exists() -> None:
    """`get` 兼容层必须存在 —— 12 处 axios 风格 `api.get(url,{params})` 依赖它。

    这条单独钉住,是因为它**已经被漏修过一次**(2026-10-01 只修了 post)。
    """
    assert "get" in _exported_methods(), "api.js 的 api 对象里必须有 get(兼容层)"


def test_no_unused_exported_api_methods() -> None:
    """⚠️ **反向守卫**:`api.js` 里声明了、但前端任何地方都不调用的方法 —— 不许堆积。

    2026-10-03 清掉 10 个(`crossRising`/`watchList`/`xianyuDaily`/`douhotWatchList`/
    `wechatShelf`/`wechatArticleTraffic`/`wechatArticleRewrites`/`trending`/`quarkShares*`)。
    它们对应的**后端接口都还活着**(`doc/API.md` 记着),只是**前端没入口** ——
    留着的唯一效果是让下一个人以为"这些功能在界面上有"。

    (与 `test_frontend_only_calls_exported_api_methods` 是一对:那条管"调了不存在的方法",
    这条管"声明了没人调的方法"。两条都红不了,前端契约才算自洽。)
    """
    exported = _exported_methods()
    used = set(_used_methods())
    # 兼容层是给"外部/早期写法"备的,不算死代码
    keep = {"get", "post"}
    unused = sorted(n for n in exported if n not in used and n not in keep)
    assert not unused, (
        "api.js 里这些方法前端从不调用(死代码):\n  " + "\n  ".join(unused) +
        "\n修法:删掉它们;真要预留就在本测试的 keep 里登记并写清为什么。"
    )
