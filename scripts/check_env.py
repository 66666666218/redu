"""校验 .env 配置是否完整(部署诊断)。

只报**键名**是否缺失/为空,不打印任何凭据值。可本地或服务器直接跑:
    python scripts/check_env.py
退出码:0=齐全,1=有缺失或空(列出问题项)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK,打不出 ✓/✗


def main() -> int:
    env = Path(".env")
    if not env.exists():
        print("✗ 未找到 .env(应 cp .env.example .env 后填写)")
        return 1
    text = env.read_text(encoding="utf-8")
    # 只取启用(非注释)的 KEY=VAL
    defined: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        defined[k.strip()] = v.strip()

    # 必填(缺失/空会让关键功能跑不起来)
    required = {
        "JWT_SECRET": "登录令牌密钥(缺失则每次重启登录态失效)",
        "DATABASE_URL": "数据库连接串(缺失连不上库)",
    }
    # 强烈建议(缺了功能降级,但建议配)
    recommended = {
        "ADMIN_EMAIL": "该邮箱注册即自动成为管理员",
        "SMTP_HOST": "邮件通知",
        "NOTIFY_TO": "告警收件人",
        "DOUHOT_COOKIE_FILE": None,        # 探测文件,可留
        "GOOFISH_COOKIE_FILE": None,
    }

    problems = []
    for k, why in required.items():
        if k not in defined:
            problems.append(f"✗ 必填缺失: {k}({why})")
        elif not defined[k]:
            problems.append(f"✗ 必填为空: {k}({why})")
    for k, why in recommended.items():
        if k not in defined:
            print(f"  ~ 建议: {k} 未设置({why})" if why else f"  ~ 建议: {k} 未设置")
        elif not defined[k]:
            print(f"  ~ 建议: {k} 为空" if why else f"  ~ 建议: {k} 为空")

    if problems:
        print("\n".join(problems))
        print(f"\n共 {len(problems)} 个必填问题")
        return 1
    print("✓ 必填配置齐全(JWT_SECRET / DATABASE_URL)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
