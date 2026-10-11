# -*- coding: utf-8 -*-
"""小红书**签名模板版本号**守卫(2026-10-11)。

⚠️ **为什么要写这条**:`Cloxl/xhshow` 自带 `SIGNATURE_DATA_TEMPLATE` 是
**`x0="4.3.5", x4="object"`** —— 一个**过时的版本号**。而服务端会拿它判定客户端是否异常。
实测(**同一 cookie、同一时刻,只改这一处**):

    · 补丁前:`HTTP 461 + code 300011 当前账号存在异常`
    · 补丁后:`HTTP 200 + code -100 登录已过期`   ← **错误真的变了**

⇒ 我先前对用户说的"**不是代码能修的**"**说早了**:账号那半边是真的,但**代码这半边也确实是错的**。

这条测试同时守两件事:
1. **常量本身**(别哪天被"还原成库自带的值"——那是回退,不是修复);
2. **补丁真的接上了**(造出来的签名器 config 上必须是新模板;`search()` 不许再用裸 `Xhshow()`)。
   第 2 点是关键 —— 只写个常量不用,和没改是一样的,而**这种"改了但没接上"在本仓已栽过多次**。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from app.services import xhs_protocol_source as m

_SRC = io.open(Path(m.__file__), encoding="utf-8").read()


def test_builtin_template_is_not_the_stale_one():
    """内置模板必须是**现行**版本号,不能是库自带的 `4.3.5` / `x4="object"`。"""
    assert m.SIGN_TEMPLATE["x0"] == "4.4.3", f"x0 过时:{m.SIGN_TEMPLATE['x0']}"
    assert m.SIGN_TEMPLATE["x4"] == "", f"x4 过时:{m.SIGN_TEMPLATE['x4']!r}"
    assert m.SIGN_TEMPLATE["x1"] == "xhs-pc-web"


def test_signer_actually_carries_the_patched_template():
    """★ **补丁真的接上了** —— 造出来的签名器 config 上要是新模板,不是库默认。"""
    x = m._signer()
    got = getattr(x.config, "SIGNATURE_DATA_TEMPLATE", None)
    assert got is not None, "签名器上没有 SIGNATURE_DATA_TEMPLATE(库结构变了?)"
    assert dict(got) == m.SIGN_TEMPLATE, f"签名器仍带着旧模板:{got}"


def test_search_does_not_construct_bare_xhshow():
    """`search()` 里不许再出现裸 `Xhshow()` —— 那等于把补丁绕过去(静默回退)。"""
    assert not re.search(r"^\s*x = Xhshow\(\)", _SRC, re.M), \
        "search() 里出现了裸 `x = Xhshow()`,模板补丁被绕过了"


def test_template_override_merges():
    """`.env` 的 `xhs_sign_template` 能覆盖单项,其余沿用内置。"""

    class _S:
        xhs_sign_template = '{"x0": "4.5.0"}'

    got = m._template(_S())
    assert got["x0"] == "4.5.0" and got["x1"] == "xhs-pc-web" and got["x4"] == ""


def test_template_override_is_forgiving():
    """配错(不是 JSON / 不是对象 / 留空)**只回落内置**,绝不让签名器起不来。"""

    class _Bad:
        xhs_sign_template = "{不是 json"

    class _List:
        xhs_sign_template = '["4.4.3"]'

    class _Empty:
        xhs_sign_template = ""

    for s in (_Bad(), _List(), _Empty(), None):
        assert m._template(s) == m.SIGN_TEMPLATE, f"回落失败:{s}"


def test_non_json_bad_override_still_yields_a_working_signer():
    """★ 端到端:即使 `.env` 配烂了,签名器也必须**照造不误**(用内置模板)。"""

    class _Bad:
        xhs_sign_template = "!!!not json!!!"

    x = m._signer(_Bad())
    assert dict(x.config.SIGNATURE_DATA_TEMPLATE) == m.SIGN_TEMPLATE
