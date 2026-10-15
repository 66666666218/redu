"""小红书 **CDP 路**单测(2026-10-15)。

这条路与页面路最大的区别是:**我们接管的是「别人的浏览器」**。
所以这里最要紧的两条不是"结果对不对",而是**别把人家的东西弄坏**:

1. **绝不杀浏览器** —— 页面路开跑前会 `kill_stale_browsers()`;那条在这里会把
   **人正在用的浏览器**关掉。本模块连**导入**都不许有它。
2. **只关自己开的那个标签** —— 绝不用现成标签导航、绝不关别人的标签。

剩下的是"别骗人":连不上/被拦/未登录**都要抛错**,不许静默返回空
(那会被上层读成"小红书没热度",本仓反复栽过)。
"""
import ast
import os
import re
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import xhs_cdp_source as C  # noqa: E402
from app.services.xhs_page_source import CARD_SEL, EMPTY_SEL, XhsPageError  # noqa: E402

_SRC = Path(C.__file__).read_text(encoding="utf-8")


def _touched(src: str) -> set[str]:
    """这个模块**真的碰过**哪些名字(调用 + 导入)。

    ⚠️ 用 AST 而不是子串匹配:模块的**文档串里会提到** `kill_stale_browsers`
    (正是为了写清"绝不用它")—— 子串匹配会把那句说明当成真的调用了。
    **说明白某件事不做的文档,不该被自己的守卫打红。**
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[-1])
    return names


_TOUCHED = _touched(_SRC)


# --------------------------------------------------------------------- 替身

class _Attr:
    def __init__(self, v):
        self._v = v

    def get_attribute(self, name, timeout=None):  # noqa: ARG002
        return self._v


class _First:
    def __init__(self, v):
        self.first = _Attr(v)


class _Card:
    def __init__(self, href, text):
        self._href, self._t = href, text

    def locator(self, sel):  # noqa: ARG002
        return _First(self._href)

    def inner_text(self):
        return self._t


class _Items:
    def __init__(self, items):
        self._c = list(items)

    def count(self):
        return len(self._c)

    def nth(self, i):
        return self._c[i]


class _Mouse:
    def __init__(self):
        self.wheels = []

    def wheel(self, dx, dy):
        self.wheels.append((dx, dy))


class _Page:
    """够用的页面替身。⚠️ `gotos` 是给"别人的标签不许被导航"那条用的。"""

    def __init__(self, url="https://www.xiaohongshu.com/explore", title="首页",
                 body="正常内容", cards=(), empty=0, ua="Mozilla/5.0 Edg/155.0.0.0"):
        self.url, self._title, self._body = url, title, body
        self._cards, self._empty, self._ua = list(cards), empty, ua
        self.gotos: list[str] = []
        self.waits: list[int] = []
        self.closed = False
        self.mouse = _Mouse()

    def goto(self, url, **kw):  # noqa: ARG002
        self.gotos.append(url)
        self.url = url

    def wait_for_selector(self, sel, timeout=None):  # noqa: ARG002
        if not self._cards:
            raise TimeoutError("no cards")

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def title(self):
        return self._title

    def content(self):
        return "<html><body>ok</body></html>"

    def inner_text(self, sel):  # noqa: ARG002
        return self._body

    def locator(self, sel):
        return _Items(self._cards) if sel == CARD_SEL else _Items([None] * self._empty)

    def close(self):
        self.closed = True

    def evaluate(self, js):
        return self._ua if "userAgent" in js else False


class _Ctx:
    def __init__(self, pages):
        self.pages = list(pages)
        self.new_pages: list[_Page] = []

    def new_page(self):
        pg = _Page()
        self.new_pages.append(pg)
        self.pages.append(pg)
        return pg


class _Browser:
    def __init__(self, ctx):
        self.contexts = [ctx]
        self.closed = False

    def close(self):
        self.closed = True


class _PW:
    def __init__(self, br):
        class _C:
            def connect_over_cdp(_self, url):  # noqa: N805, ARG002
                return br

        self.chromium = _C()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, ctx, ok=True):
    """把 `sync_playwright` / `available` 换掉,返回那个 browser 替身。"""
    br = _Browser(ctx)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _PW(br))
    monkeypatch.setattr(C, "available", lambda *a, **k: (ok, "ok" if ok else "CDP 不可达(x)"))
    return br


def _pin(ctx, **kw) -> _Page:
    """把 `ctx.new_page` 钉死成返回一个**固定**替身(便于之后断言它的行为)。

    ⚠️ 必须**自己**往 `ctx.new_pages` 里塞 —— 不塞的话后面断言拿到的是空的
    (我第一版就漏了这一步,`new_pages[0]` 直接 IndexError)。
    """
    pg = _Page(**kw)

    def _f():
        ctx.new_pages.append(pg)
        return pg

    ctx.new_page = _f           # type: ignore[method-assign]
    return pg


class _S:
    """settings 替身。"""
    xhs_cdp_url = "http://127.0.0.1:9222"
    xhs_cdp_between_kw_ms = 15000


# ------------------------------------------------------- ① 别把人家的浏览器弄坏

class Test别碰别人的浏览器:
    def test_源码里不许调用或导入_杀浏览器(self) -> None:
        """★ `kill_stale_browsers()` 是**页面路**的收尾动作(清它自己上一轮残留的 Edge);

        在这里它会关掉**人正在用的那个浏览器**。所以连导入都不许有。
        """
        assert "kill_stale_browsers" not in _TOUCHED

    def test_源码里不许自己_launch(self) -> None:
        """★ 一旦由 Playwright 启动浏览器,`navigator.webdriver` 就变回 `True` ——

        这整条路的价值当场归零。所以只许 `connect_over_cdp`。
        """
        assert "launch_persistent_context" not in _TOUCHED
        assert "launch" not in _TOUCHED
        assert "connect_over_cdp" in _TOUCHED

    def test_源码里不许拿现成标签去导航(self) -> None:
        """接管的是别人的浏览器,`ctx.pages[0]` 很可能是**人正在看的页面**。"""
        assert re.search(r"pages\[0\][^\n]*\.goto", _SRC) is None

    def test_别人的标签既不被导航也不被关(self, monkeypatch) -> None:
        mine = _Page(title="我的读者")
        other = _Page(title="别人开着的页")
        ctx = _Ctx([mine, other])
        _wire(monkeypatch, ctx)

        C.search(["网盘资源"], settings=_S())

        for pg in (mine, other):
            assert pg.gotos == [], f"动了别人的标签:{pg.title()}"
            assert pg.closed is False, f"关了别人的标签:{pg.title()}"
        assert len(ctx.new_pages) == 1, "应该只开一个新标签"
        assert ctx.new_pages[0].closed is True, "自己开的那个标签应当关掉"

    def test_要收尾的标签若不属于我们_就不关(self) -> None:
        """防御性分支:万一日后有人改成复用现成标签,这一层必须拦住。"""
        pg = _Page()
        C._close_own_page(pg, [pg])
        assert pg.closed is False

    def test_不关标签时也要能收尾(self) -> None:
        pg = _Page()
        C._close_own_page(pg, [])
        assert pg.closed is True

    def test_断开_CDP_之后不许关掉浏览器(self, monkeypatch) -> None:
        """`browser.close()` 对 connected 的 browser 是**断开连接**(实测浏览器进程仍在),

        但这条语义很反直觉 ⇒ 用替身把它钉住:我们**确实调了** close,
        而"不关浏览器"靠的是"绝不 launch"那条(见上)。
        """
        ctx = _Ctx([_Page()])
        br = _wire(monkeypatch, ctx)
        C.search(["网盘资源"], settings=_S())
        assert br.closed is True, "应该断开 CDP 连接"


# ------------------------------------------------------------ ② 不许静默返回空

class Test不许静默返回空:
    def test_连不上要抛错并给出可执行修法(self, monkeypatch) -> None:
        _wire(monkeypatch, _Ctx([_Page()]), ok=False)
        with pytest.raises(XhsPageError) as e:
            C.search(["网盘资源"], settings=_S())
        assert "CDP 不可达" in str(e.value)

    def test_available_连不上时的提示要指向_launch(self) -> None:

        class _Bad:
            xhs_cdp_url = "http://127.0.0.1:1"          #: 1 端口必然连不上

        ok, why = C.available(_Bad())
        assert ok is False
        assert "tools/xhs_cdp.py launch" in why, "光说连不上没用,要给出下一步命令"

    def test_未登录要抛错而不是返回空(self, monkeypatch) -> None:
        ctx = _Ctx([])
        _wire(monkeypatch, ctx)
        _pin(ctx, body="登录后查看搜索结果")
        with pytest.raises(XhsPageError) as e:
            C.search(["网盘资源"], settings=_S())
        assert "登录" in str(e.value)

    def test_频率风控要抛错且标成_只需等(self, monkeypatch) -> None:
        ctx = _Ctx([])
        _wire(monkeypatch, ctx)
        _pin(ctx, body="请勿频繁操作,稍后重试")
        with pytest.raises(XhsPageError) as e:
            C.search(["网盘资源"], settings=_S())
        assert e.value.rate_limit is True
        assert e.value.needs_human is False, "频率冷却**不要人动手**,标错会让人去重登"

    def test_真没有结果时返回空_不抛错(self, monkeypatch) -> None:
        ctx = _Ctx([])
        _wire(monkeypatch, ctx)
        _pin(ctx, cards=(), empty=1)
        assert C.search(["网盘资源"], settings=_S()) == []


# ------------------------------------------------------------------ ③ 形状与低频

class Test形状与低频:
    def test_返回形状与页面路一致(self, monkeypatch) -> None:
        cards = [_Card("/explore/abc123", "标题A\n作者A\n10-01\n12"),
                 _Card("/explore/def456?xsec_token=zzz", "标题B\n作者B\n10-02\n3")]
        ctx = _Ctx([])
        _wire(monkeypatch, ctx)
        _pin(ctx, cards=cards)

        rows = C.search(["网盘资源"], settings=_S())
        assert len(rows) == 2
        assert set(rows[0]) == {"uid", "name", "url", "snippet", "pan_link", "keyword"}
        assert rows[0]["uid"] == "abc123"
        assert rows[1]["url"] == "https://www.xiaohongshu.com/explore/def456", \
            "xsec_token 是**会话级**令牌,不许进库"
        assert all(r["keyword"] == "网盘资源" for r in rows)

    def test_词间休眠有硬下限(self) -> None:
        class _Tiny:
            xhs_cdp_between_kw_ms = 1

        assert C.between_kw_ms(_Tiny()) >= C._MIN_BETWEEN_MS, \
            "配置写多小都不许低于下限 —— 这条路唯一的优势是「像人」"

    def test_多个词之间真的会休眠(self, monkeypatch) -> None:
        ctx = _Ctx([])
        _wire(monkeypatch, ctx)
        pg = _pin(ctx, cards=())
        C.search(["词一", "词二"], settings=_S())
        assert any(w >= C._MIN_BETWEEN_MS for w in pg.waits), "两个词之间没有休眠"

    def test_两个词不该各开一个标签(self, monkeypatch) -> None:
        ctx = _Ctx([])
        _wire(monkeypatch, ctx)
        _pin(ctx, cards=())
        C.search(["词一", "词二"], settings=_S())
        assert len(ctx.new_pages) == 1, "两个词应当**复用同一个标签**,不是各开一个"

    def test_空关键词直接返回(self, monkeypatch) -> None:
        ctx = _Ctx([])
        _wire(monkeypatch, ctx)
        assert C.search(["  ", ""], settings=_S()) == []


# -------------------------------------------------------------------- ④ 无头告警

class Test无头告警:
    def test_接管到无头浏览器要告警(self, monkeypatch) -> None:
        """无头 UA 里写着 `HeadlessChrome/` ⇒ 等于自报自动化,这条路就白走了。"""
        seen: list[str] = []
        monkeypatch.setattr(C.logger, "warning",
                            lambda msg, *a, **k: seen.append(str(msg) % a if a else str(msg)),
                            raising=False)
        ctx = _Ctx([_Page(ua="Mozilla/5.0 HeadlessChrome/155.0.0.0 Safari/537.36")])
        C._warn_if_headless(ctx)
        assert seen and any("无头" in m for m in seen)

    def test_有头就不告警(self, monkeypatch) -> None:
        seen: list[str] = []
        monkeypatch.setattr(C.logger, "warning",
                            lambda msg, *a, **k: seen.append(str(msg)), raising=False)
        C._warn_if_headless(_Ctx([_Page(ua="Mozilla/5.0 Edg/155.0.0.0")]))
        assert seen == []
