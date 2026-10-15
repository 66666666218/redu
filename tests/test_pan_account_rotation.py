"""网盘**多账号轮换**的守卫(2026-10-16,用户口径「先跑通多账户轮换」)。

## 背景

`pan_accounts` 的池子早就有了(`system_config['pan_account_pool']`),`xunlei_group`
也接了"挑最空的 → 满了换下一个"。**但夸克/百度的取号从来没接池** ——
取这三个盘 cookie 的地方**八处各写一遍**:

    夸克 5 处:`wechat/_source._quark_cookie` / `pan_auto_clean` / `pan_discovery` /
               `quark_kouling` / `hotspot_agent`
    百度 3 处:`pan_auto_clean` / `pan_discovery` / `wechat/_enrich`

⇒ **没有一处看池** ⇒ 账号登记了也不被用,"满了自动换下一个"是句空话。
⇒ 已全部收口到 `pan_accounts.cookies_for()` / `cookie_for()`。

⚠️ 这条守卫按 **AST 查真实调用**,不按子串 —— 解释性注释里会提到那些写法
(本仓一天内为"子串守卫被自己的注释打红"栽过三次)。
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SERVICES = _ROOT / "app" / "services"
_PLATFORMS = {"quark", "baidupan", "baidu"}


def _direct_reads() -> list[tuple[str, int]]:
    """扫出**绕过池**直接按平台名取 cookie 的调用(文件名, 行号)。"""
    bad: list[tuple[str, int]] = []
    for f in sorted(_SERVICES.rglob("*.py")):
        if f.name == "pan_accounts.py":          #: 池自己当然要读库
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "get_cookie"):
                continue
            #: `get_cookie(session, user_id, platform)` —— 平台名是**第三个**参数
            for a in node.args[2:3] + [k.value for k in node.keywords if k.arg == "platform"]:
                if isinstance(a, ast.Constant) and a.value in _PLATFORMS:
                    bad.append((str(f.relative_to(_ROOT)), node.lineno))
    return bad


def test_三个盘的取号必须走统一入口() -> None:
    """★★ 绕过池 ⇒ 多账号登记了也不会被用(2026-10-16 实测:8 处全绕)。

    统一入口是 `pan_accounts.cookies_for()`(列表,最空的在前)和 `cookie_for()`(单个)。
    """
    bad = _direct_reads()
    assert not bad, (
        "这些地方**绕过账号池**直接按平台名取 cookie ⇒ 多账号不会生效:\n  "
        + "\n  ".join(f"{f}:{n}" for f, n in bad)
        + "\n改用 `pan_accounts.cookie_for(session, '<平台>', user_id=…)`"
        "(要'满了换下一个'就用 `cookies_for()` 拿整个列表循环)")


class Test池取号:
    def _session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db import models  # noqa: F401
        from app.db.database import Base
        from app.db.models import User

        eng = create_engine("sqlite://")
        Base.metadata.create_all(eng)
        db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
        db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
        db.commit()
        return db

    def test_池没配时与老路径逐字一致(self) -> None:
        """★ **单账号行为不变** —— 这是整套改动的前提,破了它就是回归。"""
        from app.services import pan_accounts as A
        from app.services.cookie_store import set_cookie

        db = self._session()
        try:
            set_cookie(db, 1, "quark", "ctoken=OLD")
            db.commit()
            assert A.cookie_for(db, "quark", user_id=1) == "ctoken=OLD"
            assert A.cookies_for(db, "quark", user_id=1) == [("quark(默认)", "ctoken=OLD")]
        finally:
            db.close()

    def test_池里两个号都要拿得到_且没探过的排最前(self) -> None:
        """★ 排序的关键一条:**刚加进来、还没探过使用率的号必须最先试** ——

        它正是"盘满了才加的那个号"。⚠️ 原来 `usable_hint` 把"未知"记成 `1.0`,
        而实测比例是 `0.9x` ⇒ **0.9x < 1.0** ⇒ 90% 满的老号反而排在它前面
        ⇒ 新号**永远轮不到**(2026-10-16 修)。
        """
        from app.services import pan_accounts as A

        db = self._session()
        try:
            A.add_account(db, "quark", "夸克1", "ctoken=OLD", cookie_key="quark")
            A.add_account(db, "quark", "夸克2", "ctoken=NEW", cookie_key="quark#2")
            #: 老号探到 90% 满;新号没探过
            ref = next(r for r in A.accounts(db, "quark", user_id=1) if r.label == "夸克1")
            A.report(db, ref, ratio=0.90)
            assert [c for _, c in A.cookies_for(db, "quark", user_id=1)] == \
                ["ctoken=NEW", "ctoken=OLD"], "没探过的新号没有被优先试 —— 多账号等于白加"
        finally:
            db.close()

    def test_取不到的那一枚不许挡住后面的(self) -> None:
        """一枚坏凭据(解不开/空)不该让整条链拿不到号。"""
        from app.services import pan_accounts as A
        from app.services.cookie_store import set_cookie

        db = self._session()
        try:
            set_cookie(db, 1, "quark#2", "ctoken=NEW")
            A.add_account(db, "quark", "夸克1", "", cookie_key="quark")     #: 空
            A.add_account(db, "quark", "夸克2", "ctoken=NEW", cookie_key="quark#2")
            assert [c for _, c in A.cookies_for(db, "quark", user_id=1)] == ["ctoken=NEW"]
        finally:
            db.close()


# ---------------------------------------------------------------------------
# ★★★ 配额探针必须**按账号**各查各的(2026-10-16)
#
# 实测:两个**不同**的迅雷号(`sub` 1050028885 / 1579917402)探配额返回
# **一模一样**的 `usage/limit` —— 不同账号不可能同值。根因**两层**:
#   ① `_fresh_cred(cred)` 漏传 `cookie_key` ⇒ **永远读默认账号** ⇒ 请求发的是 1 号的身份;
#   ② 配额缓存是**进程级单份** ⇒ 探 1 号的结果被当成 2 号的。
# 后果很实在:"挑最空的"**失真**、盘满闸门**拿空号的余量放行满号**。
# (修完实测:1 号 91.6% 满,而**新加的 2 号只有 0.74%** —— 那个 bug 让我们看不见这件事。)


def test_配额探针必须按账号传凭据() -> None:
    """★ 源码级:`_fresh_cred` 调 `_credentials` 时**必须带 `cookie_key`**。

    漏了它就是"拿 A 号的凭据去问、用的是 B 号的身份" —— 而且**不报错**,
    只是数字悄悄变成别人的(本仓母题:静默失败=假成功)。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app/services/xunlei_transfer.py"
           ).read_text(encoding="utf-8")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "_fresh_cred"), None)
    assert fn is not None, "找不到 `_fresh_cred` —— 改名了?守卫要跟着改"
    kws = {k.arg for c in ast.walk(fn) if isinstance(c, ast.Call) for k in c.keywords if k.arg}
    assert "cookie_key" in kws, (
        "`_fresh_cred` 没把 `cookie_key` 传下去 ⇒ 它会**永远读默认账号**的凭据")


def test_两个账号的配额缓存不许共用(monkeypatch) -> None:
    """★ 缓存按 `cookie_key` 分键 —— 否则第二次调用直接命中第一次的结果。"""
    from app.services import xunlei_transfer as xt

    monkeypatch.setattr(xt, "_credentials",
                        lambda settings=None, *, cookie_key="xunlei": {"sub": f"sub-{cookie_key}"})
    seen: list[str] = []

    def _probe(cred=None, cookie_key="xunlei"):
        seen.append(xt._quota_key(cred))
        return {"usage": len(seen), "limit": 100, "ratio": len(seen) / 100}

    monkeypatch.setattr(xt, "_probe_quota", _probe)
    xt.reset_quota_cache()
    a = xt.quota_info(cookie_key="xunlei")
    b = xt.quota_info(cookie_key="xunlei#2")
    assert a != b, f"两个账号拿到了同一份配额:{a} —— 缓存没按账号分键"
    #: 再问一次应当**命中缓存**(不再真打)
    xt.quota_info(cookie_key="xunlei")
    assert len(seen) == 2, f"缓存没生效(真打了 {len(seen)} 次)"
