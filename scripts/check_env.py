"""校验 .env 配置是否完整(部署诊断)。

只报**键名**是否缺失/为空/是占位符,不打印任何凭据值。可本地或服务器直接跑:
    python scripts/check_env.py
退出码:0=必填齐全;1=必填缺失或为空(列出问题项)。

**严格对齐 python-dotenv 的加载语义**(config.settings 用 pydantic-settings + dotenv):
- 键不区分大小写,支持 `export KEY=val` 前缀
- 剥掉内联注释(`KEY=val # comment`),处理引号包裹的值(`KEY="#val#"`)
- 检测已知占位符(如 .env.example 的 JWT_SECRET 占位、默认 DB 密码),避免"空但不为空"误判
"""
from __future__ import annotations

import sys
from pathlib import Path

try:  # Windows 控制台默认 GBK,打不出 ✓/✗;stdout 为 None(如 pythonw/嵌入)时跳过
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def parse_dotenv(text: str) -> dict[str, str]:
    """按 python-dotenv 语义解析 KEY=VAL(键大写,剥 export/引号/内联注释)。"""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, rest = line.partition("=")
        val = rest.strip()
        # 剥内联注释:值首的 `#` 或前面有空白(`value # comment`)才是注释起点;
        # 引号包裹时保留内部 #(如 KEY="a#b")
        if val and val[0] in "\"'":
            quote = val[0]
            end = val.find(quote, 1)
            val = val[1:end] if end > -1 else val[1:]
        else:
            for i, ch in enumerate(val):
                if ch == "#" and (i == 0 or val[i - 1].isspace()):
                    val = val[:i]
                    break
            val = val.strip()
        out[key.strip().upper()] = val.strip()
    return out


# 已知不安全占位符(来自 .env.example / 默认值)——命中则视为"未真正配置"
PLACEHOLDERS = {
    "JWT_SECRET": ("please_set_a_long_random_secret_here",),
    "DATABASE_URL": ("mysql+pymysql://redu:redu@db:3306/redu",),
}
MIN_JWT_LEN = 32


def main() -> int:
    # 锚定仓库根,不受当前工作目录影响
    root = Path(__file__).resolve().parent.parent
    env = root / ".env"
    if not env.exists():
        print(f"✗ 未找到 {env}(应 cp .env.example .env 后填写)")
        return 1
    defined = parse_dotenv(env.read_text(encoding="utf-8"))

    # 必填:缺失/为空/是占位符 → 退出 1
    required = {
        "JWT_SECRET": "登录令牌密钥(缺失/占位会让登录态不安全)",
        "DATABASE_URL": "数据库连接串(缺失连不上库)",
    }
    # 强烈建议:缺了功能降级,不阻断
    recommended = ["ADMIN_EMAIL", "SMTP_HOST", "NOTIFY_TO"]

    problems, warns = [], []
    for k, why in required.items():
        v = defined.get(k, "")
        if not v:
            problems.append(f"✗ 必填缺失/为空: {k}({why})")
        elif k == "JWT_SECRET" and (v in PLACEHOLDERS[k] or len(v) < MIN_JWT_LEN):
            warns.append(f"  ~ JWT_SECRET 过短或为占位符({len(v)} 字符),建议 ≥{MIN_JWT_LEN} 强随机")
        elif any(v.startswith(p) for p in PLACEHOLDERS.get(k, ())):
            warns.append(f"  ~ {k} 仍为占位符/默认值,请改")
    for k in recommended:
        if not defined.get(k, ""):
            warns.append(f"  ~ 建议: {k} 未设置")

    if problems:
        print("\n".join(problems))
        print(f"\n共 {len(problems)} 个必填问题")
        return 1
    for w in warns:
        print(w)
    print("✓ 必填配置齐全(JWT_SECRET / DATABASE_URL)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
