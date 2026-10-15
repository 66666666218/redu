"""公众号转存的**资源级去重**守卫(2026-10-16)。

## 背景(用户口径「相同的资源只需要保存一个」)

`_enrich_new_articles`(公众号新文后处理)转存前**只有盘链级复用**:
判据是 `pan_url` **相等**(批内 + 历史两条)。而盘商
「**同一资源换条分享链再发**」是常态(这句就写在 `quark_transfer` 的注释里)
⇒ 同一份 PDF 被几十篇文章带几十条**不同的**夸克链 ⇒ 判重**一条都不命中** ⇒ 转存几十次。

**实测(近 3 天,749 种归一化资源)**:
- 有**多份我方链**的 **82 种(10.9%)**、多存 **198 份**;
- 最多的一组:同一条资源 **34 份**。

## 修法

接上**三盘互通的统一入口** `reuse_if_have` ——
`quark_kouling` / `xunlei_kouling` / `xunlei_group` / `resource_presence` **四处早都接了,
只有这条路漏了**(同一族:统一入口建好了、有一个点没接上)。

⚠️ 这条守卫按 **AST 查真实调用**,不按子串 —— 解释性注释里**一定会**提到 `reuse_if_have`,
子串匹配会把正确的文档判红(本仓一天内栽过三次)。
"""
from __future__ import annotations

import ast
from pathlib import Path

_ENRICH = Path(__file__).resolve().parents[1] / "app/services/wechat/_enrich.py"


def _called_in(fn_name: str) -> set[str]:
    src = _ENRICH.read_text(encoding="utf-8")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == fn_name), None)
    assert fn is not None, f"找不到 `{fn_name}` —— 改名了?守卫要跟着改"
    return {c.func.id for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}


def test_公众号转存必须走三盘互通的统一入口() -> None:
    """★★★ 少了这一句,同一资源换条分享链就会被**再存一份**(实测 10.9% 的资源是这样)。"""
    assert "reuse_if_have" in _called_in("_enrich_new_articles"), (
        "公众号转存**没走三盘互通的统一入口** ⇒ 同一资源换条链就会再存一份。"
        "正确姿势:`from app.services.pan_discovery import reuse_if_have` 然后拿 `r.title` 问一次,"
        "命中就**只复用、不转存**")


def test_资源级复用必须放在盘链级复用之后() -> None:
    """⚠️ **纯增量**才安全:URL 相等那两条判据(批内/历史)的行为**一字不许动**,

    资源级判定只该在它们**都不命中**时才多问一次。顺序反了会把"同一枚链复用同一条"搅乱。
    """
    src = _ENRICH.read_text(encoding="utf-8")
    #: ⚠️ 标记要**专指** —— "资源级复用"这几个字在函数顶部(声明 `reused_have` 那里)也出现过,
    #: 拿它当锚点会量到错的位置(我第一版就这么栽了:比出来"资源级在历史复用之前")。
    i_url = src.find("② 历史复用")
    i_res = src.find("★★★ **资源级复用**")
    assert i_url != -1 and i_res != -1, "两处判重的注释标记都没找到 —— 代码结构变了?守卫要跟着改"
    assert i_url < i_res, "资源级复用跑到盘链级复用**前面**去了 ⇒ 那不再是纯增量"
