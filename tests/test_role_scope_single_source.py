"""归属判据**只有一份** —— 结构守卫(2026-11-23)。

## 它守的是什么
分体部署的「板块 → 归属侧」映射曾经有**三份副本**(`scheduler._SECTION_ROLE`、
`health.py::_SECTION_ROLE` 多一个 `pan`、`alert_service.check_health_stalls` 内联版),
三份当时**恰好一致**,靠的是人记得同时改三处。漏改任何一份都不会报错,只会:

  · 调度护栏漏改 ⇒ 两端**重复采同一个上游账号**(闲鱼"被挤爆啦"那类的形状);
  · 健康页/停摆告警漏改 ⇒ 对着**本实例不管的板块**天天报红灯(10-02、10-24 各踩过一次)。

⇒ 现在判据统一在 `app/services/role_scope.py`,本文件用 AST 把「谁还自带一份映射」查出来。

## 为什么用 AST 而不是 grep
本仓栽过「按子串查,被解释性注释打红」三次。这里查的是**真实的 dict 字面量**:
模块级模块常量(如 `SECTION_ROLE = {...}`)**或**函数体内的赋值 —— 只认 `{"某板块": "wechat"|"hotspot"}`
这种形状,注释与文档字符串天然不受影响。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

#: 唯一允许出现归属映射的地方(它就是这个测试要守的那份)。
_ALLOWED_FILE = APP / "services" / "role_scope.py"
#: 归属侧的取值 —— 只有这两个半字面量才说明"这是一张归属表"(避免误伤普通字典)。
_ROLE_VALUES = {"wechat", "hotspot"}


def _dict_literal_roles(node: ast.AST) -> set[str]:
    """这个字典字面量里**恰好像"板块→归属侧"**的那几项(不是这种形状就返回空)。"""
    if not isinstance(node, ast.Dict):
        return set()
    roles: set[str] = set()
    for k, v in zip(node.keys, node.values):
        if not (isinstance(k, ast.Constant) and isinstance(v, ast.Constant)):
            return set()
        key, val = k.value, v.value
        if not (isinstance(key, str) and isinstance(val, str)):
            return set()
        if val:                                # 归属侧必须是小写字面量,别的表(如标签表)不算
            roles.add(val)
    #: 只有**全部值都落在归属侧里**才算隐患 —— 例如 `_LABELS`(微博/闲鱼…)天然被排除。
    return roles if roles and roles <= _ROLE_VALUES and "hotspot" in roles else set()


def test_归属映射只有一份() -> None:
    """★ 除 `role_scope.py` 外,**任何模块都不许自带「板块→归属侧」的字典字面量**。

    真要加板块/改分工,只改 `role_scope.SECTION_ROLE`(以及需要时的 `KIND_SECTION`)。
    """
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        if path == _ALLOWED_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if _dict_literal_roles(node):
                offenders.append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', '?')}")
    assert not offenders, (
        "发现**自带一份归属映射**的模块 —— 映射必须只有 `app/services/role_scope.py` 一份"
        "(改分工时漏改任何一份都是静默故障):\n  " + "\n  ".join(offenders))


def test_role_scope_覆盖所有可调度板块() -> None:
    """★ 反过来:调度器认得**每一个**板块,归属表里都必须有它 ——

    漏登记的后果是 `section_allowed` 返回 True(向后兼容)⇒ **两台实例都会去采**,
    正是"未知板块不拦"这条兼容规则最容易咬人的地方。新增板块时这里会先红。
    """
    from app.services.role_scope import KIND_SECTION, SECTION_ROLE
    from app.services.schedule_service import SECTIONS

    missing = set(SECTIONS) - set(SECTION_ROLE)
    assert not missing, f"这些板块可以调度,但归属表里没有 ⇒ 两端都会采:{sorted(missing)}"

    #: kind 表指向的板块也必须是真板块(打错字会让 `kind_allowed` 永远放行)。
    bad = {k: v for k, v in KIND_SECTION.items() if v not in SECTION_ROLE}
    assert not bad, f"KIND_SECTION 指向了不存在的板块(会永远放行):{bad}"